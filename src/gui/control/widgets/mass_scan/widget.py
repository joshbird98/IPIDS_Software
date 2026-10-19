"""Mass Scan panel. The scan itself runs in the sequencer service (src/sequencer/mass_scan.py),
which holds the magnet and the SMU for it; this panel starts and stops it, draws its points as
they come (src/sequencer/points.py), and does the analysis: mass calibration, peaks and their
resolution, loading and saving traces."""
import logging
import os
import time

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QApplication, QFileDialog, QInputDialog, QLabel, QMessageBox, QTableWidgetItem,
                             QWidget)

from src.core import hard_caps
from src.core.mass_calibration import current_for_mass, mass_for_current
from src.gui.control.sequencer_link import FETCH_ORIGIN, OFFLINE_TEXT, SequencerSlot
from src.gui.control.widgets.mass_scan import isotopes, peaks, traces, ui
from src.gui.control.widgets.mass_scan.axis import symlog
from src.gui.control.widgets.mass_scan.periodic_table import PeriodicTableDialog
from src.gui.control.widgets.source_widget import setpoint_limits
from src.sequencer import protocol as P
from src.sequencer.mass_scan import SP_TAG, export_csv

log = logging.getLogger("hmi.mass_scan")

DEFAULT_ENERGY_EV = 30000.0     # when no beam energy was recorded
MAGNET = "ion_beam.beamline.magnet"
SMU = "ion_beam.beamline.faraday.smu"


def energy_ev(e_kev: float) -> float:
    return e_kev * 1000.0 if e_kev > 0 else DEFAULT_ENERGY_EV


class PrecisionMassScannerWidget(QWidget):
    def __init__(self, cmd_thread, event_helper):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.event_helper = event_helper
        self.scan = SequencerSlot(cmd_thread, P.MASS_SCAN)
        self.follower = self.scan.follower()
        self._run_shown = None            # the sequencer run whose points are on the plot
        self._passes_handled = 0          # finished passes of that run already analysed
        self._pass_fetch = None           # (pass number, ticket) fetching a finished pass
        self._tickets = {}
        self._was_active = None
        self.time_per_point = 0.0
        self.current_csv_path = None

        self.amps_data, self.amu_data, self.y_data, self.energy_data = [], [], [], []
        self.ghost_amps, self.ghost_amu, self.ghost_y, self.ghost_energy = [], [], [], []
        self.last_scan_data = []
        self.peak_labels = []
        self.calib_points = []
        self.selected_elements = set()
        self.calibration_source = "default values (not fitted this session)"
        self.mass_db = isotopes.build_mass_db(isotopes.load_isotopes(), self.selected_elements)

        ui.build(self)
        limits = setpoint_limits().get(SP_TAG)
        if limits:
            for box in (self.sp_start, self.sp_stop):
                box.setRange(*limits)
        for box in (self.sp_start, self.sp_stop):
            box.setToolTip(hard_caps.note(SP_TAG))
        self._lock_ui(False)
        self._update_eta()

    # ================================================================== the scan (sequencer)
    def _scan_settings(self) -> dict:
        return {"start_a": self.sp_start.value(), "stop_a": self.sp_stop.value(), "step_a": self.sp_step.value(),
                "tolerance": self.sp_tol.value(), "settle_time": self.sp_settle.value(),
                "samples": self.sp_samples.value(), "sample_interval": self.sp_interval.value(),
                "nplc": self.sp_nplc.value(), "loop": self.chk_loop.isChecked(),
                "export": self.chk_export.isChecked(), "export_path": self.edit_export_path.text().strip()}

    def _start_scan(self):
        if self.sp_start.value() == self.sp_stop.value():
            QMessageBox.warning(self, "Invalid Parameters", "Start and Stop values cannot be identical.")
            return
        self.btn_start.setEnabled(False)
        self.lbl_status.setText("Status: asking the sequencer to start the scan...")
        self._tickets["start"] = self.scan.send("start", self._scan_settings())

    def _stop_scan(self):
        self.btn_stop.setEnabled(False)
        self._tickets["stop"] = self.scan.send("stop")

    def _check_tickets(self):
        for verb, ticket in list(self._tickets.items()):
            if ticket.done():
                del self._tickets[verb]
                if not ticket.ok:
                    self.lbl_status.setText(f"Status: {verb} refused - {ticket.reason}")

    def update_telemetry(self, data: dict):
        self.scan.update(data)
        self._check_tickets()
        active = self.scan.active
        run = self.scan.get("run")
        if run is not None and run != self._run_shown:
            self._new_run(run)          # a new scan - or, when the HMI starts, the last one
        if self._run_shown is not None and run == self._run_shown:
            self._follow_points()
            self._follow_passes()
        if self.scan.online and (active or self.scan.state in P.FINAL):
            status = self.scan.get("status") or self.scan.state
            self.lbl_status.setText(f"Status: {status}")
            done, total = (self.scan.get("progress") or [0, 0])[:2]
            if active and total:
                self._update_progress(done, total)
        if active != self._was_active:
            self._was_active = active
            self._lock_ui(active)
        if not active:
            self._update_start_permit(data)

    def _new_run(self, run):
        self._run_shown = run
        # Joining a run part way (HMI restarted): only its latest finished pass is still kept
        self._passes_handled = max(0, (self.scan.get("passes_done") or 0) - 1)
        self._pass_fetch = None
        self._clear_plot()
        self.lbl_data_source.setText("<b>Data Source:</b> Live Scan")

    def _follow_points(self):
        changes = self.follower.update(self.scan.get("run"), self.scan.get("pass"), self.scan.get("points"),
                                       self.scan.get("tail"))
        for kind, points in changes:
            if kind == "new":
                if self.amps_data:          # a loop scan's next pass: the last one becomes the ghost trace
                    self.ghost_amps, self.ghost_amu = list(self.amps_data), list(self.amu_data)
                    self.ghost_y, self.ghost_energy = list(self.y_data), list(self.energy_data)
                self.amps_data, self.amu_data, self.y_data, self.energy_data = [], [], [], []
                self._clear_peaks()
            elif kind == "replace":
                self.amps_data, self.amu_data, self.y_data, self.energy_data = [], [], [], []
            for point in points or []:
                self._add_point(point)
        if changes:
            self._refresh_curves()
            self._update_plot_limits()

    def _follow_passes(self):
        """A finished pass: fetch all of it (whatever the plot has) and analyse it."""
        done = self.scan.get("passes_done") or 0
        if self._pass_fetch is None and done > self._passes_handled:
            pass_no = self._passes_handled + 1
            self._pass_fetch = (pass_no, self.scan.send("fetch", {"run": self._run_shown, "pass": pass_no},
                                                         origin=FETCH_ORIGIN))
        if self._pass_fetch is not None and self._pass_fetch[1].done():
            pass_no, ticket = self._pass_fetch
            self._pass_fetch = None
            self._passes_handled = pass_no if ticket.ok else max(self._passes_handled, done)
            if ticket.ok and ticket.data:
                self._handle_scan_completion([tuple(p) for p in ticket.data])

    def _add_point(self, point):
        _sp, mag, beam, t_kv, e_kv = point[:5]
        self._update_plot(mag, beam, t_kv + e_kv, refresh=False)

    def _update_start_permit(self, data):
        reason = ""
        if not self.scan.online:
            reason = OFFLINE_TEXT
        else:
            for sub, name in ((MAGNET, "Magnet"), (SMU, "SMU")):
                permit = data.get(f"{sub}.permit")
                if permit is None:
                    reason = f"{name} service not running (no live data)."
                    break
                if not permit:
                    reason = f"{name}: {data.get(f'{sub}.permit_reason') or 'not available'}"
                    break
            if not reason and data.get(f"{MAGNET}.stat_enabled") == 0.0:
                reason = "Magnet PSU must be enabled."
        busy = "start" in self._tickets
        self.btn_start.setEnabled(not reason and not busy)
        self.btn_start.setStyleSheet(ui.OFF_STYLE if reason or busy else ui.START_STYLE)
        self.btn_start.setToolTip(f"Disabled: {reason}" if reason else "Click to begin the precision mass scan sweep.")

    def _lock_ui(self, locked: bool):
        for w in (self.sp_start, self.sp_stop, self.sp_step, self.sp_tol, self.sp_settle, self.sp_samples,
                  self.sp_interval, self.sp_nplc, self.chk_loop, self.chk_export, self.edit_export_path):
            w.setEnabled(not locked)
        self.btn_start.setEnabled(not locked)
        self.btn_start.setStyleSheet(ui.OFF_STYLE if locked else ui.START_STYLE)
        self.btn_stop.setEnabled(locked)
        self.btn_stop.setStyleSheet(ui.STOP_STYLE if locked else ui.OFF_STYLE)
        if not locked:
            self.lbl_rem_time.setText("Remaining: --:--")

    def _handle_scan_completion(self, scan_data: list):
        """One finished pass: [(requested A, magnet A, beam A, target kV, extraction kV)]."""
        self.last_scan_data = scan_data
        self.btn_save_csv.setEnabled(True)
        saved = self.scan.get("file")
        exported = (self.scan.get("config") or {}).get("export")
        self.lbl_data_source.setText(f"<b>Data Source:</b> Saved as<br>{os.path.basename(saved)}" if saved and exported
                                     else "<b>Data Source:</b> Recent Scan (Unsaved)")
        self.lbl_data_source.setToolTip(saved if saved and exported else "")
        self._update_energy_display()
        self._conditional_peak_update()

    # ================================================================== plot
    def _ty(self, y):
        """Beam current -> plot y (symmetric log in the log view, see symlog())."""
        return symlog(y) if self.chk_log_y.isChecked() else np.asarray(y, dtype=float)

    def _amu(self, amps, e_kev):
        return mass_for_current(amps, energy_ev(e_kev), self.sp_k_factor.value(), self.sp_i_offset.value())

    def _update_plot(self, amps, beam, energy_kev, refresh=True):
        self.amps_data.append(amps)
        self.amu_data.append(self._amu(amps, energy_kev))
        self.y_data.append(beam)
        self.energy_data.append(energy_kev)
        if refresh:
            self._refresh_curves()
            self._update_plot_limits()

    def _refresh_curves(self):
        def split(x_arr, y_arr):
            """The whole trace, and its negative stretches on their own (drawn red on top)."""
            if not x_arr or not y_arr:
                return [], [], [], []
            x = np.array(x_arr, dtype=float)
            y = self._ty(np.array(y_arr, dtype=float))
            if not np.any(y < 0):
                return x, y, [], []
            return x, y, x, np.where(y < 0, y, np.nan)

        x, y, xn, yn = split(self.amu_data, self.y_data)
        self.curve_live_pos.setData(x, y)
        self.curve_live_neg.setData(xn, yn)
        x, y, xn, yn = split(self.ghost_amu, self.ghost_y)
        self.curve_ghost_pos.setData(x, y)
        self.curve_ghost_neg.setData(xn, yn)

    def _clear_plot(self):
        for series in (self.amps_data, self.amu_data, self.y_data, self.energy_data, self.ghost_amps, self.ghost_amu,
                       self.ghost_y, self.ghost_energy, self.last_scan_data):
            series.clear()
        self.btn_save_csv.setEnabled(False)
        self._refresh_curves()
        self._clear_peaks()
        self._update_energy_display()
        self._update_plot_limits()
        self._fit_y()
        if not self.scan.active:
            self.lbl_data_source.setText("<b>Data Source:</b> None")

    def _toggle_log_y(self, state):
        # The plot's own log mode can't show negative currents; the symlog transform is
        # applied to the data instead (see symlog()), and the axis labels it back in amps.
        self.current_axis.symlog = (state == Qt.CheckState.Checked.value)
        self.current_axis.picture = None
        self.current_axis.update()
        self._refresh_curves()
        self._update_plot_limits()
        self._conditional_peak_update()
        self._fit_y()
        self._update_calib_table()

    def _fit_y(self):
        """Y range back to the data in the visible mass range (after a scale change, new data...)."""
        self.plot_widget.getViewBox().enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)

    def _update_plot_limits(self):
        all_x = self.amu_data + self.ghost_amu
        all_y = self.y_data + self.ghost_y
        x_min, x_max = (min(all_x), max(all_x)) if all_x else (0.0, 100.0)
        # Pan/zoom limits: a margin around the data, in whichever y scale is shown
        if all_y:
            t = self._ty(np.array(all_y, dtype=float))
            lo, hi = min(float(np.nanmin(t)), 0.0), max(float(np.nanmax(t)), 0.0)
        else:
            lo, hi = 0.0, float(self._ty(1e-6))
        span = (hi - lo) or 1.0
        self.plot_widget.getViewBox().setLimits(xMin=max(0.0, x_min - 2.0), xMax=x_max + 2.0, yMin=lo - span,
                                                yMax=hi + span)

    def _recalculate_mass_axis(self):
        self.amu_data = [self._amu(a, e) for a, e in zip(self.amps_data, self.energy_data)]
        self.ghost_amu = [self._amu(a, e) for a, e in zip(self.ghost_amps, self.ghost_energy)]
        self._refresh_curves()
        self._conditional_peak_update()
        self._update_plot_limits()
        k, i_off = self.sp_k_factor.value(), self.sp_i_offset.value()
        for pt in self.calib_points:
            pt['amu_plot_x'] = mass_for_current(pt['amps'], pt['energy_ev'], k, i_off)
        self._update_calib_table()

    def _update_energy_display(self):
        valid = [e for e in self.energy_data if e > 0]
        if not self.energy_data:
            self.lbl_energy_display.setText("--- keV")
        elif not valid:
            self.lbl_energy_display.setText("Unknown")
        elif f"{min(valid):.2f}" == f"{max(valid):.2f}":
            self.lbl_energy_display.setText(f"{np.mean(valid):.2f} keV")
        else:
            self.lbl_energy_display.setText(f"{np.mean(valid):.2f} ± {(max(valid) - min(valid)) / 2.0:.2f} keV")

    # ================================================================== peaks
    def _open_periodic_table(self):
        dlg = PeriodicTableDialog(self.selected_elements, self)
        if dlg.exec():
            self.selected_elements = dlg.selected
            started = time.time()
            self.mass_db = isotopes.build_mass_db(isotopes.load_isotopes(), self.selected_elements)
            log.info("Mass database rebuilt in %.2f s: %d structures", time.time() - started,
                     sum(len(v) for v in self.mass_db.values()))
            self._conditional_peak_update()

    def _on_detect_toggled(self, checked):
        colour = "#F44336" if checked else "#2196F3"
        self.btn_detect.setText("⏹ Stop Auto-Detect" if checked else "🔍 Auto-Detect Peaks")
        self.btn_detect.setStyleSheet(f"background-color: {colour}; color: white; font-weight: bold; padding: 8px; "
                                      f"border-radius: 4px;")
        if checked:
            self._run_peak_detection()
        else:
            self._clear_peaks()

    def _conditional_peak_update(self):
        if self.btn_detect.isChecked():
            self._run_peak_detection()

    def _clear_peaks(self):
        self.peak_table.setRowCount(0)
        self.peak_markers.setData([], [])
        self.tail_markers.setData([], [])
        for label in self.peak_labels:
            self.plot_widget.removeItem(label)
        self.peak_labels.clear()
        self.lbl_res.setText("<b>Avg Mass Resolution:</b> ---")

    def _run_peak_detection(self):
        self._clear_peaks()
        if len(self.amu_data) < 10:
            return
        try:
            found = peaks.find(self.amu_data, self.y_data, self.sp_sensitivity.value(), self.sp_max_peaks.value(),
                               self.mass_db)
        except ImportError:
            self.btn_detect.setChecked(False)
            QMessageBox.critical(self, "Dependency Error", "scipy library required for peak detection. (pip install scipy)")
            return
        except ValueError as e:     # e.g. scipy rejecting degenerate data
            self.btn_detect.setChecked(False)
            QMessageBox.critical(self, "Analysis Error", f"Peak detection failed:\n{e}")
            return
        if not found:
            self.lbl_res.setText("<span style='color: #F44336; font-weight: bold;'>No peaks detected.</span>")
            return
        self.peak_table.setRowCount(len(found))
        tails_x, tails_y = [], []
        for row, peak in enumerate(found):
            if peak.labelled:
                label = pg.TextItem(html=f"<div style='text-align: center; color: {peak.colour}; font-size: 10pt; "
                                         f"font-weight: bold;'>{peak.match_html}<br>({peak.amu:.1f})</div>",
                                    anchor=(0.5, 1.2))
                label.setPos(peak.amu, float(self._ty(peak.current)))
                self.plot_widget.addItem(label)
                self.peak_labels.append(label)
            tails_x.extend([peak.amu - peaks.TAIL_OFFSET_AMU, peak.amu + peaks.TAIL_OFFSET_AMU])
            tails_y.extend([max(1e-15, peak.tail_left), max(1e-15, peak.tail_right)])
            match = QLabel(peak.match_html)
            match.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.peak_table.setItem(row, 0, QTableWidgetItem(f"{peak.amu:.2f}"))
            self.peak_table.setCellWidget(row, 1, match)
            tail = QTableWidgetItem(peaks.tail_text(peak.crosstalk_ppm))
            tail.setForeground(pg.mkColor(*peaks.tail_colour(peak.crosstalk_ppm)))
            self.peak_table.setItem(row, 2, tail)
        self.lbl_res.setText(f"<b>Avg Mass Resolution (M/ΔM):</b> <span style='color: #4CAF50;'>"
                             f"{peaks.average_resolution(found):.1f}</span>")
        self.peak_markers.setData([p.amu for p in found], self._ty([p.current for p in found]))
        self.tail_markers.setData(tails_x, self._ty(tails_y))

    # ================================================================== panels, files, timing
    def _toggle_left_panel(self):
        visible = not self.control_panel_widget.isVisible()
        self.control_panel_widget.setVisible(visible)
        self.btn_toggle_ctrl.setText("◀ Setup" if visible else "Setup ▶")

    def _toggle_right_panel(self):
        visible = not self.analysis_panel_widget.isVisible()
        self.analysis_panel_widget.setVisible(visible)
        self.btn_toggle_analysis.setText("Analysis ▶" if visible else "◀ Analysis")

    def _browse_export_path(self):
        path, _ = QFileDialog.getSaveFileName(self, "Select Base Export File",
                                              os.path.dirname(self.edit_export_path.text()), "CSV Files (*.csv)")
        if path:
            self.edit_export_path.setText(path[:-4] if path.endswith(".csv") else path)

    def _load_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load Mass Scan CSV", os.path.dirname(self.edit_export_path.text()),
                                              "CSV Files (*.csv)")
        if path:
            self.load_trace(path)

    def load_trace(self, path):
        try:
            trace = traces.load_trace(path)
        except (OSError, ValueError, KeyError) as e:
            QMessageBox.critical(self, "Load Error", f"Failed to parse CSV:\n{e}")
            return
        self.current_csv_path = path
        self._run_shown = self.scan.get("run", self._run_shown)    # a finished scan must not replace it
        self.amps_data, self.y_data, self.energy_data = trace.amps, trace.beam, trace.energy_kev
        self.last_scan_data = list(zip(trace.amps, trace.amps, trace.beam, trace.energy_kev,
                                       [0.0] * len(trace.amps)))
        self.btn_save_csv.setEnabled(True)
        if trace.k is not None:
            self.calibration_source = f"from trace {os.path.basename(path)}"
            for box, value in ((self.sp_k_factor, trace.k), (self.sp_i_offset, trace.i_off)):
                box.blockSignals(True)
                box.setValue(value)
                box.blockSignals(False)
        self._recalculate_mass_axis()
        self._update_energy_display()
        self._update_plot_limits()
        self._fit_y()
        if not trace.has_energy:
            QMessageBox.warning(self, "Missing Data", "CSV missing voltage columns; peak mass analysis may use defaults.")
        self.lbl_data_source.setText(f"<b>Data Source:</b> Loaded {os.path.basename(path)}")

    def _manual_save_csv(self):
        if not self.last_scan_data:
            QMessageBox.warning(self, "No Data", "No trace data available to save.")
            return
        try:
            path = export_csv(self.last_scan_data, self.edit_export_path.text())
        except OSError as e:
            QMessageBox.critical(self, "Save Error", f"Could not save the trace:\n{e}")
            return
        self.lbl_data_source.setText(f"<b>Data Source:</b> Saved as<br>{os.path.basename(path)}")
        self.lbl_data_source.setToolTip(path)

    def _update_eta(self):
        step = self.sp_step.value()
        n = int(round(abs(self.sp_stop.value() - self.sp_start.value()) / step)) + 1 if step > 0 else 0
        interval = max(self.sp_interval.value(), self.sp_nplc.value() * 0.02 + 0.05)  # the SMU's own integration
        self.time_per_point = 0.05 + self.sp_settle.value() + self.sp_samples.value() * interval
        m, s = divmod(int(n * self.time_per_point), 60)
        self.lbl_eta.setText(f"Est. Scan Time: {m:02d}:{s:02d}")

    def _update_progress(self, current_step: int, total_steps: int):
        m, s = divmod(int(max(0, (total_steps - current_step) * self.time_per_point)), 60)
        self.lbl_rem_time.setText(f"Remaining: {m:02d}:{s:02d}")

    # ================================================================== mouse
    def _on_mouse_clicked(self, evt):
        view_box = self.plot_widget.getViewBox()
        pos = evt.scenePos()
        if QApplication.keyboardModifiers() == Qt.KeyboardModifier.ControlModifier and \
                evt.button() == Qt.MouseButton.LeftButton:
            evt.accept()                          # Ctrl+click: calibration peak
            if view_box.sceneBoundingRect().contains(pos):
                self._add_calibration_point(view_box.mapSceneToView(pos).x())
            return
        if evt.double() and not self.scan.active:
            evt.accept()                          # double-click: put the magnet on that mass
            if view_box.sceneBoundingRect().contains(pos):
                self.jump_to_mass(view_box.mapSceneToView(pos).x())

    def jump_to_mass(self, amu: float):
        e_kev = self.energy_data[-1] if self.energy_data else 30.0
        try:
            amps = max(0.0, current_for_mass(amu, energy_ev(e_kev), self.sp_k_factor.value(), self.sp_i_offset.value()))
        except ValueError as e:
            self.lbl_status.setText(f"Status: no jump - {e}")
            return
        self.cmd_thread.send_command("magnet", SP_TAG, float(amps), origin="hmi")
        self.lbl_status.setText(f"Status: Manual Jump to {amps:.3f} A ({amu:.1f} AMU)")
        self.event_helper.log_user_marker(time.time(), f"Manual Magnet Jump: {amps:.3f} A via Mass Scan", "#9C27B0")

    # ================================================================== calibration
    def _add_calibration_point(self, click_amu):
        if not self.amu_data:
            return
        # Snap to the local maximum (within 5 points) nearest the click
        i = int(np.abs(np.array(self.amu_data) - click_amu).argmin())
        lo, hi = max(0, i - 5), min(len(self.y_data), i + 6)
        peak = lo + int(np.argmax(self.y_data[lo:hi]))
        amps, current_amu = self.amps_data[peak], self.amu_data[peak]
        true_amu, ok = QInputDialog.getDouble(
            self, "Add Calibration Peak",
            f"Peak detected at {amps:.3f} A ({current_amu:.1f} AMU).\nEnter known True AMU:",
            value=round(current_amu), min=1.0, max=300.0, decimals=2)
        if ok:
            self.calib_points.append({"amps": amps, "energy_ev": energy_ev(self.energy_data[peak]),
                                      "true_amu": true_amu, "amu_plot_x": current_amu, "peak_y": self.y_data[peak]})
            self._update_calib_table()

    def _update_calib_table(self):
        self.table_calib.setRowCount(len(self.calib_points))
        for i, pt in enumerate(self.calib_points):
            self.table_calib.setItem(i, 0, QTableWidgetItem(f"{pt['amps']:.3f}"))
            self.table_calib.setItem(i, 1, QTableWidgetItem(f"{pt['true_amu']:.2f}"))
        if self.calib_points:
            self.calib_scatter.setData(x=np.array([p['amu_plot_x'] for p in self.calib_points], dtype=float),
                                       y=np.array([float(self._ty(p['peak_y'])) for p in self.calib_points]))
            self.calib_scatter.setVisible(True)
        else:
            # Dummy data: pyqtgraph's bounds calculation trips over an empty scatter
            self.calib_scatter.setData(x=np.array([0.0]), y=np.array([0.0]))
            self.calib_scatter.setVisible(False)

    def _clear_calib_points(self):
        self.calib_points.clear()
        self.lbl_fit_stats.setText("")
        self._update_calib_table()

    def _calculate_fit(self):
        n = len(self.calib_points)
        if not n:
            return
        try:
            k, i_off, r2 = traces.fit_calibration(self.calib_points, self.sp_i_offset.value())
        except ValueError as e:
            QMessageBox.warning(self, "Fit Error", str(e))
            self.lbl_fit_stats.setText("Fit Error")
            return
        for box, value in ((self.sp_k_factor, k), (self.sp_i_offset, i_off)):
            box.blockSignals(True)
            box.setValue(value)
            box.blockSignals(False)
        self._recalculate_mass_axis()
        if r2 is None:
            self.lbl_fit_stats.setText("Fit: 1 point (I_off fixed)")
            self.calibration_source = f"fitted to 1 peak at {time.strftime('%H:%M')} (I_off kept)"
        else:
            self.lbl_fit_stats.setText(f"Fit R² = {r2:.5f} (n={n})")
            self.calibration_source = f"fitted to {n} peaks at {time.strftime('%H:%M')} (R² {r2:.5f})"
        if self.current_csv_path and os.path.exists(self.current_csv_path):
            try:
                traces.save_calibration(self.current_csv_path, k, i_off)
                log.info("Calibration k=%.2f, I_off=%.3f saved in %s", k, i_off, self.current_csv_path)
            except OSError as e:
                log.error("Could not save the calibration in %s: %s", self.current_csv_path, e)

    def _calibration_typed(self, *_):
        self.calibration_source = "entered by hand"

    def current_calibration(self) -> dict:
        """The calibration in use: AMU = k (I + I_off)^2 / E, and where it came from."""
        return {"k": self.sp_k_factor.value(), "i_off": self.sp_i_offset.value(), "source": self.calibration_source}
