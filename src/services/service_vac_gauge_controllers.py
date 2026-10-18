"""Vacuum gauges: two Leybold GRAPHIX controllers (nodes 10 and 20, three gauges each) on the
noisy rack's RS485 bus.

Every cycle all six pressures are read; one slower item (gauge status, relay status / set
points) is read round-robin. Each gauge gets rapid-rise and approaching-set-point flags, and
the gate-valve permissive (VG1 and VG2 healthy and below their set points) goes to the PLC
service. On connect the controllers' gauge names and relay set points are checked against
vac_gauges_config.json and corrected if they differ.
"""
import time
from typing import Dict, Optional

from src.core.device_service import DeviceService
from src.core.log_setup import setup_logging
from src.core.os_helper import harden_windows_process
from src.core.paths import simulation_enabled
from src.core.network_map import (ZMQ_PORT_VACUUM_PUB, ZMQ_PORT_VACUUM_CMD, TOPIC_VACUUM_DATA,
                                  NOISY_RACK_WAVESHARE_IP, NOISY_RACK_WAVESHARE_PORT)
from src.drivers.graphix import PARAM_PRESSURE, GraphixLink

PRESSURE_STALE_S = 3.0   # a gauge with no good reading for this long is published as unknown

NODE_IDS = [10, 20]
CHANNELS = [1, 2, 3]
RELAYS = [1, 2, 3, 4, 5, 6]
PARAM_STATUS = 24
PARAM_NAME = 5
PARAM_TYPE = 4
PARAM_SERIAL = 2
RELAY_PARAMS = {
    1: {"ch": "1", "on": "2", "off": "3", "status": "4"},
    2: {"ch": "5", "on": "6", "off": "7", "status": "8"},
    3: {"ch": "9", "on": "10", "off": "11", "status": "12"},
    4: {"ch": "13", "on": "14", "off": "15", "status": "16"},
    5: {"ch": "17", "on": "18", "off": "19", "status": "20"},
    6: {"ch": "21", "on": "22", "off": "23", "status": "24"},
}
# Physical (node, channel) -> gauge location
TAG_MAP = {
    (10, 1): "ion_beam.source.vacuum_gauge_1",
    (10, 2): "ion_beam.beamline.vacuum_gauge_2",
    (10, 3): "ion_beam.beamline.vacuum_gauge_3",
    (20, 1): "ion_beam.endstation.vacuum_gauge_4",
    (20, 2): "ion_beam.loadlock.vacuum_gauge_5",
    (20, 3): "ion_beam.endstation.vacuum_gauge_6",
}
GAUGE_FLAGS = ("stat_not_found", "stat_mismatch", "stat_rapid_rise", "stat_above_sp", "stat_approaching_sp",
               "stat_relay_active", "stat_comms_fail")


def _make_link():
    if simulation_enabled():
        from src.sim.vacuum import FakeGraphixLink
        return FakeGraphixLink(NOISY_RACK_WAVESHARE_IP, NOISY_RACK_WAVESHARE_PORT)
    return GraphixLink(NOISY_RACK_WAVESHARE_IP, NOISY_RACK_WAVESHARE_PORT)


class VacuumMicroservice(DeviceService):
    NAME = "service_vac_gauge_controllers"
    DEVICE_LABEL = "gauge controllers"
    PUB_ENDPOINT, CMD_ENDPOINT, TOPIC = ZMQ_PORT_VACUUM_PUB, ZMQ_PORT_VACUUM_CMD, TOPIC_VACUUM_DATA
    POLL_INTERVAL = 0.05
    RECONNECT_DELAYS = (1.0, 2.0, 5.0)
    SUBSYSTEMS = ("ion_beam.vacuum",)
    REGISTRY_SOURCE = "service_vacuum"

    def __init__(self):
        super().__init__()
        self.address = f"{NOISY_RACK_WAVESHARE_IP}:{NOISY_RACK_WAVESHARE_PORT}"
        self.link = _make_link()
        self.config = self.load_config("vac_gauges_config.json", {})
        self.tag_map = dict(TAG_MAP)
        self.rise_limit = float(self.config.get("system_interlocks", {}).get("rapid_rise_thresh_mb_s", 5.0e-5))
        self.pressure_history: Dict[str, Dict[str, float]] = {}
        self._last_good_read: Dict[str, float] = {}
        self.relay_to_channel_map = {}
        self.channel_to_relay_map = {}
        self.relay_thresholds = {}
        self.build_relay_maps()
        self.state.update({"system.connected": 0.0})
        for base in self.tag_map.values():
            for flag in GAUGE_FLAGS:
                self.state[f"{base}.{flag}"] = 0.0
        self.slow_tasks = [("gauge_status", ch) for ch in CHANNELS]
        for sp in RELAYS:
            self.slow_tasks += [("relay_status", sp), ("relay_on", sp), ("relay_off", sp), ("relay_ch", sp)]
        self._slow_idx = 0

    def build_relay_maps(self):
        """(node, relay) -> channel, (node, channel) -> relays, and each relay's on/off thresholds."""
        for node_str, node_data in self.config.items():
            if not node_str.isdigit():
                continue
            node = int(node_str)
            for relay_str, params in node_data.get("relays", {}).items():
                if params.get("channel") is None:
                    continue
                relay, ch = int(relay_str), int(params["channel"])
                self.relay_to_channel_map[(node, relay)] = ch
                self.channel_to_relay_map.setdefault((node, ch), []).append(relay)
                try:
                    self.relay_thresholds[(node, relay)] = {"on_val": float(params.get("on_val")),
                                                            "off_val": float(params.get("off_val"))}
                except (TypeError, ValueError):
                    pass

    # ------------------------------------------------------------------ link helpers
    def read_retry(self, node, group, param, retries=5) -> Optional[str]:
        for _ in range(retries):
            value = self.link.read(node, group, param)
            if value is not None:
                return value
            self.heartbeat()
            time.sleep(self.POLL_INTERVAL)
        return None

    def write_retry(self, node, group, param, value, retries=10) -> bool:
        for _ in range(retries):
            status = self.link.write(node, group, param, value)
            if status == "ACK":
                return True
            if status == "NACK":
                return False
            self.heartbeat()
            time.sleep(self.POLL_INTERVAL)
        return False

    # ------------------------------------------------------------------ connection
    def connect_device(self) -> bool:
        self.link.connect()
        return True

    def is_connected(self) -> bool:
        return self.link.connected

    def close_device(self):
        self.link.close()

    def after_connect(self, previous_failures: int):
        self.state["system.connected"] = 1.0
        self.events.log_general("Connected to simulated GRAPHIX controllers" if simulation_enabled()
                                else f"Connected to {self.address}")
        self.read_static_data()
        self.enforce_startup_config()

    def read_static_data(self):
        self.events.log_general("Reading static sensor profiles...")
        for node in NODE_IDS:
            serial = self.read_retry(node, "5", str(PARAM_SERIAL), 50)
            if serial:
                self.state[f"ion_beam.vacuum.controller_{node}.serial_number"] = serial
            for ch in CHANNELS:
                name = self.read_retry(node, str(ch), str(PARAM_NAME), 50)
                sensor = self.read_retry(node, str(ch), str(PARAM_TYPE), 50)
                base = self.tag_map.get((node, ch))
                if base:
                    if name:
                        self.state[f"{base}.name"] = name
                    if sensor:
                        self.state[f"{base}.sensor_type"] = sensor

    def enforce_startup_config(self):
        """Gauge names and relay set points as in vac_gauges_config.json (written only if they differ)."""
        if not self.config:
            self.events.log_general("WARNING: No config loaded. Skipping enforcement.")
            return
        try:
            for node_str, node_data in self.config.items():
                if not node_str.isdigit() or int(node_str) not in NODE_IDS:
                    continue
                node = int(node_str)
                for ch_str, ch_params in node_data.get("channels", {}).items():
                    target = str(ch_params.get("name", ""))[:10].strip()
                    if target and self.read_retry(node, ch_str, "5", 3) != target:
                        self.events.log_general(f"-> Node {node} Ch {ch_str}: Name mismatch. Updating to '{target}'...")
                        self.write_retry(node, ch_str, "5", target)
                        time.sleep(0.2)
                for relay_str, params in node_data.get("relays", {}).items():
                    self.enforce_relay(node, int(relay_str), params)
                    self.heartbeat()
            self.events.log_general("--- VERIFICATION COMPLETE ---")
        except Exception as e:   # carry on with whatever the controllers have; the cause goes to the log
            self.log.exception("Failed to enforce startup config")
            self.events.log_general(f"Failed to enforce startup config: {e}", severity="ERROR")

    def enforce_relay(self, node, relay, params):
        if relay not in RELAY_PARAMS:
            return
        channel, on_val, off_val = params.get("channel"), params.get("on_val"), params.get("off_val")
        if None in (channel, on_val, off_val):
            return
        p = RELAY_PARAMS[relay]
        target_ch, target_on, target_off = str(int(channel)), f"{float(on_val):.1E}", f"{float(off_val):.1E}"
        curr_ch = self.read_retry(node, "4", p["ch"], 3)
        curr_on = self.read_retry(node, "4", p["on"], 3)
        curr_off = self.read_retry(node, "4", p["off"], 3)
        if curr_ch == target_ch and curr_on is not None and curr_off is not None:
            try:
                if abs(float(curr_on) - float(on_val)) < 1e-12 and abs(float(curr_off) - float(off_val)) < 1e-12:
                    return
            except ValueError:
                pass
        self.events.log_general(f"-> Node {node} Relay {relay}: Mismatch found! Overwriting...")
        ok_ch = self.write_retry(node, "4", p["ch"], target_ch)
        time.sleep(0.2)
        ok_first = ok_second = False
        if ok_ch:
            # Keep on < off at every step: write whichever end moves away from the other first
            try:
                current_off = float(curr_off) if curr_off else 1000.0
            except ValueError:          # unreadable answer: assume the widest (write 'on' first)
                current_off = 1000.0
            first, second = ((p["on"], target_on), (p["off"], target_off)) if float(on_val) < current_off \
                else ((p["off"], target_off), (p["on"], target_on))
            ok_first = self.write_retry(node, "4", first[0], first[1])
            time.sleep(0.2)
            if ok_first:
                ok_second = self.write_retry(node, "4", second[0], second[1])
            time.sleep(0.2)
        if not (ok_ch and ok_first and ok_second):
            self.events.log_general(f"Node {node} relay {relay}: set points NOT fully applied (the controller "
                                    f"refused a write) - check channel {target_ch}, on {target_on}, off "
                                    f"{target_off} on the controller", severity="ERROR")

    # ------------------------------------------------------------------ commands
    def handle_command(self, cmd, subsystem: str, leaf: str):
        parts = cmd.tag.split('.')
        if len(parts) < 4 or "controller_" not in parts[2] or "relay_" not in parts[3]:
            cmd.reject("unsupported vacuum command")
            return
        value = self.number(cmd)
        if value is None:
            return
        if cmd.tag in self.registry_limits and not self.within_limits(cmd, value):
            return
        if not self.link.connected:
            cmd.reject("gauge controllers not connected")
            return
        try:
            node = int(parts[2].replace("controller_", ""))
            _, relay, kind = parts[3].split('_')[:3]
            relay = int(relay)
            param = RELAY_PARAMS[relay][kind]
        except (ValueError, IndexError, KeyError):
            cmd.reject("cannot parse controller/relay/parameter from tag")
            return
        text = f"{value:.1E}"
        if self.write_retry(node, "4", param, text):
            self.events.log_general(f"Executed Relay Command: Node {node}, Relay {relay} {kind.upper()} -> {text}")
            cmd.ok(f"relay {relay} {kind} set to {text}")
        else:
            cmd.fail("controller did not acknowledge the write")

    # ------------------------------------------------------------------ telemetry
    def publish_unknown(self):
        """Controllers unreachable: say so (and keep heartbeating). The PLC still gets graphix
        comms-fail and no gate-valve permissive."""
        for base in self.tag_map.values():
            self.state[f"{base}.rb_pressure"] = None
            self.state[f"{base}.stat_comms_fail"] = 1.0
        self.state["ion_beam.facilities.graphix1.stat_comms_fail"] = 1.0
        self.state["ion_beam.facilities.graphix2.stat_comms_fail"] = 1.0
        self.state["ion_beam.vacuum.gv_permissive_ready"] = 0.0
        self.state["system.connected"] = 0.0

    def poll_device(self):
        task, target = self.slow_tasks[self._slow_idx]
        for node in NODE_IDS:
            for ch in CHANNELS:
                self.read_pressure(node, ch)
            self.read_slow_item(node, task, target)
        self._slow_idx = (self._slow_idx + 1) % len(self.slow_tasks)
        fault = 0.0 if self.link.connected else 1.0
        self.state["ion_beam.facilities.graphix1.stat_comms_fail"] = fault
        self.state["ion_beam.facilities.graphix2.stat_comms_fail"] = fault
        self.state["system.connected"] = 1.0 - fault
        self.evaluate_gv_permissive()

    def read_pressure(self, node, ch):
        base = self.tag_map.get((node, ch))
        raw = self.link.read(node, str(ch), str(PARAM_PRESSURE))
        if base is None:
            return
        now = time.time()
        if raw is None:
            # Occasional garbled reads are normal on this bus; only after a few seconds without a
            # good reading is the last pressure withdrawn (published as unknown).
            self.state[f"{base}.stat_comms_fail"] = 1.0
            if now - self._last_good_read.get(base, 0.0) > PRESSURE_STALE_S:
                self.state[f"{base}.rb_pressure"] = None
            return
        try:
            p = float(raw)
        except ValueError:
            self.log.debug("Unparseable pressure from node %s ch %s: %r", node, ch, raw)
            return
        self.state[f"{base}.rb_pressure"] = p
        approaching = False
        for relay in self.channel_to_relay_map.get((node, ch), []):
            th = self.relay_thresholds.get((node, relay))
            if th and th["on_val"] < p <= th["on_val"] / 0.8:
                approaching = True
                break
        self.state[f"{base}.stat_approaching_sp"] = 1.0 if approaching else 0.0
        rapid = False
        hist = self.pressure_history.get(base)
        if hist is None:
            self.pressure_history[base] = {"pressure": p, "time": now}
        elif now - hist["time"] > 0.5:
            dp_dt = (p - hist["pressure"]) / (now - hist["time"])
            rapid = dp_dt > self.rise_limit and p > 1.0e-6 and "vacuum_gauge_4" not in base
            self.pressure_history[base] = {"pressure": p, "time": now}
        self.state[f"{base}.stat_rapid_rise"] = 1.0 if rapid else 0.0
        self.state[f"{base}.stat_comms_fail"] = 0.0
        self._last_good_read[base] = now

    def read_slow_item(self, node, task, target):
        if task == "gauge_status":
            val = self.link.read(node, str(target), str(PARAM_STATUS))
            base = self.tag_map.get((node, target))
            if not val or not base:
                return
            try:
                code = 0 if val.strip().upper() == "OK" else int(val)
            except ValueError:
                self.log.debug("Unparseable gauge status from node %s: %r", node, val)
                return
            self.state[f"{base}.stat_error_code"] = float(code)
            self.state[f"{base}.stat_not_found"] = 1.0 if code == 5 else 0.0
            self.state[f"{base}.stat_mismatch"] = 1.0 if code not in (0, 5) else 0.0
        elif task == "relay_status":
            val = self.link.read(node, "4", RELAY_PARAMS[target]["status"])
            if not val:
                return
            try:
                status = int(val)
            except ValueError:
                self.log.debug("Unparseable relay status from node %s: %r", node, val)
                return
            self.state[f"ion_beam.vacuum.controller_{node}.relay_{target}_status"] = float(status)
            ch = self.relay_to_channel_map.get((node, target))
            base = self.tag_map.get((node, ch)) if ch else None
            if base:
                self.state[f"{base}.stat_relay_active"] = 1.0 if status == 1 else 0.0
                self.state[f"{base}.stat_above_sp"] = 1.0 if status == 0 else 0.0
        else:   # relay_on / relay_off / relay_ch
            kind = task.split("_")[1]
            val = self.link.read(node, "4", RELAY_PARAMS[target][kind])
            if val:
                try:
                    self.state[f"ion_beam.vacuum.controller_{node}.relay_{target}_{kind}_sp"] = float(val)
                except ValueError:
                    self.log.debug("Unparseable relay set point from node %s: %r", node, val)

    def evaluate_gv_permissive(self):
        def faulty(base):
            return not base or any(self.state.get(f"{base}.stat_{f}", 0.0) == 1.0
                                   for f in ("not_found", "mismatch", "above_sp", "rapid_rise"))
        safe = self.link.connected and not faulty(self.tag_map.get((10, 1))) and not faulty(self.tag_map.get((10, 2)))
        self.state["ion_beam.vacuum.gv_permissive_ready"] = 1.0 if safe else 0.0

    def run(self):
        self.events.log_general("Daemon starting" + (" (SIMULATED CONTROLLERS)" if simulation_enabled() else ""))
        super().run()


if __name__ == "__main__":
    setup_logging("service_vac_gauge_controllers")
    harden_windows_process()
    VacuumMicroservice().run()
