"""The recipe builder's step editor: a form for the selected step, made for what that step does
(not a list of its JSON keys). Every field writes into the step as it is edited and the builder
re-checks the recipe; the form shows the step's problems and what it does, in words."""
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QFormLayout, QFrame, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
                             QScrollArea, QVBoxLayout, QWidget)

from src.core.conditions import number
from src.core.recipe_text import describe_step
from src.core.routines import ROUTINE_REGISTRY
from src.gui.control.dialogs.recipe_builder.blocks import block_for
from src.gui.control.dialogs.recipe_builder.fields import (ConditionRow, DurationField, NumberField, RecipeFileField,
                                                            SupplyField, TagField, ValueField)
from src.gui.style import set_prop, set_role

# Parameters the supply chooser fills in (shown under "More settings" as tags)
SUPPLY_TAGS = {"psu_control": ("cmd_enable", "stat_enabled", "sp_tag", "rb_tag"),
               "hv_conditioning": ("cmd_tag", "rb_tag", "enabled_tag")}


def _attach(step: dict, key: str, value):
    """Put a list or dict the form made into the step, on the first edit (showing a step never
    changes it: the recipe would read as changed)."""
    if step.get(key) is not value:
        step[key] = value


def _label(text: str, role: str = "caption", wrap: bool = True) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(wrap)
    set_role(lbl, role)
    return lbl


class _More(QWidget):
    """'More settings': a part of the form shown on request."""

    def __init__(self, title: str = "More settings"):
        super().__init__()
        self.button = QPushButton(f"▸ {title}")
        set_role(self.button, "flat")
        self.button.setCheckable(True)
        self.body = QWidget()
        self.form = QFormLayout(self.body)
        self.form.setContentsMargins(12, 0, 0, 0)
        self.body.hide()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.addWidget(self.button, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.body)
        self.button.toggled.connect(lambda on: (self.body.setVisible(on),
                                                self.button.setText(("▾ " if on else "▸ ") + title)))


class StepEditor(QScrollArea):
    """The form for one step. edited: a field changed the step."""
    edited = pyqtSignal()

    def __init__(self, catalog, recipes=lambda: [], recipe_names=lambda: {}, parent=None):
        """recipes(): [(file, name)] a sub-recipe step may run; recipe_names(): {file: name}."""
        super().__init__(parent)
        self.setObjectName("StepEditor")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.catalog = catalog
        self.recipes = recipes
        self.recipe_names = recipe_names
        self.step = None
        self.path = None
        self._empty()

    # ------------------------------------------------------------------ building
    def _empty(self, text: str = "Select a step to edit it, or add one from the blocks on the left."):
        self.step, self.path = None, None
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.addWidget(_label(text, "hint"))
        layout.addStretch(1)
        self._set_body(body)
        self.lbl_problems = self.lbl_text = None

    def _set_body(self, body: QWidget):
        """Swap the form. (setWidget would delete the old one at once - even while one of its own
        fields is still in the middle of telling us about a change.)"""
        old = self.takeWidget()
        if old is not None:
            old.hide()
            old.deleteLater()
        self.setWidget(body)

    def clear(self):
        self._empty()

    def show_step(self, step: dict, path: tuple, label: str = ""):
        """Build the form for `step` (at `path` in the outline). `label` replaces "Step n" in the heading
        (for a step of a draft that won't be in the recipe, so has no number)."""
        if not isinstance(step, dict):
            self._empty("This step isn't a JSON object: it can only be deleted.")
            return
        self.step, self.path = step, path
        block = block_for(step)
        body = QWidget()
        body.setObjectName("StepForm")
        layout = QVBoxLayout(body)
        layout.setSpacing(8)
        title = block.title if block is not None else f"{step.get('type')} step"
        label = label or f"Step {'.'.join(str(i + 1) for i in path)}"
        layout.addWidget(_label(f"{label}: {title}", "heading"))
        if block is not None:
            layout.addWidget(_label(block.help, "hint"))
        self.lbl_problems = QLabel()
        self.lbl_problems.setObjectName("StepProblems")
        self.lbl_problems.setWordWrap(True)
        set_role(self.lbl_problems, "note")
        self.lbl_problems.hide()
        layout.addWidget(self.lbl_problems)
        card = QFrame()
        set_role(card, "card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(10, 8, 10, 8)
        card_layout.addWidget(_label("What it does", "caption"))
        self.lbl_text = _label("", "title")
        self.lbl_text.setObjectName("StepText")
        self.lbl_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        card_layout.addWidget(self.lbl_text)
        layout.addWidget(card)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        layout.addLayout(form)
        build = getattr(self, f"_form_{str(step.get('type', '')).lower()}", None)
        if build is None:
            layout.addWidget(_label(f"The sequencer doesn't know the step type {step.get('type')!r}: change the "
                                    "file by hand, or delete the step.", "error"))
        else:
            build(form, step, layout)
        note = QLineEdit(str(step.get("comment") or ""))
        note.setObjectName("StepNote")
        note.setPlaceholderText("Optional: what this step is for (shown under it in the list)")
        note.textChanged.connect(self._on_note)
        layout.addWidget(_label("Note", "caption"))
        layout.addWidget(note)
        layout.addStretch(1)
        self._set_body(body)
        self.refresh_text()

    def _on_note(self, text):
        if text.strip():
            self.step["comment"] = text
        else:
            self.step.pop("comment", None)
        self._edited()

    def _edited(self):
        self.refresh_text()
        self.edited.emit()

    def refresh_text(self):
        if self.step is not None and self.lbl_text is not None:
            self.lbl_text.setText(describe_step(self.step, self.catalog, self.recipe_names()))

    def show_problems(self, problems, notice: str = ""):
        """This step's problems (src/core/recipe_check.py Problem), shown above its form, after a
        notice if there is one (what a draft does to the step)."""
        if self.lbl_problems is None:
            return
        if not problems and not notice:
            self.lbl_problems.hide()
            return
        errors = [p for p in problems if p.level == "error"]
        lines = ([notice] if notice else []) + [("Error: " if p.level == "error" else "Warning: ") + p.message
                                                for p in problems]
        self.lbl_problems.setText("\n".join(lines))
        set_prop(self.lbl_problems, "level", "fault" if errors else "warn" if problems else "info")
        self.lbl_problems.show()

    # ------------------------------------------------------------------ helpers
    def _set(self, data: dict, key: str, value, drop_none: bool = False):
        if value is None and drop_none:
            data.pop(key, None)
        else:
            data[key] = value
        self._edited()

    def _duration(self, form, label, data, key, default, none_text=""):
        field = DurationField(data.get(key, default), none_text=none_text)
        field.changed.connect(lambda s: self._set(data, key, s))
        form.addRow(label, field)
        return field

    # ------------------------------------------------------------------ the forms
    def _form_action(self, form, step, layout):
        tag = TagField(self.catalog, "settable", step.get("target", ""))
        tag.setObjectName("SetTag")
        value = ValueField(self.catalog, step.get("target", ""), step.get("value"))
        value.setObjectName("SetValue")
        tag.changed.connect(lambda t: (step.__setitem__("target", t), value.set_tag(t),
                                       self._set(step, "value", value.value())))
        value.changed.connect(lambda v: self._set(step, "value", v))
        form.addRow("Set", tag)
        form.addRow("To", value)

    def _form_verified_action(self, form, step, layout):
        pre = QCheckBox("First check a condition (the step fails at once if it doesn't hold)")
        pre.setChecked(bool(step.get("pre_target")))
        pre_row = ConditionRow(self.catalog, step, ("pre_target", "pre_condition", "pre_value", "pre_tolerance"))
        pre_row.setVisible(pre.isChecked())
        pre_row.changed.connect(self._edited)

        def toggle_pre(on):
            pre_row.setVisible(on)
            if not on:
                for k in ("pre_target", "pre_condition", "pre_value", "pre_tolerance"):
                    step.pop(k, None)
                pre_row.tag.set_tag("")
            else:
                step.setdefault("pre_condition", "==")
                step.setdefault("pre_value", 1.0)
            self._edited()
        pre.toggled.connect(toggle_pre)
        form.addRow(pre)
        form.addRow(pre_row)
        self._form_action(form, step, layout)
        check = ConditionRow(self.catalog, step, ("verify_target", "verify_condition", "verify_value",
                                                  "verify_tolerance"),
                             tag_placeholder="empty: the tag it sets", value_placeholder="the value it sets")
        check.changed.connect(self._edited)
        form.addRow("Then check", check)
        self._duration(form, "Within", step, "timeout_sec", 5.0)

    def _form_wait_time(self, form, step, layout):
        self._duration(form, "Wait", step, "duration_sec", 0.0)

    def _form_wait_telemetry(self, form, step, layout):
        row = ConditionRow(self.catalog, step)
        row.changed.connect(self._edited)
        form.addRow("Until", row)
        self._duration(form, "Fail after", step, "timeout_sec", 0.0, none_text="No time limit")

    def _form_user_prompt(self, form, step, layout):
        text = QPlainTextEdit(str(step.get("prompt_text") or ""))
        text.setObjectName("PromptText")
        text.setPlaceholderText("What the operator should do or check, e.g. Close the source deck doors.")
        text.setMaximumHeight(90)
        text.textChanged.connect(lambda: self._set(step, "prompt_text", text.toPlainText()))
        form.addRow("Message", text)
        conditions = step.get("verify_conditions")
        if not isinstance(conditions, list):
            conditions = []                          # (made part of the step when one is added)
        box = QWidget()
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(0, 0, 0, 0)
        hint = _label("OK is refused until each of these holds (live readings). None: OK is always accepted.",
                      "hint")
        box_layout.addWidget(hint)
        rows = QVBoxLayout()
        box_layout.addLayout(rows)
        add = QPushButton("Add a condition")
        add.setObjectName("AddCondition")
        box_layout.addWidget(add, alignment=Qt.AlignmentFlag.AlignLeft)

        def draw():
            while rows.count():
                item = rows.takeAt(0)
                if item.widget() is not None:
                    item.widget().deleteLater()
            for cond in conditions:
                if not isinstance(cond, dict):
                    continue
                row = ConditionRow(self.catalog, cond, removable=True)
                row.changed.connect(self._edited)
                row.removed.connect(lambda c=cond: (conditions.remove(c), draw(), self._edited()))
                frame = QFrame()
                set_role(frame, "card")
                inner = QVBoxLayout(frame)
                inner.setContentsMargins(8, 6, 8, 6)
                inner.addWidget(row)
                rows.addWidget(frame)

        add.clicked.connect(lambda: (_attach(step, "verify_conditions", conditions),
                                     conditions.append({"target": "", "condition": "==", "value": 1}), draw(),
                                     self._edited()))
        draw()
        form.addRow("Accept OK only once", box)

    def _form_sub_recipe(self, form, step, layout):
        field = RecipeFileField(self.recipes(), str(step.get("target_file") or ""))
        field.picked.connect(lambda f: self._set(step, "target_file", f))
        form.addRow("Recipe", field)
        layout.addWidget(_label("Its own stop conditions hold while it runs, as well as this recipe's.", "hint"))

    def _form_parallel(self, form, step, layout):
        subs = [s for s in step.get("steps") or [] if isinstance(s, dict)]
        text = ("It is done when every step inside it is. To add steps to it: select it, then add a block; "
                "or drag steps onto it in the list.")
        layout.addWidget(_label(text, "hint"))
        if subs:
            lines = "\n".join(f"{i}. {describe_step(s, self.catalog, self.recipe_names())}"
                              for i, s in enumerate(subs, 1))
            layout.addWidget(_label(lines, "caption"))
        else:
            layout.addWidget(_label("Nothing in it yet.", "warning"))

    def _form_routine_call(self, form, step, layout):
        name = step.get("routine_name")
        params = step.get("parameters")
        if not isinstance(params, dict):
            params = {}                              # (made part of the step on the first edit)
        cls = ROUTINE_REGISTRY.get(name)
        if cls is None:
            layout.addWidget(_label(f"The sequencer has no routine {name!r} (it has: "
                                    f"{', '.join(sorted(ROUTINE_REGISTRY))}). Delete this step, or add the block "
                                    "you meant.", "error"))
            return
        more = _More()
        supply_keys = SUPPLY_TAGS.get(name, ())
        if supply_keys:
            self._supply_row(form, step, params, name)
        for p in cls.PARAMS:
            if p.key == "gv_stat_closed":
                continue                                  # (read by nothing)
            target = more.form if (p.advanced or p.key in supply_keys) else form
            self._param_row(target, step, params, p, cls)
        if more.form.rowCount():
            layout.addWidget(more)

    def _supply_row(self, form, step, params, routine):
        keys = SUPPLY_TAGS[routine]
        current = str(params.get(keys[0]) or "").rpartition(".")[0]
        field = SupplyField(self.catalog, current)

        def chosen(supply):
            if supply is None:
                return
            _attach(step, "parameters", params)
            if routine == "psu_control":
                params.update({"cmd_enable": supply.enable_tag, "stat_enabled": supply.enabled_tag,
                               "sp_tag": supply.setpoint_tag, "rb_tag": supply.readback_tag,
                               "wait_for_readback": supply.wait_by_default, "tolerance": supply.tolerance,
                               "timeout_sec": float(supply.timeout_s)})
            else:
                params.update({"cmd_tag": supply.setpoint_tag, "rb_tag": supply.readback_tag,
                               "enabled_tag": supply.enabled_tag})
            self._edited()
            self.show_step(step, self.path)               # units and ranges follow the supply
        field.chosen.connect(chosen)
        form.addRow("Supply", field)

    def _param_row(self, form, step, params, p, cls):
        value = p.read(params)
        key = next((n for n in (p.key,) + p.aliases if n in params), p.key)   # an older name stays as it is

        def store(v, drop_none=True):
            _attach(step, "parameters", params)
            if v is None and drop_none:
                params.pop(key, None)
            else:
                params[key] = v
            self._edited()

        if p.kind.endswith("_tag"):
            field = TagField(self.catalog, "settable" if p.kind == "settable_tag" else "readable", value or "")
            field.changed.connect(lambda t: store(t, drop_none=False))
        elif p.kind == "bool":
            field = QCheckBox()
            field.setChecked(bool(number(value)))
            field.toggled.connect(lambda on: store(bool(on)))
        elif p.kind == "seconds":
            field = DurationField(value if value is not None else 0.0)
            field.changed.connect(lambda s: store(s))
        elif p.kind == "text":
            field = QLineEdit(str(value or ""))
            field.textChanged.connect(lambda t: store(t if t else None))
        else:
            unit = p.unit
            if p.unit_of:
                source = next((q for q in cls.PARAMS if q.key == p.unit_of), None)
                tag = source.read(params) if source is not None else ""
                unit = self.catalog.unit(tag) if tag else ""
            field = NumberField(value, unit, whole=p.kind == "integer",
                                placeholder="" if value is not None or not p.required else "needed")
            field.changed.connect(lambda v: store(v))
        if p.help:
            field.setToolTip(p.help)
        field.setObjectName(f"param_{p.key}")
        form.addRow(p.label, field)
