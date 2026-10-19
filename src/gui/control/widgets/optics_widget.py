"""Beamline optics panel: analysing magnet, steerers, slits (logged by hand) and the downstream
Spellman lenses. Whether a control is available comes from the services' permits
(src/gui/control/permits.py); mass scans are in the Mass Scan panel (run by the sequencer)."""
import logging
import time

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QDoubleSpinBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QMessageBox, QPushButton,
                             QVBoxLayout, QWidget)

from src.core import hard_caps
from src.core.commands import route_for_tag
from src.core.mass_calibration import current_for_mass
from src.core.theme import COLOR_BUTTON_STANDARD, COLOR_FAULT, COLOR_INACTIVE, COLOR_OK
from src.gui.control.arming import ArmedAction, ArmedToggle
from src.gui.control.permits import permit_reason
from src.gui.control.setpoint_box import SetpointBox
from src.gui.control.widgets.source_widget import TOLERANCE_STYLE, TOLERANCE_TIP, setpoint_limits, tolerance_flagged

log = logging.getLogger("hmi.optics")


def _show_tolerance(label, data, tags):
    if tolerance_flagged(data, tags):
        label.setStyleSheet(TOLERANCE_STYLE)
        label.setToolTip(TOLERANCE_TIP)
    elif label.styleSheet():
        label.setStyleSheet("")
        label.setToolTip("")


class BeamlineOpticsWidget(QWidget):
    def __init__(self, cmd_thread, event_helper):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.event_helper = event_helper

        # Set by the main window: returns the Mass Scan panel's calibration {"k", "i_off", "source"}
        self.mass_calibration = None
        self._limited_tags = setpoint_limits()
        self._latest_telemetry = {}

        self.main_layout = QVBoxLayout(self)

        self._init_pre_magnet_ui()
        self._init_magnet_ui()
        self._init_post_magnet_ui()
        self._init_downstream_optics_ui()

        self.main_layout.addStretch()

    def _dispatch_command(self, tag: str, value):
        self.cmd_thread.send_command(route_for_tag(tag), tag, value)

    def _toggle_master_steering(self, state: bool):
        self._dispatch_command("ion_beam.beamline.steering.cmd_enable", state)
        self._dispatch_command("ion_beam.beamline.steerer_magnet.cmd_enable", state)

    def _log_manual_slit(self, position_name: str, value: float):
        """The slits are set by hand: their widths only go into the event log. (A command used to be
        sent as well, to ion_beam.beamline.slits.* - no such tag, so every one was refused.)"""
        self.event_helper.log_user_marker(time.time(), f"Manual Adjustment: {position_name} set to {value} mm",
                                          "#9C27B0")

    def _setpoint_box(self, name: str, send) -> SetpointBox:
        """A box that sends only what the operator types or steps (setpoint_box.py)."""
        box = SetpointBox()
        box.setObjectName(name)
        box.committed.connect(send)
        return box

    def _init_pre_magnet_ui(self):
        group = QGroupBox("1. Pre-Magnet Tuning")
        layout = QGridLayout()

        # Y-Steerer (Electrostatic)
        layout.addWidget(QLabel("<b>Y-Steerer Voltage:</b>"), 0, 0)

        self.lbl_y_rb = QLabel("RB: --- V")
        self.lbl_y_rb.setMinimumWidth(80)
        layout.addWidget(self.lbl_y_rb, 0, 1)

        self.sp_y_steer = self._setpoint_box(
            "Y-steerer setpoint", lambda v: self._dispatch_command("ion_beam.beamline.steering.sp_requested_y_volts", v))
        self.sp_y_steer.setRange(*self._limited_tags.get("ion_beam.beamline.steering.sp_requested_y_volts",
                                                         (-200.0, 200.0)))
        self.sp_y_steer.setSuffix(" V")
        self.sp_y_steer.setDecimals(1)
        layout.addWidget(self.sp_y_steer, 0, 2)

        # Master Steering Enable (Applies to both X and Y)
        self.btn_steer_en = QPushButton("ENABLE STEERING")
        self.btn_steer_en.setCheckable(True)
        self.btn_steer_en.setStyleSheet(COLOR_BUTTON_STANDARD)
        self.arm_steer = ArmedToggle(self.btn_steer_en, self._toggle_master_steering, what="STEERING")
        # Span the button across rows 0 and 1
        layout.addWidget(self.btn_steer_en, 0, 3, 2, 1)

        # X-Steerer (Magnetic)
        layout.addWidget(QLabel("<b>X-Steerer Current:</b>"), 1, 0)

        self.lbl_x_rb = QLabel("RB: --- A | --- V")
        self.lbl_x_rb.setMinimumWidth(120)
        layout.addWidget(self.lbl_x_rb, 1, 1)

        self.sp_x_steer = self._setpoint_box(
            "X-steerer setpoint",
            lambda v: self._dispatch_command("ion_beam.beamline.steerer_magnet.sp_requested_current", v))
        self.sp_x_steer.setRange(*self._limited_tags.get("ion_beam.beamline.steerer_magnet.sp_requested_current",
                                                         (0.0, 80.0)))
        self.sp_x_steer.setSuffix(" A")
        self.sp_x_steer.setDecimals(2)
        self.sp_x_steer.setToolTip(hard_caps.note("ion_beam.beamline.steerer_magnet.sp_requested_current"))
        self.sp_x_steer.setSingleStep(0.1)
        layout.addWidget(self.sp_x_steer, 1, 2)

        # Object Slits
        layout.addWidget(QLabel("<b>Object Slits (L / R):</b>"), 2, 0)
        slit_layout = QHBoxLayout()

        self.sp_obj_l = self._setpoint_box("Object slit left", lambda v: self._log_manual_slit("Object Slit Left", v))
        self.sp_obj_l.setSuffix(" mm")

        self.sp_obj_r = self._setpoint_box("Object slit right", lambda v: self._log_manual_slit("Object Slit Right", v))
        self.sp_obj_r.setSuffix(" mm")

        slit_layout.addWidget(self.sp_obj_l)
        slit_layout.addWidget(self.sp_obj_r)
        layout.addLayout(slit_layout, 2, 1, 1, 3)

        group.setLayout(layout)
        self.main_layout.addWidget(group)

    def _init_magnet_ui(self):
        group = QGroupBox("2. Mass Analyzer Magnet")
        layout = QGridLayout()

        # --- Row 0: Cooling Status ---
        self.lbl_mag_cooling = QLabel("COOLING: UNKNOWN")
        self.lbl_mag_cooling.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_mag_cooling.setStyleSheet(COLOR_INACTIVE)
        layout.addWidget(self.lbl_mag_cooling, 0, 0, 1, 2)

        # --- Row 1: Main Magnet Controls ---
        layout.addWidget(QLabel("<b>Magnet Current:</b>"), 1, 0)

        self.lbl_mag_rb = QLabel("RB: --- A | --- V")
        layout.addWidget(self.lbl_mag_rb, 1, 1)

        self.sp_mag_current = self._setpoint_box(
            "Magnet setpoint", lambda v: self._dispatch_command("ion_beam.beamline.magnet.sp_requested_current", v))
        self.sp_mag_current.setRange(*self._limited_tags.get("ion_beam.beamline.magnet.sp_requested_current",
                                                             (0.0, 80.0)))
        self.sp_mag_current.setSuffix(" A")
        self.sp_mag_current.setDecimals(2)
        self.sp_mag_current.setToolTip(hard_caps.note("ion_beam.beamline.magnet.sp_requested_current"))
        self.sp_mag_current.setSingleStep(0.01)
        layout.addWidget(self.sp_mag_current, 1, 2)

        self.btn_mag_en = QPushButton("ENABLE MAGNET")
        self.btn_mag_en.setCheckable(True)
        self.btn_mag_en.setStyleSheet(COLOR_BUTTON_STANDARD)
        self.arm_mag = ArmedToggle(self.btn_mag_en,
                                   lambda on: self._dispatch_command("ion_beam.beamline.magnet.cmd_enable", on),
                                   what="MAGNET")
        layout.addWidget(self.btn_mag_en, 1, 3)

        self.btn_mag_deg = QPushButton("DEGAUSS ROUTINE")
        self.btn_mag_deg.setStyleSheet("background-color: #9C27B0; color: white; font-weight: bold;")
        # Degauss swings the magnet up to the configured amps: confirm first
        self.arm_deg = ArmedAction(self.btn_mag_deg,
                                   lambda: self._dispatch_command("ion_beam.beamline.magnet.cmd_degauss", True),
                                   what="DEGAUSS")
        layout.addWidget(self.btn_mag_deg, 1, 4)

        # --- Row 2: Auto-Tune Target ---
        layout.addWidget(QLabel("<b>Auto-Tune Target:</b>"), 2, 0)

        self.sp_mass = QDoubleSpinBox()
        self.sp_mass.setRange(1.0, 250.0)
        self.sp_mass.setDecimals(1)
        self.sp_mass.setSingleStep(1.0)
        self.sp_mass.setValue(28.0)
        self.sp_mass.setSuffix(" amu")
        self.sp_mass.setKeyboardTracking(False)
        layout.addWidget(self.sp_mass, 2, 2)

        self.btn_calc_mass = QPushButton("CALCULATE && SET AMPS")
        self.btn_calc_mass.setStyleSheet("background-color: #2196F3; color: white; font-weight: bold;")
        self.btn_calc_mass.clicked.connect(self._calculate_mass)
        layout.addWidget(self.btn_calc_mass, 2, 3, 1, 2)

        group.setLayout(layout)
        self.main_layout.addWidget(group)

    def _warn(self, title: str, text: str):
        QMessageBox.warning(self, title, text)

    def _calculate_mass(self):
        """Set the magnet for the target mass using the Mass Scan panel's calibration
        (AMU = k (I + I_off)^2 / E) and the live beam energy (target + extraction voltage),
        the same model and energy the mass scan uses.

        (This used a polynomial from magnet_config.json, which was never actually loaded - the
        path pointed at src/config - so it had always been amps = amu. Hysteresis still applies:
        a scan calibrates on the way up, so approach the current from below, or degauss first.)"""
        target_amu = self.sp_mass.value()
        cal = self.mass_calibration() if callable(self.mass_calibration) else None
        if not cal:
            self._warn("No mass calibration", "The Mass Scan panel's calibration is not available.")
            return
        data = self._latest_telemetry or {}
        tgt_kv = data.get("ion_beam.source.target.rb_voltage")
        ext_kv = data.get("ion_beam.source.extraction.rb_voltage")
        if tgt_kv is None or ext_kv is None:
            self._warn("Beam energy unknown",
                       "No live target / extraction voltage readback, so the beam energy is unknown.")
            return
        energy_ev = (tgt_kv + ext_kv) * 1000.0
        try:
            amps = current_for_mass(target_amu, energy_ev, cal["k"], cal["i_off"])
        except ValueError:
            self._warn("Calculation error", f"Beam energy is {energy_ev / 1000.0:.3f} keV - cannot resolve a mass.")
            return
        cap = hard_caps.command_cap("ion_beam.beamline.magnet.sp_requested_current")
        if cap is not None and cap.breach(amps):
            self._warn("Hard cap", f"{target_amu:g} amu at {energy_ev / 1000.0:.3f} keV needs {amps:.2f} A.\n\n"
                                   + cap.explain("that current", amps))
            return
        if not 0.0 <= amps <= self.sp_mag_current.maximum():
            self._warn("Out of range", f"{target_amu:g} amu at {energy_ev / 1000.0:.3f} keV needs {amps:.2f} A, "
                                       f"outside 0 - {self.sp_mag_current.maximum():g} A.")
            return

        self.sp_mag_current.discard_edit()
        self.sp_mag_current.show_setpoint(amps)
        self._dispatch_command("ion_beam.beamline.magnet.sp_requested_current", amps)
        self.event_helper.log_user_marker(
            time.time(), f"Magnet set to {amps:.3f} A for {target_amu:g} amu at {energy_ev / 1000.0:.3f} keV "
                         f"(k = {cal['k']:.2f}, I_off = {cal['i_off']:.3f}; calibration {cal['source']})", "#2196F3")

    def _init_post_magnet_ui(self):
        group = QGroupBox("3. Post-Magnet Tuning")
        layout = QGridLayout()

        # Image Slits
        layout.addWidget(QLabel("<b>Image Slits (L / R):</b>"), 0, 0)
        slit_layout = QHBoxLayout()

        self.sp_img_l = self._setpoint_box("Image slit left", lambda v: self._log_manual_slit("Image Slit Left", v))
        self.sp_img_l.setSuffix(" mm")

        self.sp_img_r = self._setpoint_box("Image slit right", lambda v: self._log_manual_slit("Image Slit Right", v))
        self.sp_img_r.setSuffix(" mm")

        slit_layout.addWidget(self.sp_img_l)
        slit_layout.addWidget(self.sp_img_r)
        layout.addLayout(slit_layout, 0, 1, 1, 2)

        group.setLayout(layout)
        self.main_layout.addWidget(group)

    def _init_downstream_optics_ui(self):
        group = QGroupBox("4. Downstream Lenses && Filters")
        self.optics_layout = QGridLayout()
        self.psu_controls = {}

        self._build_psu_row(0, "Beamline Einzel", "ion_beam.beamline.einzel", "kV", "µA", 0.0, 30.0)
        self._build_psu_row(1, "Neutral Trap (Pos)", "ion_beam.beamline.neutral_trap_pos", "kV", "µA", 0.0, 5.0)
        self._build_psu_row(2, "Neutral Trap (Neg)", "ion_beam.beamline.neutral_trap_neg", "kV", "µA", 0.0, 5.0)

        group.setLayout(self.optics_layout)
        self.main_layout.addWidget(group)

    def _build_psu_row(self, row, name, base_tag, pri_unit, sec_unit, min_v, max_v):
        lbl_name = QLabel(f"<b>{name}</b>")
        lbl_rb = QLabel(f"RB: --- {pri_unit} | --- {sec_unit}")
        lbl_rb.setMinimumWidth(180)

        btn_enable = QPushButton("ENABLE")
        btn_enable.setCheckable(True)
        btn_enable.setStyleSheet(COLOR_BUTTON_STANDARD)
        arm = ArmedToggle(btn_enable, lambda on, t=base_tag: self._dispatch_command(f"{t}.cmd_enable", on),
                          what=name.upper())

        # (Its current limit is the Spellman service's job: it sets one when the supply holds none)
        sp_box = self._setpoint_box(f"{name} setpoint", lambda v, t=f"{base_tag}.sp_requested_voltage":
                                    self._dispatch_command(t, v))
        sp_box.setRange(*self._limited_tags.get(f"{base_tag}.sp_requested_voltage", (min_v, max_v)))
        sp_box.setToolTip(hard_caps.note(f"{base_tag}.sp_requested_voltage"))
        sp_box.setSuffix(f" {pri_unit}")
        sp_box.setDecimals(3)
        sp_box.setSingleStep(0.001)

        self.optics_layout.addWidget(lbl_name, row, 0)
        self.optics_layout.addWidget(lbl_rb, row, 1)
        self.optics_layout.addWidget(sp_box, row, 2)
        self.optics_layout.addWidget(btn_enable, row, 3)

        self.psu_controls[name] = {"lbl_rb": lbl_rb, "sp_box": sp_box, "btn_enable": btn_enable, "arm": arm,
                                   "base_tag": base_tag, "pri_unit": pri_unit, "sec_unit": sec_unit}

    @staticmethod
    def _lock(widgets, reason: str, tip: str):
        for w in widgets:
            w.setEnabled(not reason)
            w.setToolTip(f"Disabled: {reason}" if reason else tip)

    def update_telemetry(self, data: dict):
        self._latest_telemetry = data
        plc_ok = bool(data.get("system.connected", False)) and not data.get("ion_beam.system.pc_plc_comms_lost")
        events_state = data.get("manager.services", {}).get("service_events", "OFFLINE")

        # --- 1. Magnet ---
        mag_cool = data.get("ion_beam.facilities.stat_mag_coolant_ok")
        if not plc_ok:
            self.lbl_mag_cooling.setText("MAG COOLING: UNKNOWN")
            self.lbl_mag_cooling.setStyleSheet(COLOR_INACTIVE)
        elif mag_cool:
            self.lbl_mag_cooling.setText("MAG COOLING: FLOW & TEMP OK")
            self.lbl_mag_cooling.setStyleSheet(COLOR_OK)
        else:
            self.lbl_mag_cooling.setText("MAG COOLING: FAULT")
            self.lbl_mag_cooling.setStyleSheet(COLOR_FAULT)

        # The setpoint the magnet service holds (the box never showed it, so after an HMI restart it
        # read 0.00 A whatever the magnet was at - one arrow-key step from there would have sent ~0 A)
        self.sp_mag_current.show_setpoint(data.get("ion_beam.beamline.magnet.sp_actual_current"))
        mag_v = data.get("ion_beam.beamline.magnet.rb_voltage")
        mag_i = data.get("ion_beam.beamline.magnet.rb_current")
        self.lbl_mag_rb.setText(f"RB: {mag_i:.2f} A | {mag_v:.2f} V" if mag_v is not None and mag_i is not None
                                else "RB: --- A | --- V")
        _show_tolerance(self.lbl_mag_rb, data, ("ion_beam.beamline.magnet.rb_current",))
        mag_en = data.get("ion_beam.beamline.magnet.stat_enabled")
        self.arm_mag.show_state(None if mag_en is None else bool(mag_en), COLOR_OK, COLOR_BUTTON_STANDARD,
                                "Magnet output state unknown - no current data from the magnet service.")

        # The magnet service's permit covers its connection, the safety relay and a sequence
        # holding the magnet (a mass scan, the optimiser, a recipe)
        mag_reason = permit_reason(data, "ion_beam.beamline.magnet")
        if not mag_reason and mag_cool is False:
            mag_reason = "Magnet hardware interlock/cooling fault."
        self._lock([self.btn_mag_en], mag_reason,
                   "Click twice to switch the magnet ON (confirm); once to switch it OFF.")
        self._lock([self.sp_mag_current], mag_reason,
                   "Set manual magnet current. " + hard_caps.note("ion_beam.beamline.magnet.sp_requested_current"))
        self._lock([self.btn_mag_deg], mag_reason, "Click to trigger autonomous degaussing sequence")
        self._lock([self.sp_mass], mag_reason, "Target mass for auto-tuning")
        self._lock([self.btn_calc_mass], mag_reason,
                   "Set the magnet current for the target mass, from the Mass Scan panel's calibration (k, I_off) "
                   "and the live beam energy. Hysteresis: the scan calibrates on the way up, so approach from "
                   "below (or degauss first) for the best match.")

        if self.arm_deg.armed:
            pass  # waiting for the confirming click
        elif data.get("ion_beam.beamline.magnet.stat_degaussing"):
            self.btn_mag_deg.setText("DEGAUSSING...")
            self.btn_mag_deg.setStyleSheet("background-color: #E91E63; color: white; font-weight: bold;")
        else:
            self.btn_mag_deg.setText("DEGAUSS ROUTINE")
            self.btn_mag_deg.setStyleSheet("background-color: #9C27B0; color: white; font-weight: bold;")

        # --- 2. Steerers: Y electrostatic (PLC), X magnetic (steerer PSU service) ---
        y_steer_en = bool(data.get("ion_beam.beamline.steering.stat_enabled", False))
        y_rb = data.get("ion_beam.beamline.steering.sp_actual_y_volts")
        self.lbl_y_rb.setText(f"RB: {y_rb:.1f} V" if y_rb is not None else "RB: --- V")
        if y_steer_en:      # (as before: with the steering off this reads 0 V - in the simulator's PLC at least)
            self.sp_y_steer.show_setpoint(y_rb)

        x_steer_en = bool(data.get("ion_beam.beamline.steerer_magnet.stat_enabled", False))
        x_v_rb = data.get("ion_beam.beamline.steerer_magnet.rb_voltage")
        x_i_rb = data.get("ion_beam.beamline.steerer_magnet.rb_current")
        x_i_sp = data.get("ion_beam.beamline.steerer_magnet.sp_actual_current")
        self.lbl_x_rb.setText(f"RB: {x_i_rb:.2f} A | {x_v_rb:.1f} V" if x_i_rb is not None and x_v_rb is not None
                              else "RB: --- A | --- V")
        _show_tolerance(self.lbl_x_rb, data, ("ion_beam.beamline.steerer_magnet.rb_current",))
        self.sp_x_steer.show_setpoint(x_i_sp)       # the steerer service's setpoint, output on or off

        # Master enable button shows OK only if BOTH steerers are verified active
        steer_known = ("ion_beam.beamline.steering.stat_enabled" in data
                       and "ion_beam.beamline.steerer_magnet.stat_enabled" in data)
        self.arm_steer.show_state(y_steer_en and x_steer_en if steer_known else None, COLOR_OK,
                                  COLOR_BUTTON_STANDARD,
                                  "Steering state unknown - no current data from the PLC or steerer service.")
        y_reason = permit_reason(data, "ion_beam.beamline.steering", "PLC")
        x_reason = permit_reason(data, "ion_beam.beamline.steerer_magnet", "steerer magnet")
        self._lock([self.btn_steer_en], y_reason or x_reason, "Enable Beam Steerer Outputs")
        self._lock([self.sp_y_steer], y_reason, "Adjust Y-Axis Beam Deflection")
        self._lock([self.sp_x_steer], x_reason, "Adjust X-Axis Magnet Current. "
                   + hard_caps.note("ion_beam.beamline.steerer_magnet.sp_requested_current"))

        # --- 3. Manual slit inputs (only logged: the events service must be there) ---
        slit_reason = "" if events_state == "ONLINE" else "Events microservice is offline (Cannot log manual adjustments)."
        self._lock([self.sp_obj_l, self.sp_obj_r, self.sp_img_l, self.sp_img_r], slit_reason,
                   "Update Database Log with physical slit dimensions")

        # --- 4. Spellman downstream optics (each supply has its own permit) ---
        for name, ctrl in self.psu_controls.items():
            base_tag = ctrl["base_tag"]
            rb_v = data.get(f"{base_tag}.rb_voltage")
            rb_i = data.get(f"{base_tag}.rb_current")
            v_str = f"{rb_v:.2f}" if rb_v is not None else "---"
            i_str = f"{rb_i:.2f}" if rb_i is not None else "---"
            ctrl["lbl_rb"].setText(f"RB: {v_str} {ctrl['pri_unit']} | {i_str} {ctrl['sec_unit']}")
            _show_tolerance(ctrl["lbl_rb"], data, (f"{base_tag}.rb_voltage", f"{base_tag}.rb_current"))

            ctrl["sp_box"].show_setpoint(data.get(f"{base_tag}.sp_actual_voltage"))

            reason = permit_reason(data, base_tag, "Spellman")
            self._lock([ctrl["btn_enable"]], reason, "Click twice to switch ON (confirm); once to switch OFF")
            self._lock([ctrl["sp_box"]], reason, hard_caps.note(f"{base_tag}.sp_requested_voltage"))
            stat_en = data.get(f"{base_tag}.stat_enabled")
            ctrl["arm"].show_state(None if stat_en is None else bool(stat_en), COLOR_OK, COLOR_BUTTON_STANDARD,
                                   f"{name} output state unknown - supply not answering or not configured.")
