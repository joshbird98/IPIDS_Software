import time
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QGridLayout, QLabel, QPushButton,
    QGroupBox, QListWidget, QListWidgetItem
)
from PyQt6.QtCore import Qt

from src.core.theme import (
    COLOR_OK,
    COLOR_FAULT,
    COLOR_INACTIVE,
    COLOR_WARNING,
    COLOR_BUTTON_STANDARD
)


class SystemHealthWidget(QWidget):
    """Centralized widget for Master Safety, Shutdown Sequences, and Active Faults."""

    def __init__(self, cmd_thread, fault_engine):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.fault_engine = fault_engine

        self.main_layout = QVBoxLayout(self)

        self._init_safety_ui()
        self._init_faults_ui()

        self.main_layout.addStretch()

    def _dispatch_command(self, tag: str, value):
        self.cmd_thread.send_command("plc", tag, value)

    def _init_safety_ui(self):
        safety_group = QGroupBox("Master Safety && Sequence Control")
        sl = QGridLayout()

        self.lbl_comms = QLabel("PLC COMMS: WAITING FOR DATA...")
        self.lbl_comms.setStyleSheet(COLOR_WARNING)
        self.lbl_safety_state = QLabel("SAFETY RELAY: WAITING FOR DATA...")
        self.lbl_safety_state.setStyleSheet(COLOR_WARNING)
        self.lbl_shutdown_state = QLabel("SHUTDOWN SEQ: WAITING FOR DATA...")
        self.lbl_shutdown_state.setStyleSheet(COLOR_WARNING)

        self.btn_enable_safety = QPushButton("ENABLE SAFETY RELAY")
        self.btn_enable_safety.setCheckable(True)
        self.btn_enable_safety.setStyleSheet("""
            QPushButton { background-color: #2196F3; color: white; font-weight: bold; border-radius: 4px; padding: 6px; }
            QPushButton:checked { background-color: #F44336; color: white; }
            QPushButton:disabled { background-color: #757575; color: #B0B0B0; font-weight: bold; }
        """)
        self.btn_enable_safety.clicked.connect(
            lambda *args, b=self.btn_enable_safety: self._dispatch_command("ion_beam.system.cmd_enable_safety",
                                                                           b.isChecked()))

        self.btn_reset_faults = QPushButton("RESET FAULTS")
        self.btn_reset_faults.setStyleSheet(COLOR_BUTTON_STANDARD)
        self.btn_reset_faults.clicked.connect(
            lambda *args: self._dispatch_command("ion_beam.system.cmd_fault_reset", True))

        self.btn_shutdown = QPushButton("INITIATE SHUTDOWN")
        self.btn_shutdown.setStyleSheet("""
            QPushButton { background-color: #F44336; color: white; font-weight: bold; border-radius: 4px; padding: 6px; }
            QPushButton:disabled { background-color: #757575; color: #E0E0E0; font-weight: bold; }
        """)
        self.btn_shutdown.clicked.connect(
            lambda *args: self._dispatch_command("ion_beam.system.cmd_source_shutdown", True))

        self.btn_cancel_shutdown = QPushButton("ABORT SHUTDOWN")
        self.btn_cancel_shutdown.setStyleSheet("""
            QPushButton { background-color: #FF9800; color: black; font-weight: bold; border-radius: 4px; padding: 6px; }
            QPushButton:disabled { background-color: #757575; color: #E0E0E0; font-weight: bold; }
        """)
        self.btn_cancel_shutdown.clicked.connect(
            lambda *args: self._dispatch_command("ion_beam.system.cmd_cancel_shutdown", True))

        sl.addWidget(self.lbl_comms, 0, 0)
        sl.addWidget(self.lbl_safety_state, 0, 1)
        sl.addWidget(self.lbl_shutdown_state, 0, 2)
        sl.addWidget(self.btn_enable_safety, 1, 0)
        sl.addWidget(self.btn_reset_faults, 1, 1)
        sl.addWidget(self.btn_shutdown, 1, 2)
        sl.addWidget(self.btn_cancel_shutdown, 1, 3)

        safety_group.setLayout(sl)
        self.main_layout.addWidget(safety_group)

    def _init_faults_ui(self):
        fault_group = QGroupBox("System Faults && Alarms")
        fault_layout = QVBoxLayout()

        fault_layout.addWidget(QLabel("<b>ACTIVE FAULTS:</b>"))
        self.active_faults_list = QListWidget()
        self.active_faults_list.setStyleSheet("background-color: #ffebee; color: #b71c1c; font-weight: bold;")
        self.active_faults_list.setMinimumHeight(150)

        fault_layout.addWidget(self.active_faults_list)
        fault_group.setLayout(fault_layout)
        self.main_layout.addWidget(fault_group)

    def update_telemetry(self, data: dict):
        master_comms_lost = not bool(data.get("system.connected", False)) or bool(
            data.get("ion_beam.system.pc_plc_comms_lost", False))

        relay_active = bool(data.get("ion_beam.facilities.safety_relay_active", False))
        auto_sdown = bool(data.get("ion_beam.system.src_auto_sdown", 0.0))
        active_step = max(int(data.get("ion_beam.system.remote_shutdown_step", 0.0)),
                          int(data.get("ion_beam.system.pc_shutdown_step", 0.0)))

        self._update_comms_and_safety_state(data, master_comms_lost, relay_active)
        self._update_shutdown_labels(active_step, auto_sdown, master_comms_lost)
        self._update_safety_controls(data, relay_active, active_step, master_comms_lost)
        self._update_fault_list(data, master_comms_lost)

    def _update_comms_and_safety_state(self, data: dict, master_comms_lost: bool, relay_active: bool):
        if master_comms_lost:
            self.lbl_comms.setText("PLC COMMS: OFFLINE")
            self.lbl_comms.setStyleSheet(COLOR_FAULT)
            self.lbl_safety_state.setText("SAFETY: UNKNOWN")
            self.lbl_safety_state.setStyleSheet(COLOR_INACTIVE)
            return

        self.lbl_comms.setText("PLC COMMS: OK")
        self.lbl_comms.setStyleSheet(COLOR_OK)

        w0_raw = data.get("ion_beam.faults.word_0_system")
        w0_faults = self.fault_engine.get_active_faults("UDT_Fault_Word_0_System", int(w0_raw)) if w0_raw else []

        sr_tripped = any(f["name"] == "Safety_Relay_Tripped" for f in w0_faults)
        sr_refused = any(f["name"] == "Safety_Relay_Refused" for f in w0_faults)
        remote_lost = any(f["name"] == "Remote_IO_Lost" for f in w0_faults) or bool(
            data.get("ion_beam.system.remote_io_lost", 0.0))

        if relay_active:
            self.lbl_safety_state.setText("SAFETY: LIVE")
            self.lbl_safety_state.setStyleSheet(COLOR_WARNING)
        elif sr_tripped or sr_refused:
            self.lbl_safety_state.setText("SAFETY: TRIPPED/REFUSED")
            self.lbl_safety_state.setStyleSheet(COLOR_FAULT)
        elif remote_lost:
            self.lbl_safety_state.setText("SAFETY: NO REMOTE IO")
            self.lbl_safety_state.setStyleSheet(COLOR_FAULT)
        else:
            self.lbl_safety_state.setText("SAFETY: DE-ENERGIZED")
            self.lbl_safety_state.setStyleSheet(COLOR_OK)

    def _update_shutdown_labels(self, active_step: int, auto_sdown: bool, master_comms_lost: bool):
        if master_comms_lost:
            self.lbl_shutdown_state.setText("SHUTDOWN SEQ: UNKNOWN")
            self.lbl_shutdown_state.setStyleSheet(COLOR_INACTIVE)
        elif auto_sdown or active_step > 0:
            if active_step == 1:
                self.lbl_shutdown_state.setText("SHUTDOWN SEQ: GRACE PERIOD (75% EXTR)")
            elif active_step == 2:
                self.lbl_shutdown_state.setText("SHUTDOWN SEQ: CESIUM COOLDOWN")
            elif active_step == 3:
                self.lbl_shutdown_state.setText("SHUTDOWN SEQ: FINAL KILL")
            elif active_step == 10:
                self.lbl_shutdown_state.setText("SHUTDOWN SEQ: HOLD HOT PHASE")
            elif active_step == 11:
                self.lbl_shutdown_state.setText("SHUTDOWN SEQ: FILAMENT RAMP DOWN")
            elif active_step == 99:
                self.lbl_shutdown_state.setText("SHUTDOWN SEQ: LATCHED SAFE (RESET REQ)")
            self.lbl_shutdown_state.setStyleSheet(COLOR_WARNING if active_step in [1, 2, 10, 11] else COLOR_FAULT)
        else:
            self.lbl_shutdown_state.setText("SHUTDOWN SEQ: INACTIVE")
            self.lbl_shutdown_state.setStyleSheet(COLOR_OK)

    def _update_safety_controls(self, data: dict, relay_active: bool, active_step: int, master_comms_lost: bool):
        gv_is_open = bool(data.get("ion_beam.source.chamber.stat_gv_open", False))
        source_is_cold = bool(data.get("ion_beam.source.chamber.stat_gv_close_permissive", False))

        # Directly evaluate power supply activity from telemetry tags
        psu_tags = [
            "ion_beam.source.einzel.stat_enabled",
            "ion_beam.source.extraction.stat_enabled",
            "ion_beam.source.target.stat_enabled",
            "ion_beam.source.filament.stat_enabled",
            "ion_beam.source.thermionic.stat_enabled"
        ]
        any_psu_active = any(bool(data.get(tag, False)) for tag in psu_tags)

        self.btn_reset_faults.setEnabled(not master_comms_lost)
        self.btn_reset_faults.setToolTip(
            "Disabled: GUI to PLC communications are offline." if master_comms_lost else "Click to clear latched software and hardware faults.")

        cmd_enable_safety = bool(data.get("ion_beam.system.cmd_enable_safety", False))
        timer_elapsed_ms = float(data.get("ion_beam.system.safety_timer_elapsed_ms", 0.0))
        timer_total_ms = float(data.get("ion_beam.system.safety_timer_total_ms", 5000.0))

        is_timing_out = cmd_enable_safety and (0 < timer_elapsed_ms < timer_total_ms) and not relay_active

        if is_timing_out and timer_total_ms > 0:
            remaining_sec = (timer_total_ms - timer_elapsed_ms) / 1000.0
            total_sec = timer_total_ms / 1000.0
            fill_ratio = max(0.0, min(1.0, remaining_sec / total_sec))

            sweep_style = f"""
                QPushButton {{
                    background: qlineargradient(x1:1, y1:0, x2:0, y2:0, 
                                                stop:0 #F44336, 
                                                stop:{fill_ratio:.3f} #F44336, 
                                                stop:{min(1.0, fill_ratio + 0.001):.3f} #2196F3);
                    color: white; font-weight: bold; border-radius: 4px; padding: 6px;
                }}
            """
            self.btn_enable_safety.blockSignals(True)
            self.btn_enable_safety.setChecked(True)
            self.btn_enable_safety.blockSignals(False)

            self.btn_enable_safety.setText(f"PRESS PANEL START ({int(remaining_sec) + 1}s)")
            self.btn_enable_safety.setStyleSheet(sweep_style)
            self.btn_enable_safety.setEnabled(True)
            self.btn_enable_safety.setToolTip(f"Awaiting physical panel button press... ({total_sec}s timeout)")

        else:
            self.btn_enable_safety.blockSignals(True)
            self.btn_enable_safety.setChecked(relay_active)
            self.btn_enable_safety.blockSignals(False)

            self.btn_enable_safety.setStyleSheet("""
                QPushButton { background-color: #2196F3; color: white; font-weight: bold; border-radius: 4px; padding: 6px; }
                QPushButton:checked { background-color: #F44336; color: white; }
                QPushButton:disabled { background-color: #757575; color: #B0B0B0; font-weight: bold; }
            """)

            if master_comms_lost:
                self.btn_enable_safety.setEnabled(False)
                self.btn_enable_safety.setToolTip("Disabled: GUI to PLC communications are offline.")
            elif not relay_active:
                if not gv_is_open:
                    self.btn_enable_safety.setEnabled(False)
                    self.btn_enable_safety.setText("ENABLE SAFETY RELAY")
                    self.btn_enable_safety.setToolTip(
                        "Disabled: The Gate Valve must be fully OPEN before the safety circuit can be energized.")
                else:
                    self.btn_enable_safety.setEnabled(True)
                    self.btn_enable_safety.setText("ENABLE SAFETY RELAY")
                    self.btn_enable_safety.setToolTip("Click to energize the Master Safety Relay.")
            else:
                if not source_is_cold:
                    self.btn_enable_safety.setEnabled(False)
                    self.btn_enable_safety.setText("RELAY ENERGIZED (LOCKED)")
                    self.btn_enable_safety.setToolTip(
                        "Disabled: You cannot drop the safety relay while the source is hot or active. Run a shutdown first.")
                else:
                    self.btn_enable_safety.setEnabled(True)
                    self.btn_enable_safety.setText("DISABLE SAFETY RELAY")
                    self.btn_enable_safety.setToolTip("Click to manually de-energize the Master Safety Relay.")

        can_shutdown = (active_step == 0) and relay_active and (any_psu_active or not source_is_cold)
        self.btn_shutdown.setEnabled(can_shutdown and not master_comms_lost)

        if master_comms_lost:
            self.btn_shutdown.setToolTip("Disabled: GUI to PLC communications are offline.")
        elif active_step > 0:
            self.btn_shutdown.setToolTip("Disabled: Shutdown sequence already in progress.")
        elif not relay_active:
            self.btn_shutdown.setToolTip("Disabled: Safety Relay is dead. Source is already powered off.")
        elif not any_psu_active and source_is_cold:
            self.btn_shutdown.setToolTip("Disabled: All power supplies are off and the source is already cold.")
        else:
            self.btn_shutdown.setToolTip("Click to initiate a controlled, safe shutdown of the Ion Source.")

        p_step = int(data.get("ion_beam.system.pc_shutdown_step", 0.0))
        self.btn_cancel_shutdown.setEnabled(p_step == 10 and not master_comms_lost)
        if master_comms_lost:
            self.btn_cancel_shutdown.setToolTip("Disabled: GUI to PLC communications are offline.")
        elif p_step == 10:
            self.btn_cancel_shutdown.setToolTip("Click to abort the shutdown and resume normal operations.")
        else:
            self.btn_cancel_shutdown.setToolTip(
                "Disabled: Abort is only permitted during the initial 'Hold Hot' phase.")

    def _update_fault_list(self, data: dict, master_comms_lost: bool):
        self.active_faults_list.clear()

        if master_comms_lost:
            item = QListWidgetItem("⚠️ TELEMETRY OFFLINE: FAULT STATUS UNKNOWN ⚠️")
            self.active_faults_list.addItem(item)
            return

        word_mapping = {
            "ion_beam.faults.word_0_system": "UDT_Fault_Word_0_System",
            "ion_beam.faults.word_1_pumps": "UDT_Fault_Word_1_Pumps",
            "ion_beam.faults.word_2_source": "UDT_Fault_Word_2_Source",
            "ion_beam.faults.word_4_magnet": "UDT_Fault_Word_4_Magnet",
            "ion_beam.faults.word_5_gauges": "UDT_Fault_Word_5_Gauges"
        }

        for tag, udt_key in word_mapping.items():
            raw_word = data.get(tag)
            if raw_word is not None:
                active_faults = self.fault_engine.get_active_faults(udt_key, int(raw_word))
                active_names = [f["name"] for f in active_faults]

                udt_info = self.fault_engine.fault_structures.get(udt_key, {})
                for bit_str, info in udt_info.get("bits", {}).items():
                    name = info["name"]
                    if name in active_names:
                        desc = info.get("description", "No description provided.")
                        list_item = QListWidgetItem(f"⚠️ {name.upper()}: {desc}")
                        self.active_faults_list.addItem(list_item)