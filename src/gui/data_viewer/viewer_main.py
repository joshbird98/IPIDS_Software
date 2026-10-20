import sys
import os

if __name__ == "__main__":
    if __package__ in (None, ""):
        # Started as a script: make 'src' importable from any working directory
        sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
    if "--sim" in sys.argv[1:]:
        # Simulation mode: read data_sim/ (must be set before src.core.paths is imported)
        os.environ["SINDIE_SIMULATE"] = "1"

"""SINDIE Data Viewer: live and historical telemetry in plot lanes, event markers, exports.

The window (DataViewerApp) is put together from mixins, one per concern:
    viewer_lanes.py      lanes, axes and curves          viewer_markers.py   event markers
    viewer_inspector.py  values under the cursor, pins   viewer_export.py    data + figure export
    viewer_profiles.py   saved channel/lane layouts      viewer_widgets.py   marker dialog, axes
This file: the window, its sidebar, live / historical refresh and the launcher.
"""
import copy
import logging
import threading
import time
from datetime import datetime, timedelta

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QDateTime, Qt, QTimer
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDateTimeEdit, QFormLayout, QFrame, QGroupBox,
                             QHBoxLayout, QLineEdit, QMainWindow, QPushButton, QScrollArea, QSplitter,
                             QVBoxLayout, QWidget)

from src.core import paths
from src.core.data_cache import DualPipelineCache
from src.core.event_helper import EventHelper
from src.core.os_helper import harden_windows_process
from src.core.perf_utils import PerfTracker
from src.gui.data_viewer.channel_selector import ChannelSelectorDialog
from src.gui.data_viewer.config_manager import load_registry
from src.gui.data_viewer.event_overlay import EventOverlay
from src.gui.data_viewer.viewer_export import EXPORT_TEXT, ExportMixin
from src.gui.data_viewer.viewer_inspector import InspectorMixin
from src.gui.data_viewer.viewer_lanes import LanesMixin
from src.gui.data_viewer.viewer_markers import MarkersMixin
from src.gui.data_viewer.viewer_profiles import PROFILES_FILE, ProfilesMixin
from src.gui.data_viewer.viewer_widgets import LogAxisItem, MarkerDialog, TimeAxisItem  # noqa: F401 (were defined here)

__all__ = ["DataViewerApp", "LogAxisItem", "MarkerDialog", "TimeAxisItem"]
from src.gui import theme
from src.gui.settings import gui_settings
from src.gui.style import set_role, set_tone

SIDEBAR_WIDTH = 320             # the sidebar's starting width (drag the divider to change it)
FOLLOW_TEXT = ("Following live data", "Browsing - click to follow live")
Y_TEXT = ("Y axis: automatic", "Y axis: held - click for automatic")

log = logging.getLogger("viewer")


class DataViewerApp(LanesMixin, InspectorMixin, MarkersMixin, ExportMixin, ProfilesMixin, QMainWindow):
    HIST_MARGIN = 0.5        # extra view-widths fetched either side of the view
    HIST_REFINE_ZOOM = 1.5   # re-query when zoomed in this much past the fetch
    GRID_TICK_LEVEL = 0      # grid/tick levels drawn: 0 = major only (fastest), 1 = +minor, 2 = pyqtgraph default
    LIVE_FPS = 5             # live plot refresh rate (was 10; ~halves live-mode CPU)

    def __init__(self, shared_cache, window_title="SINDIE Data Viewer"):
        super().__init__()
        self.setWindowTitle(window_title)
        self.resize(1200, 800)
        self.showMaximized()

        self.client_id = id(self)  # Unique memory address ID for this window
        self.pending_hist_id = -1

        # 1. Data Layer
        self.cache = shared_cache
        self.system_registry = load_registry()
        self.events = EventHelper("viewer_main")
        # Launch background mart sync
        self._setup_midnight_refresh()
        # The Active Chunk Pre-loader
        self.chunk_sync_timer = QTimer()
        self.chunk_sync_timer.timeout.connect(self._trigger_chunk_sync)
        self.chunk_sync_timer.start(60000)  # Run every 60 seconds

        # Trigger it immediately on startup
        self._trigger_chunk_sync()

        # The Debouncer: Prevents the UI from freezing while dragging the graph
        self.history_debounce_timer = QTimer()
        self.history_debounce_timer.setSingleShot(True)
        self.history_debounce_timer.timeout.connect(self._execute_history_fetch)

        # 2. Profiles & Config
        self.profiles_file = PROFILES_FILE                # config/workspace_profiles.json
        self.profiles = self._load_all_profiles()
        self.plot_config = copy.deepcopy(self.profiles.get("Default", {}))

        # 3. State
        self.curves = {}  # For Live Data
        self.hist_curves = {}  # For Stateless Historical Data

        self.render_timer = QTimer()
        self.render_timer.timeout.connect(self._refresh_plot_live)
        self.render_timer.start(int(1000 / self.LIVE_FPS))

        self.cache.historical_updated.connect(self._refresh_plot_historical)

        self.historical_x = np.array([])
        self.historical_y = {}
        self.is_historical_mode = False

        self.auto_scroll = True
        self._auto_panning = False
        self.t0 = time.time()
        self.live_span = 300

        self._init_ui()

        # Use the new register method so we don't wipe out other windows
        self.cache.register_client_tags(self.client_id, list(self.plot_config.keys()))

        self.pin_idx = None
        self.pin_x = None

        self.log_axis_widgets = {}
        self.y_auto_scale = True

        self.last_ui_update = 0.0
        self.ui_lockout = 1.0 / 5.0  # Cap at 5 FPS

        # F12: capture the GUI itself as a vector figure (src/gui/figure_capture.py)
        from src.gui.figure_capture import install_figure_capture
        install_figure_capture(self)
        self.btn_export.setToolTip("Export data + publication figure for a time range.\n"
                                   "To capture the GUI itself as a vector figure, press F12 "
                                   "with the mouse over the part you want.")

        # Historical coverage check (was run every 100 ms render frame)
        self.history_check_timer = QTimer()
        self.history_check_timer.timeout.connect(self._check_and_fetch_history)
        self.history_check_timer.start(500)

        # 5. Background Sync for multi-window marker updates
        self.marker_sync_timer = QTimer()
        self.marker_sync_timer.timeout.connect(self._refresh_event_markers)
        self.marker_sync_timer.start(1000)  # Check SQLite every 1 second

    def _init_ui(self):
        # View menu: the theme (shared with the HMI: switching it in either changes both), Ctrl+Shift+T
        view_menu = self.menuBar().addMenu("View")
        self.sidebar_action = view_menu.addAction("Sidebar")
        self.sidebar_action.setCheckable(True)
        self.sidebar_action.setChecked(True)
        self.sidebar_action.toggled.connect(lambda on: self.sidebar_scroll.setVisible(on))
        view_menu.addSeparator()
        theme.add_theme_menu(view_menu, self)

        # Sidebar | plots, with a divider to drag (the sidebar used to be a fixed 350 px)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.setCentralWidget(self.splitter)
        main_widget = self.splitter

        # --- Sidebar (scrolls when the window is short) ---
        self.sidebar_scroll = QScrollArea()
        self.sidebar_scroll.setMinimumWidth(250)
        self.sidebar_scroll.setWidgetResizable(True)
        self.sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sidebar_scroll.setFrameShape(QFrame.Shape.NoFrame)

        sidebar = QWidget()
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(8, 8, 8, 8)
        sidebar_layout.setSpacing(8)
        self.sidebar_scroll.setWidget(sidebar)

        # 1. Time: follow live / browse, span, go to a date, the Y axis
        view_box = QGroupBox("Time")
        view_layout = QVBoxLayout(view_box)
        self.btn_scroll_lock = QPushButton(FOLLOW_TEXT[0])
        self.btn_scroll_lock.setCheckable(True)
        self.btn_scroll_lock.setChecked(True)
        set_role(self.btn_scroll_lock, "toggle")
        self.btn_scroll_lock.setToolTip("On: the plots scroll with the newest data. Dragging or zooming the "
                                        "time axis stops that; click to follow live again.")
        self.btn_scroll_lock.clicked.connect(lambda: self._toggle_scroll_lock())
        view_layout.addWidget(self.btn_scroll_lock)

        span_layout = QHBoxLayout()
        span_layout.setSpacing(4)
        for label, minutes in [("1m", 1), ("15m", 15), ("1h", 60), ("12h", 720)]:
            btn = QPushButton(label)
            btn.setToolTip(f"Show the last {label}")
            btn.clicked.connect(lambda ch, m=minutes: self._set_timespan(m))
            span_layout.addWidget(btn)
        view_layout.addLayout(span_layout)

        jump_row = QHBoxLayout()
        self.jump_dt = QDateTimeEdit(QDateTime.currentDateTime())
        self.jump_dt.setCalendarPopup(True)
        jump_row.addWidget(self.jump_dt, stretch=1)
        btn_jump = QPushButton("Go")
        btn_jump.setToolTip("Show the plots from this date and time")
        btn_jump.clicked.connect(self._jump_to_date)
        jump_row.addWidget(btn_jump)
        view_layout.addLayout(jump_row)

        self.btn_y_lock = QPushButton(Y_TEXT[0])
        self.btn_y_lock.setCheckable(True)
        set_role(self.btn_y_lock, "toggle")
        self.btn_y_lock.setToolTip("Automatic: each lane's Y axis follows its data. Held: it stays put.")
        self.btn_y_lock.clicked.connect(self._toggle_y_lock)
        view_layout.addWidget(self.btn_y_lock)
        sidebar_layout.addWidget(view_box)

        # 2. Channels and saved profiles (which channels, in which lanes)
        chan_box = QGroupBox("Channels")
        chan_layout = QVBoxLayout(chan_box)
        self.btn_config = QPushButton("Configure channels...")
        set_tone(self.btn_config, "primary")
        self.btn_config.clicked.connect(self.open_channel_config)
        chan_layout.addWidget(self.btn_config)

        # Create a mini horizontal layout for the dropdown + delete button
        profile_row = QHBoxLayout()

        self.combo_profiles = QComboBox()
        self.combo_profiles.addItems(self.profiles.keys())
        self.combo_profiles.setToolTip("Saved profiles: a set of channels and their lanes")

        self.btn_delete_profile = QPushButton("Delete")
        self.btn_delete_profile.setToolTip("Delete this profile")
        # Connect to a wrapper function that passes the current text
        self.btn_delete_profile.clicked.connect(lambda: self._delete_profile(self.combo_profiles.currentText()))

        profile_row.addWidget(self.combo_profiles, stretch=1)
        profile_row.addWidget(self.btn_delete_profile)
        chan_layout.addLayout(profile_row)

        load_row = QHBoxLayout()
        btn_load = QPushButton("Load profile")
        btn_load.clicked.connect(self._apply_profile)
        btn_save_profile = QPushButton("Save as...")
        btn_save_profile.setToolTip("Save the channels shown now as a profile")
        btn_save_profile.clicked.connect(self._save_current_profile)
        load_row.addWidget(btn_load)
        load_row.addWidget(btn_save_profile)
        chan_layout.addLayout(load_row)
        sidebar_layout.addWidget(chan_box)

        # 3. Values: live, or under the cursor with the inspector on
        values_box = QGroupBox("Values")
        values_layout = QVBoxLayout(values_box)
        self.btn_inspector = QPushButton("Inspector")
        self.btn_inspector.setCheckable(True)
        set_role(self.btn_inspector, "toggle")
        self.btn_inspector.setToolTip("Show the values under the cursor. Ctrl+click pins a time (differences "
                                      "from it are shown); right-click clears the pin.")
        self.btn_inspector.toggled.connect(self._on_inspector_toggled)
        values_layout.addWidget(self.btn_inspector)
        self.value_display_layout = QFormLayout()
        self.value_display_layout.setHorizontalSpacing(8)
        self.value_display_layout.setVerticalSpacing(4)
        self.sidebar_value_labels = {}
        values_layout.addLayout(self.value_display_layout)
        sidebar_layout.addWidget(values_box)

        # 4. Export
        export_box = QGroupBox("Export")
        export_layout = QVBoxLayout(export_box)

        # Path selection layout
        path_layout = QHBoxLayout()
        self.edit_export_path = QLineEdit()
        # Default to root/exports
        default_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../data/exports'))

        if not os.path.exists(default_path): os.makedirs(default_path)

        # The export dialog starts from this box and writes its choice back here; remember it
        st = gui_settings("SINDIE", "DataViewer")
        self.edit_export_path.setText(os.path.join(st.value("export/dest", default_path) or default_path,
                                                   st.value("export/name", "SINDIE_Snapshot") or "SINDIE_Snapshot"))
        path_layout.addWidget(self.edit_export_path)

        btn_browse = QPushButton("...")
        btn_browse.setFixedWidth(34)
        btn_browse.clicked.connect(self._browse_export_path)
        path_layout.addWidget(btn_browse)
        export_layout.addLayout(path_layout)

        self.btn_export = QPushButton(EXPORT_TEXT)
        # lambda: QPushButton.clicked passes checked=False, which PyQt would feed
        # into _prepare_export's `options` parameter (export then did nothing)
        self.btn_export.clicked.connect(lambda: self._prepare_export())
        export_layout.addWidget(self.btn_export)
        sidebar_layout.addWidget(export_box)

        # 5. Event markers on the plots: which kinds
        events_box = QGroupBox("Event markers")
        events_layout = QVBoxLayout(events_box)
        self.overlay_frame = QWidget()
        overlay_layout = QVBoxLayout(self.overlay_frame)
        overlay_layout.setContentsMargins(0, 0, 0, 0)

        # Map UI labels to exact database event types (using pseudo-types for faults)
        self.event_filters = {
            "User Markers": ["USER_MARKER"],
            "Faults: Activated": ["FAULT_ACTIVE"],
            "Faults: Cleared": ["FAULT_CLEARED"],
            "Watchdog Drops": ["WATCHDOG_TRIP", "WATCHDOG_CLEAR"],
            "Phase Changes": ["PHASE_CHANGE"],
            "Readback Tolerance": ["TOLERANCE"],
            "Operator Commands (audit)": ["COMMAND"],
        }
        self.filter_checkboxes = {}

        for label_text, types in self.event_filters.items():
            cb = QCheckBox(label_text)
            cb.setChecked(True) if label_text == "User Markers" else cb.setChecked(False)
            cb.stateChanged.connect(self._refresh_event_markers)
            self.filter_checkboxes[label_text] = cb
            overlay_layout.addWidget(cb)

        events_layout.addWidget(self.overlay_frame)
        sidebar_layout.addWidget(events_box)

        sidebar_layout.addStretch()
        main_widget.addWidget(self.sidebar_scroll)

        pg.setConfigOptions(antialias=True)

        # Core container replaces the single PlotWidget
        self.layout_widget = pg.GraphicsLayoutWidget()
        main_widget.addWidget(self.layout_widget)
        main_widget.setStretchFactor(0, 0)
        main_widget.setStretchFactor(1, 1)
        main_widget.setSizes([SIDEBAR_WIDTH, 1400])
        # readable names in the F12 figure-capture target list
        self.layout_widget.setObjectName("Plot area")
        self.sidebar_scroll.setObjectName("Sidebar")
        main_widget.setObjectName("Window contents (no title bar)")

        # State dictionaries for the multi-lane system
        self.lanes = {}  # holds pg.PlotItem
        self.lane_axes = {}  # holds Right/Log ViewBoxes per lane
        self.lane_legends = {}  # holds pg.LegendItem per lane

        # Lists for multi-lane inspector elements
        self.v_lines = []  # Vertical crosshairs
        self.pin_lines = []  # Ctrl+Click pins
        self.delta_labels = []  # Time delta text

        self.event_overlay = EventOverlay(on_delete=self._delete_marker)
        self._pending_markers = {}   # markers sent to the events service, not yet seen in events.db
        self.event_data = {}   # loaded per view window by _refresh_event_markers

        # Connect global mouse system_logs to the entire layout scene
        self.layout_widget.scene().sigMouseMoved.connect(self._on_mouse_moved)
        self.layout_widget.scene().sigMouseClicked.connect(self._on_graph_clicked)
        self.layout_widget.scene().sigMouseClicked.connect(self._handle_click_events)

        # Background and axes in the theme now and after every switch; curves, legends, the
        # crosshair and the markers by _restyle_plots (viewer_lanes.py)
        theme.style_plot(self.layout_widget, restyle=self._restyle_plots)

        # Build the initial grid
        QTimer.singleShot(200, self.request_ui_refresh)

    def _trigger_chunk_sync(self):
        """Silently updates the RAM buffer with any newly generated 15-minute logs."""
        threading.Thread(target=self.cache.engine.sync_active_chunks, daemon=True).start()

    def _setup_midnight_refresh(self):
        """Schedules the Data Mart to reload into RAM at exactly 02:30 AM daily."""

        # 1. Trigger the immediate startup load
        threading.Thread(target=self.cache.engine.populate_macro_marts, daemon=True).start()

        # 2. Calculate milliseconds until the next 02:30 AM
        now = datetime.now()
        target = now.replace(hour=2, minute=30, second=0, microsecond=0)

        if now >= target:
            # If it is already past 2:30 AM today, schedule for tomorrow
            target += timedelta(days=1)

        ms_until_target = int((target - now).total_seconds() * 1000)

        # 3. Set a single-shot timer to bridge the gap to 02:30 AM
        self.initial_sync_timer = QTimer()
        self.initial_sync_timer.setSingleShot(True)
        self.initial_sync_timer.timeout.connect(self._trigger_daily_sync)
        self.initial_sync_timer.start(ms_until_target)

    def _trigger_daily_sync(self):
        """Fires at 02:30 AM, triggers the load, and establishes the 24h rolling timer."""
        log.info("02:30 data mart refresh")
        threading.Thread(target=self.cache.engine.populate_macro_marts, daemon=True).start()

        # 4. Now that we are aligned to 2:30 AM, start a standard 24-hour repeating loop
        self.daily_sync_timer = QTimer()
        self.daily_sync_timer.timeout.connect(
            lambda: threading.Thread(target=self.cache.engine.populate_macro_marts, daemon=True).start()
        )
        self.daily_sync_timer.start(86400000)  # 24 hours in milliseconds

    def _toggle_y_lock(self):
        self.y_auto_scale = not self.y_auto_scale

        self.btn_y_lock.setChecked(not self.y_auto_scale)
        self.btn_y_lock.setText(Y_TEXT[0] if self.y_auto_scale else Y_TEXT[1])

        # Apply the state to all lanes
        for lane_name in self.lanes:
            p = self.lanes[lane_name]
            p.vb.enableAutoRange(axis=pg.ViewBox.YAxis, enable=self.y_auto_scale)
            self.lane_axes[lane_name].enableAutoRange(axis=pg.ViewBox.YAxis, enable=self.y_auto_scale)

    def _on_cache_updated(self):
        """Signals received from cache. Redraw handled by render_timer."""
        pass

    def _refresh_plot_live(self):
        if not self.cache.tags or not getattr(self, 'lanes', None): return
        # NOTE: _check_and_fetch_history() used to run here, i.e. every frame.
        # It now runs from history_check_timer (500 ms) and the debounced view-change
        # handler, which is all it needs.
        for ta in getattr(self, 'time_axes', []): ta.set_offset(self.t0)

        live = self.cache.live
        latest = live.latest_time()
        if latest is None:
            return
        t_slice = time.perf_counter()

        base_plot = list(self.lanes.values())[0]
        view_start, view_end = base_plot.viewRange()[0]
        width = view_end - view_start

        # Auto-scroll: move the view FIRST, so this frame's data window matches what
        # is shown (previously the slice was taken for the old view, then scrolled).
        if self.auto_scroll:
            self.live_span = width
            view_end = latest - self.t0
            view_start = view_end - width
            self._auto_panning = True
            base_plot.setXRange(view_start, view_end, padding=0)
            self._auto_panning = False

        pad = width * 0.25
        selected = [t for t in self.cache.tags if self.plot_config.get(t, {}).get('selected')]
        # {tag: (x_abs, y)} - each channel at its own source's timestamps
        self.current_live = live.window(self.t0 + view_start - pad, self.t0 + view_end + pad, selected)

        PerfTracker.slice_size = sum(len(xy[0]) for xy in self.current_live.values())
        PerfTracker.log("slice", t_slice)

        t_render = time.perf_counter()
        self._render_live_curves(self.current_live)
        PerfTracker.log("render", t_render)

        # Historical curves are NOT re-set here: their data only changes when a
        # query returns (_refresh_plot_historical).
        self._settle_viewboxes()

        # --- UPDATE LIVE SIDEBAR READOUT ---
        if not self.btn_inspector.isChecked():
            for tag, val_lbl in getattr(self, 'sidebar_value_labels', {}).items():
                val = live.latest_value(tag)
                if val is None:
                    continue
                cfg = self.plot_config.get(tag, {})
                mult = cfg.get('multiplier', 1.0)
                base_unit = getattr(self, 'system_registry', {}).get(tag, {}).get("unit", "")
                val_lbl.setText(self._format_with_si_prefix(val * mult if mult != 1.0 else val, base_unit))

    def _settle_viewboxes(self):
        """Resolve pyqtgraph's deferred range/transform updates now, before Qt paints.

        pyqtgraph applies a new X range (and the Y auto-range that depends on it)
        lazily inside the paint (ViewBox.prepareForPaint). Applying the transform
        mid-paint re-queues the auto-range on every ViewBox, which schedules a
        SECOND full repaint ~15 ms later - so every live frame was painted twice.
        Running the same preparation here (twice, so the auto-range that the
        first transform update queues is also resolved) leaves nothing to change
        during the paint, giving one repaint per frame.
        """
        if getattr(self, '_settling', False):
            return  # prepareForPaint can emit sigRangeChanged -> _handle_view_change
        self._settling = True
        try:
            for lane, p in self.lanes.items():
                boxes = [p.vb]
                if lane in self.lane_axes:
                    boxes.append(self.lane_axes[lane])
                for _ in range(2):
                    for vb in boxes:
                        vb.prepareForPaint()
        finally:
            self._settling = False

    def _refresh_plot_historical(self, ts_array, vals_dict, req_id=None):
        # Show any result newer than what is on screen. Previously only the exact
        # latest request id was accepted, so while zooming/panning every completed
        # query was discarded and the plot stayed coarse/stale until you stopped.
        # Only results for THIS window's requests: the cache (and its request ids)
        # is shared between viewer windows.
        if req_id is not None:
            mine = getattr(self, '_my_hist_ids', set())
            if req_id not in mine or req_id < getattr(self, '_hist_shown_id', -1):
                return
            self._hist_shown_id = req_id
            self._my_hist_ids = {i for i in mine if i > req_id}
        self.historical_x = ts_array - self.t0
        self.historical_y = vals_dict
        self._render_curves(self.hist_curves, self.historical_x, self.historical_y, is_historical=True)
        self._settle_viewboxes()

    RENDER_LIMIT = 10000     # points per curve handed to pyqtgraph (min/max downsampled)
    LIVE_GAP_SECONDS = 2.0   # live samples further apart than this break the line

    def _handle_view_change(self):
        """Signal handler for X-Axis changes. Uses hardware state to detect Zoom vs Pan."""
        if getattr(self, '_auto_panning', False) or not self.lanes:
            return

        is_panning = QApplication.mouseButtons() != Qt.MouseButton.NoButton

        if self.auto_scroll and is_panning:
            self._toggle_scroll_lock(manual_break=True)

        if self.auto_scroll:
            base_plot = list(self.lanes.values())[0]
            vr = base_plot.viewRange()[0]
            self.live_span = vr[1] - vr[0]

        # ADD THIS: Reset the 150ms countdown every time the view shifts
        self._trigger_history_debounce()
        # Settle all lanes now so a user zoom/pan step costs one repaint, not two.
        self._settle_viewboxes()

    def _trigger_history_debounce(self):
        """Starts a 150ms countdown. If triggered again, the countdown resets."""
        self.history_debounce_timer.start(150)

    def _execute_history_fetch(self):
        """This only fires when the user STOPS dragging the mouse."""
        # Replace this with the actual method you use to load historical data.
        # e.g., self._check_and_fetch_history() or self._refresh_plot_history()

        if hasattr(self, '_check_and_fetch_history'):
            self._check_and_fetch_history()

    def request_ui_refresh(self):
        """
        The one true entry point for updating data after any UI change.
        Calls this after loading profiles, changing channels, or startup.
        """
        # 1. Clear existing items and rebuild layout
        self._build_lanes()
        self._hist_fetched = None  # channel set may have changed: force a re-query

        # 2. Reset inspector and legends
        self._ensure_inspector_items()
        self._refresh_event_markers()

        # 3. Delay the fetch by 200ms to allow the layout to stabilize
        # This prevents the 'clash' where signals fire before lanes are built.
        QTimer.singleShot(200, self._check_and_fetch_history)

    def _toggle_scroll_lock(self, manual_break=False):
        if manual_break:
            self.auto_scroll = False
        else:
            self.auto_scroll = not self.auto_scroll

        self.btn_scroll_lock.setChecked(self.auto_scroll)
        self.btn_scroll_lock.setText(FOLLOW_TEXT[0] if self.auto_scroll else FOLLOW_TEXT[1])

    # --- Profile & Config ---

    def open_channel_config(self):
        available_tags = {k: None for k in self.system_registry.keys()}
        dlg = ChannelSelectorDialog(available_tags, self.plot_config, self.system_registry, self)
        if dlg.exec():
            self.plot_config = dlg.get_selection()
            self.cache.register_client_tags(self.client_id,list(self.plot_config.keys()))

        self.request_ui_refresh()

    def _jump_to_date(self):
        if not self.lanes: return
        base_plot = list(self.lanes.values())[0]

        target_ts = self.jump_dt.dateTime().toSecsSinceEpoch()
        current_range = base_plot.viewRange()[0]
        width = current_range[1] - current_range[0]

        self._toggle_scroll_lock(manual_break=True)
        base_plot.setXRange(target_ts - self.t0, target_ts - self.t0 + width, padding=0)

    def _set_timespan(self, minutes):
        if not self.lanes: return
        base_plot = list(self.lanes.values())[0]
        span_seconds = minutes * 60
        self.live_span = span_seconds  # Update the internal target

        self._auto_panning = True

        # Get the right-most edge of the current data or view
        view_range = base_plot.viewRange()[0]
        right_edge = view_range[1]

        # If we are live, snap to the newest live sample
        latest = self.cache.live.latest_time()
        if self.auto_scroll and latest is not None:
            right_edge = latest - self.t0

        base_plot.setXRange(right_edge - span_seconds, right_edge, padding=0)
        self._check_and_fetch_history()  # History load happens under shield
        self._auto_panning = False

    def _check_and_fetch_history(self):
        if not self.lanes: return
        self._apply_time_limits()           # (the record grows; cheap unless the limits moved)
        base_plot = list(self.lanes.values())[0]

        view_start, view_end = base_plot.viewRange()[0]
        start_ts = self.t0 + view_start
        end_ts = self.t0 + view_end
        span = end_ts - start_ts

        oldest_live_ts = self.cache.live.oldest_time()
        if oldest_live_ts is None:
            oldest_live_ts = time.time()

        if start_ts < oldest_live_ts:
            self.is_historical_mode = True

            # --- COVERAGE-BASED REFETCH ---
            # Fetch half a view-width of margin either side, then only query disk
            # again when the view leaves that window or zooms in enough to need finer
            # data. Small pans/zooms are served from what is already loaded, and the
            # edges never show blank while a query is in flight.
            fetched = getattr(self, '_hist_fetched', None)
            need = fetched is None
            if not need:
                f_start, f_end, f_span = fetched
                need = (start_ts < f_start or end_ts > f_end
                        or span < f_span / self.HIST_REFINE_ZOOM)
            if need:
                margin = span * self.HIST_MARGIN
                q_start, q_end = start_ts - margin, end_ts + margin
                self._hist_fetched = (q_start, q_end, span)
                currently_selected = [tag for tag in self.cache.tags if self.plot_config.get(tag, {}).get('selected')]
                self.pending_hist_id = self.cache.request_historical_window(q_start, q_end,
                                                                            selected_tags=currently_selected)
                if not hasattr(self, '_my_hist_ids'):
                    self._my_hist_ids = set()
                self._my_hist_ids.add(self.pending_hist_id)
        else:
            if self.is_historical_mode:
                self._clear_historical_curves()
            self.is_historical_mode = False
            self._hist_fetched = None

        # Only re-place markers from cache; the 1 s marker_sync_timer does the DB read.
        self._refresh_event_markers(reload=False)

    def _clear_historical_curves(self):
        for c in self.hist_curves.values():
            c.setData([], [])

    PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..'))

    def closeEvent(self, event):
        super().closeEvent(event)


def _install_crash_logger():
    """PyQt6 calls qFatal() (abort, exit code 134 / 0xC0000409 on Windows) on ANY
    unhandled Python exception raised inside a Qt slot or timer callback, so a
    single bad frame kills the whole viewer. Log the traceback instead and keep
    running. Repeated identical errors (e.g. every 100 ms render frame) are only
    logged on the 1st, 10th, 100th... occurrence. (Installed after setup_logging,
    whose own hook logs every occurrence.)"""
    import traceback
    seen = {}

    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        n = seen.get(text, 0) + 1
        seen[text] = n
        if n in (1, 10, 100, 1000) or n % 10000 == 0:
            log.error("Unhandled exception in the viewer (occurrence #%d)", n, exc_info=(exc_type, exc, tb))

    sys.excepthook = hook


if __name__ == "__main__":
    from src.core.export_data import request_logger_flush
    from src.core.log_setup import setup_logging

    setup_logging("viewer")         # SystemLogs/viewer.log, like every service
    _install_crash_logger()

    # The viewer needs well over 200 MB resident (live ring buffer ~270 MB alone),
    # so don't apply the services' 200 MB working-set ceiling here.
    harden_windows_process(limit_working_set=False)
    app = QApplication(sys.argv)
    # The theme saved last (dark unless chosen otherwise: View > Theme), shared with the HMI
    theme.install(app)

    # 1. One data cache, shared by every window
    log.info("Data folder: %s", paths.DATA_DIR)
    master_cache = DualPipelineCache(paths.DATA_DIR)
    master_cache.start()

    # Ask the logger to write its in-RAM rows first, so the history meets the live data with no gap
    if request_logger_flush(timeout_s=2.0):
        log.info("Logger flushed its in-RAM rows")
    else:
        log.warning("Logger flush not confirmed - the last minutes of history may be missing until its next flush")

    # 2. The window(s): each fetches the data it needs as it renders
    window_1 = DataViewerApp(master_cache, window_title="SINDIE Data Viewer - Window 1")
    window_1.show()

    exit_code = app.exec()
    log.info("All windows closed: stopping the background threads")
    master_cache.stop()
    sys.exit(exit_code)
