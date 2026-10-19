"""Spellman MPD HV supplies (source einzel, beamline einzel, neutral traps) on RS485 buses
behind Waveshare bridges.

The supplies are powered through the safety relay: while it is off (or its state unknown) the
buses are severed, the supplies report disabled with unknown readbacks, and commands are
refused with that reason. Software arc detection (src/core/arc_detection.py) latches
stat_arc_exceeded until fault reset; the PLC service turns it into a PLC fault.

A supply comes up from every power-up holding a current limit of 0, and with none its output
can't rise. Before a non-zero voltage or an enable, the service sets one (default_current_ua in
mpd_config.json, else 50 uA) if the supply holds none (see ensure_current_limit).
"""
import time
from typing import Any, Dict

from src.core import hard_caps
from src.core.arc_detection import ARC_DEFAULTS, ArcDetector, arc_settings  # noqa: F401  (re-exported)
from src.core.device_service import DeviceService, ReconnectBackoff
from src.core.log_setup import Throttle, setup_logging
from src.core.os_helper import harden_windows_process
from src.core.paths import simulation_enabled
from src.core.spellman_locations import LOCATIONS     # where each supply sits in the tag tree
from src.core.network_map import ZMQ_PORT_SPELLMAN_PUB, ZMQ_PORT_SPELLMAN_CMD, TOPIC_SPELLMAN_DATA
from src.drivers.spellman_mpd import (CMD_HV_ENABLE, CMD_REQ_KV, CMD_REQ_MA, CMD_REQ_STATUS, CMD_SET_KV,
                                      CMD_SET_MA, SpellmanMPDProtocol)

PLC_TELEMETRY_TIMEOUT = 2.0   # relay state counts as unknown (= off) if the PLC goes quiet this long
HELD_SP_TOLERANCE_KV = 0.05   # a held setpoint read back this close to its hard cap is taken as at it
DEFAULT_CURRENT_UA = 50.0     # current limit set when a supply holds none (mpd_config.json: default_current_ua)


def _protocol_class():
    if simulation_enabled():
        from src.sim.spellman import FakeMPDProtocol
        return FakeMPDProtocol
    return SpellmanMPDProtocol


class SpellmanMicroservice(DeviceService):
    NAME = "service_spellman_mpd"
    DEVICE_LABEL = "Spellman supply"
    PUB_ENDPOINT, CMD_ENDPOINT, TOPIC = ZMQ_PORT_SPELLMAN_PUB, ZMQ_PORT_SPELLMAN_CMD, TOPIC_SPELLMAN_DATA
    POLL_INTERVAL = 0.1
    MAX_CMD_AGE = 0.5
    RECONNECT_DELAYS = (1.0, 2.0, 5.0, 10.0)
    WATCH_RELAY = True
    RELAY_MAX_AGE = PLC_TELEMETRY_TIMEOUT
    SUBSYSTEMS = tuple(LOCATIONS.values())
    REGISTRY_SOURCE = "service_spellman"

    def __init__(self):
        super().__init__()
        self.location_routing = dict(LOCATIONS)
        self.config = self.load_config("mpd_config.json", {"buses": []})
        self.net_config = self.load_config("network_config.json", {})
        missing = sorted(t for t, lim in self.registry_limits.items()
                         if lim["min_val"] is None or lim["max_val"] is None)
        if missing:
            self.log.warning("No min/max limits in system_tags.json for %s - commands to these will be refused "
                             "(current limits come from min_current_ua/max_current_ua in mpd_config.json)",
                             ", ".join(missing))
        self.buses: Dict[str, Any] = {}
        self._bus_backoff: Dict[str, Any] = {}
        self.sp_cache: Dict[str, float] = {}
        self.arc_detectors: Dict[str, ArcDetector] = {
            dev: ArcDetector(arc_settings(self.config, info)) for _, dev, info in self.configured_devices()}
        self.arc_fault_latches: Dict[str, bool] = {}

    @property
    def safety_relay_active(self) -> bool:
        return self.relay_ok

    # ------------------------------------------------------------------ configuration
    def configured_devices(self):
        for bus in self.config.get("buses", []):
            for dev_name, dev_info in bus.get("devices", {}).items():
                yield bus["bus_id"], dev_name, dev_info

    def locate(self, target_dev):
        for bus in self.config.get("buses", []):
            if target_dev in bus.get("devices", {}):
                return bus["bus_id"], bus["devices"][target_dev]
        return None, None

    def device_at(self, prefix):
        return next((d for d, p in self.location_routing.items() if p == prefix), None)

    def arc_detector(self, dev_name: str) -> ArcDetector:
        if dev_name not in self.arc_detectors:
            self.arc_detectors[dev_name] = ArcDetector(arc_settings(self.config, self.locate(dev_name)[1] or {}))
        return self.arc_detectors[dev_name]

    # ------------------------------------------------------------------ connection (per bus)
    def is_connected(self) -> bool:
        return True     # the buses connect and fail individually (manage_connections)

    def connect_device(self) -> bool:
        return True

    def manage_connections(self):
        bridges = self.net_config.get("waveshares", {}).get("spellman", {})
        protocol = _protocol_class()
        for bus in self.config.get("buses", []):
            bus_id = bus["bus_id"]
            if bus_id not in self.buses:
                net = bridges.get(bus_id.replace("_waveshare", ""))
                if not net:
                    if (n := self.throttle.ready(f"nobus-{bus_id}")) is not None:
                        self.log.error("Bus '%s' not found in network_config under waveshares.spellman%s",
                                       bus_id, Throttle.suffix(n))
                    continue
                self.log.info("Initialising bus '%s' at %s:%s", bus_id, net["ip"], net["port"])
                self.buses[bus_id] = protocol(net["ip"], net["port"], bus_id)
                self._bus_backoff[bus_id] = ReconnectBackoff(self.RECONNECT_DELAYS)
            hw, backoff = self.buses[bus_id], self._bus_backoff[bus_id]
            if hw.connected or not backoff.due(time.time()):
                continue
            try:
                hw.connect()
                if backoff.succeeded():
                    self.events.log_general(f"Bus {bus_id} reconnected")
                self.throttle.reset(f"connect-{bus_id}")
            except (ConnectionError, OSError) as e:
                backoff.failed(time.time())
                if (n := self.throttle.ready(f"connect-{bus_id}")) is not None:
                    self.events.log_general(f"Bus {bus_id} connection failed: {e}{Throttle.suffix(n)}",
                                            severity="WARNING")
            self.heartbeat()

    def sever_connections(self):
        # Unpowered, the supplies forget their setpoints: what was last set here is no longer what
        # they hold (the poll falls back to these when a supply doesn't answer a setpoint query)
        self.sp_cache.clear()
        for bus_id, hw in self.buses.items():
            if hw.connected and hw.sock:
                self.log.info("Safety relay off: severing connection to bus '%s'.", bus_id)
                try:
                    hw.sock.close()
                except OSError:
                    pass
                hw.connected = False

    # ------------------------------------------------------------------ safety
    def on_relay_change(self, ok: bool, why: str):
        if ok:
            self.events.log_general("Safety relay ENERGISED - Spellman supplies powered")
        elif why.startswith("no PLC"):
            self.events.log_general("No PLC telemetry - safety relay state unknown, treating as de-energised",
                                    severity="WARNING")
        else:
            self.events.log_general("Safety relay DE-ENERGISED (or its state unknown) - Spellman supplies "
                                    "unpowered, outputs reported disabled")

    def cycle(self):
        if self.relay_ok:
            self.manage_connections()
        else:
            self.sever_connections()

    def permit(self, subsystem: str):
        if not self.relay_ok:
            return False, "safety relay de-energised - supply unpowered"
        dev = self.device_at(subsystem)
        bus_id, info = self.locate(dev) if dev else (None, None)
        if bus_id is None:
            return False, "not configured in mpd_config.json"
        hw = self.buses.get(bus_id)
        if hw is None or not hw.connected:
            return False, f"bus {bus_id} not connected"
        if self.state.get(f"{subsystem}.stat_comms_fail") == 1.0:
            return False, "supply not answering"
        return True, ""

    def reset_faults(self, cmd):
        for key in self.arc_fault_latches:
            self.arc_fault_latches[key] = False
        cleared = 0
        for bus_id, _, info in self.configured_devices():
            hw = self.buses.get(bus_id)
            if hw and hw.connected:
                res = hw.transaction(info["address"], info["dev_type"], "CF", "=", "1")
                if res and "*" not in res:
                    cleared += 1
        cmd.ok(f"software arc latches cleared; hardware fault clear sent to {cleared} supply(ies)")

    # ------------------------------------------------------------------ current limit
    @staticmethod
    def default_current_ua(info: dict) -> float:
        """The current limit to set when a supply holds none: the device's default_current_ua in
        mpd_config.json, else DEFAULT_CURRENT_UA - kept within its min/max_current_ua."""
        ua = info.get("default_current_ua", DEFAULT_CURRENT_UA)
        if not hard_caps.is_number(ua) or ua <= 0:
            ua = DEFAULT_CURRENT_UA
        hi, lo = info.get("max_current_ua"), info.get("min_current_ua")
        if hard_caps.is_number(hi) and hi > 0:
            ua = min(ua, hi)
        if hard_caps.is_number(lo):
            ua = max(ua, lo)
        return float(ua)

    def ensure_current_limit(self, hw, prefix: str, info: dict) -> str:
        """Set the default current limit if the supply holds none (0, or not read yet).

        Every power-up leaves the limit at 0, and with none the output can't rise: the arc
        detection then sees an output that collapsed. The limit used to come only from the HMI's
        source panel, which sent 50 uA with each voltage - and only when the registry had current
        limits for the supply. A voltage set from anywhere else (a recipe, the optimiser), or an HMI
        reading a system_tags.json older than those limits, left the output at 0 and latched an
        arc. Returns a note for the reply ('' if nothing was needed); raises IOError if the supply
        won't take it."""
        held = self.state.get(f"{prefix}.sp_actual_current")
        if hard_caps.is_number(held) and held > 0:
            return ""
        ua = self.default_current_ua(info)
        res = hw.transaction(info["address"], info["dev_type"], CMD_SET_MA, "=", f"{ua:07.1f}")
        if not res or "*" in res:
            raise IOError(f"the supply holds no current limit and did not accept {ua:g} uA (reply: {res!r}); "
                          "without one its output can't rise")
        self.sp_cache[f"{prefix}.sp_actual_current"] = ua
        self.state[f"{prefix}.sp_actual_current"] = ua     # until the next poll reads it back
        return f"current limit {'was 0' if held == 0 else 'not read yet'}, set to {ua:g} uA"

    # ------------------------------------------------------------------ commands
    def handle_command(self, cmd, prefix: str, leaf: str):
        target_dev = self.device_at(prefix)
        bus_id, info = self.locate(target_dev)
        if bus_id is None:
            cmd.reject(f"{target_dev} is not configured in mpd_config.json")
            return
        if not self.relay_ok:
            if leaf == "cmd_enable" and not cmd.value:
                cmd.ok("already off: safety relay de-energised")
            else:
                cmd.reject("safety relay de-energised - supply is unpowered")
            return
        hw = self.buses.get(bus_id)
        if hw is None or not hw.connected:
            cmd.reject(f"bus {bus_id} not connected")
            return
        value = self.number(cmd)
        if value is None:
            return
        addr, dtype = info["address"], info["dev_type"]

        if leaf == "sp_requested_voltage":
            if self.within_limits(cmd, value, "kV"):
                try:
                    note = self.ensure_current_limit(hw, prefix, info) if value else ""
                except IOError as e:
                    cmd.fail(f"voltage not set: {e}")
                    return
                formatted = f"{value * 1000.0:07.1f}"
                res = hw.transaction(addr, dtype, CMD_SET_KV, "=", formatted)
                if res and "*" not in res:
                    self.sp_cache[f"{prefix}.sp_actual_voltage"] = value
                    cmd.ok(f"supply accepted {formatted} V" + (f" ({note})" if note else ""))
                else:
                    cmd.fail(f"supply did not accept the setpoint (reply: {res!r})")
        elif leaf == "sp_requested_current":
            if self.within_limits(cmd, value, "uA"):
                formatted = f"{max(0.0, value):07.1f}"
                res = hw.transaction(addr, dtype, CMD_SET_MA, "=", formatted)
                if res and "*" not in res:
                    self.sp_cache[f"{prefix}.sp_actual_current"] = value
                    cmd.ok(f"supply accepted {formatted} uA")
                else:
                    cmd.fail(f"supply did not accept the current limit (reply: {res!r})")
        elif leaf == "cmd_enable":
            held = self.state.get(f"{prefix}.sp_actual_voltage")    # the setpoint the supply holds (polled)
            cap = hard_caps.SPELLMAN_CAP_KV        # (read back through its DAC: a hair over at the cap is "at")
            if value and cap.breach(held) and not abs(held) <= cap.high + HELD_SP_TOLERANCE_KV:
                # e.g. set on its front panel: enabling would drive it
                cmd.reject(hard_caps.SPELLMAN_CAP_KV.explain("the voltage setpoint the supply holds", held)
                           + " Set the voltage here first.")
                return
            note = ""
            if value:
                try:
                    note = self.ensure_current_limit(hw, prefix, info)
                except IOError as e:
                    cmd.fail(f"output not enabled: {e}")
                    return
            res = hw.transaction(addr, dtype, CMD_HV_ENABLE, "=", "1" if value else "0")
            # A reply containing '*' is a refusal (any reply used to count as success, so a refused
            # 'disable' was reported as done while HV stayed on).
            if res and "*" not in res:
                self.arc_detector(target_dev).restart()
                cmd.ok(("output enabled" + (f" ({note})" if note else "")) if value else "output disabled")
            elif res is None:
                cmd.fail("no reply from supply")
            else:
                cmd.fail(f"supply refused the {'enable' if value else 'disable'} (reply: {res!r})")
        else:
            cmd.reject(f"unsupported command '{leaf}'")

    # ------------------------------------------------------------------ telemetry
    def publish_unknown(self):
        pass    # never called: is_connected() is always True (per-bus state is in poll_device)

    def poll_device(self):
        if not self.relay_ok:
            self.publish_relay_off_state()
            return
        for bus_id, dev_name, info in self.configured_devices():
            self.poll_supply(bus_id, dev_name, info)
            self.heartbeat()   # a slow/unresponsive supply must not look like a hung service
            time.sleep(0.01)

    def set_unknown(self, prefix, enabled):
        """Publish 'unknown' (null) for everything we cannot currently measure."""
        for key in ("rb_voltage", "rb_current", "sp_actual_voltage", "sp_actual_current",
                    "stat_overvoltage", "stat_overcurrent", "stat_fail"):
            self.state[f"{prefix}.{key}"] = None
        self.state[f"{prefix}.stat_enabled"] = enabled
        self.state[f"{prefix}.stat_arc_exceeded"] = \
            1.0 if self.arc_fault_latches.get(self.device_at(prefix) or "", False) else 0.0

    def publish_relay_off_state(self):
        """Relay de-energised: the supplies have no power, so their outputs are off (before, the last
        'enabled' state and readbacks kept being published and the HMI showed them as still on)."""
        for _, dev_name, _ in self.configured_devices():
            prefix = self.location_routing.get(dev_name, f"ion_beam.source.{dev_name}")
            self.state[f"{prefix}.stat_comms_fail"] = 0.0   # expected silence, not a comms fault
            self.set_unknown(prefix, 0.0)
            self.arc_detector(dev_name).restart()

    def poll_supply(self, bus_id: str, dev_name: str, info: dict):
        addr, dtype = info["address"], info["dev_type"]
        prefix = self.location_routing.get(dev_name, f"ion_beam.source.{dev_name}")
        hw = self.buses.get(bus_id)
        if not hw or not hw.connected:
            self.state[f"{prefix}.stat_comms_fail"] = 1.0
            self.set_unknown(prefix, None)
            return

        # Status first: if the supply doesn't answer, don't spend four more timeouts on it
        raw_status = hw.transaction(addr, dtype, CMD_REQ_STATUS, "?")
        if raw_status is None:
            self.state[f"{prefix}.stat_comms_fail"] = 1.0
            self.set_unknown(prefix, None)   # enabled state genuinely unknown (HV may still be on)
            if (n := self.throttle.ready(f"poll-{dev_name}")) is not None:
                self.log.warning("No reply from %s (status query)%s", dev_name, Throttle.suffix(n))
            return

        raw_kv = hw.transaction(addr, dtype, CMD_REQ_KV, "?")
        raw_ma = hw.transaction(addr, dtype, CMD_REQ_MA, "?")
        raw_sp_v = hw.transaction(addr, dtype, CMD_SET_KV, "?")
        raw_sp_i = hw.transaction(addr, dtype, CMD_SET_MA, "?")
        if raw_kv is None:
            self.state[f"{prefix}.stat_comms_fail"] = 1.0
            self.set_unknown(prefix, None)
            return
        self.state[f"{prefix}.stat_comms_fail"] = 0.0
        self.throttle.reset(f"poll-{dev_name}")

        volts_rb = ua_rb = None
        try:
            volts_rb = float(raw_kv.split('=')[-1])
            self.state[f"{prefix}.rb_voltage"] = volts_rb / 1000.0
            if raw_ma:
                ua_rb = float(raw_ma.split('=')[-1])
            self.state[f"{prefix}.rb_current"] = ua_rb
            if raw_sp_v:
                self.state[f"{prefix}.sp_actual_voltage"] = float(raw_sp_v.split('=')[-1]) / 1000.0
            elif f"{prefix}.sp_actual_voltage" in self.sp_cache:
                self.state[f"{prefix}.sp_actual_voltage"] = self.sp_cache[f"{prefix}.sp_actual_voltage"]
            if raw_sp_i:
                self.state[f"{prefix}.sp_actual_current"] = float(raw_sp_i.split('=')[-1])
            elif f"{prefix}.sp_actual_current" in self.sp_cache:
                self.state[f"{prefix}.sp_actual_current"] = self.sp_cache[f"{prefix}.sp_actual_current"]
        except ValueError as e:
            self.log.warning("Unparseable readback from %s: %s (M0=%r M1=%r V1=%r I1=%r)",
                             dev_name, e, raw_kv, raw_ma, raw_sp_v, raw_sp_i)

        try:
            status_word = int(raw_status.split('=')[-1], 16)
        except ValueError:
            self.log.warning("Unparseable status register from %s: %r", dev_name, raw_status)
            self.state[f"{prefix}.stat_enabled"] = None
            return

        is_enabled = bool(status_word & 0x01)
        self.state[f"{prefix}.stat_enabled"] = 1.0 if is_enabled else 0.0
        self.state[f"{prefix}.stat_overvoltage"] = 1.0 if (status_word & 0x04) else 0.0
        self.state[f"{prefix}.stat_overcurrent"] = 1.0 if (status_word & 0x08) else 0.0
        self.state[f"{prefix}.stat_fail"] = 1.0 if (status_word & 0x30) else 0.0   # over-temp / rail collapse
        # Raw status bits, kept for diagnosing the supplies
        self.state[f"{prefix}.debug_raw_status"] = raw_status
        self.state[f"{prefix}.debug_general_fault"] = 1.0 if (status_word & 0x02) else 0.0
        self.state[f"{prefix}.debug_hw_enable"] = 1.0 if (status_word & 0x40) else 0.0
        self.state[f"{prefix}.debug_sw_enable"] = 1.0 if (status_word & 0x80) else 0.0

        why = self.arc_detector(dev_name).update(
            is_enabled, self.state.get(f"{prefix}.sp_actual_voltage"),
            volts_rb / 1000.0 if volts_rb is not None else None, ua_rb, time.time())
        if why:
            if not self.arc_fault_latches.get(dev_name, False):
                self.events.log_general(f"ARC DETECTED on {dev_name}: {why} - latched until fault reset",
                                        severity="WARNING", metadata={"device": dev_name, "reason": why})
            self.arc_fault_latches[dev_name] = True
        self.state[f"{prefix}.stat_arc_exceeded"] = 1.0 if self.arc_fault_latches.get(dev_name, False) else 0.0

    def run(self):
        self.events.log_general("[Spellman Service] starting" + (" (SIMULATED SUPPLIES)" if simulation_enabled() else ""))
        super().run()


if __name__ == "__main__":
    setup_logging("service_spellman_mpd")
    harden_windows_process()
    SpellmanMicroservice().run()
