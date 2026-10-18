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
import zmq
from PyQt6.QtCore import QDateTime, Qt, QTimer
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDateTimeEdit, QFormLayout, QHBoxLayout, QLabel,
                             QLineEdit, QMainWindow, QPushButton, QScrollArea, QVBoxLayout, QWidget)

from src.core import paths
from src.core.data_cache import DualPipelineCache
from src.core.event_helper import EventHelper
from src.core.network_map import ZMQ_PORT_LOGGER_CMD
from src.core.os_helper import harden_windows_process
from src.core.perf_utils import PerfTracker
from src.gui.data_viewer.channel_selector import ChannelSelectorDialog
from src.gui.data_viewer.config_manager import load_registry
from src.gui.data_viewer.event_overlay import EventOverlay
from src.gui.data_viewer.viewer_export import ExportMixin
from src.gui.data_viewer.viewer_inspector import InspectorMixin
from src.gui.data_viewer.viewer_lanes import LanesMixin
from src.gui.data_viewer.viewer_markers import MarkersMixin
from src.gui.data_viewer.viewer_profiles import PROFILES_FILE, ProfilesMixin
from src.gui.data_viewer.viewer_widgets import LogAxisItem, MarkerDialog, TimeAxisItem  # noqa: F401 (were defined here)

__all__ = ["DataViewerApp", "LogAxisItem", "MarkerDialog", "TimeAxisItem"]
from src.gui.settings import gui_settings

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

        # Add these to track color assignments
        self._assigned_colors = {}  # Maps {tag: hex_color}
        self._used_colors = set()  # Tracks which hex_colors are currently in use

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
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QHBoxLayout(main_widget)

        # --- Sidebar (Now Scrollable) ---
        self.sidebar_scroll = QScrollArea()
        self.sidebar_scroll.setFixedWidth(350)  # Slightly wider to fit the scrollbar safely
        self.sidebar_scroll.setWidgetResizable(True)
        self.sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sidebar_scroll.setStyleSheet("QScrollArea { border: none; }")

        sidebar = QWidget()
        sidebar_layout = QVBoxLayout(sidebar)
        self.sidebar_scroll.setWidget(sidebar)

        self.btn_scroll_lock = QPushButton("🟢 Scroll: LOCKED")
        self.btn_scroll_lock.setStyleSheet("background-color: #4CAF50; color: white; font-weight: bold; padding: 10px;")
        self.btn_scroll_lock.clicked.connect(lambda: self._toggle_scroll_lock())
        sidebar_layout.addWidget(self.btn_scroll_lock)

        self.btn_y_lock = QPushButton("🔓 Y-Scale: AUTO")
        self.btn_y_lock.clicked.connect(self._toggle_y_lock)
        sidebar_layout.addWidget(self.btn_y_lock)

        sidebar_layout.addWidget(QLabel("<b>Profiles:</b>"))

        # Create a mini horizontal layout for the dropdown + delete button
        profile_row = QHBoxLayout()

        self.combo_profiles = QComboBox()
        self.combo_profiles.addItems(self.profiles.keys())

        self.btn_delete_profile = QPushButton("🗑️")
        self.btn_delete_profile.setFixedWidth(35)
        # Connect to a wrapper function that passes the current text
        self.btn_delete_profile.clicked.connect(lambda: self._delete_profile(self.combo_profiles.currentText()))

        profile_row.addWidget(self.combo_profiles)
        profile_row.addWidget(self.btn_delete_profile)
        sidebar_layout.addLayout(profile_row)

        btn_load = QPushButton("Load Profile")
        btn_load.clicked.connect(self._apply_profile)
        sidebar_layout.addWidget(btn_load)

        btn_save_profile = QPushButton("💾 Save Profile As...")
        btn_save_profile.clicked.connect(self._save_current_profile)
        sidebar_layout.addWidget(btn_save_profile)

        self.btn_config = QPushButton("⚙️ Configure Channels")
        self.btn_config.setStyleSheet("""
                    QPushButton {
                        background-color: #2196F3; 
                        color: white; 
                        font-weight: bold; 
                        padding: 12px;
                        border-radius: 4px;
                    }
                    QPushButton:hover {
                        background-color: #1976D2;
                    }
                """)
        self.btn_config.clicked.connect(self.open_channel_config)
        sidebar_layout.addWidget(self.btn_config)

        sidebar_layout.addWidget(QLabel("<b>Jump to Date:</b>"))
        self.jump_dt = QDateTimeEdit(QDateTime.currentDateTime())
        self.jump_dt.setCalendarPopup(True)
        sidebar_layout.addWidget(self.jump_dt)

        btn_jump = QPushButton("🚀 Teleport to Date")
        btn_jump.clicked.connect(self._jump_to_date)
        sidebar_layout.addWidget(btn_jump)

        sidebar_layout.addWidget(QLabel("<b>Timespan:</b>"))
        span_layout = QHBoxLayout()
        for label, minutes in [("1m", 1), ("15m", 15), ("1h", 60), ("12h", 720)]:
            btn = QPushButton(label)
            btn.clicked.connect(lambda ch, m=minutes: self._set_timespan(m))
            span_layout.addWidget(btn)
        sidebar_layout.addLayout(span_layout)

        sidebar_layout.addSpacing(20)
        sidebar_layout.addWidget(QLabel("<b>Diagnostics:</b>"))

        self.btn_inspector = QPushButton("🔍 Enable Inspector")
        self.btn_inspector.setCheckable(True)
        self.btn_inspector.toggled.connect(self._on_inspector_toggled)
        sidebar_layout.addWidget(self.btn_inspector)

        sidebar_layout.addSpacing(20)
        sidebar_layout.addWidget(QLabel("<b>Live Channel Values:</b>"))
        self.value_display_layout = QFormLayout()
        self.sidebar_value_labels = {}
        sidebar_layout.addLayout(self.value_display_layout)

        sidebar_layout.addSpacing(20)
        sidebar_layout.addWidget(QLabel("<b>Export Settings:</b>"))

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
        btn_browse.setFixedWidth(30)
        btn_browse.clicked.connect(self._browse_export_path)
        path_layout.addWidget(btn_browse)
        sidebar_layout.addLayout(path_layout)

        self.btn_export = QPushButton("📸 Export Snapshot")
        self.btn_export.setStyleSheet("background-color: #673AB7; color: white; font-weight: bold; padding: 8px;")
        # lambda: QPushButton.clicked passes checked=False, which PyQt would feed
        # into _prepare_export's `options` parameter (export then did nothing)
        self.btn_export.clicked.connect(lambda: self._prepare_export())
        sidebar_layout.addWidget(self.btn_export)

        sidebar_layout.addSpacing(20)
        sidebar_layout.addSpacing(20)

        # 1. The Toggle Button
        self.btn_overlay_toggle = QPushButton("▼ Event Overlays")
        self.btn_overlay_toggle.setStyleSheet("text-align: left; font-weight: bold; border: none; padding: 5px;")
        self.btn_overlay_toggle.setCheckable(True)
        sidebar_layout.addWidget(self.btn_overlay_toggle)

        # 2. The Container Frame
        self.overlay_frame = QWidget()
        overlay_layout = QVBoxLayout(self.overlay_frame)
        overlay_layout.setContentsMargins(15, 0, 0, 0)  # Indent the checkboxes slightly

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

        sidebar_layout.addWidget(self.overlay_frame)

        # 3. Toggle Logic
        def toggle_overlays(checked):
            self.overlay_frame.setVisible(not checked)
            self.btn_overlay_toggle.setText("▶ Event Overlays" if checked else "▼ Event Overlays")

        self.btn_overlay_toggle.clicked.connect(toggle_overlays)
        self.btn_overlay_toggle.setChecked(False)  # Menu starts OPEN by default
        # ----------------------------------

        sidebar_layout.addStretch()
        layout.addWidget(self.sidebar_scroll)

        pg.setConfigOptions(antialias=True)

        # Core container replaces the single PlotWidget
        self.layout_widget = pg.GraphicsLayoutWidget()
        layout.addWidget(self.layout_widget)
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
        print("[UI] Executing 02:30 AM Data Mart Refresh...")
        threading.Thread(target=self.cache.engine.populate_macro_marts, daemon=True).start()

        # 4. Now that we are aligned to 2:30 AM, start a standard 24-hour repeating loop
        self.daily_sync_timer = QTimer()
        self.daily_sync_timer.timeout.connect(
            lambda: threading.Thread(target=self.cache.engine.populate_macro_marts, daemon=True).start()
        )
        self.daily_sync_timer.start(86400000)  # 24 hours in milliseconds

    def _toggle_y_lock(self):
        self.y_auto_scale = not self.y_auto_scale

        if self.y_auto_scale:
            self.btn_y_lock.setText("🔓 Y-Scale: AUTO")
            self.btn_y_lock.setStyleSheet("")
        else:
            self.btn_y_lock.setText("🔒 Y-Scale: LOCKED")
            self.btn_y_lock.setStyleSheet("background-color: #FF9800; color: white;")

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
        #print(f"[DEBUG-UI] Received history for ID {req_id}. Expected ID: {self.pending_hist_id}. Points: {len(ts_array)}") # ADD THIS
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
        # if not self.is_historical_mode: return  ### not used right now?
        #t_start = time.perf_counter()
        self.historical_x = ts_array - self.t0
        self.historical_y = vals_dict
        #t_prep = time.perf_counter()
        self._render_curves(self.hist_curves, self.historical_x, self.historical_y, is_historical=True)
        self._settle_viewboxes()

        #t_end = time.perf_counter()
        #print(f"[UI] Historical Render Complete: {(t_end - t_prep) * 1000:.1f} ms (Total UI block: {(t_end - t_start) * 1000:.1f} ms)")

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

        if self.auto_scroll:
            self.btn_scroll_lock.setText("🟢 Scroll: LOCKED")
            self.btn_scroll_lock.setStyleSheet(
                "background-color: #4CAF50; color: white; font-weight: bold; padding: 10px;")
        else:
            self.btn_scroll_lock.setText("🔴 Scroll: MANUAL")
            self.btn_scroll_lock.setStyleSheet(
                "background-color: #f44336; color: white; font-weight: bold; padding: 10px;")

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


def _install_crash_logger(project_root):
    """PyQt6 calls qFatal() (abort, exit code 134 / 0xC0000409 on Windows) on ANY
    unhandled Python exception raised inside a Qt slot or timer callback, so a
    single bad frame kills the whole viewer. Log the traceback instead and keep
    running. Repeated identical errors (e.g. every 100 ms render frame) are only
    printed on the 1st, 10th, 100th... occurrence."""
    import traceback
    log_dir = os.path.join(project_root, "SystemLogs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "viewer_errors.log")
    seen = {}

    def hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        n = seen.get(text, 0) + 1
        seen[text] = n
        if n in (1, 10, 100, 1000) or n % 10000 == 0:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            msg = f"[{stamp}] Unhandled exception in viewer (occurrence #{n}):\n{text}\n"
            sys.stderr.write(msg)
            try:
                with open(log_path, "a") as f:
                    f.write(msg)
            except OSError:
                pass

    sys.excepthook = hook


if __name__ == "__main__":

    _install_crash_logger(os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../..")))

    # The viewer needs well over 200 MB resident (live ring buffer ~270 MB alone),
    # so don't apply the services' 200 MB working-set ceiling here.
    harden_windows_process(limit_working_set=False)
    app = QApplication(sys.argv)

    # 1. Initialize the Single Master Cache
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(current_dir, "../../.."))
    log_dir = paths.DATA_DIR
    print(log_dir)

    print("[Launcher] Spinning up Master Data Cache...")
    master_cache = DualPipelineCache(log_dir)
    master_cache.start()

    print("[Launcher] Forcing logger RAM flush to bridge historical gap...")
    try:
        cmd_req = zmq.Context.instance().socket(zmq.REQ)
        cmd_req.setsockopt(zmq.RCVTIMEO, 2000)  # 2-second timeout prevents infinite hang
        cmd_req.connect(ZMQ_PORT_LOGGER_CMD)

        # Send the command
        cmd_req.send_string("FORCE_FLUSH")

        # This blocks execution until the logger replies, eliminating the race condition
        reply = cmd_req.recv_string()
        print(f"[Launcher] Logger confirmed: {reply}")

    except zmq.error.Again:
        print("[Launcher] WARNING: Logger flush timed out. Historical gap may be present.")
    except Exception as e:
        print(f"[Launcher] WARNING: Flush failed - {e}")

    # 2. Launch Multiple Thin-Client Windows
    # The windows will automatically fetch their required data the moment they render
    window_1 = DataViewerApp(master_cache, window_title="SINDIE Data Viewer - Window 1")
    #window_2 = DataViewerApp(master_cache, window_title="SINDIE Data Viewer - Window 2")

    window_1.show()
    #window_2.show()

    # 3. Execute Application Loop
    exit_code = app.exec()

    # 4. Graceful Teardown
    print("[Launcher] All windows closed. Terminating background threads...")
    master_cache.stop()
    sys.exit(exit_code)