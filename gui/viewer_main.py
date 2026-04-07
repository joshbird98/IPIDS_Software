import sys
import os
import time
import numpy as np
from collections import deque
from datetime import datetime
import copy
import json

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QVBoxLayout, QHBoxLayout,
    QWidget, QPushButton, QSplitter, QLabel, QDateTimeEdit,
    QComboBox, QInputDialog
)
from PyQt6.QtCore import Qt, QDateTime, QTimer
import pyqtgraph as pg

# Custom utilities
from utils.config_manager_2 import load_config, save_config, load_registry
from utils.channel_selector_2 import ChannelSelectorDialog
from utils.data_engine import TimeSeriesEngine
from utils.zmq_listener import ZMQLiveEngine
from network_config import ZMQ_PORT_PLC_PUB, ZMQ_PORT_VACUUM_PUB


class LogAxisItem(pg.AxisItem):
    def tickStrings(self, values, scale, spacing):
        """Converts the raw log10 values back to precise scientific notation."""
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
        """Dynamically adjusts the date/time format based on the zoom level."""
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

                val_str = dt.strftime(fmt)
                if spacing < 1: val_str = val_str[:-3]
                strings.append(val_str)
            except Exception:
                strings.append("")
        return strings


class DataViewerApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("IPIDS Data Historian - Phase 2")
        self.resize(1100, 650)
        self.showMaximized()

        # --- 1. Core Engines ---
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(current_dir)
        log_dir = os.path.join(os.path.dirname(current_dir), "LogData")

        self.engine = TimeSeriesEngine(log_dir)
        self.system_registry = load_registry()

        config_dir = os.path.join(project_root, "config")
        os.makedirs(config_dir, exist_ok=True)
        self.profiles_file = os.path.join(config_dir, "workspace_profiles.json")

        self.profiles = self._load_all_profiles()

        # Load "Default" if it exists, otherwise grab the first available, or fallback to an empty dict
        startup_profile_name = "Default" if "Default" in self.profiles else list(self.profiles.keys())[
            0] if self.profiles else "Default"
        self.plot_config = copy.deepcopy(self.profiles.get(startup_profile_name, {}))

        # --- 2. Memory & Buffers ---
        self.raw_ts = None
        self.raw_vals = None
        self.t0 = 0.0

        self.buffer_size = 600  # 60 seconds of Live data
        self.live_x_axis = deque(maxlen=self.buffer_size)
        self.live_data_buffers = {}
        self.live_curves = {}

        # --- 3. State Machine ---
        self.is_live_mode = False
        self.auto_scroll = True
        self._auto_panning = False

        # --- 4. Threads & Timers ---
        self.zmq_engine = ZMQLiveEngine(ZMQ_PORT_PLC_PUB, ZMQ_PORT_VACUUM_PUB)
        self.zmq_engine.data_ready.connect(self._ingest_live_data)
        self.zmq_engine.start()

        self.update_timer = QTimer()
        self.update_timer.setSingleShot(True)
        self.update_timer.timeout.connect(self._perform_query)

        self.render_timer = QTimer()
        self.render_timer.timeout.connect(self._render_live_frame)

        # Initialize and boot
        self._init_ui()
        self._set_live_mode(True)

    def _init_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QHBoxLayout(main_widget)

        # ====== SIDEBAR ======
        sidebar = QWidget()
        sidebar_layout = QVBoxLayout(sidebar)

        self.btn_mode_toggle = QPushButton("🔴 Switch to LIVE Mode")
        self.btn_mode_toggle.clicked.connect(self._toggle_mode)
        sidebar_layout.addWidget(self.btn_mode_toggle)
        sidebar_layout.addSpacing(15)

        # Quick Presets
        sidebar_layout.addWidget(QLabel("<b>Quick Ranges:</b>"))
        for label, hours in [("1 Hour", 1), ("24 Hours", 24), ("7 Days", 168)]:
            btn = QPushButton(label)
            btn.clicked.connect(lambda ch, h=hours: self._set_preset_range(h))
            sidebar_layout.addWidget(btn)
        sidebar_layout.addSpacing(20)

        # Custom Range
        sidebar_layout.addWidget(QLabel("<b>Custom Range:</b>"))
        self.dt_start = QDateTimeEdit()
        self.dt_end = QDateTimeEdit()

        global_start, global_end = self.engine.get_global_bounds()
        if global_start and global_end:
            min_dt = QDateTime.fromSecsSinceEpoch(int(global_start))
            max_dt = QDateTime.fromSecsSinceEpoch(int(global_end))
            for widget in [self.dt_start, self.dt_end]:
                widget.setMinimumDateTime(min_dt)
                widget.setMaximumDateTime(max_dt)
            self.dt_start.setDateTime(
                max_dt.addSecs(-86400) if max_dt.toSecsSinceEpoch() - min_dt.toSecsSinceEpoch() > 86400 else min_dt)
            self.dt_end.setDateTime(max_dt)
        else:
            self.dt_start.setDateTime(QDateTime.currentDateTime().addDays(-1))
            self.dt_end.setDateTime(QDateTime.currentDateTime())

        for widget in [self.dt_start, self.dt_end]:
            widget.setCalendarPopup(True)
            widget.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
            sidebar_layout.addWidget(widget)

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
        sidebar_layout.addSpacing(20)

        # --- NEW: Workspace Profiles UI ---
        sidebar_layout.addWidget(QLabel("<b>Workspace Profiles:</b>"))

        profile_layout = QHBoxLayout()
        self.combo_profiles = QComboBox()
        self.combo_profiles.addItems(self.profiles.keys())
        profile_layout.addWidget(self.combo_profiles)

        btn_load_profile = QPushButton("Load")
        btn_load_profile.clicked.connect(self._load_selected_profile)
        profile_layout.addWidget(btn_load_profile)

        sidebar_layout.addLayout(profile_layout)

        self.btn_save_profile = QPushButton("💾 Save Profile As...")
        self.btn_save_profile.setStyleSheet("background-color: #607D8B; color: white; font-weight: bold;")
        self.btn_save_profile.clicked.connect(self._save_profile_as)
        sidebar_layout.addWidget(self.btn_save_profile)
        # ----------------------------------

        sidebar_layout.addStretch()

        # ====== PLOT AREA ======
        pg.setConfigOptions(antialias=True)
        self.time_axis = TimeAxisItem(orientation='bottom')
        self.log_axis = LogAxisItem(orientation='right')

        self.graph = pg.PlotWidget(axisItems={'bottom': self.time_axis, 'right': self.log_axis})
        self.graph.setBackground('k')
        self.graph.showGrid(x=True, y=True, alpha=0.3)
        self.graph.getAxis('left').setLabel('Linear Data')
        self.graph.getAxis('right').setLabel('Vacuum (mB)')

        # Initialize Legend Once
        self.legend = self.graph.addLegend(offset=(10, 10))

        # Setup Overlay ViewBox for Log Data
        self.right_viewbox = pg.ViewBox()
        self.graph.scene().addItem(self.right_viewbox)
        self.graph.getAxis('right').linkToView(self.right_viewbox)
        self.right_viewbox.setXLink(self.graph)

        self.graph.getPlotItem().vb.sigResized.connect(self._update_views)
        self.graph.sigRangeChanged.connect(self._on_view_changed)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(sidebar)
        splitter.addWidget(self.graph)
        splitter.setSizes([280, 1000])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter)

    # ==========================================
    # STATE MANAGEMENT
    # ==========================================
    def _toggle_mode(self):
        if self.is_live_mode:
            if not self.auto_scroll:
                self.auto_scroll = True
                self.btn_mode_toggle.setText("🟢 LIVE MODE (Drag graph to pause)")
                self.btn_mode_toggle.setStyleSheet(
                    "background-color: #4CAF50; color: white; font-weight: bold; padding: 10px;")
            else:
                self._set_live_mode(False)
                self._perform_query()
        else:
            self._set_live_mode(True)

    def _set_live_mode(self, active: bool):
        self.is_live_mode = active
        if active:
            self.auto_scroll = True
            self.btn_mode_toggle.setText("🟢 LIVE MODE (Drag graph to pause)")
            self.btn_mode_toggle.setStyleSheet(
                "background-color: #4CAF50; color: white; font-weight: bold; padding: 10px;")

            # Safe reset for Live Stream
            self.graph.clear()
            self.right_viewbox.clear()
            self.legend.clear()
            self.live_curves.clear()

            self._preload_live_buffers(minutes=10)

            self.t0 = time.time()
            self.time_axis.set_offset(self.t0)
            self.render_timer.start(33)
        else:
            self.btn_mode_toggle.setText("🔴 Switch to LIVE Mode")
            self.btn_mode_toggle.setStyleSheet(
                "background-color: #f44336; color: white; font-weight: bold; padding: 10px;")
            self.render_timer.stop()

    def _set_preset_range(self, hours):
        end = QDateTime.currentDateTime()
        start = end.addSecs(-hours * 3600)
        self.dt_start.setDateTime(start)
        self.dt_end.setDateTime(end)
        self._perform_query()

    def _validate_dates(self):
        self.dt_start.blockSignals(True)
        self.dt_end.blockSignals(True)
        start, end = self.dt_start.dateTime(), self.dt_end.dateTime()
        if start >= end:
            if self.sender() == self.dt_start:
                self.dt_end.setDateTime(start.addSecs(1))
            else:
                self.dt_start.setDateTime(end.addSecs(-1))
        self.dt_start.blockSignals(False)
        self.dt_end.blockSignals(False)

    def _on_view_changed(self):
        if getattr(self, '_auto_panning', False): return
        if self.is_live_mode and self.auto_scroll:
            self.auto_scroll = False
            self.btn_mode_toggle.setText("🟡 LIVE (Paused - Click to Resume)")
            self.btn_mode_toggle.setStyleSheet(
                "background-color: #FF9800; color: white; font-weight: bold; padding: 10px;")

    def _update_views(self):
        self.right_viewbox.setGeometry(self.graph.getPlotItem().vb.sceneBoundingRect())
        self.right_viewbox.linkedViewChanged(self.graph.getPlotItem().vb, self.right_viewbox.XAxis)

    def open_channel_config(self):
        # FIX: Build from the Master Registry, ensuring old logs don't hide new tags
        available_tags = {key: None for key in self.system_registry.keys()}

        dlg = ChannelSelectorDialog(available_tags, self.plot_config, self.system_registry, self)
        if dlg.exec():
            self.plot_config = dlg.get_selection()
            #save_config(self.plot_config)

            if self.is_live_mode:
                self.graph.clear()
                self.right_viewbox.clear()
                self.legend.clear()
                self.live_curves.clear()
            else:
                self._render_plot()

    # ==========================================
    # DATA PIPELINES (LIVE)
    # ==========================================
    def _ingest_live_data(self, data_dict):

        self.live_x_axis.append(time.time())
        for tag in self.plot_config.keys():
            if tag not in self.live_data_buffers:
                self.live_data_buffers[tag] = deque(maxlen=self.buffer_size)

            # Zero-order hold fallback
            val = data_dict.get(tag, self.live_data_buffers[tag][-1] if self.live_data_buffers[tag] else 0.0)
            self.live_data_buffers[tag].append(val)

    def _render_live_frame(self):
        if not self.is_live_mode or not self.live_x_axis: return

        norm_x = np.array(self.live_x_axis) - self.t0
        has_linear, has_log = False, False

        for tag, user_config in self.plot_config.items():
            if not user_config.get("selected", False): continue

            # Initialize Persistent Curve (No Name parameter prevents legend duplicates)
            if tag not in self.live_curves:
                pen = pg.mkPen(color=user_config.get('color', '#ffffff'), width=1.5)
                curve = pg.PlotDataItem(pen=pen)

                if user_config.get('scale') == 'log':
                    self.right_viewbox.addItem(curve)
                else:
                    self.graph.addItem(curve)

                self.legend.addItem(curve, name=user_config.get('label', tag))
                self.live_curves[tag] = curve

            # Inject Fast Data
            if tag in self.live_data_buffers:
                y_data = np.array(self.live_data_buffers[tag])
                min_len = min(len(norm_x), len(y_data))
                x_safe, y_safe = norm_x[-min_len:], y_data[-min_len:] * user_config.get("multiplier", 1.0)

                if user_config.get("scale") == "log":
                    has_log = True
                    with np.errstate(invalid='ignore', divide='ignore'):
                        y_safe = np.log10(np.clip(y_safe, 1.0e-12, None))
                else:
                    has_linear = True

                self.live_curves[tag].setData(x_safe, y_safe)

        if self.auto_scroll and len(norm_x) > 0:
            self._auto_panning = True
            self.graph.setXRange(norm_x[-1] - 60.0, norm_x[-1], padding=0)
            self._auto_panning = False

        self.graph.showAxis('left') if has_linear else self.graph.hideAxis('left')
        self.graph.showAxis('right') if has_log else self.graph.hideAxis('right')

    # ==========================================
    # DATA PIPELINES (HISTORY)
    # ==========================================

    def _preload_live_buffers(self, minutes=10):
        """Fetches recent history from disk to seamlessly seed the Live Mode graph."""
        # 10 minutes at ~10Hz = 6000 points. Large enough to see history, small enough to be fast.
        self.buffer_size = int(minutes * 60 * 10)
        self.live_x_axis = deque(maxlen=self.buffer_size)
        self.live_data_buffers.clear()

        # Define the window
        end_ts = time.time()
        start_ts = end_ts - (minutes * 60)

        # Quick silent query
        raw_ts, raw_vals = self.engine.query(start_ts, end_ts)

        if len(raw_ts) > 0:
            pts = min(len(raw_ts), self.buffer_size)
            self.live_x_axis.extend(raw_ts[-pts:])

            for tag, user_config in self.plot_config.items():
                self.live_data_buffers[tag] = deque(maxlen=self.buffer_size)
                if user_config.get('selected'):
                    try:
                        idx = self.engine.channel_keys.index(tag)
                        # Inject raw DB values directly into the live rolling buffers
                        self.live_data_buffers[tag].extend(raw_vals[-pts:, idx])
                    except ValueError:
                        pass  # Tag not in DB yet, that's fine

    def _perform_query(self):
        if self.is_live_mode: self._set_live_mode(False)

        self.req_start_ts = self.dt_start.dateTime().toSecsSinceEpoch()
        self.req_end_ts = self.dt_end.dateTime().toSecsSinceEpoch()

        self.raw_ts, self.raw_vals = self.engine.query(self.req_start_ts, self.req_end_ts)

        # ==========================================
        # --- DIAGNOSTIC PROBE START ---
        # ==========================================
        print("\n" + "=" * 40)
        print("🔍 HISTORY QUERY DIAGNOSTICS")
        print("=" * 40)
        print(f"UI Requested Range : {self.req_start_ts} to {self.req_end_ts}")
        print(f"Engine Returned    : {len(self.raw_ts)} rows")

        if len(self.raw_ts) > 0:
            print(f"Engine Date Range  : {self.raw_ts[0]} to {self.raw_ts[-1]}")

            # Check what tags the engine actually knows about
            print("\n📌 SELECTED TAG CHECK:")
            for tag, cfg in self.plot_config.items():
                if cfg.get('selected'):
                    if tag in self.engine.channel_keys:
                        idx = self.engine.channel_keys.index(tag)
                        max_val = np.max(self.raw_vals[:, idx])
                        print(f"  ✅ Found: {tag} | Max Value in DB: {max_val}")
                    else:
                        print(f"  ❌ Missing from DB index: {tag}")
        print("=" * 40 + "\n")
        # ==========================================
        # --- DIAGNOSTIC PROBE END ---
        # ==========================================

        if len(self.raw_ts) == 0:
            print("[History] No data found for this exact range.")
            self.t0 = self.req_start_ts
        else:
            self.t0 = self.raw_ts[0]

        self.time_axis.set_offset(self.t0)
        self._render_plot()

    def _render_plot(self):
        self.graph.clear()
        self.right_viewbox.clear()
        self.legend.clear()

        if len(self.raw_ts) == 0:
            self.graph.hideAxis('right')
            self.graph.hideAxis('left')
            return

        norm_ts = self.raw_ts - self.t0
        self.graph.setXRange(self.req_start_ts - self.t0, self.req_end_ts - self.t0, padding=0)
        target_pts = int(self.graph.width() * 2)

        has_linear, has_log = False, False

        # FIX: Loop through UI config, hunt for data in Engine
        for tag, user_config in self.plot_config.items():
            if not user_config.get('selected'): continue

            try:
                idx = self.engine.channel_keys.index(tag)
            except ValueError:
                continue  # Gracefully skip if this tag isn't in historical logs yet

            y_raw = self.raw_vals[:, idx] * user_config.get('multiplier', 1.0)
            pen = pg.mkPen(color=user_config.get('color', '#ffffff'), width=1.5)
            label = user_config.get('label', tag)

            if user_config.get('scale') == 'log':
                has_log = True
                y_safe = np.clip(y_raw, 1.0e-12, None)

                # Decimate
                if len(norm_ts) <= target_pts:
                    d_ts, d_y = norm_ts, y_safe
                else:
                    d_ts, d_y = TimeSeriesEngine.downsample_minmax(norm_ts, y_safe, target_pts)

                with np.errstate(invalid='ignore', divide='ignore'):
                    d_y_log = np.log10(d_y)

                curve = pg.PlotDataItem(x=d_ts, y=d_y_log, pen=pen)
                self.right_viewbox.addItem(curve)
                self.legend.addItem(curve, name=label)
            else:
                has_linear = True

                # Decimate
                if len(norm_ts) <= target_pts:
                    d_ts, d_y = norm_ts, y_raw
                else:
                    d_ts, d_y = TimeSeriesEngine.downsample_minmax(norm_ts, y_raw, target_pts)

                curve = pg.PlotDataItem(x=d_ts, y=d_y, pen=pen)
                self.graph.addItem(curve)
                self.legend.addItem(curve, name=label)

        # Cleanup
        self.graph.showAxis('left') if has_linear else self.graph.hideAxis('left')
        self.graph.showAxis('right') if has_log else self.graph.hideAxis('right')

    # ==========================================
    # PROFILE MANAGEMENT
    # ==========================================
    def _load_all_profiles(self):
        """Loads the master dictionary of profiles from disk."""
        try:
            with open(self.profiles_file, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            # If the file doesn't exist yet, seamlessly migrate their old plot_settings.json
            return {"Default": load_config()}

    def _save_profiles_to_disk(self):
        """Writes the master profile dictionary to disk."""
        try:
            with open(self.profiles_file, "w") as f:
                json.dump(self.profiles, f, indent=4)
        except Exception as e:
            print(f"[Profiles] Failed to save workspace profiles: {e}")

    def _load_selected_profile(self):
        """Swaps the active configuration in RAM and forces a redraw."""
        target_name = self.combo_profiles.currentText()
        if target_name in self.profiles:
            import copy
            self.plot_config = copy.deepcopy(self.profiles[target_name])

            # Smart Redraw
            if self.is_live_mode:
                self.graph.clear()
                self.right_viewbox.clear()
                self.legend.clear()
                self.live_curves.clear()
                # The 30FPS loop will rebuild the curves instantly
            else:
                self._render_plot()

    def _save_profile_as(self):
        """Prompts the user for a name and saves the current layout."""
        current_name = self.combo_profiles.currentText()

        # Pop up a text input dialog
        new_name, ok = QInputDialog.getText(
            self, "Save Profile", "Enter profile name:", text=current_name
        )

        if ok and new_name:
            new_name = new_name.strip()
            if not new_name: return

            import copy
            self.profiles[new_name] = copy.deepcopy(self.plot_config)
            self._save_profiles_to_disk()

            # Update the dropdown if it's a brand new name
            existing_items = [self.combo_profiles.itemText(i) for i in range(self.combo_profiles.count())]
            if new_name not in existing_items:
                self.combo_profiles.addItem(new_name)

            self.combo_profiles.setCurrentText(new_name)

            # Visual feedback on the button
            original_text = self.btn_save_profile.text()
            self.btn_save_profile.setText(f"✅ Saved '{new_name}'")
            self.btn_save_profile.setStyleSheet("background-color: #4CAF50; color: white; font-weight: bold;")
            QTimer.singleShot(2000, lambda: self.btn_save_profile.setText(original_text))
            QTimer.singleShot(2000, lambda: self.btn_save_profile.setStyleSheet(
                "background-color: #607D8B; color: white; font-weight: bold;"))

    def closeEvent(self, event):
        self.zmq_engine.stop()
        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = DataViewerApp()
    window.show()
    sys.exit(app.exec())