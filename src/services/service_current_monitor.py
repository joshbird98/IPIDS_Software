import time
import zmq
import os
import socket
from typing import Dict, Any
import orjson

if os.name == 'nt':
    import ctypes

    ctypes.windll.winmm.timeBeginPeriod(1)

from pymodbus.client import ModbusTcpClient
from src.core.event_helper import EventHelper
from src.core.os_helper import harden_windows_process

from src.core.network_map import (
    ZMQ_PORT_ADAM_PUB, ZMQ_PORT_ADAM_CMD, TOPIC_ADAM_DATA, ZMQ_PORT_HEARTBEAT
)

# --- Network & Hardware Parameters ---
ADAM_IP = '192.168.1.13'
TCP_PORT = 502
UDP_PORT = 1025
NUM_BNC = 8
RESISTOR_OHMS = 100000.0
POLL_INTERVAL = 0.1  # 100ms

# --- Nomenclature Mapping ---
BASE_PATHS = [
    "ion_beam.beamline.object_slits.left",
    "ion_beam.beamline.object_slits.right",
    "ion_beam.beamline.straight_through",
    "ion_beam.beamline.image_slits.left",
    "ion_beam.beamline.image_slits.right",
    "ion_beam.beamline.faraday.front_aperture",
    "ion_beam.system.facilities.adam.ch6",
    "ion_beam.system.facilities.adam.ch7"
]

# Format: BNC_Index : (ADAM_Channel, Reverse_Polarity)
BNC_MAP = [
    (3, False),  # BNC 1 -> ADAM 3
    (1, False),  # BNC 2 -> ADAM 1
    (5, True),  # BNC 3 -> ADAM 5 (Rev)
    (4, True),  # BNC 4 -> ADAM 4 (Rev)
    (2, False),  # BNC 5 -> ADAM 2
    (0, False),  # BNC 6 -> ADAM 0
    (6, True),  # BNC 7 -> ADAM 6 (Rev)
    (7, True)  # BNC 8 -> ADAM 7 (Rev)
]

# --- Range Definitions ---
RANGE_ORDER = [
    "+/- 150 mV",
    "+/- 500 mV",
    "+/- 1 V",
    "+/- 5 V",
    "+/- 10 V"
]

RANGES = {
    "+/- 150 mV": {"code": "0103", "vmax": 0.15},
    "+/- 500 mV": {"code": "0104", "vmax": 0.5},
    "+/- 1 V": {"code": "0140", "vmax": 1.0},
    "+/- 5 V": {"code": "0142", "vmax": 5.0},
    "+/- 10 V": {"code": "0143", "vmax": 10.0}
}


class AdamMicroservice:
    def __init__(self):
        self.context = zmq.Context()

        self.pub_socket = self.context.socket(zmq.PUB)
        self.pub_socket.setsockopt(zmq.SNDHWM, 5)
        self.pub_socket.bind(ZMQ_PORT_ADAM_PUB)

        self.sub_socket = self.context.socket(zmq.SUB)
        self.sub_socket.setsockopt(zmq.RCVHWM, 5)
        self.sub_socket.bind(ZMQ_PORT_ADAM_CMD)
        self.sub_socket.setsockopt_string(zmq.SUBSCRIBE, "")

        self.hb_socket = self.context.socket(zmq.PUB)
        self.hb_socket.connect(ZMQ_PORT_HEARTBEAT)
        self.last_hb_time = 0.0

        self.client = ModbusTcpClient(ADAM_IP, port=TCP_PORT, timeout=0.5)
        self.events = EventHelper("service_adam_monitor")

        # Operational State
        self.op_modes = [0] * NUM_BNC  # 0 = Auto, 1-5 = Fixed Ranges
        self.hardware_ranges = ["+/- 10 V"] * NUM_BNC
        self.cooldowns = [0] * NUM_BNC

        self.state: Dict[str, Any] = {"timestamp": 0.0, "system.cycle_time_ms": 0.0}
        self.comms_fail = False

    def _send_udp_range_cmd(self, adam_channel: int, hex_code: str):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.05)
        cmd = f"$01A{adam_channel:02d}{hex_code}\r"
        try:
            sock.sendto(cmd.encode('ascii'), (ADAM_IP, UDP_PORT))
            sock.recvfrom(1024)
        except socket.timeout:
            pass
        finally:
            sock.close()

    def _initialize_hardware(self):
        for adam_ch in range(8):
            self._send_udp_range_cmd(adam_ch, RANGES["+/- 10 V"]["code"])

    def _change_hardware_range(self, bnc_idx: int, new_range: str):
        self.hardware_ranges[bnc_idx] = new_range
        hex_code = RANGES[new_range]["code"]
        adam_channel = BNC_MAP[bnc_idx][0]
        self._send_udp_range_cmd(adam_channel, hex_code)

        # 10 ticks @ 100ms = 1.0s hardware settling timeout
        self.cooldowns[bnc_idx] = 10

    def _process_commands(self):
        try:
            while True:
                msg = self.sub_socket.recv_json(flags=zmq.NOBLOCK)
                tag = msg.get("tag", "")
                raw_value = msg.get("value", 0)
                ts = msg.get("ts", 0.0)

                # Ignore stale commands > 500ms
                if time.time() - ts > 0.5:
                    continue

                if tag.endswith(".cmd_range"):
                    base_tag = tag.replace(".cmd_range", "")

                    if base_tag in BASE_PATHS:
                        bnc_idx = BASE_PATHS.index(base_tag)

                        try:
                            mode_idx = int(raw_value)
                            if 0 <= mode_idx <= 5:
                                self.op_modes[bnc_idx] = mode_idx

                                if mode_idx != 0:
                                    target_range = RANGE_ORDER[mode_idx - 1]
                                    self._change_hardware_range(bnc_idx, target_range)
                                else:
                                    self.events.log_general(f"CH {bnc_idx} Set to Auto-Range")
                        except (ValueError, TypeError):
                            pass

        except zmq.Again:
            pass

    def _process_autorange(self, bnc_idx: int, raw_val: int, voltage: float):
        if self.op_modes[bnc_idx] != 0 or self.cooldowns[bnc_idx] > 0:
            return

        current_range = self.hardware_ranges[bnc_idx]

        # 1. Over-range Saturation (Immediate jump to max)
        if raw_val > 64500 or raw_val < 1000:
            if current_range != "+/- 10 V":
                self._change_hardware_range(bnc_idx, "+/- 10 V")
            return

        # 2. Optimal Range Finding (Scale Down)
        abs_v = abs(voltage)
        optimal_range = current_range

        for r_name in RANGE_ORDER:
            r_max = RANGES[r_name]["vmax"]
            if abs_v < (r_max * 0.75):
                optimal_range = r_name
                break

        if optimal_range != current_range:
            self._change_hardware_range(bnc_idx, optimal_range)

    def _poll_device(self):
        try:
            response = self.client.read_holding_registers(address=0, count=8)

            if response.isError():
                self.comms_fail = True
            else:
                self.comms_fail = False

                for bnc_idx in range(NUM_BNC):
                    adam_ch = BNC_MAP[bnc_idx][0]
                    reverse = BNC_MAP[bnc_idx][1]
                    raw_val = response.registers[adam_ch]
                    base_tag = BASE_PATHS[bnc_idx]

                    if self.cooldowns[bnc_idx] > 0:
                        self.cooldowns[bnc_idx] -= 1
                        self.state[f"{base_tag}.stat_ranging"] = 1.0
                        continue

                    self.state[f"{base_tag}.stat_ranging"] = 0.0

                    active_range = self.hardware_ranges[bnc_idx]
                    v_max = RANGES[active_range]["vmax"]
                    v_min = -v_max

                    voltage = ((raw_val / 65535.0) * (v_max - v_min)) + v_min
                    if reverse:
                        voltage = -voltage

                    self._process_autorange(bnc_idx, raw_val, voltage)

                    is_overrange = raw_val <= 0 or raw_val >= 65535
                    self.state[f"{base_tag}.stat_overrange"] = 1.0 if is_overrange else 0.0

                    current_amps = voltage / RESISTOR_OHMS
                    self.state[f"{base_tag}.rb_current"] = round(current_amps, 10)
                    self.state[f"{base_tag}.rb_range_idx"] = RANGE_ORDER.index(active_range)
                    self.state[f"{base_tag}.rb_op_mode"] = self.op_modes[bnc_idx]

        except Exception:
            self.comms_fail = True

        self.state["system.facilities.adam.stat_comms_fail"] = 1.0 if self.comms_fail else 0.0

    def run(self):
        self.events.log_general("[ADAM Monitor] Daemon Starting...")
        last_connect_attempt = 0.0
        reconnect_interval = 0.5
        next_tick = time.perf_counter() + POLL_INTERVAL

        try:
            while True:
                cycle_start = time.perf_counter()

                if not self.client.connected:
                    if time.time() - last_connect_attempt > reconnect_interval:
                        last_connect_attempt = time.time()
                        if self.client.connect():
                            self._initialize_hardware()
                    else:
                        self._sync_loop(next_tick)
                        next_tick += POLL_INTERVAL
                        continue

                self._process_commands()
                self._poll_device()

                self.state["timestamp"] = time.time()
                self.state["system.cycle_time_ms"] = round((time.perf_counter() - cycle_start) * 1000, 2)

                try:
                    topic = TOPIC_ADAM_DATA if isinstance(TOPIC_ADAM_DATA, bytes) else TOPIC_ADAM_DATA.encode('utf-8')
                    self.pub_socket.send_multipart([topic, orjson.dumps(self.state)])
                except Exception:
                    pass

                if time.time() - self.last_hb_time >= 0.5:
                    self.hb_socket.send_json({"service": "service_adam_monitor", "ts": time.time()})
                    self.last_hb_time = time.time()

                self._sync_loop(next_tick)
                next_tick += POLL_INTERVAL

        except KeyboardInterrupt:
            self.events.log_general("\n[ADAM Monitor] Process interrupted.")
        finally:
            self.client.close()
            self.pub_socket.close()
            self.sub_socket.close()
            self.hb_socket.close()
            self.context.term()

    def _sync_loop(self, next_tick: float):
        sleep_time = next_tick - time.perf_counter()
        if sleep_time > 0.002:
            time.sleep(sleep_time - 0.002)
        while time.perf_counter() < next_tick:
            pass


if __name__ == "__main__":
    harden_windows_process()
    AdamMicroservice().run()