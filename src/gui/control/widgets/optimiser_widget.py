"""Optimiser panel: a 2D raster of two setpoints against a beam reading, shown as a heat map.

The sweep runs in the sequencer service (src/sequencer/optimiser.py), which holds both
setpoints for it and puts them back afterwards; this panel starts and stops it and draws the
map as the points come in. On the map: drag a box to sweep that region again (finer);
double-click to end a running sweep at that point, or (when idle) to go there."""
import json
import logging

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QEvent, QRectF
from PyQt6.QtWidgets import (QCheckBox, QDoubleSpinBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QMessageBox,
                             QPushButton, QVBoxLayout, QWidget)

from src.core import hard_caps, paths
from src.core.commands import route_for_tag
from src.gui import theme
from src.gui.control.dialogs.optimiser_tag_dialog import OptimizerTagSelectorDialog
from src.gui.control.sequencer_link import OFFLINE_TEXT, SequencerSlot
from src.gui.style import set_tone
from src.sequencer import protocol as P
from src.sequencer.optimiser import DEFAULT_RB_TAG, restore_tags

log = logging.getLogger("hmi.optimiser")
LIVE_MARK = "#00FFFF"       # the live point: drawn on the heat map's colours, so the same in both themes


def subsystem_of(tag: str, data: dict):
    """The subsystem a tag belongs to: the longest prefix with a published permit."""
    parts = tag.split(".")
    for n in range(len(parts) - 1, 0, -1):
        sub = ".".join(parts[:n])
        if f"{sub}.permit" in data:
            return sub
    return None


class ParameterOptimizerWidget(QWidget):
    def __init__(self, cmd_thread, event_helper=None):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.event_helper = event_helper
        self.slot = SequencerSlot(cmd_thread, P.OPTIMISER)
        self.follower = self.slot.follower()
        self.registry = self._load_registry()
        self.rb_metric_tag = DEFAULT_RB_TAG
        self.x_tag = self.x_meta = self.y_tag = self.y_meta = None
        self.heatmap_data = None
        self.map_cfg = None               # settings of the sweep on the map
        self._run_shown = None
        self._tickets = {}
        self._was_active = None
        self._data = {}
        self.main_layout = QVBoxLayout(self)
        self._init_ui()

    @staticmethod
    def _load_registry() -> dict:
        try:
            with open(paths.REGISTRY_PATH, "r") as f:
                return json.load(f)
        except (OSError, ValueError) as e:
            log.error("Failed to load registry: %s", e)
            return {}

    # ================================================================== UI
    def _create_axis_config_ui(self, axis_label: str):
        group = QGroupBox(f"{axis_label} axis")
        layout = QGridLayout()
        btn_select = QPushButton(f"Select the {axis_label}-axis parameter...")
        layout.addWidget(btn_select, 0, 0, 1, 4)
        boxes = []
        for prefix, decimals in (("Start: ", 3), ("Stop: ", 3), ("Steps: ", 0)):
            box = QDoubleSpinBox()
            box.setPrefix(prefix)
            box.setDecimals(decimals)
            box.setKeyboardTracking(False)
            boxes.append(box)
        sp_start, sp_stop, sp_steps = boxes
        sp_steps.setRange(2, 500)
        sp_steps.setValue(20)
        layout.addWidget(sp_start, 1, 0, 1, 2)
        layout.addWidget(sp_stop, 1, 2, 1, 2)
        layout.addWidget(sp_steps, 2, 0, 1, 4)
        group.setLayout(layout)
        return group, btn_select, sp_start, sp_stop, sp_steps

    def _init_ui(self):
        ctrl_layout = QVBoxLayout()
        x_group, self.btn_x, self.sp_x_start, self.sp_x_stop, self.sp_x_steps = self._create_axis_config_ui("X")
        y_group, self.btn_y, self.sp_y_start, self.sp_y_stop, self.sp_y_steps = self._create_axis_config_ui("Y")
        self.btn_x.clicked.connect(lambda: self._open_selector("X"))
        self.btn_y.clicked.connect(lambda: self._open_selector("Y"))
        axes_layout = QHBoxLayout()
        axes_layout.addWidget(x_group)
        axes_layout.addWidget(y_group)
        ctrl_layout.addLayout(axes_layout)

        exec_group = QGroupBox("Execution")
        exec_layout = QHBoxLayout()
        self.sp_dwell = QDoubleSpinBox()
        self.sp_dwell.setPrefix("Dwell Time: ")
        self.sp_dwell.setSuffix(" s")
        self.sp_dwell.setDecimals(2)
        self.sp_dwell.setRange(0.10, 60.0)
        self.sp_dwell.setValue(0.25)
        self.cb_log_z = QCheckBox("Log Scale (Z)")
        self.cb_log_z.toggled.connect(self._update_image_render)
        self.btn_start = QPushButton("START 2D RASTER SWEEP")
        set_tone(self.btn_start, "primary")
        self.btn_start.setEnabled(False)
        self.btn_start.clicked.connect(self._start_optimization)
        self.btn_stop = QPushButton("ABORT")
        set_tone(self.btn_stop, "danger")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_optimization)
        for w in (self.sp_dwell, self.cb_log_z, self.btn_start, self.btn_stop):
            exec_layout.addWidget(w)
        exec_group.setLayout(exec_layout)
        ctrl_layout.addWidget(exec_group)
        self.lbl_status = QLabel("Status: Idle")
        self.lbl_status.setWordWrap(True)
        ctrl_layout.addWidget(self.lbl_status)
        self.main_layout.addLayout(ctrl_layout)

        self.glw = pg.GraphicsLayoutWidget()
        self.plot_widget = self.glw.addPlot(title="Beam Current Heat Map (A)")
        self.plot_widget.setLabel('bottom', "X-Axis Parameter")
        self.plot_widget.setLabel('left', "Y-Axis Parameter")
        self.plot_widget.setMouseEnabled(x=True, y=True)
        self.plot_widget.setMenuEnabled(False)
        self.plot_widget.hideButtons()
        vb = self.plot_widget.getViewBox()
        vb.setMouseMode(pg.ViewBox.RectMode)          # drag a box: sweep that region
        vb.wheelEvent = lambda event: None            # no scroll zoom (it would start sweeps by accident)
        vb.sigRangeChangedManually.connect(self._on_hone_in_dragged)
        self.image_item = pg.ImageItem()
        colormap = pg.colormap.get('plasma')
        self.image_item.setColorMap(colormap)
        self.plot_widget.addItem(self.image_item)
        self.live_crosshair = pg.ScatterPlotItem(symbol='+', size=16, pen=pg.mkPen(LIVE_MARK, width=1.0))
        self.live_crosshair.hide()
        self.plot_widget.addItem(self.live_crosshair)
        self.color_bar = pg.ColorBarItem(interactive=False, colorMap=colormap, width=12)
        self.color_bar.setImageItem(self.image_item)
        self.glw.addItem(self.color_bar)
        self.hover_text = pg.TextItem()
        self.hover_text.setZValue(100)
        self.hover_text.hide()
        self.plot_widget.addItem(self.hover_text)
        theme.style_plot(self.glw, restyle=self._restyle)
        self.plot_widget.scene().sigMouseMoved.connect(self._on_mouse_moved)
        self.plot_widget.scene().sigMouseClicked.connect(self._on_mouse_clicked)
        self.main_layout.addWidget(self.glw)
        self.glw.installEventFilter(self)

    def _restyle(self, _plot=None):
        """Theme colours for the hover box (the plot itself is styled by theme.style_plot)."""
        t = theme.tokens()
        self.hover_text.fill = pg.mkBrush(theme.qcolor(t["plot_legend_bg"]))
        self.hover_text.border = pg.mkPen(theme.qcolor(t["border"]))
        self.hover_text.setColor(theme.qcolor(t["text"]))
        self.hover_text.update()

    def eventFilter(self, obj, event):
        if obj == self.glw and event.type() == QEvent.Type.Leave:
            self.hover_text.hide()
        return super().eventFilter(obj, event)

    def _open_selector(self, axis: str):
        dialog = OptimizerTagSelectorDialog(self.registry, axis, self)
        if dialog.exec():
            self.set_axis(axis, dialog.selected_tag)

    def set_axis(self, axis: str, tag: str):
        meta = self.registry.get(tag, {})
        if axis == "X":
            self.x_tag, self.x_meta = tag, meta
            self._apply_target_constraints(meta, self.btn_x, self.sp_x_start, self.sp_x_stop, "X", tag)
            self.plot_widget.setLabel('bottom', meta.get("default_label", "X-Axis"), units=meta.get("unit", ""))
        else:
            self.y_tag, self.y_meta = tag, meta
            self._apply_target_constraints(meta, self.btn_y, self.sp_y_start, self.sp_y_stop, "Y", tag)
            self.plot_widget.setLabel('left', meta.get("default_label", "Y-Axis"), units=meta.get("unit", ""))

    @staticmethod
    def _apply_target_constraints(meta, btn, sp_start, sp_stop, axis_label, tag=""):
        unit = meta.get("unit", "")
        btn.setText(f"{axis_label}: {meta.get('default_label', 'Unknown')} [{unit}]")
        lo, hi = float(meta["min_val"]), float(meta["max_val"])
        for box, value in ((sp_start, lo), (sp_stop, hi)):
            box.setRange(lo, hi)
            box.setValue(value)
            box.setSuffix(f" {unit}")
            box.setToolTip(hard_caps.note(tag))

    # ================================================================== the sweep (sequencer)
    def sweep_settings(self) -> dict:
        return {"x_tag": self.x_tag, "x_start": self.sp_x_start.value(), "x_stop": self.sp_x_stop.value(),
                "x_steps": int(self.sp_x_steps.value()), "y_tag": self.y_tag, "y_start": self.sp_y_start.value(),
                "y_stop": self.sp_y_stop.value(), "y_steps": int(self.sp_y_steps.value()),
                "dwell": self.sp_dwell.value(), "rb_tag": self.rb_metric_tag}

    def _start_optimization(self):
        if not self.x_tag or not self.y_tag:
            return
        cfg = self.sweep_settings()
        if cfg["x_start"] == cfg["x_stop"] or cfg["y_start"] == cfg["y_stop"]:
            QMessageBox.warning(self, "Invalid Parameters", "Start and Stop values cannot be identical.")
            return
        self.btn_start.setEnabled(False)
        self.lbl_status.setText("Status: asking the sequencer to start the sweep...")
        self._tickets["start"] = self.slot.send("start", cfg)

    def _stop_optimization(self):
        self.btn_stop.setEnabled(False)
        self._tickets["stop"] = self.slot.send("stop")

    def _check_tickets(self):
        for verb, ticket in list(self._tickets.items()):
            if ticket.done():
                del self._tickets[verb]
                if not ticket.ok:
                    self.lbl_status.setText(f"Status: {verb} refused - {ticket.reason}")

    def update_telemetry(self, data: dict):
        self._data = data
        self.slot.update(data)
        self._check_tickets()
        active = self.slot.active
        run = self.slot.get("run")
        if run is not None and run != self._run_shown and (active or self._run_shown is None):
            self._new_map(run, self.slot.get("config") or {})
        if self._run_shown is not None and run == self._run_shown:
            self._follow_points()
        if self.slot.online and (active or self.slot.state in P.FINAL) and run == self._run_shown:
            self.lbl_status.setText(f"Status: {self.slot.get('status') or self.slot.state}")
        if active != self._was_active:
            self._was_active = active
            self._lock_ui(active)
        self._update_crosshair(data, active)
        if not active:
            self._update_start_permit(data)

    def _new_map(self, run, cfg):
        self._run_shown = run
        self.map_cfg = cfg
        try:
            nx, ny = int(cfg["x_steps"]), int(cfg["y_steps"])
            x0, x1, y0, y1 = (float(cfg[k]) for k in ("x_start", "x_stop", "y_start", "y_stop"))
        except (KeyError, TypeError, ValueError):
            self.heatmap_data = None
            return
        self.heatmap_data = np.full((nx, ny), np.nan)
        self.image_item.setImage(self.heatmap_data, autoLevels=False, levels=(0.0, 1.0))
        self.image_item.setRect(QRectF(x0, y0, x1 - x0, y1 - y0))
        self.plot_widget.setLimits(xMin=None, xMax=None, yMin=None, yMax=None)
        self.plot_widget.setLimits(xMin=min(x0, x1), xMax=max(x0, x1), yMin=min(y0, y1), yMax=max(y0, y1))
        self.plot_widget.setXRange(x0, x1, padding=0)
        self.plot_widget.setYRange(y0, y1, padding=0)
        for axis, tag in (("X", cfg.get("x_tag")), ("Y", cfg.get("y_tag"))):
            meta = self.registry.get(tag, {})
            self.plot_widget.setLabel('bottom' if axis == "X" else 'left', meta.get("default_label", tag or axis),
                                      units=meta.get("unit", ""))

    def _follow_points(self):
        changes = self.follower.update(self.slot.get("run"), self.slot.get("pass"), self.slot.get("points"),
                                       self.slot.get("tail"))
        if self.heatmap_data is None or not changes:
            return
        for kind, points in changes:
            if kind in ("new", "replace"):
                self.heatmap_data[:] = np.nan
            for i, j, value in points or []:
                if 0 <= int(i) < self.heatmap_data.shape[0] and 0 <= int(j) < self.heatmap_data.shape[1]:
                    self.heatmap_data[int(i), int(j)] = value
        self._update_image_render()

    def _on_data_point(self, x_idx: int, y_idx: int, val: float):
        self.heatmap_data[x_idx, y_idx] = val
        self._update_image_render()

    def _update_image_render(self):
        if self.heatmap_data is None:
            return
        shown = np.copy(self.heatmap_data)
        if self.cb_log_z.isChecked():
            with np.errstate(divide='ignore', invalid='ignore'):
                positive = shown > 0
                if not np.any(positive):
                    return
                logged = np.full_like(shown, np.nan)
                logged[positive] = np.log10(shown[positive])
                shown = logged
        if np.all(np.isnan(shown)):
            return
        z_min, z_max = float(np.nanmin(shown)), float(np.nanmax(shown))
        if z_min == z_max:
            z_max = z_min + 1e-12
        self.image_item.setImage(shown, autoLevels=False, levels=(z_min, z_max))
        self.color_bar.setLevels((z_min, z_max))

    def _lock_ui(self, is_running: bool):
        for w in (self.btn_x, self.btn_y, self.sp_x_start, self.sp_x_stop, self.sp_x_steps, self.sp_y_start,
                  self.sp_y_stop, self.sp_y_steps, self.sp_dwell):
            w.setEnabled(not is_running)
        self.btn_stop.setEnabled(is_running)
        self.btn_start.setEnabled(not is_running and self.x_tag is not None and self.y_tag is not None)

    def _update_start_permit(self, data):
        reason = ""
        if self.x_tag is None or self.y_tag is None:
            reason = "Select both X and Y parameters to sweep."
        elif not self.slot.online:
            reason = OFFLINE_TEXT
        else:
            for tag in (self.x_tag, self.y_tag):
                sub = subsystem_of(tag, data)
                if sub is None:
                    reason = f"No live data from the service that drives {tag}."
                    break
                if not data.get(f"{sub}.permit"):
                    reason = f"{sub.split('.')[-1]}: {data.get(f'{sub}.permit_reason') or 'not available'}"
                    break
                enabled = data.get(f"{sub}.stat_enabled")
                if f"{sub}.stat_enabled" in self.registry and not enabled:
                    reason = "Hardware output for selected target is disabled."
                    break
        self.btn_start.setEnabled(not reason and "start" not in self._tickets)
        self.btn_start.setToolTip(f"Disabled: {reason}" if reason else "Click to begin 2D Raster Sweep")

    def _live_value(self, tag, data):
        for t in restore_tags(tag):
            if data.get(t) is not None:
                return data[t]
        return None

    def _update_crosshair(self, data, active):
        if active or not (self.x_tag and self.y_tag) or self.heatmap_data is None:
            self.live_crosshair.hide()
            return
        x, y = self._live_value(self.x_tag, data), self._live_value(self.y_tag, data)
        if x is None or y is None:
            self.live_crosshair.hide()
        else:
            self.live_crosshair.setData([x], [y])
            self.live_crosshair.show()

    # ================================================================== mouse
    def _grid(self):
        cfg = self.map_cfg or {}
        shape = self.heatmap_data.shape
        return (np.linspace(float(cfg.get("x_start", 0)), float(cfg.get("x_stop", 1)), shape[0]),
                np.linspace(float(cfg.get("y_start", 0)), float(cfg.get("y_stop", 1)), shape[1]))

    def _on_mouse_moved(self, pos):
        if self.heatmap_data is None or not self.plot_widget.vb.sceneBoundingRect().contains(pos):
            self.hover_text.hide()
            return
        point = self.plot_widget.vb.mapSceneToView(pos)
        x_val, y_val = point.x(), point.y()
        xs, ys = self._grid()
        z = self.heatmap_data[int(np.abs(xs - x_val).argmin()), int(np.abs(ys - y_val).argmin())]
        if np.isnan(z):
            self.hover_text.hide()
            return
        cfg = self.map_cfg or {}
        x_unit = self.registry.get(cfg.get("x_tag"), {}).get("unit", "")
        y_unit = self.registry.get(cfg.get("y_tag"), {}).get("unit", "")
        z_str = "0.0 A" if z == 0.0 else (f"{z:.2e} A" if abs(z) < 0.01 else f"{z:.4f} A")
        t = theme.tokens()
        self.hover_text.setHtml(f"<div style='text-align: left; color: {t['text']};'><b>X:</b> {x_val:.2f} {x_unit}<br>"
                                f"<b>Y:</b> {y_val:.2f} {y_unit}<br><b>I: {z_str}</b><br>"
                                f"<span style='color: {t['text_muted']}; font-size: 8pt;'>Double-click to set "
                                f"point</span></div>")
        self.hover_text.setAnchor((1.0 if x_val > (xs[0] + xs[-1]) / 2 else 0.0,
                                   0.0 if y_val > (ys[0] + ys[-1]) / 2 else 1.0))
        self.hover_text.setPos(x_val, y_val)
        self.hover_text.show()

    def _on_mouse_clicked(self, evt):
        if not evt.double() or self.heatmap_data is None:
            return
        evt.accept()
        pos = evt.scenePos()
        if self.plot_widget.vb.sceneBoundingRect().contains(pos):
            point = self.plot_widget.vb.mapSceneToView(pos)
            self.go_to(point.x(), point.y())

    def go_to(self, x_val: float, y_val: float):
        """During a sweep: end it at this point (instead of restoring). Otherwise: set both now."""
        if self.slot.active:
            self._tickets["goto"] = self.slot.send("goto", {"x": x_val, "y": y_val})
            self.lbl_status.setText(f"Ending the sweep at X={x_val:.2f}, Y={y_val:.2f}...")
            return
        cfg = self.map_cfg or {}
        x_tag, y_tag = cfg.get("x_tag") or self.x_tag, cfg.get("y_tag") or self.y_tag
        if not x_tag or not y_tag:
            return
        self.cmd_thread.send_command(route_for_tag(x_tag), x_tag, float(x_val), origin="hmi")
        self.cmd_thread.send_command(route_for_tag(y_tag), y_tag, float(y_val), origin="hmi")
        self.lbl_status.setText(f"Manual Jump to X={x_val:.2f}, Y={y_val:.2f}")

    def _on_hone_in_dragged(self, *args):
        if self.slot.active or self.heatmap_data is None:
            return
        (x0, x1), (y0, y1) = self.plot_widget.viewRange()
        if abs(x1 - x0) < 1e-6 or abs(y1 - y0) < 1e-6:     # an accidental click, not a box
            return
        for box, value in ((self.sp_x_start, x0), (self.sp_x_stop, x1), (self.sp_y_start, y0), (self.sp_y_stop, y1)):
            box.setValue(value)
        self.lbl_status.setText("Honing in on selected region...")
        self._start_optimization()
