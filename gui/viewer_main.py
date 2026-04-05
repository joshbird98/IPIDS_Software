import sys
import os
import numpy as np
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QVBoxLayout, QHBoxLayout,
    QWidget, QPushButton, QSplitter, QLabel, QDateTimeEdit
)
from PyQt6.QtCore import Qt, QDateTime, QTimer
import pyqtgraph as pg

# Import the new utilities
from utils.config_manager_2 import load_config, save_config
from utils.channel_selector_2 import ChannelSelectorDialog
from utils.data_engine import TimeSeriesEngine

from datetime import datetime

class LogAxisItem(pg.AxisItem):
    def tickStrings(self, values, scale, spacing):
        """Converts the raw log10 values back to scientific notation for the UI."""
        strings = []
        for v in values:
            try:
                # E.g., if the numpy value is -8.0, display '1.0e-8'
                strings.append(f"1.0e{int(v)}")
            except:
                strings.append("")
        return strings


class TimeAxisItem(pg.AxisItem):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.offset = 0.0

    def set_offset(self, offset):
        self.offset = offset

    def tickStrings(self, values, scale, spacing):
        """Dynamically adjusts the date/time format based on the zoom level."""
        strings = []
        for v in values:
            try:
                dt = datetime.fromtimestamp(v + self.offset)

                # Determine format based on tick spacing (in seconds)
                if spacing >= 86400:  # >= 1 Day
                    fmt = '%Y-%m-%d'
                elif spacing >= 3600:  # >= 1 Hour
                    fmt = '%b %d\n%H:%M'  # e.g., Apr 05 \n 14:00 (Multiline)
                elif spacing >= 60:  # >= 1 Minute
                    fmt = '%H:%M'
                elif spacing >= 1:  # >= 1 Second
                    fmt = '%H:%M:%S'
                else:  # Sub-second (Milliseconds)
                    fmt = '%H:%M:%S.%f'

                val_str = dt.strftime(fmt)
                if spacing < 1:
                    val_str = val_str[:-3]  # Trim microseconds down to milliseconds

                strings.append(val_str)
            except Exception:
                strings.append("")
        return strings

class DataViewerApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("IPIDS Data Historian - Phase 2")
        # Set a sensible fallback size that fits all laptop screens (e.g., 720p minimum)
        self.resize(1100, 650)

        # Force the application to open fully maximized
        self.showMaximized()

        # Initialize Engine (Point this to your LogData folder)
        current_dir = os.path.dirname(os.path.abspath(__file__))
        log_dir = os.path.join(os.path.dirname(current_dir), "LogData")
        self.engine = TimeSeriesEngine(log_dir)

        self.plot_config = load_config()
        self.curves = {}

        # Buffer for current query results
        self.raw_ts = None
        self.raw_vals = None
        self.t0 = 0.0

        # Throttling timer for mouse-move updates
        self.update_timer = QTimer()
        self.update_timer.setSingleShot(True)
        self.update_timer.timeout.connect(self._perform_query)

        self._init_ui()

    def _init_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QHBoxLayout(main_widget)

        # --- Sidebar ---
        sidebar = QWidget()
        sidebar_layout = QVBoxLayout(sidebar)

        # 1. Quick Presets
        sidebar_layout.addWidget(QLabel("<b>Quick Ranges:</b>"))
        presets = [("1 Hour", 1), ("24 Hours", 24), ("7 Days", 168)]
        for label, hours in presets:
            btn = QPushButton(label)
            btn.clicked.connect(lambda ch, h=hours: self._set_preset_range(h))
            sidebar_layout.addWidget(btn)

        sidebar_layout.addSpacing(20)

        # 2. Absolute Controls
        sidebar_layout.addWidget(QLabel("<b>Custom Range:</b>"))
        self.dt_start = QDateTimeEdit()
        self.dt_end = QDateTimeEdit()

        # Determine global bounds from the engine
        global_start, global_end = self.engine.get_global_bounds()

        if global_start and global_end:
            # Set the hard limits on the UI widgets
            min_dt = QDateTime.fromSecsSinceEpoch(int(global_start))
            max_dt = QDateTime.fromSecsSinceEpoch(int(global_end))

            for widget in [self.dt_start, self.dt_end]:
                widget.setMinimumDateTime(min_dt)
                widget.setMaximumDateTime(max_dt)

            # Default view: Last 24 hours of available data
            self.dt_start.setDateTime(
                max_dt.addSecs(-86400) if max_dt.toSecsSinceEpoch() - min_dt.toSecsSinceEpoch() > 86400 else min_dt)
            self.dt_end.setDateTime(max_dt)
        else:
            # Fallback if no logs exist yet
            self.dt_start.setDateTime(QDateTime.currentDateTime().addDays(-1))
            self.dt_end.setDateTime(QDateTime.currentDateTime())

        for widget in [self.dt_start, self.dt_end]:
            widget.setCalendarPopup(True)
            widget.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
            sidebar_layout.addWidget(widget)

        # Wire up the validation signals
        self.dt_start.dateTimeChanged.connect(self._validate_dates)
        self.dt_end.dateTimeChanged.connect(self._validate_dates)

        self.btn_fetch = QPushButton("🔍 Fetch Data")
        self.btn_fetch.setStyleSheet("background-color: #2196F3; color: white; font-weight: bold;")
        self.btn_fetch.clicked.connect(self._perform_query)
        sidebar_layout.addWidget(self.btn_fetch)

        sidebar_layout.addSpacing(20)

        self.btn_config = QPushButton("⚙️ Configure Channels")
        self.btn_config.clicked.connect(self.open_channel_config)
        sidebar_layout.addWidget(self.btn_config)

        sidebar_layout.addStretch()

        # --- Plot Area ---
        pg.setConfigOptions(antialias=True)
        self.time_axis = TimeAxisItem(orientation='bottom')
        self.log_axis = LogAxisItem(orientation='right')

        # Inject BOTH custom axes during instantiation
        self.graph = pg.PlotWidget(axisItems={'bottom': self.time_axis, 'right': self.log_axis})
        self.graph.setBackground('k')
        self.graph.showGrid(x=True, y=True, alpha=0.3)
        self.graph.getAxis('left').setLabel('Linear Data')
        self.graph.getAxis('right').setLabel('Vacuum (mB)')

        # Create the independent overlay ViewBox for Log data
        self.right_viewbox = pg.ViewBox()
        self.graph.scene().addItem(self.right_viewbox)
        self.graph.getAxis('right').linkToView(self.right_viewbox)
        self.right_viewbox.setXLink(self.graph)

        # Connect the resize signal so the overlay stays perfectly aligned
        self.graph.getPlotItem().vb.sigResized.connect(self._update_views)
        self.graph.sigRangeChanged.connect(self._on_view_changed)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(sidebar)
        splitter.addWidget(self.graph)

        # 1. Set default pixel widths (280px for sidebar, the rest for the graph)
        splitter.setSizes([280, 1000])

        # 2. Assign Stretch Factors
        # Index 0 (Sidebar) gets a stretch of 0 (stays compact)
        # Index 1 (Graph) gets a stretch of 1 (absorbs all extra window space)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        layout.addWidget(splitter)

    def _set_preset_range(self, hours):
        end = QDateTime.currentDateTime()
        start = end.addSecs(-hours * 3600)
        self.dt_start.setDateTime(start)
        self.dt_end.setDateTime(end)
        self._perform_query()

    def _on_view_changed(self):
        """Triggered by mouse pan/zoom. Throttled to avoid disk thrashing."""
        self.update_timer.start(200) # Wait 200ms of quiet before re-querying

    def _perform_query(self):
        """Fetches data from the engine and updates the plot."""
        # Save the requested bounds as instance variables
        self.req_start_ts = self.dt_start.dateTime().toSecsSinceEpoch()
        self.req_end_ts = self.dt_end.dateTime().toSecsSinceEpoch()

        # Query Engine
        self.raw_ts, self.raw_vals = self.engine.query(self.req_start_ts, self.req_end_ts)

        # Graceful empty handling
        if len(self.raw_ts) == 0:
            print("No data found for this exact range. Showing empty timeline.")
            # Anchor to the start of the requested window instead of data
            self.t0 = self.req_start_ts
        else:
            self.t0 = self.raw_ts[0]

        self.time_axis.set_offset(self.t0)
        self._render_plot()

    def _render_plot(self):
        """Decimates and routes curves to their respective Linear or Log ViewBoxes."""
        self.graph.clear()
        self.right_viewbox.clear()  # Remember to clear the overlay too!

        view_start = self.req_start_ts - self.t0
        view_end = self.req_end_ts - self.t0
        self.graph.setXRange(view_start, view_end, padding=0)

        if len(self.raw_ts) == 0:
            self.graph.hideAxis('right')
            self.graph.hideAxis('left')
            return

        self.graph.addLegend(offset=(10, 10))
        norm_ts = self.raw_ts - self.t0
        target_pts = self.graph.width() * 2

        has_linear = False
        has_log = False

        for idx, tag in enumerate(self.engine.channel_keys):
            cfg = self.plot_config.get(tag, {})
            if not cfg.get('selected'): continue

            y_raw = self.raw_vals[:, idx] * cfg.get('multiplier', 1.0)
            pen = pg.mkPen(color=cfg.get('color', '#ffffff'), width=1.5)
            label = cfg.get('label', tag)

            if cfg.get('scale') == 'log':
                has_log = True
                # Log Math: Filter out zero/negative noise to prevent math errors
                valid_mask = y_raw > 1.0e-12
                if np.any(valid_mask):
                    y_clean = y_raw.copy()
                    y_clean[~valid_mask] = np.nan

                    # Decimate first (faster), then apply log10 math
                    d_ts, d_y = TimeSeriesEngine.downsample_minmax(norm_ts, y_clean, target_pts)
                    with np.errstate(invalid='ignore'):
                        d_y_log = np.log10(d_y)

                    # Plot to Right ViewBox
                    curve = pg.PlotDataItem(x=d_ts, y=d_y_log, pen=pen, name=label)
                    self.right_viewbox.addItem(curve)
                    self.graph.getPlotItem().legend.addItem(curve, name=label)
            else:
                has_linear = True
                # Standard Linear Plot
                d_ts, d_y = TimeSeriesEngine.downsample_minmax(norm_ts, y_raw, target_pts)
                curve = self.graph.plot(x=d_ts, y=d_y, pen=pen, name=label)

        # Clean UI: Hide axes if no channels are currently using them
        self.graph.showAxis('left') if has_linear else self.graph.hideAxis('left')
        self.graph.showAxis('right') if has_log else self.graph.hideAxis('right')

    def open_channel_config(self):
        available_tags = {key: None for key in self.engine.channel_keys}
        dlg = ChannelSelectorDialog(available_tags, self.plot_config, self)
        if dlg.exec():
            self.plot_config = dlg.get_selection()
            save_config(self.plot_config)
            self._render_plot()

    def _validate_dates(self):
        """Prevents the End Date from being earlier than the Start Date."""
        # Block signals temporarily to prevent an infinite loop of them updating each other
        self.dt_start.blockSignals(True)
        self.dt_end.blockSignals(True)

        start = self.dt_start.dateTime()
        end = self.dt_end.dateTime()

        if start >= end:
            if self.sender() == self.dt_start:
                # If user pushed start past end, force end to be 1 second later
                self.dt_end.setDateTime(start.addSecs(1))
            else:
                # If user pushed end behind start, force start to be 1 second earlier
                self.dt_start.setDateTime(end.addSecs(-1))

        self.dt_start.blockSignals(False)
        self.dt_end.blockSignals(False)

    def _update_views(self):
        """Forces the right ViewBox to perfectly overlay the primary ViewBox on window resize."""
        self.right_viewbox.setGeometry(self.graph.getPlotItem().vb.sceneBoundingRect())
        self.right_viewbox.linkedViewChanged(self.graph.getPlotItem().vb, self.right_viewbox.XAxis)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = DataViewerApp()
    window.show()
    sys.exit(app.exec())