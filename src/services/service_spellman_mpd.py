import json
import logging
import os
import select
import socket
import time
from typing import Any, Dict, Optional

import orjson
import zmq

from src.core.commands import CommandServer
from src.core.event_helper import EventHelper
from src.core.log_setup import Throttle, setup_logging
from src.core.os_helper import harden_windows_process
from src.core.paths import REGISTRY_PATH, simulation_enabled

# Force Windows high-resolution timers (1ms precision)
if os.name == 'nt':
    import ctypes

    ctypes.windll.winmm.timeBeginPeriod(1)

from src.core.network_map import (
    ZMQ_PORT_SPELLMAN_PUB, ZMQ_PORT_SPELLMAN_CMD, ZMQ_PORT_PLC_PUB,
    ZMQ_PORT_HEARTBEAT, TOPIC_SPELLMAN_DATA
)

# Protocol Constants
STX = "\x02"
LF = "\x0A"
SOCKET_TIMEOUT = 1.0
POLL_INTERVAL = 0.1
MAX_CMD_AGE = 0.5
RECONNECT_DELAYS = (1.0, 2.0, 5.0, 10.0)
PLC_TELEMETRY_TIMEOUT = 2.0  # relay state counts as unknown (= off) if the PLC goes quiet this long

# MPD Commands (per Manual 48113-21 Issue 3, Section 3.2)
CMD_REQ_STATUS = "SR"
CMD_REQ_KV = "M0"
CMD_REQ_MA = "M1"
CMD_SET_KV = "V1"
CMD_SET_MA = "I1"
CMD_HV_ENABLE = "EN"

log = logging.getLogger("service_spellman_mpd")

# Software arc detection; override in config/mpd_config.json under "arc_detection"
# (top level for every supply, or inside a device entry for that supply only).
ARC_DEFAULTS = {
    "collapse_fraction": 0.10,  # output below this fraction of the setpoint = collapsed = arc
    "min_setpoint_kv": 0.5,     # not judged below this setpoint (a low reading is normal there)
    "settle_s": 3.0,            # longest wait for the output to come up after an enable / setpoint change
    "overcurrent_ua": 500.0,    # a current above this counts as an arc too
}


class ArcDetector:
    """Software arc detection for one supply.

    During an arc the supply runs into its current limit and the output voltage
    collapses to a small fraction of the setpoint; that collapse is what is detected
    (readback below collapse_fraction x setpoint, or current above overcurrent_ua).
    The result latches stat_arc_exceeded, which the PLC service turns into a PLC fault.

    Not judged:
      - below min_setpoint_kv: at a setpoint of a few volts a near-zero reading is normal;
      - while the output is still coming up after an enable or a setpoint change. Watching
        starts once the output has reached half the setpoint, or settle_s after the change,
        whichever comes first - so small steps (the optimiser) never blind it, a large step
        up can't trip it while the output ramps, and a supply that never comes up at all
        (a short at switch-on) is still caught after settle_s.
    """

    def __init__(self, settings: Optional[dict] = None):
        self.s = {**ARC_DEFAULTS, **(settings or {})}
        self._sp = None       # setpoint being watched (kV)
        self._since = 0.0     # when that setpoint / the enable was first seen
        self._armed = False

    def restart(self):
        """The output was switched on or off: wait for it to come up again before judging."""
        self._sp = None

    def update(self, enabled: bool, sp_kv: Optional[float], rb_kv: Optional[float],
               rb_ua: Optional[float], now: float) -> Optional[str]:
        """Feed one poll of the supply; returns why it looks like an arc, else None."""
        if not enabled:
            self._sp = None
            return None
        if sp_kv is None or rb_kv is None:
            return None
        sp, rb = abs(sp_kv), abs(rb_kv)
        if self._sp is None or abs(sp - self._sp) > 1e-6:
            self._sp, self._since, self._armed = sp, now, False
        if not self._armed:
            if rb >= 0.5 * sp or now - self._since >= self.s["settle_s"]:
                self._armed = True
            else:
                return None
        if sp < self.s["min_setpoint_kv"]:
            return None
        if rb < self.s["collapse_fraction"] * sp:
            return (f"output collapsed to {rb:.3f} kV, below {self.s['collapse_fraction']:.0%} "
                    f"of the {sp:.3f} kV setpoint")
        if rb_ua is not None and rb_ua > self.s["overcurrent_ua"]:
            return f"output current {rb_ua:.1f} uA above {self.s['overcurrent_ua']:g} uA"
        return None


def arc_settings(config: dict, dev_info: dict) -> dict:
    """ARC_DEFAULTS <- mpd_config 'arc_detection' <- the device's own 'arc_detection'."""
    merged = dict(ARC_DEFAULTS)
    for source in (config.get("arc_detection") or {}, dev_info.get("arc_detection") or {}):
        for key, value in source.items():
            if key.startswith("_"):
                continue
            if key not in ARC_DEFAULTS:
                log.warning("Unknown arc_detection setting %r in mpd_config.json (ignored)", key)
            elif isinstance(value, bool) or not isinstance(value, (int, float)):
                log.warning("arc_detection %s=%r in mpd_config.json is not a number (using %g)",
                            key, value, merged[key])
            else:
                merged[key] = float(value)
    return merged


class SpellmanMPDProtocol:
    def __init__(self, ip: str, port: int, bus_id: str):
        self.ip = ip
        self.port = port
        self.bus_id = bus_id
        self.sock = None
        self.connected = False
        self.log = logging.getLogger(f"service_spellman_mpd.{bus_id}")

    def connect(self):
        self.log.debug("Attempting TCP connection to %s:%s", self.ip, self.port)
        if self.sock:
            try:
                self.sock.close()
            except OSError as e:
                self.log.debug("Error closing stale socket: %s", e)
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock.settimeout(SOCKET_TIMEOUT)
            self.sock.bind(('192.168.1.100', 0))
            self.sock.connect((self.ip, self.port))
            self.connected = True
            self.log.info("Connected to %s:%s", self.ip, self.port)
        except OSError as e:
            self.connected = False
            self.sock = None
            raise ConnectionError(f"{self.bus_id} {self.ip}:{self.port}: {type(e).__name__}: {e}") from e

    def _calculate_checksum(self, payload: str) -> str:
        ascii_sum = sum(ord(c) for c in payload)
        csum = 0x200 - ascii_sum
        csum = csum & 0xFF
        csum = csum & 0x7F
        csum = csum | 0x40
        calc_str = f"{csum:02X}"
        return calc_str

    def _generate_frame(self, address: str, dev_type: str, cmd: str, operator: str = "", data: str = "") -> bytes:
        payload = f"{address}{dev_type}{cmd}{operator}{data}"
        csum = self._calculate_checksum(payload)
        frame = f"{STX}{payload}{csum}{LF}".encode('ascii')
        return frame

    def transaction(self, address: str, dev_type: str, cmd: str, operator: str = "", data: str = "") -> Optional[str]:
        if not self.connected:
            return None

        try:
            # Clear stale data from RX buffer
            while select.select([self.sock], [], [], 0.0)[0]:
                if not self.sock.recv(1024):
                    raise ConnectionError("connection closed by the Waveshare bridge")

            frame = self._generate_frame(address, dev_type, cmd, operator, data)
            self.sock.sendall(frame)
            time.sleep(0.04)

            res = b""
            start_time = time.time()
            while not res.endswith(LF.encode('ascii')):
                chunk = self.sock.recv(1)
                if not chunk:
                    break
                res += chunk
                if time.time() - start_time > SOCKET_TIMEOUT:
                    break

            res_str = res.decode('ascii', errors='replace')

            if res_str.startswith(STX) and res_str.endswith(LF):
                parsed_data = res_str[1:-3]
                return parsed_data
            else:
                return None

        except socket.timeout:
            return None
        except (OSError, ConnectionError, ValueError) as e:
            self.log.warning("Transaction %s%s failed: %s: %s", cmd, operator, type(e).__name__, e)
            self.connected = False
            return None


def _protocol_class():
    if simulation_enabled():
        from src.sim.spellman import FakeMPDProtocol
        return FakeMPDProtocol
    return SpellmanMPDProtocol


class SpellmanMicroservice:
    def __init__(self):
        self.context = zmq.Context()
        self.events = EventHelper("service_spellman_mpd")

        self.pub_socket = self.context.socket(zmq.PUB)
        self.pub_socket.setsockopt(zmq.SNDHWM, 5)
        self.pub_socket.bind(ZMQ_PORT_SPELLMAN_PUB)

        self.plc_socket = self.context.socket(zmq.SUB)
        self.plc_socket.connect(ZMQ_PORT_PLC_PUB)
        self.plc_socket.setsockopt_string(zmq.SUBSCRIBE, "")

        self.commands = CommandServer(ZMQ_PORT_SPELLMAN_CMD, "service_spellman_mpd", context=self.context,
                                      events=self.events, max_age=MAX_CMD_AGE)

        self.hb_socket = self.context.socket(zmq.PUB)
        self.hb_socket.connect(ZMQ_PORT_HEARTBEAT)

        # --- ISA-95 Physical Routing Map ---
        self.location_routing = {
            "source_einzel": "ion_beam.source.einzel",
            "beamline_einzel": "ion_beam.beamline.einzel",
            "neutral_trap_pos": "ion_beam.beamline.neutral_trap_pos",
            "neutral_trap_neg": "ion_beam.beamline.neutral_trap_neg"
        }

        self.safety_relay_active = False
        self.last_plc_ts = 0.0
        self.config = self._load_config()
        self.net_config = self._load_net_config()
        self.registry_limits = self._load_registry_limits()
        self.buses: Dict[str, Any] = {}
        self._bus_next_attempt: Dict[str, float] = {}
        self._bus_failures: Dict[str, int] = {}
        self._throttle = Throttle(60.0)

        self.state: Dict[str, Any] = {"timestamp": 0.0, "system.cycle_time_ms": 0.0}
        self.sp_cache: Dict[str, float] = {}
        self.last_hb_time = 0.0
        self.running = True

        # --- Arbitration State per device ---
        self.active_ctrl_modes: Dict[str, int] = {prefix: 0 for prefix in self.location_routing.values()}

        # --- Software Arc Detection ---
        self.arc_detectors: Dict[str, ArcDetector] = {
            dev_name: ArcDetector(arc_settings(self.config, dev_info))
            for _, dev_name, dev_info in self._configured_devices()}
        self.arc_fault_latches: Dict[str, bool] = {}

    def stop(self):
        self.running = False

    def _configured_devices(self):
        for bus in self.config.get("buses", []):
            for dev_name, dev_info in bus.get("devices", {}).items():
                yield bus["bus_id"], dev_name, dev_info

    def _load_config(self) -> dict:
        config_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../config/mpd_config.json'))
        try:
            with open(config_path, "r") as f:
                data = json.load(f)
                log.info("MPD config loaded: %d bus(es).", len(data.get('buses', [])))
                return data
        except (OSError, ValueError) as e:
            self.events.log_general(f"CRITICAL: Failed to load mpd_config.json: {e}", severity="CRITICAL")
            return {"buses": []}

    def _load_net_config(self) -> dict:
        config_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../config/network_config.json'))
        try:
            with open(config_path, "r") as f:
                return json.load(f)
        except (OSError, ValueError) as e:
            self.events.log_general(f"CRITICAL: Failed to load network_config.json: {e}", severity="CRITICAL")
            return {}

    def _load_registry_limits(self) -> dict:
        registry_path = REGISTRY_PATH
        limits = {}
        try:
            with open(registry_path, "r") as f:
                registry = json.load(f)
                for tag, data in registry.items():
                    if data.get("source") == "service_spellman" and data.get("auto_controllable"):
                        limits[tag] = {
                            "min_val": data.get("min_val"),
                            "max_val": data.get("max_val")
                        }
        except (OSError, ValueError) as e:
            self.events.log_general(f"Failed to load registry limits (setpoints will be refused): {e}",
                                    severity="ERROR")
        missing = sorted(t for t, lim in limits.items() if lim["min_val"] is None or lim["max_val"] is None)
        if missing:
            log.warning("No min/max limits in system_tags.json for %s - commands to these will be refused "
                        "(current limits come from min_current_ua/max_current_ua in mpd_config.json)",
                        ", ".join(missing))
        return limits

    def _update_safety_permissives(self):
        try:
            while True:
                topic, msg = self.plc_socket.recv_multipart(flags=zmq.NOBLOCK)
                try:
                    payload = orjson.loads(msg)
                except ValueError:
                    continue
                relay_state = payload.get("ion_beam.facilities.safety_relay_active")
                self.last_plc_ts = time.time()
                new_state = bool(relay_state)  # None (PLC disconnected) counts as off
                if self.safety_relay_active != new_state:
                    self.events.log_general(
                        f"Safety relay {'ENERGISED' if new_state else 'DE-ENERGISED'} - Spellman supplies "
                        f"{'powered' if new_state else 'unpowered, outputs reported disabled'}")
                self.safety_relay_active = new_state
        except zmq.Again:
            pass
        # If the PLC service goes quiet we cannot know the relay state: treat it as off
        if self.safety_relay_active and time.time() - self.last_plc_ts > PLC_TELEMETRY_TIMEOUT:
            self.events.log_general("No PLC telemetry - safety relay state unknown, treating as de-energised",
                                    severity="WARNING")
            self.safety_relay_active = False

    def _manage_connections(self):
        ws_list = self.net_config.get("waveshares", {}).get("spellman", {})
        Protocol = _protocol_class()

        for bus in self.config.get("buses", []):
            bus_id = bus["bus_id"]
            if bus_id not in self.buses:
                target_net = ws_list.get(bus_id.replace("_waveshare", ""))
                if target_net:
                    log.info("Initializing bus '%s' at %s:%s", bus_id, target_net['ip'], target_net['port'])
                    self.buses[bus_id] = Protocol(target_net["ip"], target_net["port"], bus_id)
                else:
                    if (n := self._throttle.ready(f"nobus-{bus_id}")) is not None:
                        log.error("Bus '%s' not found in network_config under waveshares.spellman%s",
                                  bus_id, Throttle.suffix(n))
                    continue

            hw = self.buses[bus_id]
            if hw.connected or time.time() < self._bus_next_attempt.get(bus_id, 0.0):
                continue
            try:
                hw.connect()
                if self._bus_failures.get(bus_id):
                    self.events.log_general(f"Bus {bus_id} reconnected")
                self._bus_failures[bus_id] = 0
                self._throttle.reset(f"connect-{bus_id}")
            except (ConnectionError, OSError) as e:
                fails = self._bus_failures.get(bus_id, 0) + 1
                self._bus_failures[bus_id] = fails
                self._bus_next_attempt[bus_id] = time.time() + RECONNECT_DELAYS[min(fails - 1, len(RECONNECT_DELAYS) - 1)]
                if (n := self._throttle.ready(f"connect-{bus_id}")) is not None:
                    self.events.log_general(f"Bus {bus_id} connection failed: {e}{Throttle.suffix(n)}",
                                            severity="WARNING")
            self._heartbeat()

    def _sever_connections(self):
        for bus_id, hw in self.buses.items():
            if hw.connected and hw.sock:
                log.info("Safety relay off: severing connection to bus '%s'.", bus_id)
                try:
                    hw.sock.close()
                except OSError:
                    pass
                hw.connected = False

    def _locate(self, target_dev):
        for bus in self.config.get("buses", []):
            if target_dev in bus.get("devices", {}):
                return bus["bus_id"], bus["devices"][target_dev]
        return None, None

    def _reset_faults(self, cmd):
        for key in self.arc_fault_latches.keys():
            self.arc_fault_latches[key] = False
        cleared = 0
        for bus_id, _, dev_info in self._configured_devices():
            hw = self.buses.get(bus_id)
            if hw and hw.connected:
                if hw.transaction(dev_info["address"], dev_info["dev_type"], "CF", "=", "1") is not None:
                    cleared += 1
        cmd.ok(f"software arc latches cleared; hardware fault clear sent to {cleared} supply(ies)")

    def _process_commands(self):
        for cmd in self.commands.poll():
            tag, value, origin = cmd.tag, cmd.value, cmd.origin

            # --- GLOBAL FAULT RESET ---
            if tag == "ion_beam.system.cmd_fault_reset":
                if bool(value):
                    self._reset_faults(cmd)
                else:
                    cmd.ok("nothing to do")
                continue

            # --- Route to specific equipment module ---
            target_dev = prefix = cmd_type = None
            for dev_name, loc_prefix in self.location_routing.items():
                if tag.startswith(loc_prefix + "."):
                    target_dev, prefix, cmd_type = dev_name, loc_prefix, tag.split('.')[-1]
                    break
            if not target_dev:
                cmd.reject("no Spellman supply handles this tag")
                continue

            # --- 1. ARBITRATION: Handle Control Mode Changes ---
            if cmd_type == "cmd_ctrl_mode":
                try:
                    self.active_ctrl_modes[prefix] = int(value)
                except (ValueError, TypeError):
                    cmd.reject(f"invalid control mode {value!r}")
                    continue
                self.events.log_general(f"Arbitration: {target_dev} mode set to {int(value)}")
                cmd.ok()
                continue

            # --- 2. ARBITRATION: Enforce Lockout per Device ---
            active_mode = self.active_ctrl_modes.get(prefix, 0)
            if active_mode > 0 and origin != "optimizer":
                cmd.reject(f"locked out: {target_dev} is in automatic control mode {active_mode}")
                continue

            target_bus, dev_info = self._locate(target_dev)
            if target_bus is None:
                cmd.reject(f"{target_dev} is not configured in mpd_config.json")
                continue

            if not self.safety_relay_active:
                if cmd_type == "cmd_enable" and not bool(value):
                    cmd.ok("already off: safety relay de-energised")
                else:
                    cmd.reject("safety relay de-energised - supply is unpowered")
                continue

            hw = self.buses.get(target_bus)
            if hw is None or not hw.connected:
                cmd.reject(f"bus {target_bus} not connected")
                continue

            addr = dev_info["address"]
            dtype = dev_info["dev_type"]

            try:
                value_float = float(value)
            except (ValueError, TypeError):
                cmd.reject(f"value {value!r} is not a number")
                continue

            if cmd_type == "sp_requested_voltage":
                tag_limits = self.registry_limits.get(tag, {})
                min_v = tag_limits.get("min_val")
                max_v = tag_limits.get("max_val")
                if min_v is None or max_v is None:
                    cmd.reject("no min/max limits configured for this tag in system_tags.json")
                    continue
                if not (min_v <= value_float <= max_v):
                    cmd.reject(f"{value_float:g} kV is outside the allowed range [{min_v:g}, {max_v:g}] kV")
                    continue

                volts_req = value_float * 1000.0
                formatted_v = f"{volts_req:07.1f}"
                res = hw.transaction(addr, dtype, CMD_SET_KV, "=", formatted_v)
                if res and "*" not in res:
                    self.sp_cache[f"{prefix}.sp_actual_voltage"] = value_float
                    cmd.ok(f"supply accepted {formatted_v} V")
                else:
                    cmd.fail(f"supply did not accept the setpoint (reply: {res!r})")

            elif cmd_type == "sp_requested_current":
                tag_limits = self.registry_limits.get(tag, {})
                min_i = tag_limits.get("min_val")
                max_i = tag_limits.get("max_val")
                if min_i is None or max_i is None:
                    cmd.reject("no min/max limits configured for this tag in system_tags.json")
                    continue
                if not (min_i <= value_float <= max_i):
                    cmd.reject(f"{value_float:g} uA is outside the allowed range [{min_i:g}, {max_i:g}] uA")
                    continue

                ua_req = value_float if value_float > 0.0 else 0.0
                formatted_i = f"{ua_req:07.1f}"
                res = hw.transaction(addr, dtype, CMD_SET_MA, "=", formatted_i)
                if res and "*" not in res:
                    self.sp_cache[f"{prefix}.sp_actual_current"] = value_float
                    cmd.ok(f"supply accepted {formatted_i} uA")
                else:
                    cmd.fail(f"supply did not accept the current limit (reply: {res!r})")

            elif cmd_type == "cmd_enable":
                enable_str = "1" if value_float else "0"
                if hw.transaction(addr, dtype, CMD_HV_ENABLE, "=", enable_str) is not None:
                    self._arc_detector(target_dev).restart()
                    cmd.ok("output enabled" if value_float else "output disabled")
                else:
                    cmd.fail("no reply from supply")
            else:
                cmd.reject(f"unsupported command '{cmd_type}'")
        self.commands.finish()

    # ------------------------------------------------------------------ telemetry
    def _set_unknown(self, prefix, enabled):
        """Publish 'unknown' (null) for everything we cannot currently measure."""
        for key in ("rb_voltage", "rb_current", "sp_actual_voltage", "sp_actual_current",
                    "stat_overvoltage", "stat_overcurrent", "stat_fail"):
            self.state[f"{prefix}.{key}"] = None
        self.state[f"{prefix}.stat_enabled"] = enabled
        self.state[f"{prefix}.stat_arc_exceeded"] = 1.0 if self.arc_fault_latches.get(
            next((d for d, p in self.location_routing.items() if p == prefix), ""), False) else 0.0

    def _publish_relay_off_state(self):
        """Safety relay de-energised: the supplies have no power, so their outputs are off.
        Before this, the last 'enabled' state and readbacks kept being published and the
        HMI showed a powered-down supply as still enabled."""
        for bus_id, dev_name, dev_info in self._configured_devices():
            prefix = self.location_routing.get(dev_name, f"ion_beam.source.{dev_name}")
            self.state[f"{prefix}.rb_ctrl_mode"] = float(self.active_ctrl_modes.get(prefix, 0))
            self.state[f"{prefix}.stat_comms_fail"] = 0.0   # expected silence, not a comms fault
            self._set_unknown(prefix, 0.0)
            self._arc_detector(dev_name).restart()

    def _arc_detector(self, dev_name: str) -> ArcDetector:
        if dev_name not in self.arc_detectors:
            self.arc_detectors[dev_name] = ArcDetector(arc_settings(self.config, self._locate(dev_name)[1] or {}))
        return self.arc_detectors[dev_name]

    def _poll_device(self, bus_id: str, dev_name: str, dev_info: dict):
        addr, dtype = dev_info["address"], dev_info["dev_type"]
        prefix = self.location_routing.get(dev_name, f"ion_beam.source.{dev_name}")

        # Publish active control mode
        self.state[f"{prefix}.rb_ctrl_mode"] = float(self.active_ctrl_modes.get(prefix, 0))

        hw = self.buses.get(bus_id)
        if not hw or not hw.connected:
            self.state[f"{prefix}.stat_comms_fail"] = 1.0
            self._set_unknown(prefix, None)
            return

        # Status first: if the supply doesn't answer, don't spend 4 more timeouts on it
        raw_status = hw.transaction(addr, dtype, CMD_REQ_STATUS, "?")
        if raw_status is None:
            self.state[f"{prefix}.stat_comms_fail"] = 1.0
            self._set_unknown(prefix, None)   # enabled state genuinely unknown (HV may still be on)
            if (n := self._throttle.ready(f"poll-{dev_name}")) is not None:
                log.warning("No reply from %s (status query)%s", dev_name, Throttle.suffix(n))
            return

        # 1. Poll live readbacks
        raw_kv = hw.transaction(addr, dtype, CMD_REQ_KV, "?")
        raw_ma = hw.transaction(addr, dtype, CMD_REQ_MA, "?")

        # 2. Poll hardware-verified setpoints
        raw_sp_v = hw.transaction(addr, dtype, CMD_SET_KV, "?")
        raw_sp_i = hw.transaction(addr, dtype, CMD_SET_MA, "?")

        comms_fail = raw_kv is None
        self.state[f"{prefix}.stat_comms_fail"] = 1.0 if comms_fail else 0.0
        if comms_fail:
            self._set_unknown(prefix, None)
            return
        self._throttle.reset(f"poll-{dev_name}")

        volts_rb = None
        ua_rb = None
        try:
            # Parse live readbacks (M0 / M1)
            volts_rb = float(raw_kv.split('=')[-1])
            self.state[f"{prefix}.rb_voltage"] = volts_rb / 1000.0
            if raw_ma:
                ua_rb = float(raw_ma.split('=')[-1])
            self.state[f"{prefix}.rb_current"] = ua_rb

            # Parse Hardware Setpoints (V1 / I1)
            if raw_sp_v:
                sp_volts = float(raw_sp_v.split('=')[-1])
                self.state[f"{prefix}.sp_actual_voltage"] = sp_volts / 1000.0
            elif f"{prefix}.sp_actual_voltage" in self.sp_cache:
                self.state[f"{prefix}.sp_actual_voltage"] = self.sp_cache[f"{prefix}.sp_actual_voltage"]

            if raw_sp_i:
                sp_ua = float(raw_sp_i.split('=')[-1])
                self.state[f"{prefix}.sp_actual_current"] = sp_ua
            elif f"{prefix}.sp_actual_current" in self.sp_cache:
                self.state[f"{prefix}.sp_actual_current"] = self.sp_cache[f"{prefix}.sp_actual_current"]

        except ValueError as e:
            log.warning("Unparseable readback from %s: %s (M0=%r M1=%r V1=%r I1=%r)",
                        dev_name, e, raw_kv, raw_ma, raw_sp_v, raw_sp_i)

        try:
            status_hex_str = raw_status.split('=')[-1]
            status_word = int(status_hex_str, 16)
        except ValueError:
            log.warning("Unparseable status register from %s: %r", dev_name, raw_status)
            self.state[f"{prefix}.stat_enabled"] = None
            return

        is_enabled = bool(status_word & 0x01)
        self.state[f"{prefix}.stat_enabled"] = 1.0 if is_enabled else 0.0

        # Map directly to ISA-95 Equipment flags
        self.state[f"{prefix}.stat_overvoltage"] = 1.0 if (status_word & 0x04) else 0.0
        self.state[f"{prefix}.stat_overcurrent"] = 1.0 if (status_word & 0x08) else 0.0

        # Combine Over-temp and Power-rail collapse into generic Hardware Fail
        self.state[f"{prefix}.stat_fail"] = 1.0 if (status_word & 0x30) else 0.0

        # --- TEMPORARY RAW STATUS DEBUG ---
        self.state[f"{prefix}.debug_raw_status"] = raw_status
        self.state[f"{prefix}.debug_general_fault"] = 1.0 if (status_word & 0x02) else 0.0
        self.state[f"{prefix}.debug_hw_enable"] = 1.0 if (status_word & 0x40) else 0.0
        self.state[f"{prefix}.debug_sw_enable"] = 1.0 if (status_word & 0x80) else 0.0
        # ----------------------------------

        # --- AUTONOMOUS SOFTWARE ARC DETECTION ---
        # (Before: volts were compared with kV x 0.25, so it only fired on a total collapse.)
        why = self._arc_detector(dev_name).update(
            is_enabled, self.state.get(f"{prefix}.sp_actual_voltage"),
            volts_rb / 1000.0 if volts_rb is not None else None, ua_rb, time.time())
        if why:
            if not self.arc_fault_latches.get(dev_name, False):
                self.events.log_general(f"ARC DETECTED on {dev_name}: {why} - latched until fault reset",
                                        severity="WARNING", metadata={"device": dev_name, "reason": why})
            self.arc_fault_latches[dev_name] = True

        self.state[f"{prefix}.stat_arc_exceeded"] = 1.0 if self.arc_fault_latches.get(dev_name, False) else 0.0

    def _heartbeat(self):
        if time.time() - self.last_hb_time >= 0.5:
            self.hb_socket.send_json({"service": "service_spellman_mpd", "ts": time.time()})
            self.last_hb_time = time.time()

    def run(self):
        self.events.log_general("[Spellman Service] Daemon Starting (ISA-95 Architecture)..."
                                + (" (SIMULATED SUPPLIES)" if simulation_enabled() else ""))
        next_tick = time.perf_counter() + POLL_INTERVAL

        while self.running:
            cycle_start = time.perf_counter()
            self._update_safety_permissives()

            if self.safety_relay_active:
                self._manage_connections()
            else:
                self._sever_connections()

            self._process_commands()   # always answered; refused with a reason while unpowered

            if self.safety_relay_active:
                for bus_id, dev_name, dev_info in self._configured_devices():
                    self._poll_device(bus_id, dev_name, dev_info)
                    self._heartbeat()   # a slow/unresponsive supply must not look like a hung service
                    time.sleep(0.01)
            else:
                self._publish_relay_off_state()

            self.state["timestamp"] = time.time()
            self.state["system.cycle_time_ms"] = (time.perf_counter() - cycle_start) * 1000

            try:
                self.pub_socket.send_multipart([TOPIC_SPELLMAN_DATA, orjson.dumps(self.state)])
            except zmq.ZMQError as e:
                if (n := self._throttle.ready("publish")) is not None:
                    log.error("ZMQ publish failed: %s%s", e, Throttle.suffix(n))

            self._heartbeat()

            sleep_time = next_tick - time.perf_counter()
            if sleep_time > 0.002:
                time.sleep(sleep_time - 0.002)
            while time.perf_counter() < next_tick:
                pass
            next_tick += POLL_INTERVAL
            if next_tick < time.perf_counter() - 1.0:
                next_tick = time.perf_counter() + POLL_INTERVAL

        self.commands.close()
        for sock in (self.pub_socket, self.plc_socket, self.hb_socket):
            sock.close(0)


if __name__ == "__main__":
    setup_logging("service_spellman_mpd")
    harden_windows_process()
    SpellmanMicroservice().run()
