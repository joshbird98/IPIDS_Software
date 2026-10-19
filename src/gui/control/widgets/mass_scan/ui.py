"""Layout of the Mass Scan panel: settings (left), plot (centre), analysis and calibration (right).
build(w) creates the controls as attributes of the panel w and connects them to its methods."""
import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QCheckBox, QDoubleSpinBox, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                             QLineEdit, QPushButton, QSpinBox, QSplitter, QTableWidget, QVBoxLayout, QWidget)

from src.gui.control.setpoint_box import SetpointBox
from src.gui.control.widgets.mass_scan.axis import SmartCurrentAxis
from src.sequencer.mass_scan import DEFAULT_EXPORT, NPLC_RANGE

START_STYLE = "background-color: #4CAF50; color: white; font-weight: bold; padding: 12px; border-radius: 4px;"
STOP_STYLE = "background-color: #F44336; color: white; font-weight: bold; padding: 12px; border-radius: 4px;"
OFF_STYLE = "background-color: #555555; color: #888888; font-weight: bold; padding: 12px; border-radius: 4px;"
PANEL_WIDTH = 340


def _spin(lo, hi, decimals, value, suffix="", step=None, tip="", box=None):
    box = box if box is not None else QDoubleSpinBox()
    box.setRange(lo, hi)
    box.setDecimals(decimals)
    box.setValue(value)
    if suffix:
        box.setSuffix(suffix)
    if step:
        box.setSingleStep(step)
    if tip:
        box.setToolTip(tip)
    return box


def _grid(group_title, rows):
    group = QGroupBox(group_title)
    grid = QGridLayout()
    for r, (label, widget) in enumerate(rows):
        grid.addWidget(QLabel(label), r, 0)
        grid.addWidget(widget, r, 1)
    group.setLayout(grid)
    return group


def build(w):
    w.main_layout = QHBoxLayout(w)
    splitter = QSplitter(Qt.Orientation.Horizontal)
    splitter.addWidget(_settings_panel(w))
    splitter.addWidget(_plot_panel(w))
    splitter.addWidget(_analysis_panel(w))
    splitter.setSizes([PANEL_WIDTH, 800, PANEL_WIDTH])
    w.main_layout.addWidget(splitter)


def _settings_panel(w):
    w.control_panel_widget = QWidget()
    w.control_panel_widget.setFixedWidth(PANEL_WIDTH)
    panel = QVBoxLayout(w.control_panel_widget)
    panel.setContentsMargins(0, 0, 0, 0)

    w.sp_start = _spin(0.0, 150.0, 3, 0.0, " A")
    w.sp_stop = _spin(0.0, 150.0, 3, 40.0, " A")
    w.sp_step = _spin(0.001, 10.0, 3, 0.05, " A")
    panel.addWidget(_grid("Sweep Parameters", [("Start:", w.sp_start), ("Stop:", w.sp_stop), ("Step:", w.sp_step)]))

    w.sp_tol = _spin(0.001, 1.0, 3, 0.05, " A", tip="Target hardware tolerance measured in Magnet Amps.")
    w.sp_settle = _spin(0.0, 5.0, 2, 0.1, " s")
    w.sp_samples = QSpinBox()
    w.sp_samples.setRange(1, 1000)
    w.sp_samples.setValue(5)
    w.sp_interval = _spin(0.01, 1.0, 3, 0.02, " s")
    w.sp_nplc = _spin(NPLC_RANGE[0], NPLC_RANGE[1], 3, 10.0,
                      tip="SMU Integration Time (1 NPLC = 20ms). 10.0 = 200ms per sample for ultimate low noise.")
    panel.addWidget(_grid("Acquisition / Settling", [("HW Tol:", w.sp_tol), ("Wait Time:", w.sp_settle),
                                                     ("Samples:", w.sp_samples), ("Interval:", w.sp_interval),
                                                     ("SMU NPLC:", w.sp_nplc)]))
    for box in (w.sp_start, w.sp_stop, w.sp_step, w.sp_settle, w.sp_samples, w.sp_interval, w.sp_nplc):
        box.valueChanged.connect(w._update_eta)

    group = QGroupBox("Execution & Output")
    layout = QVBoxLayout()
    w.chk_loop = QCheckBox("Continuous Loop")
    w.chk_export = QCheckBox("Auto-Export CSV on Complete")
    w.chk_export.setChecked(True)
    w.chk_export.setToolTip("The sequencer saves each finished pass, even if the HMI is closed.")
    w.edit_export_path = QLineEdit(DEFAULT_EXPORT)
    btn_browse = QPushButton("...")
    btn_browse.setMaximumWidth(30)
    btn_browse.clicked.connect(w._browse_export_path)
    export_row = QHBoxLayout()
    export_row.addWidget(w.edit_export_path)
    export_row.addWidget(btn_browse)
    w.lbl_eta = QLabel("Est. Scan Time: --:--")
    w.lbl_eta.setStyleSheet("color: #2196F3; font-weight: bold;")
    w.lbl_rem_time = QLabel("Remaining: --:--")
    w.lbl_rem_time.setStyleSheet("color: #FF9800; font-weight: bold;")
    time_row = QHBoxLayout()
    time_row.addWidget(w.lbl_eta)
    time_row.addWidget(w.lbl_rem_time)
    w.btn_start = QPushButton("START PRECISION SWEEP")
    w.btn_start.setStyleSheet(START_STYLE)
    w.btn_start.clicked.connect(w._start_scan)
    w.btn_stop = QPushButton("ABORT")
    w.btn_stop.setStyleSheet(OFF_STYLE)
    w.btn_stop.setEnabled(False)
    w.btn_stop.clicked.connect(w._stop_scan)
    w.lbl_status = QLabel("Status: Idle")
    w.lbl_status.setWordWrap(True)
    w.lbl_status.setStyleSheet("font-family: monospace; color: #FF9800;")
    for item in (w.chk_loop, w.chk_export, QLabel("Base Export Path:"), export_row, time_row, w.btn_start,
                 w.btn_stop, w.lbl_status):
        (layout.addLayout if isinstance(item, QHBoxLayout) else layout.addWidget)(item)
    group.setLayout(layout)
    panel.addWidget(group)
    panel.addStretch()
    return w.control_panel_widget


def _plot_panel(w):
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    toolbar = QHBoxLayout()
    w.btn_toggle_ctrl = QPushButton("◀ Setup")
    w.btn_toggle_ctrl.clicked.connect(w._toggle_left_panel)
    w.chk_log_y = QCheckBox("Logarithmic Y-Axis")
    w.chk_log_y.stateChanged.connect(w._toggle_log_y)
    w.btn_clear = QPushButton("Clear Plot")
    w.btn_clear.clicked.connect(w._clear_plot)
    w.btn_toggle_analysis = QPushButton("Analysis ▶")
    w.btn_toggle_analysis.clicked.connect(w._toggle_right_panel)
    toolbar.addWidget(w.btn_toggle_ctrl)
    toolbar.addWidget(w.chk_log_y)
    toolbar.addStretch()
    toolbar.addWidget(w.btn_clear)
    toolbar.addWidget(w.btn_toggle_analysis)
    layout.addLayout(toolbar)

    pg.setConfigOption('background', '#1E1E1E')
    pg.setConfigOption('foreground', 'w')
    w.current_axis = SmartCurrentAxis(orientation='left')
    w.plot_widget = pg.PlotWidget(axisItems={'left': w.current_axis})
    # Y follows the data in the visible mass range (also after zooming in x)
    w.plot_widget.getViewBox().setAutoVisible(y=True)
    w.plot_widget.setLabel('bottom', "Mass (AMU)")
    w.plot_widget.setLabel('left', "Beam Current")
    w.plot_widget.showGrid(x=True, y=True, alpha=0.5)
    w.plot_widget.scene().sigMouseClicked.connect(w._on_mouse_clicked)

    plot = w.plot_widget.plot
    w.curve_ghost_pos = plot(pen=pg.mkPen('#005577', width=1, style=Qt.PenStyle.DashLine), symbol='o', symbolSize=3,
                             symbolBrush='#005577', connect='finite')
    w.curve_ghost_neg = plot(pen=pg.mkPen('#550000', width=1, style=Qt.PenStyle.DashLine), symbol='o', symbolSize=3,
                             symbolBrush='#880000', connect='finite')
    w.curve_live_pos = plot(pen=pg.mkPen('#00E5FF', width=2), symbol='o', symbolSize=5, symbolBrush='#00E5FF',
                            connect='finite')
    w.curve_live_neg = plot(pen=pg.mkPen('#FF5252', width=2), symbol='o', symbolSize=5, symbolBrush='#FF5252',
                            connect='finite')
    w.peak_markers = plot(pen=None, symbol='t1', symbolSize=10, symbolBrush='#FFEB3B', symbolPen='k')
    w.tail_markers = plot(pen=None, symbol='x', symbolSize=10, symbolPen='#F44336')
    w.calib_scatter = plot(pen=None, symbol='star', symbolSize=15, symbolBrush='#00FF00', symbolPen='k')
    layout.addWidget(w.plot_widget)
    return widget


def _analysis_panel(w):
    w.analysis_panel_widget = QWidget()
    w.analysis_panel_widget.setFixedWidth(PANEL_WIDTH)
    layout = QVBoxLayout(w.analysis_panel_widget)
    layout.setContentsMargins(0, 0, 0, 0)

    w.lbl_data_source = QLabel("<b>Data Source:</b> None")
    w.lbl_data_source.setWordWrap(True)
    w.lbl_data_source.setMinimumHeight(52)          # room for a saved file's name on a second line
    w.lbl_data_source.setStyleSheet("font-size: 13px; color: #00E5FF; background-color: #2D2D2D; padding: 6px; "
                                    "border: 1px solid #3D3D3D; border-radius: 4px; font-weight: bold;")
    layout.addWidget(w.lbl_data_source)
    files = QHBoxLayout()
    w.btn_load_csv = QPushButton("📂 Load Trace")
    w.btn_load_csv.setStyleSheet("background-color: #607D8B; color: white; font-weight: bold; padding: 6px; "
                                 "border-radius: 4px;")
    w.btn_load_csv.clicked.connect(w._load_csv)
    w.btn_save_csv = QPushButton("💾 Save Trace")
    w.btn_save_csv.setStyleSheet("background-color: #009688; color: white; font-weight: bold; padding: 6px; "
                                 "border-radius: 4px;")
    w.btn_save_csv.clicked.connect(w._manual_save_csv)
    w.btn_save_csv.setEnabled(False)
    files.addWidget(w.btn_load_csv)
    files.addWidget(w.btn_save_csv)
    layout.addLayout(files)

    calib = QGroupBox("Mass Calibration")
    grid = QGridLayout()
    w.lbl_energy_display = QLabel("--- keV")
    w.lbl_energy_display.setStyleSheet("font-weight: bold; color: #00E5FF;")
    # SetpointBox: "entered by hand" only when someone types or steps one (any focus-out used to
    # relabel a fitted calibration as typed - and that label goes into "Calculate & set amps" markers)
    w.sp_k_factor = _spin(1.0, 10000.0, 2, 1391.9, step=1.0, box=SetpointBox(live=False))
    w.sp_i_offset = _spin(-10.0, 10.0, 3, 0.223, step=0.01, box=SetpointBox(live=False))
    w.sp_k_factor.valueChanged.connect(w._recalculate_mass_axis)
    w.sp_i_offset.valueChanged.connect(w._recalculate_mass_axis)
    w.sp_k_factor.committed.connect(w._calibration_typed)
    w.sp_i_offset.committed.connect(w._calibration_typed)
    grid.addWidget(QLabel("<b>Model:</b> AMU = k * (I + I_off)² / E"), 0, 0, 1, 2)
    for r, (label, widget) in enumerate((("Beam Energy (E):", w.lbl_energy_display),
                                         ("Calib. Constant (k):", w.sp_k_factor),
                                         ("Current Offset (I_off):", w.sp_i_offset)), start=1):
        grid.addWidget(QLabel(label), r, 0)
        grid.addWidget(widget, r, 1)
    w.btn_table = QPushButton("🧪 Open Periodic Table")
    w.btn_table.clicked.connect(w._open_periodic_table)
    grid.addWidget(w.btn_table, 4, 0, 1, 2)
    calib.setLayout(grid)
    layout.addWidget(calib)

    fit = QGroupBox("Multi-Point Auto Fit")
    fit_layout = QVBoxLayout()
    w.table_calib = QTableWidget(0, 2)
    w.table_calib.setHorizontalHeaderLabels(["Magnet (A)", "True AMU"])
    w.table_calib.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    w.table_calib.verticalHeader().setVisible(False)
    w.table_calib.setMaximumHeight(120)
    buttons = QHBoxLayout()
    w.btn_clear_calib = QPushButton("Clear")
    w.btn_clear_calib.clicked.connect(w._clear_calib_points)
    w.btn_fit_calib = QPushButton("Calculate Fit")
    w.btn_fit_calib.clicked.connect(w._calculate_fit)
    w.btn_fit_calib.setStyleSheet("background-color: #9C27B0; color: white; font-weight: bold;")
    buttons.addWidget(w.btn_clear_calib)
    buttons.addWidget(w.btn_fit_calib)
    w.lbl_fit_stats = QLabel("")
    w.lbl_fit_stats.setStyleSheet("color: #00E5FF; font-weight: bold; font-size: 11px;")
    w.lbl_fit_stats.setAlignment(Qt.AlignmentFlag.AlignCenter)
    hint = QLabel("<i>Ctrl + Left Click on plot peaks to assign True AMU.</i>")
    hint.setTextFormat(Qt.TextFormat.RichText)
    fit_layout.addWidget(hint)
    fit_layout.addWidget(w.table_calib)
    fit_layout.addLayout(buttons)
    fit_layout.addWidget(w.lbl_fit_stats)
    fit.setLayout(fit_layout)
    layout.addWidget(fit)

    peaks = QGroupBox("Peak Detection")
    peaks_layout = QVBoxLayout()
    w.sp_max_peaks = QSpinBox()
    w.sp_max_peaks.setRange(1, 100)
    w.sp_max_peaks.setValue(15)
    w.sp_max_peaks.valueChanged.connect(w._conditional_peak_update)
    w.sp_sensitivity = _spin(0.0001, 100.0, 3, 0.05, " %")
    w.sp_sensitivity.valueChanged.connect(w._conditional_peak_update)
    params = QGridLayout()
    params.addWidget(QLabel("Max Peaks:"), 0, 0)
    params.addWidget(w.sp_max_peaks, 0, 1)
    params.addWidget(QLabel("Sensitivity:"), 1, 0)
    params.addWidget(w.sp_sensitivity, 1, 1)
    peaks_layout.addLayout(params)
    w.btn_detect = QPushButton("🔍 Auto-Detect Peaks")
    w.btn_detect.setCheckable(True)
    w.btn_detect.setStyleSheet("background-color: #2196F3; color: white; font-weight: bold; padding: 8px; "
                               "border-radius: 4px;")
    w.btn_detect.toggled.connect(w._on_detect_toggled)
    w.peak_table = QTableWidget(0, 3)
    w.peak_table.setHorizontalHeaderLabels(["Mass (AMU)", "Match", "Tail ±0.5"])
    w.peak_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    w.peak_table.verticalHeader().setVisible(False)
    w.peak_table.setColumnWidth(1, 120)
    w.lbl_res = QLabel("<b>Avg Mass Resolution:</b> ---")
    w.lbl_res.setStyleSheet("font-size: 11px;")
    peaks_layout.addWidget(w.btn_detect)
    peaks_layout.addWidget(w.peak_table)
    peaks_layout.addWidget(w.lbl_res)
    peaks.setLayout(peaks_layout)
    layout.addWidget(peaks)
    return w.analysis_panel_widget
