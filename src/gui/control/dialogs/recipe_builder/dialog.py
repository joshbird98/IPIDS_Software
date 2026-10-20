"""Recipe builder: build a recipe from blocks named for what they do, read it as plain sentences,
with the checks the sequencer makes before it runs one, and save it safely.

    RecipeBuilderDialog(parent, recipes_dir, load_filepath=None)

Left, the blocks (Set a value, Wait for a reading, Ask the operator, Bring the safety relay on ...);
middle, the recipe as a list of sentences in the order they run; right, the selected step's form.
Below: the problems the checks find, the stop conditions, the whole recipe as text, and drafting.

The checks (src/core/recipe_check.py) run as you type. Errors (the sequencer would refuse to start
the recipe) block saving; warnings (it would run, but probably not as meant) don't.

Drafting (src/core/recipe_ai, drafting.py): the bar above the steps asks a language model for a
draft of what the operator describes, or for a review. A draft is checked like any recipe (with
errors it goes back to the model) and shown in the list of steps as changes - added, removed,
changed - each of which can be left out; Keep makes the ticked ones, as one change Undo takes
back. The model never acts on the machine, and while a draft runs or is shown the recipe can't be
edited, so what is kept is exactly what was shown. A review leaves notes beside the steps, and any
fix it suggests can be drafted.

Saving (src/core/recipe_store.py) writes the file the recipe was opened from - never another
recipe's - only if nobody has changed it since, after a backup, and commits it to git. The version
goes up by one when the recipe has changed and its version hasn't: the sequencer's log names the
version and checksum of every recipe it runs.

The window isn't modal: the HMI stays usable while it is open. Undo and redo cover every change.
"""
import copy
import json
import logging
import os
from dataclasses import dataclass

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut, QTextCursor
from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                             QListWidget, QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
                             QSplitter, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from src.core import paths
from src.core.recipe_ai import changes as ai_changes
from src.core.recipe_ai.agent import Session
from src.core.recipe_ai.settings import Settings, availability
from src.core.recipe_catalog import shared
from src.core.recipe_check import ERROR, Problem, check_recipe, errors, warnings
from src.core.recipe_engine import RecipeError, resolve_sub_recipe
from src.core.recipe_files import RECIPES_DIR
from src.core.recipe_store import free_filename, read_recipe, renumber, save_recipe
from src.core.recipe_text import describe_policy_entry, describe_recipe, describe_step
from src.gui import theme
from src.gui.control.dialogs.recipe_builder.blocks import BLOCKS, BY_KEY, GROUPS, block_for, next_version
from src.gui.control.dialogs.recipe_builder.drafting import DraftBar, DraftPanel, Job, ProposalBanner
from src.gui.control.dialogs.recipe_builder.drafting import start as start_job
from src.gui.control.dialogs.recipe_builder.editor import StepEditor
from src.gui.control.dialogs.recipe_builder.outline import (LEVEL, MARK, NOTE, REVIEW, REVIEW_LEVEL, TEXT, Outline,
                                                             OutlineRow)
from src.gui.control.dialogs.recipe_builder.policy import PolicyEditor
from src.gui.style import chip, set_role, set_tone

log = logging.getLogger("hmi.recipe_builder")

TEMPLATES_DIR = os.path.join(paths.CONFIG_DIR, "recipe_templates")
HISTORY = 200
SEVERITY_ORDER = ("concern", "suggestion", "question")
EMPTY_TEXT = ("No steps yet. Add one from the blocks on the left, start from a template (New), or describe the "
              "recipe in the box above and press Draft.")


def empty_recipe() -> dict:
    return {"recipe_name": "New recipe", "version": "1.0", "fault_policy": [], "steps": []}


def templates(folder: str = TEMPLATES_DIR) -> list:
    """[(path, name, description)] of the recipe templates (config/recipe_templates)."""
    out = []
    try:
        names = sorted(f for f in os.listdir(folder) if f.endswith(".json"))
    except OSError:
        return out
    for name in names:
        path = os.path.join(folder, name)
        try:
            data, _ = read_recipe(path)
        except (OSError, ValueError) as e:
            log.warning("Recipe template %s not readable: %s", path, e)
            continue
        out.append((path, str(data.get("recipe_name") or name), str(data.get("description") or "")))
    return out


def _snapshot(data) -> str:
    """The recipe as text, to tell whether it has changed (a missing or empty fault policy, or an
    empty description, count as none)."""
    if isinstance(data, dict):
        data = dict(data)
        if data.get("fault_policy") in (None, {}, []):
            data.pop("fault_policy", None)
        if not str(data.get("description") or "").strip():
            data.pop("description", None)
    return json.dumps(data, sort_keys=True)


def _step_at(data, path):
    """The step at path in a recipe (None if there is none)."""
    steps = data.get("steps") if isinstance(data, dict) else None
    step = None
    try:
        for i in path:
            step = steps[i]
            steps = step.get("steps") if isinstance(step, dict) else None
    except (IndexError, KeyError, TypeError):
        return None
    return step


@dataclass
class _Draft:
    """A draft shown in the list of steps, with the changes ticked to keep."""
    request: str
    outcome: object
    base: dict                  # the recipe as it was when the draft was asked for (= the recipe: it is locked)
    proposed: dict
    changes: list
    kept: set
    rows: list                  # changes.Row, one per top-level step in the list
    result: dict = None         # the base with the kept changes
    numbers: list = None        # each row's step number in the result (None: not in it)


class SaveDetails(QDialog):
    """Before a save: the version (one up if the recipe changed and the version didn't) and what
    changed, for the git log."""

    def __init__(self, parent, where: str, version: str, suggested: str, n_warnings: int):
        super().__init__(parent)
        self.setWindowTitle("Save the recipe")
        layout = QVBoxLayout(self)
        where_label = QLabel(where)
        where_label.setWordWrap(True)
        layout.addWidget(where_label)
        form = QFormLayout()
        self.version = QLineEdit(suggested)
        self.reason = QLineEdit()
        self.reason.setPlaceholderText("Optional: what changed, e.g. extraction to 18 kV")
        form.addRow("Version", self.version)
        form.addRow("What changed", self.reason)
        layout.addLayout(form)
        if suggested != version:
            note = QLabel(f"The recipe has changed since version {version} was saved, so this is version "
                          f"{suggested}: the sequencer's log says which version of a recipe ran.")
            note.setWordWrap(True)
            set_role(note, "hint")
            layout.addWidget(note)
        if n_warnings:
            warn = QLabel(f"It has {n_warnings} warning{'s' if n_warnings != 1 else ''}: look at them (Problems, "
                          "below the steps) before you run it.")
            warn.setWordWrap(True)
            set_role(warn, "warning")
            layout.addWidget(warn)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def details(self):
        return self.version.text().strip() or "1.0", self.reason.text().strip()


class RecipeBuilderDialog(QDialog):
    """saved(file name): a recipe was saved (the Recipe Execution panel lists and reloads it)."""
    saved = pyqtSignal(str)

    def __init__(self, parent=None, config_dir: str = "", load_filepath: str = None, catalog=None):
        super().__init__(parent)
        self.setObjectName("RecipeBuilder")
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowMinMaxButtonsHint |
                            Qt.WindowType.WindowCloseButtonHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setModal(False)
        self.resize(1320, 900)
        self.recipes_dir = os.path.realpath(config_dir or RECIPES_DIR)
        self.catalog = catalog if catalog is not None else shared()
        self.filename = None            # the file it was opened from (or last saved to)
        self.opened_text = None         # that file's text then: a save checks nobody has changed it since
        self.opened_version = None
        self.data = empty_recipe()
        self.problems = []
        self._by_path = {}
        self._history, self._pos = [], -1
        self._saved_snapshot = None
        self._selected = None
        self._closing_ok = False
        self._names = {}                # file -> (mtime, recipe name)
        # drafting (src/core/recipe_ai)
        self.ai_settings = Settings.load()
        self.ai_ok, self.ai_why = availability(self.ai_settings)
        self.ai_threaded = True         # (the tests run a draft where they are)
        self._job = None                # the draft or review running
        self._session = None            # the conversation of the draft shown: a refinement carries it on
        self._draft = None              # the draft shown (_Draft)
        self._review = None             # (outcome, the keys of the notes' steps then, request)
        self._review_stale = None       # the notes whose step has changed since, as last shown
        self._check_timer = QTimer(self)
        self._check_timer.setSingleShot(True)
        self._check_timer.setInterval(150)
        self._check_timer.timeout.connect(self._check)
        self._record_timer = QTimer(self)
        self._record_timer.setSingleShot(True)
        self._record_timer.setInterval(600)
        self._record_timer.timeout.connect(self._record)
        self._build_ui()
        if load_filepath:
            self.open_file(load_filepath)
        else:
            self.load_data(empty_recipe())

    # ================================================================== the window
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        tools = QHBoxLayout()
        self.btn_new = QPushButton("New")
        self.btn_new.setToolTip("Start a new recipe: empty, or from a template (start-up, shutdown, mass scan ...)")
        self.menu_new = QMenu(self)
        self.menu_new.aboutToShow.connect(self._fill_new_menu)
        self.btn_new.setMenu(self.menu_new)
        self.btn_open = QPushButton("Open")
        self.btn_open.setToolTip("Open a saved recipe")
        self.menu_open = QMenu(self)
        self.menu_open.aboutToShow.connect(self._fill_open_menu)
        self.btn_open.setMenu(self.menu_open)
        self.btn_save = QPushButton("Save")
        set_tone(self.btn_save, "primary")
        self.btn_save.setToolTip("Save to the recipe's file (Ctrl+S). A new recipe is named after it.")
        self.btn_save.clicked.connect(lambda: self.save())
        self.btn_save_as = QPushButton("Save as new recipe")
        self.btn_save_as.setToolTip("Save as a new file, named after the recipe; the file it came from stays as "
                                    "it is")
        self.btn_save_as.clicked.connect(lambda: self.save(as_new=True))
        self.btn_undo = QPushButton("Undo")
        self.btn_undo.setToolTip("Undo (Ctrl+Z)")
        self.btn_undo.clicked.connect(self.undo)
        self.btn_redo = QPushButton("Redo")
        self.btn_redo.setToolTip("Redo (Ctrl+Y)")
        self.btn_redo.clicked.connect(self.redo)
        self.lbl_state = chip("", "off")
        self.lbl_state.setObjectName("CheckState")
        self.lbl_state.setToolTip("What the checks find: the same checks the sequencer makes before it starts a "
                                  "recipe")
        for w in (self.btn_new, self.btn_open, self.btn_save, self.btn_save_as, self.btn_undo, self.btn_redo):
            tools.addWidget(w)
        tools.addStretch(1)
        tools.addWidget(self.lbl_state)
        root.addLayout(tools)

        head = QGridLayout()
        head.setHorizontalSpacing(8)
        self.le_name = QLineEdit()
        self.le_name.setObjectName("RecipeName")
        self.le_name.textChanged.connect(lambda t: self._set_top("recipe_name", t))
        self.le_version = QLineEdit()
        self.le_version.setObjectName("RecipeVersion")
        self.le_version.setMaximumWidth(90)
        self.le_version.textChanged.connect(lambda t: self._set_top("version", t))
        self.le_description = QLineEdit()
        self.le_description.setObjectName("RecipeDescription")
        self.le_description.setPlaceholderText("Optional: what the recipe is for, and when to use it")
        self.le_description.textChanged.connect(lambda t: self._set_top("description", t, drop_empty=True))
        self.lbl_file = QLabel()
        set_role(self.lbl_file, "caption")
        for col, (text, widget) in enumerate((("Name", self.le_name), ("Version", self.le_version))):
            lbl = QLabel(text)
            set_role(lbl, "caption")
            head.addWidget(lbl, 0, col * 2)
            head.addWidget(widget, 0, col * 2 + 1)
        head.addWidget(self.lbl_file, 0, 4)
        lbl = QLabel("About")
        set_role(lbl, "caption")
        head.addWidget(lbl, 1, 0)
        head.addWidget(self.le_description, 1, 1, 1, 4)
        head.setColumnStretch(1, 3)
        head.setColumnStretch(4, 2)
        root.addLayout(head)

        self.draft_bar = DraftBar()
        self.draft_bar.draft.connect(self.request_draft)
        self.draft_bar.review.connect(self.request_review)
        self.draft_bar.cancel.connect(self.cancel_ai)
        if not self.ai_settings.enabled:
            self.draft_bar.hide()
        elif not self.ai_ok:
            self.draft_bar.set_unavailable(self.ai_why)
        root.addWidget(self.draft_bar)

        # blocks | steps | the selected step
        body = QSplitter(Qt.Orientation.Horizontal)
        body.setChildrenCollapsible(False)
        body.addWidget(self._build_palette())
        body.addWidget(self._build_steps())
        self.editor = StepEditor(self.catalog, recipes=self._recipes, recipe_names=self._recipe_names)
        self.editor.edited.connect(self._on_step_edited)
        body.addWidget(self.editor)
        body.setStretchFactor(0, 0)
        body.setStretchFactor(1, 3)
        body.setStretchFactor(2, 3)
        body.setSizes([230, 560, 520])

        self.tabs = QTabWidget()
        self.list_problems = QListWidget()
        self.list_problems.setObjectName("Problems")
        self.list_problems.setWordWrap(True)
        self.list_problems.itemActivated.connect(self._go_to_problem)
        self.list_problems.itemClicked.connect(self._go_to_problem)
        self.tabs.addTab(self.list_problems, "Problems")
        self.policy = PolicyEditor(self.catalog)
        self.policy.edited.connect(self._on_policy_edited)
        self.tabs.addTab(self.policy, "Stop conditions")
        text_tab = QWidget()
        text_layout = QVBoxLayout(text_tab)
        text_layout.setContentsMargins(0, 0, 0, 0)
        self.txt_recipe = QPlainTextEdit()
        self.txt_recipe.setObjectName("RecipeText")
        self.txt_recipe.setReadOnly(True)
        text_layout.addWidget(self.txt_recipe)
        copy_row = QHBoxLayout()
        btn_copy = QPushButton("Copy the text")
        btn_copy.clicked.connect(lambda: (self.txt_recipe.selectAll(), self.txt_recipe.copy(),
                                          self.txt_recipe.moveCursor(QTextCursor.MoveOperation.Start)))
        copy_row.addWidget(btn_copy)
        copy_row.addStretch(1)
        text_layout.addLayout(copy_row)
        self.tabs.addTab(text_tab, "Read as text")
        self.draft_panel = DraftPanel()
        self.draft_panel.keep.connect(self.keep_draft)
        self.draft_panel.discard.connect(self.discard_draft)
        self.draft_panel.toggled.connect(self.toggle_change)
        self.draft_panel.fix.connect(self.draft_fix)
        self.draft_panel.go.connect(self._go_to_note)
        self.draft_panel.dismiss.connect(self.clear_review)
        self.draft_panel.show_idle(self._idle_text())
        self.tabs.addTab(self.draft_panel, "Draft")
        if not self.ai_settings.enabled:
            self.tabs.setTabVisible(self.tabs.indexOf(self.draft_panel), False)

        split = QSplitter(Qt.Orientation.Vertical)
        split.setChildrenCollapsible(False)
        split.addWidget(body)
        split.addWidget(self.tabs)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        split.setSizes([600, 190])
        root.addWidget(split, 1)
        self.lbl_status = QLabel()
        self.lbl_status.setObjectName("BuilderStatus")
        self.lbl_status.setWordWrap(True)
        set_role(self.lbl_status, "caption")
        root.addWidget(self.lbl_status)

        for keys, slot in (("Ctrl+S", self.save), ("Ctrl+Z", self.undo), ("Ctrl+Y", self.redo),
                           ("Ctrl+Shift+Z", self.redo)):
            QShortcut(QKeySequence(keys), self, activated=lambda s=slot: s())
        QShortcut(QKeySequence(Qt.Key.Key_Delete), self.outline, activated=self.delete_step,
                  context=Qt.ShortcutContext.WidgetShortcut)
        QShortcut(QKeySequence("Ctrl+D"), self, activated=self.duplicate_step)
        QShortcut(QKeySequence("Alt+Up"), self, activated=lambda: self.move_step(-1))
        QShortcut(QKeySequence("Alt+Down"), self, activated=lambda: self.move_step(1))

    def _build_palette(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Add a step")
        set_role(title, "heading")
        layout.addWidget(title)
        self.palette = QTreeWidget()
        self.palette.setObjectName("Blocks")
        self.palette.setHeaderHidden(True)
        self.palette.setRootIsDecorated(False)
        self.palette.setItemsExpandable(False)         # (a double-click on a group heading doesn't fold it)
        self.palette.setIndentation(10)
        groups = {}
        for group in GROUPS:
            item = QTreeWidgetItem([group])
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            font = item.font(0)
            font.setBold(True)
            item.setFont(0, font)
            self.palette.addTopLevelItem(item)
            groups[group] = item
        for block in BLOCKS:
            item = QTreeWidgetItem([block.title])
            item.setData(0, Qt.ItemDataRole.UserRole, block.key)
            item.setToolTip(0, block.help)
            groups[block.group].addChild(item)
        self.palette.expandAll()
        self.palette.itemDoubleClicked.connect(
            lambda item, _c: item.data(0, Qt.ItemDataRole.UserRole) and self.add_block(item.data(0, Qt.ItemDataRole.UserRole)))
        self.palette.currentItemChanged.connect(self._show_block_help)
        layout.addWidget(self.palette, 1)
        self.lbl_block = QLabel("Double-click a block, or select it and press Add. It goes after the selected step "
                                "(inside it, for a 'Do steps at the same time' step).")
        self.lbl_block.setWordWrap(True)
        set_role(self.lbl_block, "hint")
        layout.addWidget(self.lbl_block)
        self.btn_add = QPushButton("Add")
        self.btn_add.setEnabled(False)
        self.btn_add.clicked.connect(lambda: self._selected_block() and self.add_block(self._selected_block()))
        layout.addWidget(self.btn_add)
        return box

    def _build_steps(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Steps")
        set_role(title, "heading")
        layout.addWidget(title)
        self.banner = ProposalBanner()
        self.banner.keep.connect(self.keep_draft)
        self.banner.discard.connect(self.discard_draft)
        layout.addWidget(self.banner)
        self.outline = Outline()
        self.outline.empty_text = EMPTY_TEXT
        self.outline.itemSelectionChanged.connect(self._on_select)
        self.outline.moved.connect(self.move_to)
        self.outline.change_clicked.connect(lambda cid: self.toggle_change(
            cid, self._draft is not None and cid not in self._draft.kept))
        layout.addWidget(self.outline, 1)
        row = QHBoxLayout()
        self.btn_up = QPushButton("Move up")
        self.btn_up.setToolTip("Alt+Up")
        self.btn_up.clicked.connect(lambda: self.move_step(-1))
        self.btn_down = QPushButton("Move down")
        self.btn_down.setToolTip("Alt+Down")
        self.btn_down.clicked.connect(lambda: self.move_step(1))
        self.btn_duplicate = QPushButton("Duplicate")
        self.btn_duplicate.setToolTip("Ctrl+D")
        self.btn_duplicate.clicked.connect(self.duplicate_step)
        self.btn_delete = QPushButton("Delete")
        self.btn_delete.setToolTip("Delete key")
        self.btn_delete.clicked.connect(self.delete_step)
        for b in (self.btn_up, self.btn_down, self.btn_duplicate, self.btn_delete):
            row.addWidget(b)
        row.addStretch(1)
        layout.addLayout(row)
        return box

    def _show_block_help(self, item, _previous=None):
        key = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        self.btn_add.setEnabled(bool(key) and not self._busy())
        if key:
            self.lbl_block.setText(BY_KEY[key].help)

    def _selected_block(self):
        item = self.palette.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None

    # ================================================================== the recipe
    def load_data(self, data: dict, filename: str = None, text: str = None, original: dict = None):
        """Show `data` (opened from `filename` in the recipes folder, whose text was `text`; None:
        a new recipe)."""
        data = data if isinstance(data, dict) else empty_recipe()
        cleared = []
        policy = data.get("fault_policy")
        if not isinstance(policy, list):
            if policy not in (None, {}):
                cleared.append("its stop conditions weren't a list, which the sequencer can't use")
            data["fault_policy"] = []
        if not isinstance(data.get("steps"), list):
            if "steps" in data:
                cleared.append("its steps weren't a list")
            data["steps"] = []
        renumber(data["steps"])            # an older file can repeat a number: the sequencer would refuse it
        self.data = data
        self.filename = filename
        self.opened_text = text
        self.opened_version = str(data.get("version", "1.0")) if filename else None
        self._saved_snapshot = _snapshot(original if original is not None else
                                         (copy.deepcopy(data) if filename else empty_recipe()))
        self._history, self._pos = [], -1
        self._record()
        self._closing_ok = False
        self._selected = None
        self._review = self._review_stale = None
        self._refresh_all(select=(0,) if self.data["steps"] else None)
        self.draft_panel.show_idle(self._idle_text())
        if cleared:
            self._status(f"Opened, but {' and '.join(cleared)}: they were left out here. Saving writes the recipe "
                         "as shown.")

    def open_file(self, path: str) -> bool:
        try:
            data, text = read_recipe(path)
        except (OSError, ValueError) as e:
            self._tell("Can't open the recipe", f"{os.path.basename(path)} can't be opened: {e}", "critical")
            if self._pos < 0:
                self.load_data(empty_recipe())
            return False
        real = os.path.realpath(path)
        inside = os.path.dirname(real) == self.recipes_dir
        original = copy.deepcopy(data)
        self.load_data(data, os.path.basename(real) if inside else None, text if inside else None, original)
        if not inside:
            self._status(f"{path} isn't in the recipes folder: it will be saved there as a new recipe.")
        elif _snapshot(original) != _snapshot(self.data):
            self._status("The steps were renumbered (the file repeated a number, which the sequencer refuses): save "
                         "to keep that.")
        else:
            self._status(f"Opened {os.path.basename(real)}.")
        return True

    def _set_top(self, key, text, drop_empty=False):
        if self._busy():
            return
        if drop_empty and not text.strip():
            self.data.pop(key, None)
        else:
            self.data[key] = text
        self._changed()

    def _path(self):
        return os.path.join(self.recipes_dir, self.filename) if self.filename else None

    def _steps(self) -> list:
        return self.data["steps"]

    def _get(self, path):
        steps = self._steps()
        step = None
        for i in path:
            step = steps[i]
            steps = step.get("steps", []) if isinstance(step, dict) else []
        return step

    def _container(self, path) -> list:
        return self._steps() if len(path) == 1 else self._get(path[:-1]).setdefault("steps", [])

    def _path_of(self, target) -> tuple:
        def walk(steps, base):
            for i, s in enumerate(steps):
                if s is target:
                    return base + (i,)
                if isinstance(s, dict) and isinstance(s.get("steps"), list):
                    found = walk(s["steps"], base + (i,))
                    if found:
                        return found
            return None
        return walk(self._steps(), ())

    def _recipes(self) -> list:
        """[(file, name)] of the saved recipes a sub-recipe step may run (not this one)."""
        try:
            files = sorted(f for f in os.listdir(self.recipes_dir) if f.endswith(".json"))
        except OSError:
            return []
        names = self._recipe_names()
        return [(f, names.get(f, "")) for f in files if f != self.filename]

    def _recipe_names(self) -> dict:
        out = {}
        try:
            files = [f for f in os.listdir(self.recipes_dir) if f.endswith(".json")]
        except OSError:
            return out
        for f in files:
            path = os.path.join(self.recipes_dir, f)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            cached = self._names.get(f)
            if cached is None or cached[0] != mtime:
                try:
                    data, _ = read_recipe(path)
                    name = str(data.get("recipe_name") or "")
                except (OSError, ValueError):
                    name = ""
                cached = self._names[f] = (mtime, name)
            out[f] = cached[1]
        return out

    def _load_sub(self, target):
        try:
            path = resolve_sub_recipe(self._path() or "<unsaved>", target, self.recipes_dir)
            return read_recipe(path)[0]
        except (RecipeError, OSError, ValueError):
            return None

    # ================================================================== changes and history
    def _changed(self, structural: bool = False):
        self._update_title()
        self._check_timer.start()
        if structural:
            self._record()
        else:
            self._record_timer.start()

    def _record(self):
        self._record_timer.stop()
        snap = json.dumps(self.data, sort_keys=True)         # exactly: what undo puts back
        if self._pos >= 0 and self._history[self._pos] == snap:
            self._update_history_buttons()
            return
        del self._history[self._pos + 1:]
        self._history.append(snap)
        if len(self._history) > HISTORY:
            del self._history[0]
        self._pos = len(self._history) - 1
        self._update_history_buttons()

    def _update_history_buttons(self):
        busy = self._busy()
        self.btn_undo.setEnabled(self._pos > 0 and not busy)
        self.btn_redo.setEnabled(self._pos < len(self._history) - 1 and not busy)

    def undo(self):
        if self._busy():
            return
        if self._record_timer.isActive():
            self._record()
        if self._pos > 0:
            self._pos -= 1
            self._restore(self._history[self._pos])

    def redo(self):
        if self._busy():
            return
        if self._record_timer.isActive():
            self._record()                   # an edit since the undo: it stays (and there is nothing to redo)
        if self._pos < len(self._history) - 1:
            self._pos += 1
            self._restore(self._history[self._pos])

    def _restore(self, snap: str):
        self.data = json.loads(snap)
        selected = self._selected
        try:
            if selected is not None:
                self._get(selected)
        except (IndexError, KeyError, TypeError, AttributeError):
            selected = None
        self._refresh_all(select=selected)
        self._update_history_buttons()

    def is_dirty(self) -> bool:
        return _snapshot(self.data) != self._saved_snapshot

    def _update_title(self):
        name = str(self.data.get("recipe_name") or "(no name)")
        where = self.filename or "not saved yet"
        self.setWindowTitle(f"Recipe builder: {name} ({where}){' *' if self.is_dirty() else ''}")
        if self.filename:
            self.lbl_file.setText(f"File: {self.filename}")
        else:
            self.lbl_file.setText(f"Not saved yet: it will be {free_filename(self.recipes_dir, name)}")

    # ================================================================== drawing
    def _refresh_all(self, select=None):
        for w, key in ((self.le_name, "recipe_name"), (self.le_version, "version"),
                       (self.le_description, "description")):
            w.blockSignals(True)
            w.setText(str(self.data.get(key) or ""))
            w.blockSignals(False)
        self._show_policy()
        self._check(render=False)
        self._render_outline(select)
        self._update_title()
        self._update_lock()

    def _show_policy(self):
        """The Stop conditions tab: the recipe's own list, edited in place - or while a draft is shown, the
        list keeping it would make (a copy: nothing can be edited then)."""
        if self._draft is not None:
            found = self._draft.result.get("fault_policy")
            self.policy.set_policy(copy.deepcopy(found) if isinstance(found, list) else [])
            return
        if not isinstance(self.data.get("fault_policy"), list):
            self.data["fault_policy"] = []     # (the editor must edit the recipe's own list, never a copy)
        self.policy.set_policy(self.data["fault_policy"])

    def _review_marks(self) -> dict:
        """{path: (text, level)} of the review's notes on steps that haven't changed since."""
        if self._review is None:
            return {}
        outcome, keys, _request = self._review
        out = {}
        for i, note in enumerate(outcome.review.notes):
            if not note.path or note.path == ("policy",):
                continue
            step = _step_at(self.data, note.path)
            if step is None or keys.get(i) != ai_changes.key(step):
                continue
            text, level = out.get(note.path, ("", ""))
            text = f"{text} / {note.text}" if text else note.text
            if not level or SEVERITY_ORDER.index(note.severity) < SEVERITY_ORDER.index(level):
                level = note.severity
            out[note.path] = (text, level)
        return out

    @staticmethod
    def _marks(found) -> tuple:
        n_err = sum(p.level == ERROR for p in found)
        n_warn = len(found) - n_err
        marks = [f"{n} {w}{'s' if n != 1 else ''}" for n, w in ((n_err, "error"), (n_warn, "warning")) if n]
        return "error" if n_err else "warning" if n_warn else "", ", ".join(marks)

    def _rows(self):
        if self._draft is not None:
            return self._draft_rows()
        rows = []
        names = self._recipe_names()
        reviewed = self._review_marks()

        def walk(steps, base, prefix):
            for i, s in enumerate(steps):
                path = base + (i,)
                level, mark = self._marks(self._by_path.get(path, []))
                text = describe_step(s, self.catalog, names) if isinstance(s, dict) else "(not a step)"
                note = str(s.get("comment") or "") if isinstance(s, dict) else ""
                review, review_level = reviewed.get(path, ("", ""))
                rows.append(OutlineRow(path, f"{prefix}{i + 1}", text, note if note != "No description" else "",
                                       level, mark, s.get("type") if isinstance(s, dict) else "", review=review,
                                       review_level=review_level))
                if isinstance(s, dict) and isinstance(s.get("steps"), list):
                    walk(s["steps"], path, f"{prefix}{i + 1}.")
        walk(self._steps(), (), "")
        return rows

    def _render_outline(self, select=None):
        self.outline.fill(self._rows())
        self.outline.select_path(select)
        self._show_selected(select, force=True)

    def _on_select(self):
        self._show_selected(self.outline.selected_path())

    def _show_selected(self, path, force: bool = False):
        if path == self._selected and not force:
            return
        self._selected = path
        if self._draft is not None:
            self._show_draft_step(path)
        else:
            step = None
            if path is not None:
                try:
                    step = self._get(path)
                except (IndexError, KeyError, TypeError, AttributeError):
                    path = self._selected = None
            if path is None:
                self.editor.clear()
            else:
                self.editor.show_step(step, path)
                self.editor.show_problems(self._by_path.get(path, []))
        self._update_lock()
        self.draft_bar.set_context(self._bar_context())

    def _on_step_edited(self):
        if self._busy():
            return
        self._changed()
        path = self._selected
        item = self.outline.item_at_path(path)
        if item is not None:
            step = self._get(path)
            item.setData(0, TEXT, describe_step(step, self.catalog, self._recipe_names()))
            item.setData(0, NOTE, str(step.get("comment") or ""))
            self.outline.scheduleDelayedItemsLayout()

    def _on_policy_edited(self):
        if not self._busy():
            self._changed(structural=True)

    # ================================================================== the checks
    def _checked_data(self) -> dict:
        """What the checks look at: the recipe, or with a draft shown, what keeping it would make."""
        return self._draft.result if self._draft is not None else self.data

    def _check(self, render: bool = True):
        self._check_timer.stop()
        data = self._checked_data()
        try:
            self.problems = check_recipe(data, self.catalog, self.recipes_dir, self._path())
        except Exception as e:   # a fault in the checks must not stop the builder: shown as an error
            log.exception("The recipe checks failed")
            self.problems = [Problem(ERROR, (), f"the checks failed on this recipe ({type(e).__name__}: {e}) - "
                                                "report it; it can't be saved until they pass")]
        by_path = {}
        policy = {}
        for p in self.problems:
            if p.path and p.path[0] == "policy":
                if len(p.path) > 1:
                    policy.setdefault(p.path[1], []).append(p)
            else:
                by_path.setdefault(tuple(p.path), []).append(p)
        changed = by_path != self._by_path
        self._by_path = by_path
        self.policy.show_problems(policy)           # (the list shown: the recipe's, or with the draft)
        self.policy.update_texts()
        if render and (changed or self._review is not None):
            self._render_marks()
        if self._selected is not None and self._draft is None:
            self.editor.show_problems(by_path.get(self._selected, []))
        self._fill_problems()
        n_err, n_warn = len(errors(self.problems)), len(warnings(self.problems))
        prefix = "With the draft: " if self._draft is not None else ""
        if n_err:
            self.lbl_state.setText(prefix + f"{n_err} error{'s' if n_err != 1 else ''}"
                                   + (f", {n_warn} warning{'s' if n_warn != 1 else ''}" if n_warn else ""))
            set_tone(self.lbl_state, "fault")
        elif n_warn:
            self.lbl_state.setText(prefix + f"{n_warn} warning{'s' if n_warn != 1 else ''}")
            set_tone(self.lbl_state, "warn")
        else:
            self.lbl_state.setText(prefix + "No problems found")
            set_tone(self.lbl_state, "ok")
        self.tabs.setTabText(0, f"Problems ({len(self.problems)})" if self.problems else "Problems")
        policy_list = data.get("fault_policy")
        n_policy = len(policy_list) if isinstance(policy_list, list) else 0
        self.tabs.setTabText(1, f"Stop conditions ({n_policy})")
        try:
            text = describe_recipe(data, self.catalog, self._recipe_names(), self._load_sub)
        except Exception as e:   # (the text is a view: never let it stop an edit)
            log.exception("No text for the recipe")
            text = f"(The recipe can't be shown as text: {type(e).__name__}: {e})"
        if self._draft is not None:
            text = "(As it would be with the draft's ticked changes.)\n\n" + text
        self.txt_recipe.setPlainText(text)
        if self._review is not None and self._draft is None:
            self._show_review_panel()
        self._update_banner()

    def _render_marks(self):
        if self._draft is not None:
            self._render_outline(self._selected)
            return
        for row in self._rows():
            item = self.outline.item_at_path(row.path)
            if item is not None:
                for role, value in ((TEXT, row.text), (LEVEL, row.level), (MARK, row.mark), (REVIEW, row.review),
                                    (REVIEW_LEVEL, row.review_level)):
                    item.setData(0, role, value)
        self.outline.scheduleDelayedItemsLayout()

    def _fill_problems(self):
        self.list_problems.clear()
        if not self.problems:
            text = ("No problems found: the sequencer would start this recipe" +
                    (", with the draft's ticked changes." if self._draft is not None else "."))
            item = QListWidgetItem(text)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.list_problems.addItem(item)
            return
        for p in sorted(self.problems, key=lambda p: (p.level != ERROR,)):
            item = QListWidgetItem(("Error - " if p.level == ERROR else "Warning - ") + (p.text()[:1].upper() + p.text()[1:]))
            item.setData(Qt.ItemDataRole.UserRole, p.path)
            item.setForeground(theme.color("fault" if p.level == ERROR else "warn"))
            self.list_problems.addItem(item)

    def _go_to_problem(self, item):
        path = item.data(Qt.ItemDataRole.UserRole)
        if not path:
            return
        if path[0] == "policy":
            self.tabs.setCurrentWidget(self.policy)
            if len(path) > 1:
                self.policy.list.setCurrentRow(path[1])
            return
        path = tuple(path)
        if self._draft is not None:
            path = self._row_path(path)
            if path is None:
                return
        self.outline.select_path(path, quiet=False)
        self._show_selected(path)

    # ================================================================== editing the steps
    def _insert_point(self, block):
        path = self._selected
        if path is None:
            return (), len(self._steps())
        step = self._get(path)
        inside_ok = block.in_parallel and block.key != "parallel"
        if len(path) == 1:
            if isinstance(step, dict) and step.get("type") == "PARALLEL" and inside_ok:
                return path, len(step.get("steps") or [])
            return (), path[0] + 1
        if inside_ok:
            return path[:-1], path[-1] + 1
        return (), path[0] + 1

    def add_block(self, key: str):
        if self._busy():
            return
        block = BY_KEY[key]
        step = block.new_step()
        parent, index = self._insert_point(block)
        container = self._steps() if not parent else self._get(parent).setdefault("steps", [])
        container.insert(index, step)
        renumber(self._steps())
        self._changed(structural=True)
        self._check(render=False)
        self._render_outline(self._path_of(step))
        self.editor.setFocus()

    def move_to(self, source: tuple, parent: tuple, index: int):
        """Move a step (dragged in the list) to `parent` (() for the top level) at `index`."""
        if self._busy():
            return
        step = self._get(source)
        if parent[:len(source)] == source:
            return                                       # into itself
        if parent:
            target = self._get(parent)
            block = block_for(step)
            if not isinstance(target, dict) or target.get("type") != "PARALLEL":
                return
            if block is None or not block.in_parallel or step.get("type") == "PARALLEL":
                self._status(f"A '{block.title if block else step.get('type')}' step can't go inside 'Do steps at "
                             "the same time'.")
                return
            destination = target.setdefault("steps", [])
        else:
            destination = self._steps()
        origin = self._container(source)
        i = source[-1]
        if origin is destination:
            if index > i:
                index -= 1
            if index == i:
                return
        origin.pop(i)
        destination.insert(max(0, min(index, len(destination))), step)
        renumber(self._steps())
        self._changed(structural=True)
        self._check(render=False)
        self._render_outline(self._path_of(step))

    def move_step(self, direction: int):
        path = self._selected
        if path is None or self._busy():
            return
        container = self._container(path)
        j = path[-1] + direction
        if not 0 <= j < len(container):
            return
        container[path[-1]], container[j] = container[j], container[path[-1]]
        step = container[j]
        renumber(self._steps())
        self._changed(structural=True)
        self._check(render=False)
        self._render_outline(self._path_of(step))

    def duplicate_step(self):
        path = self._selected
        if path is None or self._busy():
            return
        container = self._container(path)
        twin = copy.deepcopy(container[path[-1]])
        container.insert(path[-1] + 1, twin)
        renumber(self._steps())
        self._changed(structural=True)
        self._check(render=False)
        self._render_outline(self._path_of(twin))

    def delete_step(self):
        path = self._selected
        if path is None or self._busy():
            return
        container = self._container(path)
        del container[path[-1]]
        renumber(self._steps())
        if container:
            select = path[:-1] + (min(path[-1], len(container) - 1),)
        else:
            select = path[:-1] or None
        self._changed(structural=True)
        self._check(render=False)
        self._render_outline(select)

    # ================================================================== drafting: asking
    def _busy(self) -> bool:
        """A draft is running or shown: the recipe can't be edited (what is kept must be what was shown)."""
        return self._job is not None or self._draft is not None

    def _update_lock(self):
        busy = self._busy()
        for w in (self.palette, self.btn_new, self.btn_open, self.btn_save, self.btn_save_as, self.policy):
            w.setEnabled(not busy)
        self.btn_add.setEnabled(not busy and bool(self._selected_block()))
        for w in (self.le_name, self.le_version, self.le_description):
            w.setReadOnly(busy)
        self.outline.set_draggable(not busy)
        self.editor.setEnabled(not busy)
        running = self._job is not None             # (a refinement: the draft shown can't change meanwhile)
        self.draft_panel.set_locked(running)
        self.banner.set_locked(running)
        has = self._selected is not None and not busy
        for b in (self.btn_up, self.btn_down, self.btn_duplicate, self.btn_delete):
            b.setEnabled(has)
        self._update_history_buttons()
        if self.ai_ok and self.ai_settings.enabled:
            state = "running" if self._job is not None else "proposal" if self._draft is not None else "idle"
            if self.draft_bar.state != state:
                self.draft_bar.set_state(state, can_review=bool(self.data.get("steps")))
            elif state == "idle":
                self.draft_bar.btn_review.setEnabled(bool(self.data.get("steps")))

    def _bar_context(self) -> str:
        if self._draft is not None:
            return ("Describe what to change in the draft and press Refine - or keep or discard it, above the steps "
                    "or in the Draft tab.")
        if self._selected is not None:
            number = ".".join(str(i + 1) for i in self._selected)
            return (f"Step {number} is selected: “this step” in your request means it, and new steps go after it. "
                    "Enter asks; Shift+Enter starts a new line.")
        return ("The draft is checked, then shown in the steps as changes for you to keep or leave out. Enter asks; "
                "Shift+Enter starts a new line.")

    def _idle_text(self) -> str:
        if not self.ai_settings.enabled:
            return ""
        how = (f"Drafting uses {self.ai_settings.describe()}." if self.ai_ok else
               f"Drafting isn't available: {self.ai_why}")
        return ("Describe a recipe, or a change to this one, in the box above the steps and press Draft. The model "
                "drafts it from SINDIE's tags, procedures and templates; the draft is checked with the checks the "
                "sequencer makes (one with errors goes back to the model to fix) and shown in the steps as changes: "
                "keep them all, or untick the ones you don't want. Review asks the model to read the recipe against "
                "the procedures and note what looks wrong or missing; it changes nothing.\n\n"
                "The model never acts on the machine, and nothing is saved or run until you keep a draft and save "
                "it. It is sent your request, this recipe, the machine's tag list and the help's procedures - "
                "nothing from the machine itself.\n\n" + how)

    def request_draft(self, request: str):
        """Draft what the operator describes (or, with a draft shown, change the draft)."""
        request = str(request or "").strip()
        if not request or self._job is not None or not self.ai_ok:
            return
        self._run_ai("refine" if self._draft is not None else "draft", request)

    def request_review(self, focus: str = ""):
        if self._busy() or not self.ai_ok or not self.data.get("steps"):
            return
        self._run_ai("review", str(focus or "").strip())

    def draft_fix(self, note):
        """Draft the fix a review's note suggests (not offered for a note whose step has changed since:
        the fix may be about another step now)."""
        if self._busy() or not self.ai_ok or not note.fix:
            return
        on_step = bool(note.path) and note.path != ("policy",)
        if on_step and self._review is not None:
            outcome, keys, _request = self._review
            i = next((k for k, n in enumerate(outcome.review.notes) if n is note), None)
            step = _step_at(self.data, note.path)
            if i is None or step is None or ai_changes.key(step) != keys.get(i):
                return
        request = f"{note.fix} (The review said: {note.text})"
        self.draft_bar.edit.set_text(request)
        self.draft_bar.edit.remember(request)
        self._run_ai("draft", request, selected=note.path if on_step else None)

    def _run_ai(self, mode: str, request: str, selected=None):
        if self._record_timer.isActive():
            self._record()
        names = self._recipe_names()
        if mode == "refine":
            draft = self._draft
            unticked = [c.title for c in draft.changes if c.id not in draft.kept]
            base = draft.base
            session = self._session
            if session is not None:
                def work():
                    return session.refine(request, unticked)
            else:                       # (the conversation was dropped: a new one, shown the draft)
                session = Session("draft", self.catalog, base, self.filename, self.recipes_dir, names,
                                  self.ai_settings)
                where = self._draft_selection()

                def work():
                    return session.draft(request, where, earlier=draft.proposed, unticked=unticked)
        else:
            base = copy.deepcopy(self.data)
            session = Session(mode, self.catalog, base, self.filename, self.recipes_dir, names, self.ai_settings)
            where = self._selected if selected is None else selected
            if mode == "review":
                def work():
                    return session.review(request)
            else:
                def work():
                    return session.draft(request, where)
        job = Job(None, mode, request)

        def run(j):
            session.progress = j.progress
            session.cancelled = j.cancelled
            return work()
        job.fn = run
        job.base = base
        job.session = session
        job.on_progress = lambda text: self._ai_progress(job, text)
        job.on_done = lambda outcome: self._ai_done(job, outcome)
        job.on_failed = lambda error: self._ai_failed(job, error)
        self._job = job
        self._update_lock()
        verb = {"draft": "Drafting", "refine": "Changing the draft", "review": "Reviewing the recipe"}[mode]
        self._status(f"{verb} with {self.ai_settings.describe()}: the recipe can't be edited until it is done "
                     "(Cancel, or Escape, stops it).")
        start_job(job, threaded=self.ai_threaded)

    def cancel_ai(self):
        job = self._job
        if job is None:
            return
        job.cancel()
        job.on_progress = job.on_done = job.on_failed = None     # (a late answer is dropped)
        self._job = None
        if job.mode == "refine":
            self._session = None        # (its thread may still be using it: a refinement starts a new one)
        self._update_lock()
        self.draft_bar.set_context(self._bar_context())
        self._status("Cancelled.")

    def _ai_progress(self, job, text: str):
        if job is self._job:
            self.draft_bar.progress(text)

    def _ai_failed(self, job, error):
        if job is not self._job:
            return
        self._job = None
        if job.mode == "refine":
            self._session = None
        self._update_lock()
        message = getattr(error, "message", str(error))
        self.draft_bar.show_error(message)
        self._status(("The draft wasn't changed: " if job.mode == "refine" else "Not done: ") + message)

    def _ai_done(self, job, outcome):
        if job is not self._job:
            return
        self._job = None
        try:
            if job.mode == "review":
                self._show_review(job, outcome)
            else:
                self._show_draft(job, outcome)
        except Exception as e:      # (never left locked, or showing half a draft: said, and the recipe as it was)
            log.exception("Recipe drafting: showing the %s failed", job.mode)
            what = "review" if job.mode == "review" else "draft"
            if job.mode == "review":
                self._review = self._review_stale = None
            else:
                self._draft = self._session = None
            try:
                self._refresh_all(select=None)
            except Exception:
                log.exception("Recipe drafting: the builder couldn't be redrawn")
            self._update_lock()
            message = f"The {what} couldn't be shown ({type(e).__name__}: {e}): the recipe is as it was."
            self.draft_bar.show_error(message)
            self._status(message)
        finally:
            self._update_lock()

    # ================================================================== drafting: a draft shown
    def _show_draft(self, job, outcome):
        proposal = outcome.proposal
        names = self._recipe_names()
        found = ai_changes.compute(job.base, proposal.recipe,
                                   lambda s: describe_step(s, self.catalog, names),
                                   lambda e: describe_policy_entry(e, self.catalog))
        if not found:
            self._draft = self._session = None
            summary = proposal.summary or "the model proposed the recipe as it is."
            self._refresh_all(select=self._selected if job.mode != "refine" else None)
            self.draft_panel.show_idle(f"The draft of “{job.request}” changes nothing: {summary}\n\n"
                                       f"({outcome.cost_text()})")
            self.tabs.setCurrentWidget(self.draft_panel)
            self.draft_bar.set_context(f"The draft changes nothing: {summary}")
            self._status(f"The draft changes nothing: {summary}")
            return
        self._session = job.session
        self._draft = _Draft(job.request, outcome, job.base, proposal.recipe, found, {c.id for c in found},
                             ai_changes.merged(job.base, proposal.recipe, found))
        self._apply_kept()
        self.draft_bar.edit.clear()
        self.draft_panel.show_draft(job.request, outcome, found, self._draft.kept, outcome.cost_text())
        self.tabs.setCurrentWidget(self.draft_panel)
        first = next((k for k, r in enumerate(self._draft.rows) if r.change is not None), None)
        self._refresh_all(select=(first,) if first is not None else None)
        errs = len(errors(proposal.problems))
        self._status(f"Draft ready: {len(found)} change{'s' if len(found) != 1 else ''}"
                     + (f" - it still has {errs} error{'s' if errs != 1 else ''} (the model couldn't fix them)"
                        if errs else "") + ". Keep or discard it; Refine changes it.")

    def _apply_kept(self):
        d = self._draft
        d.result = ai_changes.apply(d.base, d.proposed, d.changes, d.kept)
        d.numbers = ai_changes.numbers(d.rows, d.kept)

    def _row_step(self, row):
        """The step a row of the draft shows: as the draft has it, or for a change left out, as it stays."""
        d = self._draft
        if row.kind == "change" and row.change not in d.kept:
            change = next(c for c in d.changes if c.id == row.change)
            return d.base["steps"][change.base[0]]
        return (d.base if row.source == "base" else d.proposed)["steps"][row.index]

    def _draft_rows(self):
        d = self._draft
        names = self._recipe_names()
        by_id = {c.id: c for c in d.changes}
        rows = []
        for k, (row, number) in enumerate(zip(d.rows, d.numbers)):
            step = self._row_step(row)
            kept = row.change is None or row.change in d.kept
            level, mark = self._marks(self._by_path.get((number - 1,), []) if number else [])
            was = ""
            if row.kind == "change":    # (kept: the step as changed, and what it was; left out: as it stays)
                if kept:
                    was = "was: " + describe_step(d.base["steps"][by_id[row.change].base[0]], self.catalog, names)
                else:
                    was = "the draft's change, left out: " + describe_step(d.proposed["steps"][row.index],
                                                                            self.catalog, names)
            note = str(step.get("comment") or "") if isinstance(step, dict) else ""
            text = describe_step(step, self.catalog, names) if isinstance(step, dict) else "(not a step)"
            rows.append(OutlineRow((k,), str(number) if number else "", text, note if note != "No description" else "",
                                   level, mark, step.get("type") if isinstance(step, dict) else "", row.kind,
                                   row.change, kept, was))
            if isinstance(step, dict) and isinstance(step.get("steps"), list):
                for j, sub in enumerate(step["steps"]):
                    level, mark = self._marks(self._by_path.get((number - 1, j), []) if number else [])
                    sub_note = str(sub.get("comment") or "") if isinstance(sub, dict) else ""
                    rows.append(OutlineRow((k, j), f"{number}.{j + 1}" if number else "",
                                           describe_step(sub, self.catalog, names) if isinstance(sub, dict)
                                           else "(not a step)", sub_note, level, mark,
                                           sub.get("type") if isinstance(sub, dict) else "", row.kind, None, kept))
        return rows

    def _row_path(self, result_path: tuple):
        """The list's path of a step of the result (with the draft), or None."""
        for k, number in enumerate(self._draft.numbers):
            if number == result_path[0] + 1:
                return (k,) + tuple(result_path[1:])
        return None

    def _draft_selection(self):
        """The selected row as a step of the base (for a new conversation about the draft)."""
        d = self._draft
        if d is None or self._selected is None or not 0 <= self._selected[0] < len(d.rows):
            return None
        row = d.rows[self._selected[0]]
        if row.source == "base":
            return (row.index,)
        if row.kind == "change":
            return (next(c for c in d.changes if c.id == row.change).base[0],)
        return None

    def _show_draft_step(self, path):
        """A step of the draft in the form, to read: nothing can be edited while a draft is shown."""
        d = self._draft
        if path is None or not 0 <= path[0] < len(d.rows):
            self.editor.clear()
            self._selected = None
            return
        row = d.rows[path[0]]
        step = self._row_step(row)
        if len(path) > 1:
            step = _step_at({"steps": [step]}, (0,) + tuple(path[1:]))
        number = d.numbers[path[0]]
        shown = ((number - 1,) if number else (row.index,)) + tuple(path[1:])
        kept = row.change in d.kept
        # A step that won't be in the recipe has no number: "Step 11" would name another step.
        label = "" if number else ("Removed by the draft" if row.kind == "remove" else "Left out of the draft")
        self.editor.show_step(copy.deepcopy(step), shown, label)
        result_path = ((number - 1,) + tuple(path[1:])) if number else None
        notice = {"add": "The draft adds this step." if kept else "The draft would add this step (left out).",
                  "remove": "The draft removes this step." if kept else "The draft would remove this step (left "
                                                                         "out: it stays).",
                  "change": "The draft changes this step (shown as changed)." if kept else
                            "The draft's change to this step is left out: it stays as it is (shown)."}.get(row.kind, "")
        self.editor.show_problems(self._by_path.get(result_path, []) if result_path else [], notice)

    def toggle_change(self, change_id: int, on: bool):
        d = self._draft
        if d is None:
            return
        if self._job is not None:                   # (a refinement runs: the ticks stay as they were)
            self.draft_panel.update_kept(d.kept, len(d.changes))
            return
        if on:
            d.kept.add(change_id)
        else:
            d.kept.discard(change_id)
        self._apply_kept()
        self.draft_panel.update_kept(d.kept, len(d.changes))
        self._show_policy()
        self._check(render=False)
        self._render_outline(self._selected)

    def _update_banner(self):
        d = self._draft
        index = self.tabs.indexOf(self.draft_panel)
        if d is not None:
            n = len(d.changes)
            self.tabs.setTabText(index, f"Draft ({n} change{'s' if n != 1 else ''})")
        elif self._review is not None:
            n = len(self._review[0].review.notes)
            self.tabs.setTabText(index, f"Review ({n} note{'s' if n != 1 else ''})")
        else:
            self.tabs.setTabText(index, "Draft")
        if d is None:
            self.banner.hide()
            return
        errs = len(errors(self.problems))
        problems = f" With them: {errs} error{'s' if errs != 1 else ''}." if errs else ""
        self.banner.show_counts(len(d.kept), len(d.changes), problems)

    def keep_draft(self):
        d = self._draft
        if d is None or self._job is not None or not d.kept:
            return
        n, total = len(d.kept), len(d.changes)
        first = next((d.numbers[k] for k, r in enumerate(d.rows)
                      if r.change in d.kept and r.kind in ("add", "change") and d.numbers[k]), None)
        result = ai_changes.apply(d.base, d.proposed, d.changes, d.kept)
        self._draft = self._session = None
        self.data = result
        self._changed(structural=True)
        self._refresh_all(select=(first - 1,) if first else None)
        kept = "the change" if n == total == 1 else f"all {total} changes" if n == total else f"{n} of {total} changes"
        self._after_draft(f"Kept {kept} from the draft of “{d.request}”. Undo (Ctrl+Z) takes them back. Save when "
                          "the recipe is as you want it.")
        self._status(f"Kept {kept} from the draft. Undo takes them back; nothing is saved until you save.")

    def discard_draft(self):
        d = self._draft
        if d is None or self._job is not None:
            return
        self._draft = self._session = None
        self._refresh_all(select=None)
        self._after_draft(f"The draft of “{d.request}” was discarded.")
        self._status("Draft discarded: the recipe is as it was.")

    def _after_draft(self, text: str):
        """The Draft tab once a draft is kept or discarded: the review's notes again if there is one (a
        fix drafted from a note comes back to them, the notes on changed steps marked so), else what
        happened."""
        if self._review is not None:
            self._review_stale = None               # (drawn again, whatever was there)
            self._show_review_panel()
            self._update_banner()
        else:
            self.draft_panel.show_idle(text + "\n\n" + self._idle_text())

    # ================================================================== drafting: a review
    def _show_review(self, job, outcome):
        keys = {}
        for i, note in enumerate(outcome.review.notes):
            step = _step_at(job.base, note.path) if note.path and note.path != ("policy",) else None
            keys[i] = ai_changes.key(step) if step is not None else None
        self._review, self._review_stale = (outcome, keys, job.request), None
        self._render_outline(self._selected)
        self._show_review_panel()
        self._update_banner()
        self.tabs.setCurrentWidget(self.draft_panel)
        n = len(outcome.review.notes)
        self._status(f"Review done: {n} note{'s' if n != 1 else ''}" + (" - shown beside the steps, and in the Draft "
                                                                           "tab." if n else "."))

    def _show_review_panel(self):
        outcome, keys, request = self._review
        stale = set()
        for i, note in enumerate(outcome.review.notes):
            if note.path and note.path != ("policy",):
                step = _step_at(self.data, note.path)
                if step is None or ai_changes.key(step) != keys.get(i):
                    stale.add(i)
        if stale == self._review_stale and self.draft_panel.currentWidget() is self.draft_panel.page_review:
            return
        self._review_stale = stale
        self.draft_panel.show_review(request, outcome, stale, outcome.cost_text(), self.draft_fix, self._go_to_note)

    def _go_to_note(self, note):
        if self._draft is None and note.path and _step_at(self.data, note.path) is not None:
            self.outline.select_path(note.path, quiet=False)
            self._show_selected(note.path)

    def clear_review(self):
        self._review = self._review_stale = None
        self._render_outline(self._selected)
        self.draft_panel.show_idle(self._idle_text())
        self._update_banner()

    # ================================================================== menus
    def _fill_new_menu(self):
        self.menu_new.clear()
        empty = self.menu_new.addAction("Empty recipe")
        empty.triggered.connect(lambda: self._may_discard() and self.load_data(empty_recipe()))
        found = templates()
        if found:
            self.menu_new.addSeparator()
            head = self.menu_new.addAction("From a template:")
            head.setEnabled(False)
        for path, name, description in found:
            action = self.menu_new.addAction(name)
            action.setToolTip(description)
            action.triggered.connect(lambda _c=False, p=path: self.new_from_template(p))
        self.menu_new.setToolTipsVisible(True)

    def new_from_template(self, path: str) -> bool:
        if not self._may_discard():
            return False
        try:
            data, _ = read_recipe(path)
        except (OSError, ValueError) as e:
            self._tell("Template not readable", f"{os.path.basename(path)} can't be read: {e}", "critical")
            return False
        data.pop("template", None)
        self.load_data(data)
        self._status(f"New recipe from the template '{data.get('recipe_name')}': check every value for your run, "
                     "then save it.")
        return True

    def _fill_open_menu(self):
        self.menu_open.clear()
        names = self._recipe_names()
        files = sorted(names)
        if not files:
            action = self.menu_open.addAction("(no recipes saved yet)")
            action.setEnabled(False)
        for f in files:
            action = self.menu_open.addAction(f"{names[f] or f}   ({f})")
            action.triggered.connect(lambda _c=False, f=f: self._may_discard() and
                                     self.open_file(os.path.join(self.recipes_dir, f)))

    # ================================================================== saving
    def save(self, as_new: bool = False, version: str = None, reason: str = None) -> bool:
        """Save (see the module doc). version / reason: as given (no questions asked)."""
        if self._busy():
            self._status("Keep or discard the draft first (or wait for it, or cancel it).")
            return False
        if self._record_timer.isActive():
            self._record()
        self._check()
        errs = errors(self.problems)
        if errs:
            shown = "\n".join(f"- {p.text()}" for p in errs[:12])
            more = f"\n(and {len(errs) - 12} more)" if len(errs) > 12 else ""
            self._tell("Not saved", "The recipe has errors - the sequencer would refuse to start it:\n\n"
                       f"{shown}{more}\n\nThey are listed under Problems.", "warning")
            self.tabs.setCurrentWidget(self.list_problems)
            return False
        filename = None if as_new else self.filename
        current = str(self.data.get("version", "1.0"))
        suggested = current
        if filename and self.opened_version is not None and current == self.opened_version and self.is_dirty():
            suggested = next_version(current)
        if version is None:
            where = (f"Save to {filename} in the recipes folder." if filename else
                     f"Save as a new recipe: {free_filename(self.recipes_dir, self.data.get('recipe_name', ''))} in "
                     "the recipes folder.")
            details = self._ask_save_details(where, current, suggested, len(warnings(self.problems)))
            if details is None:
                return False
            version, reason = details
        self.data["version"] = version
        result = save_recipe(self.data, self.recipes_dir, filename=filename,
                             expected=self.opened_text if filename else None, reason=reason or "",
                             catalog=self.catalog)
        if not result.saved:
            self.data["version"] = current
            self._tell("Not saved", result.refused[:1].upper() + result.refused[1:] + ".", "warning")
            return False
        try:
            _, text = read_recipe(result.path)
        except (OSError, ValueError):
            text = None
        self.filename, self.opened_text = result.filename, text
        self.opened_version = str(version)
        self._saved_snapshot = _snapshot(self.data)
        self._record()
        self._refresh_all(select=self._selected)
        git = (f"committed to git ({result.git})" if result.committed else f"not committed to git: {result.git}")
        backup = f"; the previous file is in {os.path.relpath(result.backup, self.recipes_dir)}" if result.backup else ""
        self._status(f"Saved {result.filename}, version {version}: {git}{backup}.")
        self.saved.emit(result.filename)
        return True

    def _ask_save_details(self, where, version, suggested, n_warnings):
        """(version, what changed) or None (cancelled)."""
        dialog = SaveDetails(self, where, version, suggested, n_warnings)
        return dialog.details() if dialog.exec() else None

    # ================================================================== closing
    def _may_discard(self) -> bool:
        """True if there are no unsaved changes, or the author saves or discards them (and a draft
        shown is kept or discarded first). A draft or review running is cancelled only if they go on."""
        if self._draft is not None:
            choice = self._ask_pending_draft()
            if choice == "cancel":
                return False
            self.cancel_ai()                        # (a refinement: the draft as shown is what is kept)
            if choice == "keep" and self._draft.kept:
                self.keep_draft()
            else:
                self.discard_draft()
        if not self.is_dirty():
            self.cancel_ai()
            return True
        choice = self._ask_unsaved()
        if choice == "cancel":
            return False
        self.cancel_ai()
        return self.save() if choice == "save" else True

    def _ask_pending_draft(self) -> str:
        """'keep', 'discard' or 'cancel', for a draft shown when the window closes."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("A draft is shown")
        box.setText("The draft's changes haven't been kept or discarded.")
        keep = box.addButton("Keep the ticked changes", QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("Discard the draft", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        return "keep" if clicked is keep else "discard" if clicked is discard else "cancel"

    def _ask_unsaved(self) -> str:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Unsaved changes")
        box.setText(f"'{self.data.get('recipe_name') or 'This recipe'}' has changes that aren't saved.")
        save = box.addButton("Save", QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("Discard the changes", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        return "save" if clicked is save else "discard" if clicked is discard else "cancel"

    def _may_close(self) -> bool:
        if not self._closing_ok:
            self._closing_ok = self._may_discard()
        return self._closing_ok

    def reject(self):
        """Escape or the close button: a draft running is cancelled first; unsaved changes are never
        lost without asking."""
        if self._job is not None:
            self.cancel_ai()
            return
        if self._may_close():
            super().reject()

    def closeEvent(self, event):
        if self._may_close():
            self.cancel_ai()
            super().closeEvent(event)
        else:
            event.ignore()

    # ================================================================== messages
    def _status(self, text: str):
        self.lbl_status.setText(text)

    def _tell(self, title: str, text: str, kind: str = "information"):
        getattr(QMessageBox, kind)(self, title, text)
