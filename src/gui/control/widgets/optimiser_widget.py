import time
import json
import os
import numpy as np
import pyqtgraph as pg
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QGridLayout, QLabel, QPushButton,
    QGroupBox, QDoubleSpinBox, QMessageBox, QDialog, QTreeWidget,
    QTreeWidgetItem, QDialogButtonBox, QHBoxLayout, QCheckBox
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QRectF, QEvent


class Optimizer2DWorker(QThread):
    data_point = pyqtSignal(int, int, float)
    scan_finished = pyqtSignal()
    status_msg = pyqtSignal(str)

    def __init__(self, cmd_thread, get_telemetry_cb,
                 x_service, x_tag, x_mode_tag, x_start, x_stop, x_steps,
                 y_service, y_tag, y_mode_tag, y_start, y_stop, y_steps,
                 rb_tag, dwell, restore_x, restore_y):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.get_telemetry_cb = get_telemetry_cb

        self.x_service = x_service
        self.x_tag = x_tag
        self.x_mode_tag = x_mode_tag
        self.x_start = x_start
        self.x_stop = x_stop
        self.x_steps = x_steps

        self.y_service = y_service
        self.y_tag = y_tag
        self.y_mode_tag = y_mode_tag
        self.y_start = y_start
        self.y_stop = y_stop
        self.y_steps = y_steps

        self.rb_tag = rb_tag
        self.dwell = dwell

        self.restore_x = restore_x
        self.restore_y = restore_y
        self.override_x = None
        self.override_y = None

        self.is_stopped = False

    def run(self):
        self.status_msg.emit("Seizing Control (Mode=1)...")
        self.cmd_thread.send_command(self.x_service, self.x_mode_tag, 1, origin="optimizer")
        if self.x_mode_tag != self.y_mode_tag:
            self.cmd_thread.send_command(self.y_service, self.y_mode_tag, 1, origin="optimizer")
        time.sleep(0.5)

        self.status_msg.emit("Executing 2D Raster Sweep...")
        x_points = np.linspace(self.x_start, self.x_stop, self.x_steps)
        y_points = np.linspace(self.y_start, self.y_stop, self.y_steps)

        for i, val_x in enumerate(x_points):
            if self.is_stopped: break

            self.cmd_thread.send_command(self.x_service, self.x_tag, float(val_x), origin="optimizer")
            time.sleep(0.2)

            for j, val_y in enumerate(y_points):
                if self.is_stopped: break

                self.cmd_thread.send_command(self.y_service, self.y_tag, float(val_y), origin="optimizer")

                start_t = time.time()
                while time.time() - start_t < self.dwell:
                    if self.is_stopped: break
                    time.sleep(0.05)

                if self.is_stopped: break

                rb_val = self.get_telemetry_cb(self.rb_tag)
                self.data_point.emit(i, j, float(rb_val))

        target_x = self.override_x if self.override_x is not None else self.restore_x
        target_y = self.override_y if self.override_y is not None else self.restore_y

        status_txt = "Jumping to override coords..." if self.override_x is not None else "Restoring pre-scan values..."
        self.status_msg.emit(status_txt)

        self.cmd_thread.send_command(self.x_service, self.x_tag, float(target_x), origin="optimizer")
        self.cmd_thread.send_command(self.y_service, self.y_tag, float(target_y), origin="optimizer")
        time.sleep(0.5)

        self.status_msg.emit("Releasing Control (Mode=0)...")
        self.cmd_thread.send_command(self.x_service, self.x_mode_tag, 0, origin="optimizer")
        if self.x_mode_tag != self.y_mode_tag:
            self.cmd_thread.send_command(self.y_service, self.y_mode_tag, 0, origin="optimizer")

        if not self.is_stopped and self.override_x is None:
            self.status_msg.emit("Optimization Complete.")

        self.scan_finished.emit()

    def stop(self):
        self.is_stopped = True


class OptimizerTagSelectorDialog(QDialog):
    def __init__(self, registry: dict, axis_label: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Select {axis_label}-Axis Sweep Parameter")
        self.resize(500, 500)
        self.registry = registry
        self.selected_tag = None
        self.selected_meta = None

        layout = QVBoxLayout(self)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["ISA-95 Hierarchy / Parameter", "Unit"])
        self.tree.setColumnWidth(0, 350)
        layout.addWidget(self.tree)

        self.tree.itemDoubleClicked.connect(self._on_double_click)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept_selection)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._populate_tree()

    def _populate_tree(self):
        folder_nodes = {}
        for full_tag, meta in self.registry.items():
            if not meta.get("auto_controllable") or meta.get("min_val") is None or meta.get("max_val") is None:
                continue

            parts = full_tag.split('.')
            parent_node = self.tree.invisibleRootItem()
            current_path = ""

            for part in parts[:-1]:
                current_path = f"{current_path}.{part}" if current_path else part
                if current_path not in folder_nodes:
                    node = QTreeWidgetItem(parent_node)
                    node.setText(0, part.replace('_', ' ').title())
                    folder_nodes[current_path] = node
                parent_node = folder_nodes[current_path]

            item = QTreeWidgetItem(parent_node)
            item.setText(0, meta.get("default_label", parts[-1]))
            item.setText(1, meta.get("unit", ""))
            item.setData(0, Qt.ItemDataRole.UserRole, {"tag": full_tag, "meta": meta})

        self.tree.collapseAll()
        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setExpanded(True)

    def _on_double_click(self, item, column):
        self.accept_selection()

    def accept_selection(self):
        items = self.tree.selectedItems()
        if items and items[0].data(0, Qt.ItemDataRole.UserRole):
            data = items[0].data(0, Qt.ItemDataRole.UserRole)
            self.selected_tag = data["tag"]
            self.selected_meta = data["meta"]
            self.accept()
        else:
            self.reject()


class ParameterOptimizerWidget(QWidget):
    def __init__(self, cmd_thread, get_telemetry_cb):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.get_telemetry_cb = get_telemetry_cb

        self.config_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../config"))
        self.registry = self._load_registry()

        self.rb_metric_tag = "ion_beam.beamline.faraday.smu.rb_current"

        self.x_tag = None
        self.x_meta = None
        self.y_tag = None
        self.y_meta = None

        self.scan_worker = None
        self.heatmap_data = None

        self.route_map = {
            "service_plc_snap7": "plc",
            "service_magnet": "magnet",
            "service_spellman": "spellman",
            "service_vacuum": "vacuum",
            "service_source_turbo": "source_turbo",
            "service_faraday_smu": "smu"
        }

        self.main_layout = QVBoxLayout(self)
        self._init_ui()

    def _load_registry(self) -> dict:
        try:
            path = os.path.join(self.config_dir, "system_tags.json")
            with open(path, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"[Optimizer] Failed to load registry: {e}")
            return {}

    def _create_axis_config_ui(self, axis_label: str):
        group = QGroupBox(f"{axis_label}-Axis Configuration")
        layout = QGridLayout()

        btn_select = QPushButton(f"Select {axis_label}-Axis Parameter...")
        btn_select.setStyleSheet("text-align: center; font-style: italic; padding: 6px;")
        layout.addWidget(btn_select, 0, 0, 1, 4)

        sp_start = QDoubleSpinBox()
        sp_start.setPrefix("Start: ")
        sp_start.setDecimals(3)
        sp_start.setKeyboardTracking(False)
        layout.addWidget(sp_start, 1, 0, 1, 2)

        sp_stop = QDoubleSpinBox()
        sp_stop.setPrefix("Stop: ")
        sp_stop.setDecimals(3)
        sp_stop.setKeyboardTracking(False)
        layout.addWidget(sp_stop, 1, 2, 1, 2)

        sp_steps = QDoubleSpinBox()
        sp_steps.setPrefix("Steps: ")
        sp_steps.setDecimals(0)
        sp_steps.setRange(2, 500)
        sp_steps.setValue(20)
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
        exec_layout.addWidget(self.sp_dwell)

        # NEW: Log Scale Toggle
        self.cb_log_z = QCheckBox("Log Scale (Z)")
        self.cb_log_z.toggled.connect(self._update_image_render)
        exec_layout.addWidget(self.cb_log_z)

        self.btn_start = QPushButton("START 2D RASTER SWEEP")
        self.btn_start.setStyleSheet("""
            QPushButton { background-color: #FF9800; color: white; font-weight: bold; }
            QPushButton:disabled { background-color: #555555; color: #888888; }
        """)
        self.btn_start.setEnabled(False)
        self.btn_start.clicked.connect(self._start_optimization)
        exec_layout.addWidget(self.btn_start)

        self.btn_stop = QPushButton("ABORT")
        self.btn_stop.setStyleSheet("""
            QPushButton { background-color: #F44336; color: white; font-weight: bold; }
            QPushButton:disabled { background-color: #555555; color: #888888; }
        """)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_optimization)
        exec_layout.addWidget(self.btn_stop)

        exec_group.setLayout(exec_layout)
        ctrl_layout.addWidget(exec_group)

        self.lbl_status = QLabel("Status: Idle")
        ctrl_layout.addWidget(self.lbl_status)
        self.main_layout.addLayout(ctrl_layout)

        pg.setConfigOption('background', '#1E1E1E')
        pg.setConfigOption('foreground', 'w')

        self.glw = pg.GraphicsLayoutWidget()
        self.plot_widget = self.glw.addPlot(title="Beam Current Heat Map (A)")
        self.plot_widget.setLabel('bottom', "X-Axis Parameter")
        self.plot_widget.setLabel('left', "Y-Axis Parameter")

        # Enable mouse interactions
        self.plot_widget.setMouseEnabled(x=True, y=True)
        self.plot_widget.setMenuEnabled(False)
        self.plot_widget.hideButtons()

        # Configure left-click to drag a bounding box (rubber band zoom)
        self.plot_widget.getViewBox().setMouseMode(pg.ViewBox.RectMode)

        # Disable scroll wheel zooming to prevent accidental rapid-fire sweeps
        self.plot_widget.getViewBox().wheelEvent = lambda event: None

        # Hook into the manual range change (fired when the user finishes dragging the box)
        self.plot_widget.getViewBox().sigRangeChangedManually.connect(self._on_hone_in_dragged)

        self.image_item = pg.ImageItem()
        colormap = pg.colormap.get('plasma')
        self.image_item.setColorMap(colormap)
        self.plot_widget.addItem(self.image_item)

        # Thinner, more precise crosshair
        self.live_crosshair = pg.ScatterPlotItem(
            symbol='+', size=16, pen=pg.mkPen('#00FFFF', width=1.0)
        )
        self.live_crosshair.hide()
        self.plot_widget.addItem(self.live_crosshair)

        self.color_bar = pg.ColorBarItem(interactive=False, colorMap=colormap, width=12)
        self.color_bar.setImageItem(self.image_item)
        self.glw.addItem(self.color_bar)

        self.hover_text = pg.TextItem(fill=pg.mkBrush(0, 0, 0, 200), color='w', border=pg.mkPen('gray'))
        self.hover_text.setZValue(100)  # Guarantee it draws over the image
        self.hover_text.hide()
        self.plot_widget.addItem(self.hover_text)

        self.plot_widget.scene().sigMouseMoved.connect(self._on_mouse_moved)
        self.plot_widget.scene().sigMouseClicked.connect(self._on_mouse_clicked)

        self.main_layout.addWidget(self.glw)
        self.glw.installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj == self.glw and event.type() == QEvent.Type.Leave:
            if hasattr(self, 'hover_text'):
                self.hover_text.hide()
        return super().eventFilter(obj, event)

    def _open_selector(self, axis: str):
        dialog = OptimizerTagSelectorDialog(self.registry, axis, self)
        if dialog.exec():
            if axis == "X":
                self.x_tag = dialog.selected_tag
                self.x_meta = dialog.selected_meta
                self._apply_target_constraints(self.x_meta, self.btn_x, self.sp_x_start, self.sp_x_stop, "X")
                self.plot_widget.setLabel('bottom', self.x_meta.get("default_label", "X-Axis"),
                                          units=self.x_meta.get("unit", ""))
            else:
                self.y_tag = dialog.selected_tag
                self.y_meta = dialog.selected_meta
                self._apply_target_constraints(self.y_meta, self.btn_y, self.sp_y_start, self.sp_y_stop, "Y")
                self.plot_widget.setLabel('left', self.y_meta.get("default_label", "Y-Axis"),
                                          units=self.y_meta.get("unit", ""))

            self._evaluate_start_ready()

    def _apply_target_constraints(self, meta: dict, btn: QPushButton, sp_start: QDoubleSpinBox, sp_stop: QDoubleSpinBox,
                                  axis_label: str):
        readable_name = meta.get("default_label", "Unknown")
        unit = meta.get("unit", "")

        btn.setText(f"{axis_label}: {readable_name} [{unit}]")
        btn.setStyleSheet("text-align: center; font-weight: bold; padding: 6px;")

        min_v = float(meta["min_val"])
        max_v = float(meta["max_val"])

        sp_start.setRange(min_v, max_v)
        sp_start.setValue(min_v)
        sp_start.setSuffix(f" {unit}")

        sp_stop.setRange(min_v, max_v)
        sp_stop.setValue(max_v)
        sp_stop.setSuffix(f" {unit}")

    def _evaluate_start_ready(self):
        if self.x_tag and self.y_tag:
            self.btn_start.setEnabled(True)
            self.btn_start.setToolTip("Click to begin 2D Raster Sweep")

    def _derive_mode_tag(self, target_tag: str) -> str:
        parts = target_tag.split('.')
        base = ".".join(parts[:-1])
        return f"{base}.cmd_ctrl_mode"

    def _derive_rb_tag(self, target_tag: str) -> str:
        return target_tag.replace("sp_requested_", "rb_").replace("cmd_", "rb_")

    def _on_mouse_moved(self, evt):
        if self.heatmap_data is None:
            return

        pos = evt
        # Use vb.sceneBoundingRect() so hovering over axes doesn't trigger the tooltip
        if self.plot_widget.vb.sceneBoundingRect().contains(pos):
            mouse_point = self.plot_widget.vb.mapSceneToView(pos)
            x_val = mouse_point.x()
            y_val = mouse_point.y()

            try:
                shape_x, shape_y = self.heatmap_data.shape
                x_points = np.linspace(self.sp_x_start.value(), self.sp_x_stop.value(), shape_x)
                y_points = np.linspace(self.sp_y_start.value(), self.sp_y_stop.value(), shape_y)

                x_idx = (np.abs(x_points - x_val)).argmin()
                y_idx = (np.abs(y_points - y_val)).argmin()

                z_val = self.heatmap_data[x_idx, y_idx]

                if np.isnan(z_val):
                    self.hover_text.hide()
                    return

                x_unit = self.x_meta.get("unit", "") if self.x_meta else ""
                y_unit = self.y_meta.get("unit", "") if self.y_meta else ""

                z_str = f"{z_val:.2e} A" if (0 < abs(z_val) < 0.01) else f"{z_val:.4f} A"
                if z_val == 0.0: z_str = "0.0 A"

                html_str = f"""
                <div style='text-align: left;'>
                    <b>X:</b> {x_val:.2f} {x_unit}<br>
                    <b>Y:</b> {y_val:.2f} {y_unit}<br>
                    <b>I:</b> <span style='color: #4CAF50;'>{z_str}</span><br>
                    <span style='color: #FF9800; font-size: 8pt;'><i>Double-click to set point</i></span>
                </div>
                """

                midpoint_x = (self.sp_x_start.value() + self.sp_x_stop.value()) / 2.0
                midpoint_y = (self.sp_y_start.value() + self.sp_y_stop.value()) / 2.0

                # Use standard Python tuples to prevent QPointF packing crashes
                anchor_x = 1.0 if x_val > midpoint_x else 0.0
                anchor_y = 0.0 if y_val > midpoint_y else 1.0

                self.hover_text.setAnchor((anchor_x, anchor_y))
                self.hover_text.setHtml(html_str)
                self.hover_text.setPos(x_val, y_val)
                self.hover_text.show()

            except Exception as e:
                print(f"[Hover Tooltip Exception] {e}")
                self.hover_text.hide()
        else:
            self.hover_text.hide()

    def _on_mouse_clicked(self, evt):
        if not evt.double() or self.heatmap_data is None:
            return

        evt.accept()  # <--- ADD THIS LINE HERE

        pos = evt.scenePos()
        if self.plot_widget.vb.sceneBoundingRect().contains(pos):
            mouse_point = self.plot_widget.vb.mapSceneToView(pos)
            x_val = mouse_point.x()
            y_val = mouse_point.y()

            if self.scan_worker and self.scan_worker.isRunning():
                self.scan_worker.override_x = x_val
                self.scan_worker.override_y = y_val
                self.scan_worker.stop()
                self.lbl_status.setText(f"Scan aborted. Jumping to X={x_val:.2f}, Y={y_val:.2f}...")
            else:
                x_target = self.route_map.get(self.x_meta["source"], "plc")
                y_target = self.route_map.get(self.y_meta["source"], "plc")

                self.cmd_thread.send_command(x_target, self.x_tag, float(x_val), origin="hmi")
                self.cmd_thread.send_command(y_target, self.y_tag, float(y_val), origin="hmi")
                self.lbl_status.setText(f"Manual Jump to X={x_val:.2f}, Y={y_val:.2f}")

    def _on_hone_in_dragged(self, *args):
        # Prevent triggering if a scan is already running, or if there's no baseline data yet
        if (self.scan_worker and self.scan_worker.isRunning()) or self.heatmap_data is None:
            return

        # Grab the newly zoomed-in bounds from the ViewBox
        view_rect = self.plot_widget.viewRange()
        new_x_start, new_x_stop = view_rect[0]
        new_y_start, new_y_stop = view_rect[1]

        # Prevent triggering on tiny/zero-width accidental clicks
        if abs(new_x_stop - new_x_start) < 1e-6 or abs(new_y_stop - new_y_start) < 1e-6:
            return

        # Update the UI spinboxes to match this new box
        self.sp_x_start.setValue(new_x_start)
        self.sp_x_stop.setValue(new_x_stop)
        self.sp_y_start.setValue(new_y_start)
        self.sp_y_stop.setValue(new_y_stop)

        # Kick off the new sweep automatically
        self.lbl_status.setText("Honing in on selected region...")
        self._start_optimization()

    def _start_optimization(self):
        if not self.x_tag or not self.y_tag:
            return

        x_start = self.sp_x_start.value()
        x_stop = self.sp_x_stop.value()
        x_steps = int(self.sp_x_steps.value())

        y_start = self.sp_y_start.value()
        y_stop = self.sp_y_stop.value()
        y_steps = int(self.sp_y_steps.value())

        if x_start == x_stop or y_start == y_stop:
            QMessageBox.warning(self, "Invalid Parameters", "Start and Stop values cannot be identical.")
            return

        x_mode_tag = self._derive_mode_tag(self.x_tag)
        y_mode_tag = self._derive_mode_tag(self.y_tag)

        if x_mode_tag not in self.registry or y_mode_tag not in self.registry:
            QMessageBox.critical(self, "Architecture Error", "Cannot find arbitration tags for selected hardware.")
            return

        x_restore = self.get_telemetry_cb(self._derive_rb_tag(self.x_tag))
        y_restore = self.get_telemetry_cb(self._derive_rb_tag(self.y_tag))

        self.heatmap_data = np.full((x_steps, y_steps), np.nan)
        self.image_item.setImage(self.heatmap_data, autoLevels=False, levels=(0.0, 1.0))

        x_width = x_stop - x_start
        y_height = y_stop - y_start
        self.image_item.setRect(QRectF(x_start, y_start, x_width, y_height))

        # Clear existing limits to prevent clamping conflicts during transition
        self.plot_widget.setLimits(xMin=None, xMax=None, yMin=None, yMax=None)

        # Apply the new bounding limits first
        self.plot_widget.setLimits(xMin=min(x_start, x_stop), xMax=max(x_start, x_stop),
                                   yMin=min(y_start, y_stop), yMax=max(y_start, y_stop))

        # Set the viewable ranges
        self.plot_widget.setXRange(x_start, x_stop, padding=0)
        self.plot_widget.setYRange(y_start, y_stop, padding=0)

        self._lock_ui(True)
        self.live_crosshair.hide()

        x_target_service = self.route_map.get(self.x_meta["source"], "plc")
        y_target_service = self.route_map.get(self.y_meta["source"], "plc")

        self.scan_worker = Optimizer2DWorker(
            cmd_thread=self.cmd_thread, get_telemetry_cb=self.get_telemetry_cb,
            x_service=x_target_service, x_tag=self.x_tag, x_mode_tag=x_mode_tag,
            x_start=x_start, x_stop=x_stop, x_steps=x_steps,
            y_service=y_target_service, y_tag=self.y_tag, y_mode_tag=y_mode_tag,
            y_start=y_start, y_stop=y_stop, y_steps=y_steps,
            rb_tag=self.rb_metric_tag, dwell=self.sp_dwell.value(),
            restore_x=x_restore, restore_y=y_restore
        )

        self.scan_worker.data_point.connect(self._on_data_point)
        self.scan_worker.status_msg.connect(self.lbl_status.setText)
        self.scan_worker.scan_finished.connect(self._on_scan_finished)
        self.scan_worker.start()

    def _update_image_render(self):
        if self.heatmap_data is None:
            return

        display_data = np.copy(self.heatmap_data)

        if self.cb_log_z.isChecked():
            # Mask values <= 0 to avoid RuntimeWarnings and invalid calculations
            with np.errstate(divide='ignore', invalid='ignore'):
                valid_mask = display_data > 0
                if not np.any(valid_mask):
                    return  # No positive data to render yet

                log_data = np.full_like(display_data, np.nan)
                log_data[valid_mask] = np.log10(display_data[valid_mask])
                display_data = log_data

        # Abort if all values are NaN
        if np.all(np.isnan(display_data)):
            return

        z_min = float(np.nanmin(display_data))
        z_max = float(np.nanmax(display_data))

        if z_min == z_max:
            z_max = z_min + 1e-12

        self.image_item.setImage(display_data, autoLevels=False, levels=(z_min, z_max))
        self.color_bar.setLevels((z_min, z_max))

    def _on_data_point(self, x_idx: int, y_idx: int, val: float):
        self.heatmap_data[x_idx, y_idx] = val
        self._update_image_render()

        z_min = float(np.nanmin(self.heatmap_data))
        z_max = float(np.nanmax(self.heatmap_data))
        if z_min == z_max: z_max = z_min + 1e-12

        self.image_item.setImage(self.heatmap_data, autoLevels=False, levels=(z_min, z_max))
        self.color_bar.setLevels((z_min, z_max))

    def _stop_optimization(self):
        if self.scan_worker and self.scan_worker.isRunning():
            self.scan_worker.stop()
            self.scan_worker.wait()
        self._lock_ui(False)
        self.lbl_status.setText("Status: Aborted by User.")

    def _on_scan_finished(self):
        self._lock_ui(False)

    def _lock_ui(self, is_running: bool):
        self.btn_x.setEnabled(not is_running)
        self.btn_y.setEnabled(not is_running)
        self.sp_x_start.setEnabled(not is_running)
        self.sp_x_stop.setEnabled(not is_running)
        self.sp_x_steps.setEnabled(not is_running)
        self.sp_y_start.setEnabled(not is_running)
        self.sp_y_stop.setEnabled(not is_running)
        self.sp_y_steps.setEnabled(not is_running)
        self.sp_dwell.setEnabled(not is_running)
        self.btn_stop.setEnabled(is_running)

        self.btn_start.setEnabled((not is_running) and (self.x_tag is not None) and (self.y_tag is not None))

    def _derive_enable_tag(self, target_tag: str) -> str:
        parts = target_tag.split('.')
        base = ".".join(parts[:-1])
        return f"{base}.stat_enabled"

    def update_telemetry(self, data: dict):
        master_comms_lost = not bool(data.get("system.connected", False)) or bool(
            data.get("ion_beam.system.pc_plc_comms_lost", False))
        relay_active = bool(data.get("ion_beam.facilities.safety_relay_active", False))

        is_sweeping = self.scan_worker is not None and self.scan_worker.isRunning()

        if not is_sweeping and self.x_tag and self.y_tag and self.heatmap_data is not None:
            live_x = data.get(self._derive_rb_tag(self.x_tag))
            live_y = data.get(self._derive_rb_tag(self.y_tag))

            if live_x is not None and live_y is not None:
                self.live_crosshair.setData([live_x], [live_y])
                self.live_crosshair.show()
            else:
                self.live_crosshair.hide()
        else:
            self.live_crosshair.hide()

        lock_reason = ""
        if master_comms_lost:
            lock_reason = "PLC communications offline."
        elif not relay_active:
            lock_reason = "Safety Relay is De-Energized."
        else:
            services_status = data.get("manager.services", {})
            service_translation = {
                "service_magnet": "service_magnet_psu",
                "service_plc_snap7": "service_plc",
                "service_faraday_smu": "service_smu",
                "service_spellman": "service_spellman_mpd",
                "service_vacuum": "service_vac_gauge_controllers",
                "service_source_turbo": "service_source_turbo"
            }

            for meta in [self.x_meta, self.y_meta]:
                if meta:
                    target_service = meta["source"]
                    heartbeat_name = service_translation.get(target_service, target_service)
                    if services_status.get(heartbeat_name, "OFFLINE") != "ONLINE":
                        lock_reason = f"Target service ({heartbeat_name}) is offline."
                        break

            if not lock_reason:
                for tag in [self.x_tag, self.y_tag]:
                    if tag:
                        enable_tag = self._derive_enable_tag(tag)
                        if enable_tag in self.registry and not bool(data.get(enable_tag, 0.0)):
                            lock_reason = "Hardware output for selected target is disabled."
                            break

        if is_sweeping and lock_reason:
            self._stop_optimization()

        if not is_sweeping:
            lockout = bool(lock_reason)
            can_start = (not lockout) and (self.x_tag is not None) and (self.y_tag is not None)

            self.btn_start.setEnabled(can_start)

            if self.x_tag is None or self.y_tag is None:
                self.btn_start.setToolTip("Disabled: Select both X and Y parameters to sweep.")
            elif lockout:
                self.btn_start.setToolTip(f"Disabled: {lock_reason}")
            else:
                self.btn_start.setToolTip("Click to begin 2D Raster Sweep")