import json
import os
from datetime import datetime
from PyQt5.QtWidgets import (QFileDialog, QMessageBox, QDialog, QVBoxLayout,
                             QTableWidget, QTableWidgetItem, QDialogButtonBox, QHeaderView)
from PyQt5.QtGui import QColor
from PyQt5.QtCore import Qt


class RecipePreviewDialog(QDialog):
    """A popup window to review changes before applying them."""

    def __init__(self, parent, current_data, new_data, tag_map):
        super().__init__(parent)
        self.setWindowTitle("Review Recipe - Confirm Changes")
        self.resize(600, 500)

        layout = QVBoxLayout(self)

        # 1. Create Table
        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Parameter Name", "Current Value", "New Value"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)

        # 2. Populate Table
        # We assume tag_map is { "Friendly Name": "plc.tag" }
        rows = []
        for friendly_name, tag in tag_map.items():
            if tag in new_data:
                rows.append((friendly_name, tag))

        self.table.setRowCount(len(rows))

        for i, (friendly_name, tag) in enumerate(rows):
            # Column 0: Name
            self.table.setItem(i, 0, QTableWidgetItem(friendly_name))

            # Column 1: Current
            curr_val = current_data.get(tag, "N/A")
            self.table.setItem(i, 1, QTableWidgetItem(str(curr_val)))

            # Column 2: New
            new_val = new_data[tag]
            item_new = QTableWidgetItem(str(new_val))

            # Highlight Logic: If changed, make it Yellow
            # Convert to string for comparison to avoid float precision issues
            if str(curr_val) != str(new_val):
                item_new.setBackground(QColor("#FFF59D"))  # Light Yellow
                item_new.setForeground(Qt.black)

            self.table.setItem(i, 2, item_new)

        layout.addWidget(self.table)

        # 3. Buttons (OK / Cancel)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class RecipeController:
    def __init__(self, main_window, plc, all_binders):
        """
        all_binders: A list of ALL binder objects from the MainGUI.
        We will index them by their tag_name for quick lookup.
        """
        self.ui = main_window
        self.plc = plc

        # 1. Create a Registry of Controllable Binders
        # Structure: { "plc.tag.name": binder_instance }
        self.binder_registry = {}

        for binder in all_binders:
            # We only care about NumberEntry binders (the ones that control things)
            # We assume your NumberEntryBinder has a 'tag_name' attribute
            if hasattr(binder, 'tag_name') and hasattr(binder, 'set_value_from_recipe'):
                self.binder_registry[binder.tag_name] = binder

        self.default_dir = os.path.join(os.getcwd(), "Recipes")
        if not os.path.exists(self.default_dir):
            os.makedirs(self.default_dir)

        self.ui.btn_save_recipe.clicked.connect(self.save_recipe)
        self.ui.btn_load_recipe.clicked.connect(self.load_recipe)

    def save_recipe(self):
        """
        Scans all controllable binders, reads their current values from the PLC,
        and saves them to a JSON file.
        """
        # 1. Gather Data
        # We read from the PLC (not the text box) to ensure we save
        # what is ACTUALLY running on the machine right now.
        setpoints_data = {}

        for tag, binder in self.binder_registry.items():
            # Read the value from the PLC tag associated with this binder
            val = self.plc.read_tag_value(tag)

            # Sanity Check: If communication is lost or tag is None, default to 0
            if val is None:
                val = 0

            # Store it
            setpoints_data[tag] = val

        # 2. Prepare File Structure (JSON)
        # We add metadata so we know when this file was created
        file_content = {
            "metadata": {
                "created_iso": datetime.now().isoformat(),
                "created_readable": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "description": "User Snapshot",
                "parameter_count": len(setpoints_data)
            },
            "setpoints": setpoints_data
        }

        # 3. Open Save Dialog
        # Opens in the 'Recipes' folder we created in __init__
        filepath, _ = QFileDialog.getSaveFileName(
            self.ui,
            "Save Recipe",
            self.default_dir,
            "JSON Files (*.json)"
        )

        # If user pressed Cancel, filepath is empty string
        if not filepath:
            return

        # 4. Write to Disk
        try:
            with open(filepath, 'w') as f:
                # indent=4 makes the file human-readable (pretty printing)
                json.dump(file_content, f, indent=4, sort_keys=True)

            # 5. Success Message
            filename = os.path.basename(filepath)
            QMessageBox.information(
                self.ui,
                "Recipe Saved",
                f"Successfully saved {len(setpoints_data)} parameters to:\n{filename}"
            )

        except Exception as e:
            # Error Handling
            QMessageBox.critical(
                self.ui,
                "Save Failed",
                f"Could not save recipe file.\n\nError: {str(e)}"
            )

        # ... (File saving logic remains the same as previous answer) ...
        # (For brevity, I'm skipping the JSON write part here - use previous code)

    def load_recipe(self):
        # 1. Open File & Parse JSON (Same as before)
        filepath, _ = QFileDialog.getOpenFileName(self.ui, "Load Recipe", self.default_dir, "JSON (*.json)")
        if not filepath: return

        try:
            with open(filepath, 'r') as f:
                content = json.load(f)
            new_setpoints = content.get("setpoints", {})
        except Exception as e:
            QMessageBox.critical(self.ui, "Error", f"Bad File: {e}")
            return

        # 2. VALIDATION: Check for Disabled Widgets
        # This is the "Polite Refusal" logic
        disabled_controls = []
        unknown_tags = []

        for tag, value in new_setpoints.items():
            if tag in self.binder_registry:
                binder = self.binder_registry[tag]
                # Check if the UI widget is actually enabled
                if not binder.widget.isEnabled():
                    # Get a friendly name if possible, or use tag
                    name = binder.widget.objectName() or tag
                    disabled_controls.append(name)
            else:
                # Tag in file but no widget found (maybe from old version?)
                unknown_tags.append(tag)

        # 3. Construct Error Message if needed
        if disabled_controls:
            msg = "<b>Cannot load recipe.</b> The following controls are currently disabled:<br><ul>"
            for name in disabled_controls:
                msg += f"<li>{name}</li>"
            msg += "</ul><br>Please enable these systems (or clear faults) and try again."

            QMessageBox.warning(self.ui, "Recipe Load Aborted", msg)
            return  # <--- STOP HERE

        # 4. Preview Dialog (Compare Current vs New)
        current_values = {tag: self.plc.read_tag_value(tag) for tag in new_setpoints}

        # We need a 'friendly map' for the table labels.
        # We can try to use the widget tooltip or object name
        friendly_map = {}
        for tag in new_setpoints:
            if tag in self.binder_registry:
                widget = self.binder_registry[tag].widget
                friendly_map[tag] = widget.toolTip() or widget.objectName() or tag

        # Show Preview
        dialog = RecipePreviewDialog(self.ui, current_values, new_setpoints, friendly_map)

        if dialog.exec_() == QDialog.Accepted:
            self._apply_via_binders(new_setpoints)

    def _apply_via_binders(self, data):
        """Apply values by telling the widgets to write themselves."""
        count = 0
        for tag, value in data.items():
            if tag in self.binder_registry:
                binder = self.binder_registry[tag]
                success = binder.set_value_from_recipe(value)
                if success:
                    count += 1

        QMessageBox.information(self.ui, "Done", f"Recipe applied! {count} parameters updated.")