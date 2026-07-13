import sys
import os
import csv
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

class ExportWorker(QThread):
    finished = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, csv_path, svg_path, selected_tags, tag_labels, x_slice, y_slices, svg_data):
        super().__init__()
        self.csv_path = csv_path
        self.svg_path = svg_path
        self.selected_tags = selected_tags
        self.tag_labels = tag_labels
        self.x_slice = x_slice
        self.y_slices = y_slices
        self.svg_data = svg_data # QByteArray from the main thread

    def run(self):
        try:
            # 1. Save Vector SVG (Lossless)
            with open(self.svg_path, 'wb') as f:
                f.write(self.svg_data)

            # 2. Build CSV
            with open(self.csv_path, 'w', newline='') as f:
                writer = csv.writer(f)
                header = ["Timestamp_UNIX", "Human_Time"] + self.tag_labels
                writer.writerow(header)

                for i in range(len(self.x_slice)):
                    ts = self.x_slice[i]
                    h_time = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S.%f')
                    row = [ts, h_time]
                    for tag in self.selected_tags:
                        row.append(self.y_slices[tag][i])
                    writer.writerow(row)

            self.finished.emit("Lossless Export Complete")
        except Exception as e:
            self.error.emit(str(e))

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
        self.render_timer.start(100)

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
        self.btn_scroll_lock.clicked.connect(self._toggle_scroll_lock)
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
        current_direc = os.path.dirname(os.path.abspath(__file__))
        default_path = os.path.join(os.path.dirname(current_direc), "exports")
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
        self.btn_export.clicked.connect(self._prepare_export)
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

        # State dictionaries for the multi-lane system
        self.lanes = {}  # holds pg.PlotItem
        self.lane_axes = {}  # holds Right/Log ViewBoxes per lane
        self.lane_legends = {}  # holds pg.LegendItem per lane

        # Lists for multi-lane inspector elements
        self.v_lines = []  # Vertical crosshairs
        self.pin_lines = []  # Ctrl+Click pins
        self.delta_labels = []  # Time delta text

        self.marker_items = {}
        self.event_data = self._load_markers()

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
            p = self.layout_widget.addPlot(row=i, col=0, axisItems={'bottom': time_axis})
            p.showGrid(x=True, y=True, alpha=0.5)
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
            log_axis = LogAxisItem(orientation='right')
            p.layout.addItem(log_axis, 2, 2)
            log_axis.linkToView(right_vb)
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
        self._check_and_fetch_history()
        for ta in getattr(self, 'time_axes', []): ta.set_offset(self.t0)

        # --- THE VIRTUAL WINDOW SLICER ---
        if self.cache.live_ptr == 0: return
        t_slice = time.perf_counter()

        x_raw = self.cache._x_live
        cap = self.cache.live_capacity
        ptr = self.cache.live_ptr % cap
        wrapped = getattr(self.cache, 'buffer_wrapped', False)

        base_plot = list(self.lanes.values())[0]
        view_start, view_end = base_plot.viewRange()[0]
        span = view_end - view_start

        abs_slice_start = (view_start - span) + self.t0
        abs_slice_end = (view_end + span) + self.t0

        if not wrapped:
            # Simple slice if we haven't hit the 5-million point wrap yet
            x_valid = x_raw[:ptr]
            start_idx = np.searchsorted(x_valid, abs_slice_start)
            end_idx = np.searchsorted(x_valid, abs_slice_end, side='right')

            x_slice = x_valid[start_idx:end_idx] - self.t0
            y_slice_dict = {tag: self.cache._y_live[tag][start_idx:end_idx]
                            for tag in self.cache.tags
                            if tag in self.cache._y_live}
        else:
            # Dual-Slice: Grab views of the Old Segment and New Segment independently
            x_old = x_raw[ptr:]
            x_new = x_raw[:ptr]

            s_idx_old = np.searchsorted(x_old, abs_slice_start)
            e_idx_old = np.searchsorted(x_old, abs_slice_end, side='right')

            s_idx_new = np.searchsorted(x_new, abs_slice_start)
            e_idx_new = np.searchsorted(x_new, abs_slice_end, side='right')

            # Concat ONLY the tiny visible slices (< 0.1ms execution time)
            x_slice = np.concatenate((x_old[s_idx_old:e_idx_old], x_new[s_idx_new:e_idx_new])) - self.t0

            y_slice_dict = {}
            for tag in self.cache.tags:
                y_raw = self.cache._y_live[tag]
                y_slice_old = y_raw[ptr:][s_idx_old:e_idx_old]
                y_slice_new = y_raw[:ptr][s_idx_new:e_idx_new]
                y_slice_dict[tag] = np.concatenate((y_slice_old, y_slice_new))

        # Cache the current render payload for the Mouse Inspector to use
        self.current_x_slice = x_slice
        self.current_y_dict = y_slice_dict

        PerfTracker.slice_size = len(x_slice)
        PerfTracker.log("slice", t_slice)

        t_render = time.perf_counter()
        self._render_curves(self.curves, x_slice, y_slice_dict, is_historical=False)
        PerfTracker.log("render", t_render)

        if self.is_historical_mode and len(self.historical_x) > 0:
            self._render_curves(self.hist_curves, self.historical_x, self.historical_y, is_historical=True)

        if self.auto_scroll:
            self._auto_panning = True
            current_width = view_end - view_start
            self.live_span = current_width

            # Extract the newest relative timestamp efficiently
            latest_idx = (ptr - 1) % cap if self.cache.live_ptr > 0 else 0
            latest_x_relative = self.cache._x_live[latest_idx] - self.t0

            base_plot.setXRange(latest_x_relative - current_width, latest_x_relative, padding=0)
            self._auto_panning = False

        # --- UPDATE LIVE SIDEBAR READOUT ---
        if not self.btn_inspector.isChecked():
            if self.cache.live_ptr > 0:
                latest_idx = (self.cache.live_ptr - 1) % self.cache.live_capacity

                for tag in self.cache.tags:
                    # Use .get() to avoid KeyErrors if the tag isn't in the sidebar dictionary yet
                    val_lbl = getattr(self, 'sidebar_value_labels', {}).get(tag)

                    if val_lbl is not None:
                        cfg = self.plot_config.get(tag, {})
                        val = self.cache._y_live[tag][latest_idx]
                        mult = cfg.get('multiplier', 1.0)
                        val = val * mult if mult != 1.0 else val

                        # --- Fetch unit and apply dynamic formatting ---
                        base_unit = getattr(self, 'system_registry', {}).get(tag, {}).get("unit", "")
                        formatted_str = self._format_with_si_prefix(val, base_unit)

                        val_lbl.setText(formatted_str)

    def _refresh_plot_historical(self, ts_array, vals_dict, req_id=None):
        #print(f"[DEBUG-UI] Received history for ID {req_id}. Expected ID: {self.pending_hist_id}. Points: {len(ts_array)}") # ADD THIS
        if req_id is not None and req_id != self.pending_hist_id:
            return
        # if not self.is_historical_mode: return  ### not used right now?
        #t_start = time.perf_counter()
        self.historical_x = ts_array - self.t0
        self.historical_y = vals_dict
        #t_prep = time.perf_counter()
        self._render_curves(self.hist_curves, self.historical_x, self.historical_y, is_historical=True)

        #t_end = time.perf_counter()
        #print(f"[UI] Historical Render Complete: {(t_end - t_prep) * 1000:.1f} ms (Total UI block: {(t_end - t_start) * 1000:.1f} ms)")

    def _render_curves(self, target_dict, x_array, y_dict, is_historical):
        sorted_tags = sorted(self.cache.tags)
        active_axes = {lane: {'linear': False, 'log': False} for lane in self.lanes}

        # =========================================================
        # --- 1. GLOBAL X-AXIS EXTRACTION (Run ONCE per frame) ---
        # =========================================================
        RENDER_LIMIT = 10000
        needs_downsample = False
        master_slice_start = 0
        master_slice_end = 0
        use_concat = False
        start_idx = 0
        end_idx = 0

        if not is_historical and self.cache.live_ptr > 0:
            ptr = self.cache.live_ptr
            cap = self.cache.live_capacity

            N_total = min(ptr, cap)
            start_idx = (ptr - N_total) % cap
            end_idx = ptr % cap

            # Reconstruct chronological X array
            if start_idx < end_idx:
                x_chron = x_array[start_idx:end_idx]
            else:
                x_chron = np.concatenate((x_array[start_idx:], x_array[:end_idx]))
                use_concat = True

            # Viewport filtering (Binary Search)
            try:
                view_range = list(self.lanes.values())[0].viewRange()[0]
                span = view_range[1] - view_range[0]

                # Add heavy padding (25%) so auto-scrolling new data NEVER clips
                v_min = view_range[0] - (span * 0.25)
                v_max = view_range[1] + (span * 0.25)

                master_slice_start = np.searchsorted(x_chron, v_min, side='left')
                master_slice_end = np.searchsorted(x_chron, v_max, side='right')

                # Failsafe: If the slice is empty, abort the filter and show all live data
                if master_slice_start >= master_slice_end:
                    master_slice_start, master_slice_end = 0, len(x_chron)

                x_raw_master = x_chron[master_slice_start:master_slice_end]

            except Exception:
                x_raw_master = x_chron
                master_slice_start = 0
                master_slice_end = len(x_chron)

        else:
            x_raw_master = x_array
            master_slice_start = 0
            master_slice_end = len(x_array)

        # Decide if we need to downsample this frame
        if len(x_raw_master) > RENDER_LIMIT:
            needs_downsample = True
            # Downsample X once, globally!
            x_plot_master, _ = self.cache.engine.downsample_minmax(x_raw_master, x_raw_master,
                                                                   target_points=RENDER_LIMIT)
        else:
            x_plot_master = x_raw_master

        # =========================================================
        # --- 2. CHANNEL LOOP (Now ultra-lightweight) ---
        # =========================================================
        for idx, tag in enumerate(sorted_tags):
            cfg = self.plot_config.get(tag, {})
            lane = cfg.get('lane', 'Lane 1')

            if not cfg.get('selected') or lane not in self.lanes:
                if tag in target_dict:
                    target_dict[tag].setVisible(False)
                continue

            if tag not in target_dict:
                color = self._get_distinct_color(tag)
                pen = pg.mkPen(color=color, width=1.0)
                c = pg.PlotDataItem(pen=pen, autoDownsample=True, clipToView=True, connect='finite',
                                    downsampleMethod='peak')

                if cfg.get('scale') == 'log':
                    self.lane_axes[lane].addItem(c)
                else:
                    self.lanes[lane].addItem(c)

                if not is_historical:
                    label_text = cfg.get('label', tag)
                    unit = getattr(self, 'system_registry', {}).get(tag, {}).get("unit", "")
                    unit_str = f" [{unit}]" if unit else ""
                    self.lane_legends[lane].addItem(c, name=f"{label_text}{unit_str}")
                target_dict[tag] = c

            target_dict[tag].setVisible(True)

            y = y_dict.get(tag, np.array([]))
            if len(y) == 0:
                target_dict[tag].setData([], [])
                continue

            # Extract Y using the globally calculated indices
            if not is_historical and self.cache.live_ptr > 0:
                if not use_concat:
                    y_chron = y[start_idx:end_idx]
                else:
                    y_chron = np.concatenate((y[start_idx:], y[:end_idx]))

                y_raw = y_chron[master_slice_start:master_slice_end]
            else:
                y_raw = y

            # Run min/max downsample ONLY if the view is zoomed out
            if needs_downsample:
                try:
                    _, y_plot_pre = self.cache.engine.downsample_minmax(x_raw_master, y_raw, target_points=RENDER_LIMIT)
                except Exception:
                    # Catch NaN array crashes
                    y_plot_pre = y_raw
            else:
                y_plot_pre = y_raw

            # Apply multipliers and log scaling
            try:
                y_plot_pre = np.asarray(y_plot_pre, dtype=np.float64)
                mult = cfg.get('multiplier', 1.0)
                y_plot = y_plot_pre if mult == 1.0 else y_plot_pre * mult
            except Exception:
                continue

            if cfg.get('scale') == 'log':
                active_axes[lane]['log'] = True
                with np.errstate(all='ignore'):
                    y_plot = np.log10(np.clip(y_plot, 1e-12, None))
            else:
                active_axes[lane]['linear'] = True

            target_dict[tag].setData(x_plot_master, y_plot)

        # UI Cleanup
        for lane, states in active_axes.items():
            left_axis = self.lanes[lane].getAxis('left')
            left_axis.show()
            show_log = states['log']
            self.lane_axes[lane].setVisible(show_log)
            if lane in self.log_axis_widgets: self.log_axis_widgets[lane].setVisible(show_log)
            left_axis.setStyle(showValues=states['linear'])

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

        # If we are live, snap to the very end of the cache using the ring buffer pointer
        if self.auto_scroll and self.cache.live_ptr > 0:
            latest_idx = (self.cache.live_ptr - 1) % self.cache.live_capacity
            right_edge = self.cache._x_live[latest_idx] - self.t0

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

        if self.cache.live_ptr == 0:
            oldest_live_ts = time.time()
        elif getattr(self.cache, 'buffer_wrapped', False):
            ptr = self.cache.live_ptr % self.cache.live_capacity
            oldest_live_ts = self.cache._x_live[ptr]
        else:
            oldest_live_ts = self.cache._x_live[0]

        if start_ts < oldest_live_ts:
            self.is_historical_mode = True

            # --- HYSTERESIS THRESHOLD ---
            # Prevent Polars death-loop by requiring a minimum view shift
            if not hasattr(self, 'last_hist_start'):
                self.last_hist_start = 0.0
                self.last_hist_end = 0.0

            # Require a 2% shift in the view window before querying disk again
            shift_threshold = span * 0.02

            if abs(start_ts - self.last_hist_start) > shift_threshold or abs(
                    end_ts - self.last_hist_end) > shift_threshold:
                self.last_hist_start = start_ts
                self.last_hist_end = end_ts
                currently_selected = [tag for tag in self.cache.tags if self.plot_config.get(tag, {}).get('selected')]
                self.pending_hist_id = self.cache.request_historical_window(start_ts, end_ts,
                                                                            selected_tags=currently_selected)
        else:
            self.is_historical_mode = False
            self._clear_historical_curves()

        self._refresh_event_markers()

    def _clear_historical_curves(self):
        for c in self.hist_curves.values():
            c.setData([], [])

    def _on_mouse_moved(self, pos):
        if not self.btn_inspector.isChecked():
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

        if not self.lanes: return
        ref_plot = hovered_plot if hovered_plot else list(self.lanes.values())[0]

        mouse_point = ref_plot.vb.mapSceneToView(pos)
        mouse_x = mouse_point.x()

        # 2. DYNAMIC PIPELINE ROUTER
        if self.cache.live_ptr == 0: return

        cap = self.cache.live_capacity
        ptr = self.cache.live_ptr % cap
        wrapped = getattr(self.cache, 'buffer_wrapped', False)

        live_start_abs = self.cache._x_live[ptr] if wrapped else self.cache._x_live[0]
        live_start_x = live_start_abs - self.t0

        if self.is_historical_mode and mouse_x < live_start_x and len(self.historical_x) > 0:
            x_source = self.historical_x
            y_source = self.historical_y
            ts_source = self.historical_x + self.t0
        else:
            if getattr(self, 'current_x_slice', None) is None or len(self.current_x_slice) == 0: return
            x_source = self.current_x_slice
            y_source = self.current_y_dict
            ts_source = self.current_x_slice + self.t0

        # 3. SNAP TO CLOSEST DATA POINT
        idx = np.searchsorted(x_source, mouse_x, side='right') - 1
        idx = np.clip(idx, 0, len(x_source) - 1)
        actual_x = x_source[idx]

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
            actual_ts = ts_source[idx]
            dt = actual_ts - active_pin
            delta_msg = f"Δt: {self._format_delta_time(dt)}"

        for i, p in enumerate(self.lanes.values()):
            label = self.delta_labels[i]
            if p == hovered_plot and delta_msg != "":
                label.setAnchor((anchor_x, anchor_y))
                label.setPos(actual_x, mouse_point.y())
                label.setText(delta_msg)
                label.show()
            else:
                label.hide()

            # --- SIDEBAR UPDATES ---
            for tag, curve in self.curves.items():
                cfg = self.plot_config.get(tag, {})
                y_data = y_source.get(tag, np.array([]))

                if len(y_data) <= idx:
                    continue

                val = y_data[idx]

                # 1. Extract the unit early
                base_unit = getattr(self, 'system_registry', {}).get(tag, {}).get("unit", "")

                # 2. Format the main absolute value
                display_text = self._format_with_si_prefix(val, base_unit)

                # CROSS-DOMAIN DELTA MATH
                if active_pin is not None:
                    pin_val = getattr(self, 'pinned_values', {}).get(tag)
                    if pin_val is not None:
                        dy = val - pin_val

                        # 3. Format the delta difference
                        formatted_dy = self._format_with_si_prefix(dy, base_unit , is_delta=True)

                        # Append the scaled and formatted delta to the main display text
                        display_text = f" (\u0394: {formatted_dy})"

                if tag in getattr(self, 'sidebar_value_labels', {}):
                    self.sidebar_value_labels[tag].setText(display_text)

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

            mouse_x = mouse_point.x()

            # --- VIRTUAL PIPELINE ROUTER ---
            if self.cache.live_ptr == 0: return

            cap = self.cache.live_capacity
            ptr = self.cache.live_ptr % cap
            wrapped = getattr(self.cache, 'buffer_wrapped', False)

            live_start_abs = self.cache._x_live[ptr] if wrapped else self.cache._x_live[0]
            live_start_x = live_start_abs - self.t0

            if self.is_historical_mode and mouse_x < live_start_x and len(self.historical_x) > 0:
                x_source = self.historical_x
                y_source = self.historical_y
                ts_source = self.historical_x + self.t0
            else:
                if getattr(self, 'current_x_slice', None) is None or len(self.current_x_slice) == 0: return
                x_source = self.current_x_slice
                y_source = self.current_y_dict
                ts_source = self.current_x_slice + self.t0

            idx = np.searchsorted(x_source, mouse_x, side='right') - 1
            idx = np.clip(idx, 0, len(x_source) - 1)

            # Store the absolute timestamp
            self.pin_timestamp = ts_source[idx]

            # CACHE THE EXACT Y-VALUES AT THE PIN LOCATION
            self.pinned_values = {}
            for tag in self.cache.tags:
                y_data = y_source.get(tag, np.array([]))
                self.pinned_values[tag] = y_data[idx] if len(y_data) > idx else None

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

        has_pin = self.pin_idx is not None
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

    def _prepare_export(self):
        if not self.cache.tags or not self.lanes: return

        selected_tags = [t for t in self.cache.tags if self.plot_config.get(t, {}).get('selected')]
        tag_labels = [self.plot_config[t].get('label', t) for t in selected_tags]
        if not selected_tags: return

        full_path = self.edit_export_path.text()
        base_dir = os.path.dirname(full_path)
        base_name = os.path.basename(full_path)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = os.path.join(base_dir, f"{base_name}_{stamp}.csv")
        svg_path = os.path.join(base_dir, f"{base_name}_{stamp}.svg")

        # Get range from top lane
        base_plot = list(self.lanes.values())[0]
        view_start, view_end = base_plot.viewRange()[0]

        # Retrieve the exact data currently rendered on the screen
        if getattr(self, 'current_x_slice', None) is None or len(self.current_x_slice) == 0: return

        # Restore absolute UNIX timestamp for the CSV
        x_slice = self.current_x_slice + self.t0
        y_slices = {tag: self.current_y_dict[tag] for tag in selected_tags}

        from PyQt6 import QtSvg
        from PyQt6.QtCore import QBuffer, QIODevice, QRectF
        pg.setConfigOptions(useOpenGL=False)

        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)

        # Export the entire layout grid
        export_rect = self.layout_widget.ci.sceneBoundingRect()

        generator = QtSvg.QSvgGenerator()
        generator.setOutputDevice(buffer)
        generator.setResolution(96)
        generator.setSize(export_rect.size().toSize())
        generator.setViewBox(export_rect)

        painter = pg.QtGui.QPainter(generator)
        painter.fillRect(export_rect, self.layout_widget.backgroundBrush())
        target_rect = QRectF(0, 0, export_rect.width(), export_rect.height())
        self.layout_widget.scene().render(painter, target_rect, export_rect)

        painter.end()
        svg_data = buffer.data()
        buffer.close()

        pg.setConfigOptions(useOpenGL=True)

        self.btn_export.setEnabled(False)
        self.btn_export.setText("⏳ Saving Lossless...")
        self.worker = ExportWorker(csv_path, svg_path, selected_tags, tag_labels, x_slice, y_slices, svg_data)
        self.worker.finished.connect(self._on_export_finished)
        self.worker.start()

    def _on_export_finished(self, msg):
        print(f"[Export] {msg}")
        self.btn_export.setEnabled(True)
        self.btn_export.setText("✅ Success!")
        QTimer.singleShot(2000, lambda: self.btn_export.setText("📸 Export Snapshot"))

    def _on_export_error(self, err):
        print(f"[Export] Error: {err}")
        self.btn_export.setEnabled(True)
        self.btn_export.setText("❌ Failed")
        QTimer.singleShot(2000, lambda: self.btn_export.setText("📸 Export Snapshot"))


    def _load_markers(self):
        """Reads filtered events directly from the SQLite database."""
        db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../data/events.db'))

        # 1. Determine which event types are currently checked in the UI
        active_types = []
        for label, cb in self.filter_checkboxes.items():
            if cb.isChecked():
                active_types.extend(self.event_filters[label])

        markers = {}
        if not active_types or not os.path.exists(db_path):
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

        try:
            with sqlite3.connect(db_path) as conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA synchronous=NORMAL;")
                cursor = conn.cursor()
                cursor.execute(query, db_query_types)

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

    def _refresh_event_markers(self):
        """Synchronizes RAM markers with SQLite database."""
        if not self.lanes: return

        base_plot = list(self.lanes.values())[0]

        # Pull fresh database state
        self.event_data = self._load_markers()

        # 1. Prune deleted or off-screen markers
        view_range = base_plot.viewRange()[0]
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

        self.layout_widget.scene().update()

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


if __name__ == "__main__":

    harden_windows_process()
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