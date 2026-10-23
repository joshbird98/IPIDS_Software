"""Layout of the Mass Scan panel: settings (left), plot (centre), analysis and calibration (right).
build(w) creates the controls as attributes of the panel w and connects them to its methods."""
import pyqtgraph as pg
from PyQt6.QtCore import QEvent, QObject, Qt
from PyQt6.QtWidgets import (QButtonGroup, QCheckBox, QDoubleSpinBox, QFormLayout, QFrame, QGroupBox,
                             QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QScrollArea, QSizePolicy,
                             QSpinBox, QSplitter, QTableWidget, QVBoxLayout, QWidget)

from src.gui import theme
from src.gui.control.setpoint_box import SetpointBox
from src.gui.control.widgets.mass_scan.axis import OtherUnitAxis, SmartCurrentAxis
from src.gui.style import set_role, set_tone
from src.sequencer.mass_scan import DEFAULT_EXPORT, NPLC_RANGE

PANEL_WIDTH = 280               # the side panels' starting width (drag the dividers to change it)
PANEL_MIN_WIDTH = 210
# The x axis: the plot against mass or against the measured magnet current (and the other on top)
X_LABELS = {"amu": "Mass (AMU)", "amps": "Magnet current (A)"}


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


def _form():
    """Label beside its field while there is room; the field below its label in a narrow panel."""
    form = QFormLayout()
    form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    return form


def _grid(group_title, rows):
    group = QGroupBox(group_title)
    form = _form()
    for label, widget in rows:
        form.addRow(label, widget)
    group.setLayout(form)
    return group


def _side(content: QWidget) -> QScrollArea:
    """A side panel that scrolls on its own: the plot keeps the panel's height, however tall the
    settings are."""
    scroll = QScrollArea()
    scroll.setObjectName("PanelScroll")
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)      # a click on empty space leaves the focus be
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setWidget(content)
    # Never narrower than its contents (which would be cut off: no sideways scrolling here)
    content.ensurePolished()
    scroll.setMinimumWidth(max(PANEL_MIN_WIDTH, content.minimumSizeHint().width()
                               + scroll.verticalScrollBar().sizeHint().width() + 2))
    return scroll


class _Balance(QObject):
    """Shares the width out as the panel is resized - side panels about a quarter each, between
    PANEL_MIN_WIDTH and PANEL_WIDTH, the plot the rest - until the operator drags a divider, which
    then stays where it was put. (Qt's own sharing gave a panel of ~850 px a ~290 px plot.)"""

    def __init__(self, w, splitter):
        super().__init__(splitter)
        self.w, self.splitter, self.moved = w, splitter, False
        splitter.splitterMoved.connect(self._moved)
        splitter.installEventFilter(self)

    def _moved(self, *_):
        self.moved = True

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Resize and not self.moved:
            self.balance()
        return False

    def balance(self):
        total = sum(self.splitter.sizes())
        if total <= 0:
            return
        sides = []
        for panel in (self.w.control_panel_widget, self.w.analysis_panel_widget):
            want = int(min(max(PANEL_WIDTH, panel.minimumWidth()), max(panel.minimumWidth(), total * 0.24)))
            sides.append(want if panel.isVisibleTo(self.w) else 0)
        left, right = sides
        self.splitter.setSizes([left, max(0, total - left - right), right])


def build(w):
    w.main_layout = QHBoxLayout(w)
    w.main_layout.setContentsMargins(4, 4, 4, 4)
    splitter = QSplitter(Qt.Orientation.Horizontal)
    splitter.setChildrenCollapsible(False)
    splitter.addWidget(_settings_panel(w))
    splitter.addWidget(_plot_panel(w))
    splitter.addWidget(_analysis_panel(w))
    splitter.setStretchFactor(0, 0)
    splitter.setStretchFactor(1, 1)               # a wider panel widens the plot
    splitter.setStretchFactor(2, 0)
    splitter.setSizes([PANEL_WIDTH, 1100, PANEL_WIDTH])
    w.splitter_balance = _Balance(w, splitter)
    w.main_layout.addWidget(splitter)
    theme.style_plot(w.plot_widget, restyle=w._restyle_plot)


def restyle(w):
    """The plot's own colours in the current theme: positive current in the first series colour,
    negative in red; the previous pass (ghost) dashed and faint."""
    t = theme.tokens()
    pos, neg = theme.series_color(0), theme.series_color(7)
    ghost_pos, ghost_neg = theme.qcolor(t["series"][0]), theme.qcolor(t["series"][7])
    ghost_pos.setAlpha(110)
    ghost_neg.setAlpha(110)
    w.curve_ghost_pos.setPen(pg.mkPen(ghost_pos, width=1, style=Qt.PenStyle.DashLine))
    w.curve_ghost_pos.setSymbolBrush(ghost_pos)
    w.curve_ghost_pos.setSymbolPen(None)
    w.curve_ghost_neg.setPen(pg.mkPen(ghost_neg, width=1, style=Qt.PenStyle.DashLine))
    w.curve_ghost_neg.setSymbolBrush(ghost_neg)
    w.curve_ghost_neg.setSymbolPen(None)
    w.curve_live_pos.setPen(pg.mkPen(pos, width=2))
    w.curve_live_pos.setSymbolBrush(pos)
    w.curve_live_pos.setSymbolPen(None)
    w.curve_live_neg.setPen(pg.mkPen(neg, width=2))
    w.curve_live_neg.setSymbolBrush(neg)
    w.curve_live_neg.setSymbolPen(None)
    outline = pg.mkPen(theme.qcolor(t["plot_bg"]), width=1)
    w.peak_markers.setSymbolBrush(theme.series_color(3))
    w.peak_markers.setSymbolPen(outline)
    w.tail_markers.setSymbolPen(pg.mkPen(theme.qcolor(t["fault"]), width=1.5))
    w.calib_scatter.setSymbolBrush(theme.qcolor(t["ok"]))
    w.calib_scatter.setSymbolPen(outline)
    w.plot_widget.showGrid(x=True, y=True, alpha=0.25)


def _settings_panel(w):
    content = QWidget()
    panel = QVBoxLayout(content)
    panel.setContentsMargins(0, 0, 6, 0)

    w.sp_start = _spin(0.0, 150.0, 3, 0.0, " A")
    w.sp_stop = _spin(0.0, 150.0, 3, 40.0, " A")
    w.sp_step = _spin(0.001, 10.0, 3, 0.05, " A")
    panel.addWidget(_grid("Sweep", [("Start:", w.sp_start), ("Stop:", w.sp_stop), ("Step:", w.sp_step)]))

    w.sp_tol = _spin(0.001, 1.0, 3, 0.05, " A", tip="Target hardware tolerance measured in Magnet Amps.")
    w.sp_settle = _spin(0.0, 5.0, 2, 0.1, " s")
    w.sp_samples = QSpinBox()
    w.sp_samples.setRange(1, 1000)
    w.sp_samples.setValue(5)
    w.sp_interval = _spin(0.01, 1.0, 3, 0.02, " s")
    w.sp_nplc = _spin(NPLC_RANGE[0], NPLC_RANGE[1], 3, 10.0,
                      tip="SMU Integration Time (1 NPLC = 20ms). 10.0 = 200ms per sample for ultimate low noise.")
    panel.addWidget(_grid("Acquisition and settling", [("HW Tol:", w.sp_tol), ("Wait Time:", w.sp_settle),
                                                     ("Samples:", w.sp_samples), ("Interval:", w.sp_interval),
                                                     ("SMU NPLC:", w.sp_nplc)]))
    for box in (w.sp_start, w.sp_stop, w.sp_step, w.sp_settle, w.sp_samples, w.sp_interval, w.sp_nplc):
        box.valueChanged.connect(w._update_eta)

    group = QGroupBox("Run and output")
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
    set_role(w.lbl_eta, "readout-sm")
    w.lbl_rem_time = QLabel("Remaining: --:--")
    set_role(w.lbl_rem_time, "readout-sm")
    time_row = QVBoxLayout()                  # (one above the other: the panel stays narrow)
    time_row.setSpacing(2)
    time_row.addWidget(w.lbl_eta)
    time_row.addWidget(w.lbl_rem_time)
    w.btn_start = QPushButton("START PRECISION SWEEP")
    set_tone(w.btn_start, "primary")
    w.btn_start.clicked.connect(w._start_scan)
    w.btn_stop = QPushButton("ABORT")
    set_tone(w.btn_stop, "danger")
    w.btn_stop.setEnabled(False)
    w.btn_stop.clicked.connect(w._stop_scan)
    w.lbl_status = QLabel("Status: Idle")
    w.lbl_status.setWordWrap(True)
    set_role(w.lbl_status, "readout-sub")
    lbl_path = QLabel("Base export path")
    set_role(lbl_path, "caption")
    for item in (w.chk_loop, w.chk_export, lbl_path, export_row, time_row, w.btn_start,
                 w.btn_stop, w.lbl_status):
        (layout.addLayout if isinstance(item, (QHBoxLayout, QVBoxLayout)) else layout.addWidget)(item)
    group.setLayout(layout)
    panel.addWidget(group)
    panel.addStretch()
    w.control_panel_widget = _side(content)
    return w.control_panel_widget


def _plot_panel(w):
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    toolbar = QHBoxLayout()
    w.btn_toggle_ctrl = QPushButton("◀ Setup")
    w.btn_toggle_ctrl.clicked.connect(w._toggle_left_panel)
    w.chk_log_y = QCheckBox("Log Y")
    w.chk_log_y.setToolTip("Logarithmic current axis (shows negative currents too: symmetric log).")
    w.chk_log_y.stateChanged.connect(w._toggle_log_y)
    w.btn_clear = QPushButton("Clear")
    w.btn_clear.setToolTip("Clear the plot.")
    w.btn_clear.clicked.connect(w._clear_plot)
    w.btn_toggle_analysis = QPushButton("Analysis ▶")
    w.btn_toggle_analysis.clicked.connect(w._toggle_right_panel)
    # X axis: mass or magnet current (one pair of toggles), and "Both": the other unit on top
    w.btn_x_amu = QPushButton("AMU")
    w.btn_x_amu.setToolTip("Plot against the mass (AMU), worked out from the magnet current with the mass "
                           "calibration.")
    w.btn_x_amps = QPushButton("Amps")
    w.btn_x_amps.setToolTip("Plot against the measured magnet current (A): needs no calibration.")
    w.x_unit_group = QButtonGroup(w)
    w.x_unit_group.setExclusive(True)
    for button, unit in ((w.btn_x_amu, "amu"), (w.btn_x_amps, "amps")):
        button.setCheckable(True)
        set_role(button, "toggle")
        w.x_unit_group.addButton(button)
        button.clicked.connect(lambda _checked, u=unit: w.set_x_axis(unit=u))
    w.btn_x_amu.setChecked(True)
    w.chk_x_both = QCheckBox("Both axes")
    w.chk_x_both.setToolTip("Also label the top axis in the other unit (magnet current above a mass plot, "
                            "mass above a current plot), at the scan's mean beam energy.")
    w.chk_x_both.toggled.connect(lambda checked: w.set_x_axis(both=checked))
    # Where the pointer is, in both units: takes the toolbar's spare room, and is cut short rather
    # than widen the panel when there is none
    w.lbl_cursor = QLabel("")
    set_role(w.lbl_cursor, "readout-sm")
    w.lbl_cursor.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    w.lbl_cursor.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    w.lbl_cursor.setToolTip("The mass and magnet current under the pointer, at the scan's mean beam energy.")
    toolbar.addWidget(w.btn_toggle_ctrl)
    toolbar.addWidget(w.chk_log_y)
    toolbar.addSpacing(10)
    x_pair = QHBoxLayout()
    x_pair.setSpacing(0)                       # (the two toggles side by side, as one control)
    x_pair.addWidget(w.btn_x_amu)
    x_pair.addWidget(w.btn_x_amps)
    toolbar.addLayout(x_pair)
    toolbar.addWidget(w.chk_x_both)
    toolbar.addWidget(w.lbl_cursor, 1)
    toolbar.addWidget(w.btn_clear)
    toolbar.addWidget(w.btn_toggle_analysis)
    layout.addLayout(toolbar)

    w.current_axis = SmartCurrentAxis(orientation='left')
    w.top_axis = OtherUnitAxis(orientation='top')
    w.plot_widget = pg.PlotWidget(axisItems={'left': w.current_axis, 'top': w.top_axis})
    # Y follows the data in the visible mass range (also after zooming in x)
    w.plot_widget.getViewBox().setAutoVisible(y=True)
    w.plot_widget.setLabel('bottom', X_LABELS["amu"])
    w.plot_widget.setLabel('left', "Beam Current")
    w.plot_widget.getPlotItem().hideAxis('top')
    w.plot_widget.scene().sigMouseClicked.connect(w._on_mouse_clicked)
    w.plot_widget.scene().sigMouseMoved.connect(w._on_mouse_moved)

    # (colours: restyle(), now and after every theme switch)
    plot = w.plot_widget.plot
    w.curve_ghost_pos = plot(symbol='o', symbolSize=3, connect='finite')
    w.curve_ghost_neg = plot(symbol='o', symbolSize=3, connect='finite')
    w.curve_live_pos = plot(symbol='o', symbolSize=5, connect='finite')
    w.curve_live_neg = plot(symbol='o', symbolSize=5, connect='finite')
    w.peak_markers = plot(pen=None, symbol='t1', symbolSize=10)
    w.tail_markers = plot(pen=None, symbol='x', symbolSize=10)
    w.calib_scatter = plot(pen=None, symbol='star', symbolSize=15)
    layout.addWidget(w.plot_widget)
    return widget


def _analysis_panel(w):
    content = QWidget()
    layout = QVBoxLayout(content)
    layout.setContentsMargins(6, 0, 0, 0)

    source = QFrame()
    set_role(source, "card")
    source_layout = QVBoxLayout(source)
    source_layout.setContentsMargins(8, 6, 8, 6)
    w.lbl_data_source = QLabel("<b>Data Source:</b> None")
    w.lbl_data_source.setWordWrap(True)
    w.lbl_data_source.setMinimumHeight(40)          # room for a saved file's name on a second line
    source_layout.addWidget(w.lbl_data_source)
    layout.addWidget(source)
    files = QHBoxLayout()
    w.btn_load_csv = QPushButton("Load trace...")
    w.btn_load_csv.clicked.connect(w._load_csv)
    w.btn_save_csv = QPushButton("Save trace")
    w.btn_save_csv.clicked.connect(w._manual_save_csv)
    w.btn_save_csv.setEnabled(False)
    files.addWidget(w.btn_load_csv)
    files.addWidget(w.btn_save_csv)
    layout.addLayout(files)

    calib = QGroupBox("Mass calibration")
    grid = _form()
    w.lbl_energy_display = QLabel("--- keV")
    set_role(w.lbl_energy_display, "readout-sm")
    # SetpointBox: "entered by hand" only when someone types or steps one (any focus-out used to
    # relabel a fitted calibration as typed - and that label goes into "Calculate & set amps" markers)
    w.sp_k_factor = _spin(1.0, 10000.0, 2, 1391.9, step=1.0, box=SetpointBox(live=False))
    w.sp_i_offset = _spin(-10.0, 10.0, 3, 0.223, step=0.01, box=SetpointBox(live=False))
    w.sp_k_factor.valueChanged.connect(w._recalculate_mass_axis)
    w.sp_i_offset.valueChanged.connect(w._recalculate_mass_axis)
    w.sp_k_factor.committed.connect(w._calibration_typed)
    w.sp_i_offset.committed.connect(w._calibration_typed)
    model = QLabel("Model: AMU = k (I + I_off)² / E")
    set_role(model, "caption")
    model.setWordWrap(True)
    grid.addRow(model)
    for label, widget in (("Beam energy E:", w.lbl_energy_display),
                          ("Constant k:", w.sp_k_factor),
                          ("Offset I_off:", w.sp_i_offset)):
        grid.addRow(label, widget)
    w.btn_table = QPushButton("Expected elements...")
    w.btn_table.setToolTip("Choose the elements you expect (periodic table): peaks are matched against "
                           "their isotopes and compounds first.")
    w.btn_table.clicked.connect(w._open_periodic_table)
    grid.addRow(w.btn_table)
    calib.setLayout(grid)
    layout.addWidget(calib)

    fit = QGroupBox("Multi-point fit")
    fit_layout = QVBoxLayout()
    w.table_calib = QTableWidget(0, 2)
    w.table_calib.setHorizontalHeaderLabels(["Magnet (A)", "True AMU"])
    w.table_calib.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    w.table_calib.verticalHeader().setVisible(False)
    w.table_calib.setMaximumHeight(120)
    buttons = QHBoxLayout()
    w.btn_clear_calib = QPushButton("Clear")
    w.btn_clear_calib.clicked.connect(w._clear_calib_points)
    w.btn_fit_calib = QPushButton("Calculate fit")
    w.btn_fit_calib.clicked.connect(w._calculate_fit)
    buttons.addWidget(w.btn_clear_calib)
    buttons.addWidget(w.btn_fit_calib)
    w.lbl_fit_stats = QLabel("")
    set_role(w.lbl_fit_stats, "readout-sub")
    w.lbl_fit_stats.setWordWrap(True)
    w.lbl_fit_stats.setAlignment(Qt.AlignmentFlag.AlignCenter)
    hint = QLabel("Ctrl + left click a peak on the plot to give its true AMU.")
    hint.setWordWrap(True)
    set_role(hint, "hint")
    fit_layout.addWidget(hint)
    fit_layout.addWidget(w.table_calib)
    fit_layout.addLayout(buttons)
    fit_layout.addWidget(w.lbl_fit_stats)
    fit.setLayout(fit_layout)
    layout.addWidget(fit)

    peaks = QGroupBox("Peak detection")
    peaks_layout = QVBoxLayout()
    w.sp_max_peaks = QSpinBox()
    w.sp_max_peaks.setRange(1, 100)
    w.sp_max_peaks.setValue(15)
    w.sp_max_peaks.valueChanged.connect(w._conditional_peak_update)
    w.sp_sensitivity = _spin(0.0001, 100.0, 3, 0.05, " %")
    w.sp_sensitivity.valueChanged.connect(w._conditional_peak_update)
    params = _form()
    params.addRow("Max peaks:", w.sp_max_peaks)
    params.addRow("Sensitivity:", w.sp_sensitivity)
    peaks_layout.addLayout(params)
    w.btn_detect = QPushButton("Auto-detect peaks")
    w.btn_detect.setCheckable(True)
    set_role(w.btn_detect, "toggle")
    w.btn_detect.toggled.connect(w._on_detect_toggled)
    w.peak_table = QTableWidget(0, 3)
    w.peak_table.setHorizontalHeaderLabels(["Mass (AMU)", "Match", "Tail ±0.5"])
    w.peak_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    w.peak_table.verticalHeader().setVisible(False)
    w.peak_table.setColumnWidth(1, 120)
    w.lbl_res = QLabel("<b>Avg Mass Resolution:</b> ---")
    w.lbl_res.setWordWrap(True)
    peaks_layout.addWidget(w.btn_detect)
    peaks_layout.addWidget(w.peak_table)
    peaks_layout.addWidget(w.lbl_res)
    peaks.setLayout(peaks_layout)
    layout.addWidget(peaks)
    layout.addStretch()
    w.analysis_panel_widget = _side(content)
    return w.analysis_panel_widget
