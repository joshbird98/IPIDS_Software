import json
import logging

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QGridLayout, QLabel,
    QPushButton, QGroupBox
)
from PyQt6.QtCore import Qt

from src.core import hard_caps, paths
from src.core.theme import (
    COLOR_OK,
    COLOR_FAULT,
    COLOR_WARNING,
    COLOR_BUTTON_STANDARD
)
from src.core.tolerance import FLAG_PREFIX
from src.gui.control.permits import permit_reason
from src.gui.control.arming import ArmedToggle
from src.gui.control.setpoint_box import SetpointBox

log = logging.getLogger("hmi.source")

TOLERANCE_STYLE = "border: 2px solid #FFB300; border-radius: 3px; padding: 1px;"
TOLERANCE_TIP = ("Readback has been outside its tolerance of the setpoint (not a fault).\n"
                 "Rules and tolerances: config/readback_tolerance.json; events are in events.db (TOLERANCE).")


def tolerance_flagged(data: dict, tags) -> bool:
    return any(data.get(FLAG_PREFIX + t) == 1.0 for t in tags)


def setpoint_limits() -> dict:
    """tag -> (min, max) for tags with both limits in the registry (the services refuse
    setpoints for the others, and anything outside them)."""
    try:
        with open(paths.config_path("system_tags.json"), "r") as f:
            reg = json.load(f)
    except (OSError, ValueError) as e:
        log.warning("Registry not readable for setpoint limits: %s", e)
        return {}
    return {t: (float(m["min_val"]), float(m["max_val"])) for t, m in reg.items()
            if m.get("min_val") is not None and m.get("max_val") is not None}


# --- Ion Source Subsystem Component ---

class IonSourceWidget(QWidget):
    def __init__(self, cmd_thread, fault_engine):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.fault_engine = fault_engine
        self._limited_tags = setpoint_limits()

        self.main_layout = QVBoxLayout(self)
        self._init_psu_ui()

    def _dispatch_command(self, tag: str, value):
        target = "spellman" if "einzel" in tag else "plc"
        self.cmd_thread.send_command(target, tag, value)

    def _apply_limits(self, sp_box, tag):
        """The setpoint box only offers what the service will accept (registry limits); a hard cap
        among them is named in its tooltip (a value typed past the box's maximum just doesn't go in)."""
        if tag in self._limited_tags:
            lo, hi = self._limited_tags[tag]
            sp_box.setRange(lo, hi)
        sp_box.setProperty("cap_note", hard_caps.note(tag))
        sp_box.setToolTip(hard_caps.note(tag))

    def _init_psu_ui(self):
        psu_group = QGroupBox("Source Power Supplies")
        self.psu_layout = QGridLayout()
        self.psu_controls = {}

        self._build_psu_row(0, "Source Einzel", "ion_beam.source.einzel", "kV", "µA", 0.0, 30.0, is_voltage=True,
                            decimals=3)
        self._build_psu_row(1, "Extraction", "ion_beam.source.extraction", "kV", "mA", 0.0, 20.0, is_voltage=True,
                            decimals=3)
        self._build_psu_row(2, "Target", "ion_beam.source.target", "kV", "mA", 0.0, 10.0, is_voltage=True, decimals=3)
        self._build_psu_row(3, "Filament", "ion_beam.source.filament", "A", "V", 0.0, 38.0, is_voltage=False,
                            decimals=3)
        self._build_psu_row(4, "Thermionic", "ion_beam.source.thermionic", "mA", "V", 0.0, 1000.0, is_voltage=False,
                            decimals=2)
        self._build_cs_row(5)

        psu_group.setLayout(self.psu_layout)
        self.main_layout.addWidget(psu_group)
        self.main_layout.addStretch()

    def _build_psu_row(self, row, name, base_tag, pri_unit, sec_unit, min_v, max_v, is_voltage, decimals):
        lbl_name = QLabel(f"<b>{name}</b>")

        if name in ["Extraction", "Target", "Filament", "Thermionic"]:
            lbl_rb = QLabel(f"RB: --- {pri_unit} | --- {sec_unit} | --- W")
            lbl_rb.setMinimumWidth(250)
        else:
            lbl_rb = QLabel(f"RB: --- {pri_unit} | --- {sec_unit}")
            lbl_rb.setMinimumWidth(180)

        lbl_badge = QLabel("")
        lbl_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_badge.setFixedWidth(100)
        lbl_badge.hide()

        btn_enable = QPushButton("ENABLE")
        btn_enable.setCheckable(True)
        btn_enable.setStyleSheet(COLOR_BUTTON_STANDARD)
        # Switching ON needs a confirming second click; OFF is immediate
        arm = ArmedToggle(btn_enable, lambda on, t=base_tag: self._dispatch_command(f"{t}.cmd_enable", on),
                          what=name.upper())

        # Sends only what the operator types or steps (setpoint_box.py): focus passing through, the
        # window losing focus or the box being locked send nothing
        sp_box = SetpointBox()
        sp_box.setObjectName(f"{name} setpoint")
        sp_box.setRange(min_v, max_v)
        sp_box.setSuffix(f" {pri_unit}")
        sp_box.setDecimals(decimals)

        if decimals >= 3:
            sp_box.setSingleStep(0.001)
        elif decimals == 2:
            sp_box.setSingleStep(0.01)
        else:
            sp_box.setSingleStep(0.1)

        sp_tag = f"{base_tag}.sp_requested_{'voltage' if is_voltage else 'current'}"
        self._apply_limits(sp_box, sp_tag)
        # (The einzel's current limit is the Spellman service's job now: it sets one when the
        # supply holds none, whoever sets the voltage - this panel used to send 50 uA with each.)
        sp_box.committed.connect(lambda value, t=sp_tag: self._dispatch_command(t, value))

        self.psu_layout.addWidget(lbl_name, row, 0)
        self.psu_layout.addWidget(lbl_rb, row, 1)
        self.psu_layout.addWidget(sp_box, row, 2)
        self.psu_layout.addWidget(lbl_badge, row, 3)
        self.psu_layout.addWidget(btn_enable, row, 4)

        self.psu_controls[name] = {
            "lbl_rb": lbl_rb, "sp_box": sp_box, "btn_enable": btn_enable, "arm": arm,
            "lbl_badge": lbl_badge, "base_tag": base_tag,
            "pri_unit": pri_unit, "sec_unit": sec_unit, "is_voltage": is_voltage,
            "decimals": decimals
        }

        if name == "Thermionic":
            btn_auto = QPushButton("AUTO EMISSION")
            btn_auto.setCheckable(True)
            btn_auto.setStyleSheet(COLOR_BUTTON_STANDARD)
            btn_auto.clicked.connect(
                lambda *args, b=btn_auto, t=base_tag: self._dispatch_command(f"{t}.cmd_auto_emission", b.isChecked()))
            self.psu_layout.addWidget(btn_auto, row, 5)
            self.psu_controls[name]["btn_auto"] = btn_auto

    def _build_cs_row(self, row):
        lbl_name = QLabel("<b>Cesium Oven</b>")
        lbl_rb = QLabel("RB: --- °C")
        lbl_rb.setMinimumWidth(180)

        lbl_badge = QLabel("")
        lbl_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_badge.setFixedWidth(100)
        lbl_badge.hide()

        btn_enable = QPushButton("HEATER ON")
        btn_enable.setCheckable(True)
        btn_enable.setStyleSheet(COLOR_BUTTON_STANDARD)
        arm = ArmedToggle(btn_enable, lambda on: self._dispatch_command("ion_beam.source.cesium.cmd_ctrl_mode",
                                                                        1 if on else 0), what="HEATER")

        btn_cool = QPushButton("FORCED COOLING")
        btn_cool.setCheckable(True)
        btn_cool.setStyleSheet(COLOR_BUTTON_STANDARD)
        btn_cool.clicked.connect(
            lambda *args, b=btn_cool: self._dispatch_command("ion_beam.source.cesium.cmd_force_cooling", b.isChecked()))

        sp_box = SetpointBox()
        sp_box.setObjectName("Cesium setpoint")
        sp_box.setRange(0.0, 200.0)
        self._apply_limits(sp_box, "ion_beam.source.cesium.sp_requested_temp")
        sp_box.setSuffix(" °C")
        sp_box.committed.connect(lambda value: self._dispatch_command("ion_beam.source.cesium.sp_requested_temp", value))

        self.psu_layout.addWidget(lbl_name, row, 0)
        self.psu_layout.addWidget(lbl_rb, row, 1)
        self.psu_layout.addWidget(sp_box, row, 2)
        self.psu_layout.addWidget(lbl_badge, row, 3)
        self.psu_layout.addWidget(btn_enable, row, 4)
        self.psu_layout.addWidget(btn_cool, row, 5)

        self.psu_controls["Cesium"] = {
            "lbl_rb": lbl_rb, "sp_box": sp_box, "btn_enable": btn_enable, "arm": arm,
            "btn_cool": btn_cool, "lbl_badge": lbl_badge,
            "base_tag": "ion_beam.source.cesium", "is_voltage": False
        }

    def update_telemetry(self, data: dict):
        try:
            # Recompute lockout conditions based on raw telemetry
            sys_connected = bool(data.get("system.connected", False))
            pc_plc_lost = bool(data.get("ion_beam.system.pc_plc_comms_lost", False))
            master_comms_lost = not sys_connected or pc_plc_lost

            relay_active = bool(data.get("ion_beam.facilities.safety_relay_active", False))

            w0_raw = data.get("ion_beam.faults.word_0_system")
            w0_faults = self.fault_engine.get_active_faults("UDT_Fault_Word_0_System", int(w0_raw)) if w0_raw else []
            remote_lost = any(f["name"] == "Remote_IO_Lost" for f in w0_faults) or bool(
                data.get("ion_beam.system.remote_io_lost", 0.0))

            auto_sdown = bool(data.get("ion_beam.system.src_auto_sdown", 0.0))
            active_step = max(int(data.get("ion_beam.system.remote_shutdown_step", 0.0)),
                              int(data.get("ion_beam.system.pc_shutdown_step", 0.0)))

            spellman_state = data.get("manager.services", {}).get("service_spellman_mpd", "OFFLINE")
            spellman_comms_lost = (spellman_state != "ONLINE")

            self._update_power_supplies(data, relay_active, remote_lost, auto_sdown, active_step, master_comms_lost,
                                        spellman_comms_lost)

        except Exception:  # never let one bad value freeze the whole panel; the cause goes to the log
            log.exception("Ion source panel update failed")

    def _update_power_supplies(self, data: dict, relay_active: bool, remote_lost: bool, auto_sdown: bool,
                               active_step: int, master_comms_lost: bool, spellman_comms_lost: bool):
        # Auto-emission: the PLC drives the filament current to hold the thermionic setpoint
        auto_emission = data.get("ion_beam.source.thermionic.stat_auto_emission") == 1.0
        for name, ctrl in self.psu_controls.items():
            base_tag = ctrl["base_tag"]
            pri_unit = ctrl.get("pri_unit", "")
            sec_unit = ctrl.get("sec_unit", "")
            sp_box = ctrl["sp_box"]
            lbl_badge = ctrl["lbl_badge"]
            is_spellman = "einzel" in base_tag

            # The owning service says whether it would take an operator command now (PLC: comms,
            # relay, remote I/O, shutdown, PLC control mode, interlock; Spellman: relay, bus,
            # supply; either: a sequence holding it)
            comp_lock_reason = permit_reason(data, base_tag, "Spellman" if is_spellman else "PLC")
            ctrl_mode = int(data.get(f"{base_tag}.ctrl_mode") or 0)      # the PLC's own control mode
            held_by = data.get(f"{base_tag}.lease_purpose")             # a sequence holding it
            comp_lockout = bool(comp_lock_reason)

            # A. Readbacks
            is_stale = master_comms_lost or (is_spellman and spellman_comms_lost)
            stale_str = " [?]" if is_stale else ""

            if name == "Cesium":
                rb_val = data.get(f"{base_tag}.rb_temp")
                est_val = data.get(f"{base_tag}.estimated_temp")
                duty_cycle = data.get(f"{base_tag}.out_cv_heating")

                if duty_cycle is not None:
                    if not relay_active or remote_lost:
                        temp_str = f"~{est_val:.1f}" if est_val is not None else "---"
                    else:
                        temp_str = f"{rb_val:.1f}" if rb_val is not None else "---"

                    ctrl["lbl_rb"].setText(f"RB: {temp_str} °C (H: {duty_cycle:.0f}%){stale_str}")
                else:
                    ctrl["lbl_rb"].setText("RB: --- °C")
                rb_tags = (f"{base_tag}.rb_temp",)
            else:
                rb_v = data.get(f"{base_tag}.rb_voltage")
                rb_i = data.get(f"{base_tag}.rb_current")
                rb_p = data.get(f"{base_tag}.rb_power")

                pri_decimals = ctrl.get("decimals", 2)
                sec_decimals = 1 if sec_unit == "µA" else 2
                p_str = f" | {rb_p:.1f} W" if rb_p is not None else ""

                if ctrl["is_voltage"]:
                    v_str = f"{rb_v:.{pri_decimals}f}" if rb_v is not None else "---"
                    i_str = f"{rb_i:.{sec_decimals}f}" if rb_i is not None else "---"
                    ctrl["lbl_rb"].setText(f"RB: {v_str} {pri_unit} | {i_str} {sec_unit}{p_str}{stale_str}")
                else:
                    i_str = f"{rb_i:.{pri_decimals}f}" if rb_i is not None else "---"
                    v_str = f"{rb_v:.{sec_decimals}f}" if rb_v is not None else "---"
                    ctrl["lbl_rb"].setText(f"RB: {i_str} {pri_unit} | {v_str} {sec_unit}{p_str}{stale_str}")
                rb_tags = (f"{base_tag}.rb_voltage", f"{base_tag}.rb_current")

            # Readback-vs-setpoint tolerance (information only, from service_monitor)
            if tolerance_flagged(data, rb_tags):
                ctrl["lbl_rb"].setStyleSheet(TOLERANCE_STYLE)
                ctrl["lbl_rb"].setToolTip(TOLERANCE_TIP)
            elif ctrl["lbl_rb"].styleSheet():
                ctrl["lbl_rb"].setStyleSheet("")
                ctrl["lbl_rb"].setToolTip("")

            # B. Active Setpoints (shown unless the operator is typing a new one)
            sp_tag = "sp_actual_temp" if name == "Cesium" else (
                "sp_actual_voltage" if ctrl["is_voltage"] else "sp_actual_current")
            sp_box.show_setpoint(data.get(f"{base_tag}.{sp_tag}"))

            # C. Hardware State
            hw_lockout = bool(data.get(f"{base_tag}.stat_lockout", 0.0))

            if name == "Cesium":
                stat_en = data.get(f"{base_tag}.stat_enabled")
                ctrl["btn_enable"].setEnabled(not comp_lockout)
                ctrl["arm"].show_state(None if stat_en is None else bool(stat_en), COLOR_OK, COLOR_BUTTON_STANDARD,
                                       "State unknown: no current data from the PLC.")
                ctrl["btn_enable"].setToolTip(
                    f"Disabled: {comp_lock_reason}" if comp_lockout else "Click to toggle Cesium Heater PID control.")

                stat_cool = bool(data.get(f"{base_tag}.stat_cooling_active", False))
                stat_wait = bool(data.get(f"{base_tag}.stat_cooling_wait", False))
                ctrl["btn_cool"].setEnabled(not master_comms_lost)
                ctrl["btn_cool"].setToolTip(
                    "Disabled: GUI to PLC communications are offline." if master_comms_lost else "Click to manually force the cooling air solenoid open.")

                if stat_cool:
                    ctrl["btn_cool"].setChecked(True)
                    ctrl["btn_cool"].setText("COOLING ACTIVE")
                    ctrl["btn_cool"].setStyleSheet("background-color: #03A9F4; color: white;")
                elif stat_wait:
                    ctrl["btn_cool"].setChecked(True)
                    ctrl["btn_cool"].setText("COMPRESSOR RESTING")
                    ctrl["btn_cool"].setStyleSheet("background-color: #FF9800; color: black; font-weight: bold;")
                else:
                    ctrl["btn_cool"].setChecked(False)
                    ctrl["btn_cool"].setText("FORCED COOLING")
                    ctrl["btn_cool"].setStyleSheet(COLOR_BUTTON_STANDARD)

            else:
                if "btn_enable" in ctrl:
                    stat_en = data.get(f"{base_tag}.stat_enabled")
                    stat_disarming = bool(data.get(f"{base_tag}.stat_disarming", False))

                    if name == "Filament" and stat_disarming:
                        ctrl["arm"].disarm()
                        ctrl["btn_enable"].blockSignals(True)
                        ctrl["btn_enable"].setChecked(True)
                        ctrl["btn_enable"].blockSignals(False)

                        ctrl["btn_enable"].setText("DISARMING...")
                        ctrl["btn_enable"].setStyleSheet(
                            "background-color: #FF9800; color: black; font-weight: bold; border-radius: 4px;")
                        ctrl["btn_enable"].setEnabled(False)
                        ctrl["btn_enable"].setToolTip(
                            "Contactor remains closed while safe step-down ramp drops current to 0A.")
                    else:
                        ctrl["btn_enable"].setEnabled(not comp_lockout)
                        ctrl["btn_enable"].setToolTip(
                            f"Disabled: {comp_lock_reason}" if comp_lockout
                            else f"Click twice to switch {name} ON (confirm); once to switch it OFF.")
                        # None = unknown (service down, supply not answering): shown as unknown,
                        # never as the last known state
                        ctrl["arm"].show_state(None if stat_en is None else bool(stat_en), COLOR_OK,
                                               COLOR_BUTTON_STANDARD,
                                               f"{name} output state unknown - no current data from its service.")

                if name == "Thermionic" and "btn_auto" in ctrl:
                    auto_em_stat = data.get(f"{base_tag}.stat_auto_emission")

                    if auto_em_stat is not None:
                        is_auto = bool(auto_em_stat)

                        ctrl["btn_auto"].blockSignals(True)
                        ctrl["btn_auto"].setChecked(is_auto)

                        if is_auto:
                            ctrl["btn_auto"].setText("AUTO EMISSION: ON")
                            ctrl["btn_auto"].setStyleSheet(COLOR_OK)
                            tt = f"Disabled: {comp_lock_reason}" if comp_lockout else "Auto-Emission ACTIVE. Click to revert to Manual Filament Control."
                        else:
                            ctrl["btn_auto"].setText("AUTO EMISSION")
                            ctrl["btn_auto"].setStyleSheet(COLOR_BUTTON_STANDARD)
                            tt = f"Disabled: {comp_lock_reason}" if comp_lockout else "Click to engage closed-loop Auto-Emission control (Cascades Filament output)."

                        ctrl["btn_auto"].setToolTip(tt)
                        ctrl["btn_auto"].blockSignals(False)

                    ctrl["btn_auto"].setEnabled(not comp_lockout)
                    if comp_lockout and auto_em_stat is None:
                        ctrl["btn_auto"].setToolTip(f"Disabled: {comp_lock_reason}")

            # D. Control Authority Badges
            if hw_lockout:
                sp_box.setEnabled(False)
                sp_box.setToolTip("Disabled: Hardware interlock or fault is active.")
                lbl_badge.setText("🔒 LOCKED")
                lbl_badge.setStyleSheet(COLOR_FAULT)
                lbl_badge.show()
            elif name == "Filament" and auto_emission:
                sp_box.setEnabled(False)
                sp_box.setToolTip("Disabled: auto-emission is on - the PLC sets the filament current to hold "
                                  "the thermionic emission setpoint.")
                lbl_badge.setText("⚙️ AUTO-EMIS")
                lbl_badge.setStyleSheet("background-color: #2196F3; color: white; border-radius: 3px;")
                lbl_badge.show()
            elif held_by:
                sp_box.setEnabled(False)
                sp_box.setToolTip(f"Disabled: {comp_lock_reason}")
                lbl_badge.setText("🤖 SEQUENCE")
                lbl_badge.setToolTip(f"Held by {held_by} (stop it, or take control in the Automation bar)")
                lbl_badge.setStyleSheet("background-color: #9C27B0; color: white; border-radius: 3px;")
                lbl_badge.show()
            elif ctrl_mode == 1:
                if name == "Cesium":
                    sp_box.setEnabled(not comp_lockout)
                    sp_box.setToolTip(f"Disabled: {comp_lock_reason}" if comp_lockout else "")
                    lbl_badge.setText("🔥 HEATING")
                    lbl_badge.setStyleSheet("background-color: #FF5722; color: white; border-radius: 3px;")
                    lbl_badge.show()
                else:
                    sp_box.setEnabled(False)
                    sp_box.setToolTip("Disabled: Control yielded to automated sequencer.")
                    lbl_badge.setText("⚙️ AUTO")
                    lbl_badge.setStyleSheet("background-color: #2196F3; color: white; border-radius: 3px;")
                    lbl_badge.show()
            elif ctrl_mode == 2:
                sp_box.setEnabled(False)
                sp_box.setToolTip("Disabled: Control yielded to active Recipe.")
                lbl_badge.setText("🤖 RECIPE")
                lbl_badge.setStyleSheet("background-color: #9C27B0; color: white; border-radius: 3px;")
                lbl_badge.show()
            elif ctrl_mode == 3:
                sp_box.setEnabled(False)
                sp_box.setToolTip("Disabled: Target is dynamically cascaded from a master PID loop.")
                lbl_badge.setText("🔗 CASCADED")
                lbl_badge.setStyleSheet("background-color: #4CAF50; color: white; border-radius: 3px;")
                lbl_badge.show()
            elif ctrl_mode == 4:
                sp_box.setEnabled(False)
                sp_box.setToolTip("Disabled: Output temporarily held by Arc Quench algorithm.")
                lbl_badge.setText("⚡ ARC QUENCH")
                lbl_badge.setStyleSheet(COLOR_WARNING)
                lbl_badge.show()
            else:
                sp_box.setEnabled(not comp_lockout)
                sp_box.setToolTip(f"Disabled: {comp_lock_reason}" if comp_lockout else sp_box.property("cap_note") or "")
                lbl_badge.hide()