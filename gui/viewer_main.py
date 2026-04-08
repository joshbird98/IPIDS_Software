import sys
import os
import csv
import time
import numpy as np
import copy
from datetime import datetime
import textwrap

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QVBoxLayout, QHBoxLayout,
    QWidget, QPushButton, QSplitter, QLabel, QDateTimeEdit,
    QComboBox, QInputDialog, QFileDialog, QLineEdit
)
from PyQt6.QtCore import Qt, QDateTime, QTimer, QThread, pyqtSignal
import pyqtgraph as pg

from utils.config_manager_2 import load_config, load_registry
from utils.channel_selector_2 import ChannelSelectorDialog
from utils.data_cache import InfiniteDataCache

from PyQt6.QtWidgets import QDialog, QFormLayout, QLineEdit, QComboBox, QDialogButtonBox
from PyQt6.QtGui import QIcon, QPixmap, QColor
from PyQt6.QtCore import QSize

class MarkerDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Event Marker")
        self.setMinimumWidth(300)
        layout = QFormLayout(self)

        self.text_input = QLineEdit()
        self.color_input = QComboBox()
        # High-visibility palette
        self.colors = {
            "Cyan": "#00E5FF",
            "Yellow": "#FFEA00",
            "Magenta": "#FF4081",
            "Orange": "#FF5722",
            "Green": "#00E676",
            "White": "#FFFFFF",
            "Red": "#F44336"
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
    def __init__(self):
        super().__init__()
        self.setWindowTitle("IPIDS Infinite Historian")
        self.resize(1200, 800)
        self.showMaximized()

        # 1. Data Layer
        current_dir = os.path.dirname(os.path.abspath(__file__))
        log_dir = os.path.join(os.path.dirname(current_dir), "LogData")
        self.cache = InfiniteDataCache(log_dir)
        self.system_registry = load_registry()

        # 2. Profiles & Config
        self.profiles_file = os.path.join(os.path.dirname(current_dir), "config", "workspace_profiles.json")
        self.profiles = self._load_all_profiles()
        self.plot_config = copy.deepcopy(self.profiles.get("Default", {}))

        # 3. State
        self.curves = {}
        self.auto_scroll = True
        self._auto_panning = False
        self.t0 = time.time()
        self.live_span = 300

        self._init_ui()

        # 4. Connect Signals & Start
        self.cache.data_updated.connect(self._on_cache_updated)
        self.cache.set_active_tags(list(self.plot_config.keys()))
        self.cache.start()

        now = time.time()
        self.cache.request_history(now - 300, now, stride=1) # Loads a little snippet of data

        QTimer.singleShot(3000, self._silent_preload) # Prelods the last 24 hours silently

        # 5. Fixed-rate Render Timer (30 FPS)
        self.render_timer = QTimer()
        self.render_timer.timeout.connect(self._refresh_plot)
        self.render_timer.start(33)

        self.pin_idx = None  # Stores the index of the reference point
        self.pin_x = None  # Stores the absolute timestamp of the pin

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

        sidebar_layout.addWidget(QLabel("<b>Profiles:</b>"))
        self.combo_profiles = QComboBox()
        self.combo_profiles.addItems(self.profiles.keys())
        sidebar_layout.addWidget(self.combo_profiles)

        btn_load = QPushButton("Load Profile")
        btn_load.clicked.connect(self._apply_profile)
        sidebar_layout.addWidget(btn_load)

        btn_save_profile = QPushButton("💾 Save Profile As...")
        btn_save_profile.clicked.connect(self._save_current_profile)
        sidebar_layout.addWidget(btn_save_profile)

        self.btn_config = QPushButton("⚙️ Configure Channels")
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
        for label, minutes in [("5m", 5), ("15m", 15), ("1h", 60), ("24h", 1440)]:
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

        self.stats_label = QLabel("Stats: (Enable Inspector)")
        self.stats_label.setStyleSheet("font-family: monospace; font-size: 10pt; color: #222; font-weight: 500;")
        self.stats_label.setWordWrap(True)
        sidebar_layout.addWidget(self.stats_label)

        sidebar_layout.addSpacing(20)
        sidebar_layout.addWidget(QLabel("<b>Export Settings:</b>"))

        # Path selection layout
        path_layout = QHBoxLayout()
        self.edit_export_path = QLineEdit()
        # Default to root/PlotData
        current_dir = os.path.dirname(os.path.abspath(__file__))
        default_path = os.path.join(os.path.dirname(current_dir), "PlotData")
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

        self._markers_visible = True
        self.btn_toggle_markers = QPushButton("👁 Markers: ON")
        self.btn_toggle_markers.setCheckable(True)
        self.btn_toggle_markers.clicked.connect(self._toggle_marker_visibility)
        sidebar_layout.addWidget(self.btn_toggle_markers)

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

        # Connect global mouse events to the entire layout scene
        self.layout_widget.scene().sigMouseMoved.connect(self._on_mouse_moved)
        self.layout_widget.scene().sigMouseClicked.connect(self._on_graph_clicked)
        self.layout_widget.scene().sigMouseClicked.connect(self._handle_click_events)

        # Build the initial grid
        self._build_lanes()

    def _build_lanes(self):
        """Reconstructs the plotting grid, preserving view state and preventing memory leaks."""
        current_range = None
        if self.lanes:
            current_range = list(self.lanes.values())[0].viewRange()[0]

        # DEEP CLEANUP
        for vb in self.lane_axes.values():
            if vb.scene(): vb.scene().removeItem(vb)
        for c in self.curves.values():
            if c.scene(): c.scene().removeItem(c)
        for m in self.marker_items.values():
            if m.scene(): m.scene().removeItem(m)

        self.layout_widget.clear()
        self.layout_widget.ci.layout.setSpacing(35)  # Increased gap between lanes

        self.lanes.clear()
        self.lane_axes.clear()
        self.lane_legends.clear()
        self.v_lines.clear()
        self.pin_lines.clear()
        self.delta_labels.clear()
        self.curves.clear()
        self.marker_items.clear()
        self.time_axes = []

        active_lanes = set()
        for tag, cfg in self.plot_config.items():
            if cfg.get('selected'): active_lanes.add(cfg.get('lane', 'Lane 1'))

        sorted_lanes = sorted(list(active_lanes))
        if not sorted_lanes: return

        base_plot = None

        for i, lane_name in enumerate(sorted_lanes):
            # 1. Create the Custom Axis FIRST
            time_axis = TimeAxisItem(orientation='bottom')
            time_axis.setHeight(45)
            if i < len(sorted_lanes) - 1:
                time_axis.hide_text = True
            self.time_axes.append(time_axis)

            # 2. Pass it INTO the plot during creation
            p = self.layout_widget.addPlot(row=i, col=0, axisItems={'bottom': time_axis})

            # 3. NOW turn on the grid (it will apply to our custom axis)
            p.showGrid(x=True, y=True, alpha=0.5)

            p.setMenuEnabled(False)
            p.vb.setMenuEnabled(False)
            p.hideButtons()
            p.vb.setMouseEnabled(x=True, y=False)
            p.vb.disableAutoRange(axis=pg.ViewBox.XAxis)

                # --- Linking & View Restoration ---
            if base_plot is None:
                base_plot = p
                base_plot.sigRangeChanged.connect(self._handle_view_change)
                self._auto_panning = True
                if current_range:
                    base_plot.setXRange(current_range[0], current_range[1], padding=0)
                else:
                    base_plot.setXRange(-300, 0, padding=0)
                self._auto_panning = False
            else:
                p.setXLink(base_plot)

            legend = p.addLegend(offset=(10, 10))
            legend.setBrush(pg.mkBrush(0, 0, 0, 150))
            self.lane_legends[lane_name] = legend

            log_axis = LogAxisItem(orientation='right')
            p.layout.addItem(log_axis, 2, 2)

            right_vb = pg.ViewBox()
            right_vb.setMenuEnabled(False)
            right_vb.disableAutoRange(axis=pg.ViewBox.XAxis)  # FIX 1 (Log Axis)
            right_vb.setMouseEnabled(x=True, y=False)

            p.scene().addItem(right_vb)
            log_axis.linkToView(right_vb)
            right_vb.setXLink(p)

            p.vb.sigResized.connect(lambda _, vb=right_vb, plot=p: self._sync_specific_viewbox(plot, vb))

            v_line = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen('y', width=1, style=Qt.PenStyle.DashLine))
            v_line.hide()
            p.addItem(v_line, ignoreBounds=True)
            self.v_lines.append(v_line)

            pin_line = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen('#FF5722', width=1.5))
            pin_line.hide()
            p.addItem(pin_line, ignoreBounds=True)
            self.pin_lines.append(pin_line)

            delta_label = pg.TextItem(anchor=(0, 0), color='#FF5722', fill=(0, 0, 0, 150))
            delta_label.hide()
            p.addItem(delta_label, ignoreBounds=True)
            self.delta_labels.append(delta_label)

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

    def _refresh_plot(self):
        if not self.cache.tags or not self.lanes: return

        for ta in self.time_axes:
            ta.set_offset(self.t0)

        x = self.cache.x_time - self.t0
        sorted_tags = sorted(self.cache.tags)
        active_axes = {lane: {'linear': False, 'log': False} for lane in self.lanes}

        for idx, tag in enumerate(sorted_tags):
            cfg = self.plot_config.get(tag, {})
            lane = cfg.get('lane', 'Lane 1')

            if not cfg.get('selected') or lane not in self.lanes:
                if tag in self.curves:
                    c = self.curves.pop(tag)
                    scene = c.scene()
                    if scene: scene.removeItem(c)
                    # FIX 3: Ensure deselected channels are scrubbed from legends
                    for legend in self.lane_legends.values():
                        legend.removeItem(c)
                continue

            if tag not in self.curves:
                color = self._get_distinct_color(idx)
                pen = pg.mkPen(color=color, width=1.5)
                c = pg.PlotDataItem(pen=pen, skipFiniteCheck=True, autoDownsample=True, clipToView=True)

                if cfg.get('scale') == 'log':
                    self.lane_axes[lane].addItem(c)
                else:
                    self.lanes[lane].addItem(c)

                self.lane_legends[lane].addItem(c, name=cfg.get('label', tag))
                self.curves[tag] = c

            y = self.cache.y_data.get(tag, np.array([]))
            if len(y) == 0: continue

            min_len = min(len(x), len(y))
            x_plot, y_plot = x[-min_len:], y[-min_len:] * cfg.get('multiplier', 1.0)

            if cfg.get('scale') == 'log':
                active_axes[lane]['log'] = True
                with np.errstate(all='ignore'):
                    y_plot = np.log10(np.clip(y_plot, 1e-12, None))
            else:
                active_axes[lane]['linear'] = True

            self.curves[tag].setData(x_plot, y_plot)

        if self.auto_scroll and len(x) > 0:
            self._auto_panning = True
            base_plot = list(self.lanes.values())[0]
            base_plot.setXRange(x[-1] - self.live_span, x[-1], padding=0)
            self._auto_panning = False

        for lane, states in active_axes.items():
            left_axis = self.lanes[lane].getAxis('left')
            left_axis.show() # Force axis ON so grid renders

            if states['linear']:
                left_axis.setStyle(showValues=True)
            else:
                left_axis.setStyle(showValues=False)

    def _silent_preload(self):
        """Quietly loads historical data into RAM so zooming out doesn't lag."""
        now = time.time()

        # Preload the last 24 hours.
        fetch_start = now - 86400  # 24 hours ago
        fetch_end = now - 300  # 5 minutes ago (already loaded on boot)

        # This will hit your InfiniteDataCache worker thread without blocking the mouse
        self.cache.request_history(fetch_start, fetch_end, stride=1)
        print("[Preload] Silent 24-hour backfill dispatched.")

    # --- Event Handlers ---

    def _handle_view_change(self):
        if getattr(self, '_auto_panning', False): return
        if self.auto_scroll: self._toggle_scroll_lock(manual_break=True)

        if not self.lanes: return
        base_plot = list(self.lanes.values())[0]

        view_start, view_end = base_plot.viewRange()[0]
        span_seconds = view_end - view_start
        start_ts = self.t0 + view_start
        end_ts = self.t0 + view_end

        if span_seconds > 86400 * 7:
            target_stride = 100
        elif span_seconds > 86400:
            target_stride = 10
        else:
            target_stride = 1

        if target_stride < self.cache.current_stride:
            # FIX 3: Erase old curves from the Legends to prevent duplicate stacking!
            for c in self.curves.values():
                scene = c.scene()
                if scene: scene.removeItem(c)
                for legend in self.lane_legends.values():
                    legend.removeItem(c)

            self.curves.clear()
            self.marker_items.clear()

            self.cache.request_history(start_ts, end_ts, stride=target_stride)
            self._refresh_event_markers()
            return

        if start_ts < self.cache.oldest_loaded_ts:
            fetch_start = start_ts - span_seconds
            self.cache.request_history(fetch_start, self.cache.oldest_loaded_ts, stride=target_stride)

        self._refresh_event_markers()

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

        name, ok = QInputDialog.getText(self, "Save Profile", "Enter profile name:")
        if ok and name.strip():
            name = name.strip()
            # 1. Add to RAM dictionary
            self.profiles[name] = copy.deepcopy(self.plot_config)

            # 2. Update UI ComboBox
            if self.combo_profiles.findText(name) == -1:
                self.combo_profiles.addItem(name)
            self.combo_profiles.setCurrentText(name)

            # 3. Save to Disk
            try:
                # Ensure directory exists
                os.makedirs(os.path.dirname(self.profiles_file), exist_ok=True)
                with open(self.profiles_file, "w") as f:
                    json.dump(self.profiles, f, indent=4)
                QMessageBox.information(self, "Success", f"Profile '{name}' saved successfully.")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to save profile:\n{e}")

    def _load_all_profiles(self):
        try:
            with open(self.profiles_file, "r") as f:
                return json.load(f)
        except:
            return {"Default": {}}

    def _apply_profile(self):
        name = self.combo_profiles.currentText()
        if name in self.profiles:
            self.plot_config = copy.deepcopy(self.profiles[name])
            self.cache.set_active_tags(list(self.plot_config.keys()))

            self._build_lanes()
            self._ensure_inspector_items()
            self._refresh_event_markers()

    def open_channel_config(self):
        available_tags = {k: None for k in self.system_registry.keys()}
        dlg = ChannelSelectorDialog(available_tags, self.plot_config, self.system_registry, self)
        if dlg.exec():
            self.plot_config = dlg.get_selection()
            self.cache.set_active_tags(list(self.plot_config.keys()))

        self._build_lanes()
        self._ensure_inspector_items()
        self._refresh_event_markers()
        self._reset_legend_text()

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

        # FIX 2: Activate the auto-panning shield so we don't break the Scroll Lock
        self._auto_panning = True

        if self.auto_scroll:
            self.live_span = span_seconds
            view_range = base_plot.viewRange()[0]
            right_edge = view_range[1]
            base_plot.setXRange(right_edge - span_seconds, right_edge, padding=0)
        else:
            # If we are looking at history, zoom out from the center of the screen
            view_range = base_plot.viewRange()[0]
            center = (view_range[0] + view_range[1]) / 2
            base_plot.setXRange(center - (span_seconds / 2), center + (span_seconds / 2), padding=0)

        # Deactivate shield
        self._auto_panning = False

    def _on_mouse_moved(self, pos):
        if not self.btn_inspector.isChecked() or len(self.cache.x_time) == 0:
            return

        hovered_plot = None
        for p in self.lanes.values():
            if p.sceneBoundingRect().contains(pos):
                hovered_plot = p
                break

        if not self.lanes: return
        ref_plot = hovered_plot if hovered_plot else list(self.lanes.values())[0]

        mouse_point = ref_plot.vb.mapSceneToView(pos)
        mouse_x = mouse_point.x()

        norm_x = self.cache.x_time - self.t0
        idx = np.searchsorted(norm_x, mouse_x, side='right') - 1
        idx = np.clip(idx, 0, len(norm_x) - 1)
        actual_x = norm_x[idx]

        for v_line in self.v_lines:
            v_line.setPos(actual_x)
            v_line.setVisible(True)

        delta_msg = ""
        if self.pin_idx is not None:
            dt = actual_x - (self.cache.x_time[self.pin_idx] - self.t0)
            delta_msg = f"Δt: {self._format_delta_time(dt)}"

        for i, p in enumerate(self.lanes.values()):
            label = self.delta_labels[i]
            if p == hovered_plot and self.pin_idx is not None:
                label.setPos(actual_x, mouse_point.y())
                label.setText(delta_msg)
                label.show()
            else:
                label.hide()

        for tag, curve in self.curves.items():
            cfg = self.plot_config.get(tag, {})
            lane = cfg.get('lane', 'Lane 1')
            y_data = self.cache.y_data.get(tag, np.array([]))

            if len(y_data) <= idx or lane not in self.lane_legends:
                continue

            val = y_data[idx]
            label_text = cfg.get('label', tag)
            fmt = ".2e" if cfg.get('scale') == 'log' else ".2f"
            legend_text = f"{label_text}: {val:{fmt}}"

            if self.pin_idx is not None:
                dy = val - y_data[self.pin_idx]
                legend_text += f" (Δ: {dy:{fmt}})"

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

            norm_x = self.cache.x_time - self.t0
            idx = np.searchsorted(norm_x, mouse_point.x(), side='right') - 1
            self.pin_idx = np.clip(idx, 0, len(norm_x) - 1)

            # Show pin across all lanes
            for pin_line in self.pin_lines:
                pin_line.setPos(norm_x[self.pin_idx])
                pin_line.show()

        # Right Click: Clear Pin
        elif event.button() == Qt.MouseButton.RightButton:
            self.pin_idx = None
            for pin_line in self.pin_lines: pin_line.hide()
            for label in self.delta_labels: label.hide()

        self._reset_legend_text()

    def _handle_click_events(self, event):
        """Double click for markers."""
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
            self.stats_label.setText("Stats: (Enable Inspector)")
            self._reset_legend_text()
            self.layout_widget.setCursor(Qt.CursorShape.ArrowCursor)

    def _reset_legend_text(self):
        """Resets all legends across all active lanes."""
        for tag, curve in self.curves.items():
            cfg = self.plot_config.get(tag, {})
            lane = cfg.get('lane', 'Lane 1')
            if lane in self.lane_legends:
                lbl_item = self.lane_legends[lane].getLabel(curve)
                if lbl_item:
                    lbl_item.setText(cfg.get('label', tag))

        # Invalidate and re-calculate sizes for all legends
        for legend in self.lane_legends.values():
            legend.layout.invalidate()
            legend.resize(0, 0)
            legend.updateSize()
            legend.layout.activate()

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
        norm_x = self.cache.x_time - self.t0
        indices = np.where((norm_x >= view_start) & (norm_x <= view_end))[0]
        if len(indices) == 0: return

        x_slice = self.cache.x_time[indices]
        y_slices = {tag: self.cache.y_data[tag][indices] for tag in selected_tags}

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
        """Loads markers from JSON file."""
        self.markers_file = os.path.join(os.path.dirname(self.profiles_file), "event_markers.json")
        if os.path.exists(self.markers_file):
            try:
                with open(self.markers_file, 'r') as f:
                    return json.load(f)
            except:
                return {}
        return {}

    def _save_markers(self, timestamp, text):
        """Saves a new marker to the JSON file."""
        all_markers = self._load_markers()
        key = f"{timestamp:.3f}"
        all_markers[key] = text
        try:
            with open(self.markers_file, 'w') as f:
                json.dump(all_markers, f, indent=4)
        except Exception as e:
            print(f"Failed to save marker: {e}")

    def _add_marker_to_graph(self, ts, text, color="#00E5FF"):
        if not self.lanes: return
        base_plot = list(self.lanes.values())[0]

        relative_x = ts - self.t0
        wrapped_text = textwrap.fill(text, width=25)
        stagger_heights = [0.90, 0.75, 0.60, 0.45, 0.30]
        row_index = int(abs(hash(str(f"{ts:.3f}"))) % len(stagger_heights))

        line = pg.InfiniteLine(
            pos=relative_x, angle=90, movable=False,
            pen=pg.mkPen(color, width=2, style=Qt.PenStyle.DashLine),
            label=wrapped_text,
            labelOpts={'position': stagger_heights[row_index], 'color': color, 'fill': (0, 0, 0, 200), 'movable': False}
        )
        line.sigClicked.connect(lambda obj, ev, t=ts: self._confirm_delete_marker(t))

        base_plot.addItem(line)
        self.marker_items[ts] = line

    def _refresh_event_markers(self):
        if not getattr(self, '_markers_visible', True) or not self.lanes: return
        base_plot = list(self.lanes.values())[0]

        view_range = base_plot.viewRange()[0]
        view_start, view_end = view_range[0], view_range[1]
        buffer_val = (view_end - view_start)

        self.event_data = self._load_markers()

        to_remove = []
        for ts, item in self.marker_items.items():
            rel_x = ts - self.t0
            if rel_x < (view_start - buffer_val) or rel_x > (view_end + buffer_val):
                scene = item.scene()
                if scene: scene.removeItem(item)
                to_remove.append(ts)
        for ts in to_remove: del self.marker_items[ts]

        for ts_str, data in self.event_data.items():
            ts = float(ts_str)
            rel_x = ts - self.t0
            if (view_start - buffer_val) <= rel_x <= (view_end + buffer_val):
                if ts not in self.marker_items:
                    if isinstance(data, dict):
                        self._add_marker_to_graph(ts, data['text'], data['color'])
                    else:
                        self._add_marker_to_graph(ts, data, "#00E5FF")

    def _confirm_delete_marker(self, ts):
        """Removes marker from RAM and Disk after user confirmation."""
        from PyQt6.QtWidgets import QMessageBox

        msg = QMessageBox()
        msg.setIcon(QMessageBox.Icon.Question)
        msg.setText("Delete this event marker?")
        msg.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)

        if msg.exec() == QMessageBox.StandardButton.Yes:
            # 1. Remove from Disk (Proximity Search)
            data = self._load_markers()
            target_key = None

            for ts_str in data.keys():
                if abs(float(ts_str) - ts) < 0.1:  # 100ms tolerance
                    target_key = ts_str
                    break

            if target_key:
                del data[target_key]
                with open(self.markers_file, 'w') as f:
                    json.dump(data, f, indent=4)
                print(f"Successfully deleted marker at {target_key}")
            else:
                print(f"Error: Could not find marker on disk matching {ts}")

            # 2. Complete RAM Cleanup (Safe Scene Removal)
            if ts in self.marker_items:
                item = self.marker_items[ts]
                scene = item.scene()
                if scene:
                    scene.removeItem(item)
                del self.marker_items[ts]

            # 3. Force UI Refresh
            self._refresh_event_markers()

    def _toggle_marker_visibility(self):
        self._markers_visible = not self._markers_visible
        if self._markers_visible:
            self.btn_toggle_markers.setText("👁 Markers: ON")
            self._refresh_event_markers()
        else:
            self.btn_toggle_markers.setText("👁 Markers: OFF")
            # Batch remove from graph (Safe Scene Removal)
            for item in self.marker_items.values():
                scene = item.scene()
                if scene:
                    scene.removeItem(item)
            self.marker_items.clear()

    def closeEvent(self, event):
        self.cache.stop()
        super().closeEvent(event)


if __name__ == "__main__":
    import json

    app = QApplication(sys.argv)
    window = DataViewerApp()
    window.show()
    sys.exit(app.exec())