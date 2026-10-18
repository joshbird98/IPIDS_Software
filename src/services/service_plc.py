"""PLC service: the S7-1200 interface data block (DB_PC_Interface) read every 100 ms with
Snap7 and published as flat tags; commands written back; the PC->PLC watchdog; the other
services' status flags (comms fail, gauge set points, Spellman faults ...) copied into the
PLC's From_PC area; PLC fault-word edges logged as FAULT events.

Commands: only cmd_* / sp_requested_* tags, within the registry limits; refused while the
subsystem is under PLC control (ctrl_mode > 0, except the cesium oven's "heater on"), while
the safety relay is off (enables of relay-powered supplies), and - mirroring the PLC - switching
off the target or ioniser while the cesium is hot. Control leases as for every device service
(src/core/leases.py); the system controls (relay, shutdown, fault reset) and the gate valve can
never be leased.
"""
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

from src.core.device_service import DeviceService
from src.core.log_setup import Throttle, setup_logging
from src.core.os_helper import harden_windows_process
from src.core.paths import REGISTRY_PATH, simulation_enabled
from src.core.network_map import (
    ZMQ_PORT_PLC_PUB, ZMQ_PORT_PLC_CMD, TOPIC_PLC_DATA, PLC_IP, PLC_RACK, PLC_SLOT, DB_INTERFACE_NUM,
    ZMQ_PORT_VACUUM_PUB, ZMQ_PORT_SPELLMAN_PUB, ZMQ_PORT_MAGNET_PUB, ZMQ_PORT_SRC_TURBO_PUB)

POLL_INTERVAL = 0.1  # 100ms cycle (10Hz)
HEARTBEAT_INTERVAL = 0.5  # PC->PLC watchdog increment
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
MODE_NAMES = {1: "PLC Auto", 2: "Recipe", 3: "Cascaded (PID slave)", 4: "Arc quench"}
# The source deck (cesium oven, filament, thermionic/ioniser) is powered through the safety relay:
# with the relay off the PLC has no readback from it (it tracks the cesium temperature with its
# thermal model instead, estimated_temp), so those readbacks are published as unknown.
SOURCE_DECK = ("ion_beam.source.cesium", "ion_beam.source.filament", "ion_beam.source.thermionic")
# While the cesium is hot (internal.cs_is_hot) the PLC keeps these on so cesium vapour stays
# ionised and directed rather than coating the source's isolator ceramics. Switching them off
# is refused here too (with that reason), and so is a target voltage below target_min_voltage.
HELD_WHILE_CS_HOT = ("ion_beam.source.target", "ion_beam.source.thermionic", "ion_beam.source.filament")
# Powered through the safety relay: can't be switched on without it
RELAY_SUBSYSTEMS = ("ion_beam.source.extraction", "ion_beam.source.target", "ion_beam.source.filament",
                    "ion_beam.source.thermionic", "ion_beam.source.cesium", "ion_beam.beamline.steering")
# Taken over by the PLC's source shutdown sequence
SHUTDOWN_SUBSYSTEMS = ("ion_beam.source.extraction", "ion_beam.source.target", "ion_beam.source.filament",
                       "ion_beam.source.thermionic", "ion_beam.source.cesium")
# Operator controls that must always stay available: never leased
UNLEASABLE = ("ion_beam.system", "ion_beam.source.chamber")

log = logging.getLogger("service_plc")


def _new_client():
    if simulation_enabled():
        from src.sim.plc import FakeSnap7Client
        return FakeSnap7Client()
    return snap7.client.Client()


def load_plc_tags():
    """(PLC tags from the registry, error or None)."""
    try:
        with open(REGISTRY_PATH, "r") as f:
            all_tags = json.load(f)
    except (OSError, ValueError) as e:
        return {}, e
    return {t: m for t, m in all_tags.items() if m.get("source") == "service_plc_snap7"}, None


def subsystems_of(tags) -> tuple:
    """Every prefix that has a command or setpoint tag under it."""
    subs = {t.rpartition(".")[0] for t in tags if t.rpartition(".")[2].startswith(COMMANDABLE_PREFIXES)}
    return tuple(sorted(subs))


class PlcMicroservice(DeviceService):
    NAME = "service_plc"
    DEVICE_LABEL = "PLC"
    PUB_ENDPOINT, CMD_ENDPOINT, TOPIC = ZMQ_PORT_PLC_PUB, ZMQ_PORT_PLC_CMD, TOPIC_PLC_DATA
    POLL_INTERVAL = POLL_INTERVAL
    MAX_CMD_AGE = MAX_CMD_AGE
    RECONNECT_DELAYS = RECONNECT_DELAYS
    UNLEASABLE = UNLEASABLE
    RETIRE_CTRL_MODE = False         # PLC control modes are real (the PLC program's)
    REGISTRY_SOURCE = "service_plc_snap7"

    def __init__(self):
        self.tags, registry_error = load_plc_tags()
        self.SUBSYSTEMS = subsystems_of(self.tags)
        super().__init__()
        self.address = f"{PLC_IP} (rack {PLC_RACK}, slot {PLC_SLOT})"
        if registry_error is not None:
            self.events.log_general(f"WARNING: {REGISTRY_PATH} not loaded ({registry_error}). Operating blind.",
                                    severity="ERROR")
        else:
            log.info("Loaded %d tags for Snap7 monitoring.", len(self.tags))

        # Telemetry of the other services, whose status flags go to the PLC
        self.telem_socket = self.context.socket(zmq.SUB)
        self.telem_socket.setsockopt(zmq.RCVHWM, 10)
        self.telem_socket.setsockopt(zmq.LINGER, 0)
        self.telem_socket.setsockopt_string(zmq.SUBSCRIBE, "")
        for endpoint in (ZMQ_PORT_VACUUM_PUB, ZMQ_PORT_SPELLMAN_PUB, ZMQ_PORT_MAGNET_PUB, ZMQ_PORT_SRC_TURBO_PUB):
            self.telem_socket.connect(endpoint)

        self.mailbox_cache: Dict[str, Any] = {}     # last batched values written (no Snap7 write floods)
        self.client = None
        self.connected = False
        self.last_heartbeat = time.time()
        self.plc_cycle_count = 0
        self.heartbeat_count = 0
        self._connect_thread = None
        self._connect_result = None
        self._refused_batched = set()

        self.db_interface_size = self._calculate_db_size(DB_INTERFACE_NUM)
        self.fault_map = self._load_fault_map()
        self.deck_readbacks = [t for t in self.tags if t.rpartition(".")[0] in SOURCE_DECK
                               and (t.rpartition(".")[2].startswith("rb_") or t.endswith(".resistance"))]
        self.state.update({"system.connected": 0.0, "system.cpu_state": "UNKNOWN",
                           "system.cycle_time_ms": POLL_INTERVAL * 1000})
        # Other services' last telemetry (their flags fall back to 'fault' if they go quiet)
        self.last_seen = {"VACUUM": time.time(), "TURBO": time.time(), "SPELLMAN": time.time(),
                          "MAGNET": time.time()}
        self.SERVICE_TIMEOUT_SEC = 2.0

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
    def is_connected(self) -> bool:
        return self.connected

    def connect_device(self) -> bool:     # not used: _try_connect below connects in a helper thread
        return False

    def _try_connect(self):
        """Non-blocking connection management. A connection attempt to an unreachable PLC can block
        for several seconds, so it runs in a helper thread while this loop keeps publishing
        'disconnected' and heartbeating - otherwise the manager sees a hang and restarts us."""
        if self._connect_thread is not None:
            if self._connect_thread.is_alive():
                return
            self._connect_thread = None
            client, err = self._connect_result
            if client is not None:
                self.client = client
                self.connected = True
                self.backoff.succeeded()
                self.throttle.reset("connect")
                self.state["system.connected"] = 1.0
                self.events.log_general(f"Connected to S7-1200 at {PLC_IP}")
            else:
                self.backoff.failed(time.time())
                if (n := self.throttle.ready("connect")) is not None:
                    self.events.log_general(f"PLC connection to {PLC_IP} failed: {err}{Throttle.suffix(n)}",
                                            severity="WARNING")
            return
        if not self.backoff.due(time.time()):
            return

        def attempt():
            client = _new_client()
            try:
                client.connect(PLC_IP, PLC_RACK, PLC_SLOT)
                self._connect_result = (client, None)
            except Exception as e:  # snap7 raises several unrelated exception types
                try:
                    client.disconnect()
                except Exception:   # the client is being thrown away either way
                    pass
                self._connect_result = (None, e)

        self._connect_result = None
        self._connect_thread = threading.Thread(target=attempt, name="plc-connect", daemon=True)
        self._connect_thread.start()

    def close_device(self):
        if self.client is not None:
            try:
                self.client.disconnect()
            except Exception as e:  # snap7 on a dead link: nothing more to do
                log.debug("disconnect on shutdown: %s", e)
        self.telem_socket.close(0)

    def _mark_disconnected(self, reason: str):
        if self.connected:
            self.events.log_general(f"PLC connection lost: {reason}", severity="WARNING")
        self.connected = False
        self.mailbox_cache.clear()
        if self.client is not None:
            try:
                self.client.disconnect()
            except Exception as e:  # snap7 may raise anything on a dead link; we are dropping it anyway
                log.debug("disconnect after error: %s", e)
        self.client = None
        self.backoff.next_at = time.time() + self.RECONNECT_DELAYS[0]
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

            if self.state.get("ion_beam.facilities.safety_relay_active") == 0.0:
                for tag in self.deck_readbacks:     # source deck unpowered: nothing to read back
                    self.state[tag] = None

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

    # ------------------------------------------------------------------ loop hooks
    def cycle(self):
        # Read first, so the commands of this cycle are judged on this cycle's PLC state
        if self.connected:
            self._read_and_parse_dbs()

    def poll_device(self):
        self._process_external_telemetry()
        self._enforce_it_watchdogs()
        self._evaluate_word_faults()
        now = time.time()
        if self.connected and now - self.last_heartbeat >= HEARTBEAT_INTERVAL:
            self._increment_watchdog()
            self.last_heartbeat = now

    def publish_unknown(self):
        self._drain_external_telemetry()

    # ------------------------------------------------------------------ permits / commands
    def permit(self, subsystem: str):
        if not self.connected:
            return False, "PLC not connected"
        s = self.state
        if s.get("ion_beam.system.pc_plc_comms_lost") == 1.0:
            return False, "the PLC reports lost communication with the PC (watchdog)"
        if subsystem in UNLEASABLE:
            return True, ""
        if subsystem in RELAY_SUBSYSTEMS and s.get("ion_beam.facilities.safety_relay_active") != 1.0:
            return False, "safety relay de-energised"
        if subsystem in SOURCE_DECK and (s.get("ion_beam.system.remote_io_lost") == 1.0
                                         or s.get("faults.Remote_IO_Lost") == 1.0):
            return False, "remote I/O to the source deck lost"
        steps = [s.get("ion_beam.system.remote_shutdown_step") or 0, s.get("ion_beam.system.pc_shutdown_step") or 0]
        if subsystem in SHUTDOWN_SUBSYSTEMS and (s.get("ion_beam.system.src_auto_sdown") == 1.0 or max(steps) > 0):
            return False, "automated source shutdown in progress"
        mode = self._locking_mode(subsystem)
        if mode:
            return False, f"{MODE_NAMES.get(mode, f'mode {mode}')}: the PLC is controlling this (ctrl_mode {mode})"
        if s.get(f"{subsystem}.stat_lockout") == 1.0:
            return False, "PLC interlock (lockout)"
        return True, ""

    def _locking_mode(self, subsystem) -> int:
        mode = self.state.get(f"{subsystem}.ctrl_mode")
        if not mode or f"{subsystem}.ctrl_mode" not in self.tags:
            return 0
        mode = int(mode)
        return 0 if mode in NON_LOCKING_MODES.get(subsystem, ()) else mode

    def reset_faults(self, cmd):
        self.handle_command(cmd, "ion_beam.system", "cmd_fault_reset")

    def handle_command(self, cmd, subsystem: str, leaf: str):
        tag, value = cmd.tag, cmd.value
        if tag not in self.tags:
            cmd.reject("unknown PLC tag (not in system_tags.json)")
            return
        if value is None:
            cmd.reject("no value given")
            return
        if not leaf.startswith(COMMANDABLE_PREFIXES):
            cmd.reject("not a command or setpoint tag - status/permissive tags are set by the PC services only")
            return
        if not self.tags[tag].get("writable", True):
            cmd.reject("tag is read-only")
            return
        if not self.connected:
            cmd.reject("PLC not connected")
            return
        # Under PLC control (ctrl_mode > 0): the PLC owns it. Switching OFF is refused too: the PLC
        # deliberately holds e.g. the target voltage and the ioniser on while the cesium is hot.
        mode = self._locking_mode(subsystem)
        if mode and leaf != "cmd_ctrl_mode":
            cmd.reject(f"locked out: {subsystem} is in automatic control mode {mode} "
                       f"({MODE_NAMES.get(mode, 'PLC controlled')})")
            return
        refusal = self._cesium_hold_refusal(tag, value)
        if refusal:
            cmd.reject(refusal)
            return
        if leaf == "cmd_enable" and value and subsystem in RELAY_SUBSYSTEMS \
                and self.state.get("ion_beam.facilities.safety_relay_active") != 1.0:
            cmd.reject("safety relay de-energised - it can't be switched on")
            return
        meta = self.registry_limits.get(tag)
        if meta is not None:
            lo, hi = meta["min_val"], meta["max_val"]
            if lo is None or hi is None:
                cmd.reject("no min/max limits configured for this tag in system_tags.json")
                return
            try:
                number = float(value)
            except (ValueError, TypeError):
                cmd.reject(f"value {value!r} is not a number")
                return
            if not lo <= number <= hi:
                cmd.reject(f"{number:g} is outside the allowed range [{lo:g}, {hi:g}]")
                return
        ok, reason = self._write_tag(tag, value)
        if ok:
            cmd.ok("written to PLC")
        else:
            cmd.fail(reason)

    def _cesium_hold_refusal(self, tag, value):
        """Why the PLC would not do this while the cesium is hot (None if it would)."""
        if self.state.get("ion_beam.internal.cs_is_hot") != 1.0:
            return None
        base, _, leaf = tag.rpartition(".")
        if base not in HELD_WHILE_CS_HOT:
            return None
        est = self.state.get("ion_beam.source.cesium.estimated_temp")
        hot = "the cesium is hot" + (f" ({est:.0f} degC estimated)" if est is not None else "")
        if leaf == "cmd_enable" and not value:
            return (f"{hot}: the PLC keeps the target and the ioniser on until it has cooled, so cesium "
                    f"vapour can't coat the source's ceramics - use forced cooling or a source shutdown")
        if base == "ion_beam.source.target" and leaf == "sp_requested_voltage":
            vmin = self.state.get("ion_beam.internal.target_min_voltage")
            try:
                below = vmin is not None and float(value) < vmin
            except (TypeError, ValueError):
                return None
            if below:
                return f"{hot}: the target must stay at or above {vmin:g} kV until it has cooled"
        return None

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


    def run(self):
        self.events.log_general("Starting Daemon..." + (" (SIMULATED PLC)" if simulation_enabled() else ""))
        super().run()


if __name__ == "__main__":
    setup_logging("service_plc")
    harden_windows_process()
    PlcMicroservice().run()
