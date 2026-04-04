import os
import csv
import time
import gc
import numpy as np
from datetime import datetime
from PyQt5.QtWidgets import (QVBoxLayout, QFileDialog, QWidget, QMessageBox, QDialog)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QImage, QPainter, QColor
import pyqtgraph as pg

# Import your helpers
from hmi_widget_classes import TimeAxisItem, ScientificAxisItem
# Import the selector we built earlier
from utils.config_manager import load_config, save_config
from utils.channel_selector import ChannelSelectorDialog


class PlotController:
    def __init__(self, main_window_ui, plc, plot_placeholder):
        self.ui = main_window_ui
        self.plc = plc
        self.container = plot_placeholder

        # 1. Load Config
        from utils.config_manager import load_config
        saved_config = load_config()

        # Safety Clamp (Prevent freeze on startup)
        active_count = sum(1 for v in saved_config.values() if v.get('selected'))
        if active_count > 15:
            print(f"⚠️ Warning: Config requested {active_count} active channels. Resetting.")
            for k in saved_config: saved_config[k]['selected'] = False
            # Enable default
            def_tag = 'system.vacuumSystem.gauges.source.readback_mB'
            if def_tag in saved_config: saved_config[def_tag]['selected'] = True

        self.full_config = saved_config
        self.channels = {k: v for k, v in saved_config.items() if v.get('selected', False)}
        self.active_channels = set(self.channels.keys())

        self.history = self.plc.history
        self.time_span_seconds = 30
        self.gc_counter = 0
        self.last_menu_close_time = 0
        self.boot_time = time.time()  # Jitter Fix Anchor

        self.curves = {}

        # --- CORRECT INITIALIZATION ORDER ---
        self._init_plot_widget()  # 1. Create Widget
        self._setup_legend()  # 2. Create Legend (Empty)
        self._setup_connections()  # 3. Connect Buttons

        self._rebuild_curves()  # 4. NOW create curves (Legend exists!)

    def _init_plot_widget(self):
        layout = QVBoxLayout(self.container)

        # 1. Create PlotWidget with Custom Axes
        pg.setConfigOptions(antialias=True)
        self.graph = pg.PlotWidget(axisItems={
            'bottom': TimeAxisItem(orientation='bottom'),
            'right': ScientificAxisItem(orientation='right')
        })

        self.graph.setBackground('k')
        self.graph.showGrid(x=True, y=True, alpha=0.3)
        layout.addWidget(self.graph)

        # Fix for Shaking
        self.graph.getAxis('left').setWidth(60)

        # 2. Setup Right Axis
        self.right_viewbox = pg.ViewBox()
        self.graph.scene().addItem(self.right_viewbox)

        self.right_axis_item = self.graph.getAxis('right')
        self.right_axis_item.linkToView(self.right_viewbox)
        self.right_axis_item.setLabel('Log Scale', units=None)
        self.right_axis_item.setWidth(60)

        # 3. Link Views
        self.right_viewbox.setXLink(self.graph.getPlotItem())
        self.graph.getPlotItem().vb.sigResized.connect(self._update_views)

        # --- REMOVE THIS LINE ---
        # self._rebuild_curves()  <-- DELETE THIS

    def _setup_connections(self):
        try:
            # Buttons
            if hasattr(self.ui, 'folderButton'):
                self.ui.folderButton.clicked.connect(self.browse_folder)
            if hasattr(self.ui, 'savePlotButton'):
                self.ui.savePlotButton.clicked.connect(self.save_data)
            if hasattr(self.ui, 'plotChannelsSelectionButton'):
                self.ui.plotChannelsSelectionButton.clicked.connect(self.show_channel_menu)

            # Slider
            if hasattr(self.ui, 'plotTimeSpanSlider'):
                self.ui.plotTimeSpanSlider.valueChanged.connect(self.update_time_span)
                self.ui.plotTimeSpanSlider.setValue(212)  # ~30 seconds default

            # Labels/Paths
            if hasattr(self.ui, 'line_edit_folder'):
                default_save_dir = os.path.join(os.getcwd(), "PlotData")
                os.makedirs(default_save_dir, exist_ok=True)
                self.ui.line_edit_folder.setText(default_save_dir)

            if hasattr(self.ui, 'line_edit_filename'):
                self.ui.line_edit_filename.setText("Experiment_01")
        except AttributeError as e:
            print(f"UI Setup Warning: {e}")

    def _update_views(self):
        self.right_viewbox.setGeometry(self.graph.getPlotItem().vb.sceneBoundingRect())
        self.right_viewbox.linkedViewChanged(self.graph.getPlotItem().vb, self.right_viewbox.XAxis)

    def _setup_legend(self):
        self.legend = pg.LegendItem(offset=(70, 10))
        self.legend.setParentItem(self.graph.getPlotItem())
        self.legend.setBrush(pg.mkBrush(0, 0, 0, 150))  # Semi-transparent
        self.legend.setPen(pg.mkPen('w'))
        self._refresh_legend()

    def _refresh_legend(self):
        self.legend.clear()
        if not self.active_channels:
            self.legend.setVisible(False)
            return

        self.legend.setVisible(True)
        for name, info in self.curves.items():
            if name in self.active_channels:
                curve_item = info['item']
                display_label = curve_item.opts['name']

                self.legend.addItem(curve_item, display_label)

    def _rebuild_curves(self):
        self.graph.clear()
        self.right_viewbox.clear()
        self.curves = {}

        if self.right_viewbox not in self.graph.scene().items():
            self.graph.scene().addItem(self.right_viewbox)

        for name, cfg in self.channels.items():
            display_name = cfg.get('label', name)
            c_code = cfg.get('color')
            if not c_code:
                c_code = self._get_persistent_color(name)
            pen = pg.mkPen(color=c_code, width=1, cosmetic=True)

            if cfg.get('scale') == 'log':
                curve = pg.PlotDataItem(pen=pen, name=display_name, connect='finite', symbol=None)
                curve.setDownsampling(auto=True, method='peak')
                curve.setClipToView(True)
                self.right_viewbox.addItem(curve)
                self.curves[name] = {'item': curve, 'axis': 'right'}
            else:
                curve = self.graph.plot(pen=pen, name=display_name, connect='finite', symbol=None)
                curve.setDownsampling(auto=True, method='peak')
                curve.setClipToView(True)
                self.curves[name] = {'item': curve, 'axis': 'left'}

        self._refresh_legend()
        self.refresh_display()

    def refresh_display(self):
        """
        Main Plotting Loop.
        - Fetches data from RingBuffer.
        - Fixes Floating Point Jitter (Axis Normalization).
        - Applies Dynamic SI Scaling (Auto-Range).
        - Applies Min/Max Downsampling for stability.
        - Updates Axes with robust scaling logic.
        """
        # 1. Visibility Check (Save CPU if tab is hidden)
        if hasattr(self.ui, 'plot_tab_widget') and not self.ui.plot_tab_widget.isVisible():
            return

        # --- PART 1: GET DATA VIEW ---
        now = time.time()
        view_start = now - self.time_span_seconds
        times, slices = self.history.get_current_view(start_time=view_start)

        if len(times) == 0: return

        # --- AXIS NORMALIZATION (Jitter Fix) ---
        norm_times = times - self.boot_time
        self.graph.getAxis('bottom').set_offset(self.boot_time)
        self.graph.setXRange(view_start - self.boot_time, now - self.boot_time, padding=0)

        # --- PART 2: UPDATE CURVES ---
        linear_min, linear_max = float('inf'), float('-inf')
        log_min, log_max = float('inf'), float('-inf')
        has_linear = False
        has_log = False

        for name, info in self.curves.items():
            # A. Check Visibility
            if name not in self.active_channels:
                info['item'].hide()
                continue

            config = self.channels.get(name)
            if not config: continue

            raw_tag = config['dataSource']
            if raw_tag not in self.history.data_buffers: continue

            info['item'].show()

            # B. Extract Data (Stitching)
            chunks = [self.history.data_buffers[raw_tag][s] for s in slices]
            if len(chunks) == 1: raw_curve_data = chunks[0]
            else: raw_curve_data = np.concatenate(chunks)

            # C. SCALING & LABELING LOGIC
            user_mult = config.get('multiplier', 1.0)
            final_label = config.get('label', name)
            scale_type = config.get('scale', 'linear')

            # SANITIZATION: If label looks like a raw tag (has dots, no units),
            # fix it immediately so the Auto-Scaler can work.
            if "." in final_label and "(" not in final_label:
                final_label = self._generate_smart_label(final_label)

            # CASE 1: Auto-Dynamic Scaling (Linear only, Multiplier must be 1.0)
            if user_mult == 1.0 and scale_type == 'linear':
                # Calculate best SI prefix (e.g. nA, mA) based on data magnitude
                dyn_mult, dyn_label = self._calculate_dynamic_unit(raw_curve_data, final_label)
                plot_data_final = raw_curve_data * dyn_mult
                final_label = dyn_label  # Override label with dynamic one

            # CASE 2: Manual Scaling (User set specific multiplier)
            else:
                plot_data_final = raw_curve_data * user_mult

            # D. Update Legend Text (If changed)
            # Only update if the text is actually different to save UI redraws
            if info['item'].opts['name'] != final_label:
                info['item'].setData(name=final_label)
                self._update_legend_label(info['item'], final_label)

            c_times = norm_times.astype(np.float32)
            c_data = plot_data_final.astype(np.float32)

            # F. Log Scale Sanitization
            if scale_type == 'log':
                # For log, we MUST filter <= 0 before plotting or it breaks
                # We use a temporary view so we don't modify the original buffer
                c_data = plot_data_final.copy()
                c_data[c_data <= 0] = np.nan
            else:
                c_data = plot_data_final

            # G. Update Graph Item
            info['item'].setData(c_times, c_data, skipFiniteCheck=True)

            # --- PART 3: ACCUMULATE RANGES FOR AUTO-SCALE ---
            valid_mask = np.isfinite(c_data)
            if not np.any(valid_mask): continue

            clean_vals = c_data[valid_mask]

            if scale_type == 'log':
                has_log = True
                # Filter noise floor for Log Scale
                pos_vals = clean_vals[clean_vals > 1e-12]
                if len(pos_vals) > 0:
                    current_min = np.min(pos_vals)
                    current_max = np.max(pos_vals)
                    if current_min < log_min: log_min = current_min
                    if current_max > log_max: log_max = current_max
            else:
                has_linear = True
                if len(clean_vals) > 5000:
                    # Stride larger arrays
                    subset = clean_vals[::50]
                    c_min = np.percentile(subset, 1)
                    c_max = np.percentile(subset, 99)
                else:
                    c_min = np.min(clean_vals)
                    c_max = np.max(clean_vals)

                if c_min < linear_min: linear_min = c_min
                if c_max > linear_max: linear_max = c_max

        # --- PART 4: AXIS ADJUSTMENT ---

        # ---------------- LINEAR AXIS ----------------
        if has_linear and linear_min != float('inf'):
            self.graph.showAxis('left')
            rng = linear_max - linear_min
            if rng == 0: rng = 1.0

            # Standard 10% Padding
            padding = rng * 0.1
            self.graph.setYRange(linear_min - padding, linear_max + padding)
        else:
            self.graph.hideAxis('left')

        # ---------------- LOG AXIS ----------------
        if has_log and log_min != float('inf'):
            self.graph.showAxis('right')
            self.right_viewbox.setVisible(True)

            # Prevent zeroes or inverted ranges
            if log_min <= 0: log_min = 1e-10
            if log_max <= 0: log_max = 1e-9

            # Smart Headroom Logic
            signal_ratio = log_max / log_min

            if signal_ratio < 5.0:
                # Static Signal (Flat line or small noise)
                # Force ~1 decade window centered on signal
                center = np.sqrt(log_min * log_max)
                final_min = center / 3.5
                final_max = center * 3.5
            else:
                # Dynamic Signal (Moving a lot)
                # Add ~20% visual padding
                final_min = log_min / 1.5
                final_max = log_max * 1.5

            self.right_viewbox.setYRange(final_min, final_max)
        else:
            self.graph.hideAxis('right')
            self.right_viewbox.setVisible(False)

    def _update_legend_label(self, item, new_label):
        """Forces the Legend to update text for a specific item."""
        # The LegendItem stores data as [ (SampleItem, LabelItem), ... ]
        # We search for our item and update the LabelItem text.
        for sample, label in self.legend.items:
            if sample.item is item:
                label.setText(new_label)
                # Force geometry update so the box resizes if text is longer
                self.legend.layout.invalidate()
                break

    def _calculate_dynamic_unit(self, data_array, base_label):
        """
        Analyzes data to find the best SI prefix (n, µ, m, k, M).
        Returns: (multiplier, new_label_text)
        """
        # 1. Parse Base Unit from Label "Name (V)" -> "V"
        # We look for a unit inside parentheses at the end
        import re
        match = re.search(r"\((.*?)\)$", base_label)
        if not match:
            # No unit found in brackets? Return defaults.
            return 1.0, base_label

        unit = match.group(1)  # e.g. "A"
        prefix_text = base_label[:match.start()].strip()  # e.g. "Beam Current"

        # 2. Find Max Magnitude (Ignore Zeros/NaNs)
        if len(data_array) == 0: return 1.0, base_label

        # Fast absolute max (handle NaN)
        valid = data_array[np.isfinite(data_array)]
        if len(valid) == 0: return 1.0, base_label

        max_val = np.max(np.abs(valid))

        if max_val == 0: return 1.0, base_label

        # 3. Determine Scale
        # We prefer engineering notation (steps of 1000)
        scale = 1.0
        prefix = ""

        if max_val >= 1e9:
            scale, prefix = 1e-9, "G"
        elif max_val >= 1e6:
            scale, prefix = 1e-6, "M"
        elif max_val >= 1e3:
            scale, prefix = 1e-3, "k"
        elif max_val >= 1.0:
            scale, prefix = 1.0, ""
        elif max_val >= 1e-3:
            scale, prefix = 1e3, "m"
        elif max_val >= 1e-6:
            scale, prefix = 1e6, "µ"
        elif max_val >= 1e-9:
            scale, prefix = 1e9, "n"
        elif max_val >= 1e-12:
            scale, prefix = 1e12, "p"
        elif max_val >= 1e-12:
            scale, prefix = 1e12, "p"

        # 4. Construct New Label
        # e.g. "Beam Current (nA)"
        new_label = f"{prefix_text} ({prefix}{unit})"

        return scale, new_label

    def _generate_smart_label(self, tag_name):
        """
        Converts 'system.beamline.drop_in_cup.readbackA' -> 'Drop In Cup (A)'.
        Used as a fallback if the saved config has raw labels.
        """
        # 1. Detect Unit
        unit = ""
        if tag_name.endswith("_mB"):
            unit = "mB"
        elif tag_name.endswith("readbackW"):
            unit = "W"
        elif tag_name.endswith("readbackV"):
            unit = "V"
        elif tag_name.endswith("readbackA"):
            unit = "A"
        elif tag_name.endswith("readbackC"):
            unit = "°C"
        elif tag_name.endswith("C"):
            unit = "°C"
        elif tag_name.endswith("W"):
            unit = "W"
        elif tag_name.endswith("V"):
            unit = "V"
        elif tag_name.endswith("A"):
            unit = "A"

        # 2. Identify Core Name (Parent)
        parts = tag_name.split('.')
        core_name = parts[-1]

        # Use parent if leaf is generic 'readback'
        if "readback" in core_name.lower() and len(parts) > 1:
            core_name = parts[-2]

        # 3. Clean Up
        core_name = core_name.replace("readback", "").replace("_mB", "")
        core_name = core_name.replace("_", " ")
        import re
        core_name = re.sub(r"(\w)([A-Z])", r"\1 \2", core_name)  # CamelCase
        core_name = core_name.title().strip()

        # 4. Return Format
        if unit:
            return f"{core_name} ({unit})"
        return core_name

    def _downsample_minmax(self, times, data, target_points=3000):
        """Preserves Min/Max envelope."""
        n_points = len(data)
        chunk_size = int(n_points // target_points)
        if chunk_size < 2: return times, data

        n_chunks = n_points // chunk_size
        n_usable = n_chunks * chunk_size

        data_view = data[:n_usable].reshape(n_chunks, chunk_size)
        time_view = times[:n_usable].reshape(n_chunks, chunk_size)

        with np.errstate(invalid='ignore'):
            mins = np.nanmin(data_view, axis=1)
            maxs = np.nanmax(data_view, axis=1)

        final_data = np.empty(n_chunks * 2, dtype=data.dtype)
        final_data[0::2] = mins
        final_data[1::2] = maxs

        final_times = np.empty(n_chunks * 2, dtype=times.dtype)
        final_times[0::2] = time_view[:, 0]
        final_times[1::2] = time_view[:, -1]

        return final_times, final_data

    def show_channel_menu(self):
        if (time.time() - self.last_menu_close_time) < 0.2: return

        available_tags = self.plc.tags
        dlg = ChannelSelectorDialog(available_tags, self.full_config, parent=self.ui)

        # --- REMOVED THE EXTRA CONNECT LINE HERE (It caused the freeze/logic duplication) ---

        if dlg.exec_() == QDialog.Accepted:
            self.full_config = dlg.get_selection()
            save_config(self.full_config)

            # Filter active
            self.channels = {k: v for k, v in self.full_config.items() if v.get('selected')}
            self.active_channels = set(self.channels.keys())

            self._rebuild_curves()
            self._refresh_legend()

        self.last_menu_close_time = time.time()

    def _get_persistent_color(self, tag_name):
        import zlib
        # A palette of 20 high-contrast, distinguishable colors (Tableau 20)
        # These pairs are designed to be distinct even if close to each other
        palette = [
            '#1f77b4', '#aec7e8',  # Blue / Light Blue
            '#ff7f0e', '#ffbb78',  # Orange / Light Orange
            '#2ca02c', '#98df8a',  # Green / Light Green
            '#d62728', '#ff9896',  # Red / Light Red
            '#9467bd', '#c5b0d5',  # Purple / Light Purple
            '#8c564b', '#c49c94',  # Brown / Light Brown
            '#e377c2', '#f7b6d2',  # Pink / Light Pink
            '#7f7f7f', '#c7c7c7',  # Gray / Light Gray
            '#bcbd22', '#dbdb8d',  # Olive / Light Olive
            '#17becf', '#9edae5'  # Cyan / Light Cyan
        ]
        # Use adler32 for a fast, consistent hash (doesn't change on restart)
        idx = zlib.adler32(tag_name.encode('utf-8')) % len(palette)
        return palette[idx]

    def update_time_span(self):
        val = self.ui.plotTimeSpanSlider.value()
        # Logarithmic slider: 10s to 1 hour
        self.time_span_seconds = 10 * (360 ** (val / 1000))

        # Update Label
        if hasattr(self.ui, 'plotTimeSpanLabel'):
            if self.time_span_seconds < 60:
                txt = f"{int(self.time_span_seconds)}s"
            else:
                m = int(self.time_span_seconds // 60)
                s = int(self.time_span_seconds % 60)
                txt = f"{m}m{s:02d}s"
            self.ui.plotTimeSpanLabel.setText(txt)

    def save_data(self):
        """Saves current view to CSV/PNG."""
        folder = self.ui.line_edit_folder.text().strip()
        name = self.ui.line_edit_filename.text().strip()

        if not folder or not name: return
        os.makedirs(folder, exist_ok=True)

        ts_str = time.strftime('%Y%m%d_%H%M%S')
        base = os.path.join(folder, f"{ts_str}_{name}")

        # 1. Save CSV (Fixed to use get_snapshot)
        try:
            # Grab full buffer history
            times, data_dict = self.history.get_snapshot()

            # Convert to list of rows for CSV
            active_cols = [c for c in self.channels.keys() if c in self.active_channels]

            with open(f"{base}.csv", 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(["Timestamp", "Unix"] + active_cols)

                # Iterate data
                for i, t in enumerate(times):
                    row = [datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M:%S.%f'), t]
                    for col in active_cols:
                        raw_tag = self.channels[col]['dataSource']
                        # Safety check for index
                        if raw_tag in data_dict and i < len(data_dict[raw_tag]):
                            row.append(data_dict[raw_tag][i])
                        else:
                            row.append("")
                    writer.writerow(row)
            print(f"Saved CSV: {base}.csv")

        except Exception as e:
            QMessageBox.critical(self.ui, "Error", f"CSV Save Failed:\n{e}")
            return

        # 2. Save PNG
        try:
            self._save_plot_image(f"{base}.png")
            QMessageBox.information(self.ui, "Saved", f"Files saved to:\n{base}")
        except Exception as e:
            print(f"PNG Error: {e}")

    def _save_plot_image(self, filepath):
        scale = 2.0
        img = QImage(self.container.size() * scale, QImage.Format_ARGB32)
        img.fill(Qt.white)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        p.scale(scale, scale)
        self.container.render(p)
        p.end()
        img.save(filepath)

    def browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self.ui, "Select Folder", self.ui.line_edit_folder.text())
        if folder: self.ui.line_edit_folder.setText(folder)