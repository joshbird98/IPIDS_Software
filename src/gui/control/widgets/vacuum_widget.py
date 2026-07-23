from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton, QFrame, QGroupBox,
)
from PyQt6.QtCore import Qt

from src.core.theme import (
    COLOR_OK,
    COLOR_FAULT,
    COLOR_INACTIVE,
    COLOR_WARNING,
    COLOR_BUTTON_STANDARD
)

# --- Vacuum Subsystem Component ---

class VacuumControlWidget(QWidget):
    def __init__(self, cmd_thread, fault_engine):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.fault_engine = fault_engine

        self.main_layout = QVBoxLayout(self)
        self._init_gauges_ui()
        self.ctrl_layout = QHBoxLayout()
        self._init_turbo_ui()
        self._init_gv_ui()
        self.main_layout.addLayout(self.ctrl_layout)
        self.main_layout.addStretch()

    def _init_gauges_ui(self):
        gauge_group = QGroupBox("Vacuum Gauges")
        gauge_layout = QGridLayout()
        self.gauges = {}
        gauge_names = {
            1: "Source (VG1)", 2: "Beamline Pre-Mag (VG2)", 3: "Beamline (VG3)",
            4: "Endstation 1 (VG4)", 5: "Loadlock (VG5)", 6: "Endstation 2 (VG6)"
        }
        for i in range(1, 7):
            frame = QFrame()
            frame.setFrameStyle(QFrame.Shape.StyledPanel | QFrame.Shadow.Raised)
            fl = QVBoxLayout(frame)
            lbl_title = QLabel(f"<b>{gauge_names[i]}</b>")
            lbl_val = QLabel("--- mbar")
            lbl_val.setStyleSheet("font-size: 13pt; font-family: monospace;")
            lbl_status = QLabel("OFFLINE")
            lbl_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl_status.setStyleSheet(COLOR_INACTIVE)
            fl.addWidget(lbl_title)
            fl.addWidget(lbl_val)
            fl.addWidget(lbl_status)
            gauge_layout.addWidget(frame, (i - 1) // 3, (i - 1) % 3)
            self.gauges[i] = {"val": lbl_val, "status": lbl_status}
        gauge_group.setLayout(gauge_layout)
        self.main_layout.addWidget(gauge_group)

    def _init_turbo_ui(self):
        turbo_group = QGroupBox("Source Turbo Pump")
        tl = QVBoxLayout()
        self.lbl_turbo_speed = QLabel("Speed: --- Hz")
        self.lbl_turbo_current = QLabel("Current: --- A")
        self.lbl_turbo_status = QLabel("OFFLINE")
        self.lbl_turbo_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_turbo_status.setStyleSheet(COLOR_INACTIVE)

        self.btn_turbo_start = QPushButton("START PUMP")
        self.btn_turbo_start.setStyleSheet(COLOR_BUTTON_STANDARD)
        self.btn_turbo_start.clicked.connect(
            lambda checked=False: self.cmd_thread.send_command("src_turbo", "ion_beam.source.turbo_pump.cmd_enable",
                                                               True))

        tl.addWidget(self.lbl_turbo_speed)
        tl.addWidget(self.lbl_turbo_current)
        tl.addWidget(self.lbl_turbo_status)
        tl.addWidget(self.btn_turbo_start)
        turbo_group.setLayout(tl)
        self.ctrl_layout.addWidget(turbo_group)

    def _init_gv_ui(self):
        gv_group = QGroupBox("Source Gate Valve Control")
        gvl = QVBoxLayout()
        self.lbl_gv_status = QLabel("UNKNOWN")
        self.lbl_gv_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_gv_status.setStyleSheet(COLOR_INACTIVE)

        self.lbl_gv_fault = QLabel("")
        self.lbl_gv_fault.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_gv_fault.hide()

        btn_row = QHBoxLayout()
        self.btn_gv_open = QPushButton("OPEN")
        self.btn_gv_open.setStyleSheet(COLOR_BUTTON_STANDARD)
        self.btn_gv_open.clicked.connect(
            lambda checked=False: self.cmd_thread.send_command("plc", "ion_beam.source.chamber.cmd_open_gv", True))

        self.btn_gv_close = QPushButton("CLOSE")
        self.btn_gv_close.setStyleSheet(COLOR_BUTTON_STANDARD)
        self.btn_gv_close.clicked.connect(
            lambda checked=False: self.cmd_thread.send_command("plc", "ion_beam.source.chamber.cmd_open_gv", False))

        btn_row.addWidget(self.btn_gv_open)
        btn_row.addWidget(self.btn_gv_close)
        gvl.addWidget(self.lbl_gv_status)
        gvl.addWidget(self.lbl_gv_fault)
        gvl.addLayout(btn_row)
        gv_group.setLayout(gvl)
        self.ctrl_layout.addWidget(gv_group)

    def update_telemetry(self, data: dict):
        try:
            sys_connected = bool(data.get("system.connected", False))
            pc_plc_lost = bool(data.get("ion_beam.system.pc_plc_comms_lost", False))
            master_comms_lost = not sys_connected or pc_plc_lost

            turbo_connected = bool(data.get("pump.connected", False))
            vac_connected = bool(data.get("vacuum.connected", False))

            self._update_gauges(data, vac_connected)
            self._update_turbo(data, turbo_connected)
            self._update_gate_valve(data, master_comms_lost)
        except Exception as e:
            print(f"[UI Parser Error] Vacuum Runtime exception: {e}")

    def _update_gauges(self, data: dict, vac_connected: bool):
        gauge_locations = {
            1: "source",
            2: "beamline",
            3: "beamline",
            4: "endstation",
            5: "loadlock",
            6: "endstation"
        }
        for i in range(1, 7):
            if not vac_connected:
                self.gauges[i]["status"].setText("OFFLINE")
                self.gauges[i]["status"].setStyleSheet(COLOR_INACTIVE)
            else:
                loc = gauge_locations[i]
                pressure = data.get(f"ion_beam.{loc}.vacuum_gauge_{i}.rb_pressure")
                status = data.get(f"ion_beam.{loc}.vacuum_gauge_{i}.stat_error_code")

                rapid_rise = data.get(f"ion_beam.{loc}.vacuum_gauge_{i}.stat_rapid_rise", False)
                not_found = data.get(f"ion_beam.{loc}.vacuum_gauge_{i}.stat_not_found", False)

                # Fetch dynamic relay states
                approaching_sp = data.get(f"ion_beam.{loc}.vacuum_gauge_{i}.stat_approaching_sp", False)
                relay_active = data.get(f"ion_beam.{loc}.vacuum_gauge_{i}.stat_relay_active", False)
                above_sp = data.get(f"ion_beam.{loc}.vacuum_gauge_{i}.stat_above_sp", False)

                if pressure is not None:
                    self.gauges[i]["val"].setText(f"{pressure:.2e} mbar")
                    if status is None: status = 0

                if status is not None:
                    if not_found or status == 5:
                        self.gauges[i]["status"].setText("NOT FOUND")
                        self.gauges[i]["status"].setStyleSheet(COLOR_FAULT)
                    elif rapid_rise:
                        self.gauges[i]["status"].setText("RAPID RISE")
                        self.gauges[i]["status"].setStyleSheet(COLOR_FAULT)
                    elif status == 3:
                        self.gauges[i]["status"].setText("SENSOR OFF")
                        self.gauges[i]["status"].setStyleSheet(COLOR_INACTIVE)
                    elif status != 0:
                        self.gauges[i]["status"].setText(f"FAULT ({int(status)})")
                        self.gauges[i]["status"].setStyleSheet(COLOR_FAULT)
                    else:
                        # Status is 0 (Online/OK) - Evaluate dynamic fail-safe relay states
                        has_relay = bool(relay_active or above_sp)

                        if has_relay:
                            if above_sp:
                                self.gauges[i]["status"].setText("TRIPPED")
                                self.gauges[i]["status"].setStyleSheet(COLOR_FAULT)
                            elif approaching_sp:
                                self.gauges[i]["status"].setText("WARNING")
                                self.gauges[i]["status"].setStyleSheet(COLOR_WARNING)
                            else:
                                self.gauges[i]["status"].setText("HEALTHY")
                                self.gauges[i]["status"].setStyleSheet(COLOR_OK)
                        else:
                            self.gauges[i]["status"].setText("ONLINE")
                            self.gauges[i]["status"].setStyleSheet(COLOR_OK)

    def _update_turbo(self, data: dict, turbo_connected: bool):
        if not turbo_connected:
            self.btn_turbo_start.setEnabled(False)
            self.btn_turbo_start.setText("-")
            self.btn_turbo_start.setToolTip("Disabled: Source Turbo telemetry stream is dead.")
            self.lbl_turbo_speed.setText(f"-")
            self.lbl_turbo_status.setText("OFFLINE")
        else:
            t_speed = data.get("ion_beam.source.turbo_pump.rb_speed_hz")
            t_current = data.get("ion_beam.source.turbo_pump.rb_current")
            t_trip = data.get("ion_beam.source.turbo_pump.stat_trip", False)
            t_ready = data.get("ion_beam.source.turbo_pump.stat_ready", False)
            t_turning = data.get("ion_beam.source.turbo_pump.stat_turning", False)

            if t_speed is not None:
                self.lbl_turbo_speed.setText(f"Speed: {t_speed} Hz")
                if t_speed >= 990: t_ready = True
            if t_current is not None:
                self.lbl_turbo_current.setText(f"Current: {t_current:.2f} A")

            if t_trip:
                self.lbl_turbo_status.setText("TRIPPED")
                self.lbl_turbo_status.setStyleSheet(COLOR_FAULT)
                self.btn_turbo_start.setEnabled(True)
                self.btn_turbo_start.setText("RESET && START")
            elif t_ready:
                self.lbl_turbo_status.setText("AT SPEED")
                self.lbl_turbo_status.setStyleSheet(COLOR_OK)
                self.btn_turbo_start.setEnabled(False)
                self.btn_turbo_start.setText("RUNNING")
            elif t_turning:
                self.lbl_turbo_status.setText("ACCELERATING")
                self.lbl_turbo_status.setStyleSheet(COLOR_WARNING)
                self.btn_turbo_start.setEnabled(False)
                self.btn_turbo_start.setText("RUNNING")
            else:
                self.lbl_turbo_status.setText("IDLE")
                self.lbl_turbo_status.setStyleSheet(COLOR_INACTIVE)
                self.btn_turbo_start.setEnabled(True)
                self.btn_turbo_start.setText("START PUMP")

    def _update_gate_valve(self, data: dict, master_comms_lost: bool):
        if master_comms_lost:
            self.btn_gv_open.setEnabled(False)
            self.btn_gv_close.setEnabled(False)
            self.btn_gv_open.setText("OFFLINE")
            self.btn_gv_close.setText("OFFLINE")
            self.btn_gv_open.setToolTip("Disabled: PLC telemetry stream is dead.")
            self.btn_gv_close.setToolTip("Disabled: PLC telemetry stream is dead.")
        else:
            raw_sys_fault = data.get("ion_beam.faults.word_0_system")
            gv_fault_active = False

            if raw_sys_fault is not None:
                active_sys_faults = self.fault_engine.get_active_faults("UDT_Fault_Word_0_System", int(raw_sys_fault))
                gv_faults = [f for f in active_sys_faults if "GateValve" in f["name"]]

                if gv_faults:
                    gv_fault_active = True
                    self.lbl_gv_fault.setText(f"FAULT: {gv_faults[0]['name'].upper()}")
                    self.lbl_gv_fault.setStyleSheet(COLOR_FAULT)
                    self.lbl_gv_fault.setVisible(True)
                else:
                    self.lbl_gv_fault.setVisible(False)

            gv_is_open = data.get("ion_beam.source.chamber.stat_gv_open")
            gv_is_closed = data.get("ion_beam.source.chamber.stat_gv_closed")
            gv_open_perm = bool(data.get("ion_beam.source.chamber.stat_gv_open_permissive", False))
            gv_close_perm = bool(data.get("ion_beam.source.chamber.stat_gv_close_permissive", False))

            if gv_is_open is not None and gv_is_closed is not None:
                gv_is_open = bool(gv_is_open)
                gv_is_closed = bool(gv_is_closed)

                if gv_is_open and not gv_is_closed:
                    self.lbl_gv_status.setText("OPEN")
                    self.lbl_gv_status.setStyleSheet(COLOR_OK)
                elif not gv_is_open and gv_is_closed:
                    self.lbl_gv_status.setText("CLOSED")
                    self.lbl_gv_status.setStyleSheet(COLOR_INACTIVE)
                elif not gv_is_open and not gv_is_closed:
                    self.lbl_gv_status.setText("TRAVELLING")
                    self.lbl_gv_status.setStyleSheet(COLOR_WARNING)
                elif gv_is_open and gv_is_closed:
                    self.lbl_gv_status.setText("SENSOR CONFLICT")
                    self.lbl_gv_status.setStyleSheet(COLOR_FAULT)

                if gv_fault_active:
                    self.btn_gv_open.setEnabled(False)
                    self.btn_gv_close.setEnabled(False)
                    self.btn_gv_open.setText("FAULT LOCKOUT")
                    self.btn_gv_close.setText("FAULT LOCKOUT")
                    self.btn_gv_open.setToolTip("Disabled: Gate valve hardware fault detected.")
                    self.btn_gv_close.setToolTip("Disabled: Gate valve hardware fault detected.")
                else:
                    self.btn_gv_open.setText("OPEN")
                    self.btn_gv_close.setText("CLOSE")
                    self.btn_gv_open.setEnabled(gv_open_perm and not gv_is_open)
                    self.btn_gv_close.setEnabled(gv_close_perm and not gv_is_closed)

                    self.btn_gv_open.setStyleSheet(
                        COLOR_BUTTON_STANDARD if self.btn_gv_open.isEnabled() else COLOR_INACTIVE)
                    self.btn_gv_close.setStyleSheet(
                        COLOR_BUTTON_STANDARD if self.btn_gv_close.isEnabled() else COLOR_INACTIVE)

                    if gv_is_open:
                        self.btn_gv_open.setToolTip("Disabled: Gate valve is already open.")
                    elif not gv_open_perm:
                        self.btn_gv_open.setToolTip(
                            "Disabled: Vacuum interlocks not met. Ensure turbo pump is at speed.")
                    else:
                        self.btn_gv_open.setToolTip("Click to command the gate valve OPEN.")

                    if gv_is_closed:
                        self.btn_gv_close.setToolTip("Disabled: Gate valve is already closed.")
                    elif not gv_close_perm:
                        self.btn_gv_close.setToolTip(
                            "Disabled: Source must be cold (HV, Filament, and Cesium OFF) to close manually.")
                    else:
                        self.btn_gv_close.setToolTip("Click to command the gate valve CLOSED.")
