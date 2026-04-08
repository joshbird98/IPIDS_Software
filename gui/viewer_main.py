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

    def set_offset(self, offset):
        self.offset = offset

    def tickStrings(self, values, scale, spacing):
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

        # --- Plot Area ---
        pg.setConfigOptions(antialias=True, useOpenGL=True)  # OpenGL for max speed
        self.time_axis = TimeAxisItem(orientation='bottom')
        self.log_axis = LogAxisItem(orientation='right')

        self.graph = pg.PlotWidget(axisItems={'bottom': self.time_axis, 'right': self.log_axis})
        self.graph.showGrid(x=True, y=True, alpha=0.3)

        self.legend = self.graph.addLegend(offset=(10, 10))
        self.legend.setBrush(pg.mkBrush(0, 0, 0, 150))  # Semi-transparent black background
        self.legend.setPen(pg.mkPen(255, 255, 255, 100))  # Thin grey border

        self.right_viewbox = pg.ViewBox()
        self.graph.scene().addItem(self.right_viewbox)
        self.graph.getAxis('right').linkToView(self.right_viewbox)
        self.right_viewbox.setXLink(self.graph)

        self.graph.setXRange(-300, 0, padding=0)
        self.graph.sigRangeChanged.connect(self._handle_view_change)
        self.graph.getPlotItem().vb.sigResized.connect(self._sync_viewboxes)

        self.graph.getPlotItem().setMenuEnabled(False)
        # Disable the context menu for both the main plot and the right-axis viewbox
        self.graph.getPlotItem().setMenuEnabled(False)
        self.graph.getPlotItem().vb.setMenuEnabled(False)
        self.right_viewbox.setMenuEnabled(False)

        # Vertical Line (Current Mouse)
        self.v_line = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen('y', width=1, style=Qt.PenStyle.DashLine))
        self.v_line.hide()
        self.graph.addItem(self.v_line, ignoreBounds=True)

        # Pin Line (Reference)
        self.pin_line = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen('#FF5722', width=1.5))
        self.pin_line.hide()
        self.graph.addItem(self.pin_line, ignoreBounds=True)

        # Delta Overlay (Small and tidy)
        self.delta_label = pg.TextItem(anchor=(0, 0), color='#FF5722', fill=(0, 0, 0, 150))
        self.graph.addItem(self.delta_label)
        self.delta_label.hide()

        # Connect signals
        self.graph.scene().sigMouseMoved.connect(self._on_mouse_moved)
        self.graph.scene().sigMouseClicked.connect(self._on_graph_clicked)

        # Store active marker objects in RAM for easy management/cleanup
        self.marker_items = {}
        self.event_data = self._load_markers()

        # Connect double-click to the plot's ViewBox
        self.graph.getPlotItem().vb.scene().sigMouseClicked.connect(self._handle_click_events)

        layout.addWidget(self.graph)

    # --- Core Rendering ---

    def _on_cache_updated(self):
        """Signals received from cache. Redraw handled by render_timer."""
        pass

    def _refresh_plot(self):
        """Unified 30FPS render loop using C++ downsampling."""
        if not self.cache.tags: return

        self.time_axis.set_offset(self.t0)
        x = self.cache.x_time - self.t0

        has_linear, has_log = False, False

        # Sort tags to ensure color consistency across refreshes
        sorted_tags = sorted(self.cache.tags)

        for idx, tag in enumerate(sorted_tags):
            cfg = self.plot_config.get(tag, {})
            if not cfg.get('selected'):
                if tag in self.curves:
                    c = self.curves.pop(tag)
                    self.graph.removeItem(c)
                    self.right_viewbox.removeItem(c)
                    self.legend.removeItem(c)
                continue

            # Lazy-create curves
            if tag not in self.curves:
                color = self._get_distinct_color(idx)
                pen = pg.mkPen(color=color, width=1.5)
                # CRITICAL: Delegation to C++
                c = pg.PlotDataItem(pen=pen, skipFiniteCheck=True, autoDownsample=True, clipToView=True)
                if cfg.get('scale') == 'log':
                    self.right_viewbox.addItem(c)
                else:
                    self.graph.addItem(c)
                self.legend.addItem(c, name=cfg.get('label', tag))
                self.curves[tag] = c

            y = self.cache.y_data.get(tag, np.array([]))
            if len(y) == 0: continue

            # Match lengths
            min_len = min(len(x), len(y))
            x_plot, y_plot = x[-min_len:], y[-min_len:] * cfg.get('multiplier', 1.0)

            if cfg.get('scale') == 'log':
                has_log = True
                with np.errstate(all='ignore'):
                    y_plot = np.log10(np.clip(y_plot, 1e-12, None))
            else:
                has_linear = True

            self.curves[tag].setData(x_plot, y_plot)

        # Auto-scroll logic
        if self.auto_scroll and len(x) > 0:
            self._auto_panning = True
            self.graph.setXRange(x[-1] - 300, x[-1], padding=0)  # 5 min view
            self._auto_panning = False

        self.graph.showAxis('left') if has_linear else self.graph.hideAxis('left')
        self.graph.showAxis('right') if has_log else self.graph.hideAxis('right')

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
        # Safety Gate: Ignore changes made by the auto-scroller itself
        if getattr(self, '_auto_panning', False):
            return

        # Break scroll lock immediately on ANY manual interaction
        if self.auto_scroll:
            self._toggle_scroll_lock(manual_break=True)

        view_start, view_end = self.graph.viewRange()[0]
        span_seconds = view_end - view_start
        start_ts = self.t0 + view_start
        end_ts = self.t0 + view_end

        # --- Resolution Logic ---
        if span_seconds > 86400 * 7:    target_stride = 100
        elif span_seconds > 86400:      target_stride = 10
        else:                           target_stride = 1


        # REFINEMENT: If zooming in (stride decreases), wipe curves to prevent ghosting
        if target_stride < self.cache.current_stride:
            self.graph.clear()
            self.right_viewbox.clear()
            self.legend.clear()
            self.curves.clear()

            self.marker_items.clear()

            # RE-ADD PERSISTENT ITEMS
            self._ensure_inspector_items()
            self._refresh_event_markers()

            # Force inspector visibility state
            if self.btn_inspector.isChecked():
                self.v_line.show()
            else:
                self.v_line.hide()

            # Trigger fetch for the entire visible window at higher res
            self.cache.request_history(start_ts, end_ts, stride=target_stride)

            self._refresh_event_markers()
            return

        # PANNING LEFT: Trigger fetch if we are looking at air
        # We ask for a bit extra (span_seconds) to prevent "stuttering" fetches
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

    def _sync_viewboxes(self):
        self.right_viewbox.setGeometry(self.graph.getPlotItem().vb.sceneBoundingRect())
        self.right_viewbox.linkedViewChanged(self.graph.getPlotItem().vb, self.right_viewbox.XAxis)

    # --- Profile & Config ---

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
            self.graph.clear()
            self.right_viewbox.clear()
            self.legend.clear()
            self.curves.clear()
            self.marker_items.clear()
            self._ensure_inspector_items()
            self._refresh_event_markers()

            # Force inspector visibility state
            if self.btn_inspector.isChecked():
                self.v_line.show()
            else:
                self.v_line.hide()

    def open_channel_config(self):
        available_tags = {k: None for k in self.system_registry.keys()}
        dlg = ChannelSelectorDialog(available_tags, self.plot_config, self.system_registry, self)
        if dlg.exec():
            self.plot_config = dlg.get_selection()
            self.cache.set_active_tags(list(self.plot_config.keys()))

        self.marker_items.clear()
        # RE-ADD PERSISTENT ITEMS
        self._ensure_inspector_items()
        self._refresh_event_markers()

        # Force inspector visibility state
        if self.btn_inspector.isChecked():
            self.v_line.show()
        else:
            self.v_line.hide()

        self._reset_legend_text()

    def _get_distinct_color(self, index):
        """Returns a high-contrast color based on the golden ratio."""
        hue = (index * 0.618033988749895) % 1.0
        color = pg.hsvColor(hue, 0.8, 1.0)
        return color

    def _jump_to_date(self):
        target_ts = self.jump_dt.dateTime().toSecsSinceEpoch()
        current_range = self.graph.viewRange()[0]
        width = current_range[1] - current_range[0]

        self._toggle_scroll_lock(manual_break=True)
        self.graph.setXRange(target_ts - self.t0, target_ts - self.t0 + width)

    def _set_timespan(self, minutes):
        view_range = self.graph.viewRange()[0]
        center = (view_range[0] + view_range[1]) / 2
        half_span = (minutes * 60) / 2
        self.graph.setXRange(center - half_span, center + half_span)

    def _on_inspector_toggled(self, checked):
        if checked:
            # Re-add to graph if it was deleted by a clear() call
            if self.v_line not in self.graph.items():
                self.graph.addItem(self.v_line, ignoreBounds=True)
            self.v_line.show()
            self.btn_inspector.setText("🔍 Inspector: ON")
            self.btn_inspector.setStyleSheet("background-color: #2196F3; color: white; font-weight: bold;")
            # Set cursor to crosshair
            self.graph.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.v_line.hide()
            self.btn_inspector.setText("🔍 Enable Inspector")
            self.btn_inspector.setStyleSheet("")
            self.stats_label.setText("Stats: (Enable Inspector)")

            for tag, curve in self.curves.items():
                label = self.plot_config.get(tag, {}).get('label', tag)
                self.legend.getLabel(curve).setText(label)

            # Restore standard arrow cursor
            self.graph.setCursor(Qt.CursorShape.ArrowCursor)

        self._reset_legend_text()

    def _on_mouse_moved(self, pos):
        if not self.btn_inspector.isChecked(): return

        if self.graph.sceneBoundingRect().contains(pos):
            mouse_point = self.graph.getPlotItem().vb.mapSceneToView(pos)
            mouse_x = mouse_point.x()
            mouse_y = mouse_point.y()

            if len(self.cache.x_time) == 0: return

            # --- 1. SNAP TO PREVIOUS DATA POINT ---
            norm_x = self.cache.x_time - self.t0
            # Find the index of the first point GREATER than mouse_x, then go back one
            idx = np.searchsorted(norm_x, mouse_x, side='right') - 1
            idx = np.clip(idx, 0, len(norm_x) - 1)

            actual_x = norm_x[idx]
            self.v_line.setPos(actual_x)
            self.v_line.show() #?

            # --- 2. UPDATE LEGEND & DELTA ---
            delta_msg = ""
            if self.pin_idx is not None:
                dt = actual_x - (self.cache.x_time[self.pin_idx] - self.t0)
                readable_dt = self._format_delta_time(dt)
                delta_msg = f"Δt: {readable_dt}"
                # --- Smart Positioning Logic ---
                view_range = self.graph.viewRange()
                x_range = view_range[0]
                y_range = view_range[1]

                # Determine horizontal offset (Shift left if at right edge)
                x_offset = (x_range[1] - x_range[0]) * 0.02
                if actual_x > x_range[0] + (x_range[1] - x_range[0]) * 0.8:
                    final_x = actual_x - x_offset
                    anchor_x = 1  # Anchor right side of text to point
                else:
                    final_x = actual_x + x_offset
                    anchor_x = 0  # Anchor left side of text to point

                # Determine vertical offset (Shift down if at top edge)
                y_offset = (y_range[1] - y_range[0]) * 0.05
                if mouse_y > y_range[0] + (y_range[1] - y_range[0]) * 0.8:
                    final_y = mouse_y - y_offset
                    anchor_y = 1
                else:
                    final_y = mouse_y + y_offset
                    anchor_y = 0

                self.delta_label.setAnchor((anchor_x, anchor_y))
                self.delta_label.setPos(final_x, final_y)
                self.delta_label.setText(delta_msg)
                self.delta_label.show()

            for tag, curve in self.curves.items():
                y_data = self.cache.y_data.get(tag, np.array([]))
                if len(y_data) <= idx: continue

                val = y_data[idx]
                label = self.plot_config.get(tag, {}).get('label', tag)
                is_log = self.plot_config.get(tag, {}).get('scale') == 'log'
                fmt = ".2e" if is_log else ".2f"

                legend_text = f"{label}: {val:{fmt}}"

                # If pinned, add the Delta-Y and Stats for this specific curve
                if self.pin_idx is not None:
                    ref_val = y_data[self.pin_idx]
                    dy = val - ref_val
                    legend_text += f" (Δ: {dy:{fmt}})"

                self.legend.getLabel(curve).setText(legend_text)

    def _on_graph_clicked(self, event):
        # Standard Middle Click to center
        if event.button() == Qt.MouseButton.MiddleButton:
            pos = event.scenePos()
            mouse_point = self.graph.getPlotItem().vb.mapSceneToView(pos)

            view_range = self.graph.viewRange()[0]
            width = view_range[1] - view_range[0]

            # Shift window so the clicked X-coordinate is in the middle
            self.graph.setXRange(mouse_point.x() - width / 2, mouse_point.x() + width / 2, padding=0)
            return

        # CTRL + LEFT CLICK to Pin
        if event.button() == Qt.MouseButton.LeftButton and QApplication.keyboardModifiers() == Qt.KeyboardModifier.ControlModifier:
            if not self.btn_inspector.isChecked(): return

            pos = event.scenePos()
            mouse_point = self.graph.getPlotItem().vb.mapSceneToView(pos)

            norm_x = self.cache.x_time - self.t0
            idx = np.searchsorted(norm_x, mouse_point.x(), side='right') - 1
            self.pin_idx = np.clip(idx, 0, len(norm_x) - 1)

            self._ensure_inspector_items()
            self._refresh_event_markers()
            self.pin_line.setPos(norm_x[self.pin_idx])
            self.pin_line.show()

        # RIGHT CLICK to clear pin
        elif event.button() == Qt.MouseButton.RightButton:
            self.pin_idx = None
            if hasattr(self, 'pin_line'): self.pin_line.hide()
            if hasattr(self, 'delta_label'): self.delta_label.hide()

        self._reset_legend_text()

    def _ensure_inspector_items(self):
        """Re-adds inspector items to the graph if they were removed by clear()."""
        # Check v_line
        if self.v_line not in self.graph.items():
            self.graph.addItem(self.v_line, ignoreBounds=True)

        # Check pin_line
        if self.pin_line not in self.graph.items():
            self.graph.addItem(self.pin_line, ignoreBounds=True)

        # Check delta_label
        if self.delta_label not in self.graph.items():
            self.graph.addItem(self.delta_label)

    def _reset_legend_text(self):
        """Resets legend labels and forces the container to shrink-wrap the text."""
        if not self.legend:
            return

        for tag, curve in self.curves.items():
            label_item = self.legend.getLabel(curve)
            if label_item:
                # Revert to original tag/label
                original_label = self.plot_config.get(tag, {}).get('label', tag)
                label_item.setText(original_label)

        # 1. Invalidate the current layout state
        self.legend.layout.invalidate()

        # 2. Force the geometry to zero.
        # PyQtGraph will immediately resize it back up to its minimum required size.
        self.legend.resize(0, 0)

        # 3. Re-trigger the size calculation
        self.legend.updateSize()

        # 4. Final layout activation
        self.legend.layout.activate()

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
        if self.graph.underMouse() and event.button() == Qt.MouseButton.LeftButton:
            self.graph.setCursor(Qt.CursorShape.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if self.btn_inspector.isChecked():
            self.graph.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.graph.setCursor(Qt.CursorShape.ArrowCursor)
        super().mouseReleaseEvent(event)

    def _browse_export_path(self):
        current = self.edit_export_path.text()
        full_path, _ = QFileDialog.getSaveFileName(self, "Select Export Base Name", current)
        if full_path:
            self.edit_export_path.setText(full_path)

    def _prepare_export(self):
        if not self.cache.tags: return

        # 1. Filter Channels (Standard)
        selected_tags = [t for t in self.cache.tags if self.plot_config.get(t, {}).get('selected')]
        tag_labels = [self.plot_config[t].get('label', t) for t in selected_tags]
        if not selected_tags: return

        # 2. Paths
        full_path = self.edit_export_path.text()
        base_dir = os.path.dirname(full_path)
        base_name = os.path.basename(full_path)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = os.path.join(base_dir, f"{base_name}_{stamp}.csv")
        svg_path = os.path.join(base_dir, f"{base_name}_{stamp}.svg")

        # 3. Data Slicing (NumPy)
        view_start, view_end = self.graph.viewRange()[0]
        norm_x = self.cache.x_time - self.t0
        indices = np.where((norm_x >= view_start) & (norm_x <= view_end))[0]
        if len(indices) == 0: return
        x_slice = self.cache.x_time[indices]
        y_slices = {tag: self.cache.y_data[tag][indices] for tag in selected_tags}

        # --- THE LOSSLESS FIX ---
        from PyQt6 import QtSvg
        from PyQt6.QtCore import QBuffer, QIODevice, QRectF

        # 1. TEMPORARILY DISABLE OPENGL
        # This forces the curves to be rendered as standard CPU vectors for the capture
        pg.setConfigOptions(useOpenGL=False)

        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)

        plot_item = self.graph.getPlotItem()
        # Force a geometry sync for the dual-axis setup
        self._sync_viewboxes()

        # We target the PlotItem's bounding box
        export_rect = plot_item.sceneBoundingRect()

        generator = QtSvg.QSvgGenerator()
        generator.setOutputDevice(buffer)

        # Standardize DPI to 96 to prevent High-DPI "Quarter Screen" shifts
        generator.setResolution(96)
        generator.setSize(export_rect.size().toSize())
        generator.setViewBox(export_rect)
        generator.setTitle("IPIDS Lossless Export")

        painter = pg.QtGui.QPainter(generator)

        # Draw background color manually
        painter.fillRect(export_rect, self.graph.backgroundBrush())

        # FIX: Explicit Scene Rendering with Zero-Offset target
        # Source: export_rect (where it is in the scene)
        # Target: A rectangle of the same size starting at (0,0) inside the SVG
        target_rect = QRectF(0, 0, export_rect.width(), export_rect.height())
        self.graph.scene().render(painter, target_rect, export_rect)

        painter.end()
        svg_data = buffer.data()
        buffer.close()

        # RESTORE OPENGL for the live GUI
        pg.setConfigOptions(useOpenGL=True)

        # 4. Launch Worker
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

    def _handle_click_events(self, event):
        """Handles double-click to add markers."""
        if event.double():
            pos = event.scenePos()
            mouse_point = self.graph.getPlotItem().vb.mapSceneToView(pos)
            clicked_x = mouse_point.x()
            absolute_ts = clicked_x + self.t0

            dlg = MarkerDialog(self)
            if dlg.exec():
                text, color = dlg.get_data()
                if text:
                    self._add_marker_to_graph(absolute_ts, text, color)
                    # Save as a dict in JSON
                    self._save_markers(absolute_ts, {"text": text, "color": color})

    def _add_marker_to_graph(self, ts, text, color="#00E5FF"):
        relative_x = ts - self.t0
        wrapped_text = textwrap.fill(text, width=25)

        stagger_heights = [0.90, 0.75, 0.60, 0.45, 0.30]
        row_index = int(abs(hash(str(f"{ts:.3f}"))) % len(stagger_heights))

        line = pg.InfiniteLine(
            pos=relative_x,
            angle=90,
            movable=False,
            pen=pg.mkPen(color, width=2, style=Qt.PenStyle.DashLine),  # Use the chosen color
            label=wrapped_text,
            labelOpts={
                'position': stagger_heights[row_index],
                'color': color,  # Match text to line
                'fill': (0, 0, 0, 200),
                'movable': False
            }
        )
        line.sigClicked.connect(lambda obj, ev, t=ts: self._confirm_delete_marker(t))
        self.graph.addItem(line)
        self.marker_items[ts] = line

    def _refresh_event_markers(self):
        if not getattr(self, '_markers_visible', True): return

        view_range = self.graph.viewRange()[0]
        view_start, view_end = view_range[0], view_range[1]
        buffer_val = (view_end - view_start)

        self.event_data = self._load_markers()

        # 1. Prune
        to_remove = []
        for ts, item in self.marker_items.items():
            rel_x = ts - self.t0
            if rel_x < (view_start - buffer_val) or rel_x > (view_end + buffer_val):
                self.graph.removeItem(item)
                to_remove.append(ts)
        for ts in to_remove:
            del self.marker_items[ts]

        # 2. Add
        for ts_str, data in self.event_data.items():
            ts = float(ts_str)
            rel_x = ts - self.t0
            if (view_start - buffer_val) <= rel_x <= (view_end + buffer_val):
                if ts not in self.marker_items:
                    # Support both old string format and new dict format
                    if isinstance(data, dict):
                        self._add_marker_to_graph(ts, data['text'], data['color'])
                    else:
                        self._add_marker_to_graph(ts, data, "#00E5FF")

    def _confirm_delete_marker(self, ts):
        """Removes marker from RAM and Disk after user confirmation."""
        # Optional: Ask before deleting to prevent frustration
        from PyQt6.QtWidgets import QMessageBox

        msg = QMessageBox()
        msg.setIcon(QMessageBox.Icon.Question)
        msg.setText("Delete this event marker?")
        msg.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)

        if msg.exec() == QMessageBox.StandardButton.Yes:
            # 1. Remove from Disk (Proximity Search)
            data = self._load_markers()
            target_key = None

            # Find the key that is numerically closest to the clicked timestamp
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

            # 2. Complete RAM Cleanup
            # Remove the specific line object
            if ts in self.marker_items:
                self.graph.removeItem(self.marker_items[ts])
                del self.marker_items[ts]

            # 3. Force UI Refresh
            # This ensures any "ghost" markers are cleared and the view is updated
            self._refresh_event_markers()

    def _toggle_marker_visibility(self):
        self._markers_visible = not self._markers_visible
        if self._markers_visible:
            self.btn_toggle_markers.setText("👁 Markers: ON")
            self._refresh_event_markers()
        else:
            self.btn_toggle_markers.setText("👁 Markers: OFF")
            # Batch remove from graph
            for item in self.marker_items.values():
                self.graph.removeItem(item)
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