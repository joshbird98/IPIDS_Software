"""Config file checks for the HMI's config editor (used by src/core/config_store.py).

check(name, data, original) lists what a service would trip over in `data` as the new contents of
config/<name>: with `original`, the structure must not change (same keys, value types - a whole
number may become a fraction, the file's rules say where it may not); then the file's own rules in
CHECKS: limits the right way round and within physical ranges, duplicate ports / addresses /
names, gauge relay set points the controllers can store, and so on.

The rules can't know every safe value (a magnet's current limit is a decision, not a fact), so the
editor also asks for a second look at any number that changes by more than a factor of 3
(config_store.large_changes).
"""
import ipaddress
import json
import math
import re

from src.core import hard_caps
from src.core.spellman_locations import LOCATIONS as SPELLMAN_LOCATIONS

GAUGE_RANGE_MBAR = (1e-10, 1000.0)     # what the gauges can read (as the registry's command limits)


def join_path(where, key):
    return f"{where}.{key}" if where else str(key)


def short_json(v):
    text = json.dumps(v, ensure_ascii=False)
    return text if len(text) <= 60 else text[:57] + "..."


def check(name: str, data, original=None) -> list:
    """Problems with `data` as the contents of config file `name` (empty list: fine).

    With `original`, the structure must not change: same keys (device keys may be renamed),
    same value types. Then the file's own rules: see CHECKS."""
    problems = []
    if original is not None:
        problems += _shape(original, data, "")
    rule = CHECKS.get(name)
    if rule is not None and not problems:
        c = _Checker()
        try:
            rule(c, data)
        except (KeyError, TypeError, AttributeError, ValueError) as e:
            c.bad(f"unexpected layout ({type(e).__name__}: {e})")
        problems += c.problems
    problems += [v.message for v in hard_caps.violations(name, data)]
    return problems


def _kind(v):
    if isinstance(v, bool):
        return "true/false"
    if isinstance(v, int):
        return "a whole number"
    if isinstance(v, float):
        return "a number"
    if isinstance(v, str):
        return "text"
    if isinstance(v, list):
        return "a list"
    if isinstance(v, dict):
        return "an object"
    return "null"


def _shape(old, new, where) -> list:
    here = where or "(file)"
    if isinstance(old, dict):
        if not isinstance(new, dict):
            return [f"{here}: must stay {_kind(old)}"]
        out = []
        missing, extra = [k for k in old if k not in new], [k for k in new if k not in old]
        if missing:
            out.append(f"{here}: missing {', '.join(map(str, missing))}")
        if extra:
            out.append(f"{here}: unexpected {', '.join(map(str, extra))}")
        for k in old:
            if k in new:
                out += _shape(old[k], new[k], join_path(where, k))
        return out
    if isinstance(old, list):
        if not isinstance(new, list):
            return [f"{here}: must stay a list"]
        if old and new:
            out = []
            for i, b in enumerate(new):
                out += _shape(old[min(i, len(old) - 1)], b, f"{where}[{i}]")
            return out
        return []
    if old is None:
        return []
    if isinstance(old, bool):
        return [] if isinstance(new, bool) else [f"{here}: must be true or false"]
    if isinstance(old, (int, float)):       # a whole number may become a fraction (rules say where not)
        if isinstance(new, bool) or not isinstance(new, (int, float)):
            return [f"{here}: must be a number"]
        return [] if math.isfinite(new) else [f"{here}: must be a finite number"]
    if isinstance(old, str):
        return [] if isinstance(new, str) else [f"{here}: must be text"]
    return []


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


class _Checker:
    def __init__(self):
        self.problems = []

    def bad(self, text):
        self.problems.append(text)

    def num(self, obj, key, where="", gt=None, ge=None, lt=None, le=None, integer=False, optional=False):
        """obj[key] as a number within the bounds (None - and a problem - if it isn't)."""
        label = join_path(where, key)
        if not isinstance(obj, dict) or key not in obj:
            if not optional:
                self.bad(f"{label}: missing")
            return None
        v = obj[key]
        if not _is_num(v) or (integer and not isinstance(v, int)):
            self.bad(f"{label}: must be {'a whole number' if integer else 'a number'} (is {short_json(v)})")
            return None
        for bound, ok, word in ((gt, lambda b: v > b, "above"), (ge, lambda b: v >= b, "at least"),
                                (lt, lambda b: v < b, "below"), (le, lambda b: v <= b, "at most")):
            if bound is not None and not ok(bound):
                self.bad(f"{label}: must be {word} {bound:g} (is {v:g})")
                return None
        return v

    def text(self, obj, key, where="", optional=False):
        label = join_path(where, key)
        if not isinstance(obj, dict) or key not in obj:
            if not optional:
                self.bad(f"{label}: missing")
            return None
        v = obj[key]
        if not isinstance(v, str) or not v.strip():
            self.bad(f"{label}: must be non-empty text")
            return None
        return v

    def order(self, lo, hi, lo_label, hi_label, strict=True):
        if lo is not None and hi is not None and (lo >= hi if strict else lo > hi):
            self.bad(f"{lo_label} ({lo:g}) must be {'below' if strict else 'at most'} {hi_label} ({hi:g})")


def _check_magnet(c: _Checker, d):
    max_v = c.num(d, "max_voltage", gt=0)
    max_i = c.num(d, "max_current", gt=0)
    c.num(d, "max_power", gt=0)
    min_i = c.num(d, "min_current_for_calc", ge=0)
    c.order(min_i, max_i, "min_current_for_calc", "max_current")
    short = c.num(d, "short_circuit_threshold", gt=0)
    nominal = c.num(d, "nominal_resistance", gt=0)
    opened = c.num(d, "open_circuit_threshold", gt=0)
    c.num(d, "resistance_tolerance", gt=0)
    c.order(short, nominal, "short_circuit_threshold", "nominal_resistance")
    c.order(nominal, opened, "nominal_resistance", "open_circuit_threshold")
    if None not in (max_i, nominal, max_v) and max_i * nominal > max_v:
        c.bad(f"max_current x nominal_resistance ({max_i * nominal:g} V) is above max_voltage ({max_v:g} V): "
              "the supply could never reach that current")
    steps = d.get("degauss_steps_amps")
    if not isinstance(steps, list) or not all(_is_num(s) for s in steps):
        c.bad("degauss_steps_amps: must be a list of numbers")
    elif max_i is not None:
        over = [s for s in steps if abs(s) > max_i]
        if over:
            c.bad(f"degauss_steps_amps: {', '.join(f'{s:g}' for s in over)} A beyond max_current ({max_i:g} A)")
    c.num(d, "degauss_step_time_sec", gt=0)


def _check_smu(c: _Checker, d):
    lo, hi = c.num(d, "min_v"), c.num(d, "max_v")
    c.order(lo, hi, "min_v", "max_v")
    c.num(d, "compliance_current", gt=0)


def _check_plc(c: _Checker, d):
    for section, limits in d.items():
        if section.startswith("_"):
            continue
        if not isinstance(limits, dict):
            c.bad(f"{section}: must be an object of min_/max_ limits")
            continue
        for key in limits:
            if key.startswith("min_"):
                hi_key = "max_" + key[4:]
                lo = c.num(limits, key, section)
                hi = c.num(limits, hi_key, section)
                c.order(lo, hi, f"{section}.{key}", f"{section}.{hi_key}")
            elif not key.startswith("max_"):
                c.bad(f"{section}.{key}: only min_*/max_* limits belong here")
            elif "min_" + key[4:] not in limits:
                c.bad(f"{section}.{key}: has no matching min_{key[4:]}")


def _check_arc(c: _Checker, arc, where):
    c.num(arc, "collapse_fraction", where, gt=0, lt=1)
    c.num(arc, "min_setpoint_kv", where, ge=0, le=30)
    c.num(arc, "settle_s", where, ge=0, le=60)
    c.num(arc, "overcurrent_ua", where, gt=0)
    c.num(arc, "confirm_polls", where, ge=1, le=10, integer=True)     # more and an arc goes unseen


def _check_mpd(c: _Checker, d):
    if "arc_detection" in d:
        _check_arc(c, d["arc_detection"], "arc_detection")
    buses = d.get("buses")
    if not isinstance(buses, list):
        c.bad("buses: must be a list")
        return
    names = set()
    for i, bus in enumerate(buses):
        where = f"buses[{i}]"
        c.text(bus, "bus_id", where)
        devices = bus.get("devices") if isinstance(bus, dict) else None
        if not isinstance(devices, dict):
            c.bad(f"{where}.devices: must be an object")
            continue
        addresses = {}
        for dev, cfg in devices.items():
            dw = f"{where}.devices.{dev}"
            if dev in names:
                c.bad(f"{dw}: device name used twice")
            names.add(dev)
            if dev not in SPELLMAN_LOCATIONS:      # the service routes by these names
                c.bad(f"{dw}: unknown supply - the names are fixed ({', '.join(SPELLMAN_LOCATIONS)})")
            if not isinstance(cfg, dict):
                c.bad(f"{dw}: must be an object")
                continue
            addr = c.text(cfg, "address", dw)
            if addr is not None:
                if not re.fullmatch(r"[0-9A-Fa-f]{2}", addr):
                    c.bad(f"{dw}.address: must be two hex digits like '01' (is {addr!r})")
                elif addr.upper() in addresses:
                    c.bad(f"{dw}.address: {addr} is also used by {addresses[addr.upper()]} on this bus")
                else:
                    addresses[addr.upper()] = dev
            c.num(cfg, "plc_unit", dw, ge=0, integer=True, optional=True)
            lo = c.num(cfg, "min_value", dw, ge=0)
            hi = c.num(cfg, "max_value", dw, gt=0)
            c.order(lo, hi, f"{dw}.min_value", f"{dw}.max_value")
            ilo = c.num(cfg, "min_current_ua", dw, ge=0, optional=True)
            ihi = c.num(cfg, "max_current_ua", dw, gt=0, optional=True)
            c.order(ilo, ihi, f"{dw}.min_current_ua", f"{dw}.max_current_ua", strict=False)
            # Set by the service when the supply holds no current limit (0 after every power-up)
            dflt = c.num(cfg, "default_current_ua", dw, gt=0, optional=True)
            c.order(ilo, dflt, f"{dw}.min_current_ua", f"{dw}.default_current_ua", strict=False)
            c.order(dflt, ihi, f"{dw}.default_current_ua", f"{dw}.max_current_ua", strict=False)
            if "arc_detection" in cfg:
                _check_arc(c, cfg["arc_detection"], f"{dw}.arc_detection")


def _two_digits(v: float) -> bool:
    """The gauge controllers store relay set points as d.dE+-XX."""
    return v > 0 and math.isclose(float(f"{v:.1E}"), v, rel_tol=1e-9)


def _check_vacuum(c: _Checker, d):
    c.num(d.get("system_interlocks", {}), "rapid_rise_thresh_mb_s", "system_interlocks", gt=0, le=1000)
    # The gauge controller relays wired into the safety relay's loop (the HMI's Interlocks panel), as
    # "node.relay"; an empty list until someone has checked the wiring (every relay of controller 10
    # is checked meanwhile)
    wired = (d.get("system_interlocks") or {}).get("safety_loop_relays") if isinstance(
        d.get("system_interlocks"), dict) else None
    if wired is not None:
        w = "system_interlocks.safety_loop_relays"
        if not isinstance(wired, list):
            c.bad(f'{w}: must be a list such as ["10.4"] (controller node.relay), or [] if not known')
        else:
            for item in wired:
                m = re.fullmatch(r"(\d+)\.([1-6])", str(item).strip())
                node_cfg = d.get(m.group(1)) if m else None
                if not m:
                    c.bad(f'{w}: "{item}" is not a controller node and relay 1-6, such as "10.4"')
                elif not isinstance(node_cfg, dict) or m.group(2) not in (node_cfg.get("relays") or {}):
                    c.bad(f"{w}: controller {m.group(1)} has no relay {m.group(2)} in this file")
    devices = {}
    for node, node_cfg in d.items():
        if node.startswith("_") or node == "system_interlocks":
            continue
        if not node.isdigit():
            c.bad(f"{node}: controller keys are bus addresses (digits)")
            continue
        channels = node_cfg.get("channels", {}) if isinstance(node_cfg, dict) else None
        if not isinstance(channels, dict):
            c.bad(f"{node}.channels: must be an object")
            continue
        for ch, ch_cfg in channels.items():
            w = f"{node}.channels.{ch}"
            for key in ("subsystem", "device", "name"):
                c.text(ch_cfg, key, w)
            dev = ch_cfg.get("device") if isinstance(ch_cfg, dict) else None
            if dev in devices:
                c.bad(f"{w}.device: {dev} is also {devices[dev]}")
            elif dev:
                devices[dev] = w
        for relay, r in (node_cfg.get("relays") or {}).items():
            w = f"{node}.relays.{relay}"
            if relay not in {"1", "2", "3", "4", "5", "6"}:     # GRAPHIX: six relays (else silently ignored)
                c.bad(f"{w}: relays are numbered 1-6")
            ch = c.num(r, "channel", w, ge=1, integer=True)
            if ch is not None and str(ch) not in channels:
                c.bad(f"{w}.channel: controller {node} has no channel {ch}")
            lo, hi = GAUGE_RANGE_MBAR
            on, off = c.num(r, "on_val", w, ge=lo, le=hi), c.num(r, "off_val", w, ge=lo, le=hi)
            c.order(on, off, f"{w}.on_val", f"{w}.off_val")
            for key, v in (("on_val", on), ("off_val", off)):
                if v is not None and not _two_digits(v):
                    c.bad(f"{w}.{key}: the controller keeps 2 significant digits ({v:g} would become "
                          f"{float(f'{v:.1E}'):g}) - use a value like {float(f'{v:.1E}'):g}")


def _walk(d, where=""):
    if isinstance(d, dict):
        for k, v in d.items():
            yield join_path(where, k), k, v
            yield from _walk(v, join_path(where, k))


def _check_network(c: _Checker, d):
    host = d.get("host")
    if not isinstance(host, str) or not host.startswith("tcp://"):
        c.bad("host: must look like tcp://127.0.0.1")
    for label, key, v in _walk(d):
        if key == "ip":
            try:
                ipaddress.ip_address(v)
            except (ValueError, TypeError):
                c.bad(f"{label}: {v!r} is not an IP address")
        elif key == "port" and not (isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 65535):
            c.bad(f"{label}: must be a port number 1-65535")
    ports = d.get("zmq_ports")
    if not isinstance(ports, dict):
        c.bad("zmq_ports: must be an object")
        return
    used = {}
    for name, port in ports.items():
        if not (isinstance(port, int) and not isinstance(port, bool) and 1024 <= port <= 65535):
            c.bad(f"zmq_ports.{name}: must be a port number 1024-65535")
        elif port in used:
            c.bad(f"zmq_ports.{name}: {port} is also {used[port]}")
        else:
            used[port] = name
    plc = d.get("plc", {})
    for key in ("rack", "slot"):
        c.num(plc, key, "plc", ge=0, integer=True)
    for key in ("db_interface", "db_retain"):
        c.num(plc, key, "plc", ge=1, integer=True)


def _check_logger(c: _Checker, d):
    tick = c.num(d, "tick_rate_seconds", ge=0.01, le=1)
    flush = c.num(d, "flush_interval_seconds", ge=60, le=3600)
    if tick and flush and flush / tick > 100_000:      # the logger holds that many rows of every tag in RAM
        c.bad(f"flush_interval_seconds / tick_rate_seconds = {flush / tick:,.0f} rows held in memory - "
              "at most 100,000")
    c.num(d, "journal_interval_seconds", ge=1, le=60, optional=True)
    c.num(d, "buffer_overhead_multiplier", ge=1, le=3, optional=True)


def _check_tolerance(c: _Checker, d):
    defaults = d.get("defaults", {})
    for key in ("delay_s", "abs_tol", "rel_tol", "min_setpoint"):
        c.num(defaults, key, "defaults", ge=0, optional=True)
    rules = d.get("rules")
    if not isinstance(rules, list):
        c.bad("rules: must be a list")
        return
    for i, rule in enumerate(rules):
        w = f"rules[{i}]"
        for key in ("name", "setpoint", "readback"):
            c.text(rule, key, w)
        for key in ("delay_s", "abs_tol", "rel_tol", "min_setpoint"):
            c.num(rule, key, w, ge=0, optional=True)


def _check_faults(c: _Checker, d):
    names = {}
    for udt, cfg in d.items():
        if udt.startswith("_"):
            continue
        if not isinstance(cfg, dict):
            c.bad(f"{udt}: must be an object")
            continue
        c.text(cfg, "registry_tag", udt)
        for bit, info in (cfg.get("bits") or {}).items():
            w = f"{udt}.bits.{bit}"
            m = re.fullmatch(r"(\d)\.(\d)", bit)
            if not m or int(m[1]) > 3 or int(m[2]) > 7:
                c.bad(f"{w}: bit must be 'byte.bit' with byte 0-3 and bit 0-7")
            name = c.text(info, "name", w)
            if name in names:
                c.bad(f"{w}.name: {name} is also {names[name]}")
            elif name:
                names[name] = w
            c.num(info, "severity", w, ge=1, le=4, integer=True, optional=True)


def _alarm_fields(c: _Checker, o, w):
    c.num(o, "priority", w, ge=1, le=4, integer=True, optional=True)    # 1-3 sound, 4 is listed only
    c.num(o, "delay_on", w, ge=0, le=3600, optional=True)
    c.num(o, "delay_off", w, ge=0, le=3600, optional=True)
    if "enabled" in o and not isinstance(o["enabled"], bool):
        c.bad(f"{w}.enabled: must be true or false")


def _check_alarms(c: _Checker, d):
    from src.alarms.rules import RULE_KINDS
    c.num(d, "shelve_max_minutes", ge=1, le=1440, optional=True)     # a shelved alarm doesn't sound
    rules = d.get("rules", [])
    if not isinstance(rules, list):
        c.bad("rules: must be a list")
        rules = []
    for i, rule in enumerate(rules):
        kind = rule.get("kind") if isinstance(rule, dict) else None
        if kind not in RULE_KINDS:
            c.bad(f"rules[{i}].kind: {kind!r} is not one of {', '.join(sorted(RULE_KINDS))}")
        else:
            _alarm_fields(c, rule, f"rules[{i}]")
    for alarm_id, o in (d.get("overrides") or {}).items():
        w = f"overrides.{alarm_id}"
        if not isinstance(o, dict):
            c.bad(f"{w}: must be an object")
            continue
        _alarm_fields(c, o, w)


CHECKS = {
    "magnet_config.json": _check_magnet,
    "steerer_magnet_config.json": _check_magnet,
    "smu_config.json": _check_smu,
    "plc_config.json": _check_plc,
    "mpd_config.json": _check_mpd,
    "vac_gauges_config.json": _check_vacuum,
    "network_config.json": _check_network,
    "logger_config.json": _check_logger,
    "readback_tolerance.json": _check_tolerance,
    "fault_config.json": _check_faults,
    "alarm_config.json": _check_alarms,
}
