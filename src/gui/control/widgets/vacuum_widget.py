import logging

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton, QFrame, QGroupBox,
)

from src.gui.reflow import ReflowGrid
from src.gui.style import chip, reserve_width, set_role, set_tone

GAUGE_NAMES = {
    1: "Source (VG1)", 2: "Beamline pre-magnet (VG2)", 3: "Beamline (VG3)",
    4: "Endstation 1 (VG4)", 5: "Loadlock (VG5)", 6: "Endstation 2 (VG6)",
}
GAUGE_LOCATIONS = {1: "source", 2: "beamline", 3: "beamline", 4: "endstation", 5: "loadlock", 6: "endstation"}


def _set(label, text: str, tone: str):
    label.setText(text)
    set_tone(label, tone)


# --- Vacuum Subsystem Component ---

class VacuumControlWidget(QWidget):
    def __init__(self, cmd_thread, fault_engine):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.fault_engine = fault_engine

        self.main_layout = QVBoxLayout(self)
        self._init_gauges_ui()
        # Turbo and gate valve side by side, or one above the other in a narrow panel
        self.ctrl_grid = ReflowGrid(max_columns=2)
        self._init_turbo_ui()
        self._init_gv_ui()
        self.main_layout.addWidget(self.ctrl_grid)
        self.main_layout.addStretch()

    def _init_gauges_ui(self):
        gauge_group = QGroupBox("Vacuum gauges")
        gauge_layout = QVBoxLayout(gauge_group)
        grid = ReflowGrid(max_columns=3)
        self.gauges = {}
        for i in range(1, 7):
            frame = QFrame()
            set_role(frame, "card")
            fl = QVBoxLayout(frame)
            fl.setContentsMargins(8, 6, 8, 8)
            fl.setSpacing(4)
            lbl_title = QLabel(GAUGE_NAMES[i])
            set_role(lbl_title, "caption")
            lbl_val = QLabel("--- mbar")
            set_role(lbl_val, "readout")
            reserve_width(lbl_val, "0.00e-00 mbar")
            lbl_status = chip("OFFLINE", "unknown")
            fl.addWidget(lbl_title)
            fl.addWidget(lbl_val)
            fl.addWidget(lbl_status)
            grid.add(frame)
            self.gauges[i] = {"val": lbl_val, "status": lbl_status}
        gauge_layout.addWidget(grid)
        self.main_layout.addWidget(gauge_group)

    def _init_turbo_ui(self):
        turbo_group = QGroupBox("Source turbo pump")
        tl = QGridLayout(turbo_group)
        tl.setColumnStretch(1, 1)
        for row, text in enumerate(("Speed", "Current")):
            cap = QLabel(text)
            set_role(cap, "caption")
            tl.addWidget(cap, row, 0)
        self.lbl_turbo_speed = QLabel("--- Hz")
        self.lbl_turbo_current = QLabel("--- A")
        for row, lbl in enumerate((self.lbl_turbo_speed, self.lbl_turbo_current)):
            set_role(lbl, "readout-sm")
            tl.addWidget(lbl, row, 1)
        self.lbl_turbo_status = chip("OFFLINE", "unknown")

        self.btn_turbo_start = QPushButton("START PUMP")
        self.btn_turbo_start.clicked.connect(
            lambda checked=False: self.cmd_thread.send_command("src_turbo", "ion_beam.source.turbo_pump.cmd_enable",
                                                               True))

        tl.addWidget(self.lbl_turbo_status, 2, 0, 1, 2)
        tl.addWidget(self.btn_turbo_start, 3, 0, 1, 2)
        self.ctrl_grid.add(turbo_group)

    def _init_gv_ui(self):
        gv_group = QGroupBox("Source gate valve")
        gvl = QVBoxLayout(gv_group)
        self.lbl_gv_status = chip("UNKNOWN", "unknown")

        self.lbl_gv_fault = chip("", "fault")
        self.lbl_gv_fault.setWordWrap(True)
        self.lbl_gv_fault.hide()

        btn_row = QHBoxLayout()
        self.btn_gv_open = QPushButton("OPEN")
        self.btn_gv_open.clicked.connect(
            lambda checked=False: self.cmd_thread.send_command("plc", "ion_beam.source.chamber.cmd_open_gv", True))

        self.btn_gv_close = QPushButton("CLOSE")
        self.btn_gv_close.clicked.connect(
            lambda checked=False: self.cmd_thread.send_command("plc", "ion_beam.source.chamber.cmd_open_gv", False))

        btn_row.addWidget(self.btn_gv_open)
        btn_row.addWidget(self.btn_gv_close)
        gvl.addWidget(self.lbl_gv_status)
        gvl.addWidget(self.lbl_gv_fault)
        gvl.addStretch()
        gvl.addLayout(btn_row)
        self.ctrl_grid.add(gv_group)

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
        except Exception:   # never let one bad value freeze the whole panel; the cause goes to the log
            logging.getLogger("hmi.vacuum").exception("Vacuum panel update failed")

    def _update_gauges(self, data: dict, vac_connected: bool):
        for i in range(1, 7):
            gauge = self.gauges[i]
            if not vac_connected:
                # Unknown, not the last value read: the gauge controller isn't answering
                gauge["val"].setText("--- mbar")
                _set(gauge["status"], "OFFLINE", "unknown")
                continue
            base = f"ion_beam.{GAUGE_LOCATIONS[i]}.vacuum_gauge_{i}"
            pressure = data.get(f"{base}.rb_pressure")
            status = data.get(f"{base}.stat_error_code")

            rapid_rise = data.get(f"{base}.stat_rapid_rise", False)
            not_found = data.get(f"{base}.stat_not_found", False)

            # Fetch dynamic relay states
            approaching_sp = data.get(f"{base}.stat_approaching_sp", False)
            relay_active = data.get(f"{base}.stat_relay_active", False)
            above_sp = data.get(f"{base}.stat_above_sp", False)

            if pressure is not None:
                gauge["val"].setText(f"{pressure:.2e} mbar")
                if status is None:
                    status = 0

            if status is None:
                continue
            if not_found or status == 5:
                _set(gauge["status"], "NOT FOUND", "fault")
            elif rapid_rise:
                _set(gauge["status"], "RAPID RISE", "fault")
            elif status == 3:
                _set(gauge["status"], "SENSOR OFF", "off")
            elif status != 0:
                _set(gauge["status"], f"FAULT ({int(status)})", "fault")
            elif relay_active or above_sp:
                # Status 0 (online): the gauge's fail-safe relay set point
                if above_sp:
                    _set(gauge["status"], "TRIPPED", "fault")
                elif approaching_sp:
                    _set(gauge["status"], "WARNING", "warn")
                else:
                    _set(gauge["status"], "HEALTHY", "ok")
            else:
                _set(gauge["status"], "ONLINE", "ok")

    def _update_turbo(self, data: dict, turbo_connected: bool):
        if not turbo_connected:
            self.btn_turbo_start.setEnabled(False)
            self.btn_turbo_start.setText("-")
            self.btn_turbo_start.setToolTip("Disabled: Source Turbo telemetry stream is dead.")
            self.lbl_turbo_speed.setText("--- Hz")
            self.lbl_turbo_current.setText("--- A")
            _set(self.lbl_turbo_status, "OFFLINE", "unknown")
            return
        t_speed = data.get("ion_beam.source.turbo_pump.rb_speed_hz")
        t_current = data.get("ion_beam.source.turbo_pump.rb_current")
        t_trip = data.get("ion_beam.source.turbo_pump.stat_trip", False)
        t_ready = data.get("ion_beam.source.turbo_pump.stat_ready", False)
        t_turning = data.get("ion_beam.source.turbo_pump.stat_turning", False)

        if t_speed is not None:
            self.lbl_turbo_speed.setText(f"{t_speed:.0f} Hz")
            if t_speed >= 990:
                t_ready = True
        if t_current is not None:
            self.lbl_turbo_current.setText(f"{t_current:.2f} A")

        self.btn_turbo_start.setToolTip("")
        if t_trip:
            _set(self.lbl_turbo_status, "TRIPPED", "fault")
            self.btn_turbo_start.setEnabled(True)
            self.btn_turbo_start.setText("RESET && START")
        elif t_ready:
            _set(self.lbl_turbo_status, "AT SPEED", "ok")
            self.btn_turbo_start.setEnabled(False)
            self.btn_turbo_start.setText("RUNNING")
        elif t_turning:
            _set(self.lbl_turbo_status, "ACCELERATING", "info")
            self.btn_turbo_start.setEnabled(False)
            self.btn_turbo_start.setText("RUNNING")
        else:
            _set(self.lbl_turbo_status, "IDLE", "off")
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
            _set(self.lbl_gv_status, "UNKNOWN", "unknown")
            return
        raw_sys_fault = data.get("ion_beam.faults.word_0_system")
        gv_fault_active = False

        if raw_sys_fault is not None:
            active_sys_faults = self.fault_engine.get_active_faults("UDT_Fault_Word_0_System", int(raw_sys_fault))
            gv_faults = [f for f in active_sys_faults if "GateValve" in f["name"]]

            if gv_faults:
                gv_fault_active = True
                self.lbl_gv_fault.setText(f"FAULT: {gv_faults[0]['name'].upper()}")
                self.lbl_gv_fault.setVisible(True)
            else:
                self.lbl_gv_fault.setVisible(False)

        gv_is_open = data.get("ion_beam.source.chamber.stat_gv_open")
        gv_is_closed = data.get("ion_beam.source.chamber.stat_gv_closed")
        gv_open_perm = bool(data.get("ion_beam.source.chamber.stat_gv_open_permissive", False))
        gv_close_perm = bool(data.get("ion_beam.source.chamber.stat_gv_close_permissive", False))

        if gv_is_open is None or gv_is_closed is None:
            return
        gv_is_open = bool(gv_is_open)
        gv_is_closed = bool(gv_is_closed)

        if gv_is_open and not gv_is_closed:
            _set(self.lbl_gv_status, "OPEN", "ok")
        elif not gv_is_open and gv_is_closed:
            _set(self.lbl_gv_status, "CLOSED", "off")
        elif not gv_is_open and not gv_is_closed:
            _set(self.lbl_gv_status, "TRAVELLING", "warn")
        else:
            _set(self.lbl_gv_status, "SENSOR CONFLICT", "fault")

        if gv_fault_active:
            self.btn_gv_open.setEnabled(False)
            self.btn_gv_close.setEnabled(False)
            self.btn_gv_open.setText("FAULT LOCKOUT")
            self.btn_gv_close.setText("FAULT LOCKOUT")
            self.btn_gv_open.setToolTip("Disabled: Gate valve hardware fault detected.")
            self.btn_gv_close.setToolTip("Disabled: Gate valve hardware fault detected.")
            return
        self.btn_gv_open.setText("OPEN")
        self.btn_gv_close.setText("CLOSE")
        self.btn_gv_open.setEnabled(gv_open_perm and not gv_is_open)
        self.btn_gv_close.setEnabled(gv_close_perm and not gv_is_closed)

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
