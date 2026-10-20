"""Recipe panel. Recipes run in the sequencer service: this panel picks a recipe file, asks the
sequencer to start / pause / resume / stop it, answers its operator prompts, and shows its
progress from telemetry - so a recipe keeps running if the HMI is closed, and the panel picks
it up again when the HMI comes back.

LOAD checks the recipe as the sequencer will before it starts it (src/core/recipe_check.py) and
shows what it finds: with an error, START stays off (the sequencer would refuse it anyway); a
warning is shown, and the recipe can still be started. The steps are listed as sentences
(src/core/recipe_text.py). The recipe builder opens beside the HMI (it isn't modal); a recipe
saved there is listed, and loaded again here if it is the one loaded."""
import logging
import os

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (QComboBox, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
                             QVBoxLayout, QWidget)

from src.core.recipe_catalog import shared
from src.core.recipe_check import check_recipe, errors, summary, warnings
from src.core.recipe_engine import Recipe, RecipeError
from src.core.recipe_files import RECIPES_DIR, migrate_legacy_recipes
from src.gui import theme
from src.gui.control.dialogs.recipe_builder import RecipeBuilderDialog
from src.gui.control.sequencer_link import OFFLINE_TEXT, SequencerSlot
from src.gui.style import chip, set_prop, set_role, set_tone
from src.sequencer import protocol as P

log = logging.getLogger("hmi.recipes")

# Step rows: (background, text) theme colours
STEP_COLOURS = {"DONE": ("ok_bg", "ok"), "FAILED": ("fault_bg", "fault"), "RUNNING": ("seq_bg", "seq"),
                "WAITING_FOR_USER": ("warn_bg", "warn")}
# The status line: violet while the sequence runs (it holds controls), amber when it waits for the
# operator, red when it failed
STATE_TONES = {P.RUNNING: "seq", P.PROMPT: "warn", P.PAUSED: "warn", P.COMPLETE: "ok",
               P.STOPPED: "off", P.FAILED: "fault"}


class RecipeExecutionWidget(QWidget):
    def __init__(self, cmd_thread):
        super().__init__()
        self.slot = SequencerSlot(cmd_thread, P.RECIPE)
        # config/recipes (in git). This used to resolve to src/config/recipes, which isn't in git:
        # anything saved there is copied across once (never overwriting).
        self.config_dir = RECIPES_DIR
        os.makedirs(self.config_dir, exist_ok=True)
        self.migrated_recipes = migrate_legacy_recipes()
        self.loaded_file = None           # recipe START runs (picked with LOAD, or the last run seen)
        self.preview = None               # its steps, read here for display before it runs
        self.load_errors = []             # what the checks found wrong with it (START stays off)
        self._builders = {}               # file (None: a new recipe) -> the open recipe builder
        self._tickets = {}                # "start" / "pause" / "ack" ... -> ticket of the last click
        self._ack_refusal = None          # (prompt number, reason) of a refused acknowledgement
        self._preview_mode = False        # show the loaded recipe rather than the last run
        self._awaiting_run = None         # run number of a start the sequencer accepted, until seen
        self._rendered = None
        self._init_ui()

    # ------------------------------------------------------------------ UI
    def _init_ui(self):
        layout = QVBoxLayout(self)

        self.btn_builder = QPushButton("NEW RECIPE")
        self.btn_builder.setToolTip("Open the recipe builder on a new recipe: empty, or from a template (New).")
        self.btn_builder.clicked.connect(lambda: self._open_builder(None))
        layout.addWidget(self.btn_builder)

        file_layout = QHBoxLayout()
        self.cb_recipes = QComboBox()
        self._populate_recipes()
        self.btn_refresh = QPushButton("↻")
        self.btn_refresh.setFixedWidth(40)
        self.btn_refresh.setToolTip("Rescan folder for new JSON files.")
        self.btn_refresh.clicked.connect(self._populate_recipes)
        self.btn_load = QPushButton("LOAD RECIPE")
        self.btn_load.setToolTip("Check the selected recipe and show its steps, ready to start.")
        self.btn_load.clicked.connect(lambda: self._load_recipe())
        self.btn_edit = QPushButton("EDIT")
        self.btn_edit.setToolTip("Open the selected recipe in the recipe builder.")
        self.btn_edit.clicked.connect(self._edit_recipe)
        for w, stretch in ((self.cb_recipes, 1), (self.btn_refresh, 0), (self.btn_load, 0), (self.btn_edit, 0)):
            file_layout.addWidget(w, stretch=stretch)
        layout.addLayout(file_layout)

        ctrl_layout = QHBoxLayout()
        self.btn_start = QPushButton("▶ START")
        self.btn_start.clicked.connect(self._start_recipe)
        self.btn_pause = QPushButton("⏸ PAUSE")
        self.btn_pause.clicked.connect(self._pause_recipe)
        # STOP only stops stepping through the recipe; it does not switch anything off or revert
        # setpoints (outputs stay as the completed steps left them).
        self.btn_abort = QPushButton("⏹ STOP")
        self.btn_abort.clicked.connect(self._abort_recipe)
        for b in (self.btn_start, self.btn_pause, self.btn_abort):
            ctrl_layout.addWidget(b)
        layout.addLayout(ctrl_layout)

        # Operator prompt: answered here, checked by the sequencer
        self.prompt_frame = QFrame()
        self.prompt_frame.setObjectName("RecipePrompt")
        set_role(self.prompt_frame, "callout")
        set_tone(self.prompt_frame, "warn")
        prompt_layout = QVBoxLayout(self.prompt_frame)
        self.lbl_prompt = QLabel()
        self.lbl_prompt.setWordWrap(True)
        set_role(self.lbl_prompt, "title")
        self.lbl_prompt_feedback = QLabel()
        self.lbl_prompt_feedback.setWordWrap(True)
        set_role(self.lbl_prompt_feedback, "error")
        prompt_buttons = QHBoxLayout()
        self.btn_ack = QPushButton("✔ ACKNOWLEDGE")
        set_tone(self.btn_ack, "primary")
        self.btn_ack.setToolTip("Confirm the action is done. The recipe checks its conditions (live values) first.")
        self.btn_ack.clicked.connect(self._acknowledge)
        self.btn_prompt_stop = QPushButton("⏹ STOP RECIPE")
        set_tone(self.btn_prompt_stop, "danger")
        self.btn_prompt_stop.setToolTip("Stop the recipe here (nothing is switched off).")
        self.btn_prompt_stop.clicked.connect(self._abort_recipe)
        prompt_buttons.addWidget(self.btn_ack)
        prompt_buttons.addWidget(self.btn_prompt_stop)
        prompt_head = QLabel("Recipe action required")
        set_role(prompt_head, "heading")
        prompt_layout.addWidget(prompt_head)
        prompt_layout.addWidget(self.lbl_prompt)
        prompt_layout.addWidget(self.lbl_prompt_feedback)
        prompt_layout.addLayout(prompt_buttons)
        self.prompt_frame.hide()
        layout.addWidget(self.prompt_frame)

        # What the checks found in the loaded recipe
        self.lbl_check = QLabel()
        self.lbl_check.setObjectName("RecipeCheck")
        self.lbl_check.setWordWrap(True)
        set_role(self.lbl_check, "note")
        self.lbl_check.hide()
        layout.addWidget(self.lbl_check)

        lbl_steps = QLabel("Steps")
        set_role(lbl_steps, "caption")
        layout.addWidget(lbl_steps)
        self.list_steps = QListWidget()
        self.list_steps.setWordWrap(True)
        self.list_steps.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.list_steps.setMinimumHeight(90)
        font = QFont(self.list_steps.font())
        font.setPointSizeF(font.pointSizeF() + 1)
        self.list_steps.setFont(font)
        layout.addWidget(self.list_steps, stretch=1)

        self.lbl_status = chip("STATUS: IDLE", "off")
        self.lbl_status.setWordWrap(True)
        layout.addWidget(self.lbl_status)
        self._set_buttons(start=False, pause=False, stop=False)
        # The step colours are painted per row: draw them again in the new theme
        theme.theme_changed().connect(self._repaint_steps)

    def _set_buttons(self, start: bool, pause: bool, stop: bool, paused: bool = False, start_tip: str = ""):
        self.btn_start.setEnabled(start)
        set_tone(self.btn_start, "primary")
        self.btn_start.setToolTip(start_tip or ("Run the loaded recipe from step 1." if start else
                                                "Load a recipe first (and the sequencer must be idle)."))
        self.btn_pause.setEnabled(pause)
        self.btn_pause.setText("▶ RESUME" if paused else "⏸ PAUSE")
        set_tone(self.btn_pause, "primary" if paused else None)
        self.btn_pause.setToolTip("Resume (the current step carries on, or starts again if control was taken)."
                                  if paused else "Suspend execution and freeze all timers.")
        self.btn_abort.setEnabled(stop)
        set_tone(self.btn_abort, "danger")
        self.btn_abort.setToolTip("Stop stepping through the recipe now. Nothing is switched off or reverted: "
                                  "outputs stay as the completed steps left them." if stop else
                                  "Stop is only available while a recipe is running.")

    # ------------------------------------------------------------------ files
    def _populate_recipes(self):
        current = self.cb_recipes.currentText()
        self.cb_recipes.clear()
        if os.path.isdir(self.config_dir):
            self.cb_recipes.addItems(sorted(f for f in os.listdir(self.config_dir) if f.endswith(".json")))
        if current:
            self.cb_recipes.setCurrentText(current)

    def select_recipe(self, filename: str):
        self._populate_recipes()
        self.cb_recipes.setCurrentText(filename)

    def _load_recipe(self, filename: str = None):
        """Check the recipe as the sequencer will before it starts it, and show its steps."""
        filename = filename or self.cb_recipes.currentText()
        if not filename:
            return
        path = os.path.join(self.config_dir, filename)
        try:
            preview = Recipe(path)
        except (OSError, RecipeError) as e:      # (RecipeError covers every fault in the file itself)
            self.preview, self.loaded_file, self.load_errors = None, None, []
            self._preview_mode = False
            self._rendered = None
            self.list_steps.clear()
            self._show_check([], f"{filename} can't be run: {e}")
            self.lbl_status.setText(f"NOT LOADED: {filename} - {e}")
            set_tone(self.lbl_status, "fault")
            return
        problems = check_recipe(preview.data, shared(), self.config_dir, path)
        self.preview = preview
        self.loaded_file = filename
        self.load_errors = errors(problems)
        self._preview_mode = True
        self._rendered = None
        self._render_preview()
        self._show_check(problems)
        self._show_ready()

    def _show_ready(self):
        if self.preview is None:
            return
        if self.load_errors:
            self.lbl_status.setText(f"NOT READY: {self.preview.name} (version {self.preview.version}) has errors")
            set_tone(self.lbl_status, "fault")
        else:
            self.lbl_status.setText(f"READY: {self.preview.name} (version {self.preview.version})")
            set_tone(self.lbl_status, "off")

    def _show_check(self, problems, failure: str = ""):
        if failure:
            text, level = failure, "fault"
        elif not problems:
            self.lbl_check.hide()
            return
        else:
            errs, warns = errors(problems), warnings(problems)
            lines = []
            if errs:
                lines.append(f"Can't be started - the sequencer would refuse it ({len(errs)} error"
                             f"{'s' if len(errs) != 1 else ''}): {summary(errs, 3)}")
            if warns:
                lines.append(f"{len(warns)} warning{'s' if len(warns) != 1 else ''} (it can still run): "
                             f"{summary(warns, 3)}")
            lines.append("EDIT opens it in the recipe builder, which lists them all.")
            text, level = "\n".join(lines), "fault" if errs else "warn"
        self.lbl_check.setText(text)
        set_prop(self.lbl_check, "level", level)
        self.lbl_check.show()

    def _edit_recipe(self):
        filename = self.cb_recipes.currentText()
        if filename:
            self._open_builder(filename)

    def _open_builder(self, filename: str = None):
        """The recipe builder, beside the HMI (one window per recipe: a second EDIT brings it back)."""
        builder = self._builders.get(filename)
        if builder is not None:
            try:
                builder.showNormal()
                builder.raise_()
                builder.activateWindow()
                return builder
            except RuntimeError:                  # (it has been closed and deleted)
                self._builders.pop(filename, None)
        path = os.path.join(self.config_dir, filename) if filename else None
        builder = RecipeBuilderDialog(self, self.config_dir, load_filepath=path)
        self._builders[filename] = builder
        builder.saved.connect(lambda f, b=builder: self._recipe_saved(f, b))
        builder.destroyed.connect(lambda *_a, b=builder: self._forget_builder(b))
        builder.show()
        return builder

    def _forget_builder(self, builder):
        for key in [k for k, v in self._builders.items() if v is builder]:
            del self._builders[key]

    def _recipe_saved(self, filename: str, builder=None):
        """A recipe was saved in the builder: list it, and load it again if it is the one loaded
        (its steps here were the old ones)."""
        if builder is not None:                   # (a new recipe has a file now: EDIT on it finds this window)
            self._forget_builder(builder)
            self._builders[filename] = builder
        self._populate_recipes()
        if filename == self.loaded_file and not self.slot.active:
            self._load_recipe(filename)

    # ------------------------------------------------------------------ commands
    def _start_recipe(self):
        if not self.loaded_file or (self._preview_mode and self.load_errors):
            return
        self.btn_start.setEnabled(False)
        self._tickets["start"] = self.slot.send("start", {"file": self.loaded_file})

    def _pause_recipe(self):
        self.btn_pause.setEnabled(False)
        verb = "resume" if self.slot.state == P.PAUSED else "pause"
        self._tickets[verb] = self.slot.send(verb)

    def _abort_recipe(self):
        self.btn_abort.setEnabled(False)
        self.btn_pause.setEnabled(False)
        self._tickets["stop"] = self.slot.send("stop")

    def _acknowledge(self):
        prompt = self.slot.get("prompt") or {}
        self.btn_ack.setEnabled(False)
        self._tickets["ack"] = self.slot.send("ack", {"seq": prompt.get("seq")})
        self._tickets["ack"].prompt_seq = prompt.get("seq")

    def _check_tickets(self):
        for verb, ticket in list(self._tickets.items()):
            if not ticket.done():
                continue
            del self._tickets[verb]
            if verb == "start" and ticket.ok:
                # Show this run from now on, even if it ends before a telemetry update shows it running
                self._awaiting_run = (ticket.data or {}).get("run")
            elif verb == "ack" and not ticket.ok:
                self._ack_refusal = (getattr(ticket, "prompt_seq", None), ticket.reason)
            elif not ticket.ok:
                self.lbl_status.setText(f"{verb.upper()} refused: {ticket.reason}")
                set_tone(self.lbl_status, "fault")

    # ------------------------------------------------------------------ telemetry
    def update_telemetry(self, data: dict):
        self.slot.update(data)
        self._check_tickets()
        if not self.slot.online:
            self._set_buttons(start=False, pause=False, stop=False, start_tip=OFFLINE_TEXT)
            self.prompt_frame.hide()
            self.lbl_status.setText("SEQUENCER OFFLINE - " + OFFLINE_TEXT)
            set_tone(self.lbl_status, "unknown")
            self._offline = True
            return
        if getattr(self, "_offline", False):      # back online: the status line says what it did before
            self._offline = False
            if self._preview_mode and self.preview is not None:
                self._show_ready()
            else:
                self.lbl_status.setText("STATUS: IDLE")
                set_tone(self.lbl_status, "off")
        state = self.slot.state
        if self._awaiting_run is not None and self.slot.get("run") == self._awaiting_run:
            self._awaiting_run = None
            self._preview_mode = False
        busy = any(v in self._tickets for v in ("start", "stop")) or self._awaiting_run is not None
        if self.slot.active:
            self._preview_mode = False
            if not self.loaded_file:
                self.loaded_file = self.slot.get("file")
            self._show_run()
            self.btn_start.setText("▶ START")
            self._set_buttons(start=False, pause="pause" not in self._tickets and "resume" not in self._tickets
                              and state != P.STOPPING, stop="stop" not in self._tickets, paused=state == P.PAUSED,
                              start_tip="A recipe is running - stop it first.")
        else:
            if self._preview_mode and self.preview is not None:
                self._render_preview()
            elif state in P.FINAL:
                self.loaded_file = self.loaded_file or self.slot.get("file")
                self._show_run()
            blocked = self._preview_mode and bool(self.load_errors)
            can_start = bool(self.loaded_file) and not busy and not blocked
            self._set_buttons(start=can_start, pause=False, stop=False,
                              start_tip=("The checks found errors in this recipe (above): the sequencer would refuse "
                                         "it. EDIT opens it in the recipe builder." if blocked else ""))
            if state in P.FINAL and can_start and not self._preview_mode:
                self.btn_start.setText("↻ RESTART")
            else:
                self.btn_start.setText("▶ START")
        self._show_prompt(state)

    def _show_run(self):
        steps = self.slot.get("steps") or []
        current = self.slot.get("step")
        state = self.slot.state
        self._render([(s.get("id"), _row_text(s.get("id"), s.get("desc", "")), _note(s.get("name", "")),
                       s.get("state", "IDLE")) for s in steps], current, state)
        text = self.slot.get("status") or state
        crumb = self.slot.get("breadcrumb") or self.slot.get("name") or ""
        version = self.slot.get("version")
        if version and crumb:
            crumb = f"{crumb}, version {version}"
        if state in P.FINAL:
            self.lbl_status.setText(f"FINISHED ({crumb}): {self.slot.get('message') or text}")
        else:
            self.lbl_status.setText(f"{state.upper()} ({crumb}): {text}")
        set_tone(self.lbl_status, STATE_TONES.get(state, "off"))

    def _render_preview(self):
        self._render([(s.step_id, _row_text(s.step_id, s.get_description()),
                       s.comment if s.comment != "No description" else "", "IDLE") for s in self.preview.steps],
                     None, None)

    def _repaint_steps(self, *_):
        signature, self._rendered = self._rendered, None
        if signature is not None:
            try:
                self._render(*signature)
            except RuntimeError:            # (the panel has gone)
                pass

    def _render(self, rows, current, state):
        signature = (tuple(rows), current, state)
        if signature == self._rendered:
            return
        self._rendered = signature
        self.list_steps.clear()
        for step_id, name, desc, step_state in rows:
            item = QListWidgetItem(name)
            item.setToolTip(f"Note: {desc}" if desc else name)
            item.setData(Qt.ItemDataRole.UserRole, step_id)
            colour = STEP_COLOURS.get(step_state)
            if step_id == current and state in P.ACTIVE and step_state not in ("DONE", "FAILED"):
                colour = STEP_COLOURS["WAITING_FOR_USER" if state == P.PROMPT else "RUNNING"]
            if colour:
                item.setBackground(theme.color(colour[0]))
                item.setForeground(theme.color(colour[1]))
            if step_state == "DONE":
                item.setText(name + "  ✓")
            elif step_state == "FAILED":
                item.setText(name + "  ✗")
            self.list_steps.addItem(item)
            if step_id == current and state in P.ACTIVE:
                self.list_steps.scrollToItem(item)

    def _show_prompt(self, state):
        prompt = self.slot.get("prompt")
        if state != P.PROMPT or not prompt:
            self.prompt_frame.hide()
            return
        self.lbl_prompt.setText(f"Step {prompt.get('step')}: {prompt.get('text')}")
        refusal = self._ack_refusal if self._ack_refusal and self._ack_refusal[0] == prompt.get("seq") else None
        self.lbl_prompt_feedback.setText(f"Not accepted - {refusal[1]}" if refusal else "")
        self.lbl_prompt_feedback.setVisible(bool(refusal))
        self.btn_ack.setEnabled("ack" not in self._tickets)
        self.prompt_frame.show()


def _row_text(step_id, text: str) -> str:
    """'3. Wait until the Faraday cup is in (fails after 10 s)'"""
    return f"{step_id}. {text}" if text else f"{step_id}."


def _note(display_name: str) -> str:
    """The author's note from a step's display name ('[3] Verify cup' -> 'Verify cup')."""
    text = display_name.split("] ", 1)[1] if display_name.startswith("[") and "] " in display_name else display_name
    return "" if text in ("No description",) or text.endswith(" steps at the same time") else text
