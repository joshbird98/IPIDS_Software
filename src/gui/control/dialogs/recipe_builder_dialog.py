"""Recipe builder: a recipe's steps (drag to reorder), the selected step's properties, its fault
policy, and an AI assistant that drafts steps. Parts: recipe_step_form.py (properties form),
fault_policy_dialog.py, recipe_builder_defaults.py (icons, routine defaults)."""
import html
import json
import logging
import os
import re

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (QDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                             QListWidgetItem, QMessageBox, QPushButton, QScrollArea, QSplitter, QTextEdit,
                             QVBoxLayout, QWidget)

from src.core.ai_assistant import RecipeAIAssistant
from src.core.recipe_engine import RecipeError, create_step
from src.core.theme import is_dark_mode
from src.gui.control.dialogs.fault_policy_dialog import FaultPolicyDialog
from src.gui.control.dialogs.recipe_builder_defaults import COLOR_HEADER, ROUTINE_DEFAULTS, STEP_ICONS
from src.gui.control.dialogs.recipe_step_form import StepFormMixin

log = logging.getLogger("hmi.recipe_builder")


def recipe_filename(name: str) -> str:
    """The file a recipe called `name` is saved as: letters, digits, '-' and '_' only, so a name
    like 'Ramp 1/2' can't point outside the recipes folder."""
    stem = re.sub(r"[^a-z0-9_-]+", "_", name.strip().lower()).strip("_")
    return (stem or "recipe") + ".json"


def recipe_problems(steps: list) -> list:
    """Why the sequencer would refuse these steps (the same checks it makes when it loads a recipe)."""
    problems = []
    for step in steps:
        try:
            create_step(step)
        except RecipeError as e:
            problems.append(str(e))
        except (TypeError, AttributeError, ValueError) as e:
            problems.append(f"step {step.get('step_id', '?') if isinstance(step, dict) else '?'}: {e}")
    return problems

class SequenceListWidget(QListWidget):
    order_changed = pyqtSignal()

    def dropEvent(self, event):
        super().dropEvent(event)
        self.order_changed.emit()


class RecipeBuilderDialog(StepFormMixin, QDialog):
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
            if not isinstance(data, dict) or not isinstance(data.get("steps", []), list):
                raise ValueError("not a recipe (expected an object with a list of steps)")
        except (OSError, ValueError) as e:
            QMessageBox.critical(self, "Load Error", f"Could not load recipe for editing: {e}")
            return
        self.le_recipe_name.setText(data.get("recipe_name", "New Recipe"))
        self.le_recipe_version.setText(str(data.get("version", "1.0")))
        self.fault_policy = data.get("fault_policy", [])
        self.current_steps = [s for s in data.get("steps", []) if isinstance(s, dict)]
        self._renumber()            # older files can repeat a step number (see _renumber)
        self._redraw_sequence()

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
                <tr><td>{html.escape(prompt)}</td></tr>
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

            recipe_data = response.get("recipe")
            if not isinstance(recipe_data, dict) or not isinstance(recipe_data.get("steps", []), list):
                recipe_data = {}
            self.le_recipe_name.setText(str(recipe_data.get("recipe_name", "AI Sequence")))
            self.le_recipe_version.setText(str(recipe_data.get("version", "1.0")))
            policy = recipe_data.get("fault_policy", [])
            self.fault_policy = policy if isinstance(policy, list) else []
            self.current_steps = [st for st in recipe_data.get("steps", []) if isinstance(st, dict)]
            self._renumber()
            self._redraw_sequence()
            problems = recipe_problems(self.current_steps)
            if problems:
                self.txt_ai_history.append(make_ai_bubble(
                    "<b>Check these steps before saving:</b><br>" + "<br>".join(html.escape(p) for p in problems),
                    "#FFF9C4", "black"))
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
            "step_id": len(self.current_steps) + 1,     # steps are always numbered 1..n (_renumber)
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
        if 0 <= row < len(self.current_steps):
            del self.current_steps[row]
            self._renumber()
            self._redraw_sequence()
            self._on_step_selected()

    def _renumber(self):
        """Steps are numbered 1..n in order. The ids used to keep their old numbers after a delete or
        a drag, so a step added later could get an id already in use - and the next drag, which
        matched list rows to steps by id, then duplicated one step and dropped the other."""
        for i, step in enumerate(self.current_steps):
            step["step_id"] = i + 1

    def _sync_list_order(self):
        """After a drag: each row carries the index its step had, so steps are matched exactly."""
        old = self.current_steps
        order = [self.list_sequence.item(i).data(Qt.ItemDataRole.UserRole)
                 for i in range(self.list_sequence.count())]
        if sorted(order) != list(range(len(old))):      # not a pure reorder: leave the steps alone
            self._redraw_sequence()
            return
        self.current_steps = [old[i] for i in order]
        self._renumber()
        self._redraw_sequence()

    def _redraw_sequence(self):
        # Save the current selection
        row = self.list_sequence.currentRow()

        self.list_sequence.blockSignals(True)
        self.list_sequence.clear()
        for index, step in enumerate(self.current_steps):
            icon = STEP_ICONS.get(step.get("type"), "▪️")
            item = QListWidgetItem(f"[{step.get('step_id', index + 1)}] {icon} {step.get('comment', '')}")
            item.setData(Qt.ItemDataRole.UserRole, index)
            self.list_sequence.addItem(item)

        # Restore the selection
        if 0 <= row < self.list_sequence.count():
            self.list_sequence.setCurrentRow(row)

        self.list_sequence.blockSignals(False)

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

    def _save_recipe(self):
        name = self.le_recipe_name.text().strip()
        if not name:
            QMessageBox.warning(self, "Invalid Name", "Recipe name cannot be empty.")
            return

        new_version = self.le_recipe_version.text().strip()
        problems = recipe_problems(self.current_steps)
        if problems:
            QMessageBox.warning(self, "Recipe Not Saved",
                                "The sequencer would refuse this recipe:\n\n" + "\n".join(problems))
            return
        filepath = os.path.join(self.config_dir, recipe_filename(name))

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

                    msg_box.addButton("Overwrite", QMessageBox.ButtonRole.DestructiveRole)
                    btn_cancel = msg_box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)

                    msg_box.exec()

                    if msg_box.clickedButton() == btn_cancel:
                        return  # Abort the save process so they can change the number
            except (OSError, ValueError, AttributeError) as e:
                log.warning("Could not read existing recipe %s for the version check: %s", filepath, e)

        # --- NORMAL SAVE EXECUTION ---
        final_data = {
            "recipe_name": name,
            "version": new_version,
            "fault_policy": self.fault_policy,
            "steps": self.current_steps
        }

        tmp = filepath + ".tmp"
        try:
            os.makedirs(self.config_dir or ".", exist_ok=True)
            with open(tmp, 'w') as f:          # written whole, then swapped in: never half a recipe
                json.dump(final_data, f, indent=2)
            os.replace(tmp, filepath)
        except (OSError, TypeError, ValueError) as e:
            QMessageBox.critical(self, "Save Error", str(e))
            return
        self.saved_path = filepath
        QMessageBox.information(self, "Success", f"Recipe '{name}' (v{new_version}) saved successfully.")
        self.accept()


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
        try:
            result = self.assistant.generate(enhanced_prompt)
        except Exception as e:      # any library/network failure must still re-enable the Generate button
            log.exception("AI assistant failed")
            result = {"status": "error", "message": str(e)}
        if not isinstance(result, dict):
            result = {"status": "error", "message": "The assistant gave no usable answer."}
        self.sig_finished.emit(result)
