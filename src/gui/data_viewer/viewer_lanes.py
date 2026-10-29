"""Plot lanes (one pyqtgraph plot per lane), their axes and the curves drawn on them.

Line colours: each lane's channels take the theme's eight series colours in turn (theme.py,
checked for colour-blind readers); a channel keeps its colour while it stays in its lane, so
adding or removing another channel doesn't repaint it. A ninth channel in one lane repeats the
colours dashed (then dotted), so no two lines in a lane look the same.
"""
import time

import numpy as np
import pyqtgraph as pg

from PyQt6.QtWidgets import QHBoxLayout, QLabel, QWidget
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QSizePolicy
from src.core.live_buffer import break_gaps
from src.gui import theme
from src.gui.data_viewer.viewer_widgets import LogAxisItem, TimeAxisItem
from src.gui.style import reserve_width, set_role, swatch
from src.gui.thesis_style import display_unit

SERIES_STYLES = (Qt.PenStyle.SolidLine, Qt.PenStyle.DashLine, Qt.PenStyle.DotLine)

# View > Line width: (name, width in pixels, points per curve). A line wider than one pixel costs Qt
# about ten times as much to draw (measured October 2026, offscreen: 16 curves of 10000 points took
# 13 ms a frame at 1 px and 600 ms at 1.5 px; drawn as separate segments, 150 ms). So the wider ones
# are drawn as segments from fewer points: at 3000 a curve (min and max of each bin kept, about two
# a pixel on a laptop screen), drawn as the viewer draws them, 16 curves take 75-85 ms a frame (27 ms
# thin) and 4 curves 27 ms (8 ms thin) - fine for a few channels, slower to pan with many.
LINE_WIDTHS = (("Thin (fastest)", 1.0, 10000), ("Medium", 1.5, 3000), ("Thick", 2.0, 3000))


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

        # Line colours: kept per channel while it stays in its lane (see the module docstring)
        self._assign_series_slots()

        # Identify which lanes actually have selected tags
        active_lanes = set()
        for tag, cfg in self.plot_config.items():
            if cfg.get('selected'):
                active_lanes.add(cfg.get('lane', 'Lane 1'))

        sorted_lanes = sorted(list(active_lanes))

        if not sorted_lanes:
            # Add a centered prompt to the layout
            t = theme.tokens()
            prompt_text = (
                "<div style='text-align: center;'>"
                f"<span style='color: {t['text_muted']}; font-size: 18pt; font-weight: bold;'>"
                "No channels selected</span><br>"
                f"<span style='color: {t['text_faint']}; font-size: 12pt;'>"
                "Choose some with Configure channels in the sidebar.</span>"
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
            p.showGrid(x=True, y=True, alpha=0.35)
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

            # G. Add Legend (theme colours: _restyle_plots)
            legend = p.addLegend(offset=(10, 10))
            self.lane_legends[lane_name] = legend

            # H. Sync Geometry (Ensures the log axis overlay aligns with the plot)
            p.vb.sigResized.connect(lambda _, vb=right_vb, plot=p: self._sync_specific_viewbox(plot, vb))

            # I. Initialize Inspector Items (Crosshairs & Pins; colours: _restyle_plots)
            v_line = pg.InfiniteLine(angle=90, movable=False)
            v_line.hide()
            p.addItem(v_line, ignoreBounds=True)
            self.v_lines.append(v_line)

            pin_line = pg.InfiniteLine(angle=90, movable=False)
            pin_line.hide()
            p.addItem(pin_line, ignoreBounds=True)
            self.pin_lines.append(pin_line)

            delta_label = pg.TextItem(anchor=(0, 0))
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
        self.sidebar_swatches = {}

        # Generate new static labels: the channel's line colour, its name, its value
        for tag in sorted_lanes:
            for t, cfg in self.plot_config.items():
                if cfg.get('selected') and cfg.get('lane') == tag:
                    val_lbl = QLabel("---")
                    set_role(val_lbl, "readout-sm")
                    reserve_width(val_lbl, "-0.00e-00 mbar")     # scientific notation and units fit
                    val_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    val_lbl.setSizePolicy(QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Preferred)

                    name = QWidget()
                    row = QHBoxLayout(name)
                    row.setContentsMargins(0, 0, 0, 0)
                    row.setSpacing(6)
                    mark = QLabel()
                    name_lbl = QLabel(str(cfg.get('label', t)))
                    set_role(name_lbl, "caption")
                    name_lbl.setWordWrap(True)
                    row.addWidget(mark)
                    row.addWidget(name_lbl, stretch=1)
                    name.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

                    self.value_display_layout.addRow(name, val_lbl)
                    self.sidebar_value_labels[t] = val_lbl
                    self.sidebar_swatches[t] = mark

        self._apply_lane_axis_styles()
        self._restyle_plots()
        self._time_limits = None
        self._apply_time_limits()

    # --- Colours (theme) ---
    def _assign_series_slots(self):
        """Each selected channel's colour slot within its lane: kept while the channel stays in
        that lane; a channel new to a lane takes the lowest free slot."""
        slots = getattr(self, "_series_slots", None)
        if slots is None:
            slots = self._series_slots = {}            # tag -> (lane, slot)
        lanes = {}
        for tag, cfg in self.plot_config.items():
            if cfg.get('selected'):
                lanes.setdefault(cfg.get('lane', 'Lane 1'), []).append(tag)
        kept = {}
        for lane, tags in lanes.items():
            taken = set()
            for tag in tags:
                old = slots.get(tag)
                if old is not None and old[0] == lane and old[1] not in taken:
                    kept[tag] = old
                    taken.add(old[1])
            for tag in sorted(t for t in tags if t not in kept):
                slot = next(i for i in range(len(tags) + len(taken) + 1) if i not in taken)
                kept[tag] = (lane, slot)
                taken.add(slot)
        self._series_slots = kept

    def _series_pen(self, tag: str):
        """The channel's line: its lane slot's theme colour; past eight in one lane, dashed, then dotted;
        as wide as View > Line width says."""
        _lane, slot = getattr(self, "_series_slots", {}).get(tag, (None, 0))
        n = len(theme.tokens()["series"])
        style = SERIES_STYLES[min(slot // n, len(SERIES_STYLES) - 1)]
        return pg.mkPen(color=theme.series_color(slot), width=getattr(self, "line_width", 1.0), style=style)

    def set_line_width(self, name: str):
        """View > Line width: the curves' width, and how many points each is drawn from."""
        _name, width, points = next((w for w in LINE_WIDTHS if w[0] == name), LINE_WIDTHS[0])
        self.line_width, self.RENDER_LIMIT = width, points
        for curves in (getattr(self, "curves", {}), getattr(self, "hist_curves", {})):
            for tag, c in curves.items():
                c.setPen(self._series_pen(tag))
                c.curve.setSegmentedLineMode("on" if width > 1.0 else "auto")
        x, y = getattr(self, "historical_x", None), getattr(self, "historical_y", None)
        if x is not None and y and getattr(self, "lanes", None):
            self._render_curves(self.hist_curves, x, y, is_historical=True)    # (with the new point budget)

    @staticmethod
    def _style_legend(legend):
        theme.style_legend(legend)

    def _restyle_plots(self, _plot=None):
        """The plots in the current theme: curves, legends, crosshair, pins, the sidebar's colour
        swatches and the event markers. Run after building the lanes and after a theme switch
        (theme.style_plot does the axes and backgrounds)."""
        if not getattr(self, "lanes", None):
            if hasattr(self, "lanes") and hasattr(self, "event_overlay") and _plot is not None:
                self._build_lanes()                 # the empty-view prompt, in the new colours
            return
        t = theme.tokens()
        for curves in (self.curves, self.hist_curves):
            for tag, c in curves.items():
                c.setPen(self._series_pen(tag))
        for legend in self.lane_legends.values():
            self._style_legend(legend)
        cross = pg.mkPen(theme.qcolor(t["plot_crosshair"]), width=1, style=Qt.PenStyle.DashLine)
        for v_line in self.v_lines:
            v_line.setPen(cross)
        pin = theme.qcolor(t["plot_pin"])
        for pin_line in self.pin_lines:
            pin_line.setPen(pg.mkPen(pin, width=1.5))
        for label in self.delta_labels:
            label.fill = pg.mkBrush(theme.qcolor(t["plot_legend_bg"]))
            label.setColor(pin)
        for tag, mark in getattr(self, "sidebar_swatches", {}).items():
            try:
                mark.setPixmap(swatch(self._series_pen(tag).color(), 12).pixmap(12, 12))
            except RuntimeError:            # (a label from before a rebuild)
                pass
        overlay = getattr(self, "event_overlay", None)
        if overlay is not None and _plot is not None:
            overlay.clear()                 # redrawn in the new colours
            self._refresh_event_markers(reload=False)

    # How far past the logged data the time axis may go: this fraction of the data's span each
    # side, but at least TIME_MARGIN_MIN_S (so a short or fresh record can still be zoomed out a bit)
    TIME_MARGIN_FRACTION = 0.10
    TIME_MARGIN_MIN_S = 3600.0

    def time_limits(self, now=None):
        """(x_min, x_max) for the time axis, in view coordinates (seconds from t0): the logged data
        (from the logger's file names) plus the live buffer, each side widened by 10 % of their span.
        A quick zoom-out used to run on to a century of empty axis."""
        now = time.time() if now is None else now
        lo = hi = now
        bounds = self.cache.engine.data_bounds()
        if bounds is not None:
            lo, hi = min(lo, bounds[0]), max(hi, bounds[1])
        oldest_live = self.cache.live.oldest_time()
        if oldest_live is not None:
            lo = min(lo, oldest_live)
        margin = max(self.TIME_MARGIN_FRACTION * (hi - lo), self.TIME_MARGIN_MIN_S)
        return lo - margin - self.t0, hi + margin - self.t0

    def _apply_time_limits(self):
        """Hold zooming and panning within time_limits (pyqtgraph's own view limits: the view stops
        at the edge). Re-applied as the record grows - cheaply, only when the limits have moved."""
        if not getattr(self, "lanes", None):
            return
        x_min, x_max = self.time_limits()
        last = getattr(self, "_time_limits", None)
        if last is not None and abs(last[0] - x_min) < 60 and abs(last[1] - x_max) < 60:
            return
        self._time_limits = (x_min, x_max)
        boxes = [p.vb for p in self.lanes.values()] + list(getattr(self, "lane_axes", {}).values())
        for vb in boxes:
            vb.setLimits(xMin=x_min, xMax=x_max, maxXRange=x_max - x_min)

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
        if value is None or not np.isfinite(value):     # no reading (was 'nan pA')
            return "---"

        # 1. Specific Override for Vacuum Pressure (Scientific Notation)
        if base_unit in ("mB", "mbar"):
            # Use 'e' for scientific notation (e.g., 5.30e-07 mbar: the registry's "mB")
            sci_fmt = "+.2e" if is_delta else ".2e"
            return f"{value:{sci_fmt}} mbar"

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

        # 4. Fallback for standard non-scaled units (V, Hz, %, etc.; the registry's degC as °C)
        return f"{value:{fmt}} {display_unit(base_unit)}"

    def _sync_specific_viewbox(self, main_plot, right_viewbox):
        """Forces geometry alignment between a specific lane and its log axis."""
        right_viewbox.setGeometry(main_plot.vb.sceneBoundingRect())
        right_viewbox.linkedViewChanged(main_plot.vb, right_viewbox.XAxis)

    # --- Core Rendering ---

    def _get_distinct_color(self, tag: str):
        """The channel's line colour (its lane slot in the theme's series colours)."""
        return self._series_pen(tag).color()

    def _curve_for(self, target_dict, tag, cfg, lane):
        """Get (or create, add to its lane and legend) the curve for a channel."""
        c = target_dict.get(tag)
        if c is None:
            pen = self._series_pen(tag)
            # clipToView/autoDownsample are off: the viewer already slices to the
            # view and min/max-downsamples, and with them on pyqtgraph re-ran
            # setData on every view change (part of the double-repaint cascade).
            c = pg.PlotDataItem(pen=pen, autoDownsample=False, clipToView=False, connect='finite')
            if getattr(self, "line_width", 1.0) > 1.0:
                c.curve.setSegmentedLineMode("on")      # (a wide line: drawn as segments - LINE_WIDTHS)
            if cfg.get('scale') == 'log':
                self.lane_axes[lane].addItem(c)
            else:
                self.lanes[lane].addItem(c)
            # One legend entry per channel, from whichever curve (live or historical)
            # appears first. Previously only live curves added entries, so with no
            # live data the plots had no legend at all.
            if tag not in self._legend_tags:
                label_text = cfg.get('label', tag)
                unit = display_unit(getattr(self, 'system_registry', {}).get(tag, {}).get("unit", ""))
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
