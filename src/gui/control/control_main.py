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

                # --- UPDATED: 32-bit DWORD Endianness Resolution ---
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
        import queue
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
    def __init__(self, cmd_thread: ZMQCommandThread, fault_engine: FaultRegistry):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.fault_engine = fault_engine
        self._gv_state_intent = None  # Tracks "OPENING", "CLOSING", or None

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
        self.btn_turbo_start.clicked.connect(self._cmd_start_turbo)
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
        self.btn_gv_open.clicked.connect(lambda: self._execute_gv_demand(True))

        self.btn_gv_close = QPushButton("CLOSE")
        self.btn_gv_close.setStyleSheet(COLOR_BUTTON_STANDARD)
        self.btn_gv_close.clicked.connect(lambda: self._execute_gv_demand(False))

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
        self.btn_turbo_start.setEnabled(False)
        self.cmd_thread.send_command("src_turbo", "ion_beam.source.turbo_pump.cmd_enable", True)

    def _execute_gv_demand(self, open_demand: bool):
        """Disables controls immediately and maps intended state machine direction."""
        self.btn_gv_open.setEnabled(False)
        self.btn_gv_close.setEnabled(False)
        self._gv_state_intent = "OPENING" if open_demand else "CLOSING"
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
                if t_speed >= 990: t_ready = True  # Firmware override normalization
            if t_current is not None:
                self.lbl_turbo_current.setText(f"Current: {t_current:.2f} A")

            if t_trip:
                self.lbl_turbo_status.setText("TRIPPED")
                self.lbl_turbo_status.setStyleSheet(COLOR_FAULT)
                self.btn_turbo_start.setEnabled(True)
            elif t_ready:
                self.lbl_turbo_status.setText("AT SPEED")
                self.lbl_turbo_status.setStyleSheet(COLOR_OK)
                self.btn_turbo_start.setEnabled(False)
            elif t_turning:
                self.lbl_turbo_status.setText("ACCELERATING")
                self.lbl_turbo_status.setStyleSheet(COLOR_WARNING)
                self.btn_turbo_start.setEnabled(False)

            # 3. Dynamic Fault Map Interception via Registry
            raw_sys_fault = data.get("ion_beam.faults.word_0_system")
            gv_fault_active = False

            if raw_sys_fault is not None:
                active_sys_faults = self.fault_engine.get_active_faults("UDT_Fault_Word_0_System",
                                                                        int(raw_sys_fault))
                gv_faults = [f for f in active_sys_faults if "GateValve" in f["name"]]

                if gv_faults:
                    gv_fault_active = True
                    self.lbl_gv_fault.setText(f"FAULT: {gv_faults[0]['name'].upper()}")
                    self.lbl_gv_fault.setStyleSheet(COLOR_FAULT)
                    self.lbl_gv_fault.setVisible(True)
                else:
                    self.lbl_gv_fault.setVisible(False)

            # --- DIAGNOSTIC: Uncomment this if the fault label still doesn't appear ---
            # 4. Discrete Actuator State and Write-Confirm Processing
            gv_ok_raw = data.get("ion_beam.source.chamber.stat_vac_ok_for_gv")
            gv_open_raw = data.get("ion_beam.source.chamber.stat_gv_open")
            gv_closed_raw = data.get("ion_beam.source.chamber.stat_gv_closed")  # NEW: Read both limit switches

            gv_ok_to_open = bool(gv_ok_raw) if gv_ok_raw is not None else False
            gv_is_open = bool(gv_open_raw) if gv_open_raw is not None else None
            gv_is_closed = bool(gv_closed_raw) if gv_closed_raw is not None else None

            # Evaluate Limit Switch State Matrix
            if gv_is_open is not None and gv_is_closed is not None:
                if gv_is_open and not gv_is_closed:
                    self.lbl_gv_status.setText("OPEN")
                    self.lbl_gv_status.setStyleSheet(COLOR_OK)
                    if self._gv_state_intent == "OPENING": self._gv_state_intent = None

                elif not gv_is_open and gv_is_closed:
                    self.lbl_gv_status.setText("CLOSED")
                    self.lbl_gv_status.setStyleSheet(COLOR_INACTIVE)
                    if self._gv_state_intent == "CLOSING": self._gv_state_intent = None

                elif not gv_is_open and not gv_is_closed:
                    # Transitional State
                    if self._gv_state_intent == "OPENING":
                        self.lbl_gv_status.setText("OPENING...")
                    elif self._gv_state_intent == "CLOSING":
                        self.lbl_gv_status.setText("CLOSING...")
                    else:
                        self.lbl_gv_status.setText("TRAVELLING")
                    self.lbl_gv_status.setStyleSheet(COLOR_WARNING)

                elif gv_is_open and gv_is_closed:
                    # Impossible physical state
                    self.lbl_gv_status.setText("SENSOR CONFLICT")
                    self.lbl_gv_status.setStyleSheet(COLOR_FAULT)

            # Interlock and write-confirm control logic arbitration
            if self._gv_state_intent is None:
                if gv_fault_active:
                    self.btn_gv_open.setEnabled(False)
                    self.btn_gv_close.setEnabled(False)
                    self.btn_gv_open.setText("FAULT LOCKOUT")
                else:
                    self.btn_gv_open.setText("OPEN")
                    if gv_is_open is not None and gv_is_closed is not None:
                        # Lock out OPEN if already open, or if interlock prevents it
                        self.btn_gv_open.setEnabled(gv_ok_to_open and not gv_is_open)
                        # Lock out CLOSE if already closed
                        self.btn_gv_close.setEnabled(not gv_is_closed)

                        self.btn_gv_open.setStyleSheet(COLOR_BUTTON_STANDARD if not gv_is_open else COLOR_INACTIVE)
                        self.btn_gv_close.setStyleSheet(
                            COLOR_BUTTON_STANDARD if not gv_is_closed else COLOR_INACTIVE)
                    else:
                        self.btn_gv_open.setEnabled(False)
                        self.btn_gv_close.setEnabled(False)

        except Exception as e:
            print(f"[UI Parser Error] Runtime exception: {e}")


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

        # 1. Master Safety & Shutdown Block
        safety_group = QGroupBox("Master Safety & Interlocks")
        sl = QHBoxLayout()

        self.lbl_comms = QLabel("PLC COMMS: OK")
        self.lbl_comms.setStyleSheet(COLOR_OK)
        self.lbl_safety_state = QLabel("SAFETY RELAY: UNKNOWN")
        self.lbl_safety_state.setStyleSheet(COLOR_INACTIVE)
        self.lbl_shutdown_state = QLabel("NORMAL")
        self.lbl_shutdown_state.setStyleSheet(COLOR_OK)

        self.btn_enable_safety = QPushButton("ENABLE SAFETY RELAY")
        self.btn_enable_safety.setStyleSheet(COLOR_BUTTON_STANDARD)
        self.btn_enable_safety.clicked.connect(
            lambda: self.cmd_thread.send_command("plc", "ion_beam.system.cmd_enable_safety", True))

        self.btn_reset_faults = QPushButton("RESET FAULTS")
        self.btn_reset_faults.setStyleSheet(COLOR_BUTTON_STANDARD)
        self.btn_reset_faults.clicked.connect(
            lambda: self.cmd_thread.send_command("plc", "ion_beam.system.cmd_fault_reset", True))

        sl.addWidget(self.lbl_comms)
        sl.addWidget(self.lbl_safety_state)
        sl.addWidget(self.lbl_shutdown_state)
        sl.addWidget(self.btn_enable_safety)
        sl.addWidget(self.btn_reset_faults)
        safety_group.setLayout(sl)
        layout.addWidget(safety_group)

        # 2. Emission Sparkline Plot (Dual Y-Axis)
        plot_group = QGroupBox("Emission Stability")
        pl_layout = QVBoxLayout()
        pg.setConfigOption('background', 'w')
        pg.setConfigOption('foreground', 'k')

        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setLabel('bottom', 'Time', units='s')
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)

        # Extract the underlying pyqtgraph PlotItem to manipulate the layout safely
        plot_item = self.plot_widget.getPlotItem()

        # Left Axis: Target Current (0-10mA range)
        plot_item.setLabel('left', 'Target Current', units='mA', color='r')
        plot_item.getAxis('left').setPen('r')
        self.tgt_curve = plot_item.plot(pen=pg.mkPen(color='r', width=2), name="Target")

        # Right Axis: Thermionic Current (0-200mA range)
        self.view_right = pg.ViewBox()
        plot_item.scene().addItem(self.view_right)
        self.axis_right = pg.AxisItem('right')

        # Inject the right axis into the internal graphics layout at column 3
        plot_item.layout.addItem(self.axis_right, 2, 3)
        self.axis_right.linkToView(self.view_right)
        self.axis_right.setLabel('Thermionic Current', units='mA', color='b')
        self.axis_right.setPen('b')
        self.view_right.setXLink(plot_item)

        self.thm_curve = pg.PlotDataItem(pen=pg.mkPen(color='b', width=2), name="Thermionic")
        self.view_right.addItem(self.thm_curve)

        # Force view update on resize
        plot_item.getViewBox().sigResized.connect(self._update_dual_axis)

        pl_layout.addWidget(self.plot_widget)
        plot_group.setLayout(pl_layout)
        layout.addWidget(plot_group)

        # 3. PSU Control Grid
        psu_group = QGroupBox("Source Power Supplies")
        self.psu_layout = QGridLayout()
        self.psu_controls = {}

        # Row, Name, Setpoint Tag, Readback Tag, Readback Unit, Min, Max, is_voltage
        self._build_psu_row(0, "Extraction", "ion_beam.source.extraction", "kV", 0.0, 20.0, is_voltage=True)
        self._build_psu_row(1, "Target", "ion_beam.source.target", "kV", 0.0, 10.0, is_voltage=True)
        self._build_psu_row(2, "Filament", "ion_beam.source.filament", "A", 0.0, 38.0, is_voltage=False)
        self._build_psu_row(3, "Thermionic", "ion_beam.source.thermionic", "mA", 0.0, 1000.0, is_voltage=False)

        # Special Cesium Row
        self._build_cs_row(4)

        psu_group.setLayout(self.psu_layout)
        layout.addWidget(psu_group)
        layout.addStretch()

        self.ui_locked = False

    def _update_dual_axis(self):
        """Maintains geometric alignment for the right Y-axis ViewBox."""
        plot_view = self.plot_widget.getPlotItem().getViewBox()
        self.view_right.setGeometry(plot_view.sceneBoundingRect())
        self.view_right.linkedViewChanged(plot_view, self.view_right.XAxis)

    def _build_psu_row(self, row, name, base_tag, unit, min_v, max_v, is_voltage):
        lbl_name = QLabel(f"<b>{name}</b>")
        lbl_rb = QLabel(f"RB: --- {unit}")

        btn_enable = QPushButton("ENABLE")
        btn_enable.setCheckable(True)
        btn_enable.setStyleSheet(COLOR_BUTTON_STANDARD)
        btn_enable.clicked.connect(
            lambda checked, t=base_tag: self.cmd_thread.send_command("plc", f"{t}.cmd_enable", checked))

        sp_box = QDoubleSpinBox()
        sp_box.setRange(min_v, max_v)
        sp_box.setSuffix(f" {unit}")
        sp_box.setDecimals(2 if is_voltage else 1)
        sp_box.editingFinished.connect(lambda t=base_tag, b=sp_box, iv=is_voltage:
                                       self.cmd_thread.send_command("plc", f"{t}.sp_{'voltage' if iv else 'current'}",
                                                                    b.value()))

        self.psu_layout.addWidget(lbl_name, row, 0)
        self.psu_layout.addWidget(lbl_rb, row, 1)
        self.psu_layout.addWidget(sp_box, row, 2)
        self.psu_layout.addWidget(btn_enable, row, 3)

        self.psu_controls[name] = {
            "lbl_rb": lbl_rb, "sp_box": sp_box, "btn_enable": btn_enable,
            "base_tag": base_tag, "unit": unit, "is_voltage": is_voltage
        }

    def _build_cs_row(self, row):
        lbl_name = QLabel("<b>Cesium Oven</b>")
        lbl_rb = QLabel("RB: --- °C")

        btn_cool = QPushButton("COOLING ACTIVE")
        btn_cool.setCheckable(True)
        btn_cool.setStyleSheet(COLOR_BUTTON_STANDARD)
        # Note: Invert logic because command is cmd_stop_cooler
        btn_cool.clicked.connect(
            lambda checked: self.cmd_thread.send_command("plc", "ion_beam.source.cesium.cmd_stop_cooler", not checked))

        sp_box = QDoubleSpinBox()
        sp_box.setRange(0.0, 200.0)
        sp_box.setSuffix(" °C")
        sp_box.editingFinished.connect(
            lambda: self.cmd_thread.send_command("plc", "ion_beam.source.cesium.sp_temp", sp_box.value()))

        self.psu_layout.addWidget(lbl_name, row, 0)
        self.psu_layout.addWidget(lbl_rb, row, 1)
        self.psu_layout.addWidget(sp_box, row, 2)
        self.psu_layout.addWidget(btn_cool, row, 3)

        self.psu_controls["Cesium"] = {
            "lbl_rb": lbl_rb, "sp_box": sp_box, "btn_cool": btn_cool,
            "base_tag": "ion_beam.source.cesium", "unit": "°C"
        }

    def update_telemetry(self, data: dict):
        try:
            # 1. Comms & Safety State Evaluation
            sys_connected = bool(data.get("system.connected", 0.0))
            pc_plc_lost = bool(data.get("ion_beam.system.pc_plc_comms_lost", 0.0))

            if not sys_connected or pc_plc_lost:
                self.lbl_comms.setText("PLC COMMS: OFFLINE")
                self.lbl_comms.setStyleSheet(COLOR_FAULT)
                self._lock_ui(True)
                return
            else:
                self.lbl_comms.setText("PLC COMMS: OK")
                self.lbl_comms.setStyleSheet(COLOR_OK)

            # Hardware Safety Relay Extraction
            relay_active_raw = data.get("ion_beam.facilities.safety_relay_active")
            relay_active = bool(relay_active_raw) if relay_active_raw is not None else False

            # Fault Word Extraction
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
                self.lbl_safety_state.setStyleSheet(COLOR_WARNING)  # High Voltage is active
            else:
                self.lbl_safety_state.setText("SAFETY: DE-ENERGIZED")
                self.lbl_safety_state.setStyleSheet(COLOR_OK)  # Safe state

            # Auto-Shutdown Logic
            auto_sdown = bool(data.get("ion_beam.system.src_auto_sdown", 0.0))
            r_step = int(data.get("ion_beam.system.remote_shutdown_step", 0.0))
            p_step = int(data.get("ion_beam.system.pc_shutdown_step", 0.0))
            active_step = max(r_step, p_step)

            # Lockout UI if safety relay drops, remote IO drops, or shutdown sequence is active
            if auto_sdown or active_step > 0 or not relay_active or remote_lost:
                self._lock_ui(True)
            else:
                self._lock_ui(False)

            if auto_sdown or active_step > 0:
                if active_step == 1:
                    self.lbl_shutdown_state.setText("SHUTDOWN: GRACE PERIOD (75% EXTR)")
                elif active_step == 2:
                    self.lbl_shutdown_state.setText("SHUTDOWN: CESIUM COOLDOWN")
                elif active_step == 3:
                    self.lbl_shutdown_state.setText("SHUTDOWN: FINAL KILL")
                elif active_step == 99:
                    self.lbl_shutdown_state.setText("SHUTDOWN: LATCHED SAFE (RESET REQ)")
                self.lbl_shutdown_state.setStyleSheet(COLOR_WARNING if active_step in [1, 2] else COLOR_FAULT)
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

                # Readbacks
                if name == "Cesium":
                    rb_val = data.get(f"{base_tag}.rb_temp")
                    cool_active = data.get(f"{base_tag}.cooling_active")
                    if cool_active is not None:
                        # Write Confirm
                        ctrl["btn_cool"].setChecked(bool(cool_active))
                        ctrl["btn_cool"].setStyleSheet(COLOR_OK if bool(cool_active) else COLOR_INACTIVE)
                else:
                    rb_type = "voltage" if "kV" in unit else "current"
                    rb_val = data.get(f"{base_tag}.rb_{rb_type}")

                if rb_val is not None:
                    ctrl["lbl_rb"].setText(f"RB: {rb_val:.2f} {unit}")

                # Button States (Write-Confirm bypass)
                if "btn_enable" in ctrl:
                    stat_enabled = bool(data.get(f"{base_tag}.stat_enabled", 0.0))
                    btn = ctrl["btn_enable"]
                    btn.setChecked(stat_enabled)
                    btn.setStyleSheet(COLOR_OK if stat_enabled else COLOR_INACTIVE)

        except Exception as e:
            print(f"[UI Parser Error] Ion Source Runtime exception: {e}")

    def _lock_ui(self, lock: bool):
        """Disables PSU inputs during comms loss, unpowered relays, or automated shutdown sequences."""
        if self.ui_locked == lock:
            return

        for name, ctrl in self.psu_controls.items():
            ctrl["sp_box"].setEnabled(not lock)
            if "btn_enable" in ctrl:
                ctrl["btn_enable"].setEnabled(not lock)
            if "btn_cool" in ctrl:
                ctrl["btn_cool"].setEnabled(not lock)

        self.btn_enable_safety.setEnabled(not lock)  # Cannot command enable if comms are lost
        self.ui_locked = lock

# --- Master Framework ---

class ControlMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("IPIDS Operator HMI")
        self.resize(1600, 900)
        self.setDockOptions(QMainWindow.DockOption.AllowNestedDocks | QMainWindow.DockOption.AllowTabbedDocks)

        # Resolve paths and load the fault map registry
        current_dir = os.path.dirname(os.path.abspath(__file__))
        fault_map_path = os.path.abspath(os.path.join(current_dir, "../../../config/fault_map.json"))
        self.fault_engine = FaultRegistry(fault_map_path)

        # Spin up communications threads
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
        """Merges new data into the master cache, then passes the full cache to subsystems."""
        self.master_telemetry_cache.update(fresh_data)

        # Pass the ENTIRE known state of the machine, not just the latest fragment
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