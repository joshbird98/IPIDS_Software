import os
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QComboBox, QLabel, QListWidget, QListWidgetItem,
    QMessageBox
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor

from src.gui.control.recipe_builder_dialog import RecipeBuilderDialog

# --- SCADA Standard Colors ---
COLOR_OK = "background-color: #4CAF50; color: white; font-weight: bold; border-radius: 4px; padding: 6px;"
COLOR_FAULT = "background-color: #F44336; color: white; font-weight: bold; border-radius: 4px; padding: 6px;"
COLOR_WARNING = "background-color: #FF9800; color: white; font-weight: bold; border-radius: 4px; padding: 6px;"
COLOR_INACTIVE = "background-color: #757575; color: white; font-weight: bold; border-radius: 4px; padding: 6px;"
COLOR_BUTTON_STANDARD = "background-color: #E0E0E0; color: black; font-weight: normal; border-radius: 4px; padding: 6px;"


class RecipeExecutionWidget(QWidget):
    def __init__(self, recipe_worker, telemetry_cache_ref=None):
        super().__init__()
        self.worker = recipe_worker
        self.telemetry_cache = telemetry_cache_ref # Save the reference
        self.config_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../config/recipes"))
        os.makedirs(self.config_dir, exist_ok=True)
        self._init_ui()
        self._wire_signals()

    def _init_ui(self):
        layout = QVBoxLayout(self)

        # 0. The Builder Entry Point
        self.btn_builder = QPushButton("🛠️ OPEN RECIPE BUILDER")
        self.btn_builder.setStyleSheet(
            "background-color: #9C27B0; color: white; font-weight: bold; border-radius: 4px; padding: 6px;")
        self.btn_builder.setToolTip("Open the interactive visual designer to create or edit recipes.")
        self.btn_builder.clicked.connect(self._open_builder)
        layout.addWidget(self.btn_builder)

        # 1. File Selection
        file_layout = QHBoxLayout()
        self.cb_recipes = QComboBox()
        self._populate_recipes()

        self.btn_refresh = QPushButton("↻")
        self.btn_refresh.setFixedWidth(40)
        self.btn_refresh.setToolTip("Rescan folder for new JSON files.")
        self.btn_refresh.clicked.connect(self._populate_recipes)

        self.btn_load = QPushButton("LOAD RECIPE")
        self.btn_load.setStyleSheet(COLOR_BUTTON_STANDARD)
        self.btn_load.clicked.connect(self._load_recipe)

        file_layout.addWidget(self.cb_recipes, stretch=1)
        file_layout.addWidget(self.btn_refresh)
        file_layout.addWidget(self.btn_load)
        layout.addLayout(file_layout)

        # 2. Master Controls
        ctrl_layout = QHBoxLayout()

        self.btn_start = QPushButton("▶ START")
        self.btn_start.clicked.connect(self._start_recipe)
        self.btn_pause = QPushButton("⏸ PAUSE")
        self.btn_pause.clicked.connect(self._pause_recipe)
        self.btn_abort = QPushButton("⏹ ABORT")
        self.btn_abort.clicked.connect(self._abort_recipe)

        ctrl_layout.addWidget(self.btn_start)
        ctrl_layout.addWidget(self.btn_pause)
        ctrl_layout.addWidget(self.btn_abort)
        layout.addLayout(ctrl_layout)

        # 3. Live Sequence View
        layout.addWidget(QLabel("<b>Execution Sequence:</b>"))
        self.list_steps = QListWidget()
        self.list_steps.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.list_steps.setStyleSheet("QListWidget { font-size: 11pt; }")
        layout.addWidget(self.list_steps)

        # 4. Status Bar
        self.lbl_status = QLabel("STATUS: IDLE")
        self.lbl_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_status.setStyleSheet(COLOR_INACTIVE)
        layout.addWidget(self.lbl_status)

        self._update_button_states("NO_RECIPE")

        dev_layout = QHBoxLayout()
        self.btn_sim_trip = QPushButton("⚡ DEV: SIMULATE RELAY TRIP")
        self.btn_sim_trip.setStyleSheet(
            "background-color: black; color: yellow; font-weight: bold; border-radius: 4px; padding: 6px;")
        self.btn_sim_trip.setToolTip("Directly injects a fault into the telemetry cache for testing.")
        self.btn_sim_trip.clicked.connect(self._dev_sim_relay_trip)
        dev_layout.addWidget(self.btn_sim_trip)
        layout.addLayout(dev_layout)

    def _dev_sim_relay_trip(self):
        if self.telemetry_cache is not None:
            self.telemetry_cache["ion_beam.facilities.safety_relay_active"] = 0.0
            print("[DEV] Injected Safety Relay = 0.0 into master cache!")

    def _wire_signals(self):
        self.worker.sig_status_update.connect(self._on_status_update)
        self.worker.sig_step_started.connect(self._on_step_started)
        self.worker.sig_step_completed.connect(self._on_step_completed)
        self.worker.sig_recipe_finished.connect(self._on_recipe_finished)
        self.worker.sig_user_prompt.connect(self._on_user_prompt)
        self.worker.sig_sequence_changed.connect(self._on_sequence_changed)

    def _on_user_prompt(self, step_id: int, message: str):
        self._on_status_update("WAITING FOR OPERATOR")
        self.lbl_status.setStyleSheet(COLOR_WARNING)

        while True:
            # 1. Pop the dialog
            QMessageBox.information(
                self,
                "Recipe Action Required",
                f"Step {step_id} requires intervention:\n\n{message}"
            )

            # 2. They clicked OK. Ask the worker if the conditions are actually met.
            if self.worker.acknowledge_prompt():
                self._on_status_update("RUNNING")
                break  # Success, break the loop
            else:
                # 3. They lied! Condition failed. Trap them or let them abort.
                reply = QMessageBox.warning(
                    self,
                    "Verification Failed",
                    "The hardware conditions required to proceed have not been met.\n\nPlease verify your physical actions, then press Retry. Or press Abort to cancel the sequence.",
                    QMessageBox.StandardButton.Retry | QMessageBox.StandardButton.Abort
                )

                if reply == QMessageBox.StandardButton.Abort:
                    self.worker.abort()
                    break  # Break loop, engine will halt

    def _on_sequence_changed(self, breadcrumb_name: str):
        """Dynamically redraws the step list when shifting in/out of sub-recipes."""
        self.list_steps.clear()

        for step in self.worker.active_recipe.steps:
            item = QListWidgetItem(step.get_display_name())
            item.setToolTip(step.get_description())
            item.setData(Qt.ItemDataRole.UserRole, step.step_id)

            # If we popped back up to a parent, previous steps will already be DONE
            if step.state == "DONE":
                item.setBackground(QColor("#C8E6C9"))
                item.setText(item.text() + "  ✓")

            self.list_steps.addItem(item)

        # Update the UI title so the user knows they are deep in a stack
        self.lbl_status.setText(f"READY/RUN: {breadcrumb_name}")

    def _update_button_states(self, state: str):
        """Dynamically applies styles and descriptive tooltips based on the engine state."""
        if state == "NO_RECIPE":
            self.btn_start.setEnabled(False)
            self.btn_pause.setEnabled(False)
            self.btn_abort.setEnabled(False)
            self.btn_start.setStyleSheet(COLOR_INACTIVE)
            self.btn_pause.setStyleSheet(COLOR_INACTIVE)
            self.btn_abort.setStyleSheet(COLOR_INACTIVE)
            self.btn_start.setToolTip("Select and load a recipe first.")

        elif state == "READY":
            self.btn_start.setEnabled(True)
            self.btn_start.setText("▶ START")
            self.btn_pause.setEnabled(False)
            self.btn_abort.setEnabled(False)
            self.btn_start.setStyleSheet(COLOR_OK)
            self.btn_pause.setStyleSheet(COLOR_INACTIVE)
            self.btn_abort.setStyleSheet(COLOR_INACTIVE)
            self.btn_start.setToolTip("Begin execution of the loaded recipe from Step 1.")

        elif state == "RUNNING":
            self.btn_start.setEnabled(False)
            self.btn_pause.setEnabled(True)
            self.btn_abort.setEnabled(True)
            self.btn_start.setStyleSheet(COLOR_INACTIVE)
            self.btn_pause.setStyleSheet(COLOR_WARNING)
            self.btn_abort.setStyleSheet(COLOR_FAULT)
            self.btn_pause.setText("⏸ PAUSE")
            self.btn_pause.setToolTip("Suspend execution and freeze all timers.")
            self.btn_abort.setToolTip("Halt the recipe immediately and revert hardware to manual mode.")

        elif state == "PAUSED":
            self.btn_start.setEnabled(True)
            self.btn_start.setText("↻ RESTART")
            self.btn_start.setStyleSheet(COLOR_WARNING)  # Make it orange so they know it's a reset
            self.btn_start.setToolTip("Wipe current progress and restart the recipe from Step 1.")
            self.btn_pause.setEnabled(True)
            self.btn_abort.setEnabled(True)
            self.btn_pause.setStyleSheet(COLOR_OK)
            self.btn_pause.setText("▶ RESUME")
            self.btn_pause.setToolTip("Resume execution and unfreeze timers.")

        elif state == "FINISHED":
            self.btn_start.setEnabled(True)
            self.btn_start.setText("↻ RESTART")
            self.btn_pause.setEnabled(False)
            self.btn_abort.setEnabled(False)
            self.btn_start.setStyleSheet(COLOR_OK)
            self.btn_pause.setStyleSheet(COLOR_INACTIVE)  # FIX: Forces Grey
            self.btn_abort.setStyleSheet(COLOR_INACTIVE)  # FIX: Forces Grey
            self.btn_start.setToolTip("Restart the current recipe from Step 1.")

        if not self.btn_start.isEnabled():
            self.btn_start.setToolTip("Recipe must be loaded and the engine must be idle to start.")
        if not self.btn_pause.isEnabled():
            self.btn_pause.setToolTip("Pause is only available while a recipe is running.")
        if not self.btn_abort.isEnabled():
            self.btn_abort.setToolTip("Abort is only available while a recipe is running.")

    def _abort_recipe(self):
        # FIX: Immediately provide UI feedback that the button was clicked
        self.btn_abort.setEnabled(False)
        self.btn_pause.setEnabled(False)
        self.btn_abort.setStyleSheet(COLOR_INACTIVE)
        self.btn_pause.setStyleSheet(COLOR_INACTIVE)
        self.worker.abort()

    def _populate_recipes(self):
        self.cb_recipes.clear()
        if not os.path.exists(self.config_dir): return
        files = [f for f in os.listdir(self.config_dir) if f.endswith(".json")]
        self.cb_recipes.addItems(files)

    def _load_recipe(self):
        filename = self.cb_recipes.currentText()
        if not filename: return
        filepath = os.path.join(self.config_dir, filename)
        try:
            self.worker.load_recipe(filepath)
            self._clear_visuals()

            # Simply trigger the sequence change to draw the root recipe
            self._on_sequence_changed(self.worker.active_recipe.name)
            self._update_button_states("READY")

        except Exception as e:
            QMessageBox.critical(self, "Load Error", f"Failed to parse recipe:\n{e}")

    def _start_recipe(self):
        # Because Start/Restart is only enabled when IDLE or FINISHED,
        # clicking it ALWAYS implies a fresh run from the beginning.
        if self.worker.root_recipe:
            self.worker.call_stack.clear()  # Dump any active sub-recipes
            self.worker.active_recipe = self.worker.root_recipe  # Re-target the master
            self.worker.current_step_idx = 0  # Go to step 0

            for step in self.worker.active_recipe.steps:
                step.reset()

            self._clear_visuals()
            self._on_sequence_changed(self.worker.active_recipe.name)  # Redraw the root list

        self._update_button_states("RUNNING")
        self.worker.play()

    def _pause_recipe(self):
        if self.worker.paused:
            self._update_button_states("RUNNING")
            self.worker.play()
        else:
            self._update_button_states("PAUSED")
            self.worker.pause()

    def _find_list_item_by_id(self, step_id):
        for i in range(self.list_steps.count()):
            item = self.list_steps.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == step_id:
                return item
        return None

    def _on_status_update(self, status: str):
        self.lbl_status.setText(f"STATUS: {status}")
        if status == "RUNNING":
            self.lbl_status.setStyleSheet(COLOR_OK)
        elif status == "PAUSED":
            self.lbl_status.setStyleSheet(COLOR_WARNING)
        elif "ABORT" in status:
            self.lbl_status.setStyleSheet(COLOR_FAULT)

    def _on_step_started(self, step_id: int, message: str):
        # Clear old highlights and set the active row to blue
        for i in range(self.list_steps.count()):
            item = self.list_steps.item(i)
            sid = item.data(Qt.ItemDataRole.UserRole)
            if sid == step_id:
                item.setBackground(QColor("#BBDEFB"))
                self.list_steps.scrollToItem(item)
            elif sid < step_id:
                # Ensure previous steps are green just in case
                item.setBackground(QColor("#C8E6C9"))

    def _on_step_completed(self, step_id: int):
        item = self._find_list_item_by_id(step_id)
        if item:
            item.setBackground(QColor("#C8E6C9"))
            if "✓" not in item.text():
                item.setText(item.text() + "  ✓")

    def _on_recipe_finished(self, success: bool, message: str):
        self._update_button_states("FINISHED")
        self.lbl_status.setText(f"FINISHED: {message}")
        if success:
            self.lbl_status.setStyleSheet(COLOR_OK)
        else:
            self.lbl_status.setStyleSheet(COLOR_FAULT)
            if self.worker.active_recipe and self.worker.current_step_idx < len(self.worker.active_recipe.steps):
                failed_step = self.worker.active_recipe.steps[self.worker.current_step_idx]
                item = self._find_list_item_by_id(failed_step.step_id)
                if item:
                    item.setBackground(QColor("#FFCDD2"))
                    if "✗" not in item.text():
                        item.setText(item.text() + "  ✗")

    def _clear_visuals(self):
        """Resets the list widget to a pristine, un-run state."""
        for i in range(self.list_steps.count()):
            item = self.list_steps.item(i)
            # Reset to default background
            item.setBackground(QColor("transparent"))
            item.setForeground(QColor("black"))

            # Strip the checkmarks and crosses
            clean_text = item.text().replace("  ✓", "").replace("  ✗", "")
            item.setText(clean_text)

    def _open_builder(self):
        # Instantiate the builder, passing the config directory so it knows where to save
        builder = RecipeBuilderDialog(self, self.config_dir)

        # exec() opens it as a modal dialog (blocks the rest of the UI until closed)
        result = builder.exec()

        # When the user saves and closes the builder, auto-refresh the dropdown list!
        self._populate_recipes()