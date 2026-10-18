"""Plot lanes (one pyqtgraph plot per lane), their axes and the curves drawn on them."""
import numpy as np
import zlib
import pyqtgraph as pg

from PyQt6.QtWidgets import QLabel
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QSizePolicy
from src.core.live_buffer import break_gaps
from src.gui.data_viewer.viewer_widgets import LogAxisItem, TimeAxisItem


class LanesMixin:
    """Plot lanes (one pyqtgraph plot per lane), their axes and the curves drawn on them. Mixed into DataViewerApp (viewer_main.py)."""

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
        self.event_overlay.clear()

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
            self.layout_widget.addLabel(prompt_text, row=0, col=0)
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
