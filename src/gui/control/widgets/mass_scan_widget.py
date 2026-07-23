import time
import os
import csv
import math
import json
import itertools
from datetime import datetime
import numpy as np
import pyqtgraph as pg

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QPushButton, QGroupBox, QDoubleSpinBox, QSpinBox, QCheckBox,
    QMessageBox, QSplitter, QTableWidget, QTableWidgetItem, QHeaderView,
    QLineEdit, QFileDialog, QDialog, QDialogButtonBox
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QMutex, QMutexLocker, QTimer


# --- Periodic Table Element Selector ---
class PeriodicTableDialog(QDialog):
    def __init__(self, current_selection, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select Expected Elements")
        self.selected = set(current_selection)
        layout = QVBoxLayout(self)

        grid = QGridLayout()
        grid.setSpacing(2)

        pt_layout = [
            (1, "H", 0, 0), (2, "He", 0, 17),
            (3, "Li", 1, 0), (4, "Be", 1, 1), (5, "B", 1, 12), (6, "C", 1, 13), (7, "N", 1, 14), (8, "O", 1, 15),
            (9, "F", 1, 16), (10, "Ne", 1, 17),
            (11, "Na", 2, 0), (12, "Mg", 2, 1), (13, "Al", 2, 12), (14, "Si", 2, 13), (15, "P", 2, 14),
            (16, "S", 2, 15), (17, "Cl", 2, 16), (18, "Ar", 2, 17),
            (19, "K", 3, 0), (20, "Ca", 3, 1), (21, "Sc", 3, 2), (22, "Ti", 3, 3), (23, "V", 3, 4), (24, "Cr", 3, 5),
            (25, "Mn", 3, 6), (26, "Fe", 3, 7), (27, "Co", 3, 8), (28, "Ni", 3, 9), (29, "Cu", 3, 10),
            (30, "Zn", 3, 11), (31, "Ga", 3, 12), (32, "Ge", 3, 13), (33, "As", 3, 14), (34, "Se", 3, 15),
            (35, "Br", 3, 16), (36, "Kr", 3, 17),
            (37, "Rb", 4, 0), (38, "Sr", 4, 1), (39, "Y", 4, 2), (40, "Zr", 4, 3), (41, "Nb", 4, 4), (42, "Mo", 4, 5),
            (43, "Tc", 4, 6), (44, "Ru", 4, 7), (45, "Rh", 4, 8), (46, "Pd", 4, 9), (47, "Ag", 4, 10),
            (48, "Cd", 4, 11), (49, "In", 4, 12), (50, "Sn", 4, 13), (51, "Sb", 4, 14), (52, "Te", 4, 15),
            (53, "I", 4, 16), (54, "Xe", 4, 17),
            (55, "Cs", 5, 0), (56, "Ba", 5, 1), (72, "Hf", 5, 2), (73, "Ta", 5, 3), (74, "W", 5, 4), (75, "Re", 5, 5),
            (76, "Os", 5, 6), (77, "Ir", 5, 7), (78, "Pt", 5, 8), (79, "Au", 5, 9), (80, "Hg", 5, 10),
            (81, "Tl", 5, 11), (82, "Pb", 5, 12), (83, "Bi", 5, 13), (84, "Po", 5, 14), (85, "At", 5, 15),
            (86, "Rn", 5, 16),
            (87, "Fr", 6, 0), (88, "Ra", 6, 1), (104, "Rf", 6, 2), (105, "Db", 6, 3), (106, "Sg", 6, 4),
            (107, "Bh", 6, 5), (108, "Hs", 6, 6), (109, "Mt", 6, 7), (110, "Ds", 6, 8), (111, "Rg", 6, 9),
            (112, "Cn", 6, 10), (113, "Nh", 6, 11), (114, "Fl", 6, 12), (115, "Mc", 6, 13), (116, "Lv", 6, 14),
            (117, "Ts", 6, 15), (118, "Og", 6, 16),
        ]

        lanths = ["La", "Ce", "Pr", "Nd", "Pm", "Sm", "Eu", "Gd", "Tb", "Dy", "Ho", "Er", "Tm", "Yb"]
        actins = ["Ac", "Th", "Pa", "U", "Np", "Pu", "Am", "Cm", "Bk", "Cf", "Es", "Fm", "Md", "No", "Lr"]

        for i, sym in enumerate(lanths):
            pt_layout.append((57 + i, sym, 8, i + 2))
        for i, sym in enumerate(actins):
            pt_layout.append((89 + i, sym, 9, i + 2))

        for z, sym, row, col in pt_layout:
            btn = QPushButton(sym)
            btn.setFixedSize(36, 36)
            btn.setCheckable(True)
            if sym in self.selected:
                btn.setChecked(True)
                btn.setStyleSheet("background-color: #2196F3; color: white; font-weight: bold;")

            def make_toggle_handler(button, element):
                def handler(checked):
                    if checked:
                        self.selected.add(element)
                        button.setStyleSheet("background-color: #2196F3; color: white; font-weight: bold;")
                    else:
                        self.selected.discard(element)
                        button.setStyleSheet("")

                return handler

            btn.toggled.connect(make_toggle_handler(btn, sym))
            grid.addWidget(btn, row, col)

        layout.addLayout(grid)

        bbox = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bbox.accepted.connect(self.accept)
        bbox.rejected.connect(self.reject)
        layout.addWidget(bbox)


# --- Custom Axis for Smart Current Formatting ---
class SmartCurrentAxis(pg.AxisItem):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.enableAutoSIPrefix(False)

    def tickStrings(self, values, scale, spacing):
        strings = []
        for v in values:
            try:
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
    data_point = pyqtSignal(float, float, float)
    scan_complete = pyqtSignal(list)
    status_msg = pyqtSignal(str)
    progress = pyqtSignal(int, int)

    def __init__(self, cmd_thread, get_telemetry_cb, event_helper, config: dict):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.get_telemetry_cb = get_telemetry_cb
        self.event_helper = event_helper
        self.config = config

        self.start_a = config['start_a']
        self.stop_a = config['stop_a']
        self.step_a = config['step_a']
        self.tolerance = config['tolerance']
        self.settle_time = config['settle_time']
        self.samples = config['samples']
        self.sample_interval = config['sample_interval']
        self.nplc = config['nplc']
        self.loop_scan = config['loop']

        self.is_stopped = False

        self.mode_tag = "ion_beam.beamline.magnet.cmd_ctrl_mode"
        self.sp_tag = "ion_beam.beamline.magnet.sp_requested_current"
        self.rb_mag_tag = "ion_beam.beamline.magnet.rb_current"
        self.smu_settled_tag = "ion_beam.beamline.faraday.smu.stat_range_settled"
        self.rb_beam_tag = "ion_beam.beamline.faraday.smu.rb_current"
        self.target_kv_tag = "ion_beam.source.target.rb_voltage"
        self.ext_kv_tag = "ion_beam.source.extraction.rb_voltage"

    def run(self):
        try:
            self.event_helper.log_general(f"Mass Scan started: {self.start_a}A to {self.stop_a}A (NPLC: {self.nplc})")
            self.status_msg.emit("Status: Seizing Control & Configuring SMU...")

            # Seize magnet control and set SMU integration
            self.cmd_thread.send_command("magnet", self.mode_tag, 1, origin="mass_scan")
            self.cmd_thread.send_command("smu", "ion_beam.beamline.faraday.smu.cmd_nplc", self.nplc, origin="mass_scan")
            time.sleep(0.5)

            num_steps = int(round(abs(self.stop_a - self.start_a) / self.step_a)) + 1
            points = np.linspace(self.start_a, self.stop_a, num_steps)

            # Ensure the worker blocks long enough for the SMU to physically complete the query
            effective_interval = max(self.sample_interval, (self.nplc * 0.02) + 0.05)

            while not self.is_stopped:
                self.status_msg.emit("Status: Scanning...")
                scan_results = []

                for idx, sp_amps in enumerate(points):
                    if self.is_stopped: break

                    point_start_t = time.time()

                    self.cmd_thread.send_command("magnet", self.sp_tag, float(sp_amps), origin="mass_scan")
                    cmd_sent_t = time.time()

                    # --- 1. Dynamic Settling Phase ---
                    timeout_start = time.time()
                    while time.time() - timeout_start < 5.0:
                        if self.is_stopped: break

                        mag_settled = abs(self.get_telemetry_cb(self.rb_mag_tag) - sp_amps) <= self.tolerance
                        smu_settled = self.get_telemetry_cb(self.smu_settled_tag) == 1.0

                        if mag_settled and smu_settled:
                            break
                        time.sleep(0.05)

                    dynamic_settle_t = time.time()

                    if self.is_stopped: break
                    time.sleep(self.settle_time)
                    static_settle_t = time.time()

                    # --- 2. Sample Acquisition Phase ---
                    mag_buffer, beam_buffer, target_buffer, ext_buffer = [], [], [], []
                    samples_collected = 0

                    while samples_collected < self.samples:
                        if self.is_stopped: break

                        # If SMU range changes mid-acquisition, flush buffer and wait
                        if self.get_telemetry_cb(self.smu_settled_tag) != 1.0:
                            mag_buffer.clear()
                            beam_buffer.clear()
                            target_buffer.clear()
                            ext_buffer.clear()
                            samples_collected = 0
                            time.sleep(0.1)  # Brief yield to allow SMU to settle
                            continue

                        mag_buffer.append(self.get_telemetry_cb(self.rb_mag_tag))
                        beam_buffer.append(self.get_telemetry_cb(self.rb_beam_tag))

                        t_kv = self.get_telemetry_cb(self.target_kv_tag)
                        e_kv = self.get_telemetry_cb(self.ext_kv_tag)
                        target_buffer.append(float(t_kv) if t_kv is not None else 0.0)
                        ext_buffer.append(float(e_kv) if e_kv is not None else 0.0)

                        samples_collected += 1
                        time.sleep(effective_interval)

                    acq_t = time.time()

                    if self.is_stopped: break

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
                    total_energy_kev = mean_target_kv + mean_ext_kv

                    scan_results.append((sp_amps, mean_mag, mean_beam, mean_target_kv, mean_ext_kv))
                    self.data_point.emit(mean_mag, mean_beam, total_energy_kev)
                    self.progress.emit(idx + 1, num_steps)

                    cmd_time = cmd_sent_t - point_start_t
                    dyn_time = dynamic_settle_t - cmd_sent_t
                    stat_time = static_settle_t - dynamic_settle_t
                    acq_time = acq_t - static_settle_t
                    tot_time = acq_t - point_start_t
                    print(
                        f"Point {idx + 1:03d}/{num_steps} [{sp_amps:.3f}A] -> Cmd: {cmd_time:.3f}s | DynSet: {dyn_time:.3f}s | StatSet: {stat_time:.3f}s | Acq: {acq_time:.3f}s | Total: {tot_time:.3f}s")

                if not self.is_stopped:
                    self.scan_complete.emit(scan_results)

                if not self.loop_scan or self.is_stopped:
                    break

                self.cmd_thread.send_command("magnet", self.sp_tag, float(points[0]), origin="mass_scan")

                reset_start = time.time()
                while time.time() - reset_start < 2.0:
                    if self.is_stopped: break
                    time.sleep(0.05)

        except Exception as e:
            print(f"[MassScanWorker] FATAL: {e}")
        finally:
            self.event_helper.log_general("Mass Scan stopped/finished.")
            self.status_msg.emit("Status: Releasing Control & Restoring SMU...")

            # Release magnet control and restore fast SMU integration
            self.cmd_thread.send_command("magnet", self.mode_tag, 0, origin="mass_scan")
            self.cmd_thread.send_command("smu", "ion_beam.beamline.faraday.smu.cmd_nplc", 1.0, origin="mass_scan")

    def stop(self):
        self.is_stopped = True


class PrecisionMassScannerWidget(QWidget):
    _isotope_cache = []
    _cache_mutex = QMutex()
    _full_db_loaded = False

    def __init__(self, cmd_thread, get_telemetry_cb, event_helper):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.get_telemetry_cb = get_telemetry_cb
        self.event_helper = event_helper
        self.worker = None
        self.time_per_point = 0.0

        self.amps_data, self.amu_data, self.y_data, self.energy_data = [], [], [], []
        self.ghost_amps, self.ghost_amu, self.ghost_y, self.ghost_energy = [], [], [], []

        self.last_scan_data = []
        self.peak_labels = []
        self.selected_elements = set([])

        self.MASS_DB = {}
        self._temp_mass_db = {}

        self._load_local_json_db()
        self._update_mass_db()

        self._init_ui()
        self._update_eta()

    def _on_full_db_loaded(self, full_cache):
        if not full_cache:
            return

        locker = QMutexLocker(self._cache_mutex)
        self.__class__._isotope_cache = list(full_cache)
        self.__class__._full_db_loaded = True

        QTimer.singleShot(0, self._perform_safe_rebuild)

    def _perform_safe_rebuild(self):
        self._update_mass_db()
        self._conditional_peak_update()
        print("[Mass DB] Rebuild performed safely in main thread.")

    def _load_local_json_db(self):
        locker = QMutexLocker(self._cache_mutex)
        if self.__class__._isotope_cache:
            return

        db_path = os.path.join(os.path.dirname(__file__), "isotope_db.json")
        try:
            with open(db_path, "r") as f:
                self.__class__._isotope_cache = json.load(f)
            print(f"[Mass DB] Instantly loaded {len(self.__class__._isotope_cache)} isotopes from JSON.")
        except FileNotFoundError:
            print("[Mass DB] isotope_db.json not found! Falling back to minimal dataset.")
            cache = []
            fallback_isos = {
                "H": [(1.008, "1H", "<sup>1</sup>H", 0.999)],
                "He": [(4.002, "4He", "<sup>4</sup>He", 0.999)],
                "C": [(12.000, "12C", "<sup>12</sup>C", 0.989), (13.003, "13C", "<sup>13</sup>C", 0.011)],
                "N": [(14.003, "14N", "<sup>14</sup>N", 0.996), (15.000, "15N", "<sup>15</sup>N", 0.004)],
                "O": [(15.995, "16O", "<sup>16</sup>O", 0.997), (17.999, "18O", "<sup>18</sup>O", 0.002)],
                "F": [(18.998, "19F", "<sup>19</sup>F", 1.000)],
                "Ne": [(19.992, "20Ne", "<sup>20</sup>Ne", 0.904), (21.991, "22Ne", "<sup>22</sup>Ne", 0.092)],
                "Si": [(27.977, "28Si", "<sup>28</sup>Si", 0.922), (28.976, "29Si", "<sup>29</sup>Si", 0.046),
                       (29.974, "30Si", "<sup>30</sup>Si", 0.031)],
                "P": [(30.974, "31P", "<sup>31</sup>P", 1.000)],
                "S": [(31.972, "32S", "<sup>32</sup>S", 0.949), (33.968, "34S", "<sup>34</sup>S", 0.042)],
                "Cl": [(34.969, "35Cl", "<sup>35</sup>Cl", 0.757), (36.966, "37Cl", "<sup>37</sup>Cl", 0.242)],
                "Ar": [(39.962, "40Ar", "<sup>40</sup>Ar", 0.996)],
                "Ge": [(69.924, "70Ge", "<sup>70</sup>Ge", 0.205), (71.922, "72Ge", "<sup>72</sup>Ge", 0.274),
                       (72.923, "73Ge", "<sup>73</sup>Ge", 0.077), (73.921, "74Ge", "<sup>74</sup>Ge", 0.365),
                       (75.921, "76Ge", "<sup>76</sup>Ge", 0.078)]
            }
            for sym, isos in fallback_isos.items():
                for mass, plain, html, prob in isos:
                    cache.append({'mass': int(round(mass)), 'html': html, 'plain': plain, 'sym': sym, 'prob': prob})
            self.__class__._isotope_cache = cache

    def _update_mass_db(self):
        locker = QMutexLocker(self._cache_mutex)
        t_start_total = time.time()
        self.MASS_DB.clear()
        self._temp_mass_db.clear()

        current_cache = list(self.__class__._isotope_cache)

        base_isos_selected = [e for e in current_cache if e['sym'] in self.selected_elements]
        base_isos_unselected = [e for e in current_cache if e['sym'] not in self.selected_elements]

        for i in range(1, 5):
            for combo in itertools.combinations_with_replacement(base_isos_selected, i):
                self._add_combo_to_db(combo, is_selected=True)

        for i in range(1, 3):
            for combo in itertools.combinations_with_replacement(base_isos_unselected, i):
                self._add_combo_to_db(combo, is_selected=False)

        adducts = [
            ("", 0, "", 1.0),
            ("+H", 1, "H", 0.15),
            ("+H2", 2, "H<sub>2</sub>", 0.05),
            ("+O", 16, "O", 0.02),
            ("+O2", 32, "O<sub>2</sub>", 0.005),
            ("+N", 14, "N", 0.01)
        ]

        new_additions = []
        for int_m, compounds_dict in self._temp_mass_db.items():
            for c_entry in compounds_dict.values():
                for add_plain, add_mass, add_html, add_prob in adducts:
                    if add_mass == 0: continue
                    t_mass = int_m + add_mass
                    if t_mass > 300: continue

                    full_plain = f"{c_entry['plain']}{add_plain}"
                    full_html = f"{c_entry['html']}{add_html}"
                    total_prob = c_entry['prob'] * add_prob

                    new_additions.append(
                        (t_mass, full_plain, full_html, c_entry['mass'] + add_mass, c_entry['is_selected'], total_prob))

        for t_mass, full_plain, full_html, c_mass, is_sel, prob in new_additions:
            if t_mass not in self._temp_mass_db:
                self._temp_mass_db[t_mass] = {}
            if full_plain not in self._temp_mass_db[t_mass]:
                self._temp_mass_db[t_mass][full_plain] = {
                    'plain': full_plain,
                    'html': full_html,
                    'mass': c_mass,
                    'is_selected': is_sel,
                    'prob': prob
                }

        for int_m, compounds_dict in self._temp_mass_db.items():
            self.MASS_DB[int_m] = list(compounds_dict.values())

        self._temp_mass_db.clear()
        print(
            f"[Mass DB] Rebuild complete in {time.time() - t_start_total:.3f}s. Loaded {sum(len(v) for v in self.MASS_DB.values())} structures.")

    def _add_combo_to_db(self, combo, is_selected):
        total_mass = sum(iso['mass'] for iso in combo)
        if total_mass > 300: return

        int_mass = int(round(total_mass))
        prob = np.prod([iso['prob'] for iso in combo])

        counts = {}
        for iso in combo:
            name = iso['plain']
            if name not in counts:
                counts[name] = {'count': 0, 'html': iso['html']}
            counts[name]['count'] += 1

        def sort_key(name):
            if "C" in name and not "Cl" in name and not "Ca" in name: return "0" + name
            if "H" in name and not "He" in name: return "1" + name
            return "2" + name

        html_parts, plain_parts = [], []
        for name in sorted(counts.keys(), key=sort_key):
            count = counts[name]['count']
            html_base = counts[name]['html']
            if count > 1:
                html_parts.append(f"{html_base}<sub>{count}</sub>")
                plain_parts.append(f"{name}{count}")
            else:
                html_parts.append(html_base)
                plain_parts.append(name)

        compound_plain = "".join(plain_parts)
        compound_html = "".join(html_parts)

        if int_mass not in self._temp_mass_db:
            self._temp_mass_db[int_mass] = {}

        if compound_plain not in self._temp_mass_db[int_mass]:
            self._temp_mass_db[int_mass][compound_plain] = {
                'plain': compound_plain,
                'html': compound_html,
                'mass': total_mass,
                'is_selected': is_selected,
                'prob': prob
            }

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
        self.sp_start.setRange(0.0, 150.0)
        self.sp_start.setDecimals(3)
        self.sp_start.setSuffix(" A")
        self.sp_start.setValue(0.0)
        self.sp_start.valueChanged.connect(self._update_eta)

        self.sp_stop = QDoubleSpinBox()
        self.sp_stop.setRange(0.0, 150.0)
        self.sp_stop.setDecimals(3)
        self.sp_stop.setSuffix(" A")
        self.sp_stop.setValue(40.0)
        self.sp_stop.valueChanged.connect(self._update_eta)

        self.sp_step = QDoubleSpinBox()
        self.sp_step.setRange(0.001, 10.0)
        self.sp_step.setDecimals(3)
        self.sp_step.setSuffix(" A")
        self.sp_step.setValue(0.05)
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
        self.sp_tol.setToolTip("Target hardware tolerance measured in Magnet Amps.")

        self.sp_settle = QDoubleSpinBox()
        self.sp_settle.setRange(0.0, 5.0)
        self.sp_settle.setDecimals(2)
        self.sp_settle.setSuffix(" s")
        self.sp_settle.setValue(0.1)
        self.sp_settle.valueChanged.connect(self._update_eta)

        self.sp_samples = QSpinBox()
        self.sp_samples.setRange(1, 1000)
        self.sp_samples.setValue(5)
        self.sp_samples.valueChanged.connect(self._update_eta)

        self.sp_interval = QDoubleSpinBox()
        self.sp_interval.setRange(0.01, 1.0)
        self.sp_interval.setDecimals(3)
        self.sp_interval.setSuffix(" s")
        self.sp_interval.setValue(0.02)
        self.sp_interval.valueChanged.connect(self._update_eta)

        self.sp_nplc = QDoubleSpinBox()
        self.sp_nplc.setRange(0.01, 100.0)
        self.sp_nplc.setDecimals(2)
        self.sp_nplc.setValue(10.0)
        self.sp_nplc.setToolTip("SMU Integration Time (1 NPLC = 20ms). 10.0 = 200ms per sample for ultimate low noise.")
        self.sp_nplc.valueChanged.connect(self._update_eta)

        layout_acq.addWidget(QLabel("HW Tol:"), 0, 0)
        layout_acq.addWidget(self.sp_tol, 0, 1)
        layout_acq.addWidget(QLabel("Wait Time:"), 1, 0)
        layout_acq.addWidget(self.sp_settle, 1, 1)
        layout_acq.addWidget(QLabel("Samples:"), 2, 0)
        layout_acq.addWidget(self.sp_samples, 2, 1)
        layout_acq.addWidget(QLabel("Interval:"), 3, 0)
        layout_acq.addWidget(self.sp_interval, 3, 1)
        layout_acq.addWidget(QLabel("SMU NPLC:"), 4, 0)
        layout_acq.addWidget(self.sp_nplc, 4, 1)
        grp_acq.setLayout(layout_acq)
        control_panel.addWidget(grp_acq)

        # 3. Execution & Export
        grp_exec = QGroupBox("Execution & Output")
        layout_exec = QVBoxLayout()

        self.chk_loop = QCheckBox("Continuous Loop")
        self.chk_export = QCheckBox("Auto-Export CSV on Complete")
        self.chk_export.setChecked(True)

        export_layout = QHBoxLayout()
        self.edit_export_path = QLineEdit()
        default_export = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "../../../../data/exports/Mass_Scans", "MassScan"))
        self.edit_export_path.setText(default_export)

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

        tb_layout = QHBoxLayout()
        self.btn_toggle_ctrl = QPushButton("◀ Setup")
        self.btn_toggle_ctrl.clicked.connect(self._toggle_left_panel)
        tb_layout.addWidget(self.btn_toggle_ctrl)

        self.chk_log_y = QCheckBox("Logarithmic Y-Axis")
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
        self.plot_widget.setLabel('bottom', "Mass (AMU)")
        self.plot_widget.setLabel('left', "Beam Current")
        self.plot_widget.showGrid(x=True, y=True, alpha=0.5)

        self.plot_widget.scene().sigMouseClicked.connect(self._on_mouse_clicked)

        # --- Pos/Neg Split Curves ---
        self.curve_ghost_pos = self.plot_widget.plot(pen=pg.mkPen('#005577', width=1, style=Qt.PenStyle.DashLine),
                                                     symbol='o', symbolSize=3, symbolBrush='#005577', connect='finite')
        self.curve_ghost_neg = self.plot_widget.plot(pen=pg.mkPen('#550000', width=1, style=Qt.PenStyle.DashLine),
                                                     symbol='o', symbolSize=3, symbolBrush='#880000', connect='finite')

        self.curve_live_pos = self.plot_widget.plot(pen=pg.mkPen('#00E5FF', width=2), symbol='o', symbolSize=5,
                                                    symbolBrush='#00E5FF', connect='finite')
        self.curve_live_neg = self.plot_widget.plot(pen=pg.mkPen('#FF5252', width=2), symbol='o', symbolSize=5,
                                                    symbolBrush='#FF5252', connect='finite')

        self.peak_markers = self.plot_widget.plot(pen=None, symbol='t1', symbolSize=10, symbolBrush='#FFEB3B',
                                                  symbolPen='k')
        self.tail_markers = self.plot_widget.plot(pen=None, symbol='x', symbolSize=10, symbolPen='#F44336')
        plot_layout.addWidget(self.plot_widget)

        # --- RIGHT PANEL: Analysis & Calibration ---
        self.analysis_panel_widget = QWidget()
        self.analysis_panel_widget.setFixedWidth(340)
        analysis_layout = QVBoxLayout(self.analysis_panel_widget)
        analysis_layout.setContentsMargins(0, 0, 0, 0)

        self.lbl_data_source = QLabel("<b>Data Source:</b> None")
        self.lbl_data_source.setStyleSheet(
            "font-size: 13px; color: #00E5FF; background-color: #2D2D2D; padding: 6px; "
            "border: 1px solid #3D3D3D; border-radius: 4px; font-weight: bold;"
        )
        analysis_layout.addWidget(self.lbl_data_source)

        file_btn_layout = QHBoxLayout()
        self.btn_load_csv = QPushButton("📂 Load Trace")
        self.btn_load_csv.setStyleSheet(
            "background-color: #607D8B; color: white; font-weight: bold; padding: 6px; border-radius: 4px;")
        self.btn_load_csv.clicked.connect(self._load_csv)
        file_btn_layout.addWidget(self.btn_load_csv)

        self.btn_save_csv = QPushButton("💾 Save Trace")
        self.btn_save_csv.setStyleSheet(
            "background-color: #009688; color: white; font-weight: bold; padding: 6px; border-radius: 4px;")
        self.btn_save_csv.clicked.connect(self._manual_save_csv)
        self.btn_save_csv.setEnabled(False)
        file_btn_layout.addWidget(self.btn_save_csv)

        analysis_layout.addLayout(file_btn_layout)

        grp_calib = QGroupBox("Mass Calibration")
        calib_layout = QGridLayout()

        self.lbl_energy_display = QLabel("--- keV")
        self.lbl_energy_display.setStyleSheet("font-weight: bold; color: #00E5FF;")

        self.sp_k_factor = QDoubleSpinBox()
        self.sp_k_factor.setRange(1.0, 10000.0)
        self.sp_k_factor.setDecimals(2)
        self.sp_k_factor.setSingleStep(1.0)
        self.sp_k_factor.setValue(1391.9)
        self.sp_k_factor.valueChanged.connect(self._recalculate_mass_axis)

        self.sp_i_offset = QDoubleSpinBox()
        self.sp_i_offset.setRange(-10.0, 10.0)
        self.sp_i_offset.setDecimals(3)
        self.sp_i_offset.setSingleStep(0.01)
        self.sp_i_offset.setValue(0.223)
        self.sp_i_offset.valueChanged.connect(self._recalculate_mass_axis)

        calib_layout.addWidget(QLabel("<b>Model:</b> AMU = k * (I + I_off)² / E"), 0, 0, 1, 2)
        calib_layout.addWidget(QLabel("Beam Energy (E):"), 1, 0)
        calib_layout.addWidget(self.lbl_energy_display, 1, 1)
        calib_layout.addWidget(QLabel("Calib. Constant (k):"), 2, 0)
        calib_layout.addWidget(self.sp_k_factor, 2, 1)
        calib_layout.addWidget(QLabel("Current Offset (I_off):"), 3, 0)
        calib_layout.addWidget(self.sp_i_offset, 3, 1)

        self.btn_table = QPushButton("🧪 Open Periodic Table")
        self.btn_table.clicked.connect(self._open_periodic_table)
        calib_layout.addWidget(self.btn_table, 4, 0, 1, 2)

        grp_calib.setLayout(calib_layout)
        analysis_layout.addWidget(grp_calib)

        grp_peaks = QGroupBox("Peak Detection")
        peaks_layout = QVBoxLayout()

        param_layout = QGridLayout()
        self.sp_max_peaks = QSpinBox()
        self.sp_max_peaks.setRange(1, 50)
        self.sp_max_peaks.setValue(15)
        self.sp_max_peaks.valueChanged.connect(self._conditional_peak_update)

        self.sp_sensitivity = QDoubleSpinBox()
        self.sp_sensitivity.setRange(0.001, 100.0)
        self.sp_sensitivity.setDecimals(3)
        self.sp_sensitivity.setSuffix(" %")
        self.sp_sensitivity.setValue(0.05)
        self.sp_sensitivity.valueChanged.connect(self._conditional_peak_update)

        param_layout.addWidget(QLabel("Max Peaks:"), 0, 0)
        param_layout.addWidget(self.sp_max_peaks, 0, 1)
        param_layout.addWidget(QLabel("Sensitivity:"), 1, 0)
        param_layout.addWidget(self.sp_sensitivity, 1, 1)
        peaks_layout.addLayout(param_layout)

        self.btn_detect = QPushButton("🔍 Auto-Detect Peaks")
        self.btn_detect.setCheckable(True)
        self.btn_detect.setStyleSheet(
            "background-color: #2196F3; color: white; font-weight: bold; padding: 8px; border-radius: 4px;")
        self.btn_detect.toggled.connect(self._on_detect_toggled)

        self.peak_table = QTableWidget(0, 3)
        self.peak_table.setHorizontalHeaderLabels(["Mass (AMU)", "Match", "Tail ±0.5"])
        self.peak_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.peak_table.verticalHeader().setVisible(False)
        self.peak_table.setColumnWidth(1, 120)

        self.lbl_res = QLabel("<b>Avg Mass Resolution:</b> ---")
        self.lbl_res.setStyleSheet("font-size: 11px;")

        peaks_layout.addWidget(self.btn_detect)
        peaks_layout.addWidget(self.peak_table)
        peaks_layout.addWidget(self.lbl_res)
        grp_peaks.setLayout(peaks_layout)
        analysis_layout.addWidget(grp_peaks)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.control_panel_widget)
        splitter.addWidget(plot_panel_widget)
        splitter.addWidget(self.analysis_panel_widget)
        splitter.setSizes([340, 800, 340])

        self.main_layout.addWidget(splitter)
        self._lock_ui(False)

    def _refresh_curves(self):
        is_log = self.chk_log_y.isChecked()

        def split_data(x_arr, y_arr):
            if not x_arr or not y_arr:
                return [], [], [], []

            x = np.array(x_arr, dtype=float)
            y = np.array(y_arr, dtype=float)

            if is_log:
                y_pos = np.copy(y)
                y_neg = np.copy(y)

                # Positive curve gets valid positive data, else NaN (breaks the line)
                y_pos[y <= 0] = np.nan

                # Negative curve gets magnitude of negative data, else NaN
                y_neg[y >= 0] = np.nan
                y_neg = np.abs(y_neg)

                return x, y_pos, x, y_neg
            else:
                # Linear mode: plot the raw signal entirely on the positive curve
                return x, y, [], []

        x_live, y_live_pos, x_live_neg, y_live_neg = split_data(self.amu_data, self.y_data)
        self.curve_live_pos.setData(x_live, y_live_pos)
        self.curve_live_neg.setData(x_live_neg, y_live_neg)

        x_ghost, y_ghost_pos, x_ghost_neg, y_ghost_neg = split_data(self.ghost_amu, self.ghost_y)
        self.curve_ghost_pos.setData(x_ghost, y_ghost_pos)
        self.curve_ghost_neg.setData(x_ghost_neg, y_ghost_neg)

    def _open_periodic_table(self):
        dlg = PeriodicTableDialog(self.selected_elements, self)
        if dlg.exec():
            self.selected_elements = dlg.selected
            self._update_mass_db()
            self._conditional_peak_update()

    def _on_detect_toggled(self, checked):
        if checked:
            self.btn_detect.setText("⏹ Stop Auto-Detect")
            self.btn_detect.setStyleSheet(
                "background-color: #F44336; color: white; font-weight: bold; padding: 8px; border-radius: 4px;")
            self._run_peak_detection()
        else:
            self.btn_detect.setText("🔍 Auto-Detect Peaks")
            self.btn_detect.setStyleSheet(
                "background-color: #2196F3; color: white; font-weight: bold; padding: 8px; border-radius: 4px;")
            self._clear_peaks()

    def _recalculate_mass_axis(self):
        k = self.sp_k_factor.value()
        i_off = self.sp_i_offset.value()

        if self.amps_data:
            self.amu_data = []
            for amps, e_kev in zip(self.amps_data, self.energy_data):
                e_ev = (e_kev * 1000.0) if e_kev > 0 else 30000.0
                amu = k * ((amps + i_off) ** 2) / e_ev
                self.amu_data.append(amu)
            self._refresh_curves()

        if self.ghost_amps:
            self.ghost_amu = []
            for amps, e_kev in zip(self.ghost_amps, self.ghost_energy):
                e_ev = (e_kev * 1000.0) if e_kev > 0 else 30000.0
                amu = k * ((amps + i_off) ** 2) / e_ev
                self.ghost_amu.append(amu)
            self._refresh_curves()

        self._conditional_peak_update()
        self._update_plot_limits()

    def _conditional_peak_update(self):
        if self.btn_detect.isChecked():
            self._run_peak_detection()

    def _clear_peaks(self):
        self.peak_table.setRowCount(0)
        self.peak_markers.setData([], [])
        self.tail_markers.setData([], [])
        for lbl in self.peak_labels:
            self.plot_widget.removeItem(lbl)
        self.peak_labels.clear()
        self.lbl_res.setText("<b>Avg Mass Resolution:</b> ---")

    def _update_energy_display(self):
        if not self.energy_data:
            self.lbl_energy_display.setText("--- keV")
            return

        valid_energy = [e for e in self.energy_data if e > 0]
        if not valid_energy:
            self.lbl_energy_display.setText("Unknown")
            return

        mean_e = np.mean(valid_energy)
        min_e = np.min(valid_energy)
        max_e = np.max(valid_energy)

        mean_str = f"{mean_e:.2f}"
        min_str = f"{min_e:.2f}"
        max_str = f"{max_e:.2f}"

        if min_str == max_str:
            self.lbl_energy_display.setText(f"{mean_str} keV")
        else:
            variation = (max_e - min_e) / 2.0
            self.lbl_energy_display.setText(f"{mean_str} ± {variation:.2f} keV")

    def _run_peak_detection(self):
        self._clear_peaks()

        if len(self.amu_data) < 10 or len(self.y_data) < 10:
            return

        try:
            from scipy.signal import find_peaks, peak_widths

            x_arr = np.array(self.amu_data)
            y_arr = np.array(self.y_data)
            y_clean = np.clip(y_arr, 0, None)

            signal_range = np.max(y_clean) - np.min(y_clean)
            min_prominence = max(1e-13, signal_range * (self.sp_sensitivity.value() / 100.0))

            peaks, properties = find_peaks(y_clean, prominence=min_prominence)

            if len(peaks) == 0:
                self.lbl_res.setText("<span style='color: #F44336; font-weight: bold;'>No peaks detected.</span>")
                return

            scores = []
            for i, p_idx in enumerate(peaks):
                prominence = properties["prominences"][i]
                amu = x_arr[p_idx]
                mass_error = abs(amu - round(amu))
                penalty = math.exp(-0.5 * (mass_error / 0.1) ** 2)
                scores.append(prominence * penalty)

            scores = np.array(scores)
            sorted_indices = np.argsort(scores)[::-1]
            top_indices = sorted_indices[:self.sp_max_peaks.value()]
            selected_peaks = peaks[top_indices]
            selected_peaks_x_sorted = np.sort(selected_peaks)

            widths, width_heights, left_ips, right_ips = peak_widths(y_clean, selected_peaks_x_sorted, rel_height=0.5)
            fwhm_amu = []
            if len(left_ips) > 0 and len(right_ips) > 0:
                left_amu = np.interp(left_ips, np.arange(len(x_arr)), x_arr)
                right_amu = np.interp(right_ips, np.arange(len(x_arr)), x_arr)
                fwhm_amu = right_amu - left_amu
            else:
                fwhm_amu = np.zeros(len(selected_peaks_x_sorted))

            is_log = self.chk_log_y.isChecked()

            self.peak_table.setRowCount(len(selected_peaks_x_sorted))

            MIN_SPACING = 0.6
            placed_label_x = []
            peaks_by_y = sorted(selected_peaks_x_sorted, key=lambda idx: y_clean[idx], reverse=True)
            peak_label_info = {}
            total_resolution = 0.0
            valid_peaks_count = 0

            for p_idx in peaks_by_y:
                center_amu = x_arr[p_idx]
                closest_mass = int(round(center_amu))
                mass_error = abs(center_amu - closest_mass)

                match_html = ""
                lbl_color = "#AAAAAA"

                if mass_error <= 0.25:
                    candidates = self.MASS_DB.get(closest_mass, [])
                    if candidates:
                        candidates = sorted(candidates, key=lambda c: c['prob'], reverse=True)
                        prioritized = [c for c in candidates if c['is_selected']]
                        others = [c for c in candidates if not c['is_selected']]

                        if prioritized:
                            match_html = "<br>".join([c['html'] for c in prioritized[:2]])
                            lbl_color = "#FFEB3B"
                        elif others:
                            match_html = others[0]['html']
                            lbl_color = "#FF9800"

                overlap = any(abs(center_amu - px) < MIN_SPACING for px in placed_label_x)
                if not overlap and match_html:
                    placed_label_x.append(center_amu)
                    html_content = f"<div style='text-align: center; color: {lbl_color}; font-size: 10pt; font-weight: bold;'>{match_html}<br>({center_amu:.1f})</div>"
                    txt = pg.TextItem(html=html_content, anchor=(0.5, 1.2))
                    y_anchor = math.log10(max(y_clean[p_idx], 1e-15)) if is_log else y_clean[p_idx]
                    txt.setPos(center_amu, y_anchor)
                    self.plot_widget.addItem(txt)
                    self.peak_labels.append(txt)

                peak_label_info[p_idx] = match_html

            tail_x_list = []
            tail_y_list = []

            for row_idx, (p_idx, f_amu) in enumerate(zip(selected_peaks_x_sorted, fwhm_amu)):
                center_amu = x_arr[p_idx]
                peak_current = y_clean[p_idx]

                res = center_amu / f_amu if f_amu > 0 else 0.0
                if res > 0:
                    total_resolution += res
                    valid_peaks_count += 1

                tail_left = np.interp(center_amu - 0.5, x_arr, y_clean)
                tail_right = np.interp(center_amu + 0.5, x_arr, y_clean)
                worst_tail = max(tail_left, tail_right)

                tail_x_list.extend([center_amu - 0.5, center_amu + 0.5])
                tail_y_list.extend([max(1e-15, tail_left), max(1e-15, tail_right)])

                crosstalk_ppm = (worst_tail / peak_current) * 1e6 if peak_current > 0 else 0.0

                if crosstalk_ppm < 1:
                    tail_str = "< 1 ppm"
                elif crosstalk_ppm > 500000:
                    tail_str = "Overlapped"
                else:
                    tail_str = f"{crosstalk_ppm:,.0f} ppm"

                lbl_match = QLabel(peak_label_info.get(p_idx, ""))
                lbl_match.setAlignment(Qt.AlignmentFlag.AlignCenter)

                self.peak_table.setItem(row_idx, 0, QTableWidgetItem(f"{center_amu:.2f}"))
                self.peak_table.setCellWidget(row_idx, 1, lbl_match)

                tail_item = QTableWidgetItem(tail_str)

                c_green = np.array([46, 125, 50])
                c_amber = np.array([255, 143, 0])
                c_red = np.array([198, 40, 40])

                val = math.log10(max(1, crosstalk_ppm))
                if val <= 2.0:
                    c = c_green
                elif val < 3.0:
                    f = val - 2.0
                    c = c_green + f * (c_amber - c_green)
                elif val < 4.0:
                    f = val - 3.0
                    c = c_amber + f * (c_red - c_amber)
                else:
                    c = c_red

                tail_item.setForeground(pg.mkColor(int(c[0]), int(c[1]), int(c[2])))
                self.peak_table.setItem(row_idx, 2, tail_item)

            avg_res = total_resolution / valid_peaks_count if valid_peaks_count > 0 else 0.0
            self.lbl_res.setText(
                f"<b>Avg Mass Resolution (M/ΔM):</b> <span style='color: #4CAF50;'>{avg_res:.1f}</span>")

            safe_peak_y = np.clip(y_arr[selected_peaks_x_sorted], 1e-15, None)
            if is_log:
                self.peak_markers.setData(x_arr[selected_peaks_x_sorted], np.log10(safe_peak_y))
                self.tail_markers.setData(tail_x_list, np.log10(np.clip(tail_y_list, 1e-15, None)))
            else:
                self.peak_markers.setData(x_arr[selected_peaks_x_sorted], safe_peak_y)
                self.tail_markers.setData(tail_x_list, tail_y_list)

        except ImportError:
            self.btn_detect.setChecked(False)
            QMessageBox.critical(self, "Dependency Error",
                                 "scipy library required for peak detection. (pip install scipy)")
        except Exception as e:
            self.btn_detect.setChecked(False)
            QMessageBox.critical(self, "Analysis Error", f"Peak detection failed:\n{e}")

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
            if file_path.endswith('.csv'):
                file_path = file_path[:-4]
            self.edit_export_path.setText(file_path)

    def _load_csv(self):
        current_dir = os.path.dirname(self.edit_export_path.text())
        filepath, _ = QFileDialog.getOpenFileName(self, "Load Mass Scan CSV", current_dir, "CSV Files (*.csv)")

        if not filepath: return

        try:
            x_vals, y_vals, energy_vals = [], [], []
            has_energy_data = False

            with open(filepath, 'r') as f:
                reader = csv.DictReader(f)
                for i, row in enumerate(reader):
                    if i == 0:
                        has_energy_data = ('Target_kV' in row and 'Extraction_kV' in row)

                    mag = float(row.get('Measured_Magnet_A', 0))
                    beam = float(row.get('Measured_Beam_A', 0))
                    x_vals.append(mag)
                    y_vals.append(beam)

                    if has_energy_data:
                        t_kv = float(row.get('Target_kV', 0) or 0.0)
                        e_kv = float(row.get('Extraction_kV', 0) or 0.0)
                        energy_vals.append(t_kv + e_kv)
                    else:
                        energy_vals.append(0.0)

            self.amps_data = x_vals
            self.y_data = y_vals
            self.energy_data = energy_vals
            self.last_scan_data = list(zip(x_vals, x_vals, y_vals, energy_vals, [0.0] * len(energy_vals)))
            self.btn_save_csv.setEnabled(True)

            self._recalculate_mass_axis()
            self._update_energy_display()
            self._conditional_peak_update()
            self._update_plot_limits()

            if not has_energy_data:
                QMessageBox.warning(self, "Missing Data",
                                    "CSV missing voltage columns; peak mass analysis may use defaults.")

            self.lbl_data_source.setText(f"<b>Data Source:</b> Loaded {os.path.basename(filepath)}")

        except Exception as e:
            QMessageBox.critical(self, "Load Error", f"Failed to parse CSV:\n{e}")

    def _update_eta(self):
        try:
            num_steps = int(round(abs(self.sp_stop.value() - self.sp_start.value()) / self.sp_step.value())) + 1

            # Ensure estimation correctly accounts for physical SMU integration delay
            physical_integration_sec = self.sp_nplc.value() * 0.02
            effective_interval = max(self.sp_interval.value(), physical_integration_sec + 0.05)

            self.time_per_point = 0.05 + self.sp_settle.value() + (self.sp_samples.value() * effective_interval)

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
                target_amu = mouse_point.x()

                k = self.sp_k_factor.value()
                i_off = self.sp_i_offset.value()
                e_kev = self.energy_data[-1] if self.energy_data else 30.0
                e_ev = e_kev * 1000.0 if e_kev > 0 else 30000.0

                try:
                    target_a = math.sqrt(max(0, (target_amu * e_ev) / k)) - i_off
                    target_a = max(0.0, target_a)
                except Exception:
                    target_a = 0.0

                self.cmd_thread.send_command("magnet", "ion_beam.beamline.magnet.sp_requested_current", float(target_a),
                                             origin="hmi")
                self.lbl_status.setText(f"Status: Manual Jump to {target_a:.3f} A ({target_amu:.1f} AMU)")
                self.event_helper.log_user_marker(time.time(), f"Manual Magnet Jump: {target_a:.3f} A via Mass Scan",
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

        self.lbl_data_source.setText("<b>Data Source:</b> Live Scan")
        self.lbl_status.setText("Status: Scanning...")

        self._clear_plot()
        self._lock_ui(True)

        config = {
            'start_a': self.sp_start.value(),
            'stop_a': self.sp_stop.value(),
            'step_a': self.sp_step.value(),
            'tolerance': self.sp_tol.value(),
            'settle_time': self.sp_settle.value(),
            'samples': self.sp_samples.value(),
            'sample_interval': self.sp_interval.value(),
            'nplc': self.sp_nplc.value(),
            'loop': self.chk_loop.isChecked()
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
        widgets = [self.sp_start, self.sp_stop, self.sp_step, self.sp_tol,
                   self.sp_settle, self.sp_samples, self.sp_interval, self.sp_nplc,
                   self.chk_loop, self.edit_export_path]
        for w in widgets:
            w.setEnabled(not locked)

        self.btn_start.setEnabled(not locked)
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

    def _update_plot(self, amps, beam, energy_kev):
        k = self.sp_k_factor.value()
        i_off = self.sp_i_offset.value()
        e_ev = (energy_kev * 1000.0) if energy_kev > 0 else 30000.0
        amu = k * ((amps + i_off) ** 2) / e_ev

        self.amps_data.append(amps)
        self.amu_data.append(amu)
        self.y_data.append(beam)
        self.energy_data.append(energy_kev)
        self._refresh_curves()
        self._update_plot_limits()

    def _clear_plot(self):
        self.amps_data.clear()
        self.amu_data.clear()
        self.y_data.clear()
        self.energy_data.clear()
        self.ghost_energy.clear()
        self.last_scan_data.clear()
        self.btn_save_csv.setEnabled(False)
        self._refresh_curves()
        self._clear_peaks()
        self._update_energy_display()
        self._update_plot_limits()
        if not self.worker or not self.worker.isRunning():
            self.lbl_data_source.setText("<b>Data Source:</b> None")

    def _toggle_log_y(self, state):
        is_log = (state == Qt.CheckState.Checked.value)

        view_box = self.plot_widget.getViewBox()
        view_rect = view_box.viewRect()
        x_min, x_max = view_rect.left(), view_rect.right()
        y_min, y_max = view_rect.bottom(), view_rect.top()

        SMU_LIMIT = 1e-11

        if is_log:
            # Check the absolute magnitude so negative spikes inform our limits
            visible_y = [abs(y) for x, y in zip(self.amu_data, self.y_data) if x_min <= x <= x_max and abs(y) > 0]
            if visible_y:
                data_min = min(visible_y)
                floor_y = max(SMU_LIMIT, data_min * 0.8)
            else:
                floor_y = SMU_LIMIT

            top_y = max(y_max, SMU_LIMIT * 10)
            new_ymin = math.log10(floor_y)
            new_ymax = math.log10(top_y)

            view_box.setLimits(yMin=math.log10(1e-15), yMax=new_ymax + 5.0)
            self.plot_widget.setLogMode(x=False, y=True)

        else:
            safe_y_max = min(y_max, 50.0)
            new_ymax = max(1e-12, 10 ** safe_y_max)
            new_ymin = - (new_ymax * 0.05)

            self.plot_widget.setLogMode(x=False, y=False)
            view_box.setLimits(yMin=None, yMax=new_ymax * 10.0)

        view_box.setXRange(x_min, x_max, padding=0)
        view_box.setYRange(new_ymin, new_ymax, padding=0)

        self._refresh_curves()
        self._update_plot_limits()
        self._conditional_peak_update()

    def _update_plot_limits(self):
        is_log = self.chk_log_y.isChecked()

        all_x = self.amu_data + self.ghost_amu
        all_y = self.y_data + self.ghost_y

        if all_x:
            data_x_min = min(all_x)
            data_x_max = max(all_x)
        else:
            data_x_min = 0.0
            data_x_max = 100.0

        x_limit_min = max(0.0, data_x_min - 2.0)
        x_limit_max = data_x_max + 2.0

        if all_y:
            if is_log:
                abs_y = [abs(y) for y in all_y if abs(y) > 0]
                max_y = max(max(abs_y), 1e-12) if abs_y else 1e-6
            else:
                max_y = max(max(all_y), 1e-12)
        else:
            max_y = 1e-6

        if is_log:
            y_limit_max = math.log10(max_y * 10.0)
            y_limit_min = math.log10(1e-15)
        else:
            y_limit_max = max_y * 10.0
            y_limit_min = None

        self.plot_widget.getViewBox().setLimits(
            xMin=x_limit_min,
            xMax=x_limit_max,
            yMax=y_limit_max,
            yMin=y_limit_min
        )

    def _manual_save_csv(self):
        if not self.last_scan_data:
            QMessageBox.warning(self, "No Data", "No trace data available to save.")
            return
        self._export_to_csv(self.last_scan_data)

    def _handle_scan_completion(self, scan_data: list):
        self.last_scan_data = scan_data
        self.btn_save_csv.setEnabled(True)
        self._update_energy_display()
        self._conditional_peak_update()

        if self.chk_export.isChecked() and len(scan_data) > 0:
            self._export_to_csv(scan_data)
        else:
            self.lbl_data_source.setText("<b>Data Source:</b> Recent Scan (Unsaved)")

        if self.chk_loop.isChecked() and self.worker and not self.worker.is_stopped:
            self.ghost_amps = list(self.amps_data)
            self.ghost_amu = list(self.amu_data)
            self.ghost_y = list(self.y_data)
            self.ghost_energy = list(self.energy_data)

            self.amps_data.clear()
            self.amu_data.clear()
            self.y_data.clear()
            self.energy_data.clear()
            self._refresh_curves()
            self._clear_peaks()
            self._update_plot_limits()
        else:
            self._lock_ui(False)
            self.lbl_status.setText("Status: Idle")

    def _export_to_csv(self, data: list):
        try:
            base_path = self.edit_export_path.text().strip()
            if not base_path:
                base_path = os.path.abspath(
                    os.path.join(os.path.dirname(__file__), "../../../../data/exports/Mass_Scans", "MassScan"))

            export_dir = os.path.dirname(base_path)
            base_name = os.path.basename(base_path)
            os.makedirs(export_dir, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{base_name}_{timestamp}.csv"
            filepath = os.path.join(export_dir, filename)

            with open(filepath, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(["Timestamp", "Requested_Magnet_A", "Measured_Magnet_A", "Measured_Beam_A", "Target_kV",
                                 "Extraction_kV"])
                now_str = datetime.now().isoformat()
                for req_mag, act_mag, act_beam, target_kv, ext_kv in data:
                    writer.writerow([now_str, req_mag, act_mag, act_beam, target_kv, ext_kv])

            self.lbl_data_source.setText(f"<b>Data Source:</b> Saved as {filename}")
        except Exception as e:
            print(f"[Mass Scan] Export Failed: {e}")