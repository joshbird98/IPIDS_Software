import os
import json
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QSplitter,
    QListWidget, QListWidgetItem, QLineEdit, QTextEdit,
    QPushButton, QLabel, QWidget, QFormLayout,
    QComboBox, QDoubleSpinBox, QGroupBox,
    QScrollArea
)
from PyQt6.QtCore import Qt
from src.gui.control.dialogs.tag_selector_dialog import TagSelectorDialog

from PyQt6.QtCore import QThread, pyqtSignal
from src.core.ai_assistant import RecipeAIAssistant
from PyQt6.QtWidgets import QMessageBox

from src.core.theme import is_dark_mode

# Set it in your PC's environment variables so it isn't hardcoded!
API_KEY = os.environ.get("GEMINI_API_KEY")

COLOR_HEADER = "background-color: #2c3e50; color: white; padding: 4px; font-weight: bold;"
COLOR_AI = "background-color: #f3e5f5; border: 1px solid #ce93d8; border-radius: 4px;"

STEP_ICONS = {
    "ACTION": "⚡", "WAIT_TIME": "⏱️", "WAIT_TELEMETRY": "📡",
    "USER_PROMPT": "🙋", "SUB_RECIPE": "🔗", "ROUTINE_CALL": "⚙️",
    "VERIFIED_ACTION": "🛡️", "PARALLEL": "🔀"
}

# --- Routine Definitions ---
ROUTINE_DEFAULTS = {
    "mass_scan": {
        "sp_target": "ion_beam.beamline.magnet.sp_requested_mass",
        "rb_target": "ion_beam.beamline_diagnostics.rb_fc_current",
        "start_val": 25.0,
        "end_val": 35.0,
        "step_size": 0.2,
        "dwell_sec": 1.0
    },
    "safety_relay_init": {
        "gv_cmd_open": "ion_beam.source.chamber.cmd_open_gv",
        "gv_stat_open": "ion_beam.source.chamber.stat_gv_open",
        "gv_open_perm": "ion_beam.source.chamber.stat_gv_open_permissive",
        "cmd_virtual": "ion_beam.system.cmd_enable_safety",
        "rb_relay": "ion_beam.facilities.safety_relay_active",
        "timeout_sec": 6.0,
        "gv_timeout_sec": 15.0,
        "stabilization_sec": 15.0
    },
    "hv_conditioning": {
        "cmd_tag": "ion_beam.source.extraction.sp_voltage",
        "rb_tag": "ion_beam.source.extraction.rb_voltage",
        "max_val": 30.0,
        "step_size": 1.0,
        "hold_time_sec": 60.0,
        "pressure_tag": "ion_beam.vacuum.vg1_pressure",
        "pressure_max": 5e-5,
        "pressure_recover": 1e-5,
        "arc_mode_tag": "ion_beam.source.extraction.stat_arc_quench_active",
        "backoff_step": 2.0,
        "max_arcs": 5,
        "arc_cooldown_sec": 5.0
    },
    "psu_control": {
            "cmd_enable": "ion_beam.source.filament.cmd_enable",
            "stat_enabled": "ion_beam.source.filament.stat_enabled",
            "sp_tag": "ion_beam.source.filament.sp_requested_current",
            "rb_tag": "ion_beam.source.filament.rb_current",
            "target_val": 5.0,
            "tolerance": 0.2,
            "timeout_sec": 45.0,
            "wait_for_readback": 1.0,
            "requires_relay": 1.0,
            "rb_relay": "ion_beam.facilities.safety_relay_active"
        }
}


class FaultPolicyDialog(QDialog):
    def __init__(self, policy_list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Global Fault Policy")
        self.resize(800, 400)
        self.policy = policy_list
        self.available_faults = self._load_fault_config()

        layout = QVBoxLayout(self)
        btn_add = QPushButton("➕ Add Specific Fault Abort")
        btn_add.clicked.connect(self._add_fault)
        layout.addWidget(btn_add)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.container = QWidget()
        self.form = QFormLayout(self.container)
        scroll.setWidget(self.container)
        layout.addWidget(scroll)

        btn_ok = QPushButton("Done")
        btn_ok.clicked.connect(self.accept)
        layout.addWidget(btn_ok)

        self._redraw()

    def _load_fault_config(self) -> list:
        """Parses fault_config.json into a flat list for the dropdown."""
        faults = []
        try:
            # Adjust path as necessary
            config_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../config/fault_config.json'))
            with open(config_path, 'r') as f:
                data = json.load(f)
                for udt_name, udt_data in data.items():
                    reg_tag = udt_data.get("registry_tag", "")
                    for bit_str, info in udt_data.get("bits", {}).items():
                        faults.append({
                            "registry_tag": reg_tag,
                            "bit_str": bit_str,
                            "name": info.get("name", "Unknown"),
                            "desc": info.get("description", ""),
                            "display": f"[{info.get('name')}] {info.get('description')}"
                        })
        except Exception as e:
            print(f"Error loading faults: {e}")
        return sorted(faults, key=lambda x: x["name"])

    def _add_fault(self):
        # We now store a specialized 'BIT_FAULT' dict
        self.policy.append({
            "type": "BIT_FAULT",
            "registry_tag": "",
            "bit_str": "",
            "name": "",
            "action": "ABORT"
        })
        self._redraw()

    def _del_fault(self, idx):
        del self.policy[idx]
        self._redraw()

    def _redraw(self):
        while self.form.count():
            item = self.form.takeAt(0)
            if item.widget(): item.widget().deleteLater()

        for i, fault in enumerate(self.policy):
            w = QWidget()
            l = QHBoxLayout(w)
            l.setContentsMargins(0, 0, 0, 0)

            cb_faults = QComboBox()
            # Populate dropdown
            for af in self.available_faults:
                cb_faults.addItem(af["display"], userData=af)

            # Set current selection
            current_name = fault.get("name")
            if current_name:
                idx = cb_faults.findText(f"[{current_name}]", Qt.MatchFlag.MatchContains)
                if idx >= 0: cb_faults.setCurrentIndex(idx)

            def _on_fault_changed(index, f=fault, cb=cb_faults):
                data = cb.itemData(index)
                if data:
                    f["registry_tag"] = data["registry_tag"]
                    f["bit_str"] = data["bit_str"]
                    f["name"] = data["name"]

            cb_faults.currentIndexChanged.connect(_on_fault_changed)
            # Trigger once to set initial blank value
            if not current_name and cb_faults.count() > 0:
                _on_fault_changed(0)

            btn_del = QPushButton("❌")
            btn_del.clicked.connect(lambda _, index=i: self._del_fault(index))

            l.addWidget(cb_faults, stretch=1)
            l.addWidget(btn_del)
            self.form.addRow(f"Trip {i + 1}:", w)


class SequenceListWidget(QListWidget):
    order_changed = pyqtSignal()

    def dropEvent(self, event):
        super().dropEvent(event)
        self.order_changed.emit()


class RecipeBuilderDialog(QDialog):
    def __init__(self, parent=None, config_dir="", load_filepath=None):
        super().__init__(parent)
        self.setWindowTitle("Interactive Recipe Builder")
        self.resize(1100, 750)
        self.config_dir = config_dir
        self.current_steps = []
        self.fault_policy = []

        self.ai_assistant = RecipeAIAssistant(self.config_dir)
        self.ai_worker = None

        self._init_ui()

        if load_filepath:
            self._load_existing_recipe(load_filepath)

    def _load_existing_recipe(self, filepath):
        try:
            with open(filepath, 'r') as f:
                data = json.load(f)
                self.le_recipe_name.setText(data.get("recipe_name", "New Recipe"))
                self.le_recipe_version.setText(data.get("version", "1.0"))
                self.fault_policy = data.get("fault_policy", [])
                self.current_steps = data.get("steps", [])
                self._redraw_sequence()
        except Exception as e:
            QMessageBox.critical(self, "Load Error", f"Could not load recipe for editing: {e}")

    def _init_ui(self):
        main_layout = QVBoxLayout(self)

        # 1. AI ASSISTANT BLOCK
        ai_group = QGroupBox("✨ AI Recipe Assistant")
        ai_group.setStyleSheet(
            "QGroupBox { border: 1px solid #ce93d8; border-radius: 6px; margin-top: 10px; font-weight: bold; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 3px 0 3px; }"
        )
        ai_group.setMinimumHeight(220)

        ai_layout = QVBoxLayout()
        ai_layout.setContentsMargins(10, 15, 10, 10)

        # Compact Status Light
        status_color = "#4CAF50" if self.ai_assistant.available else "#F44336"
        status_text = "Online" if self.ai_assistant.available else "Offline (Missing API Key)"
        lbl_status = QLabel(
            f"<span style='color:{status_color}; font-size: 14px;'>●</span> <span style='color:#555; font-size: 11px;'>{status_text}</span>")
        lbl_status.setFixedHeight(15)  # Force it to be small
        ai_layout.addWidget(lbl_status)

        # Chat History (Read-Only)
        self.txt_ai_history = QTextEdit()
        self.txt_ai_history.setReadOnly(True)
        self.txt_ai_history.setStyleSheet(
            "border: 1px solid #ce93d8; border-radius: 4px; padding: 5px;")

        # Dynamically set the welcome bubble colors
        bg_col = "#2A2A2A" if is_dark_mode else "#E5E5EA"
        txt_col = "#E0E0E0" if is_dark_mode else "black"

        welcome_html = f"""
                        <table width="100%" border="0" cellspacing="0" cellpadding="0"><tr><td align="left">
                            <table border="0" cellspacing="0" cellpadding="8" style="background-color: {bg_col}; color: {txt_col}; border-radius: 6px;">
                                <tr><td>Hello! Describe the sequence you want to build.</td></tr>
                            </table>
                        </td></tr></table><br>
                        """
        self.txt_ai_history.setHtml(welcome_html)

        ai_layout.addWidget(self.txt_ai_history)

        # Input Area
        input_layout = QHBoxLayout()
        self.txt_ai_prompt = QTextEdit()
        self.txt_ai_prompt.setPlaceholderText("Type your instructions here...")
        self.txt_ai_prompt.setMaximumHeight(45)
        self.txt_ai_prompt.setStyleSheet(
            "background-color: #F0F4F8; border: 2px solid #BBDEFB; border-radius: 6px; padding: 4px; font-size: 12px;")

        self.btn_generate_ai = QPushButton("Generate")
        self.btn_generate_ai.setFixedSize(80, 45)  # Make button a nice square block
        self.btn_generate_ai.setStyleSheet(
            "background-color: #9C27B0; color: white; font-weight: bold; border-radius: 6px;")
        self.btn_generate_ai.clicked.connect(self._on_ai_generate)

        input_layout.addWidget(self.txt_ai_prompt)
        input_layout.addWidget(self.btn_generate_ai)
        ai_layout.addLayout(input_layout)

        ai_group.setLayout(ai_layout)
        main_layout.addWidget(ai_group)

        # 2. METADATA HEADER
        meta_layout = QHBoxLayout()
        meta_layout.addWidget(QLabel("<b>Recipe Name:</b>"))
        self.le_recipe_name = QLineEdit("New Recipe")
        meta_layout.addWidget(self.le_recipe_name)
        meta_layout.addWidget(QLabel("<b>Version:</b>"))
        self.le_recipe_version = QLineEdit("1.0")
        self.le_recipe_version.setFixedWidth(50)
        meta_layout.addWidget(self.le_recipe_version)

        self.btn_faults = QPushButton("⚠️ Configure Fault Policy")
        self.btn_faults.clicked.connect(self._open_fault_policy)
        meta_layout.addWidget(self.btn_faults)
        meta_layout.addStretch()

        self.btn_save = QPushButton("💾 SAVE TO JSON")
        self.btn_save.setStyleSheet("background-color: #4CAF50; color: white; font-weight: bold; padding: 6px;")
        self.btn_save.clicked.connect(self._save_recipe)
        meta_layout.addWidget(self.btn_save)
        main_layout.addLayout(meta_layout)

        # 3. 3-PANE BUILDER
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # PANE A: Toolbox
        pane_toolbox = QWidget()
        layout_tb = QVBoxLayout(pane_toolbox)
        lbl_tb = QLabel("Toolbox")
        lbl_tb.setStyleSheet(COLOR_HEADER)
        layout_tb.addWidget(lbl_tb)
        self.list_toolbox = QListWidget()
        for st, icon in STEP_ICONS.items():
            self.list_toolbox.addItem(QListWidgetItem(f"{icon} {st}"))
        self.list_toolbox.itemDoubleClicked.connect(self._add_step_from_toolbox)
        layout_tb.addWidget(self.list_toolbox)
        splitter.addWidget(pane_toolbox)

        # PANE B: Sequence
        pane_seq = QWidget()
        layout_seq = QVBoxLayout(pane_seq)
        lbl_seq = QLabel("Execution Sequence")
        lbl_seq.setStyleSheet(COLOR_HEADER)
        layout_seq.addWidget(lbl_seq)
        self.list_sequence = SequenceListWidget()
        self.list_sequence.setAlternatingRowColors(True)
        self.list_sequence.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.list_sequence.itemSelectionChanged.connect(self._on_step_selected)
        self.list_sequence.order_changed.connect(self._sync_list_order)
        layout_seq.addWidget(self.list_sequence)
        self.btn_remove_step = QPushButton("❌ Remove Selected Step")
        self.btn_remove_step.clicked.connect(self._remove_selected_step)
        layout_seq.addWidget(self.btn_remove_step)
        splitter.addWidget(pane_seq)

        # PANE C: Properties
        pane_prop = QWidget()
        self.layout_prop = QVBoxLayout(pane_prop)
        lbl_prop = QLabel("Step Properties")
        lbl_prop.setStyleSheet(COLOR_HEADER)
        self.layout_prop.addWidget(lbl_prop)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.prop_container = QWidget()
        self.prop_form = QFormLayout(self.prop_container)
        self.scroll_area.setWidget(self.prop_container)

        self.layout_prop.addWidget(self.scroll_area)
        splitter.addWidget(pane_prop)

        splitter.setSizes([200, 400, 500])
        main_layout.addWidget(splitter)

    # --- LOGIC ---

    def _on_ai_generate(self):
        prompt = self.txt_ai_prompt.toPlainText().strip()
        if not prompt: return
        if not self.ai_assistant.available:
            QMessageBox.warning(self, "AI Offline", "Please configure the API KEY in the .env file.")
            return

        # Package the CURRENT state of the builder
        context = {
            "name": self.le_recipe_name.text(),
            "steps": self.current_steps,
            "faults": self.fault_policy
        }

        user_html = f"""
        <table width="100%" border="0" cellspacing="0" cellpadding="0"><tr><td align="right">
            <table border="0" cellspacing="0" cellpadding="8" style="background-color: #1976D2; color: #FFFFFF; border-radius: 6px;">
                <tr><td>{prompt}</td></tr>
            </table>
        </td></tr></table><br>
        """
        self.txt_ai_history.append(user_html)
        self.txt_ai_prompt.clear()

        self.btn_generate_ai.setText("...")
        self.btn_generate_ai.setEnabled(False)

        self.ai_worker = AIWorker(self.ai_assistant, prompt, context)
        self.ai_worker.sig_finished.connect(self._on_ai_finished)
        self.ai_worker.start()

    def _on_ai_finished(self, response: dict):
        self.btn_generate_ai.setText("Generate")
        self.btn_generate_ai.setEnabled(True)

        status = response.get("status")
        msg = response.get("message", "")

        # Helper to generate AI bubbles
        def make_ai_bubble(text, bg_color, text_color):
            return f"""
            <table width="100%" border="0" cellspacing="0" cellpadding="0"><tr><td align="left">
                <table border="0" cellspacing="0" cellpadding="8" style="background-color: {bg_color}; color: {text_color}; border-radius: 6px;">
                    <tr><td>{text}</td></tr>
                </table>
            </td></tr></table><br>
            """

        if status == "error":
            # Red error bubble
            bg = "#4A0000" if is_dark_mode else "#FFCDD2"
            txt = "#FFB4B4" if is_dark_mode else "#B71C1C"
            self.txt_ai_history.append(make_ai_bubble(f"<b>Error:</b> {msg}", bg, txt))

        elif status == "chat":
            # Standard AI reply bubble
            bg = "#2A2A2A" if is_dark_mode else "#E5E5EA"
            txt = "#E0E0E0" if is_dark_mode else "black"
            self.txt_ai_history.append(make_ai_bubble(msg, bg, txt))

        elif status == "success":
            # Green success bubble
            bg = "#1B5E20" if is_dark_mode else "#C8E6C9"
            txt = "#A5D6A7" if is_dark_mode else "#1B5E20"
            self.txt_ai_history.append(make_ai_bubble("Sequence generated and loaded!", bg, txt))

            recipe_data = response.get("recipe", {})
            self.le_recipe_name.setText(recipe_data.get("recipe_name", "AI Sequence"))
            self.le_recipe_version.setText(recipe_data.get("version", "1.0"))
            self.fault_policy = recipe_data.get("fault_policy", [])
            self.current_steps = recipe_data.get("steps", [])
            self._redraw_sequence()
        else:
            self.txt_ai_history.append(make_ai_bubble("Unknown response format.", "#FFF9C4", "black"))

        scrollbar = self.txt_ai_history.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())


    def _open_fault_policy(self):
        dlg = FaultPolicyDialog(self.fault_policy, self)
        dlg.exec()

    # --- LIST MANAGEMENT LOGIC ---

    def _add_step_from_toolbox(self, item: QListWidgetItem):
        step_type = item.text().split(" ", 1)[1]
        new_step = {
            "step_id": len(self.current_steps) + 1,
            "type": step_type,
            "comment": f"New {step_type}"
        }

        if step_type == "ACTION":
            new_step.update({"target": "", "value": 0.0})
        elif step_type == "WAIT_TIME":
            new_step.update({"duration_sec": 5.0})
        elif step_type == "WAIT_TELEMETRY":
            new_step.update({"target": "", "condition": "==", "value": 1.0, "timeout_sec": 0.0})
        elif step_type == "USER_PROMPT":
            new_step.update({"prompt_text": "Please acknowledge.", "verify_conditions": []})  # List!
        elif step_type == "SUB_RECIPE":
            new_step.update({"target_file": ""})
        elif step_type == "ROUTINE_CALL":
            new_step.update({"routine_name": "mass_scan", "parameters": dict(ROUTINE_DEFAULTS.get("mass_scan", {}))})
        elif step_type == "VERIFIED_ACTION":
            new_step.update({
                "target": "", "value": 1.0,
                "pre_target": "", "pre_condition": "==", "pre_value": 1.0,
                "verify_target": "", "verify_condition": "==", "verify_value": 1.0,
                "timeout_sec": 5.0
            })
        elif step_type == "PARALLEL": new_step.update({"steps": []})

        self.current_steps.append(new_step)
        self._redraw_sequence()

    def _remove_selected_step(self):
        row = self.list_sequence.currentRow()
        if row >= 0:
            del self.current_steps[row]
            self._sync_list_order()

    def _sync_list_order(self):
        reordered_steps = []
        for i in range(self.list_sequence.count()):
            item = self.list_sequence.item(i)
            old_id = item.data(Qt.ItemDataRole.UserRole)
            step = next((s for s in self.current_steps if s["step_id"] == old_id), None)
            if step: reordered_steps.append(step)

        self.current_steps = reordered_steps
        for i, step in enumerate(self.current_steps):
            if not isinstance(step["step_id"], int):
                step["step_id"] = i + 1
        self._redraw_sequence()

    def _redraw_sequence(self):
        # Save the current selection
        row = self.list_sequence.currentRow()

        self.list_sequence.blockSignals(True)
        self.list_sequence.clear()
        for step in self.current_steps:
            icon = STEP_ICONS.get(step["type"], "▪️")
            item = QListWidgetItem(f"[{step['step_id']}] {icon} {step['comment']}")
            item.setData(Qt.ItemDataRole.UserRole, step["step_id"])
            self.list_sequence.addItem(item)

        # Restore the selection
        if 0 <= row < self.list_sequence.count():
            self.list_sequence.setCurrentRow(row)

        self.list_sequence.blockSignals(False)

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

    def _on_step_selected(self):
        # 1. Nuke the old container entirely to guarantee zero ghosts
        if self.prop_container:
            self.prop_container.deleteLater()

        # 2. Create a completely fresh container and layout
        self.prop_container = QWidget()
        self.prop_form = QFormLayout(self.prop_container)
        self.scroll_area.setWidget(self.prop_container)

        row = self.list_sequence.currentRow()
        if 0 <= row < len(self.current_steps):
            self._build_properties_form(self.prop_form, self.current_steps[row], self._redraw_sequence)

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
            self.prop_form.addRow("Popup Text:", te_prompt)

            btn_add_cond = QPushButton("➕ Add Verify Condition")
            btn_add_cond.clicked.connect(lambda: self._update_val(step_data))
            self.prop_form.addRow(btn_add_cond)

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
                self.prop_form.addRow(f"Condition {i + 1}:", w)

        elif stype == "SUB_RECIPE":
            cb_recipe = QComboBox()
            if os.path.exists(self.config_dir):
                files = [f for f in os.listdir(self.config_dir) if f.endswith(".json")]
                cb_recipe.addItems([""] + files)
            cb_recipe.setCurrentText(step_data.get("target_file", ""))
            cb_recipe.currentTextChanged.connect(lambda t, s=step_data: self._update_val(s, "target_file", t))
            self.prop_form.addRow("Target File:", cb_recipe)


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

    def _save_recipe(self):
        name = self.le_recipe_name.text().strip()
        if not name:
            QMessageBox.warning(self, "Invalid Name", "Recipe name cannot be empty.")
            return

        new_version = self.le_recipe_version.text().strip()
        filename = name.lower().replace(" ", "_") + ".json"
        filepath = os.path.join(self.config_dir, filename)

        # --- NEW: VERSION COLLISION CHECK ---
        if os.path.exists(filepath):
            try:
                with open(filepath, 'r') as f:
                    existing_data = json.load(f)
                    existing_version = existing_data.get("version", "1.0")

                if existing_version == new_version:
                    # The operator forgot to increment the version number!
                    msg_box = QMessageBox(self)
                    msg_box.setIcon(QMessageBox.Icon.Warning)
                    msg_box.setWindowTitle("Traceability Warning")
                    msg_box.setText(f"A recipe named '{name}' already exists with Version {existing_version}.")
                    msg_box.setInformativeText(
                        "You are about to overwrite this file without incrementing the version number. "
                        "To maintain experimental traceability, it is highly recommended to Cancel and bump the version to 1.1, 2.0, etc.\n\n"
                        "Do you want to overwrite the existing version?"
                    )

                    btn_overwrite = msg_box.addButton("Overwrite", QMessageBox.ButtonRole.DestructiveRole)
                    btn_cancel = msg_box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)

                    msg_box.exec()

                    if msg_box.clickedButton() == btn_cancel:
                        return  # Abort the save process so they can change the number
            except Exception as e:
                print(f"[Warning] Could not read existing recipe for version check: {e}")

        # --- NORMAL SAVE EXECUTION ---
        final_data = {
            "recipe_name": name,
            "version": new_version,
            "fault_policy": self.fault_policy,
            "steps": self.current_steps
        }

        try:
            with open(filepath, 'w') as f:
                json.dump(final_data, f, indent=2)
            QMessageBox.information(self, "Success", f"Recipe '{name}' (v{new_version}) saved successfully.")
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "Save Error", str(e))


class AIWorker(QThread):
    sig_finished = pyqtSignal(dict)

    def __init__(self, assistant, prompt, context=None):
        super().__init__()
        self.assistant = assistant
        self.prompt = prompt
        self.context = context

    def run(self):
        # Runs in the background
        enhanced_prompt = f"Current Recipe State: {json.dumps(self.context)}\n\nUser Request: {self.prompt}"
        result = self.assistant.generate(enhanced_prompt)
        self.sig_finished.emit(result)