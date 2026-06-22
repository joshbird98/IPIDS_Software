import time
import json
import os
import numpy as np
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QGridLayout, QLabel, QPushButton,
    QGroupBox, QDoubleSpinBox, QMessageBox, QDialog, QTreeWidget,
    QTreeWidgetItem, QDialogButtonBox
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal


class OptimizerWorker(QThread):
    data_point = pyqtSignal(float, float)
    scan_finished = pyqtSignal()
    status_msg = pyqtSignal(str)

    def __init__(self, cmd_thread, get_telemetry_cb, target_service: str,
                 target_tag: str, mode_tag: str, rb_tag: str,
                 start_val: float, stop_val: float, steps: int, dwell: float):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.get_telemetry_cb = get_telemetry_cb
        self.target_service = target_service
        self.target_tag = target_tag
        self.mode_tag = mode_tag
        self.rb_tag = rb_tag
        self.start_val = start_val
        self.stop_val = stop_val
        self.steps = steps
        self.dwell = dwell
        self.is_stopped = False

    def run(self):
        self.status_msg.emit("Seizing Control (Mode=1)...")
        self.cmd_thread.send_command(self.target_service, self.mode_tag, 1, origin="optimizer")
        time.sleep(0.5)

        self.status_msg.emit("Executing Sweep...")
        scan_points = np.linspace(self.start_val, self.stop_val, self.steps)

        for val in scan_points:
            if self.is_stopped:
                break

            self.cmd_thread.send_command(self.target_service, self.target_tag, float(val), origin="optimizer")

            start_t = time.time()
            while time.time() - start_t < self.dwell:
                if self.is_stopped:
                    break
                time.sleep(0.05)

            if self.is_stopped:
                break

            rb_val = self.get_telemetry_cb(self.rb_tag)
            self.data_point.emit(float(val), float(rb_val))

        self.status_msg.emit("Releasing Control (Mode=0)...")
        self.cmd_thread.send_command(self.target_service, self.mode_tag, 0, origin="optimizer")

        if not self.is_stopped:
            self.status_msg.emit("Optimization Complete.")

        self.scan_finished.emit()

    def stop(self):
        self.is_stopped = True


class OptimizerTagSelectorDialog(QDialog):
    def __init__(self, registry: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select Sweep Parameter")
        self.resize(500, 500)
        self.registry = registry

        self.selected_tag = None
        self.selected_meta = None

        layout = QVBoxLayout(self)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["ISA-95 Hierarchy / Parameter", "Unit"])
        self.tree.setColumnWidth(0, 350)
        layout.addWidget(self.tree)

        self.tree.itemDoubleClicked.connect(self._on_double_click)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept_selection)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._populate_tree()

    def _populate_tree(self):
        folder_nodes = {}
        for full_tag, meta in self.registry.items():
            if not meta.get("auto_controllable"):
                continue
            if meta.get("min_val") is None or meta.get("max_val") is None:
                continue

            parts = full_tag.split('.')
            parent_node = self.tree.invisibleRootItem()
            current_path = ""

            for part in parts[:-1]:
                current_path = f"{current_path}.{part}" if current_path else part
                if current_path not in folder_nodes:
                    node = QTreeWidgetItem(parent_node)
                    node.setText(0, part.replace('_', ' ').title())
                    folder_nodes[current_path] = node
                parent_node = folder_nodes[current_path]

            item = QTreeWidgetItem(parent_node)
            item.setText(0, meta.get("default_label", parts[-1]))
            item.setText(1, meta.get("unit", ""))
            item.setData(0, Qt.ItemDataRole.UserRole, {"tag": full_tag, "meta": meta})

        self.tree.collapseAll()

    def _on_double_click(self, item, column):
        self.accept_selection()

    def accept_selection(self):
        items = self.tree.selectedItems()
        if items:
            data = items[0].data(0, Qt.ItemDataRole.UserRole)
            if data:
                self.selected_tag = data["tag"]
                self.selected_meta = data["meta"]
                self.accept()
                return
        self.reject()


class ParameterOptimizerWidget(QWidget):
    def __init__(self, cmd_thread, get_telemetry_cb):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.get_telemetry_cb = get_telemetry_cb

        self.config_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../config"))
        self.registry = self._load_registry()

        # Hardcoded Objective Metric
        self.rb_metric_tag = "ion_beam.beamline.faraday.smu.rb_current"

        self.selected_target_tag = None
        self.selected_target_meta = None

        self.scan_worker = None
        self.main_layout = QVBoxLayout(self)
        self._init_ui()

    def _derive_enable_tag(self, target_tag: str) -> str:
        parts = target_tag.split('.')
        base = ".".join(parts[:-1])
        return f"{base}.stat_enabled"

    def _load_registry(self) -> dict:
        try:
            path = os.path.join(self.config_dir, "system_tags.json")
            with open(path, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"[Optimizer] Failed to load registry: {e}")
            return {}

    def _init_ui(self):
        group = QGroupBox("1D Parameter Optimizer")
        layout = QGridLayout()

        # --- Row 0: Target Parameter Selection ---
        self.btn_select_param = QPushButton("Select Sweep Parameter...")
        self.btn_select_param.clicked.connect(self._open_selector_dialog)
        layout.addWidget(self.btn_select_param, 0, 0, 1, 2)

        self.lbl_selected_param = QLabel("<i>None Selected</i>")
        layout.addWidget(self.lbl_selected_param, 0, 2, 1, 2)

        # --- Row 1: Objective Metric ---
        layout.addWidget(QLabel("<b>Objective Metric:</b>"), 1, 0)
        lbl_metric = QLabel("Faraday Beam Current (SMU)")
        layout.addWidget(lbl_metric, 1, 1, 1, 3)

        # --- Row 2: Sweep Bounds ---
        layout.addWidget(QLabel("<b>Sweep Range:</b>"), 2, 0)

        self.sp_start = QDoubleSpinBox()
        self.sp_start.setPrefix("Start: ")
        self.sp_start.setDecimals(3)
        self.sp_start.setKeyboardTracking(False)
        layout.addWidget(self.sp_start, 2, 1)

        self.sp_stop = QDoubleSpinBox()
        self.sp_stop.setPrefix("Stop: ")
        self.sp_stop.setDecimals(3)
        self.sp_stop.setKeyboardTracking(False)
        layout.addWidget(self.sp_stop, 2, 2)

        # --- Row 3: Resolution & Timing ---
        layout.addWidget(QLabel("<b>Resolution:</b>"), 3, 0)

        self.sp_steps = QDoubleSpinBox()
        self.sp_steps.setPrefix("Steps: ")
        self.sp_steps.setDecimals(0)
        self.sp_steps.setRange(2, 1000)
        self.sp_steps.setValue(50)
        layout.addWidget(self.sp_steps, 3, 1)

        self.sp_dwell = QDoubleSpinBox()
        self.sp_dwell.setPrefix("Dwell: ")
        self.sp_dwell.setSuffix(" s")
        self.sp_dwell.setDecimals(2)
        self.sp_dwell.setRange(0.1, 60.0)
        self.sp_dwell.setValue(1.0)
        layout.addWidget(self.sp_dwell, 3, 2)

        # --- Row 4: Controls ---
        self.btn_start = QPushButton("START OPTIMIZATION")
        self.btn_start.setStyleSheet("""
                    QPushButton { background-color: #FF9800; color: white; font-weight: bold; }
                    QPushButton:disabled { background-color: #555555; color: #888888; }
                """)
        self.btn_start.setEnabled(False)
        self.btn_start.clicked.connect(self._start_optimization)
        layout.addWidget(self.btn_start, 4, 1)

        self.btn_stop = QPushButton("ABORT")
        self.btn_stop.setStyleSheet("""
                    QPushButton { background-color: #F44336; color: white; font-weight: bold; }
                    QPushButton:disabled { background-color: #555555; color: #888888; }
                """)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_optimization)
        layout.addWidget(self.btn_stop, 4, 2)

        self.lbl_status = QLabel("Status: Idle")
        layout.addWidget(self.lbl_status, 5, 0, 1, 4)

        group.setLayout(layout)
        self.main_layout.addWidget(group)
        self.main_layout.addStretch()

    def _open_selector_dialog(self):
        dialog = OptimizerTagSelectorDialog(self.registry, self)
        if dialog.exec():
            self.selected_target_tag = dialog.selected_tag
            self.selected_target_meta = dialog.selected_meta
            self._apply_target_constraints()

    def _apply_target_constraints(self):
        meta = self.selected_target_meta
        tag_name = meta.get("default_label", self.selected_target_tag.split('.')[-1])
        unit = meta.get("unit", "")

        self.lbl_selected_param.setText(f"<b>{tag_name}</b> [{unit}]")

        min_v = float(meta["min_val"])
        max_v = float(meta["max_val"])

        self.sp_start.setRange(min_v, max_v)
        self.sp_start.setValue(min_v)
        self.sp_start.setSuffix(f" {unit}")

        self.sp_stop.setRange(min_v, max_v)
        self.sp_stop.setValue(max_v)
        self.sp_stop.setSuffix(f" {unit}")

    def _derive_mode_tag(self, target_tag: str) -> str:
        parts = target_tag.split('.')
        base = ".".join(parts[:-1])
        return f"{base}.cmd_ctrl_mode"

    def _start_optimization(self):
        if not self.selected_target_tag:
            return

        target_service = self.selected_target_meta["source"]
        mode_tag = self._derive_mode_tag(self.selected_target_tag)

        start_val = self.sp_start.value()
        stop_val = self.sp_stop.value()

        if start_val == stop_val:
            QMessageBox.warning(self, "Invalid Parameters", "Start and Stop values cannot be identical.")
            return

        if mode_tag not in self.registry:
            QMessageBox.critical(self, "Architecture Error", f"Cannot find arbitration tag: {mode_tag}")
            return

        self._lock_ui(True)

        # Map the registry source to the HMI Command Thread target port
        route_map = {
            "service_plc_snap7": "plc",
            "service_magnet": "magnet",
            "service_spellman": "spellman",
            "service_vacuum": "vacuum",
            "service_source_turbo": "source_turbo",
            "service_faraday_smu": "smu"
        }
        routing_target = route_map.get(target_service, "plc")

        self.scan_worker = OptimizerWorker(
            cmd_thread=self.cmd_thread,
            get_telemetry_cb=self.get_telemetry_cb,
            target_service=routing_target,
            target_tag=self.selected_target_tag,
            mode_tag=mode_tag,
            rb_tag=self.rb_metric_tag,
            start_val=start_val,
            stop_val=stop_val,
            steps=int(self.sp_steps.value()),
            dwell=self.sp_dwell.value()
        )

        self.scan_worker.status_msg.connect(self.lbl_status.setText)
        self.scan_worker.scan_finished.connect(self._on_scan_finished)
        self.scan_worker.start()

    def _stop_optimization(self):
        if self.scan_worker and self.scan_worker.isRunning():
            self.scan_worker.stop()
            self.scan_worker.wait()
        self._lock_ui(False)
        self.lbl_status.setText("Status: Aborted by User.")

    def _on_scan_finished(self):
        self._lock_ui(False)

    def _lock_ui(self, is_running: bool):
        self.btn_select_param.setEnabled(not is_running)
        self.sp_start.setEnabled(not is_running)
        self.sp_stop.setEnabled(not is_running)
        self.sp_steps.setEnabled(not is_running)
        self.sp_dwell.setEnabled(not is_running)
        self.btn_stop.setEnabled(is_running)

        # Only re-enable Start if a valid parameter is currently loaded
        self.btn_start.setEnabled((not is_running) and (self.selected_target_tag is not None))

    def update_telemetry(self, data: dict):
        master_comms_lost = not bool(data.get("system.connected", False)) or bool(
            data.get("ion_beam.system.pc_plc_comms_lost", False))
        relay_active = bool(data.get("ion_beam.facilities.safety_relay_active", False))

        target_service = self.selected_target_meta["source"] if self.selected_target_meta else None

        service_online = True
        heartbeat_name = None

        if target_service:
            # Map registry 'source' keys to the actual Service Manager heartbeat names
            service_translation = {
                "service_magnet": "service_magnet_psu",
                "service_plc_snap7": "service_plc",
                "service_faraday_smu": "service_smu",
                "service_spellman": "service_spellman_mpd",
                "service_vacuum": "service_vac_gauge_controllers",
                "service_source_turbo": "service_source_turbo"
            }
            heartbeat_name = service_translation.get(target_service, target_service)

            services_status = data.get("manager.services", {})
            service_online = services_status.get(heartbeat_name, "OFFLINE") == "ONLINE"

        is_sweeping = self.scan_worker is not None and self.scan_worker.isRunning()

        # Check if the specific physical hardware is enabled
        hw_enabled = True
        if self.selected_target_tag:
            enable_tag = self._derive_enable_tag(self.selected_target_tag)
            # If the device has an enable tag in the registry, check it. Otherwise assume True.
            if enable_tag in self.registry:
                hw_enabled = bool(data.get(enable_tag, 0.0))

        if is_sweeping and (master_comms_lost or not relay_active or not service_online or not hw_enabled):
            self._stop_optimization()

        lock_reason = ""
        if master_comms_lost:
            lock_reason = "PLC communications offline."
        elif not relay_active:
            lock_reason = "Safety Relay is De-Energized."
        elif target_service and not service_online:
            lock_reason = f"Target service ({target_service}) is offline."
        elif not hw_enabled:
            lock_reason = f"Hardware is not enabled."

        if not is_sweeping:
            lockout = bool(lock_reason)
            can_start = (not lockout) and (self.selected_target_tag is not None)

            self.btn_start.setEnabled(can_start)

            # Dynamic Tooltips
            if self.selected_target_tag is None:
                self.btn_start.setToolTip("Disabled: Select a parameter to sweep first.")
            elif lockout:
                self.btn_start.setToolTip(f"Disabled: {lock_reason}")
            else:
                self.btn_start.setToolTip("Click to start optimization sweep.")