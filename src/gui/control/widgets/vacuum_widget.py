"""Vacuum System panel: the six gauges, the source turbo pump and the source gate valve.

A gauge or the pump without a live reading (its service stopped or restarting, a controller or
the pump not answering, no good reading from a gauge) keeps its last value, greyed, with a STALE
chip saying how long ago it was read; the tooltips say why. The turbo pump can only be started
here: it is stopped by hand, at the pump, by trained staff, never from the software.
"""
import logging
import time

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton, QFrame, QGroupBox,
)

from src.gui.control.last_known import LastKnown, age_text, stale_text
from src.gui.reflow import ReflowGrid
from src.gui.style import chip, reserve_width, set_ink, set_role, set_tone

GAUGE_NAMES = {
    1: "Source (VG1)", 2: "Beamline pre-magnet (VG2)", 3: "Beamline (VG3)",
    4: "Endstation 1 (VG4)", 5: "Loadlock (VG5)", 6: "Endstation 2 (VG6)",
}
GAUGE_LOCATIONS = {1: "source", 2: "beamline", 3: "beamline", 4: "endstation", 5: "loadlock", 6: "endstation"}
TURBO = "ion_beam.source.turbo_pump"
TURBO_START_TIP = ("Start the source turbo pump. It is stopped only by hand, at the pump, by trained staff: "
                   "never from the software.")
GV_HOLD = "ion_beam.source.chamber.vac_hold_left_s"
GV_HOLD_TIP = ("The vacuum service is restarting or not sending data. Meanwhile the PLC keeps the vacuum "
               "service's last judgment of the vacuum: the gate valve's permissive and the gauge flags as "
               "they were. If the vacuum service hasn't judged the vacuum again when the time runs out, the "
               "PLC is told the vacuum is unknown, as without the hold: an open gate valve closes, and the "
               "safety relay drops.")


def _set(label, text: str, tone: str):
    label.setText(text)
    set_tone(label, tone)


def _tip(text: str, *labels):
    for label in labels:
        if label.toolTip() != text:
            label.setToolTip(text)


# --- Vacuum Subsystem Component ---

class VacuumControlWidget(QWidget):
    def __init__(self, cmd_thread, fault_engine):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.fault_engine = fault_engine
        self.last = LastKnown()          # the last live readings, shown greyed once they stop
        self._turbo_moved = None         # (speed, time) the pump's speed last moved from
        self._turbo_dir = None           # 'up' / 'down': which way it moved

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
        self.btn_turbo_start.setToolTip(TURBO_START_TIP)
        self.btn_turbo_start.clicked.connect(
            lambda checked=False: self.cmd_thread.send_command("src_turbo", f"{TURBO}.cmd_enable", True))

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

        # The vacuum service restarting: the PLC is given its last judgment of the vacuum meanwhile
        self.lbl_gv_hold = chip("", "warn")
        self.lbl_gv_hold.setWordWrap(True)
        self.lbl_gv_hold.setToolTip(GV_HOLD_TIP)
        self.lbl_gv_hold.hide()

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
        gvl.addWidget(self.lbl_gv_hold)
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
        link_up = vac_connected and bool(data.get("VACUUM_DATA/system.connected"))
        for i in range(1, 7):
            gauge = self.gauges[i]
            base = f"ion_beam.{GAUGE_LOCATIONS[i]}.vacuum_gauge_{i}"
            pressure = data.get(f"{base}.rb_pressure") if link_up else None
            status = data.get(f"{base}.stat_error_code") if link_up else None
            not_found = bool(data.get(f"{base}.stat_not_found", False)) if link_up else False
            if pressure is not None:
                self.last.update(i, pressure)
                gauge["val"].setText(f"{pressure:.2e} mbar")
                set_ink(gauge["val"], None)
                _tip("", gauge["val"], gauge["status"])
                _set(gauge["status"], *self._gauge_state(data, base, status or 0, not_found))
                continue
            self._show_gauge_not_live(i, gauge, vac_connected, link_up, status, not_found)

    @staticmethod
    def _gauge_state(data: dict, base: str, status, not_found: bool):
        """(chip text, tone) for a gauge with a live reading, or for its controller's reason for none."""
        if not_found or status == 5:
            return "NOT FOUND", "fault"
        if data.get(f"{base}.stat_rapid_rise", False):
            return "RAPID RISE", "fault"
        if status == 3:
            return "SENSOR OFF", "off"
        if status != 0:
            return f"FAULT ({int(status)})", "fault"
        above_sp = data.get(f"{base}.stat_above_sp", False)
        if data.get(f"{base}.stat_relay_active", False) or above_sp:
            # Status 0 (online): the gauge's fail-safe relay set point
            if above_sp:
                return "TRIPPED", "fault"
            if data.get(f"{base}.stat_approaching_sp", False):
                return "WARNING", "warn"
            return "HEALTHY", "ok"
        return "ONLINE", "ok"

    def _show_gauge_not_live(self, i: int, gauge: dict, vac_connected: bool, link_up: bool, status, not_found):
        """The last pressure, greyed and STALE, or the controller's reason for having none."""
        if not vac_connected:
            why = "the vacuum service isn't sending data (stopped, restarting or not running)"
        elif not link_up:
            why = "the gauge controllers aren't answering"
        else:
            why = "no good reading from this gauge for 3 s or more"
        set_ink(gauge["val"], "muted")
        if i in self.last:
            value = self.last.value(i)
            gauge["val"].setText(f"{value:.2e} mbar")
            tip = (f"Not live: {why}.\n{value:.2e} mbar is the last reading, at {self.last.read_at(i)} "
                   f"({age_text(self.last.age(i))} ago).")
        else:
            gauge["val"].setText("--- mbar")
            tip = f"No reading: {why}."
        _tip(tip, gauge["val"], gauge["status"])
        explained = link_up and (not_found or status not in (None, 0))
        if explained:                           # the controller says why: NOT FOUND, SENSOR OFF, FAULT (n)
            _set(gauge["status"], *self._gauge_state({}, "", status, not_found))
        elif i in self.last:
            _set(gauge["status"], stale_text(self.last.age(i)), "unknown")
        else:
            _set(gauge["status"], "OFFLINE" if not vac_connected else "NO READING", "unknown")

    def _update_turbo(self, data: dict, turbo_connected: bool):
        link_up = turbo_connected and bool(data.get("SRC_TURBO_DATA/system.connected"))
        answering = link_up and not data.get(f"{TURBO}.stat_comms_fail")
        t_speed = data.get(f"{TURBO}.rb_speed_hz") if answering else None
        if t_speed is None:
            if not turbo_connected:
                why = "the turbo service isn't sending data (stopped, restarting or not running)"
            elif not link_up:
                why = "the link to the pump's controller is down"
            else:
                why = "the pump isn't answering"
            self._show_turbo_not_live(why)
            return
        t_current = data.get(f"{TURBO}.rb_current")
        t_trip = data.get(f"{TURBO}.stat_trip", False)
        t_ready = data.get(f"{TURBO}.stat_ready", False)
        t_turning = data.get(f"{TURBO}.stat_turning", False)

        self.last.update("turbo_speed", t_speed)
        self.lbl_turbo_speed.setText(f"{t_speed:.0f} Hz")
        if t_speed >= 990:
            t_ready = True
        if t_current is not None:            # (read every few cycles: between reads, the last one)
            self.last.update("turbo_current", t_current)
        if "turbo_current" in self.last:
            self.lbl_turbo_current.setText(f"{self.last.value('turbo_current'):.2f} A")
        for lbl in (self.lbl_turbo_speed, self.lbl_turbo_current):
            set_ink(lbl, None)
        _tip("", self.lbl_turbo_speed, self.lbl_turbo_current, self.lbl_turbo_status)

        _tip(TURBO_START_TIP, self.btn_turbo_start)
        direction = self._turbo_direction(t_speed)
        if t_trip:
            _set(self.lbl_turbo_status, "TRIPPED", "fault")
            self.btn_turbo_start.setEnabled(True)
            self.btn_turbo_start.setText("RESET && START")
        elif t_ready:
            _set(self.lbl_turbo_status, "AT SPEED", "ok")
            self.btn_turbo_start.setEnabled(False)
            self.btn_turbo_start.setText("RUNNING")
        elif t_turning and direction == "down":     # stopped (by hand, or a trip being reset)
            _set(self.lbl_turbo_status, "SLOWING DOWN", "info")
            self.btn_turbo_start.setEnabled(False)
            self.btn_turbo_start.setText("START PUMP")
            _tip("The pump is slowing down. It can be started again once it has stopped.", self.btn_turbo_start)
        elif t_turning:
            _set(self.lbl_turbo_status, "ACCELERATING" if direction == "up" else "TURNING", "info")
            self.btn_turbo_start.setEnabled(False)
            self.btn_turbo_start.setText("RUNNING")
        else:
            _set(self.lbl_turbo_status, "IDLE", "off")
            self.btn_turbo_start.setEnabled(True)
            self.btn_turbo_start.setText("START PUMP")

    def _turbo_direction(self, speed: float):
        """'up', 'down' or None: which way the speed has last moved (by 1 Hz or more)."""
        now = time.monotonic()
        last = self._turbo_moved
        if last is None or now - last[1] > 10.0:
            self._turbo_moved, self._turbo_dir = (speed, now), None
        elif abs(speed - last[0]) >= 1.0:
            self._turbo_moved, self._turbo_dir = (speed, now), ("up" if speed > last[0] else "down")
        return self._turbo_dir

    def _show_turbo_not_live(self, why: str):
        """The last speed and current, greyed, with STALE; nothing to press."""
        for key, lbl, unit, fmt in (("turbo_speed", self.lbl_turbo_speed, "Hz", "{:.0f}"),
                                    ("turbo_current", self.lbl_turbo_current, "A", "{:.2f}")):
            set_ink(lbl, "muted")
            if key in self.last:
                lbl.setText(f"{fmt.format(self.last.value(key))} {unit}")
                _tip(f"Not live: {why}.\nThe last reading, at {self.last.read_at(key)} "
                     f"({age_text(self.last.age(key))} ago).", lbl)
            else:
                lbl.setText(f"--- {unit}")
                _tip(f"No reading: {why}.", lbl)
        if "turbo_speed" in self.last:
            _set(self.lbl_turbo_status, stale_text(self.last.age("turbo_speed")), "unknown")
        else:
            _set(self.lbl_turbo_status, "OFFLINE", "unknown")
        _tip(f"Not live: {why}.", self.lbl_turbo_status)
        self._turbo_moved = None
        self.btn_turbo_start.setEnabled(False)
        self.btn_turbo_start.setText("START PUMP")
        _tip(f"Not available: {why}.", self.btn_turbo_start)

    def _update_gate_valve(self, data: dict, master_comms_lost: bool):
        if master_comms_lost:
            self.btn_gv_open.setEnabled(False)
            self.btn_gv_close.setEnabled(False)
            self.btn_gv_open.setText("OFFLINE")
            self.btn_gv_close.setText("OFFLINE")
            self.btn_gv_open.setToolTip("Disabled: PLC telemetry stream is dead.")
            self.btn_gv_close.setToolTip("Disabled: PLC telemetry stream is dead.")
            _set(self.lbl_gv_status, "UNKNOWN", "unknown")
            self.lbl_gv_hold.hide()
            return
        hold_left = data.get(GV_HOLD) or 0.0
        if hold_left > 0:
            self.lbl_gv_hold.setText(f"VACUUM PERMISSIVE HELD: {hold_left:.0f} s LEFT")
        self.lbl_gv_hold.setVisible(hold_left > 0)
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
                "Disabled: the PLC doesn't allow the valve to open. It needs VG1 and VG2 read, healthy and "
                "below their set points, and no VG1, VG2, GRAPHIX or watchdog fault latched (F1 for more).")
        else:
            self.btn_gv_open.setToolTip("Click to command the gate valve OPEN.")

        if gv_is_closed:
            self.btn_gv_close.setToolTip("Disabled: Gate valve is already closed.")
        elif not gv_close_perm:
            self.btn_gv_close.setToolTip(
                "Disabled: Source must be cold (HV, Filament, and Cesium OFF) to close manually.")
        else:
            self.btn_gv_close.setToolTip("Click to command the gate valve CLOSED.")
