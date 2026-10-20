"""The recipe builder's stop conditions (the recipe's fault policy): PLC faults that stop it when
latched, and live conditions that stop it when they hold. The sequencer checks them before every
step, for the recipe and for each sub-recipe running."""
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
                             QSplitter, QVBoxLayout, QWidget)

from src.core.recipe_catalog import RECOMMENDED_FAULTS
from src.core.recipe_engine import RecipeError, parse_fault_policy
from src.core.recipe_text import describe_policy_entry
from src.gui import theme
from src.gui.control.dialogs.recipe_builder.fields import ConditionRow, FaultField
from src.gui.style import set_role


def entry_problem(entry) -> str:
    """Why the sequencer can't use this entry ('' if it can)."""
    try:
        parse_fault_policy([entry])
        return ""
    except RecipeError as e:
        return str(e).replace("fault policy entry 1", "this stop condition", 1)


class PolicyEditor(QWidget):
    """Edits a recipe's fault_policy list in place. edited: it changed."""
    edited = pyqtSignal()

    def __init__(self, catalog, parent=None):
        super().__init__(parent)
        self.catalog = catalog
        self.policy = []
        self.problems = {}
        self.list = QListWidget()
        self.list.setObjectName("StopConditions")
        self.list.setWordWrap(True)
        self.btn_fault = QPushButton("Add a fault")
        self.btn_fault.setToolTip("Stop the recipe when this PLC fault is latched.")
        self.btn_condition = QPushButton("Add a condition")
        self.btn_condition.setToolTip("Stop the recipe when a live reading meets a condition, e.g. the source "
                                      "pressure above 5e-6 mbar.")
        self.btn_recommended = QPushButton("Add the recommended")
        self.btn_recommended.setToolTip("Stop if the safety relay trips (Safety_Relay_Tripped): for any recipe "
                                        "that switches on the source or the magnets.")
        self.btn_remove = QPushButton("Remove")
        for b, slot in ((self.btn_fault, self._add_fault), (self.btn_condition, self._add_condition),
                        (self.btn_recommended, self._add_recommended), (self.btn_remove, self._remove)):
            b.clicked.connect(slot)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        hint = QLabel("The recipe stops (and fails) as soon as one of these holds. Leaving them out doesn't make a "
                      "run safer: the PLC's interlocks act anyway, but the recipe would carry on sending commands.")
        hint.setWordWrap(True)
        set_role(hint, "hint")
        left_layout.addWidget(hint)
        left_layout.addWidget(self.list, 1)
        row = QHBoxLayout()
        for b in (self.btn_fault, self.btn_condition, self.btn_recommended, self.btn_remove):
            row.addWidget(b)
        row.addStretch(1)
        left_layout.addLayout(row)
        self.host = QFrame()
        set_role(self.host, "card")
        self.host_layout = QVBoxLayout(self.host)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(left)
        split.addWidget(self.host)
        split.setSizes([520, 420])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(split)
        self.list.currentRowChanged.connect(self._show_entry)
        theme.theme_changed().connect(self._repaint)

    # ------------------------------------------------------------------ the list
    def set_policy(self, policy: list, select: int = None):
        self.policy = policy
        self.render(select)

    def render(self, select: int = None):
        if select is None:
            select = self.list.currentRow()
        self.list.blockSignals(True)
        self.list.clear()
        for i, entry in enumerate(self.policy):
            item = QListWidgetItem(self._text(entry))
            self.list.addItem(item)
        self._repaint()
        self.list.blockSignals(False)
        if self.policy:
            self.list.setCurrentRow(max(0, min(select if select is not None and select >= 0 else 0,
                                               len(self.policy) - 1)))
        self._show_entry(self.list.currentRow())
        self.btn_remove.setEnabled(bool(self.policy))

    def _text(self, entry) -> str:
        why = entry_problem(entry)
        if why:
            return f"(can't be used: {why})"
        return describe_policy_entry(entry, self.catalog)[:1].upper() + describe_policy_entry(entry, self.catalog)[1:]

    def show_problems(self, problems: dict):
        """{entry index: [Problem]}"""
        self.problems = problems
        self._repaint()

    def _repaint(self, *_):
        for i in range(self.list.count()):
            item = self.list.item(i)
            found = self.problems.get(i) or []
            errors = [p for p in found if p.level == "error"] or ([1] if i < len(self.policy) and
                                                                  entry_problem(self.policy[i]) else [])
            if errors:
                item.setForeground(theme.color("fault"))
            elif found:
                item.setForeground(theme.color("warn"))
            else:
                item.setForeground(theme.color("text"))
            tip = "\n".join(p.message for p in found)
            item.setToolTip(tip)

    def update_texts(self):
        for i in range(min(self.list.count(), len(self.policy))):
            self.list.item(i).setText(self._text(self.policy[i]))

    # ------------------------------------------------------------------ one entry
    def _show_entry(self, row: int):
        while self.host_layout.count():
            item = self.host_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()
                item.widget().deleteLater()
        if not 0 <= row < len(self.policy):
            note = QLabel("No stop condition selected." if self.policy else "No stop conditions.")
            set_role(note, "hint")
            self.host_layout.addWidget(note)
            self.host_layout.addStretch(1)
            return
        entry = self.policy[row]
        kind = entry.get("type") if isinstance(entry, dict) else None
        if isinstance(entry, dict) and kind is None:
            kind = "BIT_FAULT" if "registry_tag" in entry or "bit_str" in entry else \
                "CONDITION" if "target" in entry else None
        if kind == "BIT_FAULT":
            title = QLabel("Stop when this PLC fault is latched")
            set_role(title, "title")
            field = FaultField(self.catalog, entry)

            def picked(fields):
                entry.update(fields)
                entry["type"] = "BIT_FAULT"
                entry.setdefault("action", "ABORT")
                self._changed()
            field.picked.connect(picked)
            self.host_layout.addWidget(title)
            self.host_layout.addWidget(field)
        elif kind == "CONDITION":
            title = QLabel("Stop when")
            set_role(title, "title")
            row_widget = ConditionRow(self.catalog, entry)
            row_widget.changed.connect(self._changed)
            message = QLineEdit(str(entry.get("message") or ""))
            message.setPlaceholderText("Optional: what to tell the operator, e.g. The source pressure rose")
            message.textChanged.connect(lambda t: (entry.__setitem__("message", t) if t else entry.pop("message", None),
                                                   self._changed()))
            for w in (title, row_widget, QLabel("Message"), message):
                self.host_layout.addWidget(w)
        else:
            note = QLabel(f"The sequencer can't use this entry: {entry_problem(entry)}. Remove it.")
            note.setWordWrap(True)
            set_role(note, "error")
            self.host_layout.addWidget(note)
        self.host_layout.addStretch(1)

    def _changed(self):
        self.update_texts()
        self.edited.emit()

    # ------------------------------------------------------------------ buttons
    def _present(self, name) -> bool:
        return any(isinstance(e, dict) and e.get("name") == name for e in self.policy)

    def _append(self, entry):
        self.policy.append(entry)
        self.render(len(self.policy) - 1)
        self.edited.emit()

    def _add_fault(self):
        names = list(RECOMMENDED_FAULTS) + [f.name for f in self.catalog.faults]
        fault = next((self.catalog.fault(name=n) for n in names if not self._present(n) and self.catalog.fault(name=n)),
                     None)
        if fault is None:
            return
        self._append(fault.policy_entry())

    def _add_condition(self):
        self._append({"type": "CONDITION", "target": "", "condition": ">", "value": 0.0, "action": "ABORT"})

    def _add_recommended(self):
        added = False
        for name in RECOMMENDED_FAULTS:
            fault = self.catalog.fault(name=name)
            if fault is not None and not self._present(name):
                self.policy.append(fault.policy_entry())
                added = True
        if added:
            self.render(len(self.policy) - 1)
            self.edited.emit()

    def _remove(self):
        row = self.list.currentRow()
        if 0 <= row < len(self.policy):
            del self.policy[row]
            self.render(min(row, len(self.policy) - 1))
            self.edited.emit()
