import time
import logging
from PyQt6.QtWidgets import (
    QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton, QFrame,
    QGroupBox, QDoubleSpinBox, QMessageBox, QComboBox)
from PyQt6.QtCore import Qt
from src.core.theme import (
    COLOR_OK,
    COLOR_FAULT,
    COLOR_INACTIVE,
    COLOR_BUTTON_STANDARD
)

import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import QDockWidget, QFormLayout

from src.core.mass_calibration import current_for_mass
from src.gui.control.arming import ArmedAction, ArmedToggle
from src.gui.control.widgets.source_widget import TOLERANCE_STYLE, TOLERANCE_TIP, setpoint_limits, tolerance_flagged

log = logging.getLogger("hmi.optics")


def _show_tolerance(label, data, tags):
    if tolerance_flagged(data, tags):
        label.setStyleSheet(TOLERANCE_STYLE)
        label.setToolTip(TOLERANCE_TIP)
    elif label.styleSheet():
        label.setStyleSheet("")
        label.setToolTip("")


class MassScanWorker(QThread):
    data_point = pyqtSignal(float, float)
    scan_finished = pyqtSignal(str)
    status_msg = pyqtSignal(str)

    def __init__(self, cmd_thread, get_telemetry_cb, low: float, high: float, steps: int, dwell: float):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.get_telemetry_cb = get_telemetry_cb
        self.low = low
        self.high = high
        self.steps = steps
        self.dwell = dwell

        self.is_stopped = False
        # Direct target for the magnet current
        self.current_sp_tag = "ion_beam.beamline.magnet.sp_requested_current"
        self.mag_rb_tag = "ion_beam.beamline.magnet.rb_current"
        self.beam_rb_tag = "ion_beam.beamline.faraday.smu.rb_current"

    def run(self):
        self.status_msg.emit("Starting Sweep...")
        current_points = np.linspace(self.low, self.high, self.steps)
        failure = None

        for amps in current_points:
            if self.is_stopped: break

            # Send raw Amps command directly to the magnet target
            ticket = self.cmd_thread.send_command("magnet", self.current_sp_tag, float(amps))

            # Dwell
            start = time.time()
            while time.time() - start < self.dwell:
                if self.is_stopped:
                    self.scan_finished.emit("stopped")
                    return
                time.sleep(0.05)

            if self.is_stopped: break
            if ticket.done() and not ticket.ok:
                failure = f"magnet refused {amps:.3f} A: {ticket.reason}"
                break

            # Measure - only live values; a stale or missing reading stops the sweep
            actual_mag_amps = self.get_telemetry_cb(self.mag_rb_tag)
            measured_beam_current = self.get_telemetry_cb(self.beam_rb_tag)
            if actual_mag_amps is None or measured_beam_current is None:
                failure = "magnet or Faraday cup telemetry is stale / unavailable"
                break
            self.data_point.emit(float(actual_mag_amps), float(measured_beam_current))

        # Modified completion emission
        if failure:
            log.warning("Sweep stopped: %s", failure)
            self.status_msg.emit(f"Sweep stopped: {failure}")
            self.scan_finished.emit(f"stopped ({failure})")
        elif not self.is_stopped:
            self.status_msg.emit("Scan Complete.")
            self.scan_finished.emit("complete")
        else:
            self.scan_finished.emit("stopped")

    def stop(self):
        self.is_stopped = True

class MassScanPlotWidget(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        self.figure = Figure()
        self.canvas = FigureCanvas(self.figure)
        self.ax = self.figure.add_subplot(111)

        self.ax.set_xlabel("Magnet Current (A)")
        self.ax.set_ylabel("Beam Current (A)")
        self.ax.grid(True)
        self.line, = self.ax.plot([], [], 'b.-')
        layout.addWidget(self.canvas)

        self.x_data = []
        self.y_data = []

    def add_point(self, x, y):
        self.x_data.append(x)
        self.y_data.append(y)
        self.line.set_data(self.x_data, self.y_data)

        if self.x_data:
            self.ax.set_xlim(min(self.x_data) - 1, max(self.x_data) + 1)
            min_y, max_y = min(self.y_data), max(self.y_data)
            margin = (max_y - min_y) * 0.1 if max_y != min_y else 1e-9
            self.ax.set_ylim(min_y - margin, max_y + margin)
        self.canvas.draw()

    def clear_plot(self):
        self.x_data.clear()
        self.y_data.clear()
        self.line.set_data([], [])
        self.canvas.draw()

class BeamlineOpticsWidget(QWidget):
    def __init__(self, cmd_thread, event_helper):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.event_helper = event_helper
        self.active_scan_start_str = None  # Track active start timestamp string

        # Set by the main window: returns the Mass Scan panel's calibration {"k", "i_off", "source"}
        self.mass_calibration = None
        self._limited_tags = setpoint_limits()

        # Telemetry Cache
        self._latest_telemetry = {}
        self.scan_worker = None

        self.plot_dock = None
        self.plot_widget = None

        self.main_layout = QVBoxLayout(self)

        self._init_pre_magnet_ui()
        self._init_magnet_ui()
        self._init_post_magnet_ui()
        self._init_downstream_optics_ui()

        self.main_layout.addStretch()

    def _init_plot_dock(self):
        main_win = self.window()
        if isinstance(main_win, QMainWindow):
            self.plot_dock = QDockWidget("Mass Scan Live Plot", main_win)
            self.plot_widget = MassScanPlotWidget()
            self.plot_dock.setWidget(self.plot_widget)

            self.plot_dock.setFloating(True)
            main_win.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.plot_dock)
            self.plot_dock.resize(600, 400)

            self.plot_dock.hide()
        else:
            self.plot_dock = None

    def _dispatch_spellman_setpoints(self, base_tag: str, voltage: float):
        """Dispatches voltage and autonomously sets a safe 50uA current limit to unclamp the CC loop."""
        self._dispatch_command(f"{base_tag}.sp_requested_voltage", voltage)
        # Only once current limits are configured (the service refuses - and always dropped - it otherwise)
        if f"{base_tag}.sp_requested_current" in self._limited_tags:
            self._dispatch_command(f"{base_tag}.sp_requested_current", 50.0)

    def _dispatch_command(self, tag: str, value):
        if "einzel" in tag or "neutral_trap" in tag:
            target = "spellman"
        elif "steerer_magnet" in tag:
            target = "steerer_magnet"
        elif "magnet" in tag:
            target = "magnet"
        else:
            target = "plc"
        self.cmd_thread.send_command(target, tag, value)

    def _toggle_master_steering(self, state: bool):
        self._dispatch_command("ion_beam.beamline.steering.cmd_enable", state)
        self._dispatch_command("ion_beam.beamline.steerer_magnet.cmd_enable", state)

    def _log_manual_slit(self, position_name: str, spinbox: QDoubleSpinBox):
        val = spinbox.value()
        self.event_helper.log_user_marker(time.time(), f"Manual Adjustment: {position_name} set to {val} mm", "#9C27B0")
        tag_name = position_name.lower().replace(" ", "_").replace("-", "_")
        self._dispatch_command(f"ion_beam.beamline.slits.{tag_name}", val)

    def _init_pre_magnet_ui(self):
        group = QGroupBox("1. Pre-Magnet Tuning")
        layout = QGridLayout()

        # Y-Steerer (Electrostatic)
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
        self.arm_steer = ArmedToggle(self.btn_steer_en, self._toggle_master_steering, what="STEERING")
        # Span the button across rows 0 and 1
        layout.addWidget(self.btn_steer_en, 0, 3, 2, 1)

        # X-Steerer (Magnetic)
        layout.addWidget(QLabel("<b>X-Steerer Current:</b>"), 1, 0)

        self.lbl_x_rb = QLabel("RB: --- A | --- V")
        self.lbl_x_rb.setMinimumWidth(120)
        layout.addWidget(self.lbl_x_rb, 1, 1)

        self.sp_x_steer = QDoubleSpinBox()
        self.sp_x_steer.setRange(0.0, 80.0)
        self.sp_x_steer.setSuffix(" A")
        self.sp_x_steer.setDecimals(2)
        self.sp_x_steer.setSingleStep(0.1)
        self.sp_x_steer.setKeyboardTracking(False)
        self.sp_x_steer.editingFinished.connect(
            lambda: self._dispatch_command("ion_beam.beamline.steerer_magnet.sp_requested_current", self.sp_x_steer.value()))
        layout.addWidget(self.sp_x_steer, 1, 2)

        # Object Slits
        layout.addWidget(QLabel("<b>Object Slits (L / R):</b>"), 2, 0)
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

        self.sp_mag_current = QDoubleSpinBox()
        self.sp_mag_current.setRange(0.0, 80.0)
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

        # --- Row 3: Sweep Configuration ---
        layout.addWidget(QLabel("<b>Sweep Config:</b>"), 3, 0)

        self.sp_scan_low = QDoubleSpinBox()
        self.sp_scan_low.setPrefix("L: ")
        self.sp_scan_low.setSuffix(" A")
        self.sp_scan_low.setMaximum(100.0)
        self.sp_scan_low.setValue(0.0)
        self.sp_scan_low.setKeyboardTracking(False)
        layout.addWidget(self.sp_scan_low, 3, 1)

        self.sp_scan_high = QDoubleSpinBox()
        self.sp_scan_high.setPrefix("H: ")
        self.sp_scan_high.setSuffix(" A")
        self.sp_scan_high.setMaximum(100.0)
        self.sp_scan_high.setValue(5.0)
        self.sp_scan_high.setKeyboardTracking(False)
        layout.addWidget(self.sp_scan_high, 3, 2)

        self.sp_scan_steps = QDoubleSpinBox()
        self.sp_scan_steps.setPrefix("Steps: ")
        self.sp_scan_steps.setDecimals(0)
        self.sp_scan_steps.setRange(10, 10000)
        self.sp_scan_steps.setValue(100)
        self.sp_scan_steps.setKeyboardTracking(False)
        layout.addWidget(self.sp_scan_steps, 3, 3)

        self.combo_scan_speed = QComboBox()
        self.combo_scan_speed.addItems(["Fast (0.5s dwell)", "Slow (2.0s dwell)", "Very Slow (5.0s dwell)"])
        layout.addWidget(self.combo_scan_speed, 3, 4)

        # --- Row 4: Sweep Controls ---
        self.btn_start_scan = QPushButton("START SWEEP")
        self.btn_start_scan.setStyleSheet("background-color: #FF9800; color: white; font-weight: bold;")
        self.btn_start_scan.clicked.connect(self._start_sweep)
        self.btn_start_scan.setToolTip("Click to start the automated mass scan sweep")
        layout.addWidget(self.btn_start_scan, 4, 2)

        self.btn_stop_scan = QPushButton("STOP")
        self.btn_stop_scan.setStyleSheet("background-color: #F44336; color: white; font-weight: bold;")
        self.btn_stop_scan.setEnabled(False)
        self.btn_stop_scan.clicked.connect(self._stop_sweep)
        self.btn_stop_scan.setToolTip("Disabled: No sweep currently active")
        layout.addWidget(self.btn_stop_scan, 4, 3)

        self.btn_toggle_plot = QPushButton("SHOW PLOT")
        self.btn_toggle_plot.clicked.connect(self._toggle_plot)
        self.btn_toggle_plot.setToolTip("Open or bring the live plot window to the front")
        layout.addWidget(self.btn_toggle_plot, 4, 4)

        group.setLayout(layout)
        self.main_layout.addWidget(group)

    def _get_latest_telemetry(self, tag: str):
        """Live value, or None if stale/unknown (the HMI only passes fresh values to widgets)."""
        value = self._latest_telemetry.get(tag)
        return float(value) if value is not None else None

    def _start_sweep(self):
        if not self.btn_mag_en.isChecked():
            QMessageBox.warning(self, "Hardware Interlock", "Cannot start sweep: Magnet is not enabled.")
            return

        low = self.sp_scan_low.value()
        high = self.sp_scan_high.value()
        steps = int(self.sp_scan_steps.value())

        # Parameter Safety Check
        if low >= high:
            QMessageBox.warning(self, "Invalid Sweep Parameters",
                                "The Low limit must be strictly less than the High limit.")
            return

        # Lazy-load the dock if it hasn't been created yet
        if self.plot_dock is None:
            self._init_plot_dock()

        # Safely open and clear the plot
        if self.plot_dock is not None and self.plot_widget is not None:
            self.plot_dock.show()
            self.plot_widget.clear_plot()

        # Determine dwell time from Combobox
        speed_idx = self.combo_scan_speed.currentIndex()
        if speed_idx == 0:
            dwell = 0.5
        elif speed_idx == 1:
            dwell = 2.0
        else:
            dwell = 5.0

        # --- EVENT LOGGING: START ---
        start_t = time.time()
        self.active_scan_start_str = time.strftime('%H:%M:%S',
                                                   time.localtime(start_t)) + f":{int((start_t % 1) * 1000):03d}"

        start_msg = (f"Mass Scan started at {self.active_scan_start_str} | "
                     f"Range: {low:.2f} A to {high:.2f} A | Steps: {steps} | Dwell: {dwell}s")
        self.event_helper.log_user_marker(start_t, start_msg, "#FF9800")  # Logged as Orange
        # --- EVENT LOGGING: END ---

        self.scan_worker = MassScanWorker(self.cmd_thread, self._get_latest_telemetry, low, high, steps, dwell)

        if self.plot_widget is not None:
            self.scan_worker.data_point.connect(self.plot_widget.add_point)

        self.scan_worker.scan_finished.connect(self._on_sweep_finished)
        self.scan_worker.start()

        self._update_sweep_ui_lock(True)

    def _stop_sweep(self):
        if self.scan_worker and self.scan_worker.isRunning():
            self.scan_worker.stop()
            self.scan_worker.wait()
        self._update_sweep_ui_lock(False)

    def _on_sweep_finished(self, status: str):  # Updated to receive terminal status
        self._update_sweep_ui_lock(False)

        # --- EVENT LOGGING: COMPLETION/STOP ---
        if self.active_scan_start_str:
            end_t = time.time()
            end_msg = f"Mass Scan from {self.active_scan_start_str} {status}"
            self.event_helper.log_user_marker(end_t, end_msg, "#FF9800")
            self.active_scan_start_str = None
        # --- EVENT LOGGING: END ---

    def _toggle_plot(self):
        # Lazy-load the dock if it hasn't been created yet
        if self.plot_dock is None:
            self._init_plot_dock()

        if self.plot_dock is not None:
            if self.plot_dock.isVisible():
                self.plot_dock.hide()
            else:
                self.plot_dock.show()

    def _update_sweep_ui_lock(self, is_sweeping: bool):
        # The STOP button is only enabled when sweeping
        self.btn_stop_scan.setEnabled(is_sweeping)
        self.btn_stop_scan.setToolTip(
            "Click to stop the active sweep" if is_sweeping else "Disabled: No sweep currently active"
        )

        # If we just started a sweep, immediately lock the UI.
        # (If we just stopped, we let the next 10Hz telemetry tick safely unlock it based on hardware limits)
        if is_sweeping:
            sweep_lock_msg = "Disabled: Sweep is currently active"

            self.btn_start_scan.setEnabled(False)
            self.btn_start_scan.setToolTip(sweep_lock_msg)

            self.sp_mag_current.setEnabled(False)
            self.sp_mag_current.setToolTip(sweep_lock_msg)

            self.btn_mag_deg.setEnabled(False)
            self.btn_mag_deg.setToolTip(sweep_lock_msg)

            self.sp_mass.setEnabled(False)
            self.sp_mass.setToolTip(sweep_lock_msg)

            self.btn_calc_mass.setEnabled(False)
            self.btn_calc_mass.setToolTip(sweep_lock_msg)

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
        if not 0.0 <= amps <= self.sp_mag_current.maximum():
            self._warn("Out of range", f"{target_amu:g} amu at {energy_ev / 1000.0:.3f} keV needs {amps:.2f} A, "
                                       f"outside 0 - {self.sp_mag_current.maximum():g} A.")
            return

        self.sp_mag_current.setValue(amps)
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

        sp_box = QDoubleSpinBox()
        sp_box.setRange(*self._limited_tags.get(f"{base_tag}.sp_requested_voltage", (min_v, max_v)))
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

        self.psu_controls[name] = {"lbl_rb": lbl_rb, "sp_box": sp_box, "btn_enable": btn_enable, "arm": arm,
                                   "base_tag": base_tag, "pri_unit": pri_unit, "sec_unit": sec_unit}

    def update_telemetry(self, data: dict):
        self._latest_telemetry = data

        master_comms_lost = not bool(data.get("system.connected", False)) or bool(
            data.get("ion_beam.system.pc_plc_comms_lost", False))
        relay_active = bool(data.get("ion_beam.facilities.safety_relay_active", False))

        services = data.get("manager.services", {})
        mag_state = services.get("service_magnet_psu", "OFFLINE")
        spellman_state = services.get("service_spellman_mpd", "OFFLINE")
        events_state = services.get("service_events", "OFFLINE")

        # --- 1. Update Magnet State ---
        mag_cool = data.get("ion_beam.facilities.stat_mag_coolant_ok")

        # Check if the microservice is under external automated control
        mag_ctrl_mode = data.get("ion_beam.beamline.magnet.rb_ctrl_mode", 0.0)
        mag_auto_locked = (mag_ctrl_mode > 0.0)

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
        else:
            self.lbl_mag_rb.setText("RB: --- A | --- V")
        _show_tolerance(self.lbl_mag_rb, data, ("ion_beam.beamline.magnet.rb_current",))

        mag_en = data.get("ion_beam.beamline.magnet.stat_enabled")
        mag_deg = data.get("ion_beam.beamline.magnet.stat_degaussing")

        self.arm_mag.show_state(None if mag_en is None else bool(mag_en), COLOR_OK, COLOR_BUTTON_STANDARD,
                                "Magnet output state unknown - no current data from the magnet service.")

        # ABORT SWEEP IF MAGNET DROPS OR FAULTS
        is_sweeping = self.scan_worker is not None and self.scan_worker.isRunning()
        if is_sweeping and not mag_en:
            self._stop_sweep()

        mag_lock_reason = ""
        if mag_state != "ONLINE":
            mag_lock_reason = "Magnet microservice is offline."
        elif master_comms_lost:
            mag_lock_reason = "Safety Relay state is unknown (PLC offline)."
        elif not relay_active:
            mag_lock_reason = "Safety Relay is De-Energized."
        elif mag_cool is False:
            mag_lock_reason = "Magnet hardware interlock/cooling fault."
        elif mag_auto_locked:
            mag_lock_reason = "Locked by automated sequencer."

        mag_lockout = bool(mag_lock_reason)
        mag_tt = f"Disabled: {mag_lock_reason}" if mag_lockout else "Click to enable Magnet Output"

        self.btn_mag_en.setEnabled(not mag_lockout)
        self.btn_mag_en.setToolTip(mag_tt if mag_lockout else "Click twice to switch the magnet ON (confirm); once to switch it OFF.")

        # --- Dynamic Control Lockouts & Tooltips ---
        if is_sweeping:
            # Overwrite all tooltips if a sweep is actively running
            sweep_lock_msg = "Disabled: Sweep is currently active"

            self.btn_start_scan.setEnabled(False)
            self.btn_start_scan.setToolTip(sweep_lock_msg)

            # ADDED: Enable Stop button during sweep
            self.btn_stop_scan.setEnabled(True)
            self.btn_stop_scan.setToolTip("Click to stop the active sweep")

            self.sp_mag_current.setEnabled(False)
            self.sp_mag_current.setToolTip(sweep_lock_msg)
            self.btn_mag_deg.setEnabled(False)
            self.btn_mag_deg.setToolTip(sweep_lock_msg)
            self.btn_calc_mass.setEnabled(False)
            self.btn_calc_mass.setToolTip(sweep_lock_msg)
            self.sp_mass.setEnabled(False)
            self.sp_mass.setToolTip(sweep_lock_msg)
        else:
            # Revert to hardware-based lockouts and standard tooltips
            self.btn_start_scan.setEnabled(not mag_lockout)
            self.btn_start_scan.setToolTip(
                f"Disabled: {mag_lock_reason}" if mag_lockout else "Click to start the automated mass scan sweep")

            # ADDED: Disable Stop button when NOT sweeping
            self.btn_stop_scan.setEnabled(False)
            self.btn_stop_scan.setToolTip("Disabled: No sweep currently active")

            self.sp_mag_current.setEnabled(not mag_lockout)
            self.sp_mag_current.setToolTip(
                f"Disabled: {mag_lock_reason}" if mag_lockout else "Set manual magnet current")

            self.btn_mag_deg.setEnabled(not mag_lockout)
            self.btn_mag_deg.setToolTip(
                f"Disabled: {mag_lock_reason}" if mag_lockout else "Click to trigger autonomous degaussing sequence")

            self.btn_calc_mass.setEnabled(not mag_lockout)
            self.btn_calc_mass.setToolTip(
                f"Disabled: {mag_lock_reason}" if mag_lockout else
                "Set the magnet current for the target mass, from the Mass Scan panel's calibration (k, I_off) "
                "and the live beam energy. Hysteresis: the scan calibrates on the way up, so approach from "
                "below (or degauss first) for the best match.")

            self.sp_mass.setEnabled(not mag_lockout)
            self.sp_mass.setToolTip(
                f"Disabled: {mag_lock_reason}" if mag_lockout else "Target mass for auto-tuning")

        if self.arm_deg.armed:
            pass  # waiting for the confirming click
        elif mag_deg:
            self.btn_mag_deg.setText("DEGAUSSING...")
            self.btn_mag_deg.setStyleSheet("background-color: #E91E63; color: white; font-weight: bold;")
        else:
            self.btn_mag_deg.setText("DEGAUSS ROUTINE")
            self.btn_mag_deg.setStyleSheet("background-color: #9C27B0; color: white; font-weight: bold;")

        # --- 2. Update Steerers ---
        # PLC Telemetry handling (Y-Axis)
        y_steer_en = bool(data.get("ion_beam.beamline.steering.stat_enabled", False))
        y_rb = data.get("ion_beam.beamline.steering.sp_actual_y_volts")

        self.lbl_y_rb.setText(f"RB: {y_rb:.1f} V" if y_rb is not None else "RB: --- V")
        if y_rb is not None and y_steer_en and not self.sp_y_steer.hasFocus():
            self.sp_y_steer.blockSignals(True)
            self.sp_y_steer.setValue(float(y_rb))
            self.sp_y_steer.blockSignals(False)

        # Microservice Telemetry handling (X-Axis Magnet)
        x_steer_en = bool(data.get("ion_beam.beamline.steerer_magnet.stat_enabled", False))
        x_v_rb = data.get("ion_beam.beamline.steerer_magnet.rb_voltage")
        x_i_rb = data.get("ion_beam.beamline.steerer_magnet.rb_current")
        x_i_sp = data.get("ion_beam.beamline.steerer_magnet.sp_actual_current")

        if x_i_rb is not None and x_v_rb is not None:
            self.lbl_x_rb.setText(f"RB: {x_i_rb:.2f} A | {x_v_rb:.1f} V")
        else:
            self.lbl_x_rb.setText("RB: --- A | --- V")
        _show_tolerance(self.lbl_x_rb, data, ("ion_beam.beamline.steerer_magnet.rb_current",))

        if x_i_sp is not None and x_steer_en and not self.sp_x_steer.hasFocus():
            self.sp_x_steer.blockSignals(True)
            self.sp_x_steer.setValue(float(x_i_sp))
            self.sp_x_steer.blockSignals(False)

        # Master enable button shows OK only if BOTH steerers are verified active
        both_steerers_en = y_steer_en and x_steer_en
        steer_known = ("ion_beam.beamline.steering.stat_enabled" in data
                       and "ion_beam.beamline.steerer_magnet.stat_enabled" in data)
        self.arm_steer.show_state(both_steerers_en if steer_known else None, COLOR_OK, COLOR_BUTTON_STANDARD,
                                  "Steering state unknown - no current data from the PLC or steerer service.")

        steer_y_ctrl_mode = data.get("ion_beam.beamline.steering.y.rb_ctrl_mode", 0.0)
        x_steer_ctrl_mode = data.get("ion_beam.beamline.steerer_magnet.rb_ctrl_mode", 0.0)
        steer_auto_locked = (steer_y_ctrl_mode > 0.0) or (x_steer_ctrl_mode > 0.0)

        # Heartbeat ID extracted from provided microservice code
        steer_magnet_state = services.get("service_steerer_magnet_psu", "OFFLINE")

        # Steerer Lockouts
        steer_lock_reason = ""
        if steer_magnet_state != "ONLINE":
            steer_lock_reason = "Steerer magnet microservice is offline."
        elif master_comms_lost:
            steer_lock_reason = "PLC communications are offline."
        elif not relay_active:
            steer_lock_reason = "Safety Relay is De-Energized."
        elif steer_auto_locked:
            steer_lock_reason = "Locked by automated sequencer."

        steer_lockout = bool(steer_lock_reason)

        self.btn_steer_en.setEnabled(not steer_lockout)
        self.btn_steer_en.setToolTip(
            f"Disabled: {steer_lock_reason}" if steer_lockout else "Enable Beam Steerer Outputs")
        self.sp_y_steer.setEnabled(not steer_lockout)
        self.sp_y_steer.setToolTip(
            f"Disabled: {steer_lock_reason}" if steer_lockout else "Adjust Y-Axis Beam Deflection")
        self.sp_x_steer.setEnabled(not steer_lockout)
        self.sp_x_steer.setToolTip(
            f"Disabled: {steer_lock_reason}" if steer_lockout else "Adjust X-Axis Magnet Current")

        # --- 3. Update Manual Slit Inputs ---
        slit_lock_reason = "Events microservice is offline (Cannot log manual adjustments)."
        slit_lockout = (events_state != "ONLINE")
        slit_tt = f"Disabled: {slit_lock_reason}" if slit_lockout else "Update Database Log with physical slit dimensions"

        for slit_sp in [self.sp_obj_l, self.sp_obj_r, self.sp_img_l, self.sp_img_r]:
            slit_sp.setEnabled(not slit_lockout)
            slit_sp.setToolTip(slit_tt)

        # --- 4. Update Spellman Downstream Optics ---
        spellman_ctrl_mode = data.get("ion_beam.source.einzel.rb_ctrl_mode", 0.0) # change this tag when actual tags for these spellmans are added
        spellman_auto_locked = (spellman_ctrl_mode > 0.0)

        spell_lock_reason = ""
        if spellman_state != "ONLINE":
            spell_lock_reason = "Spellman microservice is offline."
        elif master_comms_lost:
            spell_lock_reason = "Safety Relay state is unknown (PLC offline)."
        elif not relay_active:
            spell_lock_reason = "Safety Relay is De-Energized."
        elif spellman_auto_locked:
            spell_lock_reason = "Locked by automated sequencer."

        spell_lockout = bool(spell_lock_reason)
        spell_tt = f"Disabled: {spell_lock_reason}" if spell_lockout else ""

        for name, ctrl in self.psu_controls.items():
            base_tag = ctrl["base_tag"]

            rb_v = data.get(f"{base_tag}.rb_voltage")
            rb_i = data.get(f"{base_tag}.rb_current")
            v_str = f"{rb_v:.2f}" if rb_v is not None else "---"
            i_str = f"{rb_i:.2f}" if rb_i is not None else "---"
            ctrl["lbl_rb"].setText(f"RB: {v_str} {ctrl['pri_unit']} | {i_str} {ctrl['sec_unit']}")
            _show_tolerance(ctrl["lbl_rb"], data, (f"{base_tag}.rb_voltage", f"{base_tag}.rb_current"))

            sp_val = data.get(f"{base_tag}.sp_actual_voltage")
            if sp_val is not None and not ctrl["sp_box"].hasFocus():
                ctrl["sp_box"].blockSignals(True)
                ctrl["sp_box"].setValue(float(sp_val))
                ctrl["sp_box"].blockSignals(False)

            stat_en = data.get(f"{base_tag}.stat_enabled")
            ctrl["btn_enable"].setEnabled(not spell_lockout)
            ctrl["btn_enable"].setToolTip(spell_tt if spell_lockout
                                          else "Click twice to switch ON (confirm); once to switch OFF")
            ctrl["arm"].show_state(None if stat_en is None else bool(stat_en), COLOR_OK, COLOR_BUTTON_STANDARD,
                                   f"{name} output state unknown - supply not answering or not configured.")
            ctrl["sp_box"].setEnabled(not spell_lockout)
            ctrl["sp_box"].setToolTip(spell_tt)
