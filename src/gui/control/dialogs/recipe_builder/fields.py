"""The recipe builder's fields. Each edits one part of a step and says what it means:

    TagField        a tag, typed by its name or by what it is called ("extraction volt..."), or
                    chosen from the machine's tags (TagPicker); says what the tag is, its unit and
                    its range, or that the machine doesn't know it
    ValueField      a value right for a tag: On / Off for a switch, the coded values of a tag that
                    has them, a number with its unit otherwise (text is never turned into a number,
                    or a number into text, behind the author's back)
    ConditionRow    a tag, a comparison and a value (and a tolerance for "is within ±")
    NumberField     a number with a unit
    DurationField   a time in s, min or h (optionally "no time limit")
    SupplyField     one of the supplies a "switch on and set" step can drive
    RecipeFileField a saved recipe, by name
    FaultField      a PLC fault from fault_config.json
"""
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QCompleter, QDialog, QDialogButtonBox, QDoubleSpinBox,
                             QHBoxLayout, QLabel, QLineEdit, QPushButton, QTreeWidget, QTreeWidgetItem,
                             QVBoxLayout, QWidget)

from src.core.conditions import CONDITIONS, number
from src.core.recipe_catalog import subsystem_name
from src.gui import theme
from src.gui.style import set_role

SEARCH_ROLE = Qt.ItemDataRole.UserRole + 1
CUP_CMD = "ion_beam.beamline.diagnostics.cmd_insert_fc"
GV_CMD = "ion_beam.source.chamber.cmd_open_gv"
_KEEP = object()


def _row(*widgets, stretch_last=False) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    for w in widgets:
        row.addWidget(w)
    if not stretch_last:
        row.addStretch(1)
    return row


def parse_number(text: str):
    """A typed number as a float, None for nothing typed, or the text itself if it isn't a number
    (kept as typed: the recipe check then says what is wrong with it)."""
    text = str(text).strip()
    if not text:
        return None
    x = number(text)
    return x if x is not None else text


def show_number(value) -> str:
    if value is None:
        return ""
    x = number(value)
    if x is None or isinstance(value, str):
        return str(value)
    return f"{x:g}" if abs(x) >= 1e-3 or x == 0 else f"{x:.3g}"


# ============================================================================ tags
def _tag_model(tags, parent) -> QStandardItemModel:
    model = QStandardItemModel(parent)
    for t in tags:
        item = QStandardItem(f"{t.label}   ({t.name})")
        item.setData(t.name, Qt.ItemDataRole.UserRole)
        item.setData(f"{t.label} {t.name}".lower(), SEARCH_ROLE)
        item.setToolTip(t.description or t.label)
        model.appendRow(item)
    return model


class _TagCompleter(QCompleter):
    """Matches on what a tag is called and on its name; puts the name in the field."""

    def pathFromIndex(self, index):
        return index.data(Qt.ItemDataRole.UserRole) or ""


class TagPicker(QDialog):
    """Every tag the field takes, grouped by part of the machine, with a search box."""

    def __init__(self, tags, current: str = "", parent=None, title: str = "Choose a tag"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(820, 560)
        layout = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setObjectName("TagSearch")
        self.search.setPlaceholderText("Search: what it is called or its tag, e.g. extraction voltage, VG1, magnet")
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Unit", "Range", "Tag"])
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        layout.addWidget(self.tree, 1)
        groups = {}
        for t in tags:
            group = subsystem_name(t.subsystem) or t.subsystem
            parent_item = groups.get(group)
            if parent_item is None:
                parent_item = groups[group] = QTreeWidgetItem([group[:1].upper() + group[1:]])
                parent_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            item = QTreeWidgetItem([t.label[:1].upper() + t.label[1:], t.unit, t.range_text(), t.name])
            item.setData(0, Qt.ItemDataRole.UserRole, t.name)
            item.setToolTip(0, t.description or t.label)
            item.setFont(3, theme.mono_font())
            parent_item.addChild(item)
            if t.name == current:
                self._current = item
        for name in sorted(groups, key=str.lower):
            self.tree.addTopLevelItem(groups[name])
        self.tree.expandAll()
        for col, width in ((0, 330), (1, 50), (2, 120)):
            self.tree.setColumnWidth(col, width)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        layout.addWidget(buttons)
        self.search.textChanged.connect(self._filter)
        self.tree.currentItemChanged.connect(lambda *_: self.ok.setEnabled(bool(self.selected())))
        self.tree.itemDoubleClicked.connect(lambda item, _col: item.data(0, Qt.ItemDataRole.UserRole) and self.accept())
        self.ok.setEnabled(False)
        current_item = getattr(self, "_current", None)
        if current_item is not None:
            self.tree.setCurrentItem(current_item)
            self.tree.scrollToItem(current_item)
        self.search.setFocus()

    def _filter(self, text):
        words = text.lower().split()
        for i in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(i)
            shown = 0
            for j in range(group.childCount()):
                item = group.child(j)
                hay = f"{group.text(0)} {item.text(0)} {item.text(3)}".lower()
                hide = not all(w in hay for w in words)
                item.setHidden(hide)
                shown += not hide
            group.setHidden(shown == 0)

    def selected(self) -> str:
        item = self.tree.currentItem()
        return (item.data(0, Qt.ItemDataRole.UserRole) or "") if item is not None else ""


class TagField(QWidget):
    """A tag: typed, completed from what it is called, or chosen. role: 'settable' (commands and
    setpoints a recipe may send) or 'readable' (anything to wait on or check)."""
    changed = pyqtSignal(str)

    def __init__(self, catalog, role: str = "settable", tag: str = "", parent=None, placeholder: str = ""):
        super().__init__(parent)
        self.catalog, self.role = catalog, role
        self.placeholder = placeholder
        self.edit = QLineEdit(tag or "")
        self.edit.setObjectName("TagEdit")
        self.edit.setFont(theme.mono_font())
        self.edit.setClearButtonEnabled(True)
        self.edit.setPlaceholderText(placeholder or "type part of what it is called, e.g. extraction voltage")
        self.tags = catalog.settable() if role == "settable" else catalog.readable()
        self.completer = _TagCompleter(_tag_model(self.tags, self), self)
        self.completer.setCompletionRole(SEARCH_ROLE)
        self.completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer.setMaxVisibleItems(14)
        self.edit.setCompleter(self.completer)
        self.btn = QPushButton("Choose…")
        self.btn.setToolTip("Pick from every tag a recipe can " + ("set" if role == "settable" else "read") +
                            ", grouped by part of the machine")
        self.btn.clicked.connect(self._choose)
        self.caption = QLabel()
        self.caption.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addLayout(_row(self.edit, self.btn, stretch_last=True))
        layout.addWidget(self.caption)
        self.edit.setCursorPosition(0)
        self.edit.textChanged.connect(self._on_text)
        self._show_caption()

    def tag(self) -> str:
        return self.edit.text().strip()

    def set_tag(self, tag: str):
        self.edit.blockSignals(True)
        self.edit.setText(tag or "")
        self.edit.setCursorPosition(0)          # the start of a long name, where the part of the machine is
        self.edit.blockSignals(False)
        self._show_caption()

    def _on_text(self, _text):
        self._show_caption()
        self.changed.emit(self.tag())

    def _choose(self):
        picker = TagPicker(self.tags, self.tag(), self, "Choose a tag to " + ("set" if self.role == "settable" else
                                                                              "read"))
        if picker.exec() and picker.selected():
            self.edit.setText(picker.selected())

    def _show_caption(self):
        tag = self.tag()
        t = self.catalog.tag(tag) if tag else None
        if not tag:
            text, role = "", "hint"
        elif t is None:
            known = bool(self.catalog.registry)
            text = "Not a tag the machine knows" if known else "The tag registry isn't loaded: this tag can't be checked"
            role = "error" if known else "hint"
        else:
            parts = [t.label[:1].upper() + t.label[1:]]
            if t.unit:
                parts.append(t.unit)
            if t.range_text():
                parts.append(f"range {t.range_text()}")
            text, role = ", ".join(parts), "caption"
            if self.role == "settable" and not t.settable:
                text, role = f"{text}. A recipe can't set it: {t.not_settable}", "error"
        self.caption.setText(text)
        self.caption.setVisible(bool(text))
        set_role(self.caption, role)


# ============================================================================ values
def switch_words(tag: str, readable: bool = False) -> tuple:
    """What 1 and 0 are called for this tag."""
    if tag == CUP_CMD:
        return "In (insert)", "Out (retract)"
    if tag == GV_CMD:
        return "Open", "Closed"
    if readable:
        return "1 (true)", "0 (false)"
    return "On", "Off"


class NumberField(QWidget):
    """A number with its unit. What isn't a number is kept as typed (the check says so)."""
    changed = pyqtSignal(object)

    def __init__(self, value=None, unit: str = "", placeholder: str = "", parent=None, whole: bool = False):
        super().__init__(parent)
        self.whole = whole
        self.edit = QLineEdit(show_number(value))
        self.edit.setPlaceholderText(placeholder)
        self.edit.setMaximumWidth(160)
        self.unit = QLabel(unit)
        set_role(self.unit, "caption")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.edit)
        layout.addWidget(self.unit)
        layout.addStretch(1)
        self.edit.textChanged.connect(lambda _t: self.changed.emit(self.value()))

    def value(self):
        x = parse_number(self.edit.text())
        if self.whole and isinstance(x, float) and x.is_integer():
            return int(x)
        return x

    def set_value(self, value, unit: str = None):
        self.edit.blockSignals(True)
        self.edit.setText(show_number(value))
        self.edit.blockSignals(False)
        if unit is not None:
            self.unit.setText(unit)


class ValueField(QWidget):
    """A value for a tag: On/Off for a switch, its coded values for a tag that has them, a number
    with the tag's unit otherwise. set_tag() when the tag changes."""
    changed = pyqtSignal(object)

    def __init__(self, catalog, tag: str = "", value=None, readable: bool = False, parent=None,
                 placeholder: str = ""):
        super().__init__(parent)
        self.catalog = catalog
        self.readable = readable
        self.placeholder = placeholder
        self.combo = QComboBox()
        self.combo.setObjectName("ValueChoice")
        self.combo.setMinimumWidth(140)
        self.number = NumberField()
        self.number.setObjectName("ValueNumber")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.combo)
        layout.addWidget(self.number, 1)
        self.mode = "number"
        self.tag_meta = None
        self.combo.currentIndexChanged.connect(lambda _i: self.changed.emit(self.value()))
        self.number.changed.connect(lambda _v: self.changed.emit(self.value()))
        self.set_tag(tag, value)

    def set_tag(self, tag: str, value=_KEEP):
        if value is _KEEP:
            value = self.value()
        t = self.catalog.tag(tag) if tag else None
        self.tag_meta = t
        self.combo.blockSignals(True)
        self.combo.clear()
        if t is not None and t.is_switch:
            self.mode = "switch"
            on, off = switch_words(tag, self.readable)
            if value is None or value == "":
                self.combo.addItem("Choose…" if not self.placeholder else self.placeholder, None)
            self.combo.addItem(on, 1)
            self.combo.addItem(off, 0)
            x = number(value)
            if x in (0.0, 1.0):
                self.combo.setCurrentIndex(self.combo.findData(int(x)))
            elif value not in (None, ""):
                self.combo.addItem(f"{value!r} (not on or off)", value)
                self.combo.setCurrentIndex(self.combo.count() - 1)
        elif t is not None and t.choices:
            self.mode = "choice"
            if value is None or value == "":
                self.combo.addItem("Choose…", None)
            for v, meaning in t.choices:
                self.combo.addItem(f"{v:g}: {meaning}", int(v) if float(v).is_integer() else v)
            index = self.combo.findData(value if not isinstance(value, float) or not value.is_integer() else int(value))
            if index >= 0:
                self.combo.setCurrentIndex(index)
            elif value not in (None, ""):
                self.combo.addItem(f"{value!r} (not one of its values)", value)
                self.combo.setCurrentIndex(self.combo.count() - 1)
        else:
            self.mode = "number"
            unit = t.unit if t is not None else ""
            hint = self.placeholder or (f"{t.range_text()}" if t is not None and t.range_text() else "a number")
            self.number.edit.setPlaceholderText(hint)
            self.number.whole = bool(t is not None and t.is_whole)
            self.number.set_value(value, unit)
        self.combo.blockSignals(False)
        self.combo.setVisible(self.mode != "number")
        self.number.setVisible(self.mode == "number")

    def value(self):
        if self.mode == "number":
            return self.number.value()
        data = self.combo.currentData()
        if data is None:
            return None
        if self.mode == "switch" and data in (0, 1):
            # a BOOL tag takes true/false; a whole-number switch (the caesium heater) takes 1/0
            return bool(data) if self.tag_meta is not None and self.tag_meta.is_bool else data
        return data


class ConditionRow(QWidget):
    """A tag, a comparison and a value (and a tolerance for 'is within ±'), written into `data`
    under `keys` (target, condition, value, tolerance - or a step's own names for them)."""
    changed = pyqtSignal()
    removed = pyqtSignal()

    def __init__(self, catalog, data: dict, keys=("target", "condition", "value", "tolerance"), removable=False,
                 tag_placeholder: str = "", value_placeholder: str = "", parent=None):
        super().__init__(parent)
        self.data = data
        self.k_tag, self.k_op, self.k_val, self.k_tol = keys
        self.tag = TagField(catalog, "readable", data.get(self.k_tag, ""), placeholder=tag_placeholder)
        self.op = QComboBox()
        self.op.setObjectName("ConditionOp")
        for op, words in CONDITIONS.items():
            self.op.addItem(words + (" ±" if op == "within" else ""), op)
        op = data.get(self.k_op, "==")
        if self.op.findData(op) < 0:
            self.op.addItem(f"{op!r} (unknown)", op)
        self.op.setCurrentIndex(self.op.findData(op))
        self.value = ValueField(catalog, data.get(self.k_tag, "") or "", data.get(self.k_val), readable=True,
                                placeholder=value_placeholder)
        self.tol_label = QLabel("±")
        self.tol = NumberField(data.get(self.k_tol), placeholder="tolerance")
        self.btn_remove = QPushButton("Remove")
        self.btn_remove.setVisible(removable)
        self.btn_remove.clicked.connect(self.removed.emit)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.tag)
        line = QHBoxLayout()
        line.setContentsMargins(0, 0, 0, 0)
        for w in (self.op, self.value, self.tol_label, self.tol):
            line.addWidget(w)
        line.addStretch(1)
        line.addWidget(self.btn_remove)
        layout.addLayout(line)
        self._show_tolerance()
        self.tag.changed.connect(self._on_tag)
        self.op.currentIndexChanged.connect(self._on_op)
        self.value.changed.connect(self._on_value)
        self.tol.changed.connect(self._on_tol)

    def _show_tolerance(self):
        within = self.op.currentData() == "within"
        self.tol_label.setVisible(within)
        self.tol.setVisible(within)

    def _on_tag(self, tag):
        self.data[self.k_tag] = tag
        self.value.set_tag(tag)
        self._store_value(self.value.value())
        self.changed.emit()

    def _on_op(self, _index):
        self.data[self.k_op] = self.op.currentData()
        if self.op.currentData() != "within":
            self.data.pop(self.k_tol, None)
        else:
            self._on_tol(self.tol.value(), emit=False)
        self._show_tolerance()
        self.changed.emit()

    def _store_value(self, value):
        if value is None and self.value.placeholder:
            self.data.pop(self.k_val, None)          # (the step's default: the value it sets)
        else:
            self.data[self.k_val] = value

    def _on_value(self, value):
        self._store_value(value)
        self.changed.emit()

    def _on_tol(self, value, emit=True):
        if value is None:
            self.data.pop(self.k_tol, None)
        else:
            self.data[self.k_tol] = value
        if emit:
            self.changed.emit()


# ============================================================================ times
class DurationField(QWidget):
    """A time in s, min or h, kept in seconds. none_text: a box for 'no time limit' (0)."""
    changed = pyqtSignal(float)
    UNITS = (("s", 1.0), ("min", 60.0), ("h", 3600.0))

    def __init__(self, seconds=0.0, none_text: str = "", parent=None):
        super().__init__(parent)
        self.spin = QDoubleSpinBox()
        self.spin.setObjectName("Duration")
        self.spin.setDecimals(2)
        self.spin.setRange(0.0, 1e6)
        self.spin.setMinimumWidth(110)
        self.unit = QComboBox()
        for name, factor in self.UNITS:
            self.unit.addItem(name, factor)
        self.none = QCheckBox(none_text) if none_text else None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.spin)
        layout.addWidget(self.unit)
        if self.none is not None:
            layout.addWidget(self.none)
        layout.addStretch(1)
        self.set_seconds(seconds)
        self.spin.valueChanged.connect(lambda _v: self.changed.emit(self.seconds()))
        self.unit.currentIndexChanged.connect(lambda _i: self.changed.emit(self.seconds()))
        if self.none is not None:
            self.none.toggled.connect(self._on_none)

    def set_seconds(self, seconds):
        s = number(seconds)
        s = 0.0 if s is None else max(0.0, s)
        factor = 3600.0 if s >= 3600 and s % 3600 == 0 else 60.0 if s >= 60 and s % 60 == 0 else 1.0
        for w in (self.spin, self.unit):
            w.blockSignals(True)
        self.unit.setCurrentIndex(self.unit.findData(factor))
        self.spin.setValue(s / factor)
        if self.none is not None:
            self.none.blockSignals(True)
            self.none.setChecked(s == 0)
            self.none.blockSignals(False)
            self.spin.setEnabled(s != 0)
            self.unit.setEnabled(s != 0)
        for w in (self.spin, self.unit):
            w.blockSignals(False)

    def _on_none(self, checked):
        self.spin.setEnabled(not checked)
        self.unit.setEnabled(not checked)
        if not checked and self.spin.value() == 0:
            self.spin.blockSignals(True)
            self.spin.setValue(10.0)
            self.unit.setCurrentIndex(self.unit.findData(60.0))
            self.spin.blockSignals(False)
        self.changed.emit(self.seconds())

    def seconds(self) -> float:
        if self.none is not None and self.none.isChecked():
            return 0.0
        return round(self.spin.value() * (self.unit.currentData() or 1.0), 6)


# ============================================================================ choices
class SupplyField(QComboBox):
    """One of the supplies a 'switch on and set it' step can drive (src/core/recipe_catalog.py)."""
    chosen = pyqtSignal(object)         # the Supply, or None

    def __init__(self, catalog, current_key: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("Supply")
        self.catalog = catalog
        self.addItem("Choose a supply…", None)
        for s in catalog.supplies:
            self.addItem(s.name, s.key)
        index = self.findData(current_key) if current_key else 0
        if index < 0:
            self.addItem(f"Other: {current_key}", current_key)
            index = self.count() - 1
        self.setCurrentIndex(index)
        self.currentIndexChanged.connect(lambda _i: self.chosen.emit(self.supply()))

    def supply(self):
        key = self.currentData()
        return self.catalog.supply(key) if key else None


class RecipeFileField(QComboBox):
    """A saved recipe (a SUB_RECIPE step's file), shown by name."""
    picked = pyqtSignal(str)

    def __init__(self, recipes, current: str = "", parent=None):
        """recipes: [(file, recipe name)] - without the recipe being edited."""
        super().__init__(parent)
        self.setObjectName("RecipeFile")
        self.addItem("Choose a recipe…", "")
        for file, name in recipes:
            self.addItem(f"{name} ({file})" if name else file, file)
        index = self.findData(current) if current else 0
        if index < 0:
            self.addItem(f"{current} (not in the recipes folder)", current)
            index = self.count() - 1
        self.setCurrentIndex(index)
        self.currentIndexChanged.connect(lambda _i: self.picked.emit(self.currentData() or ""))


class FaultField(QComboBox):
    """A PLC fault from fault_config.json. One the file doesn't have stays shown as it is (it is
    never swapped for another fault without the author choosing one)."""
    picked = pyqtSignal(object)         # the policy entry fields: {registry_tag, bit_str, name}

    def __init__(self, catalog, entry: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("Fault")
        current = None
        for f in catalog.faults:
            self.addItem(f"{f.name.replace('_', ' ')}: {f.description}".rstrip(": "),
                         {"registry_tag": f.word_tag, "bit_str": f.bit_str, "name": f.name})
            if f.word_tag == entry.get("registry_tag") and f.bit_str == entry.get("bit_str"):
                current = self.count() - 1
        if current is None:
            self.insertItem(0, f"{entry.get('name') or '?'} ({entry.get('registry_tag')} bit {entry.get('bit_str')}) - "
                               "not a fault in fault_config.json",
                            {"registry_tag": entry.get("registry_tag"), "bit_str": entry.get("bit_str"),
                             "name": entry.get("name")})
            current = 0
        self.setCurrentIndex(current)
        self.currentIndexChanged.connect(lambda _i: self.picked.emit(self.currentData()))
