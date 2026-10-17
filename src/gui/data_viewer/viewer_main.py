import sys
import os
import time
import numpy as np
import copy
from datetime import datetime, timedelta
import textwrap
import sqlite3
import json
import zmq
import orjson
import threading
import zlib

from src.core.event_helper import EventHelper
from src.core.os_helper import harden_windows_process

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QVBoxLayout, QHBoxLayout,
    QWidget, QPushButton, QLabel, QDateTimeEdit,
    QFileDialog, QCheckBox, QScrollArea
)
from PyQt6.QtCore import Qt, QDateTime, QTimer, QThread, pyqtSignal
import pyqtgraph as pg

from src.core.network_map import ZMQ_PORT_LOGGER_CMD
from src.gui.data_viewer.config_manager import load_registry
from src.gui.data_viewer.channel_selector import ChannelSelectorDialog
from src.core.data_cache import DualPipelineCache

from PyQt6.QtWidgets import QDialog, QFormLayout, QLineEdit, QComboBox, QDialogButtonBox, QSizePolicy
from PyQt6.QtGui import QIcon, QPixmap, QColor
from PyQt6.QtCore import QSize

from src.core.perf_utils import PerfTracker
from src.core.live_buffer import break_gaps
from src.core import export_data as export_data
from src.gui.data_viewer.export_dialog import ExportDialog, ExportWorker

class MarkerDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Event Marker")
        self.setMinimumWidth(300)
        layout = QFormLayout(self)

        self.text_input = QLineEdit()
        self.color_input = QComboBox()

        # SYSTEM RESERVED COLORS:
        # Red (#F44336)    = Fault Activated
        # Blue (#2196F3)   = Fault Cleared
        # Orange (#FF9800) = Watchdog
        # Green (#4CAF50)  = Phase Change

        # User-safe distinct palette:
        self.colors = {
            "Cyan": "#00E5FF",
            "Yellow": "#FFEA00",
            "Magenta": "#FF4081",
            "White": "#FFFFFF",
            "Purple": "#9C27B0",
            "Pink": "#E91E63"
        }

        # Generate Icons for the ComboBox
        for name, hex_code in self.colors.items():
            # Create a 16x16 colored square
            pixmap = QPixmap(16, 16)
            pixmap.fill(QColor(hex_code))
            icon = QIcon(pixmap)
            self.color_input.addItem(icon, name)

        self.color_input.setIconSize(QSize(16, 16))

        layout.addRow("Note:", self.text_input)
        layout.addRow("Color:", self.color_input)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def get_data(self):
        color_name = self.color_input.currentText()
        return self.text_input.text(), self.colors[color_name]

# --- Custom Axis Items ---

class LogAxisItem(pg.AxisItem):
    def tickStrings(self, values, scale, spacing):
        strings = []
        for v in values:
            try:
                real_val = 10 ** float(v)
                strings.append(f"{real_val:.1e}")
            except:
                strings.append("")
        return strings


class TimeAxisItem(pg.AxisItem):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.offset = 0.0
        self.hide_text = False  # NEW: Flag to control text rendering

    def set_offset(self, offset):
        self.offset = offset

    def tickStrings(self, values, scale, spacing):
        # NEW: If this is an upper lane, return blank strings.
        # The grid still draws, but the text is invisible.
        if self.hide_text:
            return [""] * len(values)

        strings = []
        for v in values:
            try:
                dt = datetime.fromtimestamp(v + self.offset)
                if spacing >= 86400:
                    fmt = '%Y-%m-%d'
                elif spacing >= 3600:
                    fmt = '%b %d\n%H:%M'
                elif spacing >= 60:
                    fmt = '%H:%M'
                elif spacing >= 1:
                    fmt = '%H:%M:%S'
                else:
                    fmt = '%H:%M:%S.%f'
                strings.append(dt.strftime(fmt))
            except:
                strings.append("")
        return strings

# --- Main Application ---

class DataViewerApp(QMainWindow):
    HIST_MARGIN = 0.5        # extra view-widths fetched either side of the view
    HIST_REFINE_ZOOM = 1.5   # re-query when zoomed in this much past the fetch
    GRID_TICK_LEVEL = 0      # grid/tick levels drawn: 0 = major only (fastest), 1 = +minor, 2 = pyqtgraph default
    LIVE_FPS = 5             # live plot refresh rate (was 10; ~halves live-mode CPU)

    def __init__(self, shared_cache, window_title="IPIDS Data Viewer"):
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
        current_direc = os.path.dirname(os.path.abspath(__file__))
        self.profiles_file = os.path.join(os.path.dirname(current_direc), "config", "workspace_profiles.json")
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

        self.edit_export_path.setText(os.path.join(default_path, "IPIDS_Snapshot"))
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
            "Phase Changes": ["PHASE_CHANGE"]
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

        self.marker_items = {}
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

    def _build_lanes(self):
        """Reconstructs the plotting grid, preserving view state and preventing memory leaks."""
        current_range = None
        if self.lanes:
            # Save the current X-axis zoom level to restore it after the rebuild
            current_range = list(self.lanes.values())[0].viewRange()[0]

        # --- 1. DEEP CLEANUP ---
        # Explicitly remove items from the scene to prevent memory leaks in long-running sessions
        for vb in self.lane_axes.values():
            if vb.scene(): vb.scene().removeItem(vb)
        for c in self.curves.values():
            if c.scene(): c.scene().removeItem(c)
        for m in self.marker_items.values():
            if m.scene(): m.scene().removeItem(m)

        self.layout_widget.clear()
        self.layout_widget.ci.layout.setSpacing(35)

        # Reset all state tracking containers
        self.lanes.clear()
        self.lane_axes.clear()
        self.lane_legends.clear()
        self.log_axis_widgets = {}  # Ensure this is reset
        self.v_lines.clear()
        self.pin_lines.clear()
        self.delta_labels.clear()
        self.curves.clear()
        self.hist_curves.clear()
        self.marker_items.clear()
        self.time_axes = []
        self._legend_tags = set()

        # Attempt to free up colours for next set of channels?
        self._assigned_colors = {}
        self._used_colors = set()

        # Identify which lanes actually have selected tags
        active_lanes = set()
        for tag, cfg in self.plot_config.items():
            if cfg.get('selected'):
                active_lanes.add(cfg.get('lane', 'Lane 1'))

        sorted_lanes = sorted(list(active_lanes))

        if not sorted_lanes:
            # Add a centered prompt to the layout
            prompt_text = (
                "<div style='text-align: center;'>"
                "<span style='color: #888; font-size: 18pt; font-weight: bold;'>"
                "No Channels Selected</span><br>"
                "<span style='color: #666; font-size: 12pt;'>"
                "Click 'Configure Channels' in the sidebar to begin.</span>"
                "</div>"
            )
            label = self.layout_widget.addLabel(prompt_text, row=0, col=0)
            # Center it in the available space
            self.layout_widget.ci.layout.setRowStretchFactor(0, 1)
            return

        base_plot = None

        # --- 2. GRID CONSTRUCTION ---
        for i, lane_name in enumerate(sorted_lanes):
            # A. Setup Custom Time Axis
            time_axis = TimeAxisItem(orientation='bottom')
            time_axis.setHeight(45)
            if i < len(sorted_lanes) - 1:
                time_axis.hide_text = True  # Only show time labels on the bottom-most lane
            self.time_axes.append(time_axis)

            # B. Create the Plot Item
            # The log axis must be passed in as the PlotItem's 'right' axis:
            # PlotItem already reserves layout cell (2, 2) for its own right axis,
            # so adding a second item there later fails ("cell already taken")
            # and the log axis was never displayed.
            log_axis = LogAxisItem(orientation='right')
            p = self.layout_widget.addPlot(row=i, col=0, axisItems={'bottom': time_axis, 'right': log_axis})
            p.showGrid(x=True, y=True, alpha=0.5)
            # Grid lines dominate redraw cost (measured at 1080p: 3 tick levels
            # = 56 ms/redraw, 2 levels = 25 ms, major only = 11 ms, no grid = 10 ms).
            for _ax in ("bottom", "left"):
                p.getAxis(_ax).setStyle(maxTickLevel=self.GRID_TICK_LEVEL)
            p.setMenuEnabled(False)
            p.hideButtons()

            # C. Initialize the Log ViewBox (Fixes "Referenced before assignment")
            right_vb = pg.ViewBox()
            right_vb.setMenuEnabled(False)
            right_vb.setMouseEnabled(x=True, y=False)
            p.scene().addItem(right_vb)

            # D. Configure ViewBox Auto-Scaling (Y-Axis only)
            for vb in [p.vb, right_vb]:
                vb.setMouseEnabled(x=True, y=False)
                vb.setAutoVisible(y=True)
                vb.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)
                vb.disableAutoRange(axis=pg.ViewBox.XAxis)

                # ADD THIS: The SCADA Deadzone trick. 15% padding prevents micro-twitching.
                vb.setDefaultPadding(0.15)

            # E. Setup Log Axis (Right side)
            log_axis.linkToView(right_vb)  # re-link from p.vb to the log ViewBox
            right_vb.setXLink(p)  # Sync X-axis between Linear and Log views
            self.log_axis_widgets[lane_name] = log_axis

            # F. Linking & View Restoration
            if base_plot is None:
                base_plot = p
                # Only the first lane needs to handle the manual view change signals
                base_plot.sigRangeChanged.connect(self._handle_view_change)
                self._auto_panning = True
                if current_range:
                    base_plot.setXRange(current_range[0], current_range[1], padding=0)
                else:
                    base_plot.setXRange(-300, 0, padding=0)
                self._auto_panning = False
            else:
                # Chain all other lanes to the first one
                p.setXLink(base_plot)

            # G. Add Legend
            legend = p.addLegend(offset=(10, 10))
            legend.setBrush(pg.mkBrush(0, 0, 0, 150))
            self.lane_legends[lane_name] = legend

            # H. Sync Geometry (Ensures the log axis overlay aligns with the plot)
            p.vb.sigResized.connect(lambda _, vb=right_vb, plot=p: self._sync_specific_viewbox(plot, vb))

            # I. Initialize Inspector Items (Crosshairs & Pins)
            v_line = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen('y', width=1, style=Qt.PenStyle.DashLine))
            v_line.hide()
            p.addItem(v_line, ignoreBounds=True)
            self.v_lines.append(v_line)

            pin_line = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen('#FF5722', width=1.0))
            pin_line.hide()
            p.addItem(pin_line, ignoreBounds=True)
            self.pin_lines.append(pin_line)

            delta_label = pg.TextItem(anchor=(0, 0), color='#FF5722', fill=(0, 0, 0, 150))
            delta_label.hide()
            p.addItem(delta_label, ignoreBounds=True)
            self.delta_labels.append(delta_label)

            # Store references
            self.lanes[lane_name] = p
            self.lane_axes[lane_name] = right_vb

        # Clear existing sidebar labels
        while self.value_display_layout.count():
            item = self.value_display_layout.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        self.sidebar_value_labels.clear()

        # Generate new static labels
        for tag in sorted_lanes:
            for t, cfg in self.plot_config.items():
                if cfg.get('selected') and cfg.get('lane') == tag:
                    # 1. Right Column (Value): Swap to MinimumExpanding so it doesn't clip
                    val_lbl = QLabel("---")
                    val_lbl.setStyleSheet("font-family: monospace; font-size: 14px; color: #2196F3; font-weight: bold;")
                    val_lbl.setMinimumWidth(130)  # Safe width for scientific notation & deltas
                    val_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    val_lbl.setSizePolicy(QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Preferred)

                    # 2. Left Column (Name): Swap to Minimum so it only takes up necessary space
                    label_text = str(cfg.get('label', t))

                    # Note: The static [{unit}] was removed here because we moved the unit to the right side!
                    name_lbl = QLabel(f"{label_text}:")
                    name_lbl.setStyleSheet("font-size: 11px; color: #555;")
                    name_lbl.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Preferred)

                    self.value_display_layout.addRow(name_lbl, val_lbl)
                    self.sidebar_value_labels[t] = val_lbl

        self._apply_lane_axis_styles()

    def _apply_lane_axis_styles(self):
        """Per-lane axis/grid setup from the channel config (called on layout build).

        Linear channels -> left axis values + horizontal grid from the left axis.
        Log channels    -> right log axis; if the lane is log-only, its horizontal
        grid comes from the log axis, so grid lines line up with the decades
        instead of the hidden linear scale.
        """
        alpha = 128
        for lane, p in self.lanes.items():
            cfgs = [c for c in self.plot_config.values()
                    if c.get('selected') and c.get('lane', 'Lane 1') == lane]
            has_log = any(c.get('scale') == 'log' for c in cfgs)
            has_lin = any(c.get('scale') != 'log' for c in cfgs)
            left = p.getAxis('left')
            left.setStyle(showValues=has_lin)
            left.setGrid(alpha if has_lin else False)
            if lane in self.lane_axes:
                self.lane_axes[lane].setVisible(has_log)
            log_axis = self.log_axis_widgets.get(lane)
            if log_axis is not None:
                log_axis.setStyle(maxTickLevel=self.GRID_TICK_LEVEL)
                log_axis.setGrid(alpha if (has_log and not has_lin) else False)
                log_axis.setVisible(has_log)

    def _format_with_si_prefix(self, value: float, base_unit: str, is_delta: bool = False) -> str:
        """Dynamically scales base units to a readable SI prefix string or scientific notation."""
        if value is None:
            return "---"

        # 1. Specific Override for Vacuum Pressure (Scientific Notation)
        if base_unit == "mB":
            # Use 'e' for scientific notation (e.g., 5.30e-07)
            sci_fmt = "+.2e" if is_delta else ".2e"
            return f"{value:{sci_fmt}} {base_unit}"

        # 2. General Float Format for everything else
        fmt = "+.2f" if is_delta else ".2f"

        # 3. Dynamic SI Prefixing for Amperes
        if base_unit == "A":
            abs_val = abs(value)
            if abs_val >= 1:
                return f"{value * 1:{fmt}} A"
            if abs_val >= 1e-3:
                return f"{value * 1e3:{fmt}} mA"
            elif abs_val >= 1e-6:
                return f"{value * 1e6:{fmt}} \u03BCA"
            elif abs_val >= 1e-9:
                return f"{value * 1e9:{fmt}} nA"
            else:
                return f"{value * 1e12:{fmt}} pA"

        # 4. Fallback for standard non-scaled units (V, Hz, %, etc.)
        return f"{value:{fmt}} {base_unit}"

    def _sync_specific_viewbox(self, main_plot, right_viewbox):
        """Forces geometry alignment between a specific lane and its log axis."""
        right_viewbox.setGeometry(main_plot.vb.sceneBoundingRect())
        right_viewbox.linkedViewChanged(main_plot.vb, right_viewbox.XAxis)

    # --- Core Rendering ---

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

    def _curve_for(self, target_dict, tag, cfg, lane):
        """Get (or create, add to its lane and legend) the curve for a channel."""
        c = target_dict.get(tag)
        if c is None:
            pen = pg.mkPen(color=self._get_distinct_color(tag), width=1.0)
            # clipToView/autoDownsample are off: the viewer already slices to the
            # view and min/max-downsamples, and with them on pyqtgraph re-ran
            # setData on every view change (part of the double-repaint cascade).
            c = pg.PlotDataItem(pen=pen, autoDownsample=False, clipToView=False, connect='finite')
            if cfg.get('scale') == 'log':
                self.lane_axes[lane].addItem(c)
            else:
                self.lanes[lane].addItem(c)
            # One legend entry per channel, from whichever curve (live or historical)
            # appears first. Previously only live curves added entries, so with no
            # live data the plots had no legend at all.
            if tag not in self._legend_tags:
                label_text = cfg.get('label', tag)
                unit = getattr(self, 'system_registry', {}).get(tag, {}).get("unit", "")
                unit_str = f" [{unit}]" if unit else ""
                self.lane_legends[lane].addItem(c, name=f"{label_text}{unit_str}")
                self._legend_tags.add(tag)
            target_dict[tag] = c
        c.setVisible(True)
        return c

    @staticmethod
    def _scaled(cfg, y):
        """Apply the channel multiplier and log scaling for plotting."""
        y = np.asarray(y, dtype=np.float64)
        mult = cfg.get('multiplier', 1.0)
        if mult != 1.0:
            y = y * mult
        if cfg.get('scale') == 'log':
            with np.errstate(all='ignore'):
                y = np.log10(np.clip(y, 1e-12, None))
        return y

    def _plot_targets(self, target_dict):
        """Yield (tag, cfg, lane) for selected channels with a lane; hide the rest."""
        for tag in sorted(self.cache.tags):
            cfg = self.plot_config.get(tag, {})
            lane = cfg.get('lane', 'Lane 1')
            if not cfg.get('selected') or lane not in self.lanes:
                if tag in target_dict:
                    target_dict[tag].setVisible(False)
                continue
            yield tag, cfg, lane

    def _render_curves(self, target_dict, x_array, y_dict, is_historical=True):
        """Historical curves: all channels share one time base (x_array, relative)."""
        n = len(x_array)
        downsample = n > self.RENDER_LIMIT
        x_plot = None
        for tag, cfg, lane in self._plot_targets(target_dict):
            c = self._curve_for(target_dict, tag, cfg, lane)
            y = y_dict.get(tag)
            if y is None or len(y) == 0 or len(y) != n:
                # (length mismatch would make pyqtgraph raise inside a Qt slot)
                c.setData([], [])
                continue
            if downsample:
                xs, y = self.cache.engine.downsample_minmax(x_array, y, target_points=self.RENDER_LIMIT)
                if x_plot is None:
                    x_plot = xs
            else:
                x_plot = x_array
            c.setData(x_plot, self._scaled(cfg, y))

    def _render_live_curves(self, live):
        """Live curves: each channel has its own source's timestamps (absolute)."""
        for tag, cfg, lane in self._plot_targets(self.curves):
            c = self._curve_for(self.curves, tag, cfg, lane)
            xy = live.get(tag)
            if xy is None or len(xy[0]) == 0:
                c.setData([], [])
                continue
            x, y = xy
            if len(x) > self.RENDER_LIMIT:
                x, y = self.cache.engine.downsample_minmax(x, y, target_points=self.RENDER_LIMIT)
            x = x - self.t0
            # Break the line across dropouts (a source going quiet), scaled so a
            # zoomed-out, downsampled view isn't chopped up.
            gap = self.LIVE_GAP_SECONDS
            if len(x) > 2:
                gap = max(gap, 4.0 * (x[-1] - x[0]) / len(x))
            x, y = break_gaps(x, y, gap)
            c.setData(x, self._scaled(cfg, y))

    # --- Event Handlers ---

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

    def _save_current_profile(self):
        """Saves the current lane and channel configuration as a reusable profile."""
        from PyQt6.QtWidgets import QInputDialog, QMessageBox
        import os
        import json
        import copy

        name, ok = QInputDialog.getText(self, "Save Profile", "Enter profile name:")
        if ok and name.strip():
            name = name.strip()

            # 1. Add to RAM dictionary
            self.profiles[name] = copy.deepcopy(self.plot_config)

            # 2. Update UI ComboBox
            if self.combo_profiles.findText(name) == -1:
                self.combo_profiles.addItem(name)
            self.combo_profiles.setCurrentText(name)

            # 3. Save to Disk (Safe Multi-Window Read-Modify-Write)
            try:
                # Ensure directory exists
                os.makedirs(os.path.dirname(self.profiles_file), exist_ok=True)

                # A. Read current state of the disk to preserve other windows' saves
                disk_profiles = {}
                if os.path.exists(self.profiles_file):
                    try:
                        with open(self.profiles_file, "r") as f:
                            disk_profiles = json.load(f)
                    except json.JSONDecodeError:
                        pass  # File is corrupt or completely empty, start fresh

                # B. Inject this window's new profile into the up-to-date disk state
                disk_profiles[name] = copy.deepcopy(self.plot_config)

                # C. Save the merged dictionary back to the file
                with open(self.profiles_file, "w") as f:
                    json.dump(disk_profiles, f, indent=4)

                # D. Update local RAM so this window immediately "sees" any profiles
                #    that other windows might have saved recently.
                self.profiles = disk_profiles

                QMessageBox.information(self, "Success", f"Profile '{name}' saved successfully.")

            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to save profile:\n{e}")

    def _load_all_profiles(self):
        profiles = {"Default": {}}  # Hardcoded safety start
        try:
            if os.path.exists(self.profiles_file):
                with open(self.profiles_file, "r") as f:
                    disk_data = json.load(f)
                    if isinstance(disk_data, dict):
                        profiles.update(disk_data)
        except Exception as e:
            print(f"[Profiles] Load error, using defaults: {e}")

        return profiles

    def _apply_profile(self):
        name = self.combo_profiles.currentText()
        if name in self.profiles:
            self.plot_config = copy.deepcopy(self.profiles[name])
            self.cache.register_client_tags(self.client_id, list(self.plot_config.keys()))

            self.request_ui_refresh()

    def open_channel_config(self):
        available_tags = {k: None for k in self.system_registry.keys()}
        dlg = ChannelSelectorDialog(available_tags, self.plot_config, self.system_registry, self)
        if dlg.exec():
            self.plot_config = dlg.get_selection()
            self.cache.register_client_tags(self.client_id,list(self.plot_config.keys()))

        self.request_ui_refresh()

    import zlib
    import pyqtgraph as pg

    def _get_distinct_color(self, tag: str):
        """Returns a unique, high-visibility color, resolving hash collisions via linear probing."""

        # 1. Return immediately if this tag already has a permanent color assigned
        if tag in self._assigned_colors:
            return pg.mkColor(self._assigned_colors[tag])

        # High-Vibrancy "Neon" Palette (No pastels, maximum saturation)
        palette = [
            "#FF1493",  # Deep Pink
            "#00FFFF",  # Cyan / Aqua
            "#39FF14",  # Neon Green
            "#FF4500",  # Orange Red
            "#9400D3",  # Neon Violet
            "#FFD700",  # Golden Yellow
            "#00BFFF",  # Deep Sky Blue
            "#FF00FF",  # Magenta
            "#7FFF00",  # Chartreuse
            "#FF3131",  # Neon Red
            "#1E90FF",  # Dodger Blue
            "#FF8C00",  # Dark Orange
            "#00FA9A",  # Medium Spring Green
            "#8A2BE2",  # Blue Violet
            "#F0E68C",  # Khaki (Bright)
            "#00FF7F"   # Spring Green
        ]

        # 2. Hash the tag to find a preferred starting index
        hash_val = zlib.crc32(tag.encode('utf-8'))
        start_idx = hash_val % len(palette)

        # 3. Collision Resolution (Linear Probing)
        # Scan the palette starting from our hash index to find an empty slot
        for offset in range(len(palette)):
            idx = (start_idx + offset) % len(palette)
            candidate_color = palette[idx]

            if candidate_color not in self._used_colors:
                self._assigned_colors[tag] = candidate_color
                self._used_colors.add(candidate_color)
                return pg.mkColor(candidate_color)

        # 4. Palette Exhaustion Fallback
        # If you plot more than 20 lines simultaneously, all colors are taken.
        # Fallback to the raw hash index and accept the visual collision.
        fallback_color = palette[start_idx]
        self._assigned_colors[tag] = fallback_color
        return pg.mkColor(fallback_color)

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

    def _values_at(self, rel_x):
        """Channel values at a time (relative x) for the inspector and pins.

        Returns (snapped_rel_x, {tag: raw value}). Uses historical data when the
        time is before the live buffer, otherwise the live window (step-hold per
        channel, since channels now have their own source timestamps).
        """
        live_start = self.cache.live.oldest_time()
        in_hist = (self.is_historical_mode and len(getattr(self, 'historical_x', [])) > 0
                   and (live_start is None or rel_x + self.t0 < live_start))
        vals = {}
        if in_hist:
            xs = self.historical_x
            idx = int(np.clip(np.searchsorted(xs, rel_x, side='right') - 1, 0, len(xs) - 1))
            for tag, y in self.historical_y.items():
                if len(y) > idx:
                    vals[tag] = float(y[idx])
            return float(xs[idx]), vals
        live = getattr(self, 'current_live', None) or {}
        if not live:
            return None, vals
        abs_ts = rel_x + self.t0
        for tag, (x, y) in live.items():
            i = np.searchsorted(x, abs_ts, side='right') - 1
            if 0 <= i < len(y):
                vals[tag] = float(y[i])
        return rel_x, vals

    def _on_mouse_moved(self, pos):
        if not self.btn_inspector.isChecked() or not self.lanes:
            return

        # 1. HOVER DETECTION
        hovered_plot = None
        min_dist = float('inf')
        for p in self.lanes.values():
            rect = p.sceneBoundingRect()
            if rect.left() <= pos.x() <= rect.right():
                dist = abs(pos.y() - rect.center().y())
                if dist < min_dist:
                    min_dist = dist
                    hovered_plot = p
        ref_plot = hovered_plot if hovered_plot else list(self.lanes.values())[0]

        mouse_point = ref_plot.vb.mapSceneToView(pos)
        actual_x, values = self._values_at(mouse_point.x())
        if actual_x is None:
            return

        for v_line in self.v_lines:
            v_line.setPos(actual_x)
            v_line.setVisible(True)

        # --- DYNAMIC ANCHORING ---
        view_rect = ref_plot.vb.viewRect()
        x_pct = (mouse_point.x() - view_rect.left()) / view_rect.width()
        y_pct = (mouse_point.y() - view_rect.bottom()) / view_rect.height()
        anchor_x = 1.1 if x_pct > 0.8 else -0.1
        anchor_y = -0.2 if y_pct > 0.5 else 1.2

        active_pin = getattr(self, 'pin_timestamp', None)
        delta_msg = ""
        if active_pin is not None:
            delta_msg = f"Δt: {self._format_delta_time(actual_x + self.t0 - active_pin)}"

        for i, p in enumerate(self.lanes.values()):
            label = self.delta_labels[i]
            if p == hovered_plot and delta_msg != "":
                label.setAnchor((anchor_x, anchor_y))
                label.setPos(actual_x, mouse_point.y())
                label.setText(delta_msg)
                label.show()
            else:
                label.hide()

        # --- SIDEBAR UPDATES --- (was inside the per-lane loop, i.e. repeated per lane)
        pinned = getattr(self, 'pinned_values', {})
        for tag, lbl in getattr(self, 'sidebar_value_labels', {}).items():
            if tag not in values:
                continue
            cfg = self.plot_config.get(tag, {})
            mult = cfg.get('multiplier', 1.0)   # same scaling as the live readout
            val = values[tag] * mult
            base_unit = getattr(self, 'system_registry', {}).get(tag, {}).get("unit", "")
            display_text = self._format_with_si_prefix(val, base_unit)
            if active_pin is not None and pinned.get(tag) is not None:
                dy = val - pinned[tag] * mult
                # Append the delta (previously this REPLACED the value text)
                display_text += f"  (\u0394: {self._format_with_si_prefix(dy, base_unit, is_delta=True)})"
            lbl.setText(display_text)

    def _on_graph_clicked(self, event):
        if not self.lanes: return
        ref_plot = list(self.lanes.values())[0]
        pos = event.scenePos()
        mouse_point = ref_plot.vb.mapSceneToView(pos)

        # Middle Click: Re-center
        if event.button() == Qt.MouseButton.MiddleButton:
            view_range = ref_plot.viewRange()[0]
            width = view_range[1] - view_range[0]
            ref_plot.setXRange(mouse_point.x() - width / 2, mouse_point.x() + width / 2, padding=0)
            return

        # Ctrl + Left Click: Pin
        if event.button() == Qt.MouseButton.LeftButton and QApplication.keyboardModifiers() == Qt.KeyboardModifier.ControlModifier:
            if not self.btn_inspector.isChecked(): return

            snapped_x, values = self._values_at(mouse_point.x())
            if snapped_x is None:
                return

            # Store the absolute timestamp and the exact values at the pin
            self.pin_timestamp = snapped_x + self.t0
            self.pinned_values = {tag: values.get(tag) for tag in self.cache.tags}

            # Update visual pin lines
            for pin_line in self.pin_lines:
                pin_line.setPos(self.pin_timestamp - self.t0)
                pin_line.show()

        # Right Click: Clear Pin
        elif event.button() == Qt.MouseButton.RightButton:
            self.pin_timestamp = None
            self.pinned_values = {}
            for pin_line in self.pin_lines: pin_line.hide()
            for label in self.delta_labels: label.hide()

    def _handle_click_events(self, event):
        """Double click for markers. Only triggers if clicking the background."""
        # If the user clicked an item (like an InfiniteLine), ignore this event
        if self.layout_widget.scene().items(event.scenePos()):
            for item in self.layout_widget.scene().items(event.scenePos()):
                if isinstance(item, pg.InfiniteLine):
                    return

        if event.double() and self.lanes:
            ref_plot = list(self.lanes.values())[0]
            pos = event.scenePos()
            mouse_point = ref_plot.vb.mapSceneToView(pos)
            absolute_ts = mouse_point.x() + self.t0

            dlg = MarkerDialog(self)
            if dlg.exec():
                text, color = dlg.get_data()
                if text:
                    self._add_marker_to_graph(absolute_ts, text, color)
                    self._save_markers(absolute_ts, {"text": text, "color": color})

    def _ensure_inspector_items(self):
        """Syncs inspector visibility state across all lanes."""
        is_on = self.btn_inspector.isChecked()
        for v_line in self.v_lines:
            v_line.setVisible(is_on)

        has_pin = getattr(self, 'pin_timestamp', None) is not None  # (was self.pin_idx, never set)
        for pin_line in self.pin_lines:
            pin_line.setVisible(is_on and has_pin)

        for delta in self.delta_labels:
            if not (is_on and has_pin):
                delta.hide()

    def _on_inspector_toggled(self, checked):
        self._ensure_inspector_items()

        if checked:
            self.btn_inspector.setText("🔍 Inspector: ON")
            self.btn_inspector.setStyleSheet("background-color: #2196F3; color: white; font-weight: bold;")
            self.layout_widget.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.btn_inspector.setText("🔍 Enable Inspector")
            self.btn_inspector.setStyleSheet("")
            self.layout_widget.setCursor(Qt.CursorShape.ArrowCursor)

    def _format_delta_time(self, seconds):
        """Converts seconds into a human-readable string (e.g., 2h 46m 40s)."""
        abs_secs = abs(seconds)
        days, rem = divmod(abs_secs, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)

        parts = []
        if days > 0: parts.append(f"{int(days)}d")
        if hours > 0: parts.append(f"{int(hours)}h")
        if minutes > 0: parts.append(f"{int(minutes)}m")
        parts.append(f"{seconds:.1f}s")

        res = " ".join(parts)
        return f"-{res}" if seconds < 0 else res

    def mousePressEvent(self, event):
        if self.layout_widget.underMouse() and event.button() == Qt.MouseButton.LeftButton:
            self.layout_widget.setCursor(Qt.CursorShape.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if self.btn_inspector.isChecked():
            self.layout_widget.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.layout_widget.setCursor(Qt.CursorShape.ArrowCursor)
        super().mouseReleaseEvent(event)

    def _browse_export_path(self):
        current = self.edit_export_path.text()
        full_path, _ = QFileDialog.getSaveFileName(self, "Select Export Base Name", current)
        if full_path:
            self.edit_export_path.setText(full_path)

    PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..'))

    def _export_channels(self):
        """Selected channels in lane order, with registry unit/description."""
        sel = [t for t in self.plot_config if self.plot_config[t].get('selected') and t in self.cache.tags]
        sel.sort(key=lambda t: (self.plot_config[t].get('lane', 'Lane 1'), list(self.plot_config).index(t)))
        chans = []
        for t in sel:
            cfg = self.plot_config[t]
            reg = getattr(self, 'system_registry', {}).get(t, {})
            chans.append({"tag": t, "label": str(cfg.get('label', t)), "unit": reg.get("unit", ""),
                          "lane": cfg.get('lane', 'Lane 1'), "scale": cfg.get('scale', 'linear'),
                          "multiplier": float(cfg.get('multiplier', 1.0) or 1.0),
                          "description": reg.get("description", "")})
        return export_data._unique_names(chans)

    def _displayed_frame(self, t0, t1, tags):
        """What is drawn on screen in [t0, t1] as one table: historical rows up to
        where live coverage starts, then live rows on a merged time base (union of
        the channels' sample times, each held at its latest sample)."""
        import polars as pl
        live = getattr(self, 'current_live', None) or {}
        live_start = self.cache.live.oldest_time() if live else None
        x_parts, y_parts = [], {t: [] for t in tags}
        hist_x = getattr(self, 'historical_x', np.array([]))
        if getattr(self, 'is_historical_mode', False) and len(hist_x) > 0:
            hx = hist_x + self.t0
            h_end = t1 if live_start is None else min(t1, live_start)
            m = (hx >= t0) & (hx < h_end)
            if m.any():
                x_parts.append(hx[m])
                for t in tags:
                    y = self.historical_y.get(t)
                    y_parts[t].append(y[m] if y is not None and len(y) == len(hx) else np.full(int(m.sum()), np.nan))
        live_x = [x[(x >= t0) & (x <= t1)] for t, (x, _) in live.items() if t in y_parts]
        live_x = [x for x in live_x if len(x)]
        if live_x:
            base = np.unique(np.concatenate(live_x))
            x_parts.append(base)
            for t in tags:
                if t in live:
                    x, y = live[t]
                    k = np.searchsorted(x, base, side='right') - 1
                    y_parts[t].append(np.where(k >= 0, y[np.clip(k, 0, len(y) - 1)], np.nan))
                else:
                    y_parts[t].append(np.full(len(base), np.nan))
        if not x_parts:
            return pl.DataFrame({"timestamp": np.array([], dtype=np.float64), **{t: np.array([]) for t in tags}})
        return pl.DataFrame({"timestamp": np.concatenate(x_parts), **{t: np.concatenate(v) for t, v in y_parts.items()}})

    def _render_screen_svg(self):
        """Vector snapshot of the viewer exactly as shown (must run on the GUI thread)."""
        from PyQt6 import QtSvg
        from PyQt6.QtCore import QBuffer, QIODevice, QRectF
        prev_gl = pg.getConfigOption('useOpenGL')
        pg.setConfigOptions(useOpenGL=False)
        try:
            buffer = QBuffer()
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            export_rect = self.layout_widget.ci.sceneBoundingRect()
            generator = QtSvg.QSvgGenerator()
            generator.setOutputDevice(buffer)
            generator.setResolution(96)
            generator.setSize(export_rect.size().toSize())
            generator.setViewBox(export_rect)
            painter = pg.QtGui.QPainter(generator)
            painter.fillRect(export_rect, self.layout_widget.backgroundBrush())
            self.layout_widget.scene().render(painter, QRectF(0, 0, export_rect.width(), export_rect.height()),
                                              export_rect)
            painter.end()
            data = bytes(buffer.data())
            buffer.close()
            return data
        finally:
            # restore (the old code forced useOpenGL=True for every view created afterwards)
            pg.setConfigOptions(useOpenGL=prev_gl)

    def _prepare_export(self, options=None):
        """Export button: choose range / resolution / outputs, then export in the background.
        `options` (dict as returned by ExportDialog.options) skips the dialog."""
        if not isinstance(options, dict):
            options = None   # e.g. a stray `checked` bool from a signal
        if not self.cache.tags or not self.lanes:
            return
        channels = self._export_channels()
        if not channels:
            return
        if getattr(self, 'export_worker', None) is not None and self.export_worker.isRunning():
            return
        v0, v1 = list(self.lanes.values())[0].viewRange()[0]
        t0, t1 = v0 + self.t0, v1 + self.t0
        full_path = self.edit_export_path.text()
        if options is None:
            disp_rows = sum(len(x) for x, _ in (getattr(self, 'current_live', None) or {}).values()) \
                        + len(getattr(self, 'historical_x', []))
            dlg = ExportDialog(self, t0, t1, len(channels), export_data.logger_tick_seconds(self.PROJECT_ROOT),
                               disp_rows, os.path.dirname(full_path), os.path.basename(full_path))
            if not dlg.exec():
                return
            options = dlg.options()
            self.edit_export_path.setText(os.path.join(options["dest"], options["name"]))

        markers = []
        if options.get("markers", True):
            md = self._load_markers(options["t0"], options["t1"])
            markers = sorted(({"timestamp": float(k), "type": v.get("type", ""), "text": v.get("text", ""),
                               "color": v.get("color", "")} for k, v in md.items()), key=lambda m: m["timestamp"])
        displayed = self._displayed_frame(options["t0"], options["t1"], [c["tag"] for c in channels]) \
            if options["mode"] == "displayed" else None
        svg = self._render_screen_svg() if options.get("screen_svg") else None

        self.btn_export.setEnabled(False)
        self.btn_export.setText("⏳ Exporting...")
        self.export_worker = ExportWorker(self.cache.engine, self.cache.live, self.PROJECT_ROOT, options,
                                          channels, markers, displayed, svg)
        self.export_worker.progress.connect(lambda m: self.btn_export.setText(f"⏳ {m}"))
        self.export_worker.done.connect(self._on_export_finished)
        self.export_worker.failed.connect(self._on_export_error)
        self.export_worker.start()

    def _on_export_finished(self, folder):
        print(f"[Export] Complete - {folder}")
        self.last_export_folder = folder
        self.btn_export.setEnabled(True)
        self.btn_export.setText("✅ Exported")
        QTimer.singleShot(3000, lambda: self.btn_export.setText("📸 Export Snapshot"))
        if getattr(self, '_export_quiet', False):
            return
        from PyQt6.QtWidgets import QMessageBox
        from PyQt6.QtGui import QDesktopServices
        from PyQt6.QtCore import QUrl
        box = QMessageBox(QMessageBox.Icon.Information, "Export complete", f"Saved to:\n{folder}",
                          parent=self)
        open_btn = box.addButton("Open folder", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.setModal(False)
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(folder)))
        box.show()

    def _on_export_error(self, err):
        print(f"[Export] Error: {err}")
        self.btn_export.setEnabled(True)
        self.btn_export.setText("❌ Export failed")
        QTimer.singleShot(3000, lambda: self.btn_export.setText("📸 Export Snapshot"))
        if not getattr(self, '_export_quiet', False):
            from PyQt6.QtWidgets import QMessageBox
            box = QMessageBox(QMessageBox.Icon.Warning, "Export failed", str(err), parent=self)
            box.setModal(False)
            box.show()

    def _events_conn(self):
        """One persistent connection (needed for PRAGMA data_version change detection)."""
        db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../data/events.db'))
        if getattr(self, '_ev_conn', None) is None:
            if not os.path.exists(db_path):
                return None
            self._ev_conn = sqlite3.connect(db_path, timeout=0.5)
        return self._ev_conn

    def _events_db_version(self):
        """Changes whenever ANOTHER connection (the events service) commits."""
        conn = self._events_conn()
        if conn is None:
            return None
        try:
            return conn.execute("PRAGMA data_version").fetchone()[0]
        except sqlite3.Error:
            self._ev_conn = None
            return None

    def _active_event_types(self):
        return tuple(sorted(t for label, cb in self.filter_checkboxes.items() if cb.isChecked()
                            for t in self.event_filters[label]))

    def _load_markers(self, t_start=None, t_end=None):
        """Reads filtered events from the SQLite database, optionally only for a
        time window (uses the events table's timestamp index; the old query had
        no time bound and no usable index, so it scanned every event ever logged
        by every service, every second, on the GUI thread)."""

        # 1. Determine which event types are currently checked in the UI
        active_types = []
        for label, cb in self.filter_checkboxes.items():
            if cb.isChecked():
                active_types.extend(self.event_filters[label])

        markers = {}
        if not active_types:
            return markers

        # 2. Map UI pseudo-types back to real database types
        db_query_types = []
        for t in active_types:
            if t.startswith("FAULT"):
                if "FAULT" not in db_query_types:
                    db_query_types.append("FAULT")
            else:
                db_query_types.append(t)

        placeholders = ",".join("?" * len(db_query_types))
        query = f"SELECT timestamp, event_type, message, metadata FROM events WHERE event_type IN ({placeholders})"
        params = list(db_query_types)
        if t_start is not None:
            query += " AND timestamp BETWEEN ? AND ?"
            params += [t_start, t_end]

        conn = self._events_conn()
        if conn is None:
            return markers
        try:
            cursor = conn.execute(query, params)

            for ts, e_type, msg, meta in cursor.fetchall():
                key = f"{ts:.3f}"

                # 3. Dynamic Filtering & Color Assignment
                color = "#FFFFFF"

                if e_type == "FAULT":
                    # Extract the active state from the metadata payload
                    try:
                        is_active = json.loads(meta).get("is_active", True) if meta else True
                    except:
                        is_active = True

                    # Filter against UI checkboxes
                    if is_active and "FAULT_ACTIVE" not in active_types:
                        continue
                    if not is_active and "FAULT_CLEARED" not in active_types:
                        continue

                    # Red for Activated, Blue for Cleared
                    color = "#F44336" if is_active else "#2196F3"

                elif e_type == "USER_MARKER":
                    try:
                        color = json.loads(meta).get("color", "#00E5FF") if meta else "#00E5FF"
                    except:
                        color = "#00E5FF"

                elif "WATCHDOG" in e_type:
                    color = "#FF9800"  # Orange
                elif e_type == "PHASE_CHANGE":
                    color = "#4CAF50"  # Green

                markers[key] = {
                    "text": msg,
                    "color": color,
                    "type": e_type
                }
        except sqlite3.Error as e:
            print(f"[DB Error] Failed to read markers: {e}")
            self._ev_conn = None  # reconnect next time

        return markers

    def _save_markers(self, timestamp, data_dict):
        """Optimistic UI: Saves to RAM, forces checkbox ON, then publishes."""
        key = f"{timestamp:.3f}"
        self.event_data[key] = data_dict

        # 1. OPTIMISTIC UI: Force the checkbox to be visible
        user_cb = self.filter_checkboxes.get("User Markers")
        if user_cb and not user_cb.isChecked():
            # Block signals temporarily to prevent a double-refresh during the save
            user_cb.blockSignals(True)
            user_cb.setChecked(True)
            user_cb.blockSignals(False)

            # Explicitly refresh so the new marker renders immediately
            self._refresh_event_markers()

        # 2. Fire-and-Forget ZMQ Command to backend
        self.events.log_user_marker(marker_ts=timestamp, text=data_dict["text"], color=data_dict["color"])

    def _add_marker_to_graph(self, ts_str, text, color="#00E5FF"):
        if not self.lanes: return
        base_plot = list(self.lanes.values())[0]

        ts_float = float(ts_str)
        relative_x = ts_float - self.t0
        wrapped_text = textwrap.fill(text, width=25)

        # Stagger logic remains the same
        stagger_heights = [0.90, 0.75, 0.60, 0.45, 0.30]
        row_index = int(abs(hash(ts_str)) % len(stagger_heights))

        line = pg.InfiniteLine(
            pos=relative_x, angle=90, movable=False,
            pen=pg.mkPen(color, width=2, style=Qt.PenStyle.DashLine),
            label=wrapped_text,
            labelOpts={'position': stagger_heights[row_index], 'color': color, 'fill': (0, 0, 0, 200)}
        )

        # Store using the STRING as the key
        self.marker_items[ts_str] = line
        base_plot.addItem(line)

        # Connect click, but pass the string key
        line.sigClicked.connect(lambda obj, ev, k=ts_str: self._confirm_delete_marker(k))

    def _refresh_event_markers(self, *_, reload=True):
        """Synchronizes RAM markers with SQLite database.

        reload=False only re-places markers for the current view from the
        cached self.event_data (cheap). The SQLite read happens on the 1 s
        marker_sync_timer, filter changes and UI rebuilds, not on every
        render frame (it was previously hit ~11x per second).
        """
        if not self.lanes: return

        base_plot = list(self.lanes.values())[0]
        view_range = base_plot.viewRange()[0]

        # Pull fresh database state - only when something actually changed:
        #   * the DB was written by another connection (PRAGMA data_version),
        #   * the overlay filters changed, or
        #   * the view left the time window loaded last time.
        # Each load is a time-windowed indexed query (view +/- 2 spans).
        span = view_range[1] - view_range[0]
        a0, a1 = view_range[0] + self.t0, view_range[1] + self.t0
        types = self._active_event_types()
        win = getattr(self, '_ev_window', None)
        need = (win is None or a0 < win[0] or a1 > win[1] or types != getattr(self, '_ev_types', None))
        if reload and not need:
            version = self._events_db_version()
            need = version != getattr(self, '_ev_version', None)
        if need:
            w0, w1 = a0 - 2 * span, a1 + 2 * span
            self._ev_version = self._events_db_version()
            self.event_data = self._load_markers(w0, w1)
            self._ev_window, self._ev_types = (w0, w1), types

        # 1. Prune deleted or off-screen markers
        to_remove = []

        for ts_str, item in list(self.marker_items.items()):
            # If it was deleted by another window/service
            if ts_str not in self.event_data:
                if item.scene(): item.scene().removeItem(item)
                to_remove.append(ts_str)
                continue

            # If it's too far off-screen, unload from RAM
            rel_x = float(ts_str) - self.t0
            if rel_x < (view_range[0] - self.live_span) or rel_x > (view_range[1] + self.live_span):
                if item.scene(): item.scene().removeItem(item)
                to_remove.append(ts_str)

        for k in to_remove:
            del self.marker_items[k]

        # 2. Add new markers
        for ts_str, data in self.event_data.items():
            rel_x = float(ts_str) - self.t0
            if view_range[0] <= rel_x <= view_range[1]:
                if ts_str not in self.marker_items:
                    self._add_marker_to_graph(ts_str, data['text'], data['color'])

        # (A forced self.layout_widget.scene().update() used to sit here. It made Qt
        # repaint the whole scene 10x/s even when idle: ~45% CPU at 1080p. Adding or
        # removing items already schedules the repaint Qt needs.)

    def _confirm_delete_marker(self, ts_key):
        """Optimistic UI: Removes from screen instantly, then publishes delete command."""

        if ts_key in self.event_data:
            if self.event_data[ts_key].get("type") != "USER_MARKER":
                from PyQt6.QtWidgets import QMessageBox
                QMessageBox.warning(self, "Access Denied",
                                    "System events and faults are read-only and cannot be manually deleted.")
                return

        if hasattr(self, '_is_confirming_delete') and self._is_confirming_delete:
            return

        self._is_confirming_delete = True
        from PyQt6.QtWidgets import QMessageBox

        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Question)
        msg.setWindowTitle("Delete Marker")
        msg.setText("Delete this event marker?")
        msg.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)

        if msg.exec() == QMessageBox.StandardButton.Yes:
            # 1. Optimistic visual removal
            if ts_key in self.event_data:
                del self.event_data[ts_key]

            if ts_key in self.marker_items:
                item = self.marker_items[ts_key]
                if item.scene():
                    item.scene().removeItem(item)
                del self.marker_items[ts_key]

            # 2. Fire-and-Forget to service_events.py
            self.events.delete_user_marker(marker_ts=float(ts_key))

        self._is_confirming_delete = False

    def _show_profile_context_menu(self, pos):
        """Generates the right-click menu for items INSIDE the profile dropdown."""
        from PyQt6.QtWidgets import QMenu

        # Find exactly which item they right-clicked in the open list
        index = self.combo_profiles.view().indexAt(pos)
        if not index.isValid():
            return

        profile_name = self.combo_profiles.itemText(index.row())

        # Safeguard: Do not allow deleting the Default profile
        if not profile_name or profile_name == "Default":
            return

        menu = QMenu(self)
        delete_action = menu.addAction(f"Delete '{profile_name}'")

        # Map the position relative to the popup viewport
        global_pos = self.combo_profiles.view().viewport().mapToGlobal(pos)
        action = menu.exec(global_pos)

        if action == delete_action:
            self._delete_profile(profile_name)
            # Note: Keep the _delete_profile method exactly as I wrote it previously!

    def _delete_profile(self, profile_name):
        """Safely removes a profile from the disk and updates all UI."""
        from PyQt6.QtWidgets import QMessageBox
        import json
        import os

        # 1. Ask for confirmation
        reply = QMessageBox.question(
            self, 'Delete Profile',
            f"Are you sure you want to delete '{profile_name}'?\nThis will affect all open windows.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            # 2. Multi-Window Safe Disk Removal
            try:
                if os.path.exists(self.profiles_file):
                    with open(self.profiles_file, "r") as f:
                        disk_profiles = json.load(f)

                    if profile_name in disk_profiles:
                        del disk_profiles[profile_name]

                        with open(self.profiles_file, "w") as f:
                            json.dump(disk_profiles, f, indent=4)

                        # Update this window's local RAM
                        self.profiles = disk_profiles
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to delete profile from disk:\n{e}")
                return

            # 3. Update the UI Dropdown
            idx = self.combo_profiles.findText(profile_name)
            if idx != -1:
                self.combo_profiles.removeItem(idx)

            # 4. Snap the UI back to the Default profile safely
            self.combo_profiles.setCurrentText("Default")
            self._apply_profile()

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
    log_dir = os.path.join(project_root, "data")
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
    window_1 = DataViewerApp(master_cache, window_title="IPIDS Data Viewer - Window 1")
    #window_2 = DataViewerApp(master_cache, window_title="IPIDS Data Viewer - Window 2")

    window_1.show()
    #window_2.show()

    # 3. Execute Application Loop
    exit_code = app.exec()

    # 4. Graceful Teardown
    print("[Launcher] All windows closed. Terminating background threads...")
    master_cache.stop()
    sys.exit(exit_code)