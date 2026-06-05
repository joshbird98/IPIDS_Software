import sys
import time
from collections import deque
import zmq
import orjson
import json
import pyqtgraph as pg
import queue
import os
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QDockWidget, QWidget,
    QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton, QFrame,
    QGroupBox, QDoubleSpinBox, QScrollArea, QListWidget, QListWidgetItem,
    QComboBox, QMessageBox, QDialog, QTreeWidget, QTreeWidgetItem,
    QCheckBox, QSpinBox, QLineEdit, QInputDialog, QToolBar
)
from PyQt6.QtGui import QColor, QPixmap, QIcon
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QSize, QUrl
from PyQt6.QtMultimedia import QSoundEffect

from src.core.network_map import (
    ZMQ_PORT_PLC_PUB, ZMQ_PORT_PLC_CMD,
    ZMQ_PORT_VACUUM_PUB,
    ZMQ_PORT_SRC_TURBO_PUB, ZMQ_PORT_SRC_TURBO_CMD,
    ZMQ_PORT_MANAGER_PUB, ZMQ_PORT_MANAGER_CMD,
    ZMQ_PORT_SPELLMAN_PUB, ZMQ_PORT_SPELLMAN_CMD,
    ZMQ_PORT_MAGNET_PUB, ZMQ_PORT_MAGNET_CMD
)

from src.core.event_helper import EventHelper
from src.core.os_helper import harden_windows_process

# --- SCADA Standard Colors ---
COLOR_OK = "background-color: #4CAF50; color: white; font-weight: bold; border-radius: 4px; padding: 4px;"
COLOR_FAULT = "background-color: #F44336; color: white; font-weight: bold; border-radius: 4px; padding: 4px;"
COLOR_INACTIVE = "background-color: #757575; color: white; font-weight: bold; border-radius: 4px; padding: 4px;"
COLOR_WARNING = "background-color: #FF9800; color: white; font-weight: bold; border-radius: 4px; padding: 4px;"
COLOR_BUTTON_STANDARD = "background-color: #E0E0E0; color: black; font-weight: normal; border-radius: 4px; padding: 6px;"


# --- Global Fault Map Parser ---

class FaultRegistry:
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
                        "description": info.get("description", "No description provided.")
                    })
            except Exception as e:
                continue
        return active_faults

# --- Audio Manager ---
class AudioManager:
    def __init__(self):
        self.sounds = {}

        # Resolve the absolute path to the directory containing control_main.py
        self.base_dir = os.path.dirname(os.path.abspath(__file__))
        self.sounds_dir = os.path.join(self.base_dir, "assets", "sounds")

        self._load_sound("warning", "warning.wav", volume=0.7)
        self._load_sound("fault", "fault.wav", volume=1.0)
        self._load_sound("critical", "critical_comms_loss.wav", volume=1.0)
        self._load_sound("trip", "relay_trip.wav", volume=0.8)

    def _load_sound(self, name: str, filename: str, volume: float):
        filepath = os.path.join(self.sounds_dir, filename)

        if not os.path.exists(filepath):
            print(f"[Audio] Missing asset: {filepath}")
            return

        effect = QSoundEffect()
        effect.setSource(QUrl.fromLocalFile(filepath))
        effect.setVolume(volume)
        self.sounds[name] = effect

    def play(self, name: str):
        if name in self.sounds:
            self.sounds[name].play()


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
                    except Exception as e:
                        print(f"[ZMQ Telemetry Error] {e}") # This will print exact decoding/unpacking errors
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
            "src_turbo": ctx.socket(zmq.PUB),
            "manager": ctx.socket(zmq.PUB),
            "spellman": ctx.socket(zmq.PUB),
            "magnet": ctx.socket(zmq.PUB)
        }
        sockets["plc"].connect(ZMQ_PORT_PLC_CMD)
        sockets["src_turbo"].connect(ZMQ_PORT_SRC_TURBO_CMD)
        sockets["manager"].connect(ZMQ_PORT_MANAGER_CMD)
        sockets["spellman"].connect(ZMQ_PORT_SPELLMAN_CMD)
        sockets["magnet"].connect(ZMQ_PORT_MAGNET_CMD)

        try:
            while self.running:
                try:
                    subsystem, tag, value = self.cmd_queue.get(timeout=0.1)
                    if subsystem == "manager":
                        payload = value
                    else:
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


# --- Experiment Logging Subsystem Component ---

class ExperimentLoggerWidget(QWidget):
    def __init__(self, event_helper):
        super().__init__()
        self.event_helper = event_helper
        self.last_manager_beat = 0

        layout = QVBoxLayout(self)

        # 1. Phase Change Group
        phase_group = QGroupBox("Experiment Phase Control")
        phase_layout = QHBoxLayout()

        self.le_phase = QLineEdit()
        self.le_phase.setPlaceholderText("e.g., Silicon_Implant_Run_04")
        self.le_phase.returnPressed.connect(self._set_phase)

        self.btn_phase = QPushButton("SET NEW PHASE")
        self.btn_phase.setStyleSheet("""
            QPushButton { background-color: #4CAF50; color: white; font-weight: bold; padding: 6px; border-radius: 4px; }
            QPushButton:disabled { background-color: #757575; color: #B0B0B0; }
        """)
        self.btn_phase.clicked.connect(self._set_phase)

        phase_layout.addWidget(self.le_phase)
        phase_layout.addWidget(self.btn_phase)
        phase_group.setLayout(phase_layout)
        layout.addWidget(phase_group)

        # 2. User Marker Group
        marker_group = QGroupBox("Add Log Marker / Observation")
        marker_layout = QGridLayout()

        self.le_marker = QLineEdit()
        self.le_marker.setPlaceholderText("Type observation or comment here...")
        self.le_marker.returnPressed.connect(self._add_marker)

        self.cb_color = QComboBox()
        self.colors = {
            "White": "#FFFFFF",
            "Cyan": "#00E5FF",
            "Yellow": "#FFEA00",
            "Magenta": "#FF4081",
            "Purple": "#9C27B0",
            "Pink": "#E91E63"
        }

        for name, hex_code in self.colors.items():
            pixmap = QPixmap(16, 16)
            pixmap.fill(QColor(hex_code))
            icon = QIcon(pixmap)
            self.cb_color.addItem(icon, name, hex_code)

        self.cb_color.setIconSize(QSize(16, 16))

        self.btn_marker = QPushButton("ADD MARKER")
        self.btn_marker.setStyleSheet("""
            QPushButton { background-color: #E0E0E0; color: black; font-weight: bold; padding: 6px; border-radius: 4px; }
            QPushButton:disabled { background-color: #757575; color: #B0B0B0; }
        """)
        self.btn_marker.clicked.connect(self._add_marker)

        marker_layout.addWidget(QLabel("<b>Marker Color:</b>"), 0, 0)
        marker_layout.addWidget(self.cb_color, 0, 1)
        marker_layout.addWidget(self.le_marker, 1, 0, 1, 2)
        marker_layout.addWidget(self.btn_marker, 2, 0, 1, 2)
        marker_group.setLayout(marker_layout)
        layout.addWidget(marker_group)

        # 3. Session History
        self.list_history = QListWidget()
        self.list_history.setStyleSheet("background-color: #FAFAFA; font-family: monospace; font-size: 10pt;")
        layout.addWidget(QLabel("<b>Recent Session Logs:</b>"))
        layout.addWidget(self.list_history)

        layout.addStretch()

    def update_telemetry(self, data: dict):
        # Update our local manager heartbeat
        if "manager.services" in data:
            self.last_manager_beat = time.time()

        manager_dead = (time.time() - self.last_manager_beat) > 2.5
        events_state = data.get("manager.services", {}).get("service_events", "OFFLINE")

        # Evaluate Lockout condition
        is_locked = manager_dead or (events_state != "ONLINE")

        if manager_dead:
            lock_reason = "Service Manager is offline."
        else:
            lock_reason = f"service_events is {events_state}."

        controls = [self.le_phase, self.btn_phase, self.le_marker, self.cb_color, self.btn_marker]

        for ctrl in controls:
            ctrl.setEnabled(not is_locked)
            ctrl.setToolTip(f"Disabled: {lock_reason}" if is_locked else "")

    def _set_phase(self):
        phase = self.le_phase.text().strip()
        if phase:
            self.event_helper.log_phase(phase)
            list_item = QListWidgetItem(f"[{time.strftime('%H:%M:%S')}] PHASE: {phase.upper()}")
            list_item.setBackground(QColor("#4CAF50"))
            list_item.setForeground(QColor("white"))
            self.list_history.insertItem(0, list_item)
            self.le_phase.clear()

    def _add_marker(self):
        text = self.le_marker.text().strip()
        if text:
            hex_color = self.cb_color.currentData()
            self.event_helper.log_user_marker(time.time(), text, hex_color)
            list_item = QListWidgetItem(f"[{time.strftime('%H:%M:%S')}] MARKER: {text}")
            list_item.setBackground(QColor(hex_color))
            if QColor(hex_color).lightness() < 128:
                list_item.setForeground(QColor("white"))
            else:
                list_item.setForeground(QColor("black"))
            self.list_history.insertItem(0, list_item)
            self.le_marker.clear()


# --- Vacuum Subsystem Component ---

class VacuumControlWidget(QWidget):
    def __init__(self, cmd_thread, fault_engine):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.fault_engine = fault_engine

        self.main_layout = QVBoxLayout(self)
        self._init_gauges_ui()
        self.ctrl_layout = QHBoxLayout()
        self._init_turbo_ui()
        self._init_gv_ui()
        self.main_layout.addLayout(self.ctrl_layout)
        self.main_layout.addStretch()

    def _init_gauges_ui(self):
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
        self.main_layout.addWidget(gauge_group)

    def _init_turbo_ui(self):
        turbo_group = QGroupBox("Source Turbo Pump")
        tl = QVBoxLayout()
        self.lbl_turbo_speed = QLabel("Speed: --- Hz")
        self.lbl_turbo_current = QLabel("Current: --- A")
        self.lbl_turbo_status = QLabel("OFFLINE")
        self.lbl_turbo_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_turbo_status.setStyleSheet(COLOR_INACTIVE)

        self.btn_turbo_start = QPushButton("START PUMP")
        self.btn_turbo_start.setStyleSheet(COLOR_BUTTON_STANDARD)
        self.btn_turbo_start.clicked.connect(
            lambda checked=False: self.cmd_thread.send_command("src_turbo", "ion_beam.source.turbo_pump.cmd_enable",
                                                               True))

        tl.addWidget(self.lbl_turbo_speed)
        tl.addWidget(self.lbl_turbo_current)
        tl.addWidget(self.lbl_turbo_status)
        tl.addWidget(self.btn_turbo_start)
        turbo_group.setLayout(tl)
        self.ctrl_layout.addWidget(turbo_group)

    def _init_gv_ui(self):
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
        self.btn_gv_open.clicked.connect(
            lambda checked=False: self.cmd_thread.send_command("plc", "ion_beam.source.chamber.cmd_open_gv", True))

        self.btn_gv_close = QPushButton("CLOSE")
        self.btn_gv_close.setStyleSheet(COLOR_BUTTON_STANDARD)
        self.btn_gv_close.clicked.connect(
            lambda checked=False: self.cmd_thread.send_command("plc", "ion_beam.source.chamber.cmd_open_gv", False))

        btn_row.addWidget(self.btn_gv_open)
        btn_row.addWidget(self.btn_gv_close)
        gvl.addWidget(self.lbl_gv_status)
        gvl.addWidget(self.lbl_gv_fault)
        gvl.addLayout(btn_row)
        gv_group.setLayout(gvl)
        self.ctrl_layout.addWidget(gv_group)

    def update_telemetry(self, data: dict):
        try:
            sys_connected = bool(data.get("system.connected", False))
            pc_plc_lost = bool(data.get("ion_beam.system.pc_plc_comms_lost", False))
            master_comms_lost = not sys_connected or pc_plc_lost

            turbo_connected = bool(data.get("pump.connected", False))
            vac_connected = bool(data.get("vacuum.connected", False))

            self._update_gauges(data, vac_connected)
            self._update_turbo(data, turbo_connected)
            self._update_gate_valve(data, master_comms_lost)
        except Exception as e:
            print(f"[UI Parser Error] Vacuum Runtime exception: {e}")

    def _update_gauges(self, data: dict, vac_connected: bool):
        gauge_locations = {
            1: "source",
            2: "beamline",
            3: "beamline",
            4: "endstation",
            5: "loadlock",
            6: "endstation"
        }
        for i in range(1, 7):
            if not vac_connected:
                self.gauges[i]["status"].setText("OFFLINE")
                self.gauges[i]["status"].setStyleSheet(COLOR_INACTIVE)
            else:
                loc = gauge_locations[i]
                pressure = data.get(f"ion_beam.{loc}.vacuum_gauge_{i}.rb_pressure")
                status = data.get(f"ion_beam.{loc}.vacuum_gauge_{i}.stat_error_code")

                rapid_rise = data.get(f"ion_beam.{loc}.vacuum_gauge_{i}.stat_rapid_rise", False)
                not_found = data.get(f"ion_beam.{loc}.vacuum_gauge_{i}.stat_not_found", False)

                # Fetch dynamic relay states
                approaching_sp = data.get(f"ion_beam.{loc}.vacuum_gauge_{i}.stat_approaching_sp", False)
                relay_active = data.get(f"ion_beam.{loc}.vacuum_gauge_{i}.stat_relay_active", False)
                above_sp = data.get(f"ion_beam.{loc}.vacuum_gauge_{i}.stat_above_sp", False)

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
                    elif status == 3:
                        self.gauges[i]["status"].setText("SENSOR OFF")
                        self.gauges[i]["status"].setStyleSheet(COLOR_INACTIVE)
                    elif status != 1:
                        self.gauges[i]["status"].setText(f"FAULT ({int(status)})")
                        self.gauges[i]["status"].setStyleSheet(COLOR_FAULT)
                    else:
                        # Status is 1 (Online) - Evaluate dynamic fail-safe relay states
                        has_relay = bool(relay_active or above_sp)

                        if has_relay:
                            if above_sp:
                                self.gauges[i]["status"].setText("TRIPPED")
                                self.gauges[i]["status"].setStyleSheet(COLOR_FAULT)
                            elif approaching_sp:
                                self.gauges[i]["status"].setText("WARNING")
                                self.gauges[i]["status"].setStyleSheet(COLOR_WARNING)
                            else:
                                self.gauges[i]["status"].setText("HEALTHY")
                                self.gauges[i]["status"].setStyleSheet(COLOR_OK)
                        else:
                            self.gauges[i]["status"].setText("ONLINE")
                            self.gauges[i]["status"].setStyleSheet(COLOR_OK)

    def _update_turbo(self, data: dict, turbo_connected: bool):
        if not turbo_connected:
            self.btn_turbo_start.setEnabled(False)
            self.btn_turbo_start.setText("-")
            self.btn_turbo_start.setToolTip("Disabled: Source Turbo telemetry stream is dead.")
            self.lbl_turbo_speed.setText(f"-")
            self.lbl_turbo_status.setText("OFFLINE")
        else:
            t_speed = data.get("ion_beam.source.turbo_pump.rb_speed_hz")
            t_current = data.get("ion_beam.source.turbo_pump.rb_current")
            t_trip = data.get("ion_beam.source.turbo_pump.stat_trip", False)
            t_ready = data.get("ion_beam.source.turbo_pump.stat_ready", False)
            t_turning = data.get("ion_beam.source.turbo_pump.stat_turning", False)

            if t_speed is not None:
                self.lbl_turbo_speed.setText(f"Speed: {t_speed} Hz")
                if t_speed >= 990: t_ready = True
            if t_current is not None:
                self.lbl_turbo_current.setText(f"Current: {t_current:.2f} A")

            if t_trip:
                self.lbl_turbo_status.setText("TRIPPED")
                self.lbl_turbo_status.setStyleSheet(COLOR_FAULT)
                self.btn_turbo_start.setEnabled(True)
                self.btn_turbo_start.setText("RESET && START")
            elif t_ready:
                self.lbl_turbo_status.setText("AT SPEED")
                self.lbl_turbo_status.setStyleSheet(COLOR_OK)
                self.btn_turbo_start.setEnabled(False)
                self.btn_turbo_start.setText("RUNNING")
            elif t_turning:
                self.lbl_turbo_status.setText("ACCELERATING")
                self.lbl_turbo_status.setStyleSheet(COLOR_WARNING)
                self.btn_turbo_start.setEnabled(False)
                self.btn_turbo_start.setText("RUNNING")
            else:
                self.lbl_turbo_status.setText("IDLE")
                self.lbl_turbo_status.setStyleSheet(COLOR_INACTIVE)
                self.btn_turbo_start.setEnabled(True)
                self.btn_turbo_start.setText("START PUMP")

    def _update_gate_valve(self, data: dict, master_comms_lost: bool):
        if master_comms_lost:
            self.btn_gv_open.setEnabled(False)
            self.btn_gv_close.setEnabled(False)
            self.btn_gv_open.setText("OFFLINE")
            self.btn_gv_close.setText("OFFLINE")
            self.btn_gv_open.setToolTip("Disabled: PLC telemetry stream is dead.")
            self.btn_gv_close.setToolTip("Disabled: PLC telemetry stream is dead.")
        else:
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

            gv_is_open = data.get("ion_beam.source.chamber.stat_gv_open")
            gv_is_closed = data.get("ion_beam.source.chamber.stat_gv_closed")
            gv_open_perm = bool(data.get("ion_beam.source.chamber.stat_gv_open_permissive", False))
            gv_close_perm = bool(data.get("ion_beam.source.chamber.stat_gv_close_permissive", False))

            if gv_is_open is not None and gv_is_closed is not None:
                gv_is_open = bool(gv_is_open)
                gv_is_closed = bool(gv_is_closed)

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

                if gv_fault_active:
                    self.btn_gv_open.setEnabled(False)
                    self.btn_gv_close.setEnabled(False)
                    self.btn_gv_open.setText("FAULT LOCKOUT")
                    self.btn_gv_close.setText("FAULT LOCKOUT")
                    self.btn_gv_open.setToolTip("Disabled: Gate valve hardware fault detected.")
                    self.btn_gv_close.setToolTip("Disabled: Gate valve hardware fault detected.")
                else:
                    self.btn_gv_open.setText("OPEN")
                    self.btn_gv_close.setText("CLOSE")
                    self.btn_gv_open.setEnabled(gv_open_perm and not gv_is_open)
                    self.btn_gv_close.setEnabled(gv_close_perm and not gv_is_closed)

                    self.btn_gv_open.setStyleSheet(
                        COLOR_BUTTON_STANDARD if self.btn_gv_open.isEnabled() else COLOR_INACTIVE)
                    self.btn_gv_close.setStyleSheet(
                        COLOR_BUTTON_STANDARD if self.btn_gv_close.isEnabled() else COLOR_INACTIVE)

                    if gv_is_open:
                        self.btn_gv_open.setToolTip("Disabled: Gate valve is already open.")
                    elif not gv_open_perm:
                        self.btn_gv_open.setToolTip(
                            "Disabled: Vacuum interlocks not met. Ensure turbo pump is at speed.")
                    else:
                        self.btn_gv_open.setToolTip("Click to command the gate valve OPEN.")

                    if gv_is_closed:
                        self.btn_gv_close.setToolTip("Disabled: Gate valve is already closed.")
                    elif not gv_close_perm:
                        self.btn_gv_close.setToolTip(
                            "Disabled: Source must be cold (HV, Filament, and Cesium OFF) to close manually.")
                    else:
                        self.btn_gv_close.setToolTip("Click to command the gate valve CLOSED.")

# --- Ion Source Subsystem Component ---

class IonSourceWidget(QWidget):
    def __init__(self, cmd_thread, fault_engine):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.fault_engine = fault_engine

        self.history_len = 600
        self.time_data = deque(maxlen=self.history_len)
        self.tgt_cur_data = deque(maxlen=self.history_len)
        self.thm_cur_data = deque(maxlen=self.history_len)
        self.start_time = time.time()

        self.main_layout = QVBoxLayout(self)

        self._init_alarms_ui()
        self._init_safety_ui()
        self._init_diagnostics_ui()
        self._init_plot_ui()
        self._init_psu_ui()

    def _dispatch_command(self, tag: str, value):
        target = "spellman" if "einzel" in tag else "plc"
        self.cmd_thread.send_command(target, tag, value)

    def _update_dual_axis(self):
        plot_view = self.plot_widget.getPlotItem().getViewBox()
        self.view_right.setGeometry(plot_view.sceneBoundingRect())
        self.view_right.linkedViewChanged(plot_view, self.view_right.XAxis)

    def _init_alarms_ui(self):
        self.lbl_alarm_banner = QLabel("")
        self.lbl_alarm_banner.setStyleSheet(
            "background-color: #F44336; color: yellow; font-size: 11pt; font-weight: bold; padding: 6px;")
        self.lbl_alarm_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_alarm_banner.hide()
        self.main_layout.addWidget(self.lbl_alarm_banner)

    def _init_safety_ui(self):
        safety_group = QGroupBox("Master Safety && Sequence Control")
        sl = QGridLayout()

        self.lbl_comms = QLabel("PLC COMMS: WAITING FOR DATA...")
        self.lbl_comms.setStyleSheet(COLOR_WARNING)
        self.lbl_safety_state = QLabel("SAFETY RELAY: WAITING FOR DATA...")
        self.lbl_safety_state.setStyleSheet(COLOR_WARNING)
        self.lbl_shutdown_state = QLabel("SHUTDOWN SEQ: WAITING FOR DATA...")
        self.lbl_shutdown_state.setStyleSheet(COLOR_WARNING)

        self.btn_enable_safety = QPushButton("ENABLE SAFETY RELAY")
        self.btn_enable_safety.setCheckable(True)
        self.btn_enable_safety.setStyleSheet("""
            QPushButton { background-color: #2196F3; color: white; font-weight: bold; border-radius: 4px; padding: 6px; }
            QPushButton:checked { background-color: #F44336; color: white; }
            QPushButton:disabled { background-color: #757575; color: #B0B0B0; font-weight: bold; }
        """)
        self.btn_enable_safety.clicked.connect(
            lambda *args, b=self.btn_enable_safety: self._dispatch_command("ion_beam.system.cmd_enable_safety",
                                                                           b.isChecked()))

        self.btn_reset_faults = QPushButton("RESET FAULTS")
        self.btn_reset_faults.setStyleSheet(COLOR_BUTTON_STANDARD)
        self.btn_reset_faults.clicked.connect(
            lambda *args: self._dispatch_command("ion_beam.system.cmd_fault_reset", True))

        self.btn_shutdown = QPushButton("INITIATE SHUTDOWN")
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
        self.main_layout.addWidget(safety_group)

    def _init_diagnostics_ui(self):
        diag_group = QGroupBox("Beamline Diagnostics")
        dl = QHBoxLayout()
        self.btn_fc = QPushButton("FARADAY CUP")
        self.btn_fc.setCheckable(True)
        self.btn_fc.setStyleSheet("""
            QPushButton { background-color: #E0E0E0; color: black; border-radius: 4px; padding: 6px; }
            QPushButton:disabled { background-color: #757575; color: #B0B0B0; font-weight: bold; }
        """)
        self.btn_fc.clicked.connect(
            lambda *args, b=self.btn_fc: self._dispatch_command("ion_beam.beamline_diagnostics.cmd_insert_fc",
                                                                b.isChecked()))
        dl.addWidget(self.btn_fc)
        dl.addStretch()
        diag_group.setLayout(dl)
        self.main_layout.addWidget(diag_group)

    def _init_plot_ui(self):
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
        self.main_layout.addWidget(plot_group)

    def _dispatch_spellman_setpoints(self, base_tag: str, voltage: float):
        """Dispatches voltage and autonomously sets a safe 250uA current limit to unclamp the CC loop."""
        self._dispatch_command(f"{base_tag}.sp_requested_voltage", voltage)
        self._dispatch_command(f"{base_tag}.sp_requested_current", 250.0)

    def _init_psu_ui(self):
        psu_group = QGroupBox("Source Power Supplies")
        self.psu_layout = QGridLayout()
        self.psu_controls = {}

        # Updated to the new ISA-95 path: ion_beam.source.einzel
        self._build_psu_row(0, "Source Einzel", "ion_beam.source.einzel", "kV", "µA", 0.0, 30.0,
                            is_voltage=True, decimals=3)

        self._build_psu_row(1, "Extraction", "ion_beam.source.extraction", "kV", "mA", 0.0, 20.0, is_voltage=True, decimals=3)
        self._build_psu_row(2, "Target", "ion_beam.source.target", "kV", "mA", 0.0, 10.0, is_voltage=True, decimals=3)
        self._build_psu_row(3, "Filament", "ion_beam.source.filament", "A", "V", 0.0, 38.0, is_voltage=False, decimals=3)
        self._build_psu_row(4, "Thermionic", "ion_beam.source.thermionic", "mA", "V", 0.0, 1000.0, is_voltage=False, decimals=2)
        self._build_cs_row(5)

        psu_group.setLayout(self.psu_layout)
        self.main_layout.addWidget(psu_group)
        self.main_layout.addStretch()

    def _build_psu_row(self, row, name, base_tag, pri_unit, sec_unit, min_v, max_v, is_voltage, decimals):
        lbl_name = QLabel(f"<b>{name}</b>")

        if name in ["Extraction", "Target", "Filament", "Thermionic"]:
            lbl_rb = QLabel(f"RB: --- {pri_unit} | --- {sec_unit} | --- W")
            lbl_rb.setMinimumWidth(250)
        else:
            lbl_rb = QLabel(f"RB: --- {pri_unit} | --- {sec_unit}")
            lbl_rb.setMinimumWidth(180)

        lbl_badge = QLabel("")
        lbl_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_badge.setFixedWidth(100)
        lbl_badge.hide()

        btn_enable = QPushButton("ENABLE")
        btn_enable.setCheckable(True)
        btn_enable.setStyleSheet(COLOR_BUTTON_STANDARD)
        btn_enable.clicked.connect(
            lambda *args, b=btn_enable, t=base_tag: self._dispatch_command(f"{t}.cmd_enable", b.isChecked()))

        sp_box = QDoubleSpinBox()
        sp_box.setRange(min_v, max_v)
        sp_box.setSuffix(f" {pri_unit}")
        sp_box.setDecimals(decimals)

        if decimals >= 3:
            sp_box.setSingleStep(0.001)
        elif decimals == 2:
            sp_box.setSingleStep(0.01)
        else:
            sp_box.setSingleStep(0.1)

        sp_box.setKeyboardTracking(False)

        # Updated to check for "einzel"
        if "einzel" in base_tag:
            sp_box.editingFinished.connect(
                lambda t=base_tag, b=sp_box: self._dispatch_spellman_setpoints(t, b.value()))
        else:
            sp_box.editingFinished.connect(
                lambda t=base_tag, b=sp_box, iv=is_voltage: self._dispatch_command(
                    f"{t}.sp_requested_{'voltage' if iv else 'current'}", b.value()))

        self.psu_layout.addWidget(lbl_name, row, 0)
        self.psu_layout.addWidget(lbl_rb, row, 1)
        self.psu_layout.addWidget(sp_box, row, 2)
        self.psu_layout.addWidget(lbl_badge, row, 3)
        self.psu_layout.addWidget(btn_enable, row, 4)

        self.psu_controls[name] = {
            "lbl_rb": lbl_rb, "sp_box": sp_box, "btn_enable": btn_enable,
            "lbl_badge": lbl_badge, "base_tag": base_tag,
            "pri_unit": pri_unit, "sec_unit": sec_unit, "is_voltage": is_voltage,
            "decimals": decimals
        }

        if name == "Thermionic":
            btn_auto = QPushButton("AUTO EMISSION")
            btn_auto.setCheckable(True)
            btn_auto.setStyleSheet(COLOR_BUTTON_STANDARD)
            btn_auto.clicked.connect(
                lambda *args, b=btn_auto, t=base_tag: self._dispatch_command(f"{t}.cmd_auto_emission", b.isChecked()))
            self.psu_layout.addWidget(btn_auto, row, 5)
            self.psu_controls[name]["btn_auto"] = btn_auto

    def _build_cs_row(self, row):
        lbl_name = QLabel("<b>Cesium Oven</b>")
        lbl_rb = QLabel("RB: --- °C")
        lbl_rb.setMinimumWidth(180)

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
            "lbl_rb": lbl_rb, "sp_box": sp_box, "btn_enable": btn_enable,
            "btn_cool": btn_cool, "lbl_badge": lbl_badge,
            "base_tag": "ion_beam.source.cesium", "is_voltage": False
        }

    def update_telemetry(self, data: dict):
        try:
            master_comms_lost = self._update_comms_status(data)
            relay_active, remote_lost = self._update_alarms_and_safety_state(data, master_comms_lost)

            auto_sdown = bool(data.get("ion_beam.system.src_auto_sdown", 0.0))
            active_step = max(int(data.get("ion_beam.system.remote_shutdown_step", 0.0)),
                              int(data.get("ion_beam.system.pc_shutdown_step", 0.0)))

            spellman_state = data.get("manager.services", {}).get("service_spellman_mpd", "OFFLINE")
            spellman_comms_lost = (spellman_state != "ONLINE")

            self._update_safety_controls(data, relay_active, active_step, master_comms_lost)
            self._update_shutdown_labels(active_step, auto_sdown, master_comms_lost)
            self._update_faraday_cup(data, master_comms_lost)
            self._update_plot(data)
            self._update_power_supplies(data, relay_active, remote_lost, auto_sdown, active_step, master_comms_lost,
                                        spellman_comms_lost)

        except Exception as e:
            print(f"[UI Parser Error] Ion Source Runtime exception: {e}")

    def _update_comms_status(self, data: dict) -> bool:
        sys_connected = bool(data.get("system.connected", False))
        pc_plc_lost = bool(data.get("ion_beam.system.pc_plc_comms_lost", False))
        master_comms_lost = not sys_connected or pc_plc_lost

        if master_comms_lost:
            self.lbl_comms.setText("PLC COMMS: OFFLINE")
            self.lbl_comms.setStyleSheet(COLOR_FAULT)
        else:
            self.lbl_comms.setText("PLC COMMS: OK")
            self.lbl_comms.setStyleSheet(COLOR_OK)
        return master_comms_lost

    def _update_alarms_and_safety_state(self, data: dict, master_comms_lost: bool) -> tuple[bool, bool]:
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
                if f["severity"] == "CRITICAL": active_alarms.append(f["name"].upper().replace("_", " "))

        # --- OFFLINE / STALE ALARM BANNER OVERRIDE ---
        if master_comms_lost:
            self.lbl_alarm_banner.setText("⚠️ TELEMETRY OFFLINE: FAULT STATUS UNKNOWN ⚠️")
            self.lbl_alarm_banner.setStyleSheet(
                "background-color: #9E9E9E; color: black; font-size: 11pt; font-weight: bold; padding: 6px;")
            self.lbl_alarm_banner.show()
        elif active_alarms:
            self.lbl_alarm_banner.setText(" | ".join(active_alarms))
            self.lbl_alarm_banner.setStyleSheet(
                "background-color: #F44336; color: yellow; font-size: 11pt; font-weight: bold; padding: 6px;")
            self.lbl_alarm_banner.show()
        else:
            self.lbl_alarm_banner.hide()

        relay_active = bool(data.get("ion_beam.facilities.safety_relay_active", False))
        w0_raw = data.get("ion_beam.faults.word_0_system")
        w0_faults = self.fault_engine.get_active_faults("UDT_Fault_Word_0_System", int(w0_raw)) if w0_raw else []

        sr_tripped = any(f["name"] == "Safety_Relay_Tripped" for f in w0_faults)
        sr_refused = any(f["name"] == "Safety_Relay_Refused" for f in w0_faults)
        remote_lost = any(f["name"] == "Remote_IO_Lost" for f in w0_faults) or bool(
            data.get("ion_beam.system.remote_io_lost", 0.0))

        if master_comms_lost:
            self.lbl_safety_state.setText("SAFETY: UNKNOWN")
            self.lbl_safety_state.setStyleSheet(COLOR_INACTIVE)
        elif relay_active:
            self.lbl_safety_state.setText("SAFETY: LIVE")
            self.lbl_safety_state.setStyleSheet(COLOR_WARNING)
        elif sr_tripped or sr_refused:
            self.lbl_safety_state.setText("SAFETY: TRIPPED/REFUSED")
            self.lbl_safety_state.setStyleSheet(COLOR_FAULT)
        elif remote_lost:
            self.lbl_safety_state.setText("SAFETY: NO REMOTE IO")
            self.lbl_safety_state.setStyleSheet(COLOR_FAULT)
        else:
            self.lbl_safety_state.setText("SAFETY: DE-ENERGIZED")
            self.lbl_safety_state.setStyleSheet(COLOR_OK)

        return relay_active, remote_lost

    def _update_safety_controls(self, data: dict, relay_active: bool, active_step: int, master_comms_lost: bool):
        gv_is_open = bool(data.get("ion_beam.source.chamber.stat_gv_open", False))
        source_is_cold = bool(data.get("ion_beam.source.chamber.stat_gv_close_permissive", False))
        any_psu_active = any(
            bool(data.get(f"{ctrl['base_tag']}.stat_enabled", False)) for ctrl in self.psu_controls.values())

        self.btn_reset_faults.setEnabled(not master_comms_lost)
        self.btn_reset_faults.setToolTip(
            "Disabled: GUI to PLC communications are offline." if master_comms_lost else "Click to clear latched software and hardware faults.")

        # Extract the software command state from telemetry
        cmd_enable_safety = bool(data.get("ion_beam.system.cmd_enable_safety", False))
        timer_elapsed_ms = float(data.get("ion_beam.system.safety_timer_elapsed_ms", 0.0))
        timer_total_ms = float(data.get("ion_beam.system.safety_timer_total_ms", 5000.0))

        # Enforce that the software command must be True to show the countdown
        is_timing_out = cmd_enable_safety and (0 < timer_elapsed_ms < timer_total_ms) and not relay_active

        if is_timing_out and timer_total_ms > 0:
            remaining_sec = (timer_total_ms - timer_elapsed_ms) / 1000.0
            total_sec = timer_total_ms / 1000.0
            fill_ratio = max(0.0, min(1.0, remaining_sec / total_sec))

            sweep_style = f"""
                QPushButton {{
                    background: qlineargradient(x1:1, y1:0, x2:0, y2:0, 
                                                stop:0 #F44336, 
                                                stop:{fill_ratio:.3f} #F44336, 
                                                stop:{min(1.0, fill_ratio + 0.001):.3f} #2196F3);
                    color: white; font-weight: bold; border-radius: 4px; padding: 6px;
                }}
            """
            self.btn_enable_safety.blockSignals(True)
            self.btn_enable_safety.setChecked(True)
            self.btn_enable_safety.blockSignals(False)

            self.btn_enable_safety.setText(f"PRESS PANEL START ({int(remaining_sec) + 1}s)")
            self.btn_enable_safety.setStyleSheet(sweep_style)
            self.btn_enable_safety.setEnabled(True)
            self.btn_enable_safety.setToolTip(f"Awaiting physical panel button press... ({total_sec}s timeout)")

        else:
            self.btn_enable_safety.blockSignals(True)
            self.btn_enable_safety.setChecked(relay_active)
            self.btn_enable_safety.blockSignals(False)

            self.btn_enable_safety.setStyleSheet("""
                QPushButton { background-color: #2196F3; color: white; font-weight: bold; border-radius: 4px; padding: 6px; }
                QPushButton:checked { background-color: #F44336; color: white; }
                QPushButton:disabled { background-color: #757575; color: #B0B0B0; font-weight: bold; }
            """)

            if master_comms_lost:
                self.btn_enable_safety.setEnabled(False)
                self.btn_enable_safety.setToolTip("Disabled: GUI to PLC communications are offline.")
            elif not relay_active:
                if not gv_is_open:
                    self.btn_enable_safety.setEnabled(False)
                    self.btn_enable_safety.setText("ENABLE SAFETY RELAY")
                    self.btn_enable_safety.setToolTip(
                        "Disabled: The Gate Valve must be fully OPEN before the safety circuit can be energized.")
                else:
                    self.btn_enable_safety.setEnabled(True)
                    self.btn_enable_safety.setText("ENABLE SAFETY RELAY")
                    self.btn_enable_safety.setToolTip("Click to energize the Master Safety Relay.")
            else:
                if not source_is_cold:
                    self.btn_enable_safety.setEnabled(False)
                    self.btn_enable_safety.setText("RELAY ENERGIZED (LOCKED)")
                    self.btn_enable_safety.setToolTip(
                        "Disabled: You cannot drop the safety relay while the source is hot or active. Run a shutdown first.")
                else:
                    self.btn_enable_safety.setEnabled(True)
                    self.btn_enable_safety.setText("DISABLE SAFETY RELAY")
                    self.btn_enable_safety.setToolTip("Click to manually de-energize the Master Safety Relay.")

        # Permit shutdown if PSUs are active OR the source thermal mass is hot
        can_shutdown = (active_step == 0) and relay_active and (any_psu_active or not source_is_cold)
        self.btn_shutdown.setEnabled(can_shutdown and not master_comms_lost)

        if master_comms_lost:
            self.btn_shutdown.setToolTip("Disabled: GUI to PLC communications are offline.")
        elif active_step > 0:
            self.btn_shutdown.setToolTip("Disabled: Shutdown sequence already in progress.")
        elif not relay_active:
            self.btn_shutdown.setToolTip("Disabled: Safety Relay is dead. Source is already powered off.")
        elif not any_psu_active and source_is_cold:
            self.btn_shutdown.setToolTip("Disabled: All power supplies are off and the source is already cold.")
        else:
            self.btn_shutdown.setToolTip("Click to initiate a controlled, safe shutdown of the Ion Source.")

        p_step = int(data.get("ion_beam.system.pc_shutdown_step", 0.0))
        self.btn_cancel_shutdown.setEnabled(p_step == 10 and not master_comms_lost)
        if master_comms_lost:
            self.btn_cancel_shutdown.setToolTip("Disabled: GUI to PLC communications are offline.")
        elif p_step == 10:
            self.btn_cancel_shutdown.setToolTip("Click to abort the shutdown and resume normal operations.")
        else:
            self.btn_cancel_shutdown.setToolTip(
                "Disabled: Abort is only permitted during the initial 'Hold Hot' phase.")

    def _update_faraday_cup(self, data: dict, master_comms_lost: bool):
        fc_inserted = data.get("ion_beam.beamline_diagnostics.stat_fc_inserted")
        fc_retracted = data.get("ion_beam.beamline_diagnostics.stat_fc_retracted")
        fc_lockout = bool(data.get("ion_beam.beamline_diagnostics.stat_lockout", False))
        air_ok = bool(data.get("ion_beam.facilities.stat_air_press_ok", True))

        self.btn_fc.blockSignals(True)
        fc_state_string = "UNKNOWN"

        if fc_inserted is not None and fc_retracted is not None:
            fc_in, fc_out = bool(fc_inserted), bool(fc_retracted)
            if fc_in and not fc_out:
                fc_state_string = "INSERTED"
                self.btn_fc.setChecked(True)
                self.btn_fc.setText("FARADAY CUP: INSERTED")
                self.btn_fc.setStyleSheet(
                    """QPushButton { background-color: #FF9800; color: black; font-weight: bold; border-radius: 4px; padding: 6px; } QPushButton:disabled { background-color: #757575; color: #B0B0B0; font-weight: bold; }""")
            elif not fc_in and fc_out:
                fc_state_string = "RETRACTED"
                self.btn_fc.setChecked(False)
                self.btn_fc.setText("FARADAY CUP: RETRACTED")
                self.btn_fc.setStyleSheet(
                    """QPushButton { background-color: #E0E0E0; color: black; border-radius: 4px; padding: 6px; } QPushButton:disabled { background-color: #757575; color: #B0B0B0; font-weight: bold; }""")
            elif not fc_in and not fc_out:
                fc_state_string = "UNPOWERED / TRAVELLING"
                self.btn_fc.setText("FARADAY CUP: NO PNEUMATICS")
                self.btn_fc.setStyleSheet(COLOR_FAULT)
            elif fc_in and fc_out:
                fc_state_string = "SENSOR ERROR"
                self.btn_fc.setText("FARADAY CUP: SENSOR ERROR")
                self.btn_fc.setStyleSheet(COLOR_FAULT)

        if master_comms_lost:
            self.btn_fc.setEnabled(False)
            self.btn_fc.setToolTip("Disabled: GUI to PLC communications are offline.")
        elif not air_ok:
            self.btn_fc.setEnabled(False)
            self.btn_fc.setToolTip("Disabled: Facility air pressure is lost. Actuator cannot be moved.")
        elif fc_lockout:
            self.btn_fc.setEnabled(False)
            self.btn_fc.setToolTip(f"Disabled: Faraday Cup is locked {fc_state_string} by an active PLC sequence.")
        else:
            self.btn_fc.setEnabled(True)
            self.btn_fc.setToolTip("Click to toggle the Faraday Cup position in the beamline.")

        self.btn_fc.blockSignals(False)

    def _update_shutdown_labels(self, active_step: int, auto_sdown: bool, master_comms_lost: bool):
        if master_comms_lost:
            self.lbl_shutdown_state.setText("SHUTDOWN SEQ: UNKNOWN")
            self.lbl_shutdown_state.setStyleSheet(COLOR_INACTIVE)
        elif auto_sdown or active_step > 0:
            if active_step == 1:
                self.lbl_shutdown_state.setText("SHUTDOWN SEQ: GRACE PERIOD (75% EXTR)")
            elif active_step == 2:
                self.lbl_shutdown_state.setText("SHUTDOWN SEQ: CESIUM COOLDOWN")
            elif active_step == 3:
                self.lbl_shutdown_state.setText("SHUTDOWN SEQ: FINAL KILL")
            elif active_step == 10:
                self.lbl_shutdown_state.setText("SHUTDOWN SEQ: HOLD HOT PHASE")
            elif active_step == 11:
                self.lbl_shutdown_state.setText("SHUTDOWN SEQ: FILAMENT RAMP DOWN")
            elif active_step == 99:
                self.lbl_shutdown_state.setText("SHUTDOWN SEQ: LATCHED SAFE (RESET REQ)")
            self.lbl_shutdown_state.setStyleSheet(COLOR_WARNING if active_step in [1, 2, 10, 11] else COLOR_FAULT)
        else:
            self.lbl_shutdown_state.setText("SHUTDOWN SEQ: INACTIVE")
            self.lbl_shutdown_state.setStyleSheet(COLOR_OK)

    def _update_plot(self, data: dict):
        t_now = time.time() - self.start_time
        self.time_data.append(t_now)
        self.tgt_cur_data.append(data.get("ion_beam.source.target.rb_current", 0.0))
        self.thm_cur_data.append(data.get("ion_beam.source.thermionic.rb_current", 0.0))

        self.tgt_curve.setData(list(self.time_data), list(self.tgt_cur_data))
        self.thm_curve.setData(list(self.time_data), list(self.thm_cur_data))

    def _update_power_supplies(self, data: dict, relay_active: bool, remote_lost: bool, auto_sdown: bool,
                               active_step: int, master_comms_lost: bool, spellman_comms_lost: bool):
        for name, ctrl in self.psu_controls.items():
            base_tag = ctrl["base_tag"]
            pri_unit = ctrl.get("pri_unit", "")
            sec_unit = ctrl.get("sec_unit", "")
            sp_box = ctrl["sp_box"]
            lbl_badge = ctrl["lbl_badge"]
            is_spellman = "einzel" in base_tag

            comp_lock_reason = ""
            if is_spellman and spellman_comms_lost:
                comp_lock_reason = "Spellman microservice is offline."
            elif master_comms_lost:
                comp_lock_reason = "Safety Relay state is unknown (PLC offline)."
            elif not relay_active:
                comp_lock_reason = "Safety Relay is De-Energized."
            elif remote_lost:
                comp_lock_reason = "Remote I/O communications lost."
            elif auto_sdown or active_step > 0:
                comp_lock_reason = "Automated shutdown sequence in progress."

            comp_lockout = bool(comp_lock_reason)

            # A. Readbacks (PV) with Dynamic Power Append
            is_stale = master_comms_lost or (is_spellman and spellman_comms_lost)
            stale_str = " [?]" if is_stale else ""

            if name == "Cesium":
                rb_val = data.get(f"{base_tag}.rb_temp")
                est_val = data.get(f"{base_tag}.estimated_temp")
                duty_cycle = data.get(f"{base_tag}.out_cv_heating")

                if duty_cycle is not None:
                    # If hardware is dead, fall back to the PLC's thermal estimate
                    if not relay_active or remote_lost:
                        temp_str = f"~{est_val:.1f}" if est_val is not None else "---"
                    else:
                        temp_str = f"{rb_val:.1f}" if rb_val is not None else "---"

                    ctrl["lbl_rb"].setText(f"RB: {temp_str} °C (H: {duty_cycle:.0f}%){stale_str}")
            else:
                rb_v = data.get(f"{base_tag}.rb_voltage")
                rb_i = data.get(f"{base_tag}.rb_current")
                rb_p = data.get(f"{base_tag}.rb_power")

                # Match primary readback to the SpinBox precision
                pri_decimals = ctrl.get("decimals", 2)

                # Cap the secondary µA readout at 1 DP to match the hardware serial limits
                sec_decimals = 1 if sec_unit == "µA" else 2

                # Append wattage conditionally if present
                p_str = f" | {rb_p:.1f} W" if rb_p is not None else ""

                if ctrl["is_voltage"]:
                    v_str = f"{rb_v:.{pri_decimals}f}" if rb_v is not None else "---"
                    i_str = f"{rb_i:.{sec_decimals}f}" if rb_i is not None else "---"
                    ctrl["lbl_rb"].setText(f"RB: {v_str} {pri_unit} | {i_str} {sec_unit}{p_str}{stale_str}")
                else:
                    i_str = f"{rb_i:.{pri_decimals}f}" if rb_i is not None else "---"
                    v_str = f"{rb_v:.{sec_decimals}f}" if rb_v is not None else "---"
                    ctrl["lbl_rb"].setText(f"RB: {i_str} {pri_unit} | {v_str} {sec_unit}{p_str}{stale_str}")

            # B. Active Setpoint Synchronization
            sp_tag = "sp_actual_temp" if name == "Cesium" else (
                "sp_actual_voltage" if ctrl["is_voltage"] else "sp_actual_current")
            active_sp = data.get(f"{base_tag}.{sp_tag}")
            if active_sp is not None and not sp_box.hasFocus():
                sp_box.blockSignals(True)
                sp_box.setValue(float(active_sp))
                sp_box.blockSignals(False)

            # C. Hardware State & Component Logic Sync
            ctrl_mode = data.get(f"{base_tag}.ctrl_mode", 0)
            hw_lockout = bool(data.get(f"{base_tag}.stat_lockout", 0.0))

            if name == "Cesium":
                stat_en = data.get(f"{base_tag}.stat_enabled")
                if stat_en is not None:
                    ctrl["btn_enable"].setChecked(bool(stat_en))
                    ctrl["btn_enable"].setStyleSheet(COLOR_OK if stat_en else COLOR_BUTTON_STANDARD)

                ctrl["btn_enable"].setEnabled(not comp_lockout)
                ctrl["btn_enable"].setToolTip(
                    f"Disabled: {comp_lock_reason}" if comp_lockout else "Click to toggle Cesium Heater PID control.")

                stat_cool = bool(data.get(f"{base_tag}.stat_cooling_active", False))
                stat_wait = bool(data.get(f"{base_tag}.stat_cooling_wait", False))
                ctrl["btn_cool"].setEnabled(not master_comms_lost)
                ctrl["btn_cool"].setToolTip(
                    "Disabled: GUI to PLC communications are offline." if master_comms_lost else "Click to manually force the cooling air solenoid open.")

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
                    stat_disarming = bool(data.get(f"{base_tag}.stat_disarming", False))

                    if name == "Filament" and stat_disarming:
                        ctrl["btn_enable"].blockSignals(True)
                        ctrl["btn_enable"].setChecked(True)
                        ctrl["btn_enable"].blockSignals(False)

                        ctrl["btn_enable"].setText("DISARMING...")
                        ctrl["btn_enable"].setStyleSheet(
                            "background-color: #FF9800; color: black; font-weight: bold; border-radius: 4px;")
                        ctrl["btn_enable"].setEnabled(False)
                        ctrl["btn_enable"].setToolTip(
                            "Contactor remains closed while safe step-down ramp drops current to 0A.")
                    else:
                        if name == "Filament":
                            ctrl["btn_enable"].setText("ENABLE")

                        if stat_en is not None:
                            ctrl["btn_enable"].setChecked(bool(stat_en))
                            ctrl["btn_enable"].setStyleSheet(COLOR_OK if stat_en else COLOR_BUTTON_STANDARD)

                        ctrl["btn_enable"].setEnabled(not comp_lockout)
                        ctrl["btn_enable"].setToolTip(
                            f"Disabled: {comp_lock_reason}" if comp_lockout else f"Click to toggle {name} power output.")

                if name == "Thermionic" and "btn_auto" in ctrl:
                    auto_em_stat = data.get(f"{base_tag}.stat_auto_emission")

                    if auto_em_stat is not None:
                        is_auto = bool(auto_em_stat)

                        # Block signals to prevent the UI update from accidentally triggering a ZMQ command
                        ctrl["btn_auto"].blockSignals(True)
                        ctrl["btn_auto"].setChecked(is_auto)

                        if is_auto:
                            ctrl["btn_auto"].setText("AUTO EMISSION: ON")
                            ctrl["btn_auto"].setStyleSheet(COLOR_OK)
                            tt = f"Disabled: {comp_lock_reason}" if comp_lockout else "Auto-Emission ACTIVE. Click to revert to Manual Filament Control."
                        else:
                            ctrl["btn_auto"].setText("AUTO EMISSION")
                            ctrl["btn_auto"].setStyleSheet(COLOR_BUTTON_STANDARD)
                            tt = f"Disabled: {comp_lock_reason}" if comp_lockout else "Click to engage closed-loop Auto-Emission control (Cascades Filament output)."

                        ctrl["btn_auto"].setToolTip(tt)
                        ctrl["btn_auto"].blockSignals(False)

                    ctrl["btn_auto"].setEnabled(not comp_lockout)
                    if comp_lockout and auto_em_stat is None:
                        ctrl["btn_auto"].setToolTip(f"Disabled: {comp_lock_reason}")

            # D. Control Authority Badges & Spinbox Lockouts
            if hw_lockout:
                sp_box.setEnabled(False)
                sp_box.setToolTip("Disabled: Hardware interlock or fault is active.")
                lbl_badge.setText("🔒 LOCKED")
                lbl_badge.setStyleSheet(COLOR_FAULT)
                lbl_badge.show()
            elif ctrl_mode == 1:
                if name == "Cesium":
                    sp_box.setEnabled(not comp_lockout)
                    sp_box.setToolTip(f"Disabled: {comp_lock_reason}" if comp_lockout else "")
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
                sp_box.setEnabled(not comp_lockout)
                sp_box.setToolTip(f"Disabled: {comp_lock_reason}" if comp_lockout else "")
                lbl_badge.hide()

class BeamlineOpticsWidget(QWidget):
    def __init__(self, cmd_thread, event_helper):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.event_helper = event_helper

        self.config_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../config"))
        self.magnet_config = self._load_magnet_config()

        self.main_layout = QVBoxLayout(self)

        self._init_pre_magnet_ui()
        self._init_magnet_ui()
        self._init_post_magnet_ui()
        self._init_downstream_optics_ui()

        self.main_layout.addStretch()

    def _dispatch_spellman_setpoints(self, base_tag: str, voltage: float):
        """Dispatches voltage and autonomously sets a safe 250uA current limit to unclamp the CC loop."""
        self._dispatch_command(f"{base_tag}.sp_requested_voltage", voltage)
        self._dispatch_command(f"{base_tag}.sp_requested_current", 250.0)

    def _load_magnet_config(self):
        try:
            path = os.path.join(self.config_dir, "magnet_config.json")
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            return {"mass_calibration_poly": [0.0, 1.0, 0.0, 0.0]}

    def _dispatch_command(self, tag: str, value):
        if "einzel" in tag or "neutral_trap" in tag:
            target = "spellman"
        elif "magnet" in tag:
            target = "magnet"
        else:
            target = "plc"
        self.cmd_thread.send_command(target, tag, value)

    def _log_manual_slit(self, position_name: str, spinbox: QDoubleSpinBox):
        val = spinbox.value()
        self.event_helper.log_user_marker(time.time(), f"Manual Adjustment: {position_name} set to {val} mm", "#9C27B0")
        tag_name = position_name.lower().replace(" ", "_").replace("-", "_")
        self._dispatch_command(f"ion_beam.beamline.slits.{tag_name}", val)

    def _init_pre_magnet_ui(self):
        group = QGroupBox("1. Pre-Magnet Tuning")
        layout = QGridLayout()

        # Y-Steerer
        layout.addWidget(QLabel("<b>Y-Steerer Voltage:</b>"), 0, 0)

        self.lbl_y_rb = QLabel("RB: --- V")
        self.lbl_y_rb.setMinimumWidth(80)
        layout.addWidget(self.lbl_y_rb, 0, 1)

        self.sp_y_steer = QDoubleSpinBox()
        self.sp_y_steer.setRange(-200.0, 200.0)
        self.sp_y_steer.setSuffix(" V")
        self.sp_y_steer.setDecimals(1)
        self.sp_y_steer.setKeyboardTracking(False)
        self.sp_y_steer.editingFinished.connect(
            lambda: self._dispatch_command("ion_beam.beamline.steering.sp_requested_y_volts", self.sp_y_steer.value()))
        layout.addWidget(self.sp_y_steer, 0, 2)

        # Master Steering Enable (Applies to both X and Y)
        self.btn_steer_en = QPushButton("ENABLE STEERING")
        self.btn_steer_en.setCheckable(True)
        self.btn_steer_en.setStyleSheet(COLOR_BUTTON_STANDARD)
        self.btn_steer_en.clicked.connect(
            lambda *args: self._dispatch_command("ion_beam.beamline.steering.cmd_enable",
                                                 self.btn_steer_en.isChecked()))
        layout.addWidget(self.btn_steer_en, 0, 3)

        # Object Slits
        layout.addWidget(QLabel("<b>Object Slits (L / R):</b>"), 1, 0)
        slit_layout = QHBoxLayout()

        self.sp_obj_l = QDoubleSpinBox()
        self.sp_obj_l.setSuffix(" mm")
        self.sp_obj_l.setKeyboardTracking(False)
        self.sp_obj_l.editingFinished.connect(lambda: self._log_manual_slit("Object Slit Left", self.sp_obj_l))

        self.sp_obj_r = QDoubleSpinBox()
        self.sp_obj_r.setSuffix(" mm")
        self.sp_obj_r.setKeyboardTracking(False)
        self.sp_obj_r.editingFinished.connect(lambda: self._log_manual_slit("Object Slit Right", self.sp_obj_r))

        slit_layout.addWidget(self.sp_obj_l)
        slit_layout.addWidget(self.sp_obj_r)
        layout.addLayout(slit_layout, 1, 1, 1, 2)

        group.setLayout(layout)
        self.main_layout.addWidget(group)

    def _init_magnet_ui(self):
        group = QGroupBox("2. Mass Analyzer Magnet")
        layout = QGridLayout()

        self.lbl_mag_cooling = QLabel("COOLING: UNKNOWN")
        self.lbl_mag_cooling.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_mag_cooling.setStyleSheet(COLOR_INACTIVE)
        layout.addWidget(self.lbl_mag_cooling, 0, 0, 1, 2)

        layout.addWidget(QLabel("<b>Magnet Current:</b>"), 1, 0)
        self.lbl_mag_rb = QLabel("RB: --- A | --- V")
        layout.addWidget(self.lbl_mag_rb, 1, 1)

        self.sp_mag_current = QDoubleSpinBox()
        self.sp_mag_current.setRange(0.0, 50.0)
        self.sp_mag_current.setSuffix(" A")
        self.sp_mag_current.setDecimals(2)
        self.sp_mag_current.setSingleStep(0.01)
        self.sp_mag_current.setKeyboardTracking(False)
        self.sp_mag_current.editingFinished.connect(
            lambda: self._dispatch_command("ion_beam.beamline.magnet.sp_requested_current",
                                           self.sp_mag_current.value()))
        layout.addWidget(self.sp_mag_current, 1, 2)

        self.btn_mag_en = QPushButton("ENABLE MAGNET")
        self.btn_mag_en.setCheckable(True)
        self.btn_mag_en.setStyleSheet(COLOR_BUTTON_STANDARD)
        self.btn_mag_en.clicked.connect(
            lambda *args: self._dispatch_command("ion_beam.beamline.magnet.cmd_enable", self.btn_mag_en.isChecked()))
        layout.addWidget(self.btn_mag_en, 1, 3)

        self.btn_mag_deg = QPushButton("DEGAUSS ROUTINE")
        self.btn_mag_deg.setStyleSheet("background-color: #9C27B0; color: white; font-weight: bold;")
        self.btn_mag_deg.clicked.connect(
            lambda *args: self._dispatch_command("ion_beam.beamline.magnet.cmd_degauss", True))
        layout.addWidget(self.btn_mag_deg, 1, 4)

        calc_frame = QFrame()
        calc_frame.setStyleSheet("background-color: #E3F2FD; border-radius: 4px; padding: 4px;")
        calc_layout = QHBoxLayout(calc_frame)
        calc_layout.setContentsMargins(4, 4, 4, 4)

        calc_layout.addWidget(QLabel("<b>Auto-Tune Mass (amu):</b>"))
        self.sp_mass = QDoubleSpinBox()
        self.sp_mass.setRange(1.0, 250.0)
        self.sp_mass.setDecimals(1)
        self.sp_mass.setSingleStep(1.0)
        self.sp_mass.setValue(28.0)
        self.sp_mass.setKeyboardTracking(False)
        calc_layout.addWidget(self.sp_mass)

        self.btn_calc_mass = QPushButton("CALCULATE && SET AMPS")
        self.btn_calc_mass.setStyleSheet("background-color: #2196F3; color: white; font-weight: bold;")
        self.btn_calc_mass.clicked.connect(self._calculate_mass)
        calc_layout.addWidget(self.btn_calc_mass)

        layout.addWidget(calc_frame, 2, 0, 1, 5)
        group.setLayout(layout)
        self.main_layout.addWidget(group)

    def _calculate_mass(self):
        target_amu = self.sp_mass.value()

        app = QApplication.instance()
        main_win = next(w for w in app.topLevelWidgets() if isinstance(w, QMainWindow))
        cache = main_win.master_telemetry_cache

        extr_kv = cache.get("ion_beam.source.extraction.rb_voltage", 0.0)

        if extr_kv <= 0:
            QMessageBox.warning(self, "Calculation Error", "Beam energy is 0V. Cannot resolve mass.")
            return

        poly_coeffs = self.magnet_config.get("mass_calibration_poly", [0.0, 1.0, 0.0, 0.0])
        x_factor = (target_amu * extr_kv) ** 0.5

        required_amps = sum(coeff * (x_factor ** idx) for idx, coeff in enumerate(poly_coeffs))
        required_amps = max(0.0, min(required_amps, self.sp_mag_current.maximum()))

        self.sp_mag_current.setValue(required_amps)
        self._dispatch_command("ion_beam.beamline.magnet.sp_requested_current", required_amps)

        self.event_helper.log_user_marker(time.time(),
                                          f"Auto-Tuned Magnet to {required_amps:.2f} A for {target_amu} amu at {extr_kv:.2f} kV",
                                          "#2196F3")

    def _init_post_magnet_ui(self):
        group = QGroupBox("3. Post-Magnet Tuning")
        layout = QGridLayout()

        # X-Steerer
        layout.addWidget(QLabel("<b>X-Steerer Voltage:</b>"), 0, 0)

        self.lbl_x_rb = QLabel("RB: --- V")
        self.lbl_x_rb.setMinimumWidth(80)
        layout.addWidget(self.lbl_x_rb, 0, 1)

        self.sp_x_steer = QDoubleSpinBox()
        self.sp_x_steer.setRange(-200.0, 200.0)
        self.sp_x_steer.setSuffix(" V")
        self.sp_x_steer.setDecimals(1)
        self.sp_x_steer.setKeyboardTracking(False)
        self.sp_x_steer.editingFinished.connect(
            lambda: self._dispatch_command("ion_beam.beamline.steering.sp_requested_x_volts", self.sp_x_steer.value()))
        layout.addWidget(self.sp_x_steer, 0, 2)

        # Image Slits
        layout.addWidget(QLabel("<b>Image Slits (L / R):</b>"), 1, 0)
        slit_layout = QHBoxLayout()

        self.sp_img_l = QDoubleSpinBox()
        self.sp_img_l.setSuffix(" mm")
        self.sp_img_l.setKeyboardTracking(False)
        self.sp_img_l.editingFinished.connect(lambda: self._log_manual_slit("Image Slit Left", self.sp_img_l))

        self.sp_img_r = QDoubleSpinBox()
        self.sp_img_r.setSuffix(" mm")
        self.sp_img_r.setKeyboardTracking(False)
        self.sp_img_r.editingFinished.connect(lambda: self._log_manual_slit("Image Slit Right", self.sp_img_r))

        slit_layout.addWidget(self.sp_img_l)
        slit_layout.addWidget(self.sp_img_r)
        layout.addLayout(slit_layout, 1, 1, 1, 2)

        group.setLayout(layout)
        self.main_layout.addWidget(group)

    def _init_downstream_optics_ui(self):
        group = QGroupBox("4. Downstream Lenses && Filters")
        self.optics_layout = QGridLayout()
        self.psu_controls = {}

        self._build_psu_row(0, "Beamline Einzel", "ion_beam.beamline.einzel", "kV", "µA", 0.0, 30.0)
        self._build_psu_row(1, "Neutral Trap (Pos)", "ion_beam.beamline.neutral_trap_pos", "kV", "µA", 0.0, 5.0)
        self._build_psu_row(2, "Neutral Trap (Neg)", "ion_beam.beamline.neutral_trap_neg", "kV", "µA", 0.0, 5.0)

        group.setLayout(self.optics_layout)
        self.main_layout.addWidget(group)

    def _build_psu_row(self, row, name, base_tag, pri_unit, sec_unit, min_v, max_v):
        lbl_name = QLabel(f"<b>{name}</b>")
        lbl_rb = QLabel(f"RB: --- {pri_unit} | --- {sec_unit}")
        lbl_rb.setMinimumWidth(180)

        btn_enable = QPushButton("ENABLE")
        btn_enable.setCheckable(True)
        btn_enable.setStyleSheet(COLOR_BUTTON_STANDARD)
        btn_enable.clicked.connect(
            lambda *args, b=btn_enable, t=base_tag: self._dispatch_command(f"{t}.cmd_enable", b.isChecked()))

        sp_box = QDoubleSpinBox()
        sp_box.setRange(min_v, max_v)
        sp_box.setSuffix(f" {pri_unit}")
        sp_box.setDecimals(3)
        sp_box.setSingleStep(0.001)
        sp_box.setKeyboardTracking(False)
        sp_box.editingFinished.connect(
            lambda t=base_tag, b=sp_box: self._dispatch_spellman_setpoints(t, b.value()))

        self.optics_layout.addWidget(lbl_name, row, 0)
        self.optics_layout.addWidget(lbl_rb, row, 1)
        self.optics_layout.addWidget(sp_box, row, 2)
        self.optics_layout.addWidget(btn_enable, row, 3)

        self.psu_controls[name] = {"lbl_rb": lbl_rb, "sp_box": sp_box, "btn_enable": btn_enable, "base_tag": base_tag,
                                   "pri_unit": pri_unit, "sec_unit": sec_unit}

    def update_telemetry(self, data: dict):
        master_comms_lost = not bool(data.get("system.connected", False)) or bool(
            data.get("ion_beam.system.pc_plc_comms_lost", False))
        relay_active = bool(data.get("ion_beam.facilities.safety_relay_active", False))

        services = data.get("manager.services", {})
        mag_state = services.get("service_magnet_psu", "OFFLINE")
        spellman_state = services.get("service_spellman_mpd", "OFFLINE")
        events_state = services.get("service_events", "OFFLINE")

        # --- 1. Update Magnet State ---
        mag_cool = data.get("ion_beam.facilities.stat_mag_coolant_ok")

        if master_comms_lost:
            self.lbl_mag_cooling.setText("MAG COOLING: UNKNOWN")
            self.lbl_mag_cooling.setStyleSheet(COLOR_INACTIVE)
        elif mag_cool == True:
            self.lbl_mag_cooling.setText("MAG COOLING: FLOW & TEMP OK")
            self.lbl_mag_cooling.setStyleSheet(COLOR_OK)
        else:
            self.lbl_mag_cooling.setText("MAG COOLING: FAULT")
            self.lbl_mag_cooling.setStyleSheet(COLOR_FAULT)

        mag_v = data.get("ion_beam.beamline.magnet.rb_voltage")
        mag_i = data.get("ion_beam.beamline.magnet.rb_current")
        if mag_v is not None and mag_i is not None:
            self.lbl_mag_rb.setText(f"RB: {mag_i:.2f} A | {mag_v:.2f} V")

        mag_en = data.get("ion_beam.beamline.magnet.stat_enabled")
        mag_deg = data.get("ion_beam.beamline.magnet.stat_degaussing")

        if mag_en is not None:
            self.btn_mag_en.setChecked(bool(mag_en))
            self.btn_mag_en.setStyleSheet(COLOR_OK if mag_en else COLOR_BUTTON_STANDARD)

        mag_lock_reason = ""
        if mag_state != "ONLINE":
            mag_lock_reason = "Magnet microservice is offline."
        elif master_comms_lost:
            mag_lock_reason = "Safety Relay state is unknown (PLC offline)."
        elif not relay_active:
            mag_lock_reason = "Safety Relay is De-Energized."
        elif mag_cool is False:
            mag_lock_reason = "Magnet hardware interlock/cooling fault."

        mag_lockout = bool(mag_lock_reason)
        mag_tt = f"Disabled: {mag_lock_reason}" if mag_lockout else "Click to enable Magnet Output"

        self.btn_mag_en.setEnabled(not mag_lockout)
        self.btn_mag_en.setToolTip(mag_tt)
        self.sp_mag_current.setEnabled(not mag_lockout)
        self.sp_mag_current.setToolTip(mag_tt)
        self.btn_mag_deg.setEnabled(not mag_lockout)
        self.btn_mag_deg.setToolTip(
            f"Disabled: {mag_lock_reason}" if mag_lockout else "Click to trigger autonomous degaussing sequence")
        self.btn_calc_mass.setEnabled(not mag_lockout)
        self.btn_calc_mass.setToolTip(
            f"Disabled: {mag_lock_reason}" if mag_lockout else "Click to auto-tune magnet current for target mass")

        if mag_deg:
            self.btn_mag_deg.setText("DEGAUSSING...")
            self.btn_mag_deg.setStyleSheet("background-color: #E91E63; color: white; font-weight: bold;")
        else:
            self.btn_mag_deg.setText("DEGAUSS ROUTINE")
            self.btn_mag_deg.setStyleSheet("background-color: #9C27B0; color: white; font-weight: bold;")

        # --- 2. Update Steerers ---
        # PLC Telemetry handling
        steer_en = bool(data.get("ion_beam.beamline.steering.stat_enabled", False))
        self.btn_steer_en.setChecked(steer_en)
        self.btn_steer_en.setStyleSheet(COLOR_OK if steer_en else COLOR_BUTTON_STANDARD)

        # Read the Actual Setpoints active in the PLC
        y_rb = data.get("ion_beam.beamline.steering.sp_actual_y_volts")
        x_rb = data.get("ion_beam.beamline.steering.sp_actual_x_volts")

        # The text label ALWAYS tracks the true PLC output
        self.lbl_y_rb.setText(f"RB: {y_rb:.1f} V" if y_rb is not None else "RB: --- V")
        self.lbl_x_rb.setText(f"RB: {x_rb:.1f} V" if x_rb is not None else "RB: --- V")

        # The Spinbox ONLY syncs to the PLC if the steerer is enabled.
        # This prevents your typed setpoint from zeroing out when the output drops.
        if y_rb is not None and steer_en and not self.sp_y_steer.hasFocus():
            self.sp_y_steer.blockSignals(True)
            self.sp_y_steer.setValue(float(y_rb))
            self.sp_y_steer.blockSignals(False)

        if x_rb is not None and steer_en and not self.sp_x_steer.hasFocus():
            self.sp_x_steer.blockSignals(True)
            self.sp_x_steer.setValue(float(x_rb))
            self.sp_x_steer.blockSignals(False)

        # Steerer Lockouts
        steer_lock_reason = ""
        if master_comms_lost:
            steer_lock_reason = "PLC communications are offline."
        elif not relay_active:
            steer_lock_reason = "Safety Relay is De-Energized."

        steer_lockout = bool(steer_lock_reason)

        self.btn_steer_en.setEnabled(not steer_lockout)
        self.btn_steer_en.setToolTip(
            f"Disabled: {steer_lock_reason}" if steer_lockout else "Enable Beam Steerer Outputs")
        self.sp_y_steer.setEnabled(not steer_lockout)
        self.sp_y_steer.setToolTip(
            f"Disabled: {steer_lock_reason}" if steer_lockout else "Adjust Y-Axis Beam Deflection")
        self.sp_x_steer.setEnabled(not steer_lockout)
        self.sp_x_steer.setToolTip(
            f"Disabled: {steer_lock_reason}" if steer_lockout else "Adjust X-Axis Beam Deflection")

        # --- 3. Update Manual Slit Inputs ---
        slit_lock_reason = "Events microservice is offline (Cannot log manual adjustments)."
        slit_lockout = (events_state != "ONLINE")
        slit_tt = f"Disabled: {slit_lock_reason}" if slit_lockout else "Update Database Log with physical slit dimensions"

        for slit_sp in [self.sp_obj_l, self.sp_obj_r, self.sp_img_l, self.sp_img_r]:
            slit_sp.setEnabled(not slit_lockout)
            slit_sp.setToolTip(slit_tt)

        # --- 4. Update Spellman Downstream Optics ---
        spell_lock_reason = ""
        if spellman_state != "ONLINE":
            spell_lock_reason = "Spellman microservice is offline."
        elif master_comms_lost:
            spell_lock_reason = "Safety Relay state is unknown (PLC offline)."
        elif not relay_active:
            spell_lock_reason = "Safety Relay is De-Energized."

        spell_lockout = bool(spell_lock_reason)
        spell_tt = f"Disabled: {spell_lock_reason}" if spell_lockout else ""

        for name, ctrl in self.psu_controls.items():
            base_tag = ctrl["base_tag"]

            rb_v = data.get(f"{base_tag}.rb_voltage")
            rb_i = data.get(f"{base_tag}.rb_current")
            v_str = f"{rb_v:.2f}" if rb_v is not None else "---"
            i_str = f"{rb_i:.2f}" if rb_i is not None else "---"
            ctrl["lbl_rb"].setText(f"RB: {v_str} {ctrl['pri_unit']} | {i_str} {ctrl['sec_unit']}")

            sp_val = data.get(f"{base_tag}.sp_actual_voltage")
            if sp_val is not None and not ctrl["sp_box"].hasFocus():
                ctrl["sp_box"].blockSignals(True)
                ctrl["sp_box"].setValue(float(sp_val))
                ctrl["sp_box"].blockSignals(False)

            stat_en = data.get(f"{base_tag}.stat_enabled")
            if stat_en is not None:
                ctrl["btn_enable"].setChecked(bool(stat_en))
                ctrl["btn_enable"].setStyleSheet(COLOR_OK if stat_en else COLOR_BUTTON_STANDARD)

            ctrl["btn_enable"].setEnabled(not spell_lockout)
            ctrl["btn_enable"].setToolTip(spell_tt if spell_lockout else "Click to toggle power output")
            ctrl["sp_box"].setEnabled(not spell_lockout)
            ctrl["sp_box"].setToolTip(spell_tt)

# --- System Diagnostics Subsystem Component ---

class DiagnosticsWidget(QWidget):
    def __init__(self, cmd_thread, fault_engine):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.fault_engine = fault_engine
        self.last_manager_beat = 0

        main_layout = QHBoxLayout(self)

        svc_group = QGroupBox("Microservice Infrastructure")
        self.svc_layout = QVBoxLayout()

        self.lbl_manager_status = QLabel("<b>SERVICE MANAGER:</b> WAITING FOR DATA...")
        self.lbl_manager_status.setStyleSheet(COLOR_WARNING)
        self.svc_layout.addWidget(self.lbl_manager_status)

        self.service_rows = {}
        self.svc_layout.addStretch()
        svc_group.setLayout(self.svc_layout)

        fault_group = QGroupBox("Hardware && Software Fault Registry")
        fault_layout = QVBoxLayout()

        fault_layout.addWidget(QLabel("<b>ACTIVE ALARMS:</b>"))
        self.active_alarms_list = QListWidget()
        self.active_alarms_list.setStyleSheet("background-color: #ffebee; color: #b71c1c; font-weight: bold;")
        self.active_alarms_list.setMaximumHeight(150)
        fault_layout.addWidget(self.active_alarms_list)

        fault_layout.addWidget(QLabel("<b>DIAGNOSTIC BIT MAP:</b>"))
        self.fault_labels = {}

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)

        for udt_name, udt_info in self.fault_engine.fault_structures.items():
            clean_name = udt_name.replace("UDT_Fault_Word_", "").replace("_", " ")
            gb = QGroupBox(clean_name)
            gl = QGridLayout()

            row, col = 0, 0
            for bit_str, info in udt_info.get("bits", {}).items():
                lbl = QLabel(info["name"])
                lbl.setStyleSheet(COLOR_OK)
                lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

                desc = info.get("description", "No description provided.")
                lbl.setToolTip(f"<b>{info['name']}</b><br>{desc}<br><i>Status: OK</i>")

                gl.addWidget(lbl, row, col)
                self.fault_labels[info["name"]] = {"label": lbl, "desc": desc}

                col += 1
                if col > 3:
                    col = 0
                    row += 1

            gb.setLayout(gl)
            scroll_layout.addWidget(gb)

        scroll_layout.addStretch()
        scroll.setWidget(scroll_content)
        fault_layout.addWidget(scroll)
        fault_group.setLayout(fault_layout)

        main_layout.addWidget(svc_group, stretch=1)
        main_layout.addWidget(fault_group, stretch=3)

    def _cmd_restart_service(self, svc_name):
        payload = {"command": "restart", "service": svc_name}
        self.cmd_thread.send_command("manager", "manager_cmd", payload)

    def update_telemetry(self, data: dict):
        master_comms_lost = not bool(data.get("system.connected", False)) or bool(
            data.get("ion_beam.system.pc_plc_comms_lost", False))

        services = data.get("manager.services")
        now = time.time()

        if services:
            self.last_manager_beat = now
            self.lbl_manager_status.setText("<b>SERVICE MANAGER:</b> ONLINE")
            self.lbl_manager_status.setStyleSheet("color: #4CAF50; font-size: 11pt;")

            for svc_name, state in services.items():
                if svc_name not in self.service_rows:
                    row_widget = QWidget()
                    row_layout = QHBoxLayout(row_widget)
                    row_layout.setContentsMargins(0, 0, 0, 0)

                    lbl = QLabel(f"<b>{svc_name}:</b> {state}")
                    btn_restart = QPushButton("Restart")
                    btn_restart.setStyleSheet(COLOR_BUTTON_STANDARD)
                    btn_restart.clicked.connect(lambda checked, s=svc_name: self._cmd_restart_service(s))

                    row_layout.addWidget(lbl)
                    row_layout.addWidget(btn_restart)
                    self.svc_layout.insertWidget(self.svc_layout.count() - 1, row_widget)
                    self.service_rows[svc_name] = {"label": lbl, "btn": btn_restart}

                lbl = self.service_rows[svc_name]["label"]
                lbl.setText(f"<b>{svc_name}:</b> {state}")
                if state == "ONLINE":
                    lbl.setStyleSheet("color: #4CAF50;")  # Green
                elif state == "STARTING":
                    # Flashes a vibrant Blue/Orange color to clearly indicate initialization
                    lbl.setStyleSheet("color: #2196F3; font-weight: bold;")
                elif state in ["CRASHED", "HANGING"]:
                    lbl.setStyleSheet("color: #F44336; font-weight: bold;")  # Red
                else:
                    lbl.setStyleSheet("color: #757575;")  # Gray

        if now - self.last_manager_beat > 2.5:
            self.lbl_manager_status.setText("<b>SERVICE MANAGER:</b> OFFLINE")
            self.lbl_manager_status.setStyleSheet(
                "background-color: #F44336; color: white; font-size: 11pt; padding: 4px;")
            for row in self.service_rows.values():
                row["label"].setStyleSheet("color: #757575;")
                row["label"].setText(row["label"].text().split(":")[0] + ": UNKNOWN")
                row["btn"].setEnabled(False)
        else:
            for row in self.service_rows.values():
                row["btn"].setEnabled(True)

        word_mapping = {
            "ion_beam.faults.word_0_system": "UDT_Fault_Word_0_System",
            "ion_beam.faults.word_1_pumps": "UDT_Fault_Word_1_Pumps",
            "ion_beam.faults.word_2_source": "UDT_Fault_Word_2_Source",
            #"ion_beam.faults.word_3_spellman": "UDT_Fault_Word_3_Spellman",
            "ion_beam.faults.word_4_magnet": "UDT_Fault_Word_4_Magnet",
            "ion_beam.faults.word_5_gauges": "UDT_Fault_Word_5_Gauges"
        }

        if master_comms_lost:
            for item in self.fault_labels.values():
                item["label"].setStyleSheet(COLOR_INACTIVE)
                item["label"].setToolTip("Comms Offline")
            return

        self.active_alarms_list.clear()

        for tag, udt_key in word_mapping.items():
            raw_word = data.get(tag)
            if raw_word is not None:
                active_faults = self.fault_engine.get_active_faults(udt_key, int(raw_word))
                active_names = [f["name"] for f in active_faults]

                udt_info = self.fault_engine.fault_structures.get(udt_key, {})
                for bit_str, info in udt_info.get("bits", {}).items():
                    name = info["name"]
                    if name in self.fault_labels:
                        lbl_item = self.fault_labels[name]
                        lbl = lbl_item["label"]
                        desc = lbl_item["desc"]

                        if name in active_names:
                            lbl.setStyleSheet(COLOR_FAULT)
                            lbl.setToolTip(f"<b>{name}</b><br>{desc}<br><i style='color:red;'>Status: FAULT ACTIVE</i>")
                            list_item = QListWidgetItem(f"⚠️ {name.upper()}: {desc}")
                            self.active_alarms_list.addItem(list_item)
                        else:
                            lbl.setStyleSheet(COLOR_OK)
                            lbl.setToolTip(f"<b>{name}</b><br>{desc}<br><i style='color:green;'>Status: OK</i>")


# --- Configuration Editor Form (Dynamic Dialog) ---

class ConfigEditorDialog(QDialog):
    def __init__(self, cmd_thread, parent=None):
        super().__init__(parent)
        self.setWindowTitle("System Configuration Editor")
        self.resize(700, 700)
        self.cmd_thread = cmd_thread
        self.config_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../config"))

        self.CONFIG_TO_SERVICE = {
            "fault_config.json": "ALL SERVICES",
            "network_config.json": "ALL SERVICES",
            "logger_config.json": "service_logger",
            "magnet_config.json": "service_magnet_psu",
            "mpd_config.json": "service_spellman_mpd",
            "vac_gauges_config.json": "service_vac_gauge_controllers"
        }

        layout = QVBoxLayout(self)

        top_layout = QHBoxLayout()
        top_layout.addWidget(QLabel("<b>Target Config:</b>"))

        self.cb_files = QComboBox()
        self._populate_files()
        self.cb_files.currentTextChanged.connect(self._load_file)
        top_layout.addWidget(self.cb_files)

        top_layout.addWidget(QLabel("<b>Target Service:</b>"))
        self.cb_service = QComboBox()
        self.cb_service.addItems([
            "ALL SERVICES", "service_plc", "service_magnet_psu",
            "service_source_turbo", "service_spellman_mpd",
            "service_logger", "service_vac_gauge_controllers"
        ])
        self.cb_service.currentTextChanged.connect(self._update_button_text)
        top_layout.addWidget(self.cb_service)

        layout.addLayout(top_layout)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Configuration Key", "Value (Auto-Typed)"])
        self.tree.setColumnWidth(0, 300)
        self.tree.setAlternatingRowColors(True)
        layout.addWidget(self.tree)

        self.btn_save = QPushButton("💾 APPLY CHANGES && RESTART SERVICE")
        self.btn_save.setStyleSheet(
            "background-color: #2196F3; color: white; font-weight: bold; padding: 10px; font-size: 11pt;")
        self.btn_save.clicked.connect(self._save_and_restart)
        layout.addWidget(self.btn_save)

        if self.cb_files.count() > 0:
            self._load_file(self.cb_files.currentText())

    def _populate_files(self):
        if not os.path.exists(self.config_dir): return
        files = [f for f in os.listdir(self.config_dir) if f.endswith(".json") and f != "system_tags.json"]
        self.cb_files.addItems(files)

    def _update_button_text(self, svc_name=None):
        svc_name = svc_name or self.cb_service.currentText()
        if svc_name == "ALL SERVICES":
            self.btn_save.setText("💾 APPLY CHANGES && RESTART ALL SERVICES")
        else:
            self.btn_save.setText(f"💾 APPLY CHANGES && RESTART {svc_name.upper()}")

    def _load_file(self, filename):
        if not filename: return

        target_svc = self.CONFIG_TO_SERVICE.get(filename, "service_plc")
        if self.cb_service.findText(target_svc) == -1:
            self.cb_service.addItem(target_svc)
        self.cb_service.setCurrentText(target_svc)
        self._update_button_text(target_svc)

        self.tree.clear()
        path = os.path.join(self.config_dir, filename)
        try:
            with open(path, 'r') as f:
                data = orjson.loads(f.read())
            # Inject parent_key=None to start
            self._populate_tree(self.tree.invisibleRootItem(), data, parent_key=None)
            self.tree.expandAll()
        except Exception as e:
            QMessageBox.critical(self, "Parse Error", f"Failed to parse JSON:\n{e}")

    def _populate_tree(self, parent_node, data, parent_key=None):
        if isinstance(data, dict):
            for key, value in data.items():
                item = QTreeWidgetItem(parent_node)
                item.setText(0, str(key))

                # NEW: Allow editable keys for specific sub-dictionaries
                if parent_key == "devices":
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
                    item.setBackground(0, QColor("#E3F2FD"))
                    item.setToolTip(0, "Double click to rename this device key.")

                if isinstance(value, (dict, list)):
                    self._populate_tree(item, value, parent_key=key)
                else:
                    self._add_edit_widget(item, key, value)
        elif isinstance(data, list):
            for i, value in enumerate(data):
                item = QTreeWidgetItem(parent_node)
                item.setText(0, f"[{i}]")
                if isinstance(value, (dict, list)):
                    self._populate_tree(item, value, parent_key=None)
                else:
                    self._add_edit_widget(item, f"[{i}]", value)

    def _add_edit_widget(self, item, key, value):
        if isinstance(value, bool):
            w = QCheckBox()
            w.setChecked(value)
        elif isinstance(value, int):
            w = QSpinBox()
            w.setRange(-2147483648, 2147483647)
            w.setValue(value)
        elif isinstance(value, float):
            text_val = f"{value:g}"
            if 'e' not in text_val.lower() and abs(value) < 0.001 and value != 0.0:
                text_val = f"{value:.2e}"
            w = QLineEdit(text_val)
        else:
            w = QLineEdit(str(value))

        # NEW: Lock out the bus_id from edits
        if key == "bus_id":
            w.setReadOnly(True)
            w.setStyleSheet("background-color: #EEEEEE; color: #757575;")
            w.setToolTip("Bus ID cannot be changed as it maps to physical hardware and network configs.")

        item.setData(0, Qt.ItemDataRole.UserRole, type(value))
        self.tree.setItemWidget(item, 1, w)

    def _extract_tree(self, node):
        if node.childCount() == 0:
            w = self.tree.itemWidget(node, 1)
            orig_type = node.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(w, QCheckBox):
                return w.isChecked()
            elif isinstance(w, QSpinBox):
                return w.value()
            elif isinstance(w, QLineEdit):
                val = w.text()
                if orig_type == float:
                    try:
                        return float(val)
                    except ValueError:
                        raise ValueError(f"Invalid float format: {val}")
                elif orig_type == int:
                    try:
                        return int(val)
                    except ValueError:
                        raise ValueError(f"Invalid integer format: {val}")
                else:
                    return val

        first_child_key = node.child(0).text(0)
        if first_child_key.startswith("[") and first_child_key.endswith("]"):
            res = []
            for i in range(node.childCount()):
                res.append(self._extract_tree(node.child(i)))
            return res
        else:
            res = {}
            for i in range(node.childCount()):
                child = node.child(i)
                # child.text(0) extracts the (potentially edited) dictionary key!
                res[child.text(0)] = self._extract_tree(child)
            return res

    def _save_and_restart(self):
        filename = self.cb_files.currentText()
        if not filename: return

        try:
            new_data = self._extract_tree(self.tree.invisibleRootItem())
            path = os.path.join(self.config_dir, filename)

            with open(path, 'wb') as f:
                f.write(orjson.dumps(new_data, option=orjson.OPT_INDENT_2))

            registry_triggers = ["fault_config.json", "mpd_config.json", "vac_gauges_config.json"]
            if filename in registry_triggers:
                try:
                    import subprocess
                    build_script = os.path.abspath(os.path.join(self.config_dir, "../src/core/build_registry.py"))
                    if os.path.exists(build_script):
                        subprocess.run([sys.executable, build_script], check=True)
                        print("[GUI Output] Successfully rebuilt system_tags.json")
                except Exception as e:
                    QMessageBox.warning(self, "Build Error", f"Config saved, but build_registry.py failed:\n{e}")
                    return

            target_svc = self.cb_service.currentText()

            if target_svc == "ALL SERVICES":
                master_list = [
                    "service_events", "service_logger", "service_data_compactor",
                    "service_plc", "service_vac_gauge_controllers",
                    "service_source_turbo", "service_magnet_psu", "service_spellman_mpd"
                ]
                for svc in master_list:
                    self.cmd_thread.send_command("manager", "manager_cmd", {"command": "restart", "service": svc})
                    time.sleep(0.02)

                msg = f"Saved {filename} && Rebuilt Tags!" if filename in registry_triggers else f"Saved {filename}!"
                QMessageBox.information(self, "Success", f"{msg}\nMass restart broadcasted to ALL SERVICES.")
            else:
                payload = {"command": "restart", "service": target_svc}
                self.cmd_thread.send_command("manager", "manager_cmd", payload)

                msg = f"Saved {filename} && Rebuilt Tags!" if filename in registry_triggers else f"Saved {filename}!"
                QMessageBox.information(self, "Success", f"{msg}\nRestart command sent to {target_svc}.")

            self.accept()

        except Exception as e:
            QMessageBox.critical(self, "Save Error", f"Failed to compile config. Check your inputs:\n\n{e}")


# --- Master Framework ---

class ControlMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("IPIDS Operator HMI")
        self.resize(1600, 900)
        self.setDockOptions(QMainWindow.DockOption.AllowNestedDocks | QMainWindow.DockOption.AllowTabbedDocks)

        # --- AUDIO SUBSYSTEM ---
        self.audio = AudioManager()

        # --- EDGE DETECTION MEMORY ---
        self.memory_active_faults = set()
        self.memory_vg_warnings = {i: False for i in range(1, 7)}
        self.memory_vg_trips = {i: False for i in range(1, 7)}
        self.memory_comms_lost = False

        toolbar = QToolBar("Main Toolbar")
        self.addToolBar(toolbar)

        btn_settings = QPushButton("⚙️ CONFIG EDITOR")
        btn_settings.setStyleSheet("font-weight: bold; padding: 4px;")
        btn_settings.clicked.connect(self._show_config_editor)
        toolbar.addWidget(btn_settings)

        current_dir = os.path.dirname(os.path.abspath(__file__))
        fault_map_path = os.path.abspath(os.path.join(current_dir, "../../../config/fault_config.json"))
        self.fault_engine = FaultRegistry(fault_map_path)

        self.cmd_thread = ZMQCommandThread()
        self.cmd_thread.start()

        self.event_helper = EventHelper("hmi_control")

        self.master_telemetry_cache = {}

        telemetry_ports = [
            ZMQ_PORT_PLC_PUB, ZMQ_PORT_VACUUM_PUB,
            ZMQ_PORT_SRC_TURBO_PUB, ZMQ_PORT_MANAGER_PUB,
            ZMQ_PORT_SPELLMAN_PUB, ZMQ_PORT_MAGNET_PUB,
        ]
        self.telemetry_thread = ZMQTelemetryThread(telemetry_ports)
        self.telemetry_thread.data_received.connect(self._route_telemetry)
        self.telemetry_thread.start()

        self.subsystems = {}
        self._init_docks()

        # 2. ADD MAGNET TO WATCHDOG TRACKER
        self.last_seen = {
            "plc": 0.0,
            "vacuum": 0.0,
            "src_turbo": 0.0,
            "magnet": 0.0
        }

        self.watchdog_timer = QTimer(self)
        self.watchdog_timer.timeout.connect(self._ui_update_loop)
        self.watchdog_timer.start(100)

    def _show_config_editor(self):
        pwd, ok = QInputDialog.getText(self, "Authentication Required", "Enter Engineering Password:",
                                       QLineEdit.EchoMode.Password)
        if ok and pwd == "admin":
            editor = ConfigEditorDialog(self.cmd_thread, self)
            editor.exec()
        elif ok:
            QMessageBox.warning(self, "Access Denied", "Incorrect password.")

    def _init_docks(self):
        # 1. Left Area Docks
        vac_dock = QDockWidget("Vacuum System", self)
        vac_dock.setObjectName("VacuumDock")
        self.vac_widget = VacuumControlWidget(self.cmd_thread, self.fault_engine)
        vac_dock.setWidget(self.vac_widget)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, vac_dock)
        self.subsystems["vacuum"] = self.vac_widget

        log_dock = QDockWidget("Experiment Logging", self)
        log_dock.setObjectName("LogDock")
        self.log_widget = ExperimentLoggerWidget(self.event_helper)
        log_dock.setWidget(self.log_widget)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, log_dock)
        self.subsystems["logging"] = self.log_widget

        # Tabify Left Area and bring Vacuum to front
        self.tabifyDockWidget(vac_dock, log_dock)
        vac_dock.raise_()

        # 2. Right Area Docks
        src_dock = QDockWidget("TESS Ion Source Control", self)
        src_dock.setObjectName("SourceDock")
        self.src_widget = IonSourceWidget(self.cmd_thread, self.fault_engine)
        src_dock.setWidget(self.src_widget)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, src_dock)
        self.subsystems["source"] = self.src_widget

        optics_dock = QDockWidget("Beamline Optics", self)
        optics_dock.setObjectName("OpticsDock")
        self.optics_widget = BeamlineOpticsWidget(self.cmd_thread, self.event_helper)
        optics_dock.setWidget(self.optics_widget)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, optics_dock)
        self.subsystems["optics"] = self.optics_widget

        # Tabify Right Area and bring Source to front
        self.tabifyDockWidget(src_dock, optics_dock)
        src_dock.raise_()

        # 3. Bottom Area Dock
        diag_dock = QDockWidget("System Diagnostics", self)
        diag_dock.setObjectName("DiagDock")
        self.diag_widget = DiagnosticsWidget(self.cmd_thread, self.fault_engine)
        diag_dock.setWidget(self.diag_widget)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, diag_dock)
        self.subsystems["diagnostics"] = self.diag_widget

    def _route_telemetry(self, fresh_data: dict):
        now = time.time()

        if "system.connected" in fresh_data:
            self.last_seen["plc"] = now
            fresh_data["system.connected"] = True

        if "ion_beam.source.turbo_pump.rb_speed_hz" in fresh_data:
            self.last_seen["src_turbo"] = now
            fresh_data["pump.connected"] = True

        if any("vacuum_gauge" in key for key in fresh_data.keys()):
            self.last_seen["vacuum"] = now
            fresh_data["vacuum.connected"] = True

        if "ion_beam.beamline.magnet.rb_voltage" in fresh_data:
            self.last_seen["magnet"] = now
            fresh_data["magnet.connected"] = True

        self.master_telemetry_cache.update(fresh_data)

    def _ui_update_loop(self):
        now = time.time()

        if now - self.last_seen["plc"] > 2.5 and self.master_telemetry_cache.get("system.connected", True):
            print("[Watchdog] PLC ZMQ stream lost!")
            self.master_telemetry_cache["system.connected"] = False

        if now - self.last_seen["src_turbo"] > 2.5 and self.master_telemetry_cache.get("pump.connected", True):
            print("[Watchdog] Turbo Pump ZMQ stream lost!")
            self.master_telemetry_cache["pump.connected"] = False

        if now - self.last_seen["vacuum"] > 2.5 and self.master_telemetry_cache.get("vacuum.connected", True):
            print("[Watchdog] Vacuum Gauge ZMQ stream lost!")
            self.master_telemetry_cache["vacuum.connected"] = False

        # 5. MAGNET WATCHDOG TIMEOUT
        if now - self.last_seen["magnet"] > 2.5 and self.master_telemetry_cache.get("magnet.connected", True):
            print("[Watchdog] Magnet ZMQ stream lost!")
            self.master_telemetry_cache["magnet.connected"] = False

        self.vac_widget.update_telemetry(self.master_telemetry_cache)
        self.src_widget.update_telemetry(self.master_telemetry_cache)
        self.optics_widget.update_telemetry(self.master_telemetry_cache)
        self.diag_widget.update_telemetry(self.master_telemetry_cache)
        self.log_widget.update_telemetry(self.master_telemetry_cache)

        # Evaluate audio triggers after UI components process data
        self._evaluate_audio_triggers()

    def _evaluate_audio_triggers(self):
        data = self.master_telemetry_cache

        comms_lost = not bool(data.get("system.connected", False)) or bool(
            data.get("ion_beam.system.pc_plc_comms_lost", False))
        if comms_lost and not self.memory_comms_lost:
            self.audio.play("critical")
        self.memory_comms_lost = comms_lost

        if comms_lost:
            return

        # Updated locations to match the new Area-based Vertical SCL
        gauge_locations = {1: "source", 2: "beamline", 3: "beamline", 4: "beamline", 5: "endstation", 6: "loadlock"}
        for i in range(1, 7):
            loc = gauge_locations[i]
            approaching = bool(data.get(f"ion_beam.{loc}.vacuum_gauge_{i}.stat_approaching_sp", False))
            tripped = bool(data.get(f"ion_beam.{loc}.vacuum_gauge_{i}.stat_above_sp", False))

            if approaching and not self.memory_vg_warnings[i]:
                self.audio.play("warning")
            self.memory_vg_warnings[i] = approaching

            if tripped and not self.memory_vg_trips[i]:
                self.audio.play("trip")
            self.memory_vg_trips[i] = tripped

        current_active_faults = set()
        word_mapping = {
            "ion_beam.faults.word_0_system": "UDT_Fault_Word_0_System",
            "ion_beam.faults.word_1_vacuum": "UDT_Fault_Word_1_Vacuum",
            "ion_beam.faults.word_2_source": "UDT_Fault_Word_2_Source",
        }

        for tag, udt_key in word_mapping.items():
            raw_word = data.get(tag)
            if raw_word is not None:
                faults = self.fault_engine.get_active_faults(udt_key, int(raw_word))
                for f in faults:
                    current_active_faults.add(f["name"])

        new_faults = current_active_faults - self.memory_active_faults
        if new_faults:
            self.audio.play("fault")

        self.memory_active_faults = current_active_faults

    def closeEvent(self, event):
        self.telemetry_thread.stop()
        self.cmd_thread.stop()
        super().closeEvent(event)


# Enforce non-pageable high-priority scheduling class immediately upon thread launch
if __name__ == "__main__":
    harden_windows_process()
    app = QApplication(sys.argv)
    window = ControlMainWindow()
    window.show()
    sys.exit(app.exec())