"""Recipe builder: the properties form of the selected step (recursively, for PARALLEL sub-steps)."""
import os

from PyQt6.QtWidgets import (QComboBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                             QPushButton, QTextEdit, QVBoxLayout, QWidget)

from src.gui.control.dialogs.recipe_builder_defaults import ROUTINE_DEFAULTS, STEP_ICONS
from src.gui.control.dialogs.tag_selector_dialog import TagSelectorDialog


class StepFormMixin:
    """Mixed into RecipeBuilderDialog. Needs self.config_dir and self._on_step_selected()."""

    def _open_tag_selector(self, line_edit_widget):
        dlg = TagSelectorDialog(self)
        if dlg.exec():
            tag = dlg.get_selected_tag()
            if tag: line_edit_widget.setText(tag)

    # --- REFACTORED PROPERTIES PANE GENERATOR ---

    def _update_val(self, target_dict: dict, key: str, value, update_cb=None):
        if isinstance(value, str):
            if value.lower() == "true": value = True
            elif value.lower() == "false": value = False
            else:
                try: value = float(value) if "." in value or "e" in value.lower() else int(value)
                except ValueError: pass
        target_dict[key] = value
        if key == "comment" and update_cb: update_cb()

    # USER_PROMPT verify conditions (rebuilding the form shows the new list; USER_PROMPT steps are
    # top-level only - PARALLEL can't hold one - so the selected step's form is the one to rebuild)
    def _add_verify_cond(self, step_data: dict):
        step_data.setdefault("verify_conditions", []).append({"target": "", "condition": "==", "value": 1.0})
        self._on_step_selected()

    def _del_verify_cond(self, step_data: dict, idx: int):
        conds = step_data.get("verify_conditions", [])
        if 0 <= idx < len(conds):
            del conds[idx]
        self._on_step_selected()

    def _build_properties_form(self, form: QFormLayout, step_data: dict, update_cb):
        """Recursively builds the property UI. Can call itself for PARALLEL sub-steps."""

        # Helper widgets to keep code clean
        def _add_tag_input(label, dict_key):
            le = QLineEdit(step_data.get(dict_key, ""))
            le.setToolTip("Double-click to open ISA-95 Tree Selector.")
            le.mouseDoubleClickEvent = lambda e, w=le: self._open_tag_selector(w)
            le.textChanged.connect(lambda t, s=step_data, k=dict_key: self._update_val(s, k, t, None))
            form.addRow(label, le)
            return le

        def _add_val_input(label, dict_key):
            le = QLineEdit(str(step_data.get(dict_key, 0.0)))
            le.textChanged.connect(lambda t, s=step_data, k=dict_key: self._update_val(s, k, t, None))
            form.addRow(label, le)
            return le

        # 1. Common Property
        le_comment = QLineEdit(step_data.get("comment", ""))
        le_comment.textChanged.connect(lambda t, s=step_data: self._update_val(s, "comment", t, update_cb))
        form.addRow("Comment/Label:", le_comment)

        stype = step_data.get("type")

        # 2. Type-Specific Renderers
        if stype == "ACTION":
            _add_tag_input("Target Tag:", "target")
            _add_val_input("Command Value:", "value")

        elif stype == "WAIT_TIME":
            sb = QDoubleSpinBox()
            sb.setRange(0.0, 36000.0)
            sb.setValue(float(step_data.get("duration_sec", 0.0)))
            sb.valueChanged.connect(lambda v, s=step_data: self._update_val(s, "duration_sec", v, None))
            form.addRow("Duration (sec):", sb)

        elif stype == "WAIT_TELEMETRY":
            _add_tag_input("Target Tag:", "target")
            cb = QComboBox()
            cb.addItems(["==", ">", "<", ">=", "<="])
            cb.setCurrentText(step_data.get("condition", "=="))
            cb.currentTextChanged.connect(lambda t, s=step_data: self._update_val(s, "condition", t, None))
            form.addRow("Condition:", cb)
            _add_val_input("Target Value:", "value")
            sb_to = QDoubleSpinBox()
            sb_to.setRange(0.0, 36000.0)
            sb_to.setValue(float(step_data.get("timeout_sec", 0.0)))
            sb_to.valueChanged.connect(lambda v, s=step_data: self._update_val(s, "timeout_sec", v, None))
            form.addRow("Timeout (sec):", sb_to)

        elif stype == "VERIFIED_ACTION":
            _add_tag_input("Pre-Req Tag:", "pre_target")
            cb = QComboBox()
            cb.addItems(["==", ">", "<", ">=", "<="])
            cb.setCurrentText(step_data.get("pre_condition", "=="))
            cb.currentTextChanged.connect(lambda t, s=step_data: self._update_val(s, "pre_condition", t, None))
            form.addRow("Pre-Req Condition:", cb)
            _add_val_input("Pre-Req Value:", "pre_value")

            _add_tag_input("Command Tag:", "target")
            _add_val_input("Command Value:", "value")

            _add_tag_input("Verify Tag:", "verify_target")
            cb2 = QComboBox()
            cb2.addItems(["==", ">", "<", ">=", "<="])
            cb2.setCurrentText(step_data.get("verify_condition", "=="))
            cb2.currentTextChanged.connect(lambda t, s=step_data: self._update_val(s, "verify_condition", t, None))
            form.addRow("Verify Condition:", cb2)
            _add_val_input("Verify Value:", "verify_value")

            sb_to = QDoubleSpinBox()
            sb_to.setRange(0.0, 3600.0)
            sb_to.setValue(float(step_data.get("timeout_sec", 5.0)))
            sb_to.valueChanged.connect(lambda v, s=step_data: self._update_val(s, "timeout_sec", v, None))
            form.addRow("Timeout (sec):", sb_to)


        elif stype == "PARALLEL":
            form.addRow(QLabel("<hr><b>Parallel Sub-Sequence Builder</b>"))

            w_tools = QWidget()
            l_tools = QHBoxLayout(w_tools)
            l_tools.setContentsMargins(0, 0, 0, 0)
            cb_type = QComboBox()
            cb_type.addItems(["ACTION", "VERIFIED_ACTION", "WAIT_TIME", "WAIT_TELEMETRY", "ROUTINE_CALL"])
            btn_add = QPushButton("➕ Add Sub-Step")
            l_tools.addWidget(cb_type)
            l_tools.addWidget(btn_add)
            form.addRow(w_tools)

            sub_list = QListWidget()
            sub_list.setMinimumHeight(100)
            sub_list.setMaximumHeight(150)
            form.addRow(sub_list)

            sub_host_widget = QWidget()
            sub_host_layout = QVBoxLayout(sub_host_widget)
            sub_host_layout.setContentsMargins(0, 0, 0, 0)
            form.addRow(sub_host_widget)

            sub_state = {"container": None}
            sub_steps = step_data.setdefault("steps", [])

            def _redraw_sub():
                # Mutate items in-place instead of clearing to prevent focus-stealing while typing!
                sub_list.blockSignals(True)
                # Remove excess items if we deleted some
                while sub_list.count() > len(sub_steps):
                    sub_list.takeItem(sub_list.count() - 1)
                # Update or add items
                for i, ss in enumerate(sub_steps):
                    ss["step_id"] = (step_data["step_id"] * 100) + (i + 1)
                    icon = STEP_ICONS.get(ss["type"], "▪️")
                    display_text = f"[{ss['step_id']}] {icon} {ss.get('comment', '')}"
                    item = sub_list.item(i)
                    if item:
                        item.setText(display_text)
                    else:
                        sub_list.addItem(display_text)
                sub_list.blockSignals(False)

            def _add_sub():
                st = cb_type.currentText()
                new_ss = {"step_id": 0, "type": st, "comment": f"New {st}"}
                if st == "ACTION":
                    new_ss.update({"target": "", "value": 0.0})
                elif st == "WAIT_TIME":
                    new_ss.update({"duration_sec": 5.0})
                elif st == "WAIT_TELEMETRY":
                    new_ss.update({"target": "", "condition": "==", "value": 1.0, "timeout_sec": 0.0})
                elif st == "VERIFIED_ACTION":
                    new_ss.update(
                        {"target": "", "value": 1.0, "pre_target": "", "pre_condition": "==", "pre_value": 1.0,
                         "verify_target": "", "verify_condition": "==", "verify_value": 1.0, "timeout_sec": 5.0})
                elif st == "ROUTINE_CALL": new_ss.update({"routine_name": "hv_conditioning", "parameters": dict(
                    ROUTINE_DEFAULTS.get("hv_conditioning", {}))})

                sub_steps.append(new_ss)
                _redraw_sub()
                sub_list.setCurrentRow(len(sub_steps) - 1)

            def _on_sub_sel():
                if sub_state["container"]:
                    sub_state["container"].deleteLater()
                sub_state["container"] = QWidget()
                # Scope the stylesheet to ONLY the parent container so QLineEdits stay native and clickable!
                sub_state["container"].setObjectName("SubBox")
                sub_state["container"].setStyleSheet(
                    "QWidget#SubBox { background-color: #E3F2FD; border-radius: 4px; border: 1px solid #BBDEFB; }")
                sub_form_layout = QFormLayout(sub_state["container"])
                sub_form_layout.setContentsMargins(8, 8, 8, 8)  # Use layout margins instead of CSS padding
                sub_host_layout.addWidget(sub_state["container"])
                r = sub_list.currentRow()
                if r >= 0:
                    btn_del = QPushButton("❌ Delete Sub-Step")
                    btn_del.setStyleSheet("background-color: #FFEBEE; color: #D32F2F; border: 1px solid #D32F2F;")
                    btn_del.clicked.connect(lambda: _del_sub(r))
                    sub_form_layout.addRow(btn_del)
                    self._build_properties_form(sub_form_layout, sub_steps[r], _redraw_sub)

            def _del_sub(idx):
                del sub_steps[idx]
                _redraw_sub()
                if sub_state["container"]:
                    sub_state["container"].deleteLater()
                    sub_state["container"] = None
            btn_add.clicked.connect(_add_sub)
            sub_list.itemSelectionChanged.connect(_on_sub_sel)
            _redraw_sub()

        elif stype == "USER_PROMPT":
            te_prompt = QTextEdit(step_data.get("prompt_text", ""))
            te_prompt.setMaximumHeight(60)
            te_prompt.textChanged.connect(
                lambda s=step_data, w=te_prompt: self._update_val(s, "prompt_text", w.toPlainText()))
            form.addRow("Popup Text:", te_prompt)

            btn_add_cond = QPushButton("➕ Add Verify Condition")
            btn_add_cond.setToolTip("The operator's acknowledgement is refused until every condition holds.")
            btn_add_cond.clicked.connect(lambda _=False, sd=step_data: self._add_verify_cond(sd))
            form.addRow(btn_add_cond)

            for i, cond in enumerate(step_data.get("verify_conditions", [])):
                w = QWidget()
                l = QHBoxLayout(w)
                l.setContentsMargins(0, 0, 0, 0)

                le_t = QLineEdit(cond.get("target", ""))
                le_t.mouseDoubleClickEvent = lambda e, le=le_t: self._open_tag_selector(le)
                le_t.textChanged.connect(lambda t, c=cond: self._update_val(c, "target", t))

                cb_c = QComboBox()
                cb_c.addItems(["==", ">", "<", ">=", "<="])
                cb_c.setCurrentText(cond.get("condition", "=="))
                cb_c.currentTextChanged.connect(lambda t, c=cond: self._update_val(c, "condition", t))

                le_v = QLineEdit(str(cond.get("value", 1.0)))
                le_v.textChanged.connect(lambda t, c=cond: self._update_val(c, "value", t))

                btn_del = QPushButton("❌")
                btn_del.clicked.connect(lambda _, idx=i, sd=step_data: self._del_verify_cond(sd, idx))

                l.addWidget(le_t)
                l.addWidget(cb_c)
                l.addWidget(le_v)
                l.addWidget(btn_del)
                form.addRow(f"Condition {i + 1}:", w)

        elif stype == "SUB_RECIPE":
            cb_recipe = QComboBox()
            if os.path.exists(self.config_dir):
                files = [f for f in os.listdir(self.config_dir) if f.endswith(".json")]
                cb_recipe.addItems([""] + files)
            cb_recipe.setCurrentText(step_data.get("target_file", ""))
            cb_recipe.currentTextChanged.connect(lambda t, s=step_data: self._update_val(s, "target_file", t))
            form.addRow("Target File:", cb_recipe)


        elif stype == "ROUTINE_CALL":
            cb_routine = QComboBox()
            cb_routine.addItems(list(ROUTINE_DEFAULTS.keys()))
            cb_routine.setCurrentText(step_data.get("routine_name", "mass_scan"))
            form.addRow("Routine:", cb_routine)
            # Dynamic container for parameters
            param_container = QWidget()
            param_layout = QFormLayout(param_container)
            param_layout.setContentsMargins(0, 0, 0, 0)
            form.addRow(param_container)

            def _draw_params():
                while param_layout.count():
                    item = param_layout.takeAt(0)
                    if item.widget():
                        item.widget().deleteLater()

                params = step_data.get("parameters", {})
                for k, v in params.items():
                    # If parameter ends in _tag, add double-click ISA-95 selector
                    if k.endswith("_tag"):
                        le = QLineEdit(str(v))
                        le.setToolTip("Double-click to open ISA-95 Tree Selector.")
                        le.mouseDoubleClickEvent = lambda e, w=le: self._open_tag_selector(w)
                        le.textChanged.connect(lambda t, s=params, key=k: self._update_val(s, key, t, None))
                        param_layout.addRow(k, le)
                    else:
                        le = QLineEdit(str(v))
                        le.textChanged.connect(lambda t, s=params, key=k: self._update_val(s, key, t, None))
                        param_layout.addRow(k, le)

            def _on_routine_changed(r_name):
                step_data["routine_name"] = r_name
                step_data["parameters"] = dict(ROUTINE_DEFAULTS.get(r_name, {}))
                _draw_params()
            cb_routine.currentTextChanged.connect(_on_routine_changed)
            _draw_params()
