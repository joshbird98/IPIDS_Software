"""Recipe panel. Recipes run in the sequencer service: this panel picks a recipe file, asks the
sequencer to start / pause / resume / stop it, answers its operator prompts, and shows its
progress from telemetry - so a recipe keeps running if the HMI is closed, and the panel picks
it up again when the HMI comes back."""
import os

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (QComboBox, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox,
                             QPushButton, QVBoxLayout, QWidget)

from src.core.recipe_engine import Recipe, RecipeError
from src.core.recipe_files import RECIPES_DIR, migrate_legacy_recipes
from src.gui.control.dialogs.recipe_builder_dialog import RecipeBuilderDialog
from src.gui.control.sequencer_link import OFFLINE_TEXT, SequencerSlot
from src.sequencer import protocol as P

# --- SCADA Standard Colors ---
COLOR_OK = "background-color: #4CAF50; color: white; font-weight: bold; border-radius: 4px; padding: 6px;"
COLOR_FAULT = "background-color: #F44336; color: white; font-weight: bold; border-radius: 4px; padding: 6px;"
COLOR_WARNING = "background-color: #FF9800; color: white; font-weight: bold; border-radius: 4px; padding: 6px;"
COLOR_INACTIVE = "background-color: #757575; color: white; font-weight: bold; border-radius: 4px; padding: 6px;"
COLOR_BUTTON_STANDARD = "background-color: #E0E0E0; color: black; font-weight: normal; border-radius: 4px; padding: 6px;"
PROMPT_STYLE = "QFrame#RecipePrompt { background-color: #FFF3E0; border: 2px solid #FF9800; border-radius: 6px; }"

STEP_COLOURS = {"DONE": "#C8E6C9", "FAILED": "#FFCDD2", "RUNNING": "#BBDEFB", "WAITING_FOR_USER": "#FFE0B2"}
STATE_STYLES = {P.RUNNING: COLOR_OK, P.PROMPT: COLOR_WARNING, P.PAUSED: COLOR_WARNING, P.COMPLETE: COLOR_OK,
                P.STOPPED: COLOR_WARNING, P.FAILED: COLOR_FAULT}


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
        self._tickets = {}                # "start" / "pause" / "ack" ... -> ticket of the last click
        self._ack_refusal = None          # (prompt number, reason) of a refused acknowledgement
        self._preview_mode = False        # show the loaded recipe rather than the last run
        self._awaiting_run = None         # run number of a start the sequencer accepted, until seen
        self._rendered = None
        self._init_ui()

    # ------------------------------------------------------------------ UI
    def _init_ui(self):
        layout = QVBoxLayout(self)

        self.btn_builder = QPushButton("🛠️ OPEN RECIPE BUILDER")
        self.btn_builder.setStyleSheet(
            "background-color: #9C27B0; color: white; font-weight: bold; border-radius: 4px; padding: 6px;")
        self.btn_builder.setToolTip("Open the interactive visual designer to create or edit recipes.")
        self.btn_builder.clicked.connect(self._open_builder)
        layout.addWidget(self.btn_builder)

        file_layout = QHBoxLayout()
        self.cb_recipes = QComboBox()
        self._populate_recipes()
        self.btn_refresh = QPushButton("↻")
        self.btn_refresh.setFixedWidth(40)
        self.btn_refresh.setToolTip("Rescan folder for new JSON files.")
        self.btn_refresh.clicked.connect(self._populate_recipes)
        self.btn_load = QPushButton("LOAD RECIPE")
        self.btn_load.setStyleSheet(COLOR_BUTTON_STANDARD)
        self.btn_load.setToolTip("Show the selected recipe's steps, ready to start.")
        self.btn_load.clicked.connect(self._load_recipe)
        self.btn_edit = QPushButton("✏️ EDIT")
        self.btn_edit.setToolTip("Open the selected recipe in the Builder for editing.")
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
        self.prompt_frame.setStyleSheet(PROMPT_STYLE)
        prompt_layout = QVBoxLayout(self.prompt_frame)
        self.lbl_prompt = QLabel()
        self.lbl_prompt.setWordWrap(True)
        self.lbl_prompt.setStyleSheet("font-weight: bold; color: black;")
        self.lbl_prompt_feedback = QLabel()
        self.lbl_prompt_feedback.setWordWrap(True)
        self.lbl_prompt_feedback.setStyleSheet("color: #B71C1C;")
        prompt_buttons = QHBoxLayout()
        self.btn_ack = QPushButton("✔ ACKNOWLEDGE")
        self.btn_ack.setStyleSheet(COLOR_OK)
        self.btn_ack.setToolTip("Confirm the action is done. The recipe checks its conditions (live values) first.")
        self.btn_ack.clicked.connect(self._acknowledge)
        self.btn_prompt_stop = QPushButton("⏹ STOP RECIPE")
        self.btn_prompt_stop.setStyleSheet(COLOR_FAULT)
        self.btn_prompt_stop.setToolTip("Stop the recipe here (nothing is switched off).")
        self.btn_prompt_stop.clicked.connect(self._abort_recipe)
        prompt_buttons.addWidget(self.btn_ack)
        prompt_buttons.addWidget(self.btn_prompt_stop)
        prompt_layout.addWidget(QLabel("<b>Recipe action required</b>"))
        prompt_layout.addWidget(self.lbl_prompt)
        prompt_layout.addWidget(self.lbl_prompt_feedback)
        prompt_layout.addLayout(prompt_buttons)
        self.prompt_frame.hide()
        layout.addWidget(self.prompt_frame)

        layout.addWidget(QLabel("<b>Execution Sequence:</b>"))
        self.list_steps = QListWidget()
        self.list_steps.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.list_steps.setStyleSheet("QListWidget { font-size: 11pt; }")
        layout.addWidget(self.list_steps)

        self.lbl_status = QLabel("STATUS: IDLE")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_status.setStyleSheet(COLOR_INACTIVE)
        layout.addWidget(self.lbl_status)
        self._set_buttons(start=False, pause=False, stop=False)

    def _set_buttons(self, start: bool, pause: bool, stop: bool, paused: bool = False, start_tip: str = ""):
        self.btn_start.setEnabled(start)
        self.btn_start.setStyleSheet(COLOR_OK if start else COLOR_INACTIVE)
        self.btn_start.setToolTip(start_tip or ("Run the loaded recipe from step 1." if start else
                                                "Load a recipe first (and the sequencer must be idle)."))
        self.btn_pause.setEnabled(pause)
        self.btn_pause.setText("▶ RESUME" if paused else "⏸ PAUSE")
        self.btn_pause.setStyleSheet((COLOR_OK if paused else COLOR_WARNING) if pause else COLOR_INACTIVE)
        self.btn_pause.setToolTip("Resume (the current step carries on, or starts again if control was taken)."
                                  if paused else "Suspend execution and freeze all timers.")
        self.btn_abort.setEnabled(stop)
        self.btn_abort.setStyleSheet(COLOR_FAULT if stop else COLOR_INACTIVE)
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

    def _load_recipe(self):
        filename = self.cb_recipes.currentText()
        if not filename:
            return
        try:
            self.preview = Recipe(os.path.join(self.config_dir, filename))
        except (OSError, RecipeError) as e:
            QMessageBox.critical(self, "Load Error", f"Failed to parse recipe:\n{e}")
            return
        self.loaded_file = filename
        self._preview_mode = True
        self._rendered = None
        self._render_preview()
        self.lbl_status.setText(f"READY: {self.preview.name}")
        self.lbl_status.setStyleSheet(COLOR_INACTIVE)

    def _edit_recipe(self):
        filename = self.cb_recipes.currentText()
        if not filename:
            return
        RecipeBuilderDialog(self, self.config_dir, load_filepath=os.path.join(self.config_dir, filename)).exec()
        self._populate_recipes()

    def _open_builder(self):
        RecipeBuilderDialog(self, self.config_dir).exec()
        self._populate_recipes()

    # ------------------------------------------------------------------ commands
    def _start_recipe(self):
        if not self.loaded_file:
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
                self.lbl_status.setStyleSheet(COLOR_FAULT)

    # ------------------------------------------------------------------ telemetry
    def update_telemetry(self, data: dict):
        self.slot.update(data)
        self._check_tickets()
        if not self.slot.online:
            self._set_buttons(start=False, pause=False, stop=False, start_tip=OFFLINE_TEXT)
            self.prompt_frame.hide()
            self.lbl_status.setText("SEQUENCER OFFLINE - " + OFFLINE_TEXT)
            self.lbl_status.setStyleSheet(COLOR_INACTIVE)
            return
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
            can_start = bool(self.loaded_file) and not busy
            self._set_buttons(start=can_start, pause=False, stop=False)
            if state in P.FINAL and can_start and not self._preview_mode:
                self.btn_start.setText("↻ RESTART")
            else:
                self.btn_start.setText("▶ START")
        self._show_prompt(state)

    def _show_run(self):
        steps = self.slot.get("steps") or []
        current = self.slot.get("step")
        state = self.slot.state
        self._render([(s.get("id"), s.get("name", ""), s.get("desc", ""), s.get("state", "IDLE")) for s in steps],
                     current, state)
        text = self.slot.get("status") or state
        crumb = self.slot.get("breadcrumb") or self.slot.get("name") or ""
        if state in P.FINAL:
            self.lbl_status.setText(f"FINISHED ({crumb}): {self.slot.get('message') or text}")
        else:
            self.lbl_status.setText(f"{state.upper()} ({crumb}): {text}")
        self.lbl_status.setStyleSheet(STATE_STYLES.get(state, COLOR_INACTIVE))

    def _render_preview(self):
        self._render([(s.step_id, s.get_display_name(), s.get_description(), "IDLE") for s in self.preview.steps],
                     None, None)

    def _render(self, rows, current, state):
        signature = (tuple(rows), current, state)
        if signature == self._rendered:
            return
        self._rendered = signature
        self.list_steps.clear()
        for step_id, name, desc, step_state in rows:
            item = QListWidgetItem(name)
            item.setToolTip(desc)
            item.setData(Qt.ItemDataRole.UserRole, step_id)
            colour = STEP_COLOURS.get(step_state)
            if step_id == current and state in P.ACTIVE and step_state not in ("DONE", "FAILED"):
                colour = STEP_COLOURS["WAITING_FOR_USER" if state == P.PROMPT else "RUNNING"]
            if colour:
                item.setBackground(QColor(colour))
                item.setForeground(QColor("black"))
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
