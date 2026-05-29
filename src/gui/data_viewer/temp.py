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

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QVBoxLayout, QHBoxLayout,
    QWidget, QPushButton, QLabel, QDateTimeEdit,
    QFileDialog, QCheckBox
)
from PyQt6.QtCore import Qt, QDateTime, QTimer, QThread, pyqtSignal
import pyqtgraph as pg

from src.core.network_config import ZMQ_PORT_LOGGER_CMD
from src.gui.data_viewer.config_manager import load_registry
from src.gui.data_viewer.channel_selector import ChannelSelectorDialog
from src.core.data_cache import DualPipelineCache

from PyQt6.QtWidgets import QDialog, QFormLayout, QLineEdit, QComboBox, QDialogButtonBox
from PyQt6.QtGui import QIcon, QPixmap, QColor
from PyQt6.QtCore import QSize

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
        self.ui_lockout = 1.0 / 30.0  # Cap at 30 FPS

        # 5. Background Sync for multi-window marker updates
        self.marker_sync_timer = QTimer()
        self.marker_sync_timer.timeout.connect(self._refresh_event_markers)
        self.marker_sync_timer.start(1000)  # Check SQLite every 1 second

    # --- Core Rendering ---

    def _on_cache_updated(self):
        """Signals received from cache. Redraw handled by render_timer."""
        pass

    def _refresh_plot_live(self):
        now = time.perf_counter()
        if now - self.last_ui_update < self.ui_lockout: return
        self.last_ui_update = now
        if not self.cache.tags or not self.lanes: return
        for ta in self.time_axes: ta.set_offset(self.t0)

        x = self.cache.x_live_view - self.t0
        self._render_curves(self.curves, x, self.cache.y_live_view, is_historical=False)

        if self.is_historical_mode and len(self.historical_x) > 0:
            self._render_curves(self.hist_curves, self.historical_x, self.historical_y, is_historical=True)

        if self.auto_scroll and len(x) > 0:
            self._auto_panning = True
            base_plot = list(self.lanes.values())[0]
            view_range = base_plot.viewRange()[0]
            current_width = view_range[1] - view_range[0]
            self.live_span = current_width
            base_plot.setXRange(x[-1] - current_width, x[-1], padding=0)
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
        # Force the Qt Application to flush the render buffer immediately so we can measure it
        QApplication.processEvents()

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
            if len(y) == 0: continue

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
        if self.auto_scroll and len(self.cache.x_time) > 0:
            right_edge = self.cache.x_time[-1] - self.t0

        base_plot.setXRange(right_edge - span_seconds, right_edge, padding=0)
        self._check_and_fetch_history()  # History load happens under shield
        self._auto_panning = False

    def _check_and_fetch_history(self):
        if not self.lanes: return
        base_plot = list(self.lanes.values())[0]

        view_start, view_end = base_plot.viewRange()[0]
        start_ts = self.t0 + view_start
        end_ts = self.t0 + view_end

        # DEBUG: See what the UI is asking for
        #print(f"[UI State] View Span: {start_ts:.1f} to {end_ts:.1f} (Duration: {(end_ts - start_ts) / 3600:.2f} hrs)")

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


if __name__ == "__main__":
    import json
    import zmq
    import orjson

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
    window_2 = DataViewerApp(master_cache, window_title="IPIDS Data Viewer - Window 2")

    window_1.show()
    window_2.show()

    # 3. Execute Application Loop
    exit_code = app.exec()

    # 4. Graceful Teardown
    print("[Launcher] All windows closed. Terminating background threads...")
    master_cache.stop()
    sys.exit(exit_code)