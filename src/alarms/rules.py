"""Which alarms exist, and when each one is active, from the live telemetry.

config/alarm_config.json:

    "rules": [
      {"kind": "plc_faults"}       one alarm per PLC fault bit in fault_config.json:
                                   plc.<Name>, active while faults.<Name> is 1 (severity 1 -> high,
                                   2 -> medium); in the first-out group "plc"
      {"kind": "flag", "match": "*.stat_comms_fail", "exclude": [...], "id": "comms.{path}",
       "message": "{device}: ...", "priority": 3, "active_when": 1.0}
                                   one alarm per telemetry tag matching the pattern ({path}: the tag
                                   without "ion_beam." and its last part; {device}: the same, readable);
                                   "tag" instead of "match" for one tag
      {"kind": "stale", "topic": "PLC_DATA", "id": "comms.plc", "message": "..."}
                                   a service has stopped publishing
      {"kind": "services"}         a service the manager runs is not running
      {"kind": "sequencer"}        the last recipe / mass scan / optimiser run failed
    ]
    common keys: priority (1 critical, 2 high, 3 medium, 4 low), delay_on / delay_off (seconds),
                 group, suppressed_by (alarm-id patterns: while one of them is active this alarm
                 is listed but not annunciated - e.g. every gauge's comms while the gauge
                 controller's comms are down)
    "overrides": {"<alarm id or pattern>": {"priority": 1, "enabled": false, "delay_on": 5, ...}}
    "first_out_groups": ["plc"]

Unknown values (a service's data gone stale) never change an alarm: the loss of the data is its
own alarm (stale / services rules).
"""
import fnmatch
import json
import logging
from dataclasses import dataclass, field

from src.core import paths
from src.sequencer import protocol as SEQ

log = logging.getLogger("alarms.rules")

STARTUP_GRACE_S = 10.0          # services publishing late after a restart are not 'stale' yet
SEVERITY_PRIORITY = {1: 2, 2: 3}
NOT_RUNNING = {"OFFLINE": "not running", "CRASHED": "crashed", "HANGING": "hanging (no heartbeat)",
               "BACKOFF": "waiting to be restarted", "STOPPED": "stopped by the operator"}

DEFAULT_CONFIG = {
    "first_out_groups": ["plc"],
    "shelve_max_minutes": 480,
    "rules": [
        {"kind": "plc_faults"},
        {"kind": "stale", "topic": "PLC_DATA", "id": "comms.plc_service", "priority": 1, "delay_on": 3,
         "message": "No data from the PLC service - machine state unknown"},
        {"kind": "flag", "tag": "PLC_DATA/system.connected", "active_when": 0.0, "id": "comms.plc", "priority": 1,
         "delay_on": 3, "message": "The PLC service has lost the PLC"},
        {"kind": "stale", "topic": "MANAGER", "id": "comms.service_manager", "priority": 2, "delay_on": 5,
         "message": "No data from the service manager"},
        {"kind": "services", "priority": 2, "delay_on": 10},
        # A gauge's comms follow its controller's: listed, but only the controller's alarm sounds
        {"kind": "flag", "match": "*.vacuum_gauge_*.stat_comms_fail", "id": "comms.{path}", "priority": 3,
         "delay_on": 5, "message": "{device}: communication lost", "suppressed_by": ["comms.facilities.graphix*"]},
        {"kind": "flag", "match": "*.stat_comms_fail", "exclude": ["*.vacuum_gauge_*"], "id": "comms.{path}",
         "priority": 3, "delay_on": 5, "message": "{device}: communication lost"},
        {"kind": "flag", "match": "*.stat_above_sp", "id": "vacuum.{path}.above_sp", "priority": 2,
         "message": "{device}: pressure above its set point"},
        {"kind": "flag", "match": "*.stat_approaching_sp", "id": "vacuum.{path}.approaching_sp", "priority": 3,
         "delay_on": 2, "message": "{device}: pressure approaching its set point"},
        {"kind": "flag", "match": "*.stat_rapid_rise", "id": "vacuum.{path}.rapid_rise", "priority": 2,
         "message": "{device}: pressure rising rapidly"},
        {"kind": "flag", "tag": "ion_beam.vacuum.stat_relay_sp_mismatch", "id": "vacuum.relay_set_points",
         "priority": 2, "message": "Gauge relay set points not confirmed against vac_gauges_config.json - see the "
                                   "event log (VG1/VG2 relays: gate valve held shut)"},
        {"kind": "flag", "match": "*.stat_arc_exceeded", "id": "hv.{path}.arc", "priority": 2,
         "message": "{device}: arc (output collapsed) - latched until fault reset"},
        {"kind": "sequencer", "priority": 3},
    ],
    "overrides": {
        "plc.Safety_Relay_Tripped": {"priority": 1},
        "plc.PC_Watchdog_Timeout": {"priority": 1},
        # The PLC flags a service's data as lost after 2 s: while the services start up that is
        # normal for a few seconds, so only alarm if it lasts
        "plc.*_Comms_Fail": {"delay_on": 10},
    },
}


@dataclass
class AlarmDef:
    id: str
    message: str
    priority: int
    condition: object               # True / False / None (unknown)
    group: str = ""
    delay_on: float = 0.0
    delay_off: float = 0.0
    suppressed_by: list = field(default_factory=list)


def short_path(tag_prefix: str) -> str:
    """'ion_beam.source.vacuum_gauge_1' -> 'source.vacuum_gauge_1'."""
    return tag_prefix[len("ion_beam."):] if tag_prefix.startswith("ion_beam.") else tag_prefix


def device_name(tag_prefix: str) -> str:
    """'ion_beam.source.vacuum_gauge_1' -> 'source vacuum gauge 1'."""
    return short_path(tag_prefix).replace(".", " ").replace("_", " ")


def load_config(path: str = None) -> dict:
    """The defaults, with config/alarm_config.json on top ("rules" replaces, "overrides" merges)."""
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    path = path or paths.config_path("alarm_config.json")
    try:
        with open(path, "r") as f:
            user = json.load(f)
    except FileNotFoundError:
        return config
    except (OSError, ValueError) as e:
        log.error("%s not loaded (%s) - using the built-in alarm rules", path, e)
        return config
    for key, value in user.items():
        if key == "overrides":
            config["overrides"].update(value)
        elif not key.startswith("_"):
            config[key] = value
    return config


def load_fault_config(path: str = None) -> dict:
    path = path or paths.config_path("fault_config.json")
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        log.error("fault_config.json not loaded (%s): no PLC fault alarms", e)
        return {}


RULE_KINDS = frozenset({"plc_faults", "flag", "stale", "services", "sequencer"})


class AlarmRules:
    def __init__(self, config: dict, fault_config: dict, started: float):
        self.config = config
        self.fault_bits = [(bits["name"], bits.get("severity", 1), bits.get("description", bits["name"]))
                           for word in fault_config.values() for bits in word.get("bits", {}).values()]
        self.started = started

    def evaluate(self, cache, now: float) -> list:
        """Every alarm the rules define now, with its condition."""
        out = []
        keys = None
        for rule in self.config.get("rules", []):
            kind = rule.get("kind")
            if kind == "plc_faults":
                out.extend(self._plc_faults(cache))
            elif kind == "flag":
                keys = keys if keys is not None else cache.keys()
                out.extend(self._flags(rule, cache, keys))
            elif kind == "stale":
                out.append(self._stale(rule, cache, now))
            elif kind == "services":
                out.extend(self._services(rule, cache))
            elif kind == "sequencer":
                out.extend(self._sequencer(rule, cache))
            else:
                log.warning("Unknown alarm rule kind %r", kind)
        return [d for d in (self._override(d) for d in out) if d is not None]

    def _override(self, d: AlarmDef):
        """Overrides whose key is a pattern ("plc.*_Comms_Fail") apply first, an exact id last."""
        overrides = self.config.get("overrides", {})
        matching = [o for k, o in overrides.items() if k != d.id and any(c in k for c in "*?[")
                    and fnmatch.fnmatchcase(d.id, k)]
        if d.id in overrides:
            matching.append(overrides[d.id])
        for o in matching:
            if o.get("enabled") is False:
                return None
            for key in ("priority", "message", "delay_on", "delay_off", "group", "suppressed_by"):
                if key in o:
                    setattr(d, key, o[key])
        return d

    @staticmethod
    def _common(rule, d: AlarmDef) -> AlarmDef:
        for key in ("priority", "delay_on", "delay_off", "group", "suppressed_by"):
            if key in rule:
                setattr(d, key, rule[key])
        return d

    def _plc_faults(self, cache):
        out = []
        for name, severity, description in self.fault_bits:
            value = cache.get_fresh(f"faults.{name}")
            out.append(AlarmDef(f"plc.{name}", f"PLC: {description}", SEVERITY_PRIORITY.get(severity, 3),
                                None if value is None else value == 1.0, group="plc"))
        return out

    def _flags(self, rule, cache, keys):
        if rule.get("tag"):
            tags = [rule["tag"]]
        else:
            exclude = rule.get("exclude", [])
            tags = [k for k in keys if "/" not in k and fnmatch.fnmatchcase(k, rule.get("match", ""))
                    and not any(fnmatch.fnmatchcase(k, x) for x in exclude)]
        out = []
        for tag in sorted(tags):
            prefix = tag.rsplit(".", 1)[0]
            names = {"device": device_name(prefix), "path": short_path(prefix), "tag": tag}
            value = cache.get_fresh(tag)
            condition = None if value is None else value == rule.get("active_when", 1.0)
            out.append(self._common(rule, AlarmDef(rule["id"].format(**names),
                                                   rule.get("message", "{device}").format(**names), 3, condition)))
        return out

    def _stale(self, rule, cache, now):
        topic = rule["topic"]
        if now - self.started < STARTUP_GRACE_S:
            condition = None
        else:
            condition = not cache.topic_fresh(topic)
        return self._common(rule, AlarmDef(rule["id"], rule.get("message", f"No data from {topic}"), 2, condition))

    def _services(self, rule, cache):
        states = cache.get_fresh("manager.services")
        if not isinstance(states, dict):
            return []
        out = []
        for service, state in states.items():
            stopped = state == "STOPPED"
            d = self._common(rule, AlarmDef(f"service.{service}", f"{service} is {NOT_RUNNING.get(state, state)}",
                                            2, state not in ("ONLINE", "STARTING")))
            if stopped:
                d.priority = 4          # on purpose: listed, not sounded
                d.message = f"{service} was stopped by the operator"
            out.append(d)
        return out

    def _sequencer(self, rule, cache):
        out = []
        for slot in SEQ.SLOTS:
            state = cache.get_fresh(SEQ.key(slot, "state"))
            if state is None:
                continue
            purpose = cache.get_fresh(SEQ.key(slot, "purpose")) or slot.replace("_", " ")
            message = cache.get_fresh(SEQ.key(slot, "message")) or "failed"
            out.append(self._common(rule, AlarmDef(f"sequencer.{slot}.failed", f"{purpose} failed: {message}", 3,
                                                   state == SEQ.FAILED)))
        return out


def suppressed(defn: AlarmDef, active_ids) -> bool:
    return any(fnmatch.fnmatchcase(a, pattern) for pattern in defn.suppressed_by for a in active_ids if a != defn.id)
