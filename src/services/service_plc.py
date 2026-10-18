import json
import logging
import os
import threading
import time
from typing import Any, Dict

import orjson
import snap7
import zmq
from snap7.util import get_bool, get_dint, get_dword, get_int, get_real, set_bool, set_dint, set_int, set_real

from src.core.commands import CommandServer
from src.core.event_helper import EventHelper
from src.core.log_setup import Throttle, setup_logging
from src.core.os_helper import harden_windows_process
from src.core.paths import REGISTRY_PATH, simulation_enabled

# Force Windows high-resolution timers (1ms precision) for OS sleep accuracy
if os.name == 'nt':
    import ctypes

    ctypes.windll.winmm.timeBeginPeriod(1)

from src.core.network_map import (
    ZMQ_PORT_PLC_PUB, ZMQ_PORT_PLC_CMD, TOPIC_PLC_DATA, PLC_IP, PLC_RACK, PLC_SLOT, DB_INTERFACE_NUM,
    ZMQ_PORT_VACUUM_PUB, ZMQ_PORT_SPELLMAN_PUB, ZMQ_PORT_MAGNET_PUB, ZMQ_PORT_SRC_TURBO_PUB,
    ZMQ_PORT_HEARTBEAT)

POLL_INTERVAL = 0.1  # 100ms cycle (10Hz)
HEARTBEAT_INTERVAL = 0.5  # 2Hz Watchdog
MAX_CMD_AGE = 0.5  # TTL: Reject incoming commands older than 500ms
RECONNECT_DELAYS = (1.0, 2.0, 5.0, 10.0)  # seconds between PLC connection attempts (backs off, then stays at 10 s)

# Only these kinds of tag may be written by a command. Everything else in the
# PC->PLC area (stat_* permissives such as stat_vac_ok_for_gv, the watchdog)
# is computed by this service and must not be settable from outside.
COMMANDABLE_PREFIXES = ("cmd_", "sp_requested_", "testing_cmd_")
# ctrl_mode values that are NOT an automatic lockout: the cesium oven's mode 1 is just
# "heater on" (the HMI lets the operator adjust the setpoint then; this service used to
# refuse it as "automatic control mode 1").
NON_LOCKING_MODES = {"ion_beam.source.cesium": {1}}

log = logging.getLogger("service_plc")


def _new_client():
    if simulation_enabled():
        from src.sim.plc import FakeSnap7Client
        return FakeSnap7Client()
    return snap7.client.Client()


class PlcMicroservice:
    def __init__(self):
        self.context = zmq.Context()
        self.events = EventHelper("service_plc")

        # --- ZMQ Setup (Defense in Depth: Transport Layer) ---
        self.pub_socket = self.context.socket(zmq.PUB)
        self.pub_socket.setsockopt(zmq.SNDHWM, 5)
        self.pub_socket.bind(ZMQ_PORT_PLC_PUB)

        # Commands arrive on a ROUTER socket and every one gets an ok/rejected/failed reply
        self.commands = CommandServer(ZMQ_PORT_PLC_CMD, "service_plc", context=self.context,
                                      events=self.events, max_age=MAX_CMD_AGE)

        # --- ZMQ Telemetry Subscriber (Listening to other Python services) ---
        self.telem_socket = self.context.socket(zmq.SUB)
        self.telem_socket.setsockopt(zmq.RCVHWM, 10)
        self.telem_socket.setsockopt_string(zmq.SUBSCRIBE, "")

        self.telem_socket.connect(ZMQ_PORT_VACUUM_PUB)
        self.telem_socket.connect(ZMQ_PORT_SPELLMAN_PUB)
        self.telem_socket.connect(ZMQ_PORT_MAGNET_PUB)
        self.telem_socket.connect(ZMQ_PORT_SRC_TURBO_PUB)

        # --- Mailbox Cache (Prevents Snap7 write-flooding) ---
        self.mailbox_cache: Dict[str, Any] = {}

        # --- Snap7 Setup ---
        self.client = None
        self.connected = False
        self.watchdog_state = False
        self.last_heartbeat = time.time()
        self.plc_cycle_count = 0
        self.heartbeat_count = 0
        self._connect_thread = None
        self._connect_result = None
        self._next_connect_at = 0.0
        self._connect_failures = 0
        self._throttle = Throttle(60.0)
        self._refused_batched = set()

        # --- Tag Registry & Configuration ---
        self.tags = self._load_tag_registry()
        self.registry_limits = self._load_registry_limits()
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
        self.SERVICE_TIMEOUT_SEC = 2.0

        self.hb_socket = self.context.socket(zmq.PUB)
        self.hb_socket.connect(ZMQ_PORT_HEARTBEAT)
        self.last_hb_time = 0.0
        self.running = True

    def stop(self):
        self.running = False

    def _evaluate_word_faults(self):
        """Unpacks Siemens 32-bit DWORDs and logs edge transitions via EventHelper."""
        word_keys = {
            "ion_beam.faults.word_0_system": "UDT_Fault_Word_0_System",
            "ion_beam.faults.word_1_pumps": "UDT_Fault_Word_1_Pumps",
            "ion_beam.faults.word_2_source": "UDT_Fault_Word_2_Source",
            "ion_beam.faults.word_3_spellman": "UDT_Fault_Word_3_Spellman",
            "ion_beam.faults.word_4_magnet": "UDT_Fault_Word_4_Magnet",
            "ion_beam.faults.word_5_gauges": "UDT_Fault_Word_5_Gauges"
        }

        if not hasattr(self, "raw_fault_map"):
            config_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../config/fault_config.json'))
            try:
                with open(config_path, "r") as f:
                    self.raw_fault_map = json.load(f)
            except (OSError, ValueError) as e:
                log.error("Cannot load %s - PLC fault edges will not be logged: %s", config_path, e)
                self.raw_fault_map = {}

        if not hasattr(self, "fault_latches"):
            self.fault_latches = {}

        for tag, udt_name in word_keys.items():
            word_val = self.state.get(tag)
            if word_val is None:
                continue

            word_int = int(word_val)
            bits_dict = self.raw_fault_map.get(udt_name, {}).get("bits", {})

            for offset_str, meta in bits_dict.items():
                byte_idx, bit_idx = map(int, offset_str.split('.'))
                shift_amount = (3 - byte_idx) * 8 + bit_idx
                is_active = bool((word_int >> shift_amount) & 1)

                fault_name = meta["name"]
                if fault_name not in self.fault_latches:
                    self.fault_latches[fault_name] = is_active
                    continue

                if is_active != self.fault_latches[fault_name]:
                    self.events.log_fault(fault_name, active=is_active)
                    self.fault_latches[fault_name] = is_active

    def _load_fault_map(self) -> dict:
        config_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../config/fault_config.json'))
        try:
            with open(config_path, "r") as f:
                raw_json = json.load(f)

            dynamic_map = {}
            for udt_name, data in raw_json.items():
                target_tag = data.get("registry_tag")
                if target_tag:
                    dynamic_map[target_tag] = data.get("bits", {})
            log.info("Mapped %d dynamic fault structures.", len(dynamic_map))
            return dynamic_map
        except (OSError, ValueError) as e:
            self.events.log_general(f"Failed to load fault map: {e}", severity="ERROR")
            return {}

    def _load_tag_registry(self) -> dict:
        registry_path = REGISTRY_PATH
        try:
            with open(registry_path, "r") as f:
                all_tags = json.load(f)

            plc_tags = {tag: meta for tag, meta in all_tags.items() if meta.get("source") == "service_plc_snap7"}
            log.info("Loaded %d tags for Snap7 monitoring.", len(plc_tags))
            return plc_tags
        except FileNotFoundError:
            self.events.log_general(f"WARNING: {registry_path} not found. Operating blind.", severity="ERROR")
            return {}

    def _load_registry_limits(self) -> dict:
        registry_path = REGISTRY_PATH
        limits = {}
        try:
            with open(registry_path, "r") as f:
                all_tags = json.load(f)
                for tag, meta in all_tags.items():
                    if meta.get("source") == "service_plc_snap7" and meta.get("auto_controllable"):
                        limits[tag] = {
                            "min_val": meta.get("min_val"),
                            "max_val": meta.get("max_val")
                        }
        except (OSError, ValueError) as e:
            self.events.log_general(f"WARNING: Failed to load registry limits: {e}", severity="ERROR")
        return limits

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

    # ------------------------------------------------------------------ connection
    def _poll_connection(self):
        """Non-blocking connection management. A connection attempt to an unreachable
        PLC can block for several seconds, so it runs in a helper thread while this
        loop keeps publishing 'disconnected' and sending heartbeats - otherwise the
        service manager sees a hang and restarts the service over and over."""
        if self._connect_thread is not None:
            if self._connect_thread.is_alive():
                return
            self._connect_thread = None
            client, err = self._connect_result
            if client is not None:
                self.client = client
                self.connected = True
                self._connect_failures = 0
                self._throttle.reset("connect")
                self.state["system.connected"] = 1.0
                self.events.log_general(f"Connected to S7-1200 at {PLC_IP}")
            else:
                self._connect_failures += 1
                delay = RECONNECT_DELAYS[min(self._connect_failures - 1, len(RECONNECT_DELAYS) - 1)]
                self._next_connect_at = time.time() + delay
                n = self._throttle.ready("connect")
                if n is not None:
                    self.events.log_general(f"PLC connection to {PLC_IP} failed: {err}{Throttle.suffix(n)}",
                                            severity="WARNING")
            return

        if time.time() >= self._next_connect_at:
            def attempt():
                client = _new_client()
                try:
                    client.connect(PLC_IP, PLC_RACK, PLC_SLOT)
                    self._connect_result = (client, None)
                except Exception as e:  # snap7 raises several exception types
                    try:
                        client.disconnect()
                    except Exception:
                        pass
                    self._connect_result = (None, e)

            self._connect_result = None
            self._connect_thread = threading.Thread(target=attempt, name="plc-connect", daemon=True)
            self._connect_thread.start()

    def _mark_disconnected(self, reason: str):
        if self.connected:
            self.events.log_general(f"PLC connection lost: {reason}", severity="WARNING")
        self.connected = False
        self.mailbox_cache.clear()
        if self.client is not None:
            try:
                self.client.disconnect()
            except Exception as e:
                log.debug("disconnect after error: %s", e)
        self.client = None
        self._next_connect_at = time.time() + RECONNECT_DELAYS[0]
        self.state["system.connected"] = 0.0
        self.state["system.cpu_state"] = "DISCONNECTED"
        # Nothing read from the PLC is known any more: publish 'unknown' (null) rather
        # than the last values, so the HMI shows '---' and the logger records gaps.
        for tag in self.tags:
            if tag in self.state:
                self.state[tag] = None
        for key in [k for k in self.state if k.startswith("faults.")]:
            self.state[key] = None

    def _increment_watchdog(self):
        watchdog_tag = "ion_beam.system.plc_watchdog"
        if watchdog_tag not in self.tags:
            return
        self.heartbeat_count += 1
        if self.heartbeat_count > 1000:
            self.heartbeat_count = 0
        self._write_tag(watchdog_tag, self.heartbeat_count)

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
        raw_word_int = get_dword(raw_db_bytes, byte_offset)
        self.state[tag_name] = float(raw_word_int)

        struct_bytes = raw_db_bytes[byte_offset: byte_offset + 4]
        for offset_str, fault_meta in self.fault_map[tag_name].items():
            byte_idx, bit_idx = map(int, offset_str.split('.'))
            is_active = get_bool(struct_bytes, byte_idx, bit_idx)
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

        except Exception as e:  # snap7 raises RuntimeError / S7Error / OSError subclasses
            self._mark_disconnected(f"read failed: {e}")

    def _write_tag(self, tag: str, new_value: Any):
        """Write one tag. Returns (ok, reason)."""
        if tag not in self.tags:
            return False, "unknown PLC tag"

        meta = self.tags[tag]
        if not meta.get("writable", True):
            return False, "tag is read-only"
        if not self.connected or self.client is None:
            return False, "PLC not connected"

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
            elif dtype in ("DINT", "UDINT"):
                data = bytearray(4)
                set_dint(data, 0, int(new_value))
                self.client.db_write(db_num, byte_offset, data)
            else:
                return False, f"datatype {dtype} not supported for writes"
            return True, ""
        except (TypeError, ValueError) as e:
            return False, f"bad value for {dtype}: {e}"
        except Exception as e:
            self._mark_disconnected(f"write of {tag} failed: {e}")
            return False, f"PLC write failed: {e}"

    def _write_tags_batched(self, updates: Dict[str, Any]):
        if not self.connected or self.client is None:
            return
        type_sizes = {
            "BOOL": 1, "BYTE": 1, "CHAR": 1,
            "INT": 2, "UINT": 2, "WORD": 2,
            "DINT": 4, "UDINT": 4, "DWORD": 4, "REAL": 4
        }

        changes_by_db = {}
        for tag, new_value in updates.items():
            meta = self.tags.get(tag)
            if meta is None:
                continue
            # Same rule as single writes: never touch tags the registry marks read-only
            # (a read-modify-write of a PLC-owned byte could race the PLC program).
            if not meta.get("writable", True):
                if tag not in self._refused_batched:
                    self._refused_batched.add(tag)
                    log.error("Refusing batched write to read-only tag %s (check system_tags.json)", tag)
                continue
            if self.mailbox_cache.get(tag) != new_value:
                db_num = meta.get("db_number")
                if db_num not in changes_by_db:
                    changes_by_db[db_num] = []
                changes_by_db[db_num].append((tag, meta, new_value))

        for db_num, tag_list in changes_by_db.items():
            try:
                min_byte = min(item[1].get("byte_offset", 0) for item in tag_list)
                max_end_byte = max(
                    item[1].get("byte_offset", 0) + type_sizes.get(item[1].get("datatype", "BOOL"), 1)
                    for item in tag_list
                )

                length = max_end_byte - min_byte
                if length % 2 != 0:
                    length += 1

                db_data = self.client.db_read(db_num, min_byte, length)

                for tag, meta, value in tag_list:
                    rel_byte = meta.get("byte_offset", 0) - min_byte
                    dtype = meta.get("datatype", "BOOL")

                    if dtype == "BOOL":
                        set_bool(db_data, rel_byte, meta.get("bit_offset", 0), bool(value))
                    elif dtype == "REAL":
                        set_real(db_data, rel_byte, float(value))
                    elif dtype in ["INT", "UINT"]:
                        set_int(db_data, rel_byte, int(value))
                    elif dtype in ["DINT", "UDINT"]:
                        set_dint(db_data, rel_byte, int(value))

                self.client.db_write(db_num, min_byte, db_data)

                for tag, _, value in tag_list:
                    self.mailbox_cache[tag] = value
            except Exception as e:
                self._mark_disconnected(f"batched write on DB {db_num} failed: {e}")
                return

    def _process_external_telemetry(self):
        """Processes and stages flat payloads before batching to avoid write floods."""
        pending_updates = {}
        try:
            while True:
                topic, msg = self.telem_socket.recv_multipart(flags=zmq.NOBLOCK)
                try:
                    payload = orjson.loads(msg)
                except ValueError:
                    log.warning("Unreadable telemetry on %r dropped", topic)
                    continue
                topic_str = topic.decode('utf-8').upper()

                for service in self.last_seen.keys():
                    if service in topic_str:
                        self.last_seen[service] = time.time()

                # A null (unknown) flag from another service counts as a fault for the PLC
                def flag(key, unknown=True):
                    v = payload.get(key, 0.0)
                    return unknown if v is None else bool(v)

                if "TURBO" in topic_str:
                    base = "ion_beam.source.turbo_pump"
                    pending_updates[f"{base}.stat_comms_fail"] = flag(f"{base}.stat_comms_fail")
                    pending_updates[f"{base}.stat_error"] = flag(f"{base}.stat_error", False)
                    pending_updates[f"{base}.stat_warning"] = flag(f"{base}.stat_warning", False)
                    pending_updates[f"{base}.stat_trip"] = flag(f"{base}.stat_trip", False)

                elif "VACUUM" in topic_str:
                    pending_updates["ion_beam.facilities.graphix1.stat_comms_fail"] = flag(
                        "ion_beam.facilities.graphix1.stat_comms_fail")
                    pending_updates["ion_beam.facilities.graphix2.stat_comms_fail"] = flag(
                        "ion_beam.facilities.graphix2.stat_comms_fail")
                    pending_updates["ion_beam.source.chamber.stat_vac_ok_for_gv"] = flag(
                        "ion_beam.vacuum.gv_permissive_ready", False)

                    vg_paths = [
                        "ion_beam.source.vacuum_gauge_1",
                        "ion_beam.beamline.vacuum_gauge_2",
                        "ion_beam.beamline.vacuum_gauge_3",
                        "ion_beam.endstation.vacuum_gauge_4",
                        "ion_beam.loadlock.vacuum_gauge_5",
                        "ion_beam.endstation.vacuum_gauge_6"
                    ]
                    for path in vg_paths:
                        pending_updates[f"{path}.stat_not_found"] = flag(f"{path}.stat_not_found", False)
                        pending_updates[f"{path}.stat_mismatch"] = flag(f"{path}.stat_mismatch", False)
                        pending_updates[f"{path}.stat_above_sp"] = flag(f"{path}.stat_above_sp", False)
                        pending_updates[f"{path}.stat_rapid_rise"] = flag(f"{path}.stat_rapid_rise", False)

                elif "SPELLMAN" in topic_str:
                    spellman_paths = [
                        "ion_beam.source.einzel",
                        "ion_beam.beamline.einzel",
                        "ion_beam.beamline.neutral_trap_pos",
                        "ion_beam.beamline.neutral_trap_neg"
                    ]
                    for path in spellman_paths:
                        pending_updates[f"{path}.stat_comms_fail"] = flag(f"{path}.stat_comms_fail")
                        pending_updates[f"{path}.stat_overcurrent"] = flag(f"{path}.stat_overcurrent", False)
                        pending_updates[f"{path}.stat_overvoltage"] = flag(f"{path}.stat_overvoltage", False)
                        pending_updates[f"{path}.stat_fail"] = flag(f"{path}.stat_fail", False)
                        pending_updates[f"{path}.stat_arc_exceeded"] = flag(f"{path}.stat_arc_exceeded", False)

                elif "MAGNET" in topic_str:
                    base = "ion_beam.beamline.magnet"
                    pending_updates[f"{base}.stat_comms_fail"] = flag(f"{base}.stat_comms_fail")
                    pending_updates[f"{base}.stat_open_circuit"] = flag(f"{base}.stat_open_circuit", False)
                    pending_updates[f"{base}.stat_short_circuit"] = flag(f"{base}.stat_short_circuit", False)
                    pending_updates[f"{base}.stat_unexpected_res"] = flag(f"{base}.stat_unexpected_res", False)
                    pending_updates[f"{base}.stat_psu_overtemp"] = flag(f"{base}.stat_psu_overtemp", False)
                    pending_updates[f"{base}.stat_psu_powerfail"] = flag(f"{base}.stat_psu_powerfail", False)
                    pending_updates[f"{base}.stat_psu_ovc"] = flag(f"{base}.stat_psu_ovc", False)
                    pending_updates[f"{base}.stat_psu_ovp"] = flag(f"{base}.stat_psu_ovp", False)

        except zmq.Again:
            if pending_updates:
                self._write_tags_batched(pending_updates)

    def _enforce_it_watchdogs(self):
        current_time = time.time()
        fallback_updates = {}

        if current_time - self.last_seen["VACUUM"] > self.SERVICE_TIMEOUT_SEC:
            fallback_updates["ion_beam.source.chamber.stat_vac_ok_for_gv"] = False
            fallback_updates["ion_beam.facilities.graphix1.stat_comms_fail"] = True
            fallback_updates["ion_beam.facilities.graphix2.stat_comms_fail"] = True

        if current_time - self.last_seen["TURBO"] > self.SERVICE_TIMEOUT_SEC:
            fallback_updates["ion_beam.source.turbo_pump.stat_comms_fail"] = True

        if current_time - self.last_seen["SPELLMAN"] > self.SERVICE_TIMEOUT_SEC:
            spellman_paths = [
                "ion_beam.source.einzel",
                "ion_beam.beamline.einzel",
                "ion_beam.beamline.neutral_trap_pos",
                "ion_beam.beamline.neutral_trap_neg"
            ]
            for path in spellman_paths:
                fallback_updates[f"{path}.stat_comms_fail"] = True

        if current_time - self.last_seen["MAGNET"] > self.SERVICE_TIMEOUT_SEC:
            fallback_updates["ion_beam.beamline.magnet.stat_comms_fail"] = True

        if fallback_updates:
            self._write_tags_batched(fallback_updates)

    def _process_commands(self):
        for cmd in self.commands.poll():
            tag, value, origin = cmd.tag, cmd.value, cmd.origin

            if tag not in self.tags:
                cmd.reject("unknown PLC tag (not in system_tags.json)")
                continue
            if value is None:
                cmd.reject("no value given")
                continue
            if not tag.split('.')[-1].startswith(COMMANDABLE_PREFIXES):
                cmd.reject("not a command or setpoint tag - status/permissive tags are set by the PC services only")
                continue
            if not self.tags[tag].get("writable", True):
                cmd.reject("tag is read-only")
                continue
            if not self.connected:
                cmd.reject("PLC not connected")
                continue

            # --- 1. ARBITRATION: Software Gatekeeper ---
            # Infer the subsystem. e.g., ion_beam.source.extraction.sp_requested_voltage -> ion_beam.source.extraction
            parts = tag.split('.')
            if len(parts) >= 3:
                subsystem_base = ".".join(parts[:-1])
                rb_mode_tag = f"{subsystem_base}.ctrl_mode"

                # Check if the subsystem has an active mode, and ensure we aren't trying to change the mode itself
                # Switching something OFF is never locked out (an operator must always be able to)
                switching_off = tag.endswith(".cmd_enable") and not value
                if rb_mode_tag in self.tags and not tag.endswith("cmd_ctrl_mode") and not switching_off:
                    active_mode = self.state.get(rb_mode_tag) or 0.0
                    if int(active_mode) in NON_LOCKING_MODES.get(subsystem_base, ()):
                        active_mode = 0.0
                    if active_mode > 0 and origin != "optimizer":
                        cmd.reject(f"locked out: {subsystem_base} is in automatic control mode {int(active_mode)}")
                        continue

            # --- 2. FAIL-SAFE LIMITS: Software Gatekeeper ---
            # Check PC-side limits to prevent Snap7 network flooding, even though the PLC hardware provides ultimate protection.
            meta = self.registry_limits.get(tag)
            if meta is not None:
                min_v = meta["min_val"]
                max_v = meta["max_val"]

                # Drop the command entirely if the bounds have not been configured in the JSON yet
                if min_v is None or max_v is None:
                    cmd.reject("no min/max limits configured for this tag in system_tags.json")
                    continue

                try:
                    float_val = float(value)
                except (ValueError, TypeError):
                    cmd.reject(f"value {value!r} is not a number")
                    continue
                if not (min_v <= float_val <= max_v):
                    cmd.reject(f"{float_val:g} is outside the allowed range [{min_v:g}, {max_v:g}]")
                    continue

            # --- 3. Execute Write ---
            ok, reason = self._write_tag(tag, value)
            if ok:
                cmd.ok("written to PLC")
            else:
                cmd.fail(reason)

    def _publish(self, current_time_unix):
        self.state["timestamp"] = current_time_unix
        try:
            self.pub_socket.send_multipart([TOPIC_PLC_DATA, orjson.dumps(self.state)])
        except zmq.ZMQError as e:
            if (n := self._throttle.ready("publish")) is not None:
                log.error("Publish error: %s%s", e, Throttle.suffix(n))

        if current_time_unix - self.last_hb_time >= 0.5:
            self.hb_socket.send_json({"service": "service_plc", "ts": current_time_unix})
            self.last_hb_time = current_time_unix

    def run(self):
        self.events.log_general("Starting Daemon..." + (" (SIMULATED PLC)" if simulation_enabled() else ""))
        current_time_pc = time.perf_counter()
        next_tick = current_time_pc + POLL_INTERVAL

        while self.running:
            if not self.connected:
                self._poll_connection()

            if self.connected:
                self._read_and_parse_dbs()
            self._process_commands()          # always answered, even while disconnected
            if self.connected:
                self._process_external_telemetry()
                self._enforce_it_watchdogs()
                self._evaluate_word_faults()
            else:
                self._drain_external_telemetry()

            current_time_unix = time.time()
            if self.connected and current_time_unix - self.last_heartbeat >= HEARTBEAT_INTERVAL:
                self._increment_watchdog()
                self.last_heartbeat = current_time_unix

            self._publish(current_time_unix)

            sleep_time = next_tick - time.perf_counter()
            if sleep_time > 0.002:
                time.sleep(sleep_time - 0.002)

            while time.perf_counter() < next_tick:
                pass
            next_tick += POLL_INTERVAL
            # If a cycle overran badly (e.g. PLC timeouts), don't try to catch up with a burst
            if next_tick < time.perf_counter() - 1.0:
                next_tick = time.perf_counter() + POLL_INTERVAL

        self.commands.close()
        for sock in (self.pub_socket, self.telem_socket, self.hb_socket):
            sock.close(0)

    def _drain_external_telemetry(self):
        """While the PLC is unreachable, keep the subscription drained and the IT watchdogs current."""
        try:
            while True:
                topic, _ = self.telem_socket.recv_multipart(flags=zmq.NOBLOCK)
                topic_str = topic.decode('utf-8', 'replace').upper()
                for service in self.last_seen:
                    if service in topic_str:
                        self.last_seen[service] = time.time()
        except zmq.Again:
            pass


if __name__ == "__main__":
    setup_logging("service_plc")
    harden_windows_process()
    PlcMicroservice().run()
