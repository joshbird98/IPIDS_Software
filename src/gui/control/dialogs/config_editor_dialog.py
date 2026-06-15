import sys
import time
import orjson
import os
from PyQt6.QtWidgets import (QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QMessageBox, QDialog, QTreeWidget, QTreeWidgetItem,
    QCheckBox, QSpinBox, QLineEdit
)
from PyQt6.QtGui import QColor
from PyQt6.QtCore import Qt

# --- Configuration Editor Form (Dynamic Dialog) ---

class ConfigEditorDialog(QDialog):
    def __init__(self, cmd_thread, parent=None):
        super().__init__(parent)
        self.setWindowTitle("System Configuration Editor")
        self.resize(700, 700)
        self.cmd_thread = cmd_thread
        self.config_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../config"))

        self.CONFIG_TO_SERVICE = {
            "fault_config.json": "ALL SERVICES",
            "network_config.json": "ALL SERVICES",
            "logger_config.json": "service_logger",
            "magnet_config.json": "service_magnet_psu",
            "mpd_config.json": "service_spellman_mpd",
            "vac_gauges_config.json": "service_vac_gauge_controllers",
            "theme_config.json": "HMI_GUI",
        }

        layout = QVBoxLayout(self)

        top_layout = QHBoxLayout()
        top_layout.addWidget(QLabel("<b>Target Config:</b>"))

        self.cb_files = QComboBox()
        self._populate_files()
        self.cb_files.currentTextChanged.connect(self._load_file)
        top_layout.addWidget(self.cb_files)

        top_layout.addWidget(QLabel("<b>Target Service:</b>"))
        self.cb_service = QComboBox()
        self.cb_service.addItems([
            "ALL SERVICES", "service_plc", "service_magnet_psu",
            "service_source_turbo", "service_spellman_mpd",
            "service_logger", "service_vac_gauge_controllers"
        ])
        self.cb_service.currentTextChanged.connect(self._update_button_text)
        top_layout.addWidget(self.cb_service)

        layout.addLayout(top_layout)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Configuration Key", "Value (Auto-Typed)"])
        self.tree.setColumnWidth(0, 300)
        self.tree.setAlternatingRowColors(True)
        layout.addWidget(self.tree)

        self.btn_save = QPushButton("💾 APPLY CHANGES && RESTART SERVICE")
        self.btn_save.setStyleSheet(
            "background-color: #2196F3; color: white; font-weight: bold; padding: 10px; font-size: 11pt;")
        self.btn_save.clicked.connect(self._save_and_restart)
        layout.addWidget(self.btn_save)

        if self.cb_files.count() > 0:
            self._load_file(self.cb_files.currentText())

    def _populate_files(self):
        if not os.path.exists(self.config_dir): return
        files = [f for f in os.listdir(self.config_dir) if f.endswith(".json") and f != "system_tags.json"]
        self.cb_files.addItems(files)

    def _update_button_text(self, svc_name=None):
        svc_name = svc_name or self.cb_service.currentText()
        if svc_name == "ALL SERVICES":
            self.btn_save.setText("💾 APPLY CHANGES && RESTART ALL SERVICES")
        else:
            self.btn_save.setText(f"💾 APPLY CHANGES && RESTART {svc_name.upper()}")

    def _load_file(self, filename):
        if not filename: return

        target_svc = self.CONFIG_TO_SERVICE.get(filename, "service_plc")
        if self.cb_service.findText(target_svc) == -1:
            self.cb_service.addItem(target_svc)
        self.cb_service.setCurrentText(target_svc)
        self._update_button_text(target_svc)

        self.tree.clear()
        path = os.path.join(self.config_dir, filename)
        try:
            with open(path, 'r') as f:
                data = orjson.loads(f.read())
            # Inject parent_key=None to start
            self._populate_tree(self.tree.invisibleRootItem(), data, parent_key=None)
            self.tree.expandAll()
        except Exception as e:
            QMessageBox.critical(self, "Parse Error", f"Failed to parse JSON:\n{e}")

    def _populate_tree(self, parent_node, data, parent_key=None):
        if isinstance(data, dict):
            for key, value in data.items():
                item = QTreeWidgetItem(parent_node)
                item.setText(0, str(key))

                # NEW: Allow editable keys for specific sub-dictionaries
                if parent_key == "devices":
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
                    item.setBackground(0, QColor("#E3F2FD"))
                    item.setToolTip(0, "Double click to rename this device key.")

                if isinstance(value, (dict, list)):
                    self._populate_tree(item, value, parent_key=key)
                else:
                    self._add_edit_widget(item, key, value)
        elif isinstance(data, list):
            for i, value in enumerate(data):
                item = QTreeWidgetItem(parent_node)
                item.setText(0, f"[{i}]")
                if isinstance(value, (dict, list)):
                    self._populate_tree(item, value, parent_key=None)
                else:
                    self._add_edit_widget(item, f"[{i}]", value)

    def _add_edit_widget(self, item, key, value):
        if isinstance(value, bool):
            w = QCheckBox()
            w.setChecked(value)
        elif isinstance(value, int):
            w = QSpinBox()
            w.setRange(-2147483648, 2147483647)
            w.setValue(value)
        elif isinstance(value, float):
            text_val = f"{value:g}"
            if 'e' not in text_val.lower() and abs(value) < 0.001 and value != 0.0:
                text_val = f"{value:.2e}"
            w = QLineEdit(text_val)
        else:
            w = QLineEdit(str(value))

        # NEW: Lock out the bus_id from edits
        if key == "bus_id":
            w.setReadOnly(True)
            w.setStyleSheet("background-color: #EEEEEE; color: #757575;")
            w.setToolTip("Bus ID cannot be changed as it maps to physical hardware and network configs.")

        item.setData(0, Qt.ItemDataRole.UserRole, type(value))
        self.tree.setItemWidget(item, 1, w)

    def _extract_tree(self, node):
        if node.childCount() == 0:
            w = self.tree.itemWidget(node, 1)
            orig_type = node.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(w, QCheckBox):
                return w.isChecked()
            elif isinstance(w, QSpinBox):
                return w.value()
            elif isinstance(w, QLineEdit):
                val = w.text()
                if orig_type == float:
                    try:
                        return float(val)
                    except ValueError:
                        raise ValueError(f"Invalid float format: {val}")
                elif orig_type == int:
                    try:
                        return int(val)
                    except ValueError:
                        raise ValueError(f"Invalid integer format: {val}")
                else:
                    return val

        first_child_key = node.child(0).text(0)
        if first_child_key.startswith("[") and first_child_key.endswith("]"):
            res = []
            for i in range(node.childCount()):
                res.append(self._extract_tree(node.child(i)))
            return res
        else:
            res = {}
            for i in range(node.childCount()):
                child = node.child(i)
                # child.text(0) extracts the (potentially edited) dictionary key!
                res[child.text(0)] = self._extract_tree(child)
            return res

    def _save_and_restart(self):
        filename = self.cb_files.currentText()
        if not filename: return

        try:
            new_data = self._extract_tree(self.tree.invisibleRootItem())
            path = os.path.join(self.config_dir, filename)

            with open(path, 'wb') as f:
                f.write(orjson.dumps(new_data, option=orjson.OPT_INDENT_2))

            registry_triggers = ["fault_config.json", "mpd_config.json", "vac_gauges_config.json"]
            if filename in registry_triggers:
                try:
                    import subprocess
                    build_script = os.path.abspath(os.path.join(self.config_dir, "../src/core/build_registry.py"))
                    if os.path.exists(build_script):
                        subprocess.run([sys.executable, build_script], check=True)
                        print("[GUI Output] Successfully rebuilt system_tags.json")
                except Exception as e:
                    QMessageBox.warning(self, "Build Error", f"Config saved, but build_registry.py failed:\n{e}")
                    return

            target_svc = self.cb_service.currentText()

            if target_svc == "HMI_GUI":
                QMessageBox.information(
                    self,
                    "GUI Config Saved",
                    f"Saved {filename}!\n\nYou must restart the Operator HMI application for theme or layout changes to take effect."
                )
            elif target_svc == "ALL SERVICES":
                master_list = [
                    "service_events", "service_logger", "service_data_compactor",
                    "service_plc", "service_vac_gauge_controllers",
                    "service_source_turbo", "service_magnet_psu", "service_spellman_mpd"
                ]
                for svc in master_list:
                    self.cmd_thread.send_command("manager", "manager_cmd", {"command": "restart", "service": svc})
                    time.sleep(0.02)

                msg = f"Saved {filename} && Rebuilt Tags!" if filename in registry_triggers else f"Saved {filename}!"
                QMessageBox.information(self, "Success", f"{msg}\nMass restart broadcasted to ALL SERVICES.")
            else:
                payload = {"command": "restart", "service": target_svc}
                self.cmd_thread.send_command("manager", "manager_cmd", payload)

                msg = f"Saved {filename} && Rebuilt Tags!" if filename in registry_triggers else f"Saved {filename}!"
                QMessageBox.information(self, "Success", f"{msg}\nRestart command sent to {target_svc}.")

            self.accept()

        except Exception as e:
            QMessageBox.critical(self, "Save Error", f"Failed to compile config. Check your inputs:\n\n{e}")
