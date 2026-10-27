import json
import logging

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QGridLayout, QLabel,
    QPushButton, QGroupBox
)

from src.core import hard_caps, paths
from src.core.tolerance import FLAG_PREFIX
from src.gui.control.permits import permit_reason
from src.gui.control.arming import ArmedToggle
from src.gui.control.setpoint_box import SetpointBox
from src.gui.style import chip, reserve_width, set_prop, set_role, set_tone

log = logging.getLogger("hmi.source")

BODY_TRIP_TEXT = "PLC trips above 40 °C"
TOLERANCE_TIP = ("Readback has been outside its tolerance of the setpoint (not a fault).\n"
                 "Rules and tolerances: config/readback_tolerance.json; events are in events.db (TOLERANCE).")


def tolerance_flagged(data: dict, tags) -> bool:
    return any(data.get(FLAG_PREFIX + t) == 1.0 for t in tags)


def show_tolerance(label, flagged: bool, normal_tip: str = ""):
    """An amber outline on a readback that has been outside its tolerance (information, not a fault)."""
    if set_prop(label, "tolerance", "out" if flagged else None):
        label.setToolTip(TOLERANCE_TIP if flagged else normal_tip)


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


def _readback_labels(primary_sample: str, secondary_sample: str):
    """The main readback (what the setpoint controls) and a smaller second line (the other values)."""
    lbl_rb = QLabel()
    set_role(lbl_rb, "readout")
    reserve_width(lbl_rb, primary_sample)
    lbl_rb2 = QLabel()
    set_role(lbl_rb2, "readout-sub")
    reserve_width(lbl_rb2, secondary_sample)
    return lbl_rb, lbl_rb2


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
        psu_group = QGroupBox("Source power supplies")
        self.psu_layout = QGridLayout()
        self.psu_layout.setHorizontalSpacing(10)
        self.psu_layout.setVerticalSpacing(2)
        self.psu_controls = {}
        for col, text in enumerate(("Supply", "Readback", "Setpoint", "Mode", "Output")):
            head = QLabel(text)
            set_role(head, "caption")
            self.psu_layout.addWidget(head, 0, col)

        # Each supply takes three grid rows: the main readback with the controls, the readback's
        # second line (and a supply's second button), then a gap before the next supply
        self._build_psu_row(1, "Source Einzel", "ion_beam.source.einzel", "kV", "µA", 0.0, 30.0, is_voltage=True,
                            decimals=3)
        self._build_psu_row(4, "Extraction", "ion_beam.source.extraction", "kV", "mA", 0.0, 20.0, is_voltage=True,
                            decimals=3)
        self._build_psu_row(7, "Target", "ion_beam.source.target", "kV", "mA", 0.0, 10.0, is_voltage=True, decimals=3)
        self._build_psu_row(10, "Filament", "ion_beam.source.filament", "A", "V", 0.0, 38.0, is_voltage=False,
                            decimals=3)
        self._build_psu_row(13, "Thermionic", "ion_beam.source.thermionic", "mA", "V", 0.0, 1000.0, is_voltage=False,
                            decimals=2)
        self._build_cs_row(16)
        self._build_body_row(19)
        for gap_row in (3, 6, 9, 12, 15, 18):
            self.psu_layout.setRowMinimumHeight(gap_row, 10)
        # The mode column keeps its width when no badge shows, so the buttons don't move
        probe = chip("ARC QUENCH", "warn")
        probe.ensurePolished()
        self.psu_layout.setColumnMinimumWidth(3, probe.sizeHint().width())
        probe.deleteLater()
        self.psu_layout.setColumnStretch(5, 1)          # spare width goes to the right

        psu_group.setLayout(self.psu_layout)
        self.main_layout.addWidget(psu_group)
        self.main_layout.addStretch()

    def _build_psu_row(self, row, name, base_tag, pri_unit, sec_unit, min_v, max_v, is_voltage, decimals):
        lbl_name = QLabel(name)
        set_role(lbl_name, "title")

        has_power = name in ["Extraction", "Target", "Filament", "Thermionic"]
        lbl_rb, lbl_rb2 = _readback_labels(f"0000.000 {pri_unit} [?]",
                                           f"0000.00 {sec_unit} | 0000.0 W" if has_power else f"0000.0 {sec_unit}")
        lbl_rb.setText(f"--- {pri_unit}")
        lbl_rb2.setText(f"--- {sec_unit} | --- W" if has_power else f"--- {sec_unit}")

        lbl_badge = chip("", "info")
        lbl_badge.hide()

        btn_enable = QPushButton("ENABLE")
        btn_enable.setCheckable(True)
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

        if name == "Filament":
            sp_box.setSingleStep(0.01)      # (1 mA a click went nowhere: 28.509 A after nine, Oct 2026)
        elif decimals >= 3:
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
        sp_box.why_not_taken = lambda n=name: self._why_not_taken(n)

        self.psu_controls[name] = {
            "lbl_rb": lbl_rb, "lbl_rb2": lbl_rb2, "sp_box": sp_box, "btn_enable": btn_enable, "arm": arm,
            "lbl_badge": lbl_badge, "base_tag": base_tag,
            "pri_unit": pri_unit, "sec_unit": sec_unit, "is_voltage": is_voltage,
            "decimals": decimals
        }

        self._place(row, lbl_name, lbl_rb, lbl_rb2, sp_box, lbl_badge, btn_enable)
        if name == "Thermionic":
            btn_auto = QPushButton("AUTO EMISSION")
            btn_auto.setCheckable(True)
            btn_auto.clicked.connect(
                lambda *args, b=btn_auto, t=base_tag: self._dispatch_command(f"{t}.cmd_auto_emission", b.isChecked()))
            self.psu_layout.addWidget(btn_auto, row + 1, 4)
            self.psu_controls[name]["btn_auto"] = btn_auto

    def _place(self, row, lbl_name, lbl_rb, lbl_rb2, sp_box, lbl_badge, btn_enable):
        middle = Qt.AlignmentFlag.AlignVCenter
        self.psu_layout.addWidget(lbl_name, row, 0, middle)
        self.psu_layout.addWidget(lbl_rb, row, 1, middle)
        self.psu_layout.addWidget(lbl_rb2, row + 1, 1, Qt.AlignmentFlag.AlignTop)
        self.psu_layout.addWidget(sp_box, row, 2)
        self.psu_layout.addWidget(lbl_badge, row, 3, middle)
        self.psu_layout.addWidget(btn_enable, row, 4)

    def _build_body_row(self, row):
        """The source body's temperature, read only: the start-up checks look for it between 10 degC
        and room temperature once the relay is on, and the PLC trips the source above 40 degC."""
        lbl_name = QLabel("Source body")
        set_role(lbl_name, "title")
        lbl_rb, lbl_rb2 = _readback_labels("~000.0 °C [?]", BODY_TRIP_TEXT)
        lbl_rb.setText("--- °C")
        lbl_rb2.setText(BODY_TRIP_TEXT)
        lbl_rb.setToolTip("Source body temperature, read by the PLC. The source deck's readings are live only "
                          "while the safety relay is on, so it shows --- while the relay is off.")
        middle = Qt.AlignmentFlag.AlignVCenter
        self.psu_layout.addWidget(lbl_name, row, 0, middle)
        self.psu_layout.addWidget(lbl_rb, row, 1, middle)
        self.psu_layout.addWidget(lbl_rb2, row + 1, 1, Qt.AlignmentFlag.AlignTop)
        self.lbl_body = lbl_rb

    def _build_cs_row(self, row):
        lbl_name = QLabel("Caesium oven")
        set_role(lbl_name, "title")
        lbl_rb, lbl_rb2 = _readback_labels("~000.0 °C [?]", "heater 100 % | cooling air on")
        lbl_rb.setText("--- °C")
        lbl_rb2.setText("heater --- %")

        lbl_badge = chip("", "info")
        lbl_badge.hide()

        btn_enable = QPushButton("HEATER ON")
        btn_enable.setCheckable(True)
        arm = ArmedToggle(btn_enable, lambda on: self._dispatch_command("ion_beam.source.cesium.cmd_ctrl_mode",
                                                                        1 if on else 0), what="HEATER")

        btn_cool = QPushButton("FORCED COOLING")
        btn_cool.setCheckable(True)
        btn_cool.clicked.connect(
            lambda *args, b=btn_cool: self._dispatch_command("ion_beam.source.cesium.cmd_force_cooling", b.isChecked()))

        sp_box = SetpointBox()
        sp_box.setObjectName("Caesium setpoint")
        sp_box.setRange(0.0, 200.0)
        self._apply_limits(sp_box, "ion_beam.source.cesium.sp_requested_temp")
        sp_box.setSuffix(" °C")
        sp_box.committed.connect(lambda value: self._dispatch_command("ion_beam.source.cesium.sp_requested_temp", value))

        self._place(row, lbl_name, lbl_rb, lbl_rb2, sp_box, lbl_badge, btn_enable)
        self.psu_layout.addWidget(btn_cool, row + 1, 4)

        self.psu_controls["Cesium"] = {
            "lbl_rb": lbl_rb, "lbl_rb2": lbl_rb2, "sp_box": sp_box, "btn_enable": btn_enable, "arm": arm,
            "btn_cool": btn_cool, "lbl_badge": lbl_badge,
            "base_tag": "ion_beam.source.cesium", "is_voltage": False
        }

    def _show_cooling(self, ctrl, data, base_tag, comms_lost, relay_active, auto_sdown):
        """FORCED COOLING shows YOUR request (the PC's cmd_force_cooling, as the PLC holds it); whether
        the cooling air is actually on is on the oven's second line ("cooling air on"), with why.
        The button used to show the air valve: with the relay off the PLC always opens it, so
        switching forced cooling off seemed to do nothing (it read COOLING ACTIVE again)."""
        btn = ctrl["btn_cool"]
        air_on = bool(data.get(f"{base_tag}.stat_cooling_active", False))
        resting = bool(data.get(f"{base_tag}.stat_cooling_wait", False))
        asked = data.get(f"{base_tag}.cmd_force_cooling")
        asked = btn.isChecked() if asked is None else bool(asked)
        btn.setEnabled(not comms_lost)
        btn.setChecked(asked)
        if asked and resting:
            btn.setText("COMPRESSOR RESTING")
            set_tone(btn, "warn")
        elif asked:
            btn.setText("FORCED COOLING: ON")
            set_tone(btn, "info")
        else:
            btn.setText("FORCED COOLING")
            set_tone(btn, None)
        if not air_on:
            why = "The cooling air is off."
        elif asked:
            why = "The cooling air is on: you asked for forced cooling."
        elif not relay_active:
            why = ("The cooling air is on because the safety relay is off: the PLC opens it whenever the relay "
                   "is off, whatever is asked here.")
        elif auto_sdown:
            why = "The cooling air is on: a shutdown sequence is cooling the oven."
        else:
            why = ("The cooling air is on: the PLC is cooling the oven itself (an over-temperature, a cooling "
                   "fault, or the relay or the heating lost while the oven was hot).")
        ctrl["lbl_rb2"].setToolTip(why)
        if comms_lost:
            btn.setToolTip("Disabled: GUI to PLC communications are offline.")
        elif asked:
            btn.setToolTip(f"Forced cooling is on at your request: click to stop asking for it. {why}")
        else:
            btn.setToolTip(f"Click to ask the PLC to open the oven's cooling air (forced cooling). {why}")

    def _why_not_taken(self, name: str) -> str:
        """Why a setpoint sent from a supply's box may not have been taken (setpoint_box.py), from the
        latest telemetry: the likeliest reason, or '' if none shows."""
        data = getattr(self, "_data", None) or {}
        base = self.psu_controls.get(name, {}).get("base_tag", "")
        if name == "Filament" and data.get("ion_beam.source.thermionic.stat_auto_emission") == 1.0:
            return "auto-emission is on: the PLC sets the filament current itself - switch AUTO EMISSION off first"
        if base and "einzel" not in base and data.get(f"{base}.stat_enabled") == 0.0:
            return (f"the {name.lower()} supply is off, and the PLC zeroes the setpoint of a supply that is off - "
                    f"switch it on, then set it")
        mode = data.get(f"{base}.ctrl_mode") if base else None
        if mode:
            return f"the PLC reports control mode {int(mode)} for it: it is driving it itself"
        return ""

    def update_telemetry(self, data: dict):
        self._data = data
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
            body = data.get("ion_beam.source.chamber.rb_body_temp")
            if body is None or not relay_active or remote_lost:
                self.lbl_body.setText("--- °C")
            else:
                self.lbl_body.setText(f"{body:.1f} °C" + (" [?]" if master_comms_lost else ""))

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

                    air = " | cooling air on" if data.get(f"{base_tag}.stat_cooling_active") else ""
                    ctrl["lbl_rb"].setText(f"{temp_str} °C{stale_str}")
                    ctrl["lbl_rb2"].setText(f"heater {duty_cycle:.0f} %{air}")
                else:
                    ctrl["lbl_rb"].setText("--- °C")
                    ctrl["lbl_rb2"].setText("heater --- %")
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
                    ctrl["lbl_rb"].setText(f"{v_str} {pri_unit}{stale_str}")
                    ctrl["lbl_rb2"].setText(f"{i_str} {sec_unit}{p_str}")
                else:
                    i_str = f"{rb_i:.{pri_decimals}f}" if rb_i is not None else "---"
                    v_str = f"{rb_v:.{sec_decimals}f}" if rb_v is not None else "---"
                    ctrl["lbl_rb"].setText(f"{i_str} {pri_unit}{stale_str}")
                    ctrl["lbl_rb2"].setText(f"{v_str} {sec_unit}{p_str}")
                rb_tags = (f"{base_tag}.rb_voltage", f"{base_tag}.rb_current")

            # Readback-vs-setpoint tolerance (information only, from service_monitor)
            show_tolerance(ctrl["lbl_rb"], tolerance_flagged(data, rb_tags))

            # B. Active Setpoints (shown unless the operator is typing a new one)
            sp_tag = "sp_actual_temp" if name == "Cesium" else (
                "sp_actual_voltage" if ctrl["is_voltage"] else "sp_actual_current")
            sp_box.show_setpoint(data.get(f"{base_tag}.{sp_tag}"))

            # C. Hardware State
            hw_lockout = bool(data.get(f"{base_tag}.stat_lockout", 0.0))

            if name == "Cesium":
                stat_en = data.get(f"{base_tag}.stat_enabled")
                ctrl["btn_enable"].setEnabled(not comp_lockout)
                ctrl["arm"].show_state(None if stat_en is None else bool(stat_en),
                                       "State unknown: no current data from the PLC.")
                ctrl["btn_enable"].setToolTip(
                    f"Disabled: {comp_lock_reason}" if comp_lockout else "Click to toggle Caesium Heater PID control.")

                self._show_cooling(ctrl, data, base_tag, master_comms_lost, relay_active, auto_sdown)

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
                        set_tone(ctrl["btn_enable"], "warn")
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
                        ctrl["arm"].show_state(None if stat_en is None else bool(stat_en),
                                               f"{name} output state unknown - no current data from its service.")

                if name == "Thermionic" and "btn_auto" in ctrl:
                    auto_em_stat = data.get(f"{base_tag}.stat_auto_emission")

                    if auto_em_stat is not None:
                        is_auto = bool(auto_em_stat)

                        ctrl["btn_auto"].blockSignals(True)
                        ctrl["btn_auto"].setChecked(is_auto)

                        if is_auto:
                            ctrl["btn_auto"].setText("AUTO EMISSION: ON")
                            set_tone(ctrl["btn_auto"], "info")
                            tt = f"Disabled: {comp_lock_reason}" if comp_lockout else "Auto-Emission ACTIVE. Click to revert to Manual Filament Control."
                        else:
                            ctrl["btn_auto"].setText("AUTO EMISSION")
                            set_tone(ctrl["btn_auto"], None)
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
                self._badge(lbl_badge, "LOCKED", "fault")
            elif name == "Filament" and auto_emission:
                sp_box.setEnabled(False)
                sp_box.setToolTip("Disabled: auto-emission is on - the PLC sets the filament current to hold "
                                  "the thermionic emission setpoint.")
                self._badge(lbl_badge, "AUTO-EMIS", "info")
            elif held_by:
                sp_box.setEnabled(False)
                sp_box.setToolTip(f"Disabled: {comp_lock_reason}")
                self._badge(lbl_badge, "SEQUENCE", "seq",
                            f"Held by {held_by} (stop it, or take control in the Automation bar)")
            elif ctrl_mode == 1:
                if name == "Cesium":
                    sp_box.setEnabled(not comp_lockout)
                    sp_box.setToolTip(f"Disabled: {comp_lock_reason}" if comp_lockout else "")
                    self._badge(lbl_badge, "HEATING", "on", "The PLC's PID is holding the oven temperature.")
                else:
                    sp_box.setEnabled(False)
                    sp_box.setToolTip("Disabled: Control yielded to automated sequencer.")
                    self._badge(lbl_badge, "AUTO", "info")
            elif ctrl_mode == 2:
                sp_box.setEnabled(False)
                sp_box.setToolTip("Disabled: Control yielded to active Recipe.")
                self._badge(lbl_badge, "RECIPE", "seq")
            elif ctrl_mode == 3:
                sp_box.setEnabled(False)
                sp_box.setToolTip("Disabled: Target is dynamically cascaded from a master PID loop.")
                self._badge(lbl_badge, "CASCADED", "info")
            elif ctrl_mode == 4:
                sp_box.setEnabled(False)
                sp_box.setToolTip("Disabled: Output temporarily held by Arc Quench algorithm.")
                self._badge(lbl_badge, "ARC QUENCH", "warn")
            else:
                sp_box.setEnabled(not comp_lockout)
                sp_box.setToolTip(f"Disabled: {comp_lock_reason}" if comp_lockout else sp_box.property("cap_note") or "")
                if not lbl_badge.isHidden():
                    lbl_badge.hide()

    @staticmethod
    def _badge(lbl, text: str, tone: str, tip: str = ""):
        """The control-mode badge beside a supply: who or what is driving it."""
        lbl.setText(text)
        set_tone(lbl, tone)
        lbl.setToolTip(tip)
        if lbl.isHidden():
            lbl.show()