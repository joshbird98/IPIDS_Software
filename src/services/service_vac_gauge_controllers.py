"""Vacuum gauges: two Leybold GRAPHIX controllers (nodes 10 and 20, three gauges each) on the
noisy rack's RS485 bus.

Every cycle all six pressures are read; one slower item (gauge status, relay status / set
points) is read round-robin. Each gauge gets rapid-rise and approaching-set-point flags, and
the gate-valve permissive (VG1 and VG2 healthy and below their set points) goes to the PLC
service. On connect the controllers' gauge names and relay set points are checked against
vac_gauges_config.json and corrected if they differ.

The permissive is only given while VG1's and VG2's relays - what it is built from - are
confirmed to hold the configured channel and set points: read back after the start-up check
(an acknowledged write isn't proof) and in the round-robin from then on, so a set point the
controller refused, or one changed on its front panel or lost in a reset, holds it back. The
replies aren't checked by the driver, so a deliberate read needs two agreeing answers and a
routine one only changes a known value after three in a row. A config that gives VG1 or VG2
no relay holds it back too (the permissive couldn't judge that gauge). Any relay that differs
raises stat_relay_sp_mismatch (an alarm) and an ERROR event. A relay set point command (on /
off only, within the registry limits, on below off) changes what is expected until the
service restarts and writes the config back.
"""
import time
from typing import Dict, Optional

from src.core import hard_caps
from src.core.device_service import DeviceService
from src.core.log_setup import setup_logging
from src.core.os_helper import harden_windows_process
from src.core.paths import simulation_enabled
from src.core.network_map import (ZMQ_PORT_VACUUM_PUB, ZMQ_PORT_VACUUM_CMD, TOPIC_VACUUM_DATA,
                                  NOISY_RACK_WAVESHARE_IP, NOISY_RACK_WAVESHARE_PORT)
from src.drivers.graphix import PARAM_PRESSURE, GraphixLink

PRESSURE_STALE_S = 3.0   # a gauge with no good reading for this long is published as unknown
GV_STALE_S = 10.0        # ...and VG1 / VG2 without one for this long lose the gate-valve permissive (it used to
                         # keep its last value; longer than the display's limit, as a drop can close the valve)

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
GV_GAUGES = ((10, 1), (10, 2))  # VG1 and VG2: the gate-valve permissive is built from their relays
RELAY_KINDS = ("ch", "on", "off")
RELAY_KIND_NAMES = {"ch": "channel", "on": "on", "off": "off"}
RELAY_REREADS = 3   # a relay value that is known only changes when this many routine reads in a row give the
                    # same other value (~25 s): a garbled or stale answer on this noisy bus (the driver can't
                    # check replies) must neither drop the gate-valve permissive nor give it
RELAY_TRUSTED_TRIES = 4     # a deliberate read needs two agreeing answers in this many tries
MISMATCH_TAG = "ion_beam.vacuum.stat_relay_sp_mismatch"
GRAPHIX = {10: "graphix1", 20: "graphix2"}     # the registry's names for the controllers (relay limits)


def relay_value(kind: str, raw) -> Optional[str]:
    """A relay's channel ('1') or set point as the controller holds it ('5.5E-06', two significant
    digits); None if unreadable."""
    try:
        return str(int(float(raw))) if kind == "ch" else f"{float(raw):.1E}"
    except (TypeError, ValueError, OverflowError):
        return None


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
        self.relay_configured = {}  # (node, relay) -> {"ch", "on", "off"} as configured, in the controller's format
        self.relay_expected = {}    # the same, changed by relay set point commands (until a restart)
        self.relay_seen = {}        # (node, relay) -> the same as last read from the controller (None: not read)
        self._relay_differs = {}    # (node, relay, kind) -> (value, routine reads in a row giving it)
        self._relay_bad = {}        # (node, relay) -> reported as not matching (for the events)
        self._relays_checked = False    # the start-up check is done (unconfirmed relays alarm from then on)
        self.relay_cap_problems = {}    # (node, relay) -> why its config values are refused (hard caps)
        self.silent_nodes = set()       # controllers that answered nothing at connect (not checked then)
        self.build_relay_maps()
        self.gv_config_problems = self._find_gv_config_problems()
        self._gv_problems_reported = False
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
                expected = {"ch": relay_value("ch", ch), "on": relay_value("on", params.get("on_val")),
                            "off": relay_value("off", params.get("off_val"))}
                # The cap is judged on what would be written: a hand-edited file may hold "0.7" (text),
                # which the editor refuses as not a number but float() here would turn into 0.7
                cap = hard_caps.relay_cap(node, ch)
                over = [k for k in ("on", "off") if expected[k] is not None and cap.breach(float(expected[k]))]
                if over:            # a file edited past the editor: never written to the controller
                    self.relay_cap_problems[(node, relay)] = cap.explain(
                        f"vac_gauges_config.json node {node} relay {relay} {over[0]} point", float(expected[over[0]]))
                elif node in NODE_IDS and relay in RELAY_PARAMS and None not in expected.values():
                    self.relay_expected[(node, relay)] = expected
                    self.relay_configured[(node, relay)] = dict(expected)

    # ------------------------------------------------------------------ link helpers
    # The start-up check makes many reads in a row: every attempt heartbeats (rate-limited), or
    # the manager would take the service for hung (2 s) while it checks the controllers.
    def read_retry(self, node, group, param, retries=5) -> Optional[str]:
        for _ in range(retries):
            self.heartbeat()
            value = self.link.read(node, group, param)
            if value is not None:
                return value
            time.sleep(self.POLL_INTERVAL)
        return None

    def write_retry(self, node, group, param, value, retries=10) -> bool:
        for _ in range(retries):
            self.heartbeat()
            status = self.link.write(node, group, param, value)
            if status == "ACK":
                return True
            if status == "NACK":
                return False
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
        self.relay_seen, self._relay_differs = {}, {}      # read afresh (the controllers may have been reset)
        self._relays_checked = False
        self.read_static_data()
        self.enforce_startup_config()

    def read_static_data(self):
        """Serial number, gauge names and types. A controller that answers none of it is left out
        of the start-up check (silent_nodes): writing to one that doesn't answer drops the link
        (the write times out), which re-ran the whole connect - in a loop, with VG1/VG2 unread,
        for as long as e.g. node 20 was switched off."""
        self.events.log_general("Reading static sensor profiles...")
        self.silent_nodes = set()
        for node in NODE_IDS:
            serial = self.read_retry(node, "5", str(PARAM_SERIAL), 10)
            if serial:
                self.state[f"ion_beam.vacuum.controller_{node}.serial_number"] = serial
            answered = bool(serial)
            for ch in CHANNELS:
                name = self.read_retry(node, str(ch), str(PARAM_NAME), 10 if answered else 2)
                sensor = self.read_retry(node, str(ch), str(PARAM_TYPE), 10 if answered else 2)
                answered = answered or bool(name or sensor)
                base = self.tag_map.get((node, ch))
                if base:
                    if name:
                        self.state[f"{base}.name"] = name
                    if sensor:
                        self.state[f"{base}.sensor_type"] = sensor
            if not answered:
                self.silent_nodes.add(node)
                self.events.log_general(f"Gauge controller node {node} doesn't answer: its names and relays aren't "
                                        "checked until the service reconnects (its relay set points are read in "
                                        "the meantime if it comes back)", severity="ERROR")

    def enforce_startup_config(self):
        """Gauge names and relay set points as in vac_gauges_config.json (written only if they differ)."""
        if not self._gv_problems_reported:
            self._gv_problems_reported = True
            if self.gv_config_problems:
                self.events.log_general("Gate-valve permissive withheld: it judges VG1 and VG2 by their relays, and "
                                        + "; ".join(self.gv_config_problems), severity="ERROR")
            for (node, relay), why in sorted(self.relay_cap_problems.items()):
                if not any(why in p for p in self.gv_config_problems):
                    self.events.log_general(f"Node {node} relay {relay} not set: {why}", severity="ERROR")
        if not self.config:
            self.events.log_general("WARNING: No config loaded. Skipping enforcement.")
            self._relays_checked = True
            return
        try:
            for node in NODE_IDS:
                if node not in self.silent_nodes:
                    self.check_node(node)
            self.events.log_general("--- VERIFICATION COMPLETE ---")
        except Exception as e:   # carry on with whatever the controllers have; the cause goes to the log
            self.log.exception("Failed to enforce startup config")
            self.events.log_general(f"Failed to enforce startup config: {e}", severity="ERROR")
        finally:
            self._relays_checked = True     # from now on, a relay still unconfirmed is an alarm

    def check_node(self, node):
        """One controller's gauge names and relays as in the config (written only if they differ)."""
        node_data = self.config.get(str(node), {})
        for ch_str, ch_params in node_data.get("channels", {}).items():
            target = str(ch_params.get("name", ""))[:10].strip()
            if target and self.read_retry(node, ch_str, "5", 3) != target:
                self.events.log_general(f"-> Node {node} Ch {ch_str}: Name mismatch. Updating to '{target}'...")
                self.write_retry(node, ch_str, "5", target)
                time.sleep(0.2)
        for relay_str, params in node_data.get("relays", {}).items():
            self.enforce_relay(node, int(relay_str), params)
            self.heartbeat()

    def recheck_returning_nodes(self, answered):
        """A controller silent at connect that answers now gets the start-up check it missed (its
        relays would otherwise only be known from routine reads, and never corrected)."""
        for node in sorted(self.silent_nodes & answered):
            self.silent_nodes.discard(node)
            self.events.log_general(f"Gauge controller node {node} answers again: checking its names and relay "
                                    "set points")
            try:
                self.check_node(node)
            except Exception as e:   # as at start-up: carry on, the cause goes to the log
                self.log.exception("Check of node %s failed", node)
                self.events.log_general(f"Check of gauge controller node {node} failed: {e}", severity="ERROR")

    def enforce_relay(self, node, relay, params=None):
        """Make the controller's relay match the config (written only if it differs), then read it
        back: what the controller holds afterwards - not whether it acknowledged the writes -
        decides whether the gate-valve permissive may be given."""
        exp = self.relay_expected.get((node, relay))
        if exp is None:         # not a relay of these controllers, or channel / on / off missing in the config
            return
        p = RELAY_PARAMS[relay]
        held = {kind: self.read_relay_value(node, relay, kind) for kind in RELAY_KINDS}
        self.relay_seen[(node, relay)] = dict.fromkeys(RELAY_KINDS)
        for kind in RELAY_KINDS:
            self.relay_reading(node, relay, kind, held[kind], trusted=True)
        if self.relay_state(node, relay) == "match":
            self.review_relay(node, relay)
            return
        if not any(held.values()):      # no answer at all: a write would only time out (and drop the link)
            self.review_relay(node, relay, after_write=True, note=" - the controller didn't answer")
            return
        self.events.log_general(f"-> Node {node} Relay {relay}: Mismatch found! Overwriting...")
        ok_ch = self.write_retry(node, "4", p["ch"], exp["ch"])
        time.sleep(0.2)
        ok_first = ok_second = False
        if ok_ch:
            # Keep on < off at every step: write whichever end moves away from the other first
            current_off = float(held["off"] or 1000.0)      # unreadable: assume the widest
            first, second = ((p["on"], exp["on"]), (p["off"], exp["off"])) if float(exp["on"]) < current_off \
                else ((p["off"], exp["off"]), (p["on"], exp["on"]))
            ok_first = self.write_retry(node, "4", first[0], first[1])
            time.sleep(0.2)
            if ok_first:
                ok_second = self.write_retry(node, "4", second[0], second[1])
            time.sleep(0.2)
        self.relay_seen[(node, relay)] = dict.fromkeys(RELAY_KINDS)     # only the read-back counts now
        for kind in RELAY_KINDS:
            self.relay_reading(node, relay, kind, self.read_relay_value(node, relay, kind), trusted=True)
        refused = not (ok_ch and ok_first and ok_second)
        self.review_relay(node, relay, after_write=True,
                          note=" - the controller refused a write" if refused
                          else " - the controller acknowledged the writes but holds these")

    # ------------------------------------------------------------------ relay set point checks
    def read_relay_value(self, node, relay, kind) -> Optional[str]:
        """A deliberate read of a relay's channel / on / off: two answers in a row that agree, or
        None - one garbled (or stale) answer on this bus mustn't count as what the controller holds."""
        last = None
        for _ in range(RELAY_TRUSTED_TRIES):
            value = relay_value(kind, self.read_retry(node, "4", RELAY_PARAMS[relay][kind], 3))
            if value is not None and value == last:
                return value
            last = value if value is not None else last
        return None

    def relay_reading(self, node, relay, kind, raw, trusted=False):
        """One read of a relay's channel / on / off. trusted: a deliberate read-back (see
        read_relay_value) is taken as it is; a routine read only changes what is known - either
        way, to or from what is expected, and from unknown too - once RELAY_REREADS of them in a
        row give the same value."""
        exp = self.relay_expected.get((node, relay))
        value = relay_value(kind, raw)
        if exp is None or value is None:
            return
        seen = self.relay_seen.setdefault((node, relay), dict.fromkeys(RELAY_KINDS))
        key = (node, relay, kind)
        if trusted or value == seen[kind]:
            self._relay_differs.pop(key, None)
            seen[kind] = value
            return
        last, n = self._relay_differs.get(key, (None, 0))
        n = n + 1 if value == last else 1
        if n < RELAY_REREADS:
            self._relay_differs[key] = (value, n)
        else:
            self._relay_differs.pop(key, None)
            seen[kind] = value

    def relay_state(self, node, relay) -> Optional[str]:
        """'match', 'differs', or None while not every value has been read."""
        seen = self.relay_seen.get((node, relay))
        if not seen or None in seen.values():
            return None
        return "match" if seen == self.relay_expected[(node, relay)] else "differs"

    def feeds_gv(self, node, relay) -> bool:
        return (node, int(self.relay_expected[(node, relay)]["ch"])) in GV_GAUGES

    def relays_of(self, gauge):
        """The relays vac_gauges_config.json gives a gauge (node, channel)."""
        return sorted(r for (n, r), ch in self.relay_to_channel_map.items() if (n, ch) == gauge)

    def _find_gv_config_problems(self) -> list:
        problems = []
        for gauge in GV_GAUGES:
            name = TAG_MAP[gauge].rsplit(".", 1)[-1].replace("vacuum_gauge_", "VG")
            relays = self.relays_of(gauge)
            if not relays:
                problems.append(f"{name} has no relay in vac_gauges_config.json")
            problems += [self.relay_cap_problems.get((gauge[0], r)) or
                         f"{name}'s relay {r} has no valid channel / on / off point in vac_gauges_config.json"
                         for r in relays if (gauge[0], r) not in self.relay_expected]
        return problems

    def gv_relays_confirmed(self) -> bool:
        """Every relay the config gives VG1 and VG2 holds what is expected - and each has at least
        one: without one the permissive couldn't judge that gauge's pressure."""
        return not self.gv_config_problems and all(
            self.relay_state(gauge[0], r) == "match" for gauge in GV_GAUGES for r in self.relays_of(gauge))

    def review_relay(self, node, relay, after_write=False, note=""):
        """An ERROR event when a relay stops matching the config (or, after a write, can't be read
        back), and an INFO one when it matches again."""
        state = self.relay_state(node, relay)
        if state is None and not after_write:
            return
        bad = state != "match"
        if bad == self._relay_bad.get((node, relay), False):
            return
        self._relay_bad[(node, relay)] = bad
        gauge = self.tag_map.get((node, int(self.relay_expected[(node, relay)]["ch"])), "")
        gauge = gauge.rsplit(".", 1)[-1].replace("vacuum_gauge_", "VG")
        label = f"Node {node} relay {relay}" + (f" ({gauge})" if gauge else "")
        exp, seen = self.relay_expected[(node, relay)], self.relay_seen.get((node, relay)) or {}
        source = ("vac_gauges_config.json" if exp == self.relay_configured.get((node, relay))
                  else "what was last set (a set point command)")
        if not bad:
            self.events.log_general(f"{label}: set points match {source} again")
            return
        if state is None:
            what = "could not be read back"
        else:
            what = f"differ from {source}: " + ", ".join(
                f"{RELAY_KIND_NAMES[k]} {seen[k]} (should be {exp[k]})" for k in RELAY_KINDS if seen[k] != exp[k])
        held = " - gate-valve permissive withheld until they match" if self.feeds_gv(node, relay) else ""
        self.events.log_general(f"{label}: set points {what}{note}{held}", severity="ERROR")

    # ------------------------------------------------------------------ commands
    def handle_command(self, cmd, subsystem: str, leaf: str):
        """ion_beam.vacuum.controller_<node>.relay_<n>_on / _off: a relay set point, within its
        gauge's hard cap (src/core/hard_caps.py), the registry limits of its graphix tag, and with
        on below off. Channels come from the config."""
        parts = cmd.tag.split('.')
        if len(parts) < 4 or "controller_" not in parts[2] or "relay_" not in parts[3]:
            cmd.reject("unsupported vacuum command")
            return
        value = self.number(cmd)
        if value is None:
            return
        try:
            node = int(parts[2].replace("controller_", ""))
            _, relay, kind = parts[3].split('_')[:3]
            relay = int(relay)
            param = RELAY_PARAMS[relay][kind]
        except (ValueError, IndexError, KeyError):
            cmd.reject("cannot parse controller/relay/parameter from tag")
            return
        if kind not in ("on", "off"):
            cmd.reject("only a relay's on / off set points can be set here (its channel comes from "
                       "vac_gauges_config.json)")
            return
        exp = self.relay_expected.get((node, relay))
        cap = hard_caps.relay_cap(node, exp["ch"] if exp else self.relay_to_channel_map.get((node, relay)))
        if cap.breach(value):       # first, so the reason given is the cap's
            cmd.reject(cap.explain(f"node {node} relay {relay} {kind} point", value))
            return
        lim = self.registry_limits.get(f"ion_beam.facilities.{GRAPHIX.get(node)}.relay_{relay}_{kind}") \
            or self.registry_limits.get(cmd.tag) or {}
        lo, hi = lim.get("min_val"), lim.get("max_val")
        if lo is None or hi is None:
            cmd.reject("no limits for this relay set point in system_tags.json")
            return
        if not lo <= value <= hi:
            cmd.reject(f"{value:.1E} mbar is outside {lo:g}..{hi:g} mbar")
            return
        text = f"{value:.1E}"
        if exp is not None:
            on, off = (text, exp["off"]) if kind == "on" else (exp["on"], text)
            if not float(on) < float(off):
                cmd.reject(f"the on point must stay below the off point (on {on}, off {off} mbar)")
                return
        if not self.link.connected:
            cmd.reject("gauge controllers not connected")
            return
        if not self.write_retry(node, "4", param, text):
            cmd.fail("controller did not acknowledge the write")
            return
        note = ""
        if exp is not None:
            # A deliberate change: the set point check expects it from now on (until a restart
            # writes the config back). For a relay that matched, the acknowledgement is taken - one
            # garbled read-back mustn't drop the permissive - and the routine reads check it from
            # here on; one that didn't match is only confirmed by what is read back.
            was_ok = self.relay_state(node, relay) == "match"
            exp[kind] = text
            seen = self.relay_seen.setdefault((node, relay), dict.fromkeys(RELAY_KINDS))
            self._relay_differs.pop((node, relay, kind), None)
            if was_ok:
                seen[kind] = text
            else:
                seen[kind] = None
                self.relay_reading(node, relay, kind, self.read_relay_value(node, relay, kind), trusted=True)
            self.review_relay(node, relay, after_write=not was_ok)
            configured = self.relay_configured[(node, relay)][kind]
            if text != configured:
                note = f" (vac_gauges_config.json says {configured}: the service writes that back when it restarts)"
        self.events.log_general(f"Executed Relay Command: Node {node}, Relay {relay} {kind.upper()} -> {text}" + note)
        cmd.ok(f"relay {relay} {kind} set to {text}")

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
        self.state[MISMATCH_TAG] = None
        self.state["system.connected"] = 0.0

    def poll_device(self):
        task, target = self.slow_tasks[self._slow_idx]
        answered = set()
        for node in NODE_IDS:
            for ch in CHANNELS:
                if self.read_pressure(node, ch):
                    answered.add(node)
            self.read_slow_item(node, task, target)
        self._slow_idx = (self._slow_idx + 1) % len(self.slow_tasks)
        if self.silent_nodes & answered:
            self.recheck_returning_nodes(answered)
        fault = 0.0 if self.link.connected else 1.0
        self.state["ion_beam.facilities.graphix1.stat_comms_fail"] = fault
        self.state["ion_beam.facilities.graphix2.stat_comms_fail"] = fault
        self.state["system.connected"] = 1.0 - fault
        self.evaluate_gv_permissive()

    def read_pressure(self, node, ch) -> bool:
        """True if the controller answered. (Times here are monotonic: a clock step mustn't make a
        gauge look stale - that can drop the gate-valve permissive - or rising.)"""
        base = self.tag_map.get((node, ch))
        self.heartbeat()            # with both controllers silent a poll takes ~2.6 s of timeouts
        raw = self.link.read(node, str(ch), str(PARAM_PRESSURE))
        if base is None:
            return raw is not None
        now = time.monotonic()
        if raw is None:
            # Occasional garbled reads are normal on this bus; only after a few seconds without a
            # good reading is the last pressure withdrawn (published as unknown).
            self.state[f"{base}.stat_comms_fail"] = 1.0
            if now - self._last_good_read.get(base, -1e9) > PRESSURE_STALE_S:
                self.state[f"{base}.rb_pressure"] = None
            return False
        try:
            p = float(raw)
        except ValueError:
            self.log.debug("Unparseable pressure from node %s ch %s: %r", node, ch, raw)
            return True
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
        return True

    def read_slow_item(self, node, task, target):
        self.heartbeat()
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
                if (node, target) in self.relay_expected:
                    self.relay_reading(node, target, kind, val)
                    self.review_relay(node, target)

    def evaluate_gv_permissive(self):
        now = time.monotonic()

        def faulty(base):     # (no good reading for GV_STALE_S counts too - one garbled answer doesn't)
            return not base or now - self._last_good_read.get(base, -1e9) > GV_STALE_S or any(
                self.state.get(f"{base}.stat_{f}", 0.0) == 1.0 for f in ("not_found", "mismatch", "above_sp",
                                                                          "rapid_rise"))
        safe = (self.link.connected and not faulty(self.tag_map.get((10, 1))) and not faulty(self.tag_map.get((10, 2)))
                and self.gv_relays_confirmed())
        self.state["ion_beam.vacuum.gv_permissive_ready"] = 1.0 if safe else 0.0
        states = [self.relay_state(n, r) for n, r in self.relay_expected]
        if self.gv_config_problems or self.relay_cap_problems or "differs" in states or \
                (self._relays_checked and None in states):
            self.state[MISMATCH_TAG] = 1.0      # differs, or still unread after the start-up check: an alarm
        else:
            self.state[MISMATCH_TAG] = None if None in states else 0.0

    def run(self):
        self.events.log_general("Daemon starting" + (" (SIMULATED CONTROLLERS)" if simulation_enabled() else ""))
        super().run()


if __name__ == "__main__":
    setup_logging("service_vac_gauge_controllers")
    harden_windows_process()
    VacuumMicroservice().run()
