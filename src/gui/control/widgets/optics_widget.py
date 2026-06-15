import time
import json
import os
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton, QFrame,
    QGroupBox, QDoubleSpinBox, QMessageBox)
from PyQt6.QtCore import Qt
from src.core.theme import (
    COLOR_OK,
    COLOR_FAULT,
    COLOR_INACTIVE,
    COLOR_BUTTON_STANDARD
)

class BeamlineOpticsWidget(QWidget):
    def __init__(self, cmd_thread, event_helper):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.event_helper = event_helper

        self.config_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../config"))
        self.magnet_config = self._load_magnet_config()

        self.main_layout = QVBoxLayout(self)

        self._init_pre_magnet_ui()
        self._init_magnet_ui()
        self._init_post_magnet_ui()
        self._init_downstream_optics_ui()

        self.main_layout.addStretch()

    def _dispatch_spellman_setpoints(self, base_tag: str, voltage: float):
        """Dispatches voltage and autonomously sets a safe 50uA current limit to unclamp the CC loop."""
        self._dispatch_command(f"{base_tag}.sp_requested_voltage", voltage)
        self._dispatch_command(f"{base_tag}.sp_requested_current", 50.0)

    def _load_magnet_config(self):
        try:
            path = os.path.join(self.config_dir, "magnet_config.json")
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            return {"mass_calibration_poly": [0.0, 1.0, 0.0, 0.0]}

    def _dispatch_command(self, tag: str, value):
        if "einzel" in tag or "neutral_trap" in tag:
            target = "spellman"
        elif "magnet" in tag:
            target = "magnet"
        else:
            target = "plc"
        self.cmd_thread.send_command(target, tag, value)

    def _log_manual_slit(self, position_name: str, spinbox: QDoubleSpinBox):
        val = spinbox.value()
        self.event_helper.log_user_marker(time.time(), f"Manual Adjustment: {position_name} set to {val} mm", "#9C27B0")
        tag_name = position_name.lower().replace(" ", "_").replace("-", "_")
        self._dispatch_command(f"ion_beam.beamline.slits.{tag_name}", val)

    def _init_pre_magnet_ui(self):
        group = QGroupBox("1. Pre-Magnet Tuning")
        layout = QGridLayout()

        # Y-Steerer
        layout.addWidget(QLabel("<b>Y-Steerer Voltage:</b>"), 0, 0)

        self.lbl_y_rb = QLabel("RB: --- V")
        self.lbl_y_rb.setMinimumWidth(80)
        layout.addWidget(self.lbl_y_rb, 0, 1)

        self.sp_y_steer = QDoubleSpinBox()
        self.sp_y_steer.setRange(-200.0, 200.0)
        self.sp_y_steer.setSuffix(" V")
        self.sp_y_steer.setDecimals(1)
        self.sp_y_steer.setKeyboardTracking(False)
        self.sp_y_steer.editingFinished.connect(
            lambda: self._dispatch_command("ion_beam.beamline.steering.sp_requested_y_volts", self.sp_y_steer.value()))
        layout.addWidget(self.sp_y_steer, 0, 2)

        # Master Steering Enable (Applies to both X and Y)
        self.btn_steer_en = QPushButton("ENABLE STEERING")
        self.btn_steer_en.setCheckable(True)
        self.btn_steer_en.setStyleSheet(COLOR_BUTTON_STANDARD)
        self.btn_steer_en.clicked.connect(
            lambda *args: self._dispatch_command("ion_beam.beamline.steering.cmd_enable",
                                                 self.btn_steer_en.isChecked()))
        layout.addWidget(self.btn_steer_en, 0, 3)

        # Object Slits
        layout.addWidget(QLabel("<b>Object Slits (L / R):</b>"), 1, 0)
        slit_layout = QHBoxLayout()

        self.sp_obj_l = QDoubleSpinBox()
        self.sp_obj_l.setSuffix(" mm")
        self.sp_obj_l.setKeyboardTracking(False)
        self.sp_obj_l.editingFinished.connect(lambda: self._log_manual_slit("Object Slit Left", self.sp_obj_l))

        self.sp_obj_r = QDoubleSpinBox()
        self.sp_obj_r.setSuffix(" mm")
        self.sp_obj_r.setKeyboardTracking(False)
        self.sp_obj_r.editingFinished.connect(lambda: self._log_manual_slit("Object Slit Right", self.sp_obj_r))

        slit_layout.addWidget(self.sp_obj_l)
        slit_layout.addWidget(self.sp_obj_r)
        layout.addLayout(slit_layout, 1, 1, 1, 2)

        group.setLayout(layout)
        self.main_layout.addWidget(group)

    def _init_magnet_ui(self):
        group = QGroupBox("2. Mass Analyzer Magnet")
        layout = QGridLayout()

        self.lbl_mag_cooling = QLabel("COOLING: UNKNOWN")
        self.lbl_mag_cooling.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_mag_cooling.setStyleSheet(COLOR_INACTIVE)
        layout.addWidget(self.lbl_mag_cooling, 0, 0, 1, 2)

        layout.addWidget(QLabel("<b>Magnet Current:</b>"), 1, 0)
        self.lbl_mag_rb = QLabel("RB: --- A | --- V")
        layout.addWidget(self.lbl_mag_rb, 1, 1)

        self.sp_mag_current = QDoubleSpinBox()
        self.sp_mag_current.setRange(0.0, 50.0)
        self.sp_mag_current.setSuffix(" A")
        self.sp_mag_current.setDecimals(2)
        self.sp_mag_current.setSingleStep(0.01)
        self.sp_mag_current.setKeyboardTracking(False)
        self.sp_mag_current.editingFinished.connect(
            lambda: self._dispatch_command("ion_beam.beamline.magnet.sp_requested_current",
                                           self.sp_mag_current.value()))
        layout.addWidget(self.sp_mag_current, 1, 2)

        self.btn_mag_en = QPushButton("ENABLE MAGNET")
        self.btn_mag_en.setCheckable(True)
        self.btn_mag_en.setStyleSheet(COLOR_BUTTON_STANDARD)
        self.btn_mag_en.clicked.connect(
            lambda *args: self._dispatch_command("ion_beam.beamline.magnet.cmd_enable", self.btn_mag_en.isChecked()))
        layout.addWidget(self.btn_mag_en, 1, 3)

        self.btn_mag_deg = QPushButton("DEGAUSS ROUTINE")
        self.btn_mag_deg.setStyleSheet("background-color: #9C27B0; color: white; font-weight: bold;")
        self.btn_mag_deg.clicked.connect(
            lambda *args: self._dispatch_command("ion_beam.beamline.magnet.cmd_degauss", True))
        layout.addWidget(self.btn_mag_deg, 1, 4)

        calc_frame = QFrame()
        calc_frame.setStyleSheet("background-color: #E3F2FD; border-radius: 4px; padding: 4px;")
        calc_layout = QHBoxLayout(calc_frame)
        calc_layout.setContentsMargins(4, 4, 4, 4)

        calc_layout.addWidget(QLabel("<b>Auto-Tune Mass (amu):</b>"))
        self.sp_mass = QDoubleSpinBox()
        self.sp_mass.setRange(1.0, 250.0)
        self.sp_mass.setDecimals(1)
        self.sp_mass.setSingleStep(1.0)
        self.sp_mass.setValue(28.0)
        self.sp_mass.setKeyboardTracking(False)
        calc_layout.addWidget(self.sp_mass)

        self.btn_calc_mass = QPushButton("CALCULATE && SET AMPS")
        self.btn_calc_mass.setStyleSheet("background-color: #2196F3; color: white; font-weight: bold;")
        self.btn_calc_mass.clicked.connect(self._calculate_mass)
        calc_layout.addWidget(self.btn_calc_mass)

        layout.addWidget(calc_frame, 2, 0, 1, 5)
        group.setLayout(layout)
        self.main_layout.addWidget(group)

    def _calculate_mass(self):
        target_amu = self.sp_mass.value()

        app = QApplication.instance()
        main_win = next(w for w in app.topLevelWidgets() if isinstance(w, QMainWindow))
        cache = main_win.master_telemetry_cache

        extr_kv = cache.get("ion_beam.source.extraction.rb_voltage", 0.0)

        if extr_kv <= 0:
            QMessageBox.warning(self, "Calculation Error", "Beam energy is 0V. Cannot resolve mass.")
            return

        poly_coeffs = self.magnet_config.get("mass_calibration_poly", [0.0, 1.0, 0.0, 0.0])
        x_factor = (target_amu * extr_kv) ** 0.5

        required_amps = sum(coeff * (x_factor ** idx) for idx, coeff in enumerate(poly_coeffs))
        required_amps = max(0.0, min(required_amps, self.sp_mag_current.maximum()))

        self.sp_mag_current.setValue(required_amps)
        self._dispatch_command("ion_beam.beamline.magnet.sp_requested_current", required_amps)

        self.event_helper.log_user_marker(time.time(),
                                          f"Auto-Tuned Magnet to {required_amps:.2f} A for {target_amu} amu at {extr_kv:.2f} kV",
                                          "#2196F3")

    def _init_post_magnet_ui(self):
        group = QGroupBox("3. Post-Magnet Tuning")
        layout = QGridLayout()

        # X-Steerer
        layout.addWidget(QLabel("<b>X-Steerer Voltage:</b>"), 0, 0)

        self.lbl_x_rb = QLabel("RB: --- V")
        self.lbl_x_rb.setMinimumWidth(80)
        layout.addWidget(self.lbl_x_rb, 0, 1)

        self.sp_x_steer = QDoubleSpinBox()
        self.sp_x_steer.setRange(-200.0, 200.0)
        self.sp_x_steer.setSuffix(" V")
        self.sp_x_steer.setDecimals(1)
        self.sp_x_steer.setKeyboardTracking(False)
        self.sp_x_steer.editingFinished.connect(
            lambda: self._dispatch_command("ion_beam.beamline.steering.sp_requested_x_volts", self.sp_x_steer.value()))
        layout.addWidget(self.sp_x_steer, 0, 2)

        # Image Slits
        layout.addWidget(QLabel("<b>Image Slits (L / R):</b>"), 1, 0)
        slit_layout = QHBoxLayout()

        self.sp_img_l = QDoubleSpinBox()
        self.sp_img_l.setSuffix(" mm")
        self.sp_img_l.setKeyboardTracking(False)
        self.sp_img_l.editingFinished.connect(lambda: self._log_manual_slit("Image Slit Left", self.sp_img_l))

        self.sp_img_r = QDoubleSpinBox()
        self.sp_img_r.setSuffix(" mm")
        self.sp_img_r.setKeyboardTracking(False)
        self.sp_img_r.editingFinished.connect(lambda: self._log_manual_slit("Image Slit Right", self.sp_img_r))

        slit_layout.addWidget(self.sp_img_l)
        slit_layout.addWidget(self.sp_img_r)
        layout.addLayout(slit_layout, 1, 1, 1, 2)

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
        btn_enable.clicked.connect(
            lambda *args, b=btn_enable, t=base_tag: self._dispatch_command(f"{t}.cmd_enable", b.isChecked()))

        sp_box = QDoubleSpinBox()
        sp_box.setRange(min_v, max_v)
        sp_box.setSuffix(f" {pri_unit}")
        sp_box.setDecimals(3)
        sp_box.setSingleStep(0.001)
        sp_box.setKeyboardTracking(False)
        sp_box.editingFinished.connect(
            lambda t=base_tag, b=sp_box: self._dispatch_spellman_setpoints(t, b.value()))

        self.optics_layout.addWidget(lbl_name, row, 0)
        self.optics_layout.addWidget(lbl_rb, row, 1)
        self.optics_layout.addWidget(sp_box, row, 2)
        self.optics_layout.addWidget(btn_enable, row, 3)

        self.psu_controls[name] = {"lbl_rb": lbl_rb, "sp_box": sp_box, "btn_enable": btn_enable, "base_tag": base_tag,
                                   "pri_unit": pri_unit, "sec_unit": sec_unit}

    def update_telemetry(self, data: dict):
        master_comms_lost = not bool(data.get("system.connected", False)) or bool(
            data.get("ion_beam.system.pc_plc_comms_lost", False))
        relay_active = bool(data.get("ion_beam.facilities.safety_relay_active", False))

        services = data.get("manager.services", {})
        mag_state = services.get("service_magnet_psu", "OFFLINE")
        spellman_state = services.get("service_spellman_mpd", "OFFLINE")
        events_state = services.get("service_events", "OFFLINE")

        # --- 1. Update Magnet State ---
        mag_cool = data.get("ion_beam.facilities.stat_mag_coolant_ok")

        if master_comms_lost:
            self.lbl_mag_cooling.setText("MAG COOLING: UNKNOWN")
            self.lbl_mag_cooling.setStyleSheet(COLOR_INACTIVE)
        elif mag_cool == True:
            self.lbl_mag_cooling.setText("MAG COOLING: FLOW & TEMP OK")
            self.lbl_mag_cooling.setStyleSheet(COLOR_OK)
        else:
            self.lbl_mag_cooling.setText("MAG COOLING: FAULT")
            self.lbl_mag_cooling.setStyleSheet(COLOR_FAULT)

        mag_v = data.get("ion_beam.beamline.magnet.rb_voltage")
        mag_i = data.get("ion_beam.beamline.magnet.rb_current")
        if mag_v is not None and mag_i is not None:
            self.lbl_mag_rb.setText(f"RB: {mag_i:.2f} A | {mag_v:.2f} V")

        mag_en = data.get("ion_beam.beamline.magnet.stat_enabled")
        mag_deg = data.get("ion_beam.beamline.magnet.stat_degaussing")

        if mag_en is not None:
            self.btn_mag_en.setChecked(bool(mag_en))
            self.btn_mag_en.setStyleSheet(COLOR_OK if mag_en else COLOR_BUTTON_STANDARD)

        mag_lock_reason = ""
        if mag_state != "ONLINE":
            mag_lock_reason = "Magnet microservice is offline."
        elif master_comms_lost:
            mag_lock_reason = "Safety Relay state is unknown (PLC offline)."
        elif not relay_active:
            mag_lock_reason = "Safety Relay is De-Energized."
        elif mag_cool is False:
            mag_lock_reason = "Magnet hardware interlock/cooling fault."

        mag_lockout = bool(mag_lock_reason)
        mag_tt = f"Disabled: {mag_lock_reason}" if mag_lockout else "Click to enable Magnet Output"

        self.btn_mag_en.setEnabled(not mag_lockout)
        self.btn_mag_en.setToolTip(mag_tt)
        self.sp_mag_current.setEnabled(not mag_lockout)
        self.sp_mag_current.setToolTip(mag_tt)
        self.btn_mag_deg.setEnabled(not mag_lockout)
        self.btn_mag_deg.setToolTip(
            f"Disabled: {mag_lock_reason}" if mag_lockout else "Click to trigger autonomous degaussing sequence")
        self.btn_calc_mass.setEnabled(not mag_lockout)
        self.btn_calc_mass.setToolTip(
            f"Disabled: {mag_lock_reason}" if mag_lockout else "Click to auto-tune magnet current for target mass")

        if mag_deg:
            self.btn_mag_deg.setText("DEGAUSSING...")
            self.btn_mag_deg.setStyleSheet("background-color: #E91E63; color: white; font-weight: bold;")
        else:
            self.btn_mag_deg.setText("DEGAUSS ROUTINE")
            self.btn_mag_deg.setStyleSheet("background-color: #9C27B0; color: white; font-weight: bold;")

        # --- 2. Update Steerers ---
        # PLC Telemetry handling
        steer_en = bool(data.get("ion_beam.beamline.steering.stat_enabled", False))
        self.btn_steer_en.setChecked(steer_en)
        self.btn_steer_en.setStyleSheet(COLOR_OK if steer_en else COLOR_BUTTON_STANDARD)

        # Read the Actual Setpoints active in the PLC
        y_rb = data.get("ion_beam.beamline.steering.sp_actual_y_volts")
        x_rb = data.get("ion_beam.beamline.steering.sp_actual_x_volts")

        # The text label ALWAYS tracks the true PLC output
        self.lbl_y_rb.setText(f"RB: {y_rb:.1f} V" if y_rb is not None else "RB: --- V")
        self.lbl_x_rb.setText(f"RB: {x_rb:.1f} V" if x_rb is not None else "RB: --- V")

        # The Spinbox ONLY syncs to the PLC if the steerer is enabled.
        # This prevents your typed setpoint from zeroing out when the output drops.
        if y_rb is not None and steer_en and not self.sp_y_steer.hasFocus():
            self.sp_y_steer.blockSignals(True)
            self.sp_y_steer.setValue(float(y_rb))
            self.sp_y_steer.blockSignals(False)

        if x_rb is not None and steer_en and not self.sp_x_steer.hasFocus():
            self.sp_x_steer.blockSignals(True)
            self.sp_x_steer.setValue(float(x_rb))
            self.sp_x_steer.blockSignals(False)

        # Steerer Lockouts
        steer_lock_reason = ""
        if master_comms_lost:
            steer_lock_reason = "PLC communications are offline."
        elif not relay_active:
            steer_lock_reason = "Safety Relay is De-Energized."

        steer_lockout = bool(steer_lock_reason)

        self.btn_steer_en.setEnabled(not steer_lockout)
        self.btn_steer_en.setToolTip(
            f"Disabled: {steer_lock_reason}" if steer_lockout else "Enable Beam Steerer Outputs")
        self.sp_y_steer.setEnabled(not steer_lockout)
        self.sp_y_steer.setToolTip(
            f"Disabled: {steer_lock_reason}" if steer_lockout else "Adjust Y-Axis Beam Deflection")
        self.sp_x_steer.setEnabled(not steer_lockout)
        self.sp_x_steer.setToolTip(
            f"Disabled: {steer_lock_reason}" if steer_lockout else "Adjust X-Axis Beam Deflection")

        # --- 3. Update Manual Slit Inputs ---
        slit_lock_reason = "Events microservice is offline (Cannot log manual adjustments)."
        slit_lockout = (events_state != "ONLINE")
        slit_tt = f"Disabled: {slit_lock_reason}" if slit_lockout else "Update Database Log with physical slit dimensions"

        for slit_sp in [self.sp_obj_l, self.sp_obj_r, self.sp_img_l, self.sp_img_r]:
            slit_sp.setEnabled(not slit_lockout)
            slit_sp.setToolTip(slit_tt)

        # --- 4. Update Spellman Downstream Optics ---
        spell_lock_reason = ""
        if spellman_state != "ONLINE":
            spell_lock_reason = "Spellman microservice is offline."
        elif master_comms_lost:
            spell_lock_reason = "Safety Relay state is unknown (PLC offline)."
        elif not relay_active:
            spell_lock_reason = "Safety Relay is De-Energized."

        spell_lockout = bool(spell_lock_reason)
        spell_tt = f"Disabled: {spell_lock_reason}" if spell_lockout else ""

        for name, ctrl in self.psu_controls.items():
            base_tag = ctrl["base_tag"]

            rb_v = data.get(f"{base_tag}.rb_voltage")
            rb_i = data.get(f"{base_tag}.rb_current")
            v_str = f"{rb_v:.2f}" if rb_v is not None else "---"
            i_str = f"{rb_i:.2f}" if rb_i is not None else "---"
            ctrl["lbl_rb"].setText(f"RB: {v_str} {ctrl['pri_unit']} | {i_str} {ctrl['sec_unit']}")

            sp_val = data.get(f"{base_tag}.sp_actual_voltage")
            if sp_val is not None and not ctrl["sp_box"].hasFocus():
                ctrl["sp_box"].blockSignals(True)
                ctrl["sp_box"].setValue(float(sp_val))
                ctrl["sp_box"].blockSignals(False)

            stat_en = data.get(f"{base_tag}.stat_enabled")
            if stat_en is not None:
                ctrl["btn_enable"].setChecked(bool(stat_en))
                ctrl["btn_enable"].setStyleSheet(COLOR_OK if stat_en else COLOR_BUTTON_STANDARD)

            ctrl["btn_enable"].setEnabled(not spell_lockout)
            ctrl["btn_enable"].setToolTip(spell_tt if spell_lockout else "Click to toggle power output")
            ctrl["sp_box"].setEnabled(not spell_lockout)
            ctrl["sp_box"].setToolTip(spell_tt)
