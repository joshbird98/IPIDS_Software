import time
import os
import zmq
import json
import snap7
from snap7.util import get_bool, get_int, get_dint, get_real, get_dword, set_bool, set_int, set_real
from typing import Dict, Any
import orjson
from src.core.event_helper import EventHelper

# Force Windows high-resolution timers (1ms precision) for OS sleep accuracy
if os.name == 'nt':
    import ctypes

    ctypes.windll.winmm.timeBeginPeriod(1)

from src.core.network_config import (
    ZMQ_PORT_PLC_PUB, ZMQ_PORT_PLC_CMD, TOPIC_PLC_DATA, PLC_IP, PLC_RACK, PLC_SLOT, DB_INTERFACE_NUM,
    ZMQ_PORT_VACUUM_PUB, ZMQ_PORT_SPELLMAN_PUB, ZMQ_PORT_MAGNET_PUB, ZMQ_PORT_SRC_TURBO_PUB,
    ZMQ_PORT_HEARTBEAT)

POLL_INTERVAL = 0.1  # 100ms cycle (10Hz)
HEARTBEAT_INTERVAL = 1.0  # 1Hz Watchdog
MAX_CMD_AGE = 0.5  # TTL: Reject incoming commands older than 500ms



class PlcMicroservice:
    def __init__(self):
        self.context = zmq.Context()

        # --- ZMQ Setup (Defense in Depth: Transport Layer) ---
        self.pub_socket = self.context.socket(zmq.PUB)
        self.pub_socket.setsockopt(zmq.SNDHWM, 5)
        self.pub_socket.bind(ZMQ_PORT_PLC_PUB)

        self.sub_socket = self.context.socket(zmq.SUB)
        self.sub_socket.setsockopt(zmq.RCVHWM, 5)
        self.sub_socket.bind(ZMQ_PORT_PLC_CMD)
        self.sub_socket.setsockopt_string(zmq.SUBSCRIBE, "")

        # --- ZMQ Telemetry Subscriber (Listening to other Python services) ---
        self.telem_socket = self.context.socket(zmq.SUB)
        self.telem_socket.setsockopt(zmq.RCVHWM, 10)
        self.telem_socket.setsockopt_string(zmq.SUBSCRIBE, "")

        self.telem_socket.connect(ZMQ_PORT_VACUUM_PUB)
        self.telem_socket.connect(ZMQ_PORT_SPELLMAN_PUB)
        self.telem_socket.connect(ZMQ_PORT_MAGNET_PUB)
        self.telem_socket.connect(ZMQ_PORT_SRC_TURBO_PUB)

        self.events = EventHelper("service_plc")

        # --- Mailbox Cache (Prevents Snap7 write-flooding) ---
        self.mailbox_cache: Dict[str, Any] = {}

        # --- Snap7 Setup ---
        self.client = snap7.client.Client()
        self.connected = False
        self.watchdog_state = False
        self.last_heartbeat = time.time()
        self.plc_cycle_count = 0

        # --- Tag Registry ---
        self.tags = self._load_tag_registry()
        self.db_interface_size = self._calculate_db_size(DB_INTERFACE_NUM)
        self.fault_map = self._load_fault_map()

        # --- Strict 1D Flat Payload ---
        self.state: Dict[str, Any] = {
            "timestamp": 0.0,
            "system.connected": 0.0,
            "system.cpu_state": "UNKNOWN",
            "system.cycle_time_ms": POLL_INTERVAL * 1000
        }

        # --- Internal IT Watchdogs ---
        self.last_seen = {
            "VACUUM": time.time(),
            "TURBO": time.time(),
            "SPELLMAN": time.time(),
            "MAGNET": time.time()
        }
        self.SERVICE_TIMEOUT_SEC = 1.0

        self.hb_socket = self.context.socket(zmq.PUB)
        self.hb_socket.connect(ZMQ_PORT_HEARTBEAT)
        self.last_hb_time = 0.0

    def _evaluate_word_faults(self):
        """Unpacks Siemens 32-bit DWORDs and logs edge transitions via EventHelper."""

        # Static mapping from 1D payload keys to the fault_map.json UDT names
        word_keys = {
            "ion_beam.faults.word_0": "UDT_Fault_Word_0_System",
            "ion_beam.faults.word_1": "UDT_Fault_Word_1_Pumps",
            "ion_beam.faults.word_2": "UDT_Fault_Word_2_Source",
            "ion_beam.faults.word_3": "UDT_Fault_Word_3_Spellman",
            "ion_beam.faults.word_4": "UDT_Fault_Word_4_Magnet",
            "ion_beam.faults.word_5": "UDT_Fault_Word_5_Gauges"
        }

        # Ensure fault_map and latches exist
        if not hasattr(self, "raw_fault_map"):
            config_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../config/fault_map.json'))
            try:
                with open(config_path, "r") as f:
                    self.raw_fault_map = json.load(f)
            except Exception:
                self.raw_fault_map = {}

        if not hasattr(self, "fault_latches"):
            self.fault_latches = {}

        # Evaluate each word
        for tag, udt_name in word_keys.items():
            word_val = self.state.get(tag)
            if word_val is None:
                continue

            word_int = int(word_val)
            bits_dict = self.raw_fault_map.get(udt_name, {}).get("bits", {})

            for offset_str, meta in bits_dict.items():
                byte_idx, bit_idx = map(int, offset_str.split('.'))

                # Siemens Big-Endian: Byte 0 is the highest byte (shifted 24 bits left)
                shift_amount = (3 - byte_idx) * 8 + bit_idx
                is_active = bool((word_int >> shift_amount) & 1)

                fault_name = meta["name"]

                # Silent initialization on first cycle
                if fault_name not in self.fault_latches:
                    self.fault_latches[fault_name] = is_active
                    continue

                # Edge detection
                if is_active != self.fault_latches[fault_name]:
                    print(f"Logging {fault_name}")
                    self.events.log_fault(fault_name, active=is_active)
                    self.fault_latches[fault_name] = is_active

    def _load_fault_map(self) -> dict:
        config_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../config/fault_map.json'))
        try:
            with open(config_path, "r") as f:
                raw_json = json.load(f)

            dynamic_map = {}
            for udt_name, data in raw_json.items():
                target_tag = data.get("registry_tag")
                if target_tag:
                    dynamic_map[target_tag] = data.get("bits", {})
            self.events.log_general(f"Mapped {len(dynamic_map)} dynamic fault structures.")
            return dynamic_map
        except Exception as e:
            self.events.log_general(f"Failed to load fault map: {e}")
            return {}

    def _load_tag_registry(self) -> dict:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(os.path.dirname(current_dir))
        registry_path = os.path.join(project_root, "config", "system_tags.json")
        try:
            with open(registry_path, "r") as f:
                all_tags = json.load(f)

            plc_tags = {tag: meta for tag, meta in all_tags.items() if meta.get("source") == "service_plc_snap7"}
            self.events.log_general(f"Loaded {len(plc_tags)} tags for Snap7 monitoring.")
            return plc_tags
        except FileNotFoundError:
            self.events.log_general(f"WARNING: {registry_path} not found. Operating blind.")
            return {}

    def _calculate_db_size(self, db_num: int) -> int:
        max_byte = 0
        type_sizes = {"BOOL": 1, "INT": 2, "UINT": 2, "DINT": 4, "UDINT": 4, "REAL": 4, "DWORD": 4, "TIME": 4}

        for tag, meta in self.tags.items():
            if meta.get("db_number") == db_num:
                offset = meta.get("byte_offset", 0)
                dtype = meta.get("datatype", "UNKNOWN")
                tag_end = offset + type_sizes.get(dtype, 2)
                if tag_end > max_byte:
                    max_byte = tag_end

        if max_byte % 2 != 0:
            max_byte += 1
        return max_byte

    def _connect_plc(self):
        try:
            if self.client.get_connected():
                self.client.disconnect()
            self.client.connect(PLC_IP, PLC_RACK, PLC_SLOT)
            self.connected = True
            self.state["system.connected"] = 1.0
            self.events.log_general(f"Connected to S7-1200 at {PLC_IP}")
        except Exception as e:
            self.connected = False
            self.state["system.connected"] = 0.0
            self.state["system.cpu_state"] = "DISCONNECTED"
            self.events.log_general(f"Connection failed: {e}")

    def _toggle_watchdog(self):
        watchdog_tag = "ion_beam.system.plc_watchdog"
        if watchdog_tag not in self.tags:
            return
        self.watchdog_state = not self.watchdog_state
        self._write_tag(watchdog_tag, self.watchdog_state)

    def _parse_bytearray(self, data: bytearray, byte_idx: int, bit_idx: int, dtype: str):
        if dtype == "BOOL":
            return 1.0 if get_bool(data, byte_idx, bit_idx) else 0.0
        elif dtype == "INT":
            return get_int(data, byte_idx)
        elif dtype == "DINT":
            return get_dint(data, byte_idx)
        elif dtype == "REAL":
            return get_real(data, byte_idx)
        elif dtype == "DWORD":
            return get_dword(data, byte_idx)
        return None

    def _parse_fault_struct(self, raw_db_bytes: bytearray, byte_offset: int, tag_name: str):
        struct_bytes = raw_db_bytes[byte_offset: byte_offset + 4]
        for offset_str, fault_meta in self.fault_map[tag_name].items():
            byte_idx, bit_idx = map(int, offset_str.split('.'))
            is_active = get_bool(struct_bytes, byte_idx, bit_idx)

            # Map directly to 1D numeric array format
            fault_name = fault_meta["name"]
            self.state[f"faults.{fault_name}"] = 1.0 if is_active else 0.0

    def _read_and_parse_dbs(self):
        try:
            raw_interface = self.client.db_read(DB_INTERFACE_NUM, 0, self.db_interface_size)

            for tag, meta in self.tags.items():
                if meta.get("db_number") != DB_INTERFACE_NUM:
                    continue
                if "From_PC" in tag:
                    continue

                byte_offset = meta.get("byte_offset", 0)

                if tag in self.fault_map:
                    self._parse_fault_struct(raw_interface, byte_offset, tag)
                else:
                    val = self._parse_bytearray(
                        raw_interface,
                        byte_offset,
                        meta.get("bit_offset", 0),
                        meta.get("datatype", "UNKNOWN")
                    )
                    self.state[tag] = val

            current_count = self.state.get("ion_beam.system.plc_cycle_count")
            if current_count is not None:
                self.state["system.cpu_state"] = "RUN" if current_count != self.plc_cycle_count else "STOP"
                self.plc_cycle_count = current_count

        except Exception as e:
            self.events.log_general(f"Cycle aborted due to error: {e}")
            self.state["system.cpu_state"] = "DISCONNECTED"
            self.connected = False

    def _write_tag(self, tag: str, new_value: Any) -> bool:
        if tag not in self.tags:
            return False

        meta = self.tags[tag]
        if not meta.get("writable", True):
            return False

        byte_offset = meta.get("byte_offset", 0)
        bit_offset = meta.get("bit_offset", 0)
        dtype = meta.get("datatype", "UNKNOWN")
        db_num = meta.get("db_number")

        try:
            if dtype == "BOOL":
                data = self.client.db_read(db_num, byte_offset, 1)
                set_bool(data, 0, bit_offset, bool(new_value))
                self.client.db_write(db_num, byte_offset, data)
            elif dtype == "REAL":
                data = bytearray(4)
                set_real(data, 0, float(new_value))
                self.client.db_write(db_num, byte_offset, data)
            elif dtype == "INT":
                data = bytearray(2)
                set_int(data, 0, int(new_value))
                self.client.db_write(db_num, byte_offset, data)
            return True
        except Exception as e:
            self.events.log_general(f"Write failed for '{tag}': {e}")
            self.connected = False
            return False

    def _update_plc_mailbox(self, tag: str, new_value: bool):
        if self.mailbox_cache.get(tag) != new_value:
            success = self._write_tag(tag, new_value)
            if success:
                self.mailbox_cache[tag] = new_value

    def _process_external_telemetry(self):
        """Expects 1D Flat payloads mapped directly to registry tags."""
        try:
            while True:
                topic, msg = self.telem_socket.recv_multipart(flags=zmq.NOBLOCK)
                payload = orjson.loads(msg)
                topic_str = topic.decode('utf-8')

                for service in self.last_seen.keys():
                    if service in topic_str:
                        self.last_seen[service] = time.time()

                if "TURBO" in topic_str:
                    self._update_plc_mailbox("ion_beam.pump.status.stat_src_turbo_comms_fail",
                                             bool(payload.get("ion_beam.pump.status.stat_src_turbo_comms_fail", 0.0)))
                    self._update_plc_mailbox("ion_beam.pump.status.stat_src_turbo_error",
                                             bool(payload.get("ion_beam.pump.status.stat_src_turbo_error", 0.0)))
                    self._update_plc_mailbox("ion_beam.pump.status.stat_src_turbo_warning",
                                             bool(payload.get("ion_beam.pump.status.stat_src_turbo_warning", 0.0)))
                    self._update_plc_mailbox("ion_beam.pump.status.stat_src_turbo_trip",
                                             bool(payload.get("ion_beam.pump.status.stat_src_turbo_trip", 0.0)))

                elif "VACUUM" in topic_str:
                    self._update_plc_mailbox("ion_beam.gauges.status.stat_graphix1_comms_fail",
                                             bool(payload.get("ion_beam.gauges.status.stat_graphix1_comms_fail", 0.0)))
                    self._update_plc_mailbox("ion_beam.gauges.status.stat_graphix2_comms_fail",
                                             bool(payload.get("ion_beam.gauges.status.stat_graphix2_comms_fail", 0.0)))

                    for i in range(1, 7):
                        self._update_plc_mailbox(f"ion_beam.gauges.status.stat_vg{i}_not_found",
                                                 bool(payload.get(f"ion_beam.gauges.status.stat_vg{i}_not_found", 0.0)))
                        self._update_plc_mailbox(f"ion_beam.gauges.status.stat_vg{i}_mismatch",
                                                 bool(payload.get(f"ion_beam.gauges.status.stat_vg{i}_mismatch", 0.0)))
                        self._update_plc_mailbox(f"ion_beam.gauges.status.stat_vg{i}_above_sp",
                                                 bool(payload.get(f"ion_beam.gauges.status.stat_vg{i}_above_sp", 0.0)))
                        self._update_plc_mailbox(f"ion_beam.gauges.status.stat_vg{i}_rapid_rise", bool(
                            payload.get(f"ion_beam.gauges.status.stat_vg{i}_rapid_rise", 0.0)))

                    gv_safe = bool(payload.get("ion_beam.vacuum.gv_permissive_ready", 0.0))
                    self._update_plc_mailbox("ion_beam.source.chamber.stat_vac_ok_for_gv", gv_safe)

                elif "SPELLMAN" in topic_str:
                    for i in range(1, 6):
                        self._update_plc_mailbox(f"ion_beam.spellman.status.stat_unit{i}_comms_fail", bool(
                            payload.get(f"ion_beam.spellman.status.stat_unit{i}_comms_fail", 0.0)))
                        self._update_plc_mailbox(f"ion_beam.spellman.status.stat_unit{i}_overcurrent", bool(
                            payload.get(f"ion_beam.spellman.status.stat_unit{i}_overcurrent", 0.0)))
                        self._update_plc_mailbox(f"ion_beam.spellman.status.stat_unit{i}_undervoltage", bool(
                            payload.get(f"ion_beam.spellman.status.stat_unit{i}_undervoltage", 0.0)))
                        self._update_plc_mailbox(f"ion_beam.spellman.status.stat_unit{i}_arc_exceeded", bool(
                            payload.get(f"ion_beam.spellman.status.stat_unit{i}_arc_exceeded", 0.0)))

                elif "MAGNET" in topic_str:
                    self._update_plc_mailbox("ion_beam.magnet.status.stat_comms_fail",
                                             bool(payload.get("ion_beam.magnet.status.stat_comms_fail", 0.0)))
                    self._update_plc_mailbox("ion_beam.magnet.status.stat_open_circuit",
                                             bool(payload.get("ion_beam.magnet.status.stat_open_circuit", 0.0)))
                    self._update_plc_mailbox("ion_beam.magnet.status.stat_short_circuit",
                                             bool(payload.get("ion_beam.magnet.status.stat_short_circuit", 0.0)))
                    self._update_plc_mailbox("ion_beam.magnet.status.stat_unexpected_res",
                                             bool(payload.get("ion_beam.magnet.status.stat_unexpected_res", 0.0)))
                    self._update_plc_mailbox("ion_beam.magnet.status.stat_psu_overtemp",
                                             bool(payload.get("ion_beam.magnet.status.stat_psu_overtemp", 0.0)))
                    self._update_plc_mailbox("ion_beam.magnet.status.stat_psu_powerfail",
                                             bool(payload.get("ion_beam.magnet.status.stat_psu_powerfail", 0.0)))
                    self._update_plc_mailbox("ion_beam.magnet.status.stat_psu_ovc",
                                             bool(payload.get("ion_beam.magnet.status.stat_psu_ovc", 0.0)))
                    self._update_plc_mailbox("ion_beam.magnet.status.stat_psu_ovp",
                                             bool(payload.get("ion_beam.magnet.status.stat_psu_ovp", 0.0)))

        except zmq.Again:
            pass

    def _enforce_it_watchdogs(self):
        current_time = time.time()

        if current_time - self.last_seen["VACUUM"] > self.SERVICE_TIMEOUT_SEC:
            self._update_plc_mailbox("ion_beam.source.chamber.stat_vac_ok_for_gv", False)
            self._update_plc_mailbox("ion_beam.gauges.status.stat_graphix1_comms_fail", True)
            self._update_plc_mailbox("ion_beam.gauges.status.stat_graphix2_comms_fail", True)

        if current_time - self.last_seen["TURBO"] > self.SERVICE_TIMEOUT_SEC:
            self._update_plc_mailbox("ion_beam.pump.status.stat_src_turbo_comms_fail", True)

        if current_time - self.last_seen["SPELLMAN"] > self.SERVICE_TIMEOUT_SEC:
            for i in range(1, 6):
                self._update_plc_mailbox(f"ion_beam.spellman.status.stat_unit{i}_comms_fail", True)

    def _process_commands(self):
        try:
            while True:
                msg = self.sub_socket.recv_json(flags=zmq.NOBLOCK)
                tag = msg.get("tag")
                value = msg.get("value")
                timestamp = msg.get("ts", 0.0)

                age = time.time() - timestamp
                if age > MAX_CMD_AGE:
                    self.events.log_general(f"WARNING: Dropped stale command '{tag}' (Age: {age:.2f}s)")
                    continue

                if tag in self.tags and value is not None:
                    self._write_tag(tag, value)
        except zmq.Again:
            pass

    def run(self):
        self.events.log_general("Starting Daemon...")

        current_time_pc = time.perf_counter()
        next_tick = current_time_pc + POLL_INTERVAL

        while True:
            current_time_pc = time.perf_counter()

            if not self.connected:
                self._connect_plc()
                if not self.connected:
                    # Connection failed spin-wait
                    sleep_time = next_tick - time.perf_counter()
                    if sleep_time > 0.002:
                        time.sleep(sleep_time - 0.002)
                    while time.perf_counter() < next_tick:
                        pass
                    next_tick += POLL_INTERVAL
                    continue

            self._read_and_parse_dbs()
            self._process_commands()
            self._process_external_telemetry()
            self._enforce_it_watchdogs()
            self._evaluate_word_faults()

            current_time_unix = time.time()
            if current_time_unix - self.last_heartbeat >= HEARTBEAT_INTERVAL:
                self._toggle_watchdog()
                self.last_heartbeat = current_time_unix

            self.state["timestamp"] = current_time_unix

            try:
                topic = TOPIC_PLC_DATA if isinstance(TOPIC_PLC_DATA, bytes) else TOPIC_PLC_DATA.encode('utf-8')
                self.pub_socket.send_multipart([topic, orjson.dumps(self.state)])
            except Exception as e:
                self.events.log_general(f"Publish Error: {e}")

            if current_time_unix - self.last_hb_time >= 0.5:
                self.hb_socket.send_json({"service": "service_plc", "ts": current_time_unix})
                self.last_hb_time = current_time_unix

            # Strict 10Hz OS Scheduling (Spin-Wait)
            sleep_time = next_tick - time.perf_counter()
            if sleep_time > 0.002:
                time.sleep(sleep_time - 0.002)

            while time.perf_counter() < next_tick:
                pass

            next_tick += POLL_INTERVAL


if __name__ == "__main__":
    PlcMicroservice().run()