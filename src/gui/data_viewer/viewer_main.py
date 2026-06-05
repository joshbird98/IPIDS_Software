import sys
import os
import csv
import time
import numpy as np
import copy
from datetime import datetime
import textwrap
import sqlite3
import json
import zmq
import orjson

from src.core.event_helper import EventHelper
from src.core.os_helper import harden_windows_process

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QVBoxLayout, QHBoxLayout,
    QWidget, QPushButton, QLabel, QDateTimeEdit,
    QFileDialog, QCheckBox
)
from PyQt6.QtCore import Qt, QDateTime, QTimer, QThread, pyqtSignal
import pyqtgraph as pg

from src.core.network_map import ZMQ_PORT_LOGGER_CMD
from src.gui.data_viewer.config_manager import load_registry
from src.gui.data_viewer.channel_selector import ChannelSelectorDialog
from src.core.data_cache import DualPipelineCache

from PyQt6.QtWidgets import QDialog, QFormLayout, QLineEdit, QComboBox, QDialogButtonBox
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

        # 2. Profiles & Config
        current_direc = os.path.dirname(os.path.abspath(__file__))
        self.profiles_file = os.path.join(os.path.dirname(current_direc), "config", "workspace_profiles.json")
        self.profiles = self._load_all_profiles()
        self.plot_config = copy.deepcopy(self.profiles.get("Default", {}))

        # 3. State
        self.curves = {}  # For Live Data
        self.hist_curves = {}  # For Stateless Historical Data

        # Connect the two pipelines
        self.cache.live_updated.connect(self._refresh_plot_live)
        self.cache.historical_updated.connect(self._refresh_plot_historical)

        self.historical_x = np.array([])
        self.historical_y = {}
        self.is_historical_mode = False

        self.auto_scroll = True
        self._auto_panning = False
        self.t0 = time.time()
        self.live_span = 300

        self._init_ui()

        # 4. Connect Signals
        self.cache.live_updated.connect(self._refresh_plot_live)
        self.cache.historical_updated.connect(self._refresh_plot_historical)

        self.historical_x = np.array([])
        self.historical_y = {}
        self.is_historical_mode = False

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

        # --- Sidebar ---
        sidebar = QWidget()
        sidebar.setFixedWidth(280)
        sidebar_layout = QVBoxLayout(sidebar)

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
        layout.addWidget(sidebar)

        pg.setConfigOptions(antialias=True, useOpenGL=True)

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
            # We disable X-AutoRange because our own Scroll Lock/Timespan logic handles X
            for vb in [p.vb, right_vb]:
                vb.setMouseEnabled(x=True, y=False)
                vb.setAutoVisible(y=True)  # Focus Y-scale only on visible data points
                vb.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)
                vb.disableAutoRange(axis=pg.ViewBox.XAxis)

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

    def _sync_specific_viewbox(self, main_plot, right_viewbox):
        """Forces geometry alignment between a specific lane and its log axis."""
        right_viewbox.setGeometry(main_plot.vb.sceneBoundingRect())
        right_viewbox.linkedViewChanged(main_plot.vb, right_viewbox.XAxis)

    # --- Core Rendering ---

    def _on_cache_updated(self):
        """Signals received from cache. Redraw handled by render_timer."""
        pass

    def _refresh_plot_live(self):
        now = time.perf_counter()
        if now - self.last_ui_update < self.ui_lockout: return
        self.last_ui_update = now

        if not self.cache.tags or not getattr(self, 'lanes', None): return
        for ta in getattr(self, 'time_axes', []): ta.set_offset(self.t0)

        # Grab the raw reference without allocating any new memory
        x_raw = self.cache.x_live_view
        if len(x_raw) == 0: return

        t_slice = time.perf_counter()

        # 1. DYNAMIC VIEWPORT SLICING (No Array Math Yet!)
        base_plot = list(self.lanes.values())[0]
        view_start, view_end = base_plot.viewRange()[0]
        span = view_end - view_start

        # Convert UI view bounds into absolute time to match the raw backend array
        abs_slice_start = (view_start - span) + self.t0
        abs_slice_end = (view_end + span) + self.t0

        # Search the raw O(1) view directly
        start_idx = np.searchsorted(x_raw, abs_slice_start)
        end_idx = np.searchsorted(x_raw, abs_slice_end, side='right')

        x_slice = x_raw[start_idx:end_idx] - self.t0

        y_slice_dict = {}
        for tag in self.cache.tags:
            y_arr = self.cache.y_live_view.get(tag)
            if y_arr is not None:
                # Slice Y-data using the same indices
                y_slice_dict[tag] = y_arr[start_idx:end_idx]

        MAX_LIVE_RENDER_POINTS = 10000
        if len(x_slice) > MAX_LIVE_RENDER_POINTS and len(y_slice_dict) > 0:
            from src.core.data_engine import TimeSeriesEngine  # Import your engine

            # Use the first tag to align the decimated time axis
            first_tag = list(y_slice_dict.keys())[0]
            x_decimated, _ = TimeSeriesEngine.downsample_minmax(
                x_slice, y_slice_dict[first_tag], target_points=MAX_LIVE_RENDER_POINTS
            )

            new_y_dict = {}
            for tag, y_arr in y_slice_dict.items():
                _, y_decimated = TimeSeriesEngine.downsample_minmax(
                    x_slice, y_arr, target_points=MAX_LIVE_RENDER_POINTS
                )
                new_y_dict[tag] = y_decimated

            # Replace the massive raw arrays with the protected decimated arrays
            x_slice = x_decimated
            y_slice_dict = new_y_dict

        PerfTracker.slice_size = len(x_slice)
        PerfTracker.log("slice", t_slice)

        # --- START TRACKING RENDER TIME ---
        t_render = time.perf_counter()

        self._render_curves(self.curves, x_slice, y_slice_dict, is_historical=False)

        PerfTracker.log("render", t_render)

        if self.is_historical_mode and len(self.historical_x) > 0:
            self._render_curves(self.hist_curves, self.historical_x, self.historical_y, is_historical=True)

        if self.auto_scroll:
            self._auto_panning = True
            current_width = view_end - view_start
            self.live_span = current_width

            # Use raw absolute time to find the newest edge
            latest_x_relative = x_raw[-1] - self.t0
            base_plot.setXRange(latest_x_relative - current_width, latest_x_relative, padding=0)
            self._auto_panning = False

    def _refresh_plot_historical(self, ts_array, vals_dict, req_id=None):
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

        for idx, tag in enumerate(sorted_tags):
            cfg = self.plot_config.get(tag, {})
            lane = cfg.get('lane', 'Lane 1')

            if not cfg.get('selected') or lane not in self.lanes:
                if tag in target_dict:
                    target_dict[tag].setVisible(False)
                continue

            if tag not in target_dict:
                color = self._get_distinct_color(idx)
                pen = pg.mkPen(color=color, width=1.0)
                c = pg.PlotDataItem(pen=pen, autoDownsample=True, clipToView=True, connect='finite')

                if cfg.get('scale') == 'log': self.lane_axes[lane].addItem(c)
                else: self.lanes[lane].addItem(c)

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

            min_len = min(len(x_array), len(y))
            x_plot = x_array[-min_len:]
            try:
                y_raw = np.asarray(y[-min_len:], dtype=np.float64)
                y_plot = y_raw * cfg.get('multiplier', 1.0)
            except: continue

            if cfg.get('scale') == 'log':
                active_axes[lane]['log'] = True
                with np.errstate(all='ignore'):
                    y_plot = np.log10(np.clip(y_plot, 1e-12, None))
            else:
                active_axes[lane]['linear'] = True

            target_dict[tag].setData(x_plot, y_plot)

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

        # 1. HARDWARE CHECK: Is the user actually holding a mouse button?
        # Wheel zooming happens with NoButton.
        # Dragging/Panning happens with Left or Middle button pressed.
        is_panning = QApplication.mouseButtons() != Qt.MouseButton.NoButton

        if self.auto_scroll and is_panning:
            # User is physically dragging the graph away from 'Now'
            self._toggle_scroll_lock(manual_break=True)

        # 2. If we are still in Auto-Scroll (meaning they just used the Scroll Wheel),
        # we update the width so the live edge doesn't "snap" back to 5 minutes.
        if self.auto_scroll:
            base_plot = list(self.lanes.values())[0]
            vr = base_plot.viewRange()[0]
            self.live_span = vr[1] - vr[0]

        self._check_and_fetch_history()
        self._refresh_plot_live()

    def request_ui_refresh(self):
        """
        The one true entry point for updating data after any UI change.
        Calls this after loading profiles, changing channels, or startup.
        """
        # 1. Clear existing items and rebuild layout
        self._build_lanes()

        # 2. Reset inspector and legends
        self._ensure_inspector_items()
        self._reset_legend_text()
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

    def _get_distinct_color(self, index):
        """Returns a high-contrast color based on the golden ratio."""
        hue = (index * 0.618033988749895) % 1.0
        color = pg.hsvColor(hue, 0.8, 1.0)
        return color

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

        # If we are live, snap to the very end of the cache
        if self.auto_scroll and len(self.cache.x_live_view) > 0:
            right_edge = self.cache.x_live_view[-1] - self.t0

        base_plot.setXRange(right_edge - span_seconds, right_edge, padding=0)
        self._check_and_fetch_history()  # History load happens under shield
        self._auto_panning = False

    def _check_and_fetch_history(self):
        if not self.lanes: return
        base_plot = list(self.lanes.values())[0]

        view_start, view_end = base_plot.viewRange()[0]
        start_ts = self.t0 + view_start
        end_ts = self.t0 + view_end

        oldest_live_ts = self.cache.x_live_view[0] if len(self.cache.x_live_view) > 0 else time.time() - (3600 * 12)
        # DEBUG: See what the UI is asking for
        print(f"[UI State] View Span: {start_ts:.1f} to {end_ts:.1f} (Duration: {(end_ts - start_ts) / 3600:.2f} hrs | Oldest Live: {oldest_live_ts:.1f}")

        # Are we looking at data older than the 1-hour live tail?
        oldest_live_ts = self.cache.x_live_view[0] if len(self.cache.x_live_view) > 0 else time.time() - (3600 * 12)

        if start_ts < oldest_live_ts:
            #print(f"[UI State] Historical Mode Triggered. Oldest Live: {oldest_live_ts:.1f}")
            self.is_historical_mode = True
            currently_selected = [tag for tag in self.cache.tags if self.plot_config.get(tag, {}).get('selected')]
            self.pending_hist_id = self.cache.request_historical_window(start_ts, end_ts, selected_tags=currently_selected)
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
        # Determine the physical X-coordinate where Live Data begins
        live_start_x = (self.cache.x_live_view[0] - self.t0) if len(self.cache.x_live_view) > 0 else float('inf')

        # If history is loaded AND mouse is to the left of the live boundary, read from Polars
        if self.is_historical_mode and mouse_x < live_start_x and len(self.historical_x) > 0:
            x_source = self.historical_x
            y_source = self.historical_y
            ts_source = self.historical_x + self.t0
        else:
            if len(self.cache.x_live_view) == 0: return
            x_source = self.cache.x_live_view - self.t0
            y_source = self.cache.y_live_view
            ts_source = self.cache.x_live_view

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

            # --- LEGEND UPDATES ---
            for tag, curve in self.curves.items():  # We update the base live curves legends
                cfg = self.plot_config.get(tag, {})
                lane = cfg.get('lane', 'Lane 1')

                # Pull data from the dynamically selected source
                y_data = y_source.get(tag, np.array([]))

                if len(y_data) <= idx or lane not in self.lane_legends:
                    continue

                val = y_data[idx]
                label_text = cfg.get('label', tag)
                fmt = ".2e" if cfg.get('scale') == 'log' else ".2f"

                unit = getattr(self, 'system_registry', {}).get(tag, {}).get("unit", "")
                unit_bracket = f" [{unit}]" if unit else ""
                display_name = f"{label_text}{unit_bracket}"

                legend_text = f"{display_name}: {val:{fmt}}"

                # 4. CROSS-DOMAIN DELTA MATH
                if active_pin is not None:
                    # Look up the exact value we cached when the pin was dropped
                    pin_val = getattr(self, 'pinned_values', {}).get(tag)
                    if pin_val is not None:
                        dy = val - pin_val
                        unit_suffix = f" {unit}" if unit else ""
                        legend_text += f" (Δ: {dy:{fmt}}{unit_suffix})"

                lbl_item = self.lane_legends[lane].getLabel(curve)
                if lbl_item:
                    lbl_item.setText(legend_text)

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

            # DYNAMIC PIPELINE ROUTER
            live_start_x = (self.cache.x_live_view[0] - self.t0) if len(self.cache.x_live_view) > 0 else float('inf')

            if self.is_historical_mode and mouse_x < live_start_x and len(self.historical_x) > 0:
                x_source = self.historical_x
                y_source = self.historical_y
                ts_source = self.historical_x + self.t0
            else:
                if len(self.cache.x_live_view) == 0: return
                x_source = self.cache.x_live_view - self.t0
                y_source = self.cache.y_live_view
                ts_source = self.cache.x_live_view

            idx = np.searchsorted(x_source, mouse_x, side='right') - 1
            idx = np.clip(idx, 0, len(x_source) - 1)

            # Store the absolute timestamp
            self.pin_timestamp = ts_source[idx]

            # CACHE THE EXACT Y-VALUES AT THE PIN LOCATION
            # This allows flawless delta math even if the mouse crosses into a different pipeline
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

        self._reset_legend_text()

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
            self._reset_legend_text()
            self.layout_widget.setCursor(Qt.CursorShape.ArrowCursor)

    def _reset_legend_text(self):
        """Restores legends to 'Label [Unit]' format when inspector is off."""
        for tag, curve in self.curves.items():
            cfg = self.plot_config.get(tag, {})
            lane = cfg.get('lane', 'Lane 1')

            if lane in self.lane_legends:
                lbl_item = self.lane_legends[lane].getLabel(curve)
                if lbl_item:
                    label_text = cfg.get('label', tag)
                    # Fetch unit from registry
                    unit = getattr(self, 'system_registry', {}).get(tag, {}).get("unit", "")
                    unit_str = f" [{unit}]" if unit else ""

                    lbl_item.setText(f"{label_text}{unit_str}")

        # Force layout update to prevent text clipping
        for legend in self.lane_legends.values():
            legend.layout.invalidate()
            legend.resize(0, 0)
            legend.updateSize()
            #legend.layout.activate()

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
        norm_x = self.cache.x_live_view - self.t0
        indices = np.where((norm_x >= view_start) & (norm_x <= view_end))[0]
        if len(indices) == 0: return

        x_slice = self.cache.x_live_view[indices]
        y_slices = {tag: self.cache.y_live_view[tag][indices] for tag in selected_tags}

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