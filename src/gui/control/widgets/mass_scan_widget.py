import time
import os
import csv
import math
from datetime import datetime
import numpy as np
import pyqtgraph as pg

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QPushButton, QGroupBox, QDoubleSpinBox, QSpinBox, QCheckBox,
    QMessageBox, QSplitter, QTableWidget, QTableWidgetItem, QHeaderView,
    QLineEdit, QFileDialog
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal


# --- Custom Axis for Smart Current Formatting ---
class SmartCurrentAxis(pg.AxisItem):
    """Dynamically formats axis ticks into pA, nA, µA, mA based on magnitude."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Prevent PyQtGraph from automatically appending "x1e-06" to the axis label
        self.enableAutoSIPrefix(False)

    def tickStrings(self, values, scale, spacing):
        strings = []
        for v in values:
            try:
                # If the plot is in LogMode, the values given to the axis are actually log10(v).
                # We must exponentiate them back to absolute current to format them properly.
                val = 10 ** float(v) if self.logMode else float(v)

                abs_val = abs(val)
                if abs_val == 0:
                    strings.append("0 A")
                elif abs_val >= 1:
                    strings.append(f"{val:.2f} A")
                elif abs_val >= 1e-3:
                    strings.append(f"{val * 1e3:.1f} mA")
                elif abs_val >= 1e-6:
                    strings.append(f"{val * 1e6:.1f} µA")
                elif abs_val >= 1e-9:
                    strings.append(f"{val * 1e9:.1f} nA")
                else:
                    strings.append(f"{val * 1e12:.1f} pA")
            except:
                strings.append("")
        return strings


# --- State Machine Worker ---
class PrecisionScanWorker(QThread):
    data_point = pyqtSignal(float, float)
    scan_complete = pyqtSignal(list)
    status_msg = pyqtSignal(str)
    progress = pyqtSignal(int, int)  # Current Step, Total Steps

    def __init__(self, cmd_thread, get_telemetry_cb, event_helper, config: dict):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.get_telemetry_cb = get_telemetry_cb
        self.event_helper = event_helper

        self.start_a = config['start']
        self.stop_a = config['stop']
        self.step_a = config['step']
        self.tolerance = config['tolerance']
        self.settle_time = config['settle_time']
        self.samples = config['samples']
        self.sample_interval = config['sample_interval']
        self.loop_scan = config['loop']

        self.is_stopped = False

        self.mode_tag = "ion_beam.beamline.magnet.cmd_ctrl_mode"
        self.sp_tag = "ion_beam.beamline.magnet.sp_requested_current"
        self.rb_mag_tag = "ion_beam.beamline.magnet.rb_current"
        self.rb_beam_tag = "ion_beam.beamline.faraday.smu.rb_current"
        self.target_kv_tag = "ion_beam.source.target.rb_voltage"
        self.ext_kv_tag = "ion_beam.source.extraction.rb_voltage"

    def run(self):
        try:
            self.event_helper.log_general(f"Mass Scan started: {self.start_a}A to {self.stop_a}A")
            self.status_msg.emit("Seizing Control (Mode=1)...")
            self.cmd_thread.send_command("magnet", self.mode_tag, 1, origin="mass_scan")
            time.sleep(0.5)

            num_steps = int(round(abs(self.stop_a - self.start_a) / self.step_a)) + 1

            # Sawtooth Logic: Vector is always generated A -> B regardless of loop iteration
            points = np.linspace(self.start_a, self.stop_a, num_steps)

            while not self.is_stopped:
                self.status_msg.emit("Executing Precision Sweep...")
                scan_results = []

                for idx, sp_amps in enumerate(points):
                    if self.is_stopped: break

                    self.status_msg.emit(f"Moving to {sp_amps:.3f} A...")
                    self.cmd_thread.send_command("magnet", self.sp_tag, float(sp_amps), origin="mass_scan")

                    # Dynamic Settling
                    timeout_start = time.time()
                    while time.time() - timeout_start < 5.0:
                        if self.is_stopped: break
                        if abs(self.get_telemetry_cb(self.rb_mag_tag) - sp_amps) <= self.tolerance:
                            break
                        time.sleep(0.05)

                    if self.is_stopped: break

                    # Static Settling
                    time.sleep(self.settle_time)

                    # Acquisition
                    mag_buffer, beam_buffer, target_buffer, ext_buffer = [], [], [], []
                    for _ in range(self.samples):
                        if self.is_stopped: break
                        mag_buffer.append(self.get_telemetry_cb(self.rb_mag_tag))
                        beam_buffer.append(self.get_telemetry_cb(self.rb_beam_tag))
                        target_buffer.append(self.get_telemetry_cb(self.target_kv_tag))
                        ext_buffer.append(self.get_telemetry_cb(self.ext_kv_tag))
                        time.sleep(self.sample_interval)

                    if self.is_stopped: break

                    # Truncated Aggregation
                    if self.samples >= 10:
                        trim_idx = max(1, int(self.samples * 0.1))
                        mag_buffer = mag_buffer[trim_idx:-trim_idx]
                        beam_buffer = beam_buffer[trim_idx:-trim_idx]
                        target_buffer = target_buffer[trim_idx:-trim_idx]
                        ext_buffer = ext_buffer[trim_idx:-trim_idx]

                    mean_mag = float(np.mean(mag_buffer))
                    mean_beam = float(np.mean(beam_buffer))
                    mean_target_kv = float(np.mean(target_buffer))
                    mean_ext_kv = float(np.mean(ext_buffer))

                    scan_results.append((sp_amps, mean_mag, mean_beam, mean_target_kv, mean_ext_kv))
                    self.data_point.emit(mean_mag, mean_beam)
                    self.progress.emit(idx + 1, num_steps)

                if not self.is_stopped:
                    self.scan_complete.emit(scan_results)

                if not self.loop_scan or self.is_stopped:
                    break

                # Reset: Flyback to the start position before iterating the while-loop again
                self.status_msg.emit("Resetting to Start Position...")
                self.cmd_thread.send_command("magnet", self.sp_tag, float(points[0]), origin="mass_scan")

                reset_start = time.time()
                while time.time() - reset_start < 2.0:
                    if self.is_stopped: break
                    time.sleep(0.05)

        except Exception as e:
            print(f"[MassScanWorker] FATAL: {e}")
        finally:
            self.event_helper.log_general("Mass Scan stopped/finished.")
            self.status_msg.emit("Releasing Control...")
            self.cmd_thread.send_command("magnet", self.mode_tag, 0, origin="mass_scan")

    def stop(self):
        self.is_stopped = True


class PrecisionMassScannerWidget(QWidget):
    def __init__(self, cmd_thread, get_telemetry_cb, event_helper):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.get_telemetry_cb = get_telemetry_cb
        self.event_helper = event_helper
        self.worker = None
        self.time_per_point = 0.0
        self.sp_energy = 0

        self._init_ui()
        self._update_eta()

    def _init_ui(self):
        self.main_layout = QHBoxLayout(self)

        # --- LEFT PANEL: Configuration ---
        self.control_panel_widget = QWidget()
        self.control_panel_widget.setFixedWidth(340)
        control_panel = QVBoxLayout(self.control_panel_widget)
        control_panel.setContentsMargins(0, 0, 0, 0)

        # 1. Sweep Parameters
        grp_sweep = QGroupBox("Sweep Parameters")
        layout_sweep = QGridLayout()

        self.sp_start = QDoubleSpinBox()
        self.sp_start.setRange(0.0, 100.0)
        self.sp_start.setDecimals(3)
        self.sp_start.setSuffix(" A")
        self.sp_start.setValue(0.0)
        self.sp_start.setToolTip("The starting current of the mass analyzer magnet sweep.")
        self.sp_start.valueChanged.connect(self._update_eta)

        self.sp_stop = QDoubleSpinBox()
        self.sp_stop.setRange(0.0, 100.0)
        self.sp_stop.setDecimals(3)
        self.sp_stop.setSuffix(" A")
        self.sp_stop.setValue(10.0)
        self.sp_stop.setToolTip("The final target current of the sweep. (Can be lower than Start for reverse scans).")
        self.sp_stop.valueChanged.connect(self._update_eta)

        self.sp_step = QDoubleSpinBox()
        self.sp_step.setRange(0.001, 10.0)
        self.sp_step.setDecimals(3)
        self.sp_step.setSuffix(" A")
        self.sp_step.setValue(0.02)
        self.sp_step.setToolTip("The absolute resolution of each current step.")
        self.sp_step.valueChanged.connect(self._update_eta)

        layout_sweep.addWidget(QLabel("Start:"), 0, 0)
        layout_sweep.addWidget(self.sp_start, 0, 1)
        layout_sweep.addWidget(QLabel("Stop:"), 1, 0)
        layout_sweep.addWidget(self.sp_stop, 1, 1)
        layout_sweep.addWidget(QLabel("Step:"), 2, 0)
        layout_sweep.addWidget(self.sp_step, 2, 1)
        grp_sweep.setLayout(layout_sweep)
        control_panel.addWidget(grp_sweep)

        # 2. Acquisition Settings
        grp_acq = QGroupBox("Acquisition / Settling")
        layout_acq = QGridLayout()

        self.sp_tol = QDoubleSpinBox()
        self.sp_tol.setRange(0.001, 1.0)
        self.sp_tol.setDecimals(3)
        self.sp_tol.setSuffix(" A")
        self.sp_tol.setValue(0.05)
        self.sp_tol.setToolTip(
            "The acceptable difference between the requested setpoint and physical readback before static settling begins.")

        self.sp_settle = QDoubleSpinBox()
        self.sp_settle.setRange(0.0, 5.0)
        self.sp_settle.setDecimals(2)
        self.sp_settle.setSuffix(" s")
        self.sp_settle.setValue(0.1)
        self.sp_settle.setToolTip(
            "Wait time after reaching the tolerance threshold. Allows magnetic hysteresis to physically stabilize.")
        self.sp_settle.valueChanged.connect(self._update_eta)

        self.sp_samples = QSpinBox()
        self.sp_samples.setRange(1, 1000)
        self.sp_samples.setValue(3)
        self.sp_samples.setToolTip("Number of Faraday cup current measurements to take at each magnet step.")
        self.sp_samples.valueChanged.connect(self._update_eta)

        self.sp_interval = QDoubleSpinBox()
        self.sp_interval.setRange(0.01, 1.0)
        self.sp_interval.setDecimals(3)
        self.sp_interval.setSuffix(" s")
        self.sp_interval.setValue(0.02)
        self.sp_interval.setToolTip("Delay between each discrete sample acquisition.")
        self.sp_interval.valueChanged.connect(self._update_eta)

        layout_acq.addWidget(QLabel("Settle Tol:"), 0, 0)
        layout_acq.addWidget(self.sp_tol, 0, 1)
        layout_acq.addWidget(QLabel("Wait Time:"), 1, 0)
        layout_acq.addWidget(self.sp_settle, 1, 1)
        layout_acq.addWidget(QLabel("Samples:"), 2, 0)
        layout_acq.addWidget(self.sp_samples, 2, 1)
        layout_acq.addWidget(QLabel("Interval:"), 3, 0)
        layout_acq.addWidget(self.sp_interval, 3, 1)
        grp_acq.setLayout(layout_acq)
        control_panel.addWidget(grp_acq)

        # 3. Execution & Export
        grp_exec = QGroupBox("Execution & Output")
        layout_exec = QVBoxLayout()

        self.chk_loop = QCheckBox("Continuous Loop")
        self.chk_loop.setToolTip(
            "Instead of stopping at the end, the scanner jumps back to the start and sweeps again, ghosting the previous trace.")

        self.chk_export = QCheckBox("Auto-Export CSV on Complete")
        self.chk_export.setChecked(True)
        self.chk_export.setToolTip("Save the trace data. Filename will auto-append the timestamp.")

        # Filepath selector
        export_layout = QHBoxLayout()
        self.edit_export_path = QLineEdit()
        default_export = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../data/exports/Mass_Scans", "MassScan"))
        self.edit_export_path.setText(default_export)
        self.edit_export_path.setToolTip("Base path and filename. A timestamp will be appended automatically.")

        btn_browse = QPushButton("...")
        btn_browse.setMaximumWidth(30)
        btn_browse.clicked.connect(self._browse_export_path)
        export_layout.addWidget(self.edit_export_path)
        export_layout.addWidget(btn_browse)

        self.lbl_eta = QLabel("Est. Scan Time: --:--")
        self.lbl_eta.setStyleSheet("color: #2196F3; font-weight: bold;")
        self.lbl_rem_time = QLabel("Remaining: --:--")
        self.lbl_rem_time.setStyleSheet("color: #FF9800; font-weight: bold;")

        self.btn_start = QPushButton("START PRECISION SWEEP")
        self.btn_start.setStyleSheet(
            "background-color: #4CAF50; color: white; font-weight: bold; padding: 12px; border-radius: 4px;")
        self.btn_start.clicked.connect(self._start_scan)

        self.btn_stop = QPushButton("ABORT")
        self.btn_stop.setStyleSheet(
            "background-color: #555555; color: white; font-weight: bold; padding: 12px; border-radius: 4px;")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_scan)

        layout_exec.addWidget(self.chk_loop)
        layout_exec.addWidget(self.chk_export)
        layout_exec.addWidget(QLabel("Base Export Path:"))
        layout_exec.addLayout(export_layout)

        time_layout = QHBoxLayout()
        time_layout.addWidget(self.lbl_eta)
        time_layout.addWidget(self.lbl_rem_time)
        layout_exec.addLayout(time_layout)

        layout_exec.addWidget(self.btn_start)
        layout_exec.addWidget(self.btn_stop)

        self.lbl_status = QLabel("Status: Idle")
        self.lbl_status.setStyleSheet("font-family: monospace; color: #FF9800;")
        layout_exec.addWidget(self.lbl_status)

        grp_exec.setLayout(layout_exec)
        control_panel.addWidget(grp_exec)
        control_panel.addStretch()

        # --- CENTER PANEL: Plotting ---
        plot_panel_widget = QWidget()
        plot_layout = QVBoxLayout(plot_panel_widget)
        plot_layout.setContentsMargins(0, 0, 0, 0)

        # Toolbar
        tb_layout = QHBoxLayout()

        self.btn_toggle_ctrl = QPushButton("◀ Setup")
        self.btn_toggle_ctrl.clicked.connect(self._toggle_left_panel)
        tb_layout.addWidget(self.btn_toggle_ctrl)

        self.chk_log_y = QCheckBox("Logarithmic Y-Axis")
        self.chk_log_y.setToolTip("Switch the Beam Current axis to log10 scale. (Note: clips values <= 0)")
        self.chk_log_y.stateChanged.connect(self._toggle_log_y)
        tb_layout.addWidget(self.chk_log_y)

        tb_layout.addStretch()

        self.btn_clear = QPushButton("Clear Plot")
        self.btn_clear.clicked.connect(self._clear_plot)
        tb_layout.addWidget(self.btn_clear)

        self.btn_toggle_analysis = QPushButton("Analysis ▶")
        self.btn_toggle_analysis.clicked.connect(self._toggle_right_panel)
        tb_layout.addWidget(self.btn_toggle_analysis)

        plot_layout.addLayout(tb_layout)

        pg.setConfigOption('background', '#1E1E1E')
        pg.setConfigOption('foreground', 'w')

        smart_axis = SmartCurrentAxis(orientation='left')
        self.plot_widget = pg.PlotWidget(axisItems={'left': smart_axis})
        self.plot_widget.setLabel('bottom', "Magnet Current (A)")
        self.plot_widget.setLabel('left', "Beam Current")
        self.plot_widget.showGrid(x=True, y=True, alpha=0.5)

        # Double click connection
        self.plot_widget.scene().sigMouseClicked.connect(self._on_mouse_clicked)

        self.x_data, self.y_data = [], []

        # Ghost trace uses a dimmer, dashed styling
        self.curve_ghost = self.plot_widget.plot(pen=pg.mkPen('#005577', width=1, style=Qt.PenStyle.DashLine),
                                                 symbol='o', symbolSize=3, symbolBrush='#005577')
        self.curve_live = self.plot_widget.plot(pen=pg.mkPen('#00E5FF', width=2), symbol='o', symbolSize=5,
                                                symbolBrush='#00E5FF')
        # Peak detection markers (yellow triangles or crosses over detected peaks)
        self.peak_markers = self.plot_widget.plot(pen=None, symbol='t1', symbolSize=10, symbolBrush='#FFEB3B',
                                                  symbolPen='k')

        plot_layout.addWidget(self.plot_widget)

        # --- RIGHT PANEL: Analysis & Calibration ---
        self.analysis_panel_widget = QWidget()
        self.analysis_panel_widget.setFixedWidth(320)
        analysis_layout = QVBoxLayout(self.analysis_panel_widget)
        analysis_layout.setContentsMargins(0, 0, 0, 0)

        # Load CSV Feature
        self.btn_load_csv = QPushButton("📂 Load CSV Trace")
        self.btn_load_csv.setToolTip("Load a previously exported Mass Scan CSV to display as a background trace.")
        self.btn_load_csv.setStyleSheet(
            "background-color: #607D8B; color: white; font-weight: bold; padding: 6px; border-radius: 4px;")
        self.btn_load_csv.clicked.connect(self._load_csv)
        analysis_layout.addWidget(self.btn_load_csv)

        grp_calib = QGroupBox("Mass Calibration")
        calib_layout = QGridLayout()

        self.sp_k_factor = QDoubleSpinBox()
        self.sp_k_factor.setRange(0.0001, 10000.0)
        self.sp_k_factor.setDecimals(4)
        self.sp_k_factor.setValue(12.5)
        self.sp_k_factor.setToolTip("Empirical calibration constant k in: AMU = k * (I^2 / E)")

        calib_layout.addWidget(QLabel("Beam Energy (E):"), 0, 0)
        calib_layout.addWidget(QLabel(f"{self.sp_energy} keV)"), 0, 1)
        calib_layout.addWidget(QLabel("Calib. Factor (k):"), 1, 0)
        calib_layout.addWidget(self.sp_k_factor, 1, 1)
        grp_calib.setLayout(calib_layout)
        analysis_layout.addWidget(grp_calib)

        self.btn_table = QPushButton("🧪 Open Periodic Table")
        self.btn_table.setToolTip("Future Feature: Interactive periodic table for isotope selection.")
        self.btn_table.setEnabled(False)
        calib_layout.addWidget(self.btn_table)
        grp_calib.setLayout(calib_layout)
        analysis_layout.addWidget(grp_calib)

        # 2. Peak Detection Group
        grp_peaks = QGroupBox("Peak Detection")
        peaks_layout = QVBoxLayout()

        param_layout = QHBoxLayout()
        self.sp_max_peaks = QSpinBox()
        self.sp_max_peaks.setRange(1, 50)
        self.sp_max_peaks.setValue(5)
        self.sp_max_peaks.setToolTip("Maximum number of peaks to isolate (sorted by prominence).")
        param_layout.addWidget(QLabel("Max Peaks:"))
        param_layout.addWidget(self.sp_max_peaks)
        peaks_layout.addLayout(param_layout)

        self.btn_detect = QPushButton("🔍 Auto-Detect Peaks")
        self.btn_detect.setStyleSheet(
            "background-color: #2196F3; color: white; font-weight: bold; padding: 8px; border-radius: 4px;")
        self.btn_detect.clicked.connect(self._run_peak_detection)

        self.peak_table = QTableWidget(0, 4)
        self.peak_table.setHorizontalHeaderLabels(["Amps", "AMU", "FWHM (A)", "M/ΔM"])
        self.peak_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)

        self.lbl_res = QLabel("<b>Avg Mass Resolution:</b> ---")
        self.lbl_res.setStyleSheet("font-size: 11px;")

        peaks_layout.addWidget(self.btn_detect)
        peaks_layout.addWidget(self.peak_table)
        peaks_layout.addWidget(self.lbl_res)
        grp_peaks.setLayout(peaks_layout)
        analysis_layout.addWidget(grp_peaks)

        # Combine all panels using a Splitter
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.control_panel_widget)
        splitter.addWidget(plot_panel_widget)
        splitter.addWidget(self.analysis_panel_widget)
        splitter.setSizes([340, 800, 300])

        self.main_layout.addWidget(splitter)
        self._lock_ui(False)

    def _run_peak_detection(self):
        # 1. Ensure we have data to analyze
        if len(self.x_data) < 10 or len(self.y_data) < 10:
            QMessageBox.warning(self, "No Data", "Not enough data points on screen to perform peak detection.")
            return

        try:
            from scipy.signal import find_peaks, peak_widths

            x_arr = np.array(self.x_data)
            y_arr = np.array(self.y_data)

            # If data is noisy or baseline fluctuates around zero, clip negative values
            y_clean = np.clip(y_arr, 0, None)

            # 2. Find peaks with a minimal prominence threshold (3% of total signal range)
            signal_range = np.max(y_clean) - np.min(y_clean)
            min_prominence = max(1e-13, signal_range * 0.03)

            peaks, properties = find_peaks(y_clean, prominence=min_prominence)

            # If no peaks found, visually alert the user and clear table
            if len(peaks) == 0:
                self.peak_table.setRowCount(0)
                self.lbl_res.setText(
                    "<span style='color: #F44336; font-weight: bold;'>No peaks detected! Try adjusting scan range or noise threshold.</span>")
                return

            # 3. Sort by prominence descending and cap at user's Max Peaks setting
            prominences = properties["prominences"]
            sorted_indices = np.argsort(prominences)[::-1]
            top_indices = sorted_indices[:self.sp_max_peaks.value()]
            selected_peaks = peaks[top_indices]

            # Sort again by X-coordinate (Amps) so the table reads left-to-right cleanly
            selected_peaks = np.sort(selected_peaks)

            # 4. Calculate FWHM at half maximum (rel_height=0.5)
            widths, width_heights, left_ips, right_ips = peak_widths(y_clean, selected_peaks, rel_height=0.5)

            # Convert fractional index widths into physical current widths (Delta Amps)
            step_size = abs(x_arr[1] - x_arr[0]) if len(x_arr) > 1 else self.sp_step.value()
            fwhm_amps = widths * step_size

            # 5. Populate Table & Calculate Mass Resolution
            self.peak_table.setRowCount(len(selected_peaks))

            k = self.sp_k_factor.value()
            energy_kev = self.sp_energy
            energy_ev = energy_kev * 1000.0

            total_resolution = 0.0
            valid_peaks_count = 0

            for row_idx, (p_idx, fwhm_a) in enumerate(zip(selected_peaks, fwhm_amps)):
                center_a = x_arr[p_idx]

                # Calibration: AMU = k * (I^2 / E)
                est_amu = k * ((center_a ** 2) / energy_ev)

                # Resolution: R = I / (2 * Delta_I)
                res = center_a / (2.0 * fwhm_a) if fwhm_a > 0 else 0.0

                if res > 0:
                    total_resolution += res
                    valid_peaks_count += 1

                # Populate table cells
                self.peak_table.setItem(row_idx, 0, QTableWidgetItem(f"{center_a:.3f}"))
                self.peak_table.setItem(row_idx, 1, QTableWidgetItem(f"{est_amu:.2f}"))
                self.peak_table.setItem(row_idx, 2, QTableWidgetItem(f"{fwhm_a:.4f}"))
                self.peak_table.setItem(row_idx, 3, QTableWidgetItem(f"{res:.1f}"))

            # 6. Display Average Resolution
            avg_res = total_resolution / valid_peaks_count if valid_peaks_count > 0 else 0.0
            self.lbl_res.setText(
                f"<b>Avg Mass Resolution (M/ΔM):</b> <span style='color: #4CAF50;'>{avg_res:.1f}</span>")

            # Optional: Highlight detected peaks on the plot with markers
            self._draw_peak_markers(x_arr[selected_peaks], y_arr[selected_peaks])

        except ImportError:
            QMessageBox.critical(self, "Dependency Error",
                                 "The 'scipy' library is required for peak detection.\nPlease install it via: pip install scipy")
        except Exception as e:
            QMessageBox.critical(self, "Analysis Error", f"Peak detection failed:\n{e}")

    def _draw_peak_markers(self, x_vals, y_vals):
        self.peak_markers.setData(x_vals, y_vals)


    # --- UI Toggle Methods ---
    def _toggle_left_panel(self):
        visible = not self.control_panel_widget.isVisible()
        self.control_panel_widget.setVisible(visible)
        self.btn_toggle_ctrl.setText("◀ Setup" if visible else "Setup ▶")

    def _toggle_right_panel(self):
        visible = not self.analysis_panel_widget.isVisible()
        self.analysis_panel_widget.setVisible(visible)
        self.btn_toggle_analysis.setText("Analysis ▶" if visible else "◀ Analysis")

    def _browse_export_path(self):
        current_dir = os.path.dirname(self.edit_export_path.text())
        file_path, _ = QFileDialog.getSaveFileName(self, "Select Base Export File", current_dir, "CSV Files (*.csv)")
        if file_path:
            # Strip the .csv if they typed it, so we can append the timestamp cleanly
            if file_path.endswith('.csv'):
                file_path = file_path[:-4]
            self.edit_export_path.setText(file_path)

    def _load_csv(self):
        current_dir = os.path.dirname(self.edit_export_path.text())
        filepath, _ = QFileDialog.getOpenFileName(self, "Load Mass Scan CSV", current_dir, "CSV Files (*.csv)")

        if not filepath: return

        try:
            x_vals, y_vals = [], []
            target_kv, ext_kv = 0.0, 0.0

            with open(filepath, 'r') as f:
                reader = csv.DictReader(f)
                for i, row in enumerate(reader):
                    mag = float(row.get('Measured_Magnet_A', 0))
                    beam = float(row.get('Measured_Beam_A', 0))
                    x_vals.append(mag)
                    y_vals.append(beam)

                    # Capture voltages from the first data row (safe fallback for legacy CSVs without these columns)
                    if i == 0:
                        target_kv = float(row.get('Target_kV', 0) or 0.0)
                        ext_kv = float(row.get('Extraction_kV', 0) or 0.0)

            self.x_data = x_vals
            self.y_data = y_vals
            self.curve_live.setData(self.x_data, self.y_data)

            total_energy_kev = target_kv + ext_kv
            if total_energy_kev > 0:
                self.sp_energy = total_energy_kev
                self.lbl_status.setText(f"Status: Loaded {os.path.basename(filepath)} ({total_energy_kev:.2f} keV)")
            else:
                self.lbl_status.setText(f"Status: Loaded {os.path.basename(filepath)}")

        except Exception as e:
            QMessageBox.critical(self, "Load Error", f"Failed to parse CSV:\n{e}")

    def _update_eta(self):
        try:
            num_steps = int(round(abs(self.sp_stop.value() - self.sp_start.value()) / self.sp_step.value())) + 1
            self.time_per_point = 0.05 + self.sp_settle.value() + (self.sp_samples.value() * self.sp_interval.value())

            total_sec = num_steps * self.time_per_point
            m, s = divmod(int(total_sec), 60)
            self.lbl_eta.setText(f"Est. Scan Time: {m:02d}:{s:02d}")
        except Exception:
            self.lbl_eta.setText("Est. Scan Time: Error")

    def _update_progress(self, current_step: int, total_steps: int):
        rem_sec = (total_steps - current_step) * self.time_per_point
        m, s = divmod(int(max(0, rem_sec)), 60)
        self.lbl_rem_time.setText(f"Remaining: {m:02d}:{s:02d}")

    def _on_mouse_clicked(self, evt):
        if evt.double() and not (self.worker and self.worker.isRunning()):
            evt.accept()
            pos = evt.scenePos()
            if self.plot_widget.vb.sceneBoundingRect().contains(pos):
                mouse_point = self.plot_widget.vb.mapSceneToView(pos)
                target_a = mouse_point.x()
                self.cmd_thread.send_command("magnet", "ion_beam.beamline.magnet.sp_requested_current", float(target_a),
                                             origin="hmi")
                self.lbl_status.setText(f"Manual Jump to {target_a:.3f} A")
                self.event_helper.log_user_marker(time.time(),
                                                  f"Manual Magnet Jump: {target_a:.3f} A via Mass Scan Widget",
                                                  "#9C27B0")

    def update_telemetry(self, data: dict):
        is_sweeping = self.worker is not None and self.worker.isRunning()

        lock_reason = ""
        if not data.get("system.connected"):
            lock_reason = "PLC communications offline."
        elif not data.get("ion_beam.facilities.safety_relay_active"):
            lock_reason = "Safety Relay is De-Energized."
        else:
            services = data.get("manager.services", {})
            if services.get("service_magnet_psu", "OFFLINE") != "ONLINE":
                lock_reason = "Magnet service is offline."
            else:
                enable_val = data.get("ion_beam.beamline.magnet.stat_enabled")
                if enable_val is not None and not bool(enable_val):
                    lock_reason = "Magnet PSU must be enabled."

        if is_sweeping and lock_reason:
            self._stop_scan()

        if not is_sweeping:
            lockout = bool(lock_reason)
            self.btn_start.setEnabled(not lockout)
            self.btn_start.setStyleSheet(
                "background-color: #555555; color: #888888; font-weight: bold; padding: 12px; border-radius: 4px;" if lockout
                else "background-color: #4CAF50; color: white; font-weight: bold; padding: 12px; border-radius: 4px;"
            )
            self.btn_start.setToolTip(
                f"Disabled: {lock_reason}" if lockout else "Click to begin the precision mass scan sweep.")

    def _start_scan(self):
        if self.sp_start.value() == self.sp_stop.value():
            QMessageBox.warning(self, "Invalid Parameters", "Start and Stop values cannot be identical.")
            return

        # Auto-update beam energy from live telemetry before starting
        live_target = float(self.get_telemetry_cb("ion_beam.source.target.rb_voltage") or 0.0)
        live_ext = float(self.get_telemetry_cb("ion_beam.source.extraction.rb_voltage") or 0.0)
        live_energy_kev = live_target + live_ext
        if live_energy_kev > 0:
            self.sp_energy = live_energy_kev

        self._clear_plot()
        self._lock_ui(True)

        config = {
            'start': self.sp_start.value(), 'stop': self.sp_stop.value(),
            'step': self.sp_step.value(), 'tolerance': self.sp_tol.value(),
            'settle_time': self.sp_settle.value(), 'samples': self.sp_samples.value(),
            'sample_interval': self.sp_interval.value(), 'loop': self.chk_loop.isChecked()
        }

        self.worker = PrecisionScanWorker(self.cmd_thread, self.get_telemetry_cb, self.event_helper, config)
        self.worker.data_point.connect(self._update_plot)
        self.worker.progress.connect(self._update_progress)
        self.worker.status_msg.connect(self.lbl_status.setText)
        self.worker.scan_complete.connect(self._handle_scan_completion)
        self.worker.start()

    def _stop_scan(self):
        if self.worker:
            self.worker.stop()
            self.worker.wait()
        self._lock_ui(False)

    def _lock_ui(self, locked: bool):
        lock_msg = "Disabled: Sweep is currently running."

        widgets = [self.sp_start, self.sp_stop, self.sp_step, self.sp_tol,
                   self.sp_settle, self.sp_samples, self.sp_interval, self.chk_loop,
                   self.edit_export_path]
        for w in widgets:
            w.setEnabled(not locked)

        self.btn_start.setEnabled(False if locked else True)
        self.btn_start.setStyleSheet(
            "background-color: #555555; color: #888888; font-weight: bold; padding: 12px; border-radius: 4px;" if locked
            else "background-color: #4CAF50; color: white; font-weight: bold; padding: 12px; border-radius: 4px;"
        )

        self.btn_stop.setEnabled(locked)
        self.btn_stop.setStyleSheet(
            "background-color: #F44336; color: white; font-weight: bold; padding: 12px; border-radius: 4px;" if locked
            else "background-color: #555555; color: #888888; font-weight: bold; padding: 12px; border-radius: 4px;"
        )

        if not locked:
            self.lbl_rem_time.setText("Remaining: --:--")

    def _update_plot(self, x, y):
        self.x_data.append(x)
        self.y_data.append(y)
        self.curve_live.setData(self.x_data, self.y_data)

    def _clear_plot(self):
        self.x_data.clear()
        self.y_data.clear()
        self.curve_live.setData([], [])
        self.curve_ghost.setData([], [])
        self.peak_markers.setData([], [])

    def _toggle_log_y(self, state):
        self.plot_widget.setLogMode(x=False, y=(state == Qt.CheckState.Checked.value))

    def _handle_scan_completion(self, scan_data: list):
        if self.chk_export.isChecked() and len(scan_data) > 0:
            self._export_to_csv(scan_data)

        if self.chk_loop.isChecked() and self.worker and not self.worker.is_stopped:
            # Ghost the completed trace to the background
            self.curve_ghost.setData(self.x_data, self.y_data)

            # Clear live data to prepare for the new sweep
            self.x_data.clear()
            self.y_data.clear()
            self.curve_live.setData([], [])
        else:
            self._lock_ui(False)
            self.lbl_status.setText("Status: Scan Complete.")

    def _export_to_csv(self, data: list):
        try:
            base_path = self.edit_export_path.text().strip()
            if not base_path:
                base_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../data/exports/Mass_Scans", "MassScan"))

            export_dir = os.path.dirname(base_path)
            base_name = os.path.basename(base_path)

            os.makedirs(export_dir, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{base_name}_{timestamp}.csv"
            filepath = os.path.join(export_dir, filename)

            with open(filepath, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(["Timestamp", "Requested_Magnet_A", "Measured_Magnet_A", "Measured_Beam_A", "Target_kV", "Extraction_kV"])
                now_str = datetime.now().isoformat()
                for req_mag, act_mag, act_beam, target_kv, ext_kv in data:
                    writer.writerow([now_str, req_mag, act_mag, act_beam, target_kv, ext_kv])

            self.lbl_status.setText(f"Status: Exported to {filename}")
        except Exception as e:
            print(f"[Mass Scan] Export Failed: {e}")