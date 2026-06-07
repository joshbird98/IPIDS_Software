import os
import json
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QSplitter,
    QListWidget, QListWidgetItem, QLineEdit, QTextEdit,
    QPushButton, QLabel, QWidget, QFormLayout,
    QComboBox, QDoubleSpinBox, QMessageBox, QGroupBox,
    QScrollArea
)
from PyQt6.QtCore import Qt, pyqtSignal
from src.gui.control.tag_selector_dialog import TagSelectorDialog

from PyQt6.QtCore import QThread, pyqtSignal
from src.core.ai_assistant import RecipeAIAssistant
from PyQt6.QtWidgets import QMessageBox

# Set it in your PC's environment variables so it isn't hardcoded!
API_KEY = os.environ.get("GEMINI_API_KEY")

COLOR_HEADER = "background-color: #2c3e50; color: white; padding: 4px; font-weight: bold;"
COLOR_AI = "background-color: #f3e5f5; border: 1px solid #ce93d8; border-radius: 4px;"

STEP_ICONS = {
    "ACTION": "⚡", "WAIT_TIME": "⏱️", "WAIT_TELEMETRY": "📡",
    "USER_PROMPT": "🙋", "SUB_RECIPE": "🔗", "ROUTINE_CALL": "⚙️"
}

# --- NEW: Routine Definitions ---
ROUTINE_DEFAULTS = {
    "mass_scan": {
        "sp_target": "ion_beam.beamline.magnet.sp_requested_mass",
        "rb_target": "ion_beam.beamline_diagnostics.rb_fc_current",
        "start_val": 25.0,
        "end_val": 35.0,
        "step_size": 0.2,
        "dwell_sec": 1.0
    },
    "hv_conditioning": {
        "sp_target": "ion_beam.source.extraction.sp_requested_voltage",
        "max_val": 20.0,
        "step_size": 0.5,
        "dwell_sec": 60.0
    }
}


class FaultPolicyDialog(QDialog):
    """Sub-dialog to manage global safety aborts."""

    def __init__(self, policy_list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Global Fault Policy")
        self.resize(700, 400)
        self.policy = policy_list
        self.main_builder = parent  # To access tag selector

        layout = QVBoxLayout(self)

        btn_add = QPushButton("➕ Add Safety Abort Condition")
        btn_add.setStyleSheet("background-color: #F44336; color: white; font-weight: bold; padding: 6px;")
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

    def _add_fault(self):
        self.policy.append({
            "target": "", "condition": "==", "value": 0.0,
            "action": "ABORT", "message": "Hardware fault tripped!"
        })
        self._redraw()

    def _del_fault(self, idx):
        del self.policy[idx]
        self._redraw()

    def _update_dict(self, d, key, val):
        if isinstance(val, str):
            try:
                val = float(val) if "." in val or "e" in val.lower() else int(val)
            except ValueError:
                pass
        d[key] = val

    def _redraw(self):
        while self.form.count():
            item = self.form.takeAt(0)
            if item.widget(): item.widget().deleteLater()

        for i, fault in enumerate(self.policy):
            w = QWidget()
            l = QHBoxLayout(w)
            l.setContentsMargins(0, 0, 0, 0)

            le_t = QLineEdit(fault.get("target", ""))
            le_t.setPlaceholderText("Tag")
            le_t.mouseDoubleClickEvent = lambda e, le=le_t: self.main_builder._open_tag_selector(le)
            le_t.textChanged.connect(lambda t, f=fault: self._update_dict(f, "target", t))

            cb_c = QComboBox()
            cb_c.addItems(["==", ">", "<", ">=", "<="])
            cb_c.setCurrentText(fault.get("condition", "=="))
            cb_c.currentTextChanged.connect(lambda t, f=fault: self._update_dict(f, "condition", t))

            le_v = QLineEdit(str(fault.get("value", 0.0)))
            le_v.setFixedWidth(60)
            le_v.textChanged.connect(lambda t, f=fault: self._update_dict(f, "value", t))

            le_m = QLineEdit(fault.get("message", ""))
            le_m.setPlaceholderText("Error Message")
            le_m.textChanged.connect(lambda t, f=fault: self._update_dict(f, "message", t))

            btn_del = QPushButton("❌")
            btn_del.clicked.connect(lambda _, idx=i: self._del_fault(idx))

            l.addWidget(le_t)
            l.addWidget(cb_c)
            l.addWidget(le_v)
            l.addWidget(QLabel("Msg:"))
            l.addWidget(le_m)
            l.addWidget(btn_del)
            self.form.addRow(f"Trip {i + 1}:", w)


class SequenceListWidget(QListWidget):
    order_changed = pyqtSignal()

    def dropEvent(self, event):
        super().dropEvent(event)
        self.order_changed.emit()


class RecipeBuilderDialog(QDialog):
    def __init__(self, parent=None, config_dir=""):
        super().__init__(parent)
        self.setWindowTitle("Interactive Recipe Builder")
        self.resize(1100, 750)
        self.config_dir = config_dir
        self.current_steps = []
        self.fault_policy = []
        self.ai_assistant = RecipeAIAssistant(self.config_dir)
        self.ai_worker = None
        self._init_ui()

    def _init_ui(self):
        main_layout = QVBoxLayout(self)

        # 1. AI ASSISTANT BLOCK
        ai_group = QGroupBox("✨ AI Recipe Assistant")
        # Ensure the box doesn't get squished and has a nice background
        ai_group.setStyleSheet(
            "QGroupBox { background-color: #f8f9fa; border: 1px solid #ce93d8; border-radius: 6px; margin-top: 10px; font-weight: bold; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 3px 0 3px; }")
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
            "background-color: white; border: 1px solid #ddd; border-radius: 4px; padding: 5px;")

        # FIXED: Use nested tables for tight, left-aligned chat bubbles
        welcome_html = """
                <table width="100%" border="0" cellspacing="0" cellpadding="0"><tr><td align="left">
                    <table border="0" cellspacing="0" cellpadding="8" style="background-color: #E5E5EA; color: black;">
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
        # Distinct color to clearly show it's an entry box
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
        self.btn_faults.clicked.connect(self._open_fault_policy)  # NEW
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

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.prop_container = QWidget()
        self.prop_form = QFormLayout(self.prop_container)
        scroll.setWidget(self.prop_container)

        self.layout_prop.addWidget(scroll)
        splitter.addWidget(pane_prop)

        splitter.setSizes([200, 400, 500])
        main_layout.addWidget(splitter)

    # --- LOGIC ---

    def _open_fault_policy(self):
        dlg = FaultPolicyDialog(self.fault_policy, self)
        dlg.exec()

    def _on_ai_generate(self):
        prompt = self.txt_ai_prompt.toPlainText().strip()
        if not prompt:
            return

        if not self.ai_assistant.available:
            QMessageBox.warning(self, "AI Offline", "Please configure your GEMINI_API_KEY in the .env file.")
            return

        # FIXED: User Message (Right-aligned, Blue Bubble using nested tables)
        user_html = f"""
        <table width="100%" border="0" cellspacing="0" cellpadding="0"><tr><td align="right">
            <table border="0" cellspacing="0" cellpadding="8" style="background-color: #007AFF; color: white;">
                <tr><td>{prompt}</td></tr>
            </table>
        </td></tr></table><br>
        """
        self.txt_ai_history.append(user_html)
        self.txt_ai_prompt.clear()

        self.btn_generate_ai.setText("...")
        self.btn_generate_ai.setEnabled(False)

        self.ai_worker = AIWorker(self.ai_assistant, prompt)
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
                <table border="0" cellspacing="0" cellpadding="8" style="background-color: {bg_color}; color: {text_color};">
                    <tr><td>{text}</td></tr>
                </table>
            </td></tr></table><br>
            """

        if status == "error":
            self.txt_ai_history.append(make_ai_bubble(f"<b>Error:</b> {msg}", "#FFCDD2", "#B71C1C"))
        elif status == "chat":
            self.txt_ai_history.append(make_ai_bubble(msg, "#E5E5EA", "black"))
        elif status == "success":
            self.txt_ai_history.append(make_ai_bubble("Sequence generated and loaded!", "#C8E6C9", "#1B5E20"))

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
            # Auto load mass scan defaults initially
            new_step.update({"routine_name": "mass_scan", "parameters": dict(ROUTINE_DEFAULTS["mass_scan"])})

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
            step["step_id"] = i + 1
        self._redraw_sequence()

    def _redraw_sequence(self):
        self.list_sequence.blockSignals(True)
        self.list_sequence.clear()
        for step in self.current_steps:
            icon = STEP_ICONS.get(step["type"], "▪️")
            item = QListWidgetItem(f"[{step['step_id']}] {icon} {step['comment']}")
            item.setData(Qt.ItemDataRole.UserRole, step["step_id"])
            self.list_sequence.addItem(item)
        self.list_sequence.blockSignals(False)
        self._on_step_selected()

    def _open_tag_selector(self, line_edit_widget):
        dlg = TagSelectorDialog(self)
        if dlg.exec():
            tag = dlg.get_selected_tag()
            if tag: line_edit_widget.setText(tag)

    def _add_verify_cond(self, step_dict):
        conds = step_dict.setdefault("verify_conditions", [])
        conds.append({"target": "", "condition": "==", "value": 1.0})
        self._on_step_selected()  # Redraw properties

    def _del_verify_cond(self, step_dict, idx):
        del step_dict["verify_conditions"][idx]
        self._on_step_selected()

    def _on_step_selected(self):
        row = self.list_sequence.currentRow()
        if row < 0 or row >= len(self.current_steps):
            while self.prop_form.count():
                item = self.prop_form.takeAt(0)
                if item.widget(): item.widget().deleteLater()
            return

        step_data = self.current_steps[row]

        while self.prop_form.count():
            item = self.prop_form.takeAt(0)
            if item.widget(): item.widget().deleteLater()

        # 1. Common Properties
        le_comment = QLineEdit(step_data.get("comment", ""))
        le_comment.textChanged.connect(lambda t, s=step_data: self._update_step_data(s, "comment", t))
        self.prop_form.addRow("Comment/Label:", le_comment)

        stype = step_data.get("type")

        # 2. Type-Specific Properties
        if stype == "ACTION" or stype == "WAIT_TELEMETRY":
            le_target = QLineEdit(step_data.get("target", ""))
            le_target.setToolTip("Double-click to open ISA-95 Tree Selector.")
            le_target.mouseDoubleClickEvent = lambda e, le=le_target: self._open_tag_selector(le)
            le_target.textChanged.connect(lambda t, s=step_data: self._update_step_data(s, "target", t))
            self.prop_form.addRow("Target Tag:", le_target)

        if stype == "ACTION":
            le_value = QLineEdit(str(step_data.get("value", "")))
            le_value.textChanged.connect(lambda t, s=step_data: self._update_step_data(s, "value", t))
            self.prop_form.addRow("Command Value:", le_value)

        elif stype == "WAIT_TIME":
            sb_dur = QDoubleSpinBox()
            sb_dur.setRange(0.0, 36000.0)
            sb_dur.setValue(float(step_data.get("duration_sec", 0.0)))
            sb_dur.valueChanged.connect(lambda v, s=step_data: self._update_step_data(s, "duration_sec", v))
            self.prop_form.addRow("Duration (sec):", sb_dur)

        elif stype == "WAIT_TELEMETRY":
            cb_cond = QComboBox()
            cb_cond.addItems(["==", ">", "<", ">=", "<="])
            cb_cond.setCurrentText(step_data.get("condition", "=="))
            cb_cond.currentTextChanged.connect(lambda t, s=step_data: self._update_step_data(s, "condition", t))
            self.prop_form.addRow("Condition:", cb_cond)

            le_val = QLineEdit(str(step_data.get("value", "")))
            le_val.textChanged.connect(lambda t, s=step_data: self._update_step_data(s, "value", t))
            self.prop_form.addRow("Target Value:", le_val)

            sb_to = QDoubleSpinBox()
            sb_to.setRange(0.0, 36000.0)
            sb_to.setValue(float(step_data.get("timeout_sec", 0.0)))
            sb_to.valueChanged.connect(lambda v, s=step_data: self._update_step_data(s, "timeout_sec", v))
            self.prop_form.addRow("Timeout (sec):", sb_to)

        elif stype == "USER_PROMPT":
            te_prompt = QTextEdit(step_data.get("prompt_text", ""))
            te_prompt.setMaximumHeight(60)
            te_prompt.textChanged.connect(
                lambda s=step_data, w=te_prompt: self._update_step_data(s, "prompt_text", w.toPlainText()))
            self.prop_form.addRow("Popup Text:", te_prompt)

            btn_add_cond = QPushButton("➕ Add Verify Condition")
            btn_add_cond.clicked.connect(lambda: self._add_verify_cond(step_data))
            self.prop_form.addRow(btn_add_cond)

            for i, cond in enumerate(step_data.get("verify_conditions", [])):
                w = QWidget()
                l = QHBoxLayout(w)
                l.setContentsMargins(0, 0, 0, 0)

                le_t = QLineEdit(cond.get("target", ""))
                le_t.mouseDoubleClickEvent = lambda e, le=le_t: self._open_tag_selector(le)
                le_t.textChanged.connect(lambda t, c=cond: self._update_step_data(c, "target", t))

                cb_c = QComboBox()
                cb_c.addItems(["==", ">", "<", ">=", "<="])
                cb_c.setCurrentText(cond.get("condition", "=="))
                cb_c.currentTextChanged.connect(lambda t, c=cond: self._update_step_data(c, "condition", t))

                le_v = QLineEdit(str(cond.get("value", 1.0)))
                le_v.textChanged.connect(lambda t, c=cond: self._update_step_data(c, "value", t))

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
            cb_recipe.currentTextChanged.connect(lambda t, s=step_data: self._update_step_data(s, "target_file", t))
            self.prop_form.addRow("Target File:", cb_recipe)

        elif stype == "ROUTINE_CALL":
            # 1. Dropdown for Routine Selection
            cb_routines = QComboBox()
            cb_routines.addItems(list(ROUTINE_DEFAULTS.keys()))
            cb_routines.setCurrentText(step_data.get("routine_name", "mass_scan"))

            def _on_routine_changed(new_name):
                step_data["routine_name"] = new_name
                # Wipe old params, clone new defaults
                step_data["parameters"] = dict(ROUTINE_DEFAULTS.get(new_name, {}))
                self._on_step_selected()  # Redraw fields

            cb_routines.currentTextChanged.connect(_on_routine_changed)
            self.prop_form.addRow("Routine ID:", cb_routines)

            # 2. Dynamic form fields for every parameter
            params = step_data.setdefault("parameters", {})
            for k, v in params.items():
                le_param = QLineEdit(str(v))
                if "target" in k:  # Add double click hook for targets
                    le_param.mouseDoubleClickEvent = lambda e, le=le_param: self._open_tag_selector(le)
                le_param.textChanged.connect(lambda t, s=params, key=k: self._update_step_data(s, key, t))
                self.prop_form.addRow(f"  └ {k}:", le_param)

    def _update_step_data(self, target_dict: dict, key: str, value):
        if isinstance(value, str):
            if value.lower() == "true":
                value = True
            elif value.lower() == "false":
                value = False
            else:
                try:
                    value = float(value) if "." in value or "e" in value.lower() else int(value)
                except ValueError:
                    pass
        target_dict[key] = value

        if key == "comment":
            row = self.list_sequence.currentRow()
            if row >= 0:
                icon = STEP_ICONS.get(self.current_steps[row]["type"], "")
                self.list_sequence.item(row).setText(f"[{target_dict.get('step_id')}] {icon} {value}")

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

    def __init__(self, assistant, prompt):
        super().__init__()
        self.assistant = assistant
        self.prompt = prompt

    def run(self):
        # Runs in the background
        result = self.assistant.generate(self.prompt)
        self.sig_finished.emit(result)