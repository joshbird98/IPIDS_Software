import sys
import time
from collections import deque
import zmq
import orjson
import pyqtgraph as pg
import queue
import os
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QDockWidget, QWidget,
    QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton, QFrame, QGroupBox, QDoubleSpinBox
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal

from src.core.network_config import (
    ZMQ_PORT_PLC_PUB, ZMQ_PORT_PLC_CMD,
    ZMQ_PORT_VACUUM_PUB,
    ZMQ_PORT_SRC_TURBO_PUB, ZMQ_PORT_SRC_TURBO_CMD
)

# --- SCADA Standard Colors ---
COLOR_OK = "background-color: #4CAF50; color: white; font-weight: bold; border-radius: 4px; padding: 4px;"
COLOR_FAULT = "background-color: #F44336; color: white; font-weight: bold; border-radius: 4px; padding: 4px;"
COLOR_INACTIVE = "background-color: #757575; color: white; font-weight: bold; border-radius: 4px; padding: 4px;"
COLOR_WARNING = "background-color: #FF9800; color: white; font-weight: bold; border-radius: 4px; padding: 4px;"
COLOR_BUTTON_STANDARD = "background-color: #E0E0E0; color: black; font-weight: normal; border-radius: 4px; padding: 6px;"


# --- Global Fault Map Parser ---

class FaultRegistry:
    """Loads and decodes Siemens packed bit structures from fault_map.json."""

    def __init__(self, file_path: str):
        self.fault_structures = {}
        self._load_map(file_path)

    def _load_map(self, file_path: str):
        if not os.path.exists(file_path):
            print(f"[Fault Engine] Critical Error: {file_path} not found.")
            return
        try:
            with open(file_path, "r") as f:
                self.fault_structures = orjson.loads(f.read())
        except Exception as e:
            print(f"[Fault Engine] Failed to parse JSON: {e}")

    def get_active_faults(self, udt_key: str, word_value: int) -> list[dict]:
        """Parses an integer word value against UDT definitions using 32-bit Siemens byte swapping."""
        active_faults = []
        udt_group = self.fault_structures.get(udt_key)
        if not udt_group or word_value == 0:
            return active_faults

        bits_definition = udt_group.get("bits", {})
        for bit_str, info in bits_definition.items():
            try:
                parts = bit_str.split('.')
                x = int(parts[0])
                y = int(parts[1])

                bit_pos = (3 - x) * 8 + y

                if bool(word_value & (1 << bit_pos)):
                    active_faults.append({
                        "name": info["name"],
                        "severity": info["severity"],
                        "description": info["description"]
                    })
            except Exception as e:
                print(f"[Fault Engine] Bit parse error on {bit_str}: {e}")
                continue

        return active_faults


# --- Background Network Workers ---

class ZMQTelemetryThread(QThread):
    data_received = pyqtSignal(dict)

    def __init__(self, ports: list[str]):
        super().__init__()
        self.ports = ports
        self.running = True

    def run(self):
        ctx = zmq.Context.instance()
        sub_socket = ctx.socket(zmq.SUB)
        for port in self.ports:
            sub_socket.connect(port)
        sub_socket.setsockopt(zmq.SUBSCRIBE, b"")

        poller = zmq.Poller()
        poller.register(sub_socket, zmq.POLLIN)

        try:
            while self.running:
                socks = dict(poller.poll(timeout=100))
                if sub_socket in socks and socks[sub_socket] == zmq.POLLIN:
                    try:
                        topic, payload = sub_socket.recv_multipart(flags=zmq.NOBLOCK)
                        self.data_received.emit(orjson.loads(payload))
                    except:
                        continue
        finally:
            sub_socket.setsockopt(zmq.LINGER, 0)
            sub_socket.close()

    def stop(self):
        self.running = False
        self.wait()


class ZMQCommandThread(QThread):
    def __init__(self):
        super().__init__()
        self.cmd_queue = queue.Queue()
        self.running = True

    def send_command(self, subsystem: str, tag: str, value: any):
        self.cmd_queue.put((subsystem, tag, value))

    def run(self):
        ctx = zmq.Context.instance()
        sockets = {
            "plc": ctx.socket(zmq.PUB),
            "src_turbo": ctx.socket(zmq.PUB)
        }
        sockets["plc"].connect(ZMQ_PORT_PLC_CMD)
        sockets["src_turbo"].connect(ZMQ_PORT_SRC_TURBO_CMD)

        try:
            while self.running:
                try:
                    subsystem, tag, value = self.cmd_queue.get(timeout=0.1)
                    payload = {"tag": tag, "value": value, "ts": time.time()}
                    sockets[subsystem].send_json(payload)
                except queue.Empty:
                    continue
                except:
                    continue
        finally:
            for s in sockets.values():
                s.setsockopt(zmq.LINGER, 0)
                s.close()

    def stop(self):
        self.running = False
        self.wait()


# --- Vacuum Subsystem Component ---

class VacuumControlWidget(QWidget):
    def __init__(self, cmd_thread, fault_engine):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.fault_engine = fault_engine

        layout = QVBoxLayout(self)

        # 1. Gauge Display Matrix
        gauge_group = QGroupBox("Vacuum Gauges")
        gauge_layout = QGridLayout()
        self.gauges = {}
        gauge_names = {
            1: "Source (VG1)", 2: "Beamline Pre-Mag (VG2)", 3: "Beamline (VG3)",
            4: "Endstation 1 (VG4)", 5: "Loadlock (VG5)", 6: "Endstation 2 (VG6)"
        }
        for i in range(1, 7):
            frame = QFrame()
            frame.setFrameStyle(QFrame.Shape.StyledPanel | QFrame.Shadow.Raised)
            fl = QVBoxLayout(frame)
            lbl_title = QLabel(f"<b>{gauge_names[i]}</b>")
            lbl_val = QLabel("--- mbar")
            lbl_val.setStyleSheet("font-size: 13pt; font-family: monospace;")
            lbl_status = QLabel("OFFLINE")
            lbl_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl_status.setStyleSheet(COLOR_INACTIVE)
            fl.addWidget(lbl_title)
            fl.addWidget(lbl_val)
            fl.addWidget(lbl_status)
            gauge_layout.addWidget(frame, (i - 1) // 3, (i - 1) % 3)
            self.gauges[i] = {"val": lbl_val, "status": lbl_status}
        gauge_group.setLayout(gauge_layout)
        layout.addWidget(gauge_group)

        # 2. Actuator Controls
        ctrl_layout = QHBoxLayout()

        # Turbopump Subpanel
        turbo_group = QGroupBox("Source Turbo Pump")
        tl = QVBoxLayout()
        self.lbl_turbo_speed = QLabel("Speed: --- Hz")
        self.lbl_turbo_current = QLabel("Current: --- A")
        self.lbl_turbo_status = QLabel("OFFLINE")
        self.lbl_turbo_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_turbo_status.setStyleSheet(COLOR_INACTIVE)

        self.btn_turbo_start = QPushButton("START PUMP")
        self.btn_turbo_start.setStyleSheet(COLOR_BUTTON_STANDARD)
        self.btn_turbo_start.clicked.connect(lambda checked=False: self._cmd_start_turbo())

        tl.addWidget(self.lbl_turbo_speed)
        tl.addWidget(self.lbl_turbo_current)
        tl.addWidget(self.lbl_turbo_status)
        tl.addWidget(self.btn_turbo_start)
        turbo_group.setLayout(tl)
        ctrl_layout.addWidget(turbo_group)

        # Discrete Gate Valve Subpanel
        gv_group = QGroupBox("Source Gate Valve Control")
        gvl = QVBoxLayout()
        self.lbl_gv_status = QLabel("UNKNOWN")
        self.lbl_gv_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_gv_status.setStyleSheet(COLOR_INACTIVE)

        self.lbl_gv_fault = QLabel("")
        self.lbl_gv_fault.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_gv_fault.hide()

        btn_row = QHBoxLayout()
        self.btn_gv_open = QPushButton("OPEN")
        self.btn_gv_open.setStyleSheet(COLOR_BUTTON_STANDARD)
        self.btn_gv_open.clicked.connect(lambda checked=False: self._execute_gv_demand(True))

        self.btn_gv_close = QPushButton("CLOSE")
        self.btn_gv_close.setStyleSheet(COLOR_BUTTON_STANDARD)
        self.btn_gv_close.clicked.connect(lambda checked=False: self._execute_gv_demand(False))

        btn_row.addWidget(self.btn_gv_open)
        btn_row.addWidget(self.btn_gv_close)
        gvl.addWidget(self.lbl_gv_status)
        gvl.addWidget(self.lbl_gv_fault)
        gvl.addLayout(btn_row)
        gv_group.setLayout(gvl)
        ctrl_layout.addWidget(gv_group)

        layout.addLayout(ctrl_layout)
        layout.addStretch()

    def _cmd_start_turbo(self):
        print("[GUI Output] Dispatching -> ion_beam.source.turbo_pump.cmd_enable = True")
        self.cmd_thread.send_command("src_turbo", "ion_beam.source.turbo_pump.cmd_enable", True)

    def _execute_gv_demand(self, open_demand: bool):
        print(f"[GUI Output] Dispatching -> ion_beam.source.chamber.cmd_open_gv = {open_demand}")
        self.cmd_thread.send_command("plc", "ion_beam.source.chamber.cmd_open_gv", open_demand)

    def update_telemetry(self, data: dict):
        try:
            # 1. Gauge Processing Pipeline
            gauge_locations = {1: "source", 2: "beamline", 3: "beamline", 4: "endstation", 5: "loadlock",
                               6: "endstation"}
            for i in range(1, 7):
                loc = gauge_locations[i]
                pressure = data.get(f"ion_beam.{loc}.vacuum_gauge_{i}.pressure")
                status = data.get(f"ion_beam.{loc}.vacuum_gauge_{i}.status")
                rapid_rise = data.get(f"ion_beam.gauges.status.stat_vg{i}_rapid_rise", False)
                not_found = data.get(f"ion_beam.gauges.status.stat_vg{i}_not_found", False)

                if pressure is not None:
                    self.gauges[i]["val"].setText(f"{pressure:.2e} mbar")
                    if status is None: status = 1

                if status is not None:
                    if not_found or status == 0:
                        self.gauges[i]["status"].setText("NOT FOUND")
                        self.gauges[i]["status"].setStyleSheet(COLOR_FAULT)
                    elif rapid_rise:
                        self.gauges[i]["status"].setText("RAPID RISE")
                        self.gauges[i]["status"].setStyleSheet(COLOR_FAULT)
                    elif status == 1:
                        self.gauges[i]["status"].setText("ONLINE")
                        self.gauges[i]["status"].setStyleSheet(COLOR_OK)
                    elif status == 3:
                        self.gauges[i]["status"].setText("SENSOR OFF")
                        self.gauges[i]["status"].setStyleSheet(COLOR_INACTIVE)
                    else:
                        self.gauges[i]["status"].setText(f"FAULT ({status})")
                        self.gauges[i]["status"].setStyleSheet(COLOR_FAULT)

            # 2. Turbopump Processing Pipeline
            t_speed = data.get("ion_beam.source.turbo_pump.speed_hz")
            t_current = data.get("ion_beam.source.turbo_pump.current")
            t_trip = data.get("ion_beam.pump.status.stat_src_turbo_trip", False)
            t_ready = data.get("ion_beam.source.turbo_pump.status_ready", False)
            t_turning = data.get("ion_beam.source.turbo_pump.status_turning", False)

            if t_speed is not None:
                self.lbl_turbo_speed.setText(f"Speed: {t_speed} Hz")
                if t_speed >= 990: t_ready = True
            if t_current is not None:
                self.lbl_turbo_current.setText(f"Current: {t_current:.2f} A")

            if t_trip:
                self.lbl_turbo_status.setText("TRIPPED")
                self.lbl_turbo_status.setStyleSheet(COLOR_FAULT)
                self.btn_turbo_start.setEnabled(True)
                self.btn_turbo_start.setText("RESET & START")
                self.btn_turbo_start.setToolTip("Click to attempt a fault reset and start the pump.")
            elif t_ready:
                self.lbl_turbo_status.setText("AT SPEED")
                self.lbl_turbo_status.setStyleSheet(COLOR_OK)
                self.btn_turbo_start.setEnabled(False)
                self.btn_turbo_start.setText("RUNNING")
                self.btn_turbo_start.setToolTip("Disabled: Pump is already at target speed.")
            elif t_turning:
                self.lbl_turbo_status.setText("ACCELERATING")
                self.lbl_turbo_status.setStyleSheet(COLOR_WARNING)
                self.btn_turbo_start.setEnabled(False)
                self.btn_turbo_start.setText("RUNNING")
                self.btn_turbo_start.setToolTip("Disabled: Pump is currently accelerating.")
            else:
                self.lbl_turbo_status.setText("IDLE")
                self.lbl_turbo_status.setStyleSheet(COLOR_INACTIVE)
                self.btn_turbo_start.setEnabled(True)
                self.btn_turbo_start.setText("START PUMP")
                self.btn_turbo_start.setToolTip("Click to start the Source Turbopump.")

            # 3. Dynamic Fault Map Interception
            raw_sys_fault = data.get("ion_beam.faults.word_0_system")
            gv_fault_active = False

            if raw_sys_fault is not None:
                active_sys_faults = self.fault_engine.get_active_faults("UDT_Fault_Word_0_System", int(raw_sys_fault))
                gv_faults = [f for f in active_sys_faults if "GateValve" in f["name"]]

                if gv_faults:
                    gv_fault_active = True
                    self.lbl_gv_fault.setText(f"FAULT: {gv_faults[0]['name'].upper()}")
                    self.lbl_gv_fault.setStyleSheet(COLOR_FAULT)
                    self.lbl_gv_fault.setVisible(True)
                else:
                    self.lbl_gv_fault.setVisible(False)

            # 4. Gate Valve State & Permission Evaluation
            gv_is_open = data.get("ion_beam.source.chamber.stat_gv_open")
            gv_is_closed = data.get("ion_beam.source.chamber.stat_gv_closed")
            gv_open_perm = bool(data.get("ion_beam.source.chamber.stat_gv_open_permissive", False))
            gv_close_perm = bool(data.get("ion_beam.source.chamber.stat_gv_close_permissive", False))

            if gv_is_open is not None and gv_is_closed is not None:
                gv_is_open = bool(gv_is_open)
                gv_is_closed = bool(gv_is_closed)

                # Determine Badge
                if gv_is_open and not gv_is_closed:
                    self.lbl_gv_status.setText("OPEN")
                    self.lbl_gv_status.setStyleSheet(COLOR_OK)
                elif not gv_is_open and gv_is_closed:
                    self.lbl_gv_status.setText("CLOSED")
                    self.lbl_gv_status.setStyleSheet(COLOR_INACTIVE)
                elif not gv_is_open and not gv_is_closed:
                    self.lbl_gv_status.setText("TRAVELLING")
                    self.lbl_gv_status.setStyleSheet(COLOR_WARNING)
                elif gv_is_open and gv_is_closed:
                    self.lbl_gv_status.setText("SENSOR CONFLICT")
                    self.lbl_gv_status.setStyleSheet(COLOR_FAULT)

                # Dynamic Component-Level Lockouts with Informative Tooltips
                if gv_fault_active:
                    self.btn_gv_open.setEnabled(False)
                    self.btn_gv_close.setEnabled(False)
                    self.btn_gv_open.setText("FAULT LOCKOUT")
                    self.btn_gv_open.setToolTip("Disabled: Gate valve hardware fault detected.")
                    self.btn_gv_close.setToolTip("Disabled: Gate valve hardware fault detected.")
                else:
                    self.btn_gv_open.setText("OPEN")
                    self.btn_gv_open.setEnabled(gv_open_perm and not gv_is_open)
                    self.btn_gv_close.setEnabled(gv_close_perm and not gv_is_closed)

                    self.btn_gv_open.setStyleSheet(
                        COLOR_BUTTON_STANDARD if self.btn_gv_open.isEnabled() else COLOR_INACTIVE)
                    self.btn_gv_close.setStyleSheet(
                        COLOR_BUTTON_STANDARD if self.btn_gv_close.isEnabled() else COLOR_INACTIVE)

                    # Determine exact Open Tooltip
                    if gv_is_open:
                        self.btn_gv_open.setToolTip("Disabled: Gate valve is already open.")
                    elif not gv_open_perm:
                        self.btn_gv_open.setToolTip(
                            "Disabled: Vacuum interlocks not met. Ensure turbo pump is at speed.")
                    else:
                        self.btn_gv_open.setToolTip("Click to command the gate valve OPEN.")

                    # Determine exact Close Tooltip
                    if gv_is_closed:
                        self.btn_gv_close.setToolTip("Disabled: Gate valve is already closed.")
                    elif not gv_close_perm:
                        self.btn_gv_close.setToolTip(
                            "Disabled: Source must be cold (HV, Filament, and Cesium OFF) to close manually.")
                    else:
                        self.btn_gv_close.setToolTip("Click to command the gate valve CLOSED.")

        except Exception as e:
            print(f"[UI Parser Error] Vacuum Runtime exception: {e}")


class IonSourceWidget(QWidget):
    def __init__(self, cmd_thread, fault_engine):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.fault_engine = fault_engine

        # High-frequency data buffers (60 seconds at 10Hz = 600 points)
        self.history_len = 600
        self.time_data = deque(maxlen=self.history_len)
        self.tgt_cur_data = deque(maxlen=self.history_len)
        self.thm_cur_data = deque(maxlen=self.history_len)
        self.start_time = time.time()

        layout = QVBoxLayout(self)

        # Master System Alarm Banner (Hidden by default)
        self.lbl_alarm_banner = QLabel("")
        self.lbl_alarm_banner.setStyleSheet(
            "background-color: #F44336; color: yellow; font-size: 11pt; font-weight: bold; padding: 6px;")
        self.lbl_alarm_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_alarm_banner.hide()
        layout.addWidget(self.lbl_alarm_banner)

        # 1. Master Safety & Shutdown Block
        safety_group = QGroupBox("Master Safety & Sequence Control")
        sl = QGridLayout()

        self.lbl_comms = QLabel("PLC COMMS: OK")
        self.lbl_comms.setStyleSheet(COLOR_OK)
        self.lbl_safety_state = QLabel("SAFETY RELAY: UNKNOWN")
        self.lbl_safety_state.setStyleSheet(COLOR_INACTIVE)
        self.lbl_shutdown_state = QLabel("NORMAL")
        self.lbl_shutdown_state.setStyleSheet(COLOR_OK)

        self.btn_enable_safety = QPushButton("ENABLE SAFETY RELAY")
        self.btn_enable_safety.setStyleSheet("""
                    QPushButton { 
                        background-color: #2196F3; /* Action Blue when clickable */
                        color: white; 
                        font-weight: bold; 
                        border-radius: 4px; 
                        padding: 6px; 
                    }
                    QPushButton:disabled { 
                        background-color: #757575; /* Dull grey when locked out */
                        color: #B0B0B0; 
                        font-weight: bold; 
                    }
                """)
        self.btn_enable_safety.clicked.connect(
            lambda *args: self._dispatch_command("ion_beam.system.cmd_enable_safety", True))

        self.btn_reset_faults = QPushButton("RESET FAULTS")
        self.btn_reset_faults.setStyleSheet(COLOR_BUTTON_STANDARD)
        self.btn_reset_faults.clicked.connect(
            lambda *args: self._dispatch_command("ion_beam.system.cmd_fault_reset", True))

        self.btn_shutdown = QPushButton("INITIATE SHUTDOWN")
        # Explicitly scope the CSS to QPushButton to protect the ToolTip,
        # and add a :disabled state to handle the grey-out effect automatically.
        self.btn_shutdown.setStyleSheet("""
                    QPushButton { background-color: #F44336; color: white; font-weight: bold; border-radius: 4px; padding: 6px; }
                    QPushButton:disabled { background-color: #757575; color: #E0E0E0; font-weight: bold; }
                """)
        self.btn_shutdown.clicked.connect(
            lambda *args: self._dispatch_command("ion_beam.system.cmd_source_shutdown", True))

        self.btn_cancel_shutdown = QPushButton("ABORT SHUTDOWN")
        self.btn_cancel_shutdown.setStyleSheet("""
                    QPushButton { background-color: #FF9800; color: black; font-weight: bold; border-radius: 4px; padding: 6px; }
                    QPushButton:disabled { background-color: #757575; color: #E0E0E0; font-weight: bold; }
                """)
        self.btn_cancel_shutdown.clicked.connect(
            lambda *args: self._dispatch_command("ion_beam.system.cmd_cancel_shutdown", True))

        sl.addWidget(self.lbl_comms, 0, 0)
        sl.addWidget(self.lbl_safety_state, 0, 1)
        sl.addWidget(self.lbl_shutdown_state, 0, 2)
        sl.addWidget(self.btn_enable_safety, 1, 0)
        sl.addWidget(self.btn_reset_faults, 1, 1)
        sl.addWidget(self.btn_shutdown, 1, 2)
        sl.addWidget(self.btn_cancel_shutdown, 1, 3)
        safety_group.setLayout(sl)
        layout.addWidget(safety_group)

        # 2. Beamline Diagnostics
        diag_group = QGroupBox("Beamline Diagnostics")
        dl = QHBoxLayout()
        self.btn_fc = QPushButton("FARADAY CUP")
        self.btn_fc.setCheckable(True)
        self.btn_fc.setStyleSheet(COLOR_BUTTON_STANDARD)
        self.btn_fc.clicked.connect(
            lambda *args, b=self.btn_fc: self._dispatch_command("ion_beam.beamline_diagnostics.cmd_insert_fc",
                                                                b.isChecked()))
        dl.addWidget(self.btn_fc)
        dl.addStretch()
        diag_group.setLayout(dl)
        layout.addWidget(diag_group)

        # 3. Emission Sparkline Plot (Dual Y-Axis)
        plot_group = QGroupBox("Emission Stability")
        pl_layout = QVBoxLayout()
        pg.setConfigOption('background', 'w')
        pg.setConfigOption('foreground', 'k')

        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setLabel('bottom', 'Time', units='s')
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)

        plot_item = self.plot_widget.getPlotItem()

        plot_item.setLabel('left', 'Target Current', units='mA', color='r')
        plot_item.getAxis('left').setPen('r')
        self.tgt_curve = plot_item.plot(pen=pg.mkPen(color='r', width=2), name="Target")

        self.view_right = pg.ViewBox()
        plot_item.scene().addItem(self.view_right)
        self.axis_right = pg.AxisItem('right')

        plot_item.layout.addItem(self.axis_right, 2, 3)
        self.axis_right.linkToView(self.view_right)
        self.axis_right.setLabel('Thermionic Current', units='mA', color='b')
        self.axis_right.setPen('b')
        self.view_right.setXLink(plot_item)

        self.thm_curve = pg.PlotDataItem(pen=pg.mkPen(color='b', width=2), name="Thermionic")
        self.view_right.addItem(self.thm_curve)

        plot_item.getViewBox().sigResized.connect(self._update_dual_axis)

        pl_layout.addWidget(self.plot_widget)
        plot_group.setLayout(pl_layout)
        layout.addWidget(plot_group)

        # 4. PSU Control Grid
        psu_group = QGroupBox("Source Power Supplies")
        self.psu_layout = QGridLayout()
        self.psu_controls = {}

        self._build_psu_row(0, "Extraction", "ion_beam.source.extraction", "kV", 0.0, 20.0, is_voltage=True)
        self._build_psu_row(1, "Target", "ion_beam.source.target", "kV", 0.0, 10.0, is_voltage=True)
        self._build_psu_row(2, "Filament", "ion_beam.source.filament", "A", 0.0, 38.0, is_voltage=False)
        self._build_psu_row(3, "Thermionic", "ion_beam.source.thermionic", "mA", 0.0, 1000.0, is_voltage=False)

        self._build_cs_row(4)

        psu_group.setLayout(self.psu_layout)
        layout.addWidget(psu_group)
        layout.addStretch()

    def _dispatch_command(self, tag: str, value):
        """Debug wrapper to guarantee visibility of outgoing ZMQ commands."""
        print(f"[GUI Output] Dispatching -> {tag} = {value}")
        self.cmd_thread.send_command("plc", tag, value)

    def _update_dual_axis(self):
        """Maintains geometric alignment for the right Y-axis ViewBox."""
        plot_view = self.plot_widget.getPlotItem().getViewBox()
        self.view_right.setGeometry(plot_view.sceneBoundingRect())
        self.view_right.linkedViewChanged(plot_view, self.view_right.XAxis)

    def _build_psu_row(self, row, name, base_tag, unit, min_v, max_v, is_voltage):
        lbl_name = QLabel(f"<b>{name}</b>")
        lbl_rb = QLabel(f"RB: --- {unit}")

        lbl_badge = QLabel("")
        lbl_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_badge.setFixedWidth(100)
        lbl_badge.hide()

        btn_enable = QPushButton("ENABLE")
        btn_enable.setCheckable(True)
        btn_enable.setStyleSheet(COLOR_BUTTON_STANDARD)

        btn_enable.clicked.connect(
            lambda *args, b=btn_enable, t=base_tag: self._dispatch_command(f"{t}.cmd_enable", b.isChecked())
        )

        sp_box = QDoubleSpinBox()
        sp_box.setRange(min_v, max_v)
        sp_box.setSuffix(f" {unit}")
        sp_box.setDecimals(2 if is_voltage else 1)
        sp_box.editingFinished.connect(lambda t=base_tag, b=sp_box, iv=is_voltage:
                                       self._dispatch_command(f"{t}.sp_requested_{'voltage' if iv else 'current'}",
                                                              b.value()))

        self.psu_layout.addWidget(lbl_name, row, 0)
        self.psu_layout.addWidget(lbl_rb, row, 1)
        self.psu_layout.addWidget(sp_box, row, 2)
        self.psu_layout.addWidget(lbl_badge, row, 3)
        self.psu_layout.addWidget(btn_enable, row, 4)

        self.psu_controls[name] = {
            "lbl_rb": lbl_rb, "sp_box": sp_box, "btn_enable": btn_enable,
            "lbl_badge": lbl_badge, "base_tag": base_tag, "unit": unit, "is_voltage": is_voltage
        }

        if name == "Thermionic":
            btn_auto = QPushButton("AUTO EMISSION")
            btn_auto.setCheckable(True)
            btn_auto.setStyleSheet(COLOR_BUTTON_STANDARD)
            btn_auto.clicked.connect(
                lambda *args, b=btn_auto, t=base_tag: self._dispatch_command(f"{t}.cmd_auto_emission", b.isChecked())
            )
            self.psu_layout.addWidget(btn_auto, row, 5)
            self.psu_controls[name]["btn_auto"] = btn_auto

    def _build_cs_row(self, row):
        lbl_name = QLabel("<b>Cesium Oven</b>")
        lbl_rb = QLabel("RB: --- °C")

        lbl_badge = QLabel("")
        lbl_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_badge.setFixedWidth(100)
        lbl_badge.hide()

        btn_enable = QPushButton("HEATER ON")
        btn_enable.setCheckable(True)
        btn_enable.setStyleSheet(COLOR_BUTTON_STANDARD)
        btn_enable.clicked.connect(
            lambda *args, b=btn_enable: self._dispatch_command("ion_beam.source.cesium.cmd_ctrl_mode",
                                                               1 if b.isChecked() else 0))

        btn_cool = QPushButton("FORCED COOLING")
        btn_cool.setCheckable(True)
        btn_cool.setStyleSheet(COLOR_BUTTON_STANDARD)
        btn_cool.clicked.connect(
            lambda *args, b=btn_cool: self._dispatch_command("ion_beam.source.cesium.cmd_force_cooling", b.isChecked()))

        sp_box = QDoubleSpinBox()
        sp_box.setRange(0.0, 200.0)
        sp_box.setSuffix(" °C")
        sp_box.editingFinished.connect(
            lambda: self._dispatch_command("ion_beam.source.cesium.sp_requested_temp", sp_box.value()))

        self.psu_layout.addWidget(lbl_name, row, 0)
        self.psu_layout.addWidget(lbl_rb, row, 1)
        self.psu_layout.addWidget(sp_box, row, 2)
        self.psu_layout.addWidget(lbl_badge, row, 3)
        self.psu_layout.addWidget(btn_enable, row, 4)
        self.psu_layout.addWidget(btn_cool, row, 5)

        self.psu_controls["Cesium"] = {
            "lbl_rb": lbl_rb, "sp_box": sp_box, "btn_enable": btn_enable, "btn_cool": btn_cool, "lbl_badge": lbl_badge,
            "base_tag": "ion_beam.source.cesium", "unit": "°C", "is_voltage": False
        }

    def update_telemetry(self, data: dict):
        try:
            sys_connected = bool(data.get("system.connected", 0.0))
            pc_plc_lost = bool(data.get("ion_beam.system.pc_plc_comms_lost", 0.0))

            if not sys_connected or pc_plc_lost:
                self.lbl_comms.setText("PLC COMMS: OFFLINE")
                self.lbl_comms.setStyleSheet(COLOR_FAULT)
            else:
                self.lbl_comms.setText("PLC COMMS: OK")
                self.lbl_comms.setStyleSheet(COLOR_OK)

            # --- FAULT & FIRST-OUT EVALUATION ---
            active_alarms = []

            first_out = int(data.get("ion_beam.settings.first_out_code", 0))
            if first_out == 10:
                active_alarms.append("SAFETY TRIP: PROBABLE VACUUM SPIKE")
            elif first_out == 11:
                active_alarms.append("SAFETY TRIP: PHYSICAL INTERLOCK OR POWER SAG")

            w2_raw = data.get("ion_beam.faults.word_2_source")
            if w2_raw:
                w2_faults = self.fault_engine.get_active_faults("UDT_Fault_Word_2_Source", int(w2_raw))
                for f in w2_faults:
                    if f["severity"] == "CRITICAL":
                        active_alarms.append(f["name"].upper().replace("_", " "))

            if active_alarms:
                self.lbl_alarm_banner.setText(" | ".join(active_alarms))
                self.lbl_alarm_banner.show()
            else:
                self.lbl_alarm_banner.hide()

            # --- SAFETY RELAY & REMOTE IO EVALUATION ---
            relay_active_raw = data.get("ion_beam.facilities.safety_relay_active")
            relay_active = bool(relay_active_raw) if relay_active_raw is not None else False

            w0_raw = data.get("ion_beam.faults.word_0_system")
            w0_faults = self.fault_engine.get_active_faults("UDT_Fault_Word_0_System", int(w0_raw)) if w0_raw else []

            sr_tripped = any(f["name"] == "Safety_Relay_Tripped" for f in w0_faults)
            sr_refused = any(f["name"] == "Safety_Relay_Refused" for f in w0_faults)
            remote_lost = any(f["name"] == "Remote_IO_Lost" for f in w0_faults) or bool(
                data.get("ion_beam.system.remote_io_lost", 0.0))

            if sr_tripped or sr_refused:
                self.lbl_safety_state.setText("SAFETY: TRIPPED/REFUSED")
                self.lbl_safety_state.setStyleSheet(COLOR_FAULT)
            elif remote_lost:
                self.lbl_safety_state.setText("SAFETY: NO REMOTE IO")
                self.lbl_safety_state.setStyleSheet(COLOR_FAULT)
            elif relay_active:
                self.lbl_safety_state.setText("SAFETY: LIVE")
                self.lbl_safety_state.setStyleSheet(COLOR_WARNING)
            else:
                self.lbl_safety_state.setText("SAFETY: DE-ENERGIZED")
                self.lbl_safety_state.setStyleSheet(COLOR_OK)

            # --- MASTER STATE VARIABLES ---
            auto_sdown = bool(data.get("ion_beam.system.src_auto_sdown", 0.0))
            r_step = int(data.get("ion_beam.system.remote_shutdown_step", 0.0))
            p_step = int(data.get("ion_beam.system.pc_shutdown_step", 0.0))
            active_step = max(r_step, p_step)

            master_psu_lockout = (auto_sdown or active_step > 0 or not relay_active or remote_lost)

            # Determine explicit string for PSU lockouts to feed into tooltips
            psu_lock_reason = ""
            if not relay_active:
                psu_lock_reason = "Safety Relay is De-Energized."
            elif remote_lost:
                psu_lock_reason = "Remote I/O communications lost."
            elif auto_sdown or active_step > 0:
                psu_lock_reason = "Automated shutdown sequence in progress."

            # 1. Component-Level Sequences & Buttons
            gv_is_open = bool(data.get("ion_beam.source.chamber.stat_gv_open", False))
            any_psu_active = any(
                bool(data.get(f"{ctrl['base_tag']}.stat_enabled", False)) for ctrl in self.psu_controls.values())

            self.btn_reset_faults.setEnabled(True)
            self.btn_reset_faults.setToolTip("Click to clear latched software and hardware faults.")

            self.btn_enable_safety.setEnabled(not relay_active)
            if relay_active:
                self.btn_enable_safety.setEnabled(False)
                self.btn_enable_safety.setToolTip("Disabled: Safety Relay is already energized.")
            elif not gv_is_open:
                self.btn_enable_safety.setEnabled(False)
                self.btn_enable_safety.setToolTip(
                    "Disabled: The Gate Valve must be fully OPEN before the safety circuit can be energized.")
            else:
                self.btn_enable_safety.setEnabled(True)
                self.btn_enable_safety.setToolTip("Click to attempt energizing the Master Safety Relay.")

            can_shutdown = (active_step == 0) and relay_active and any_psu_active
            self.btn_shutdown.setEnabled(can_shutdown)

            if active_step > 0:
                self.btn_shutdown.setToolTip("Disabled: Shutdown sequence already in progress.")
            elif not relay_active:
                self.btn_shutdown.setToolTip("Disabled: Safety Relay is dead. Source is already powered off.")
            elif not any_psu_active:
                self.btn_shutdown.setToolTip("Disabled: All power supplies and the Cesium heater are already off.")
            else:
                self.btn_shutdown.setToolTip("Click to initiate a controlled, safe shutdown of the Ion Source.")

            self.btn_cancel_shutdown.setEnabled(p_step == 10)
            if p_step == 10:
                self.btn_cancel_shutdown.setToolTip("Click to abort the shutdown and resume normal operations.")
            else:
                self.btn_cancel_shutdown.setToolTip(
                    "Disabled: Abort is only permitted during the initial 'Hold Hot' phase.")

            self.btn_fc.setEnabled(not remote_lost)
            if remote_lost:
                self.btn_fc.setToolTip("Disabled: Remote I/O communications are offline.")
            else:
                self.btn_fc.setToolTip("Click to toggle the Faraday Cup position in the beamline.")

            if auto_sdown or active_step > 0:
                if active_step == 1:
                    self.lbl_shutdown_state.setText("SHUTDOWN: GRACE PERIOD (75% EXTR)")
                elif active_step == 2:
                    self.lbl_shutdown_state.setText("SHUTDOWN: CESIUM COOLDOWN")
                elif active_step == 3:
                    self.lbl_shutdown_state.setText("SHUTDOWN: FINAL KILL")
                elif active_step == 10:
                    self.lbl_shutdown_state.setText("SHUTDOWN: HOLD HOT PHASE")
                elif active_step == 11:
                    self.lbl_shutdown_state.setText("SHUTDOWN: FILAMENT RAMP DOWN")
                elif active_step == 99:
                    self.lbl_shutdown_state.setText("SHUTDOWN: LATCHED SAFE (RESET REQ)")

                self.lbl_shutdown_state.setStyleSheet(COLOR_WARNING if active_step in [1, 2, 10, 11] else COLOR_FAULT)
            else:
                self.lbl_shutdown_state.setText("NORMAL")
                self.lbl_shutdown_state.setStyleSheet(COLOR_OK)

            # 2. Update Sparkline Plot
            t_now = time.time() - self.start_time
            tgt_c = data.get("ion_beam.source.target.rb_current", 0.0)
            thm_c = data.get("ion_beam.source.thermionic.rb_current", 0.0)

            self.time_data.append(t_now)
            self.tgt_cur_data.append(tgt_c)
            self.thm_cur_data.append(thm_c)

            self.tgt_curve.setData(list(self.time_data), list(self.tgt_cur_data))
            self.thm_curve.setData(list(self.time_data), list(self.thm_cur_data))

            # 3. Update PSU Readbacks & States
            for name, ctrl in self.psu_controls.items():
                base_tag = ctrl["base_tag"]
                unit = ctrl["unit"]
                sp_box = ctrl["sp_box"]
                lbl_badge = ctrl["lbl_badge"]

                # A. Readbacks (PV)
                if name == "Cesium":
                    rb_val = data.get(f"{base_tag}.rb_temp")
                    duty_cycle = data.get(f"{base_tag}.out_cv_heating")
                    if rb_val is not None and duty_cycle is not None:
                        ctrl["lbl_rb"].setText(f"RB: {rb_val:.1f} {unit} (H: {duty_cycle:.0f}%)")
                else:
                    rb_type = "voltage" if "kV" in unit else "current"
                    rb_val = data.get(f"{base_tag}.rb_{rb_type}")
                    if rb_val is not None:
                        ctrl["lbl_rb"].setText(f"RB: {rb_val:.2f} {unit}")

                # B. Active Setpoint Synchronization
                if name == "Cesium":
                    sp_tag = "sp_actual_temp"
                elif ctrl["is_voltage"]:
                    sp_tag = "sp_actual_voltage"
                else:
                    sp_tag = "sp_actual_current"

                active_sp = data.get(f"{base_tag}.{sp_tag}")

                if active_sp is not None and not sp_box.hasFocus():
                    sp_box.blockSignals(True)
                    sp_box.setValue(float(active_sp))
                    sp_box.blockSignals(False)

                # C. Hardware State & Component Logic Sync
                ctrl_mode = data.get(f"{base_tag}.ctrl_mode", 0)
                lockout = bool(data.get(f"{base_tag}.stat_lockout", 0.0))

                if name == "Cesium":
                    stat_en = data.get(f"{base_tag}.stat_enabled")
                    if stat_en is not None:
                        ctrl["btn_enable"].setChecked(bool(stat_en))
                        ctrl["btn_enable"].setStyleSheet(COLOR_OK if stat_en else COLOR_BUTTON_STANDARD)
                        ctrl["btn_enable"].setEnabled(not master_psu_lockout)

                        if master_psu_lockout:
                            ctrl["btn_enable"].setToolTip(f"Disabled: {psu_lock_reason}")
                        else:
                            ctrl["btn_enable"].setToolTip("Click to toggle Cesium Heater PID control.")

                    stat_cool = bool(data.get(f"{base_tag}.stat_cooling_active", False))
                    stat_wait = bool(data.get(f"{base_tag}.stat_cooling_wait", False))

                    ctrl["btn_cool"].setEnabled(True)
                    ctrl["btn_cool"].setToolTip("Click to manually force the cooling air solenoid open.")

                    if stat_cool:
                        ctrl["btn_cool"].setChecked(True)
                        ctrl["btn_cool"].setText("COOLING ACTIVE")
                        ctrl["btn_cool"].setStyleSheet("background-color: #03A9F4; color: white;")
                    elif stat_wait:
                        ctrl["btn_cool"].setChecked(True)
                        ctrl["btn_cool"].setText("COMPRESSOR RESTING")
                        ctrl["btn_cool"].setStyleSheet("background-color: #FF9800; color: black; font-weight: bold;")
                    else:
                        ctrl["btn_cool"].setChecked(False)
                        ctrl["btn_cool"].setText("FORCED COOLING")
                        ctrl["btn_cool"].setStyleSheet(COLOR_BUTTON_STANDARD)

                else:
                    if "btn_enable" in ctrl:
                        stat_en = data.get(f"{base_tag}.stat_enabled")
                        if stat_en is not None:
                            ctrl["btn_enable"].setChecked(bool(stat_en))
                            ctrl["btn_enable"].setStyleSheet(COLOR_OK if stat_en else COLOR_BUTTON_STANDARD)
                            ctrl["btn_enable"].setEnabled(not master_psu_lockout)

                            if master_psu_lockout:
                                ctrl["btn_enable"].setToolTip(f"Disabled: {psu_lock_reason}")
                            else:
                                ctrl["btn_enable"].setToolTip(f"Click to toggle {name} power output.")

                    if name == "Thermionic" and "btn_auto" in ctrl:
                        auto_em_stat = data.get(f"{base_tag}.stat_auto_emission")
                        if auto_em_stat is not None:
                            ctrl["btn_auto"].setChecked(bool(auto_em_stat))
                            ctrl["btn_auto"].setStyleSheet(COLOR_OK if auto_em_stat else COLOR_BUTTON_STANDARD)
                            ctrl["btn_auto"].setEnabled(not master_psu_lockout)

                            if master_psu_lockout:
                                ctrl["btn_auto"].setToolTip(f"Disabled: {psu_lock_reason}")
                            else:
                                ctrl["btn_auto"].setToolTip("Click to toggle closed-loop Auto-Emission control.")

                # D. Control Authority Badges & Spinbox Lockouts
                if lockout:
                    sp_box.setEnabled(False)
                    sp_box.setToolTip("Disabled: Hardware interlock or fault is active.")
                    lbl_badge.setText("🔒 LOCKED")
                    lbl_badge.setStyleSheet(COLOR_FAULT)
                    lbl_badge.show()
                elif ctrl_mode == 1:
                    if name == "Cesium":
                        sp_box.setEnabled(not master_psu_lockout)
                        sp_box.setToolTip(f"Disabled: {psu_lock_reason}" if master_psu_lockout else "")
                        lbl_badge.setText("🔥 HEATING")
                        lbl_badge.setStyleSheet("background-color: #FF5722; color: white; border-radius: 3px;")
                        lbl_badge.show()
                    else:
                        sp_box.setEnabled(False)
                        sp_box.setToolTip("Disabled: Control yielded to PLC automated sequence.")
                        lbl_badge.setText("⚙️ PLC AUTO")
                        lbl_badge.setStyleSheet("background-color: #2196F3; color: white; border-radius: 3px;")
                        lbl_badge.show()
                elif ctrl_mode == 2:
                    sp_box.setEnabled(False)
                    sp_box.setToolTip("Disabled: Control yielded to active Recipe.")
                    lbl_badge.setText("🤖 RECIPE")
                    lbl_badge.setStyleSheet("background-color: #9C27B0; color: white; border-radius: 3px;")
                    lbl_badge.show()
                elif ctrl_mode == 3:
                    sp_box.setEnabled(False)
                    sp_box.setToolTip("Disabled: Target is dynamically cascaded from a master PID loop.")
                    lbl_badge.setText("🔗 CASCADED")
                    lbl_badge.setStyleSheet("background-color: #4CAF50; color: white; border-radius: 3px;")
                    lbl_badge.show()
                elif ctrl_mode == 4:
                    sp_box.setEnabled(False)
                    sp_box.setToolTip("Disabled: Output temporarily held by Arc Quench algorithm.")
                    lbl_badge.setText("⚡ ARC QUENCH")
                    lbl_badge.setStyleSheet(COLOR_WARNING)
                    lbl_badge.show()
                else:
                    sp_box.setEnabled(not master_psu_lockout)
                    sp_box.setToolTip(f"Disabled: {psu_lock_reason}" if master_psu_lockout else "")
                    lbl_badge.hide()

        except Exception as e:
            print(f"[UI Parser Error] Ion Source Runtime exception: {e}")


# --- Master Framework ---

class ControlMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("IPIDS Operator HMI")
        self.resize(1600, 900)
        self.setDockOptions(QMainWindow.DockOption.AllowNestedDocks | QMainWindow.DockOption.AllowTabbedDocks)

        current_dir = os.path.dirname(os.path.abspath(__file__))
        fault_map_path = os.path.abspath(os.path.join(current_dir, "../../../config/fault_map.json"))
        self.fault_engine = FaultRegistry(fault_map_path)

        self.cmd_thread = ZMQCommandThread()
        self.cmd_thread.start()

        self.master_telemetry_cache = {}

        telemetry_ports = [ZMQ_PORT_PLC_PUB, ZMQ_PORT_VACUUM_PUB, ZMQ_PORT_SRC_TURBO_PUB]
        self.telemetry_thread = ZMQTelemetryThread(telemetry_ports)
        self.telemetry_thread.data_received.connect(self._route_telemetry)
        self.telemetry_thread.start()

        self.subsystems = {}
        self._init_docks()

    def _init_docks(self):
        vac_dock = QDockWidget("Vacuum System", self)
        vac_dock.setObjectName("VacuumDock")
        self.vac_widget = VacuumControlWidget(self.cmd_thread, self.fault_engine)
        vac_dock.setWidget(self.vac_widget)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, vac_dock)
        self.subsystems["vacuum"] = self.vac_widget

        src_dock = QDockWidget("TESS Ion Source Control", self)
        src_dock.setObjectName("SourceDock")
        self.src_widget = IonSourceWidget(self.cmd_thread, self.fault_engine)
        src_dock.setWidget(self.src_widget)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, src_dock)
        self.subsystems["source"] = self.src_widget

    def _route_telemetry(self, fresh_data: dict):
        self.master_telemetry_cache.update(fresh_data)
        self.vac_widget.update_telemetry(self.master_telemetry_cache)
        self.src_widget.update_telemetry(self.master_telemetry_cache)

    def closeEvent(self, event):
        self.telemetry_thread.stop()
        self.cmd_thread.stop()
        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = ControlMainWindow()
    window.show()
    sys.exit(app.exec())