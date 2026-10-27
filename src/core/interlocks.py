"""Every interlock between the operator and the machine, and what is holding each one now, worked
out from the HMI's live telemetry - for the Interlocks panel (src/gui/control/widgets/
interlocks_widget.py). No Qt here.

The PLC's interlocks are as docs/how-it-works/plc.md describes them, and this module follows that
page: if the PLC program changes, change both. Each permissive is a chain of conditions in series,
every one of which must hold for its output (the safety relay's enable, the gate valve opening...).
A condition is

    OK        it holds
    HOLDING   it doesn't, so it keeps the output off (the group marks the first such)
    UNKNOWN   no live value: the PLC or a service isn't reporting, or it hasn't been read yet
    HIDDEN    in the safety relay's hardwired loop, which no software sees: the two doors, the two
              ESOs, the key switch and START are wired in series and can't be told apart
    SUSPECT   might be what holds it, but the software can't tell
    ON / OFF  an operator's request (ENABLE SAFETY RELAY, OPEN), or an output
    INFO      context: the PLC's own verdict, a latched fault, a sequence holding controls

The gauge controller's set-point relay and the magnet coolant switch are in the hardwired loop
too, but the PC can see them (the vacuum service reads the relay; the PLC has an input from the
switch), so they can be ruled in or out when the relay won't close. Which of gauge controller 1's
relays is wired into the loop is set in vac_gauges_config.json (system_interlocks.
safety_loop_relays, e.g. ["10.4"]); while it is [], every relay the file lists for that
controller is checked. A gauge relay is ruled out only on a current reading: its state read
within RELAY_RECENT_S, and its gauge's live pressure on the right side of the relay's set points
(which the vacuum service has confirmed on the controller).

Where the PLC publishes its own verdict - the gate valve's two permissives, the caesium heating
lockout, Ready in the machine state - it is shown with the conditions, and a verdict that doesn't
match them is flagged: the PLC program may then differ from its description.

    overview = evaluate(view, loop=LoopConfig.load(), fault_text=fault_descriptions(...))
    for group in overview.groups: group.status, group.summary, group.conditions ...
"""
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from src.core import paths

log = logging.getLogger("interlocks")

OK, HOLDING, UNKNOWN, HIDDEN, SUSPECT, ON, OFF, INFO = (
    "ok", "holding", "unknown", "hidden", "suspect", "on", "off", "info")
# What a condition is: an input the PLC (or the PC) reads, a latched PLC fault, the operator's
# request, part of the hardwired loop, the PLC's own verdict, the chain's output, or a sequence
# holding controls (a lease)
INPUT, FAULT, REQUEST, LOOP, PLC, OUTPUT, LEASE = "input", "fault", "request", "loop", "plc", "output", "lease"
GATES = (INPUT, FAULT, REQUEST, LOOP)
# How a group's heading looks: its output is on; it needs the operator's attention (asked for but
# held, or waiting for START); a latched fault holds it; normal; the PLC holds it on by design; unknown
LEVEL_ON, LEVEL_ATTENTION, LEVEL_FAULT, LEVEL_NORMAL, LEVEL_AUTO, LEVEL_UNKNOWN = (
    "on", "attention", "fault", "normal", "auto", "unknown")

# --- Tags (docs/how-it-works/plc.md) ------------------------------------------------------------
RELAY = "ion_beam.facilities.safety_relay_active"
ENABLE = "ion_beam.system.cmd_enable_safety"
TURBO_PROTECTION = "ion_beam.system.turbo_protection"     # service_plc: the source turbo protection holds the source
TURBO_GUARD_TEXT = "ion_beam.source.turbo_pump.guard_message"
TIMER, TIMER_TOTAL = "ion_beam.system.safety_timer_elapsed_ms", "ion_beam.system.safety_timer_total_ms"
AIR = "ion_beam.facilities.stat_air_press_ok"
SRC_COOLANT = "ion_beam.facilities.stat_src_coolant_ok"
MAG_COOLANT = "ion_beam.facilities.stat_mag_coolant_ok"
CHAMBER = "ion_beam.source.chamber"
GV_OPEN, GV_CLOSED = f"{CHAMBER}.stat_gv_open", f"{CHAMBER}.stat_gv_closed"
GV_OPEN_PERMISSIVE = f"{CHAMBER}.stat_gv_open_permissive"
GV_CLOSE_PERMISSIVE = f"{CHAMBER}.stat_gv_close_permissive"
GV_REQUEST = f"{CHAMBER}.cmd_open_gv"
VAC_OK = f"{CHAMBER}.stat_vac_ok_for_gv"                # what the PC gives the PLC
VAC_HOLD_LEFT = f"{CHAMBER}.vac_hold_left_s"            # the PLC service's hold through a vacuum-service restart
KILL = "ion_beam.internal.seq_cmd_kill_relay"
SEQ_GV = "ion_beam.internal.seq_cmd_open_gv"
FORCE_COOLER = "ion_beam.internal.seq_cmd_force_cooler"
CS_HOT, FIL_HOT = "ion_beam.internal.cs_is_hot", "ion_beam.internal.fil_is_hot"
CS_HEATING_PERMISSIVE = "ion_beam.internal.cs_heating_permissive"
MACHINE_STATE = "ion_beam.system.machine_state"
COMMS_LOST = "ion_beam.system.pc_plc_comms_lost"
REMOTE_IO_LOST = "ion_beam.system.remote_io_lost"
AUTO_SHUTDOWN = "ion_beam.system.src_auto_sdown"
REMOTE_STEP, PC_STEP = "ion_beam.system.remote_shutdown_step", "ion_beam.system.pc_shutdown_step"
SRC = "ion_beam.source"
CESIUM = f"{SRC}.cesium"
PLC_ONLINE = "system.connected"                         # TelemetryCache.view(): the PLC is reachable
VACUUM_FRESH = "vacuum.connected"                       # ... the vacuum service is publishing
VACUUM_LINK = "VACUUM_DATA/system.connected"            # ... and reaching the gauge controllers
VAC_JUDGED = "ion_beam.vacuum.stat_judged"
VAC_MISMATCH = "ion_beam.vacuum.stat_relay_sp_mismatch"
GAUGES = {"VG1": "ion_beam.source.vacuum_gauge_1", "VG2": "ion_beam.beamline.vacuum_gauge_2",
          "VG3": "ion_beam.beamline.vacuum_gauge_3", "VG4": "ion_beam.endstation.vacuum_gauge_4",
          "VG5": "ion_beam.loadlock.vacuum_gauge_5", "VG6": "ion_beam.endstation.vacuum_gauge_6"}
HV_OFF_KV = 0.15            # the close permissive: extraction and target readbacks below this
# A gauge controller relay's state counts as current for this long after the vacuum service read it
# (it reads each relay about every 10 s, and publishes the state as unknown after 30 s)
RELAY_RECENT_S = 25.0

MACHINE_STATES = {0: "Init", 1: "Safe / not ready", 2: "Ready", 3: "Live / HV", 99: "Fault"}
MACHINE_MEANS = {0: "The PLC is starting.",
                 1: "The safety relay is open and the PLC's conditions for it aren't all met.",
                 2: "The PLC's conditions for the safety relay are met: it can be switched on.",
                 3: "The safety relay is closed: high voltage is available.",
                 99: "A PLC fault is latched (see Fault reset)."}
# As the System Health panel shows them
SHUTDOWN_STEPS = {1: "GRACE PERIOD (75% EXTR)", 2: "CESIUM COOLDOWN", 3: "FINAL KILL", 10: "HOLD HOT PHASE",
                  11: "FILAMENT RAMP DOWN", 99: "LATCHED SAFE (RESET REQ)"}
# A fault reset is accepted at these steps (FB5), and refused at the others
RESET_REMOTE_STEPS, RESET_PC_STEPS = (0, 3, 99), (0, 2, 3, 99)
CTRL_MODES = {1: "PLC auto", 3: "cascaded (auto-emission)", 4: "arc quench"}
SOURCE_SUPPLIES = (("extraction", "Extraction"), ("target", "Target (cathode)"), ("filament", "Filament"),
                   ("thermionic", "Thermionic (ioniser)"))
VG_FAULTS = ("Not_Found", "Type_Mismatch", "Above_SP_Warn", "Rapid_Rise_Warn")
LOOP_CONTROLLER = 10        # gauge controller 1 (GRAPHIX node 10): VG1 to VG3
NO_PLC = "no data from the PLC"
RESET = "RESET FAULTS"


@dataclass
class Condition:
    label: str
    state: str
    detail: str = ""
    kind: str = INPUT
    tag: str = ""           # the tag it is read from (to plot in the data viewer)
    see: str = ""           # the key of the group that says more about it
    section: str = ""       # the part of its group it belongs to (a sub-heading), if any
    why: str = ""           # when it holds the chain: what holds it, as a phrase ("the gate valve is closed")

    @property
    def gates(self) -> bool:
        """Part of the chain (rather than context or the result)."""
        return self.kind in GATES

    def reason(self) -> str:
        return self.why or f"{self.label.lower()}: {self.detail}"


@dataclass
class Group:
    key: str
    title: str
    intro: str                          # one line: what the chain decides
    conditions: List[Condition]
    status: str = ""                    # the heading's chip: LIVE, OFF, HELD ...
    level: str = LEVEL_NORMAL
    summary: str = ""                   # one sentence: what holds it, or what it would take
    notes: List[Tuple[str, str]] = field(default_factory=list)     # (text, 'info' or 'warn')
    chain: bool = True                  # conditions in series (False: a plain list)
    disagree: str = ""                  # the PLC's own verdict doesn't match the conditions: why

    @property
    def first(self) -> Optional[int]:
        """Index of the first condition holding the output off."""
        return next((i for i, c in enumerate(self.conditions) if c.gates and c.state == HOLDING), None)

    def holding(self) -> List[Condition]:
        return [c for c in self.conditions if c.gates and c.state == HOLDING]


@dataclass
class Overview:
    plc_online: bool
    machine_name: str                   # 'READY', 'FAULT', 'PLC OFFLINE' ...
    machine: str                        # what that means now, as a sentence or two
    machine_level: str
    groups: List[Group]

    def group(self, key: str) -> Optional[Group]:
        return next((g for g in self.groups if g.key == key), None)


# --- The safety relay's loop: which gauge controller relays are in it -------------------------------
@dataclass
class LoopConfig:
    """From vac_gauges_config.json: the gauge controller relays wired into the safety relay's
    loop (None: not set, so every relay the file lists for gauge controller 1 is a candidate),
    each relay's channel and set points, and the gauges' names by (node, channel)."""
    relays: Optional[List[Tuple[int, int]]] = None
    relay_info: Dict[Tuple[int, int], dict] = field(default_factory=dict)
    gauges: Dict[Tuple[int, int], str] = field(default_factory=dict)
    problem: str = ""

    @classmethod
    def load(cls, path: str = None) -> "LoopConfig":
        path = path or paths.config_path("vac_gauges_config.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                return cls.parse(json.load(f))
        except (OSError, ValueError) as e:
            log.warning("Interlocks: %s not readable (%s): the loop's gauge relays are taken as unknown", path, e)
            return cls(problem=f"vac_gauges_config.json isn't readable ({e})")

    @classmethod
    def parse(cls, cfg: dict) -> "LoopConfig":
        out = cls()
        if not isinstance(cfg, dict):
            out.problem = "vac_gauges_config.json isn't an object"
            return out
        for node, node_cfg in cfg.items():
            if not str(node).isdigit() or not isinstance(node_cfg, dict):
                continue
            n = int(node)
            for ch, ch_cfg in (node_cfg.get("channels") or {}).items():
                device = ch_cfg.get("device", "") if isinstance(ch_cfg, dict) else ""
                m = re.search(r"(\d+)$", str(device))
                if str(ch).isdigit():
                    out.gauges[(n, int(ch))] = f"VG{m.group(1)}" if m else str(device)
            for relay, r in (node_cfg.get("relays") or {}).items():
                if str(relay).isdigit() and isinstance(r, dict):
                    try:
                        out.relay_info[(n, int(relay))] = {"ch": int(r["channel"]), "on": float(r["on_val"]),
                                                           "off": float(r["off_val"])}
                    except (KeyError, TypeError, ValueError):
                        out.relay_info[(n, int(relay))] = {}
        settings = cfg.get("system_interlocks")
        wired = settings.get("safety_loop_relays") if isinstance(settings, dict) else None
        if wired:                       # (an empty list: not known yet)
            parsed = [parse_relay(x) for x in wired] if isinstance(wired, list) else [None]
            if None in parsed:
                out.problem = f"system_interlocks.safety_loop_relays isn't understood ({wired!r})"
            else:
                out.relays = parsed
        return out

    def candidates(self) -> List[Tuple[int, int]]:
        if self.relays:
            return list(self.relays)
        found = sorted(k for k in self.relay_info if k[0] == LOOP_CONTROLLER)
        return found or [(LOOP_CONTROLLER, r) for r in range(1, 7)]


def parse_relay(text) -> Optional[Tuple[int, int]]:
    """'10.4' -> (10, 4): gauge controller node 10, relay 4 (None if it isn't like that)."""
    m = re.fullmatch(r"\s*(\d+)\.([1-6])\s*", str(text))
    return (int(m.group(1)), int(m.group(2))) if m else None


def relay_status_tag(node: int, relay: int) -> str:
    """As the vacuum service publishes it: 1 closed, 0 open, unknown if not read for 30 s."""
    return f"ion_beam.vacuum.controller_{node}.relay_{relay}_status"


def relay_read_at_tag(node: int, relay: int) -> str:
    """When the vacuum service last read that relay's state (epoch seconds)."""
    return f"ion_beam.vacuum.controller_{node}.relay_{relay}_read_at"


# --- Reading the telemetry -------------------------------------------------------------------------
class _Read:
    def __init__(self, data: dict, now: float = None):
        self.d = data
        self.now = time.time() if now is None else now      # (epoch seconds, as the services stamp)

    def num(self, tag) -> Optional[float]:
        v = self.d.get(tag)
        if v is None or isinstance(v, str):
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if math.isfinite(f) else None

    def flag(self, tag) -> Optional[bool]:
        v = self.num(tag)
        return None if v is None else v != 0.0

    def fault(self, name) -> Optional[bool]:
        """A latched PLC fault (service_plc publishes each bit as faults.<name>)."""
        return self.flag(f"faults.{name}")

    def step(self) -> Optional[int]:
        """The shutdown step the System Health panel shows (the higher of the two)."""
        a, b = self.num(REMOTE_STEP), self.num(PC_STEP)
        return None if a is None or b is None else int(max(a, b))

    def pressure(self, base: str) -> str:
        p = self.num(f"{base}.rb_pressure")
        return f"{p:.1E} mbar" if p is not None else "no reading"


def _step_name(step) -> str:
    return SHUTDOWN_STEPS.get(step, f"step {step}")


def _switch(r: _Read, label, tag, ok, bad, why, fault=None, see="", section="") -> Condition:
    """A PLC input that holds the chain while it reads 0; its latched fault (if any) is noted."""
    v = r.flag(tag)
    if v is None:
        return Condition(label, UNKNOWN, NO_PLC, tag=tag, section=section)
    if v:
        latched = fault and r.fault(fault)
        return Condition(label, OK, ok + (f" ({fault} latched: {RESET} clears it)" if latched else ""),
                         tag=tag, section=section)
    return Condition(label, HOLDING, bad, tag=tag, see=see, section=section, why=why)


def _no_fault(r: _Read, label, names, fix="", section="") -> Condition:
    """A chain that needs none of these PLC faults latched."""
    values = {n: r.fault(n) for n in names}
    latched = [n for n, v in values.items() if v]
    if latched:
        fix = fix or f"{RESET} once the cause has gone"
        listed = ", ".join(latched)
        return Condition(label, HOLDING, f"latched: {listed} ({fix})", kind=FAULT, tag=f"faults.{latched[0]}",
                         section=section, why=f"{listed} {'is' if len(latched) == 1 else 'are'} latched ({fix})")
    if any(v is None for v in values.values()):
        return Condition(label, UNKNOWN, NO_PLC, kind=FAULT, section=section)
    return Condition(label, OK, "none latched" if len(names) > 1 else "not latched", kind=FAULT, section=section)


def _flag_cond(r: _Read, label, tag, ok, bad, why, invert=False, see="", section="", kind=INPUT) -> Condition:
    """A flag that holds the chain while it reads 0 (invert: while it reads 1)."""
    v = r.flag(tag)
    if v is None:
        return Condition(label, UNKNOWN, NO_PLC, tag=tag, section=section, kind=kind)
    if v != invert:
        return Condition(label, OK, ok, tag=tag, section=section, kind=kind)
    return Condition(label, HOLDING, bad, tag=tag, see=see, section=section, kind=kind, why=why)


def _gate_valve_position(r: _Read) -> Tuple[Optional[bool], str]:
    """(open?, 'open' / 'closed' / 'moving' / 'both limit switches made')."""
    is_open, is_closed = r.flag(GV_OPEN), r.flag(GV_CLOSED)
    if is_open is None or is_closed is None:
        return None, NO_PLC
    if is_open and is_closed:
        return False, "both limit switches made (GateValve_Conflict)"
    if is_open:
        return True, "open"
    return False, "closed" if is_closed else "between its limit switches (moving)"


def _unknown_any(conds) -> bool:
    return any(c.state == UNKNOWN for c in conds)


def _all_ok(conds) -> Optional[bool]:
    """All hold (True), one doesn't (False), or not all known and none holding (None)."""
    if any(c.state == HOLDING for c in conds):
        return False
    return None if _unknown_any(conds) else True


# --- The safety relay -------------------------------------------------------------------------------
LOOP_HIDDEN = "Doors (2), ESOs (2), key switch, START"
SECTION_PLC, SECTION_LOOP = "The PLC's enable", "The relay's hardwired loop"


def gauge_relay_now(r: _Read, loop: LoopConfig, key: Tuple[int, int]) -> Tuple[str, str]:
    """('closed' | 'open' | 'unknown', what it rests on: "VG1 at 2.1E-07 mbar ...") for one gauge
    controller relay, now.

    Closed needs both: the state read as closed within RELAY_RECENT_S, and its gauge answering
    with a pressure no higher than the relay's off point (set points confirmed on the controller).
    Open: a pressure above the off point, or the state read as open recently (unless the pressure
    is below the on point, when the readings disagree). Anything else is unknown: a state from
    before a vacuum-service restart, a controller that stopped answering, a single garbled read."""
    node, relay = key
    info = loop.relay_info.get(key) or {}
    on, off = info.get("on"), info.get("off")
    gauge = loop.gauges.get((node, info.get("ch")), "") if info else ""
    base = GAUGES.get(gauge, "")
    status, at = r.num(relay_status_tag(node, relay)), r.num(relay_read_at_tag(node, relay))
    age = None if status is None or at is None else r.now - at
    recent = age is not None and -2.0 <= age <= RELAY_RECENT_S
    ago = f"{max(age, 0):.0f} s ago" if age is not None else ""
    confirmed = r.flag(VAC_MISMATCH) is False
    p, no_p = None, ""
    if not info:
        no_p = "its set points aren't in vac_gauges_config.json"
    elif not confirmed:
        no_p = "its set points aren't confirmed on the controller yet"
    elif not base:
        no_p = "its gauge isn't known"
    elif r.flag(f"{base}.stat_comms_fail") is not False:
        no_p = f"{gauge} isn't answering"
    elif r.flag(f"{base}.stat_not_found") or r.flag(f"{base}.stat_mismatch"):
        no_p = f"{gauge} reports a fault"
    else:
        p = r.num(f"{base}.rb_pressure")
        no_p = "" if p is not None else f"{gauge} has no live reading"
    at_p = f"{gauge} at {p:.1E} mbar" if p is not None else ""
    if p is not None and p > off:
        return "open", f"{at_p}, above the {off:.1E} at which it opens"
    if recent and status == 0:
        if p is not None and p < on:
            return "unknown", f"read open {ago}, but {at_p} is below the {on:.1E} at which it closes (the readings disagree)"
        return "open", f"read open {ago}" + (f" ({at_p})" if at_p else "")
    if recent and status == 1:
        if p is None:
            return "unknown", f"read closed {ago}, but {no_p}"
        if p < on:
            return "closed", f"{at_p} (it opens above {off:.1E})"
        return "closed", f"{at_p}, between its set points (it opens above {off:.1E})"
    if status is None or at is None:
        return "unknown", "not read recently"
    if not recent:
        return "unknown", f"last read {ago}"
    return "unknown", f"read as {status:g}"


def _relays_text(keys, judged) -> str:
    """'relay 4: VG1 at ...', with relays that say the same merged: 'relays 1 and 4: VG1 at ...'."""
    same = {}
    for key in keys:
        same.setdefault(judged[key][1], []).append(key[1])
    out = []
    for text, relays in same.items():
        if len(relays) == 1:
            out.append(f"relay {relays[0]}: {text}")
        else:
            names = ", ".join(str(n) for n in relays[:-1]) + f" and {relays[-1]}"
            out.append(f"relays {names}: " + text.replace("it opens", "they open").replace("it closes", "they close")
                       .replace("between its", "between their"))
    return "; ".join(out)


def loop_vacuum_relay(r: _Read, loop: LoopConfig, relay_closed: bool = False) -> Condition:
    """The gauge controller's set-point relay in the safety relay's loop, now: closed, open, or
    unknown (gauge_relay_now). While the safety relay is closed the loop is made, so a relay on
    controller 1 that is open isn't the one wired into it."""
    label = "Gauge controller relay (vacuum)"
    keys = loop.candidates()
    known = bool(loop.relays)
    tags = [relay_status_tag(*k) for k in keys]
    base = Condition(label, UNKNOWN, kind=LOOP, tag=tags[0] if len(tags) == 1 else "", section=SECTION_LOOP)
    if not r.d.get(VACUUM_FRESH) or r.flag(VACUUM_LINK) is False:
        why = ("no data from the vacuum service" if not r.d.get(VACUUM_FRESH) else
               "the gauge controllers aren't answering the vacuum service")
        if relay_closed:
            return Condition(label, OK, f"the loop is made ({why})", kind=LOOP, tag=base.tag, section=SECTION_LOOP)
        base.detail = why
        return base
    judged = {k: gauge_relay_now(r, loop, k) for k in keys}
    opened = [k for k in keys if judged[k][0] == "open"]
    unknown = [k for k in keys if judged[k][0] == "unknown"]
    if opened:
        text = _relays_text(opened, judged)
        one = len(opened) == 1
        tag = relay_status_tag(*opened[0])
        if known:
            return Condition(label, HOLDING, f"open: {text}", kind=LOOP, tag=tag, section=SECTION_LOOP,
                             why=f"the gauge controller's relay in the loop is open ({text})")
        if relay_closed:
            return Condition(label, OK, "the loop is made, so " + ("this open relay isn't in it" if one else
                             "these open relays aren't in it") + f" ({text})", kind=LOOP, tag=tag, section=SECTION_LOOP)
        return Condition(label, SUSPECT, f"open: {text}. Which relay is wired into the loop isn't set in "
                         "vac_gauges_config.json: if it is " + ("this one" if one else "one of these")
                         + ", the loop is open", kind=LOOP, tag=tag, section=SECTION_LOOP,
                         why=f"an open relay on gauge controller 1, if it is the one wired into the loop ({text})")
    if unknown and relay_closed:            # (the loop is made, whatever the readings)
        return Condition(label, OK, f"the loop is made ({_relays_text(unknown, judged)})", kind=LOOP, tag=base.tag,
                         section=SECTION_LOOP)
    if unknown:
        base.detail = _relays_text(unknown, judged)
        return base
    if known:
        return Condition(label, OK, "closed: " + _relays_text(keys, judged), kind=LOOP, tag=base.tag,
                         section=SECTION_LOOP)
    return Condition(label, OK, f"all {len(keys)} relays on gauge controller 1 closed (which one is in the loop "
                     "isn't set yet)", kind=LOOP, section=SECTION_LOOP)


def _relay_group(r: _Read, loop: LoopConfig, close_permissive: Optional[bool]) -> Group:
    relay, requested = r.flag(RELAY), r.flag(ENABLE)
    elapsed, total = r.num(TIMER), r.num(TIMER_TOTAL)
    waiting = bool(requested) and relay is False and elapsed is not None and bool(total) and 0 < elapsed < total
    left = int((total - elapsed) / 1000) + 1 if waiting else 0
    gv_open, gv_text = _gate_valve_position(r)
    kill = r.flag(KILL)
    step = r.step()
    kill_label = "No relay kill (shutdown sequence)"
    if kill is None:
        kill_cond = Condition(kill_label, UNKNOWN, NO_PLC, tag=KILL, section=SECTION_PLC)
    elif kill:
        at = f" at {_step_name(step)}" if step else ""
        kill_cond = Condition(kill_label, HOLDING, f"killed{at}: {RESET} clears it", tag=KILL, section=SECTION_PLC,
                              why=f"the shutdown sequence killed the relay{at} ({RESET} clears it)")
    else:
        kill_cond = Condition(kill_label, OK, "none", tag=KILL, section=SECTION_PLC)
    plc = [
        _switch(r, "Compressed air", AIR, "pressure OK", "pressure low", "the compressed air pressure is low",
                "Air_Pressure_Low", section=SECTION_PLC),
        _switch(r, "Source coolant flow", SRC_COOLANT, "flowing", "flow low", "the source coolant flow is low",
                "Src_Coolant_Flow_Low", section=SECTION_PLC),
        _switch(r, "Magnet coolant", MAG_COOLANT, "flow and temperature OK", "flow low or too hot",
                "the magnet coolant flow is low or too hot", "Mag_Coolant_Flow_Low", section=SECTION_PLC),
        Condition("Gate valve open", UNKNOWN if gv_open is None else OK if gv_open else HOLDING, gv_text,
                  tag=GV_OPEN, see="" if gv_open else "gv_open", section=SECTION_PLC,
                  why=f"the gate valve is {gv_text}"),
        kill_cond,
        _no_fault(r, "No caesium over-temperature", ["Cesium_Oven_OverTemp"],
                  f"{RESET} once the oven is below 200 °C", section=SECTION_PLC),
    ]
    permissive = _all_ok(plc)
    if requested is None:
        request = Condition("ENABLE SAFETY RELAY", UNKNOWN, NO_PLC, kind=REQUEST, tag=ENABLE, section=SECTION_PLC)
    elif requested:
        request = Condition("ENABLE SAFETY RELAY", ON, f"requested: press START ({left} s left)" if waiting else
                            "requested", kind=REQUEST, tag=ENABLE, section=SECTION_PLC)
    else:
        request = Condition("ENABLE SAFETY RELAY", OFF, "not requested (System Health panel)", kind=REQUEST,
                            tag=ENABLE, section=SECTION_PLC)
    state = r.num(MACHINE_STATE)
    plc_says = {1: False, 2: True, 3: True}.get(int(state)) if state is not None else None
    verdict = Condition("The PLC's verdict", INFO, NO_PLC if state is None else
                        f"{MACHINE_STATES.get(int(state), 'state')} (machine state {int(state)})", kind=PLC,
                        tag=MACHINE_STATE, section=SECTION_PLC)

    # The hardwired loop: the vacuum relay and the magnet coolant switch can be seen, the rest can't
    vac = loop_vacuum_relay(r, loop, relay_closed=bool(relay))
    mag = _flag_cond(r, "Magnet coolant switch", MAG_COOLANT, "its PLC input reads OK", "its PLC input reads open",
                     "the magnet coolant switch is open", section=SECTION_LOOP, kind=LOOP)
    refused, tripped = r.fault("Safety_Relay_Refused"), r.fault("Safety_Relay_Tripped")
    first = next((c for c in plc if c.state == HOLDING), None)
    loop_hold = next((c for c in (vac, mag) if c.state == HOLDING), None)
    if relay:
        hidden = Condition(LOOP_HIDDEN, OK, "all made: the relay is closed", kind=LOOP, section=SECTION_LOOP)
    elif waiting:
        hidden = Condition(LOOP_HIDDEN, HIDDEN, "waiting for START; the rest can't be seen", kind=LOOP,
                           section=SECTION_LOOP)
    elif relay is False and (refused or tripped) and first is None and loop_hold is None:
        hidden = Condition(LOOP_HIDDEN, SUSPECT, "one of these may be open: the software can't tell which",
                           kind=LOOP, section=SECTION_LOOP)
    else:
        hidden = Condition(LOOP_HIDDEN, HIDDEN, "in series and unseen: the PLC only sees whether the relay closes",
                           kind=LOOP, section=SECTION_LOOP)
    output = Condition("Safety relay", UNKNOWN if relay is None else ON if relay else OFF,
                       NO_PLC if relay is None else "closed: high voltage available" if relay else "open",
                       kind=OUTPUT, tag=RELAY)
    g = Group("relay", "Safety relay",
              "The PLC gives its enable when all of these hold; the relay then closes on START if its hardwired "
              "loop is made.", plc + [request, verdict, vac, mag, hidden, output])

    if relay is None:
        g.status, g.level, g.summary = "UNKNOWN", LEVEL_UNKNOWN, "No data from the PLC."
    elif relay:
        g.status, g.level = "LIVE", LEVEL_ATTENTION      # as System Health: high voltage is available
        g.summary = "Closed: the supplies on the relay can be switched on."
        if close_permissive is False:
            g.notes.append(("The source is hot or at high voltage, so the PLC keeps the relay on (see Source cold).",
                            "info"))
    else:
        if tripped:
            g.status, g.level = "TRIPPED", LEVEL_FAULT
        elif refused:
            g.status, g.level = "REFUSED", LEVEL_FAULT
        elif waiting:
            g.status, g.level = "PRESS START", LEVEL_ATTENTION
        elif requested:
            g.status, g.level = "HELD", LEVEL_ATTENTION
        else:
            g.status, g.level = "OFF", LEVEL_NORMAL
        if waiting:
            g.summary = f"The PLC gives its enable: press START on the High Voltage Systems Control box ({left} s left)."
            if loop_hold is not None:
                g.summary += f" The relay can't close while {loop_hold.reason()}."
        elif first is not None:
            g.summary = f"Held off by the PLC: {first.reason()}."
        elif plc_says is False and permissive:
            g.summary = (f"The PLC reports not ready (machine state {int(state)}), though the conditions listed "
                         "hold.")
        elif permissive is None:
            g.summary = "Not all of the PLC's conditions are known."
        elif loop_hold is not None:
            g.summary = f"The PLC's conditions hold, but the relay can't close: {loop_hold.reason()}."
        elif refused or tripped:
            g.summary = f"The PLC's conditions hold now: {RESET}, then try again."
        elif requested:
            g.summary = "Requested: press START on the High Voltage Systems Control box."
        else:
            g.summary = "The PLC's conditions hold: press ENABLE SAFETY RELAY, then START."
        if not waiting:
            g.notes += [(text, "warn") for text in _loop_diagnosis(refused, tripped, first, vac, mag)]
    if r.flag(TURBO_PROTECTION):
        g.notes.insert(0, ("The source turbo protection has tripped: the PC refuses ENABLE SAFETY RELAY (and the "
                           "source's supplies) until RESET FAULTS, once the turbo's load is back to normal. "
                           + str(r.d.get(TURBO_GUARD_TEXT) or ""), "warn"))
    if permissive is not None and plc_says is not None and plc_says != permissive and not r.step():
        said = f"{MACHINE_STATES.get(int(state), 'state')} (machine state {int(state)})"
        g.disagree = (f"The PLC says {said}, but by the conditions listed its enable "
                      + ("couldn't be given" if plc_says else "could be given") + ": the PLC program may differ "
                      "from its description.")
    elif relay and vac.state == HOLDING:
        g.disagree = ("The safety relay is closed, but the gauge controller relay set as in its loop reads open: "
                      "check system_interlocks.safety_loop_relays in vac_gauges_config.json.")
    return g


def _loop_diagnosis(refused, tripped, first, vac, mag) -> List[str]:
    """Why the relay didn't close (refused) or opened by itself (tripped), as far as the software
    can tell. The doors, ESOs, key switch and START can't be told apart: the PLC only sees that the
    relay didn't close within 5 s of its enable. What can be seen in the loop is judged now: a
    gauge relay closed now isn't holding the loop open now, whatever it did at the time."""
    if not (refused or tripped):
        return []
    parts = ["The relay opened without being switched off (Safety_Relay_Tripped)." if tripped else
             "The relay didn't close within 5 s of the PLC's enable (Safety_Relay_Refused)."]
    if first is not None:
        parts.append(f"The PLC withholds its enable now as well: {first.reason()}.")
    holding = [c for c in (vac, mag) if c.state == HOLDING]
    if holding:
        said = [c for c in holding if first is None or c.tag != first.tag]     # (the magnet coolant: said already)
        if said:
            parts.append("Now " + " and ".join(c.reason() for c in said)
                         + (": that alone holds the loop open." if len(said) == 1 else
                            ": either alone holds the loop open."))
        parts.append("A door, an ESO or the key switch could be open as well: the software can't see those.")
        parts.append(f"Once that is cleared, {RESET} and try again.")
        return [" ".join(parts)]
    ruled_out = [c for c in (vac, mag) if c.state == OK]
    if ruled_out:
        parts.append("Ruled out: the " + " and the ".join(c.label.lower() for c in ruled_out)
                     + " (closed now, so not holding the loop open).")
    if tripped:
        left = ["a door, an ESO or the key switch opened",
                "something opened for a moment and closed again (the latched faults, and the first-out ⭐ in the "
                "Alarms panel, say what the PLC saw)"]
    else:
        left = ["a door, an ESO or the key switch is open", "START wasn't pressed within the 5 s"]
    left += [c.reason() for c in (vac, mag) if c.state == SUSPECT]
    left += [f"the {c.label.lower()} (not known now: {c.detail})" for c in (vac, mag) if c.state == UNKNOWN]
    parts.append("That leaves: " + "; or ".join(left) + ". The software can't tell which.")
    parts.append(f"Once that is cleared, {RESET} and try again." if first is not None else
                 f"Check them, then {RESET} and try again.")
    return [" ".join(parts)]


# --- The gate valve ---------------------------------------------------------------------------------
def _vacuum_reason(r: _Read) -> str:
    """Why the vacuum service doesn't give the gate valve its vacuum permissive (VG1 and VG2 below
    their set points, read recently, their relays confirmed)."""
    if not r.d.get(VACUUM_FRESH):
        return "the vacuum service isn't sending data"
    if r.flag(VACUUM_LINK) is False:
        return "the gauge controllers aren't answering"
    if r.flag(VAC_JUDGED) is False:
        return "the vacuum service has only just started and hasn't judged the vacuum yet"
    reasons = []
    for name in ("VG1", "VG2"):
        base = GAUGES[name]
        if r.flag(f"{base}.stat_above_sp"):
            reasons.append(f"{name} above its set point ({r.pressure(base)})")
        elif r.num(f"{base}.rb_pressure") is None:
            reasons.append(f"{name} has no reading")
        for flag, text in (("stat_not_found", "not found"), ("stat_mismatch", "a gauge fault"),
                           ("stat_rapid_rise", "rising rapidly")):
            if r.flag(f"{base}.{flag}"):
                reasons.append(f"{name} {text}")
    if r.flag(VAC_MISMATCH):
        reasons.append("a gauge relay's set points aren't confirmed (see the Vacuum System panel)")
    return "; ".join(reasons) or "VG1's and VG2's relays not yet read back against vac_gauges_config.json"


def _gv_open_group(r: _Read) -> Group:
    vac = r.flag(VAC_OK)
    hold = r.num(VAC_HOLD_LEFT) or 0.0
    label = "Vacuum good (the PC's flag)"
    if vac is None:
        vac_cond = Condition(label, UNKNOWN, NO_PLC, tag=VAC_OK)
    elif vac:
        vac_cond = Condition(label, OK, f"held through a vacuum-service restart ({hold:.0f} s left)" if hold > 0
                             else "VG1 and VG2 below their relays' set points", tag=VAC_OK)
    else:
        reason = _vacuum_reason(r)
        vac_cond = Condition(label, HOLDING, reason, tag=VAC_OK, why=f"the vacuum isn't good for it ({reason})")
    if r.flag(COMMS_LOST):
        watchdog = Condition("PC watchdog", HOLDING, "the PLC has lost the PC's watchdog", kind=FAULT, tag=COMMS_LOST,
                             why="the PLC has lost the PC's watchdog")
    else:
        watchdog = _no_fault(r, "PC watchdog", ["PC_Watchdog_Timeout"])
    perm = [vac_cond, watchdog,
            _no_fault(r, "Gauge controllers' comms", ["Graphix_1_Comms_Fail", "Graphix_2_Comms_Fail"]),
            _no_fault(r, "VG1 healthy", [f"VG1_{f}" for f in VG_FAULTS],
                      f"{RESET} once the pressure is back below the set point"),
            _no_fault(r, "VG2 healthy", [f"VG2_{f}" for f in VG_FAULTS],
                      f"{RESET} once the pressure is back below the set point")]
    computed = _all_ok(perm)
    plc_bit = r.flag(GV_OPEN_PERMISSIVE)
    bit = Condition("The PLC's open permissive", INFO, NO_PLC if plc_bit is None else
                    "given" if plc_bit else "withheld", kind=PLC, tag=GV_OPEN_PERMISSIVE)
    seq, step = r.flag(SEQ_GV), r.step()
    seq_label = "Shutdown sequence allows it"
    if seq is None:
        seq_cond = Condition(seq_label, UNKNOWN, NO_PLC, tag=SEQ_GV)
    elif seq:
        seq_cond = Condition(seq_label, OK, "allowed", tag=SEQ_GV)
    else:
        text = f"forbidden at {_step_name(step)}" if step else f"forbidden since the last shutdown: {RESET} gives it back"
        seq_cond = Condition(seq_label, HOLDING, text, tag=SEQ_GV, why=f"the shutdown sequencer forbids it ({text})")
    air = _switch(r, "Compressed air", AIR, "pressure OK", "pressure low (the valve is pneumatic)",
                  "the compressed air pressure is low", "Air_Pressure_Low")
    requested = r.flag(GV_REQUEST)
    request = Condition("OPEN", UNKNOWN if requested is None else ON if requested else OFF,
                        NO_PLC if requested is None else "requested" if requested else
                        "not requested (Vacuum System panel)", kind=REQUEST, tag=GV_REQUEST)
    is_open, text = _gate_valve_position(r)
    output = Condition("Gate valve", UNKNOWN if is_open is None else ON if is_open else OFF, text, kind=OUTPUT,
                       tag=GV_OPEN)
    g = Group("gv_open", "Gate valve",
              "The PLC opens the source gate valve while OPEN is requested and all of these hold, and closes it "
              "when one goes.", perm + [bit, seq_cond, air, request, output])
    # The PLC's own open permissive decides, where it is known; the conditions say why
    held_perm = [c for c in perm if c.state == HOLDING]
    held_other = [c for c in (seq_cond, air) if c.state == HOLDING]
    if is_open is None:
        g.status, g.level, g.summary = "UNKNOWN", LEVEL_UNKNOWN, "No data from the PLC."
    elif is_open:
        g.status, g.level, g.summary = "OPEN", LEVEL_NORMAL, "Open."
    else:
        g.status = "CLOSED" if text == "closed" else "MOVING" if "moving" in text else "CONFLICT"
        cause = None
        if plc_bit is False:
            g.summary = (f"Can't open: {held_perm[0].reason()}, so the PLC withholds its open permissive." if held_perm
                         else "Can't open: the PLC withholds its open permissive.")
            cause = held_perm[0] if held_perm else bit
        elif (held_perm and plc_bit is None) or held_other:
            cause = (held_perm if plc_bit is None and held_perm else held_other)[0]
            g.summary = f"Can't open: {cause.reason()}."
        elif requested:
            g.level, g.summary = LEVEL_ATTENTION, "Requested: the valve takes a few seconds to open."
        elif _unknown_any(c for c in g.conditions if c.gates and (plc_bit is None or c not in perm)):
            g.level, g.summary = LEVEL_NORMAL, "Not all of the conditions are known."
        else:
            g.level, g.summary = LEVEL_NORMAL, "Ready to open: press OPEN on the Vacuum System panel."
        if cause is not None:
            g.level = LEVEL_FAULT if cause.kind == FAULT else LEVEL_ATTENTION if requested else LEVEL_NORMAL
    if computed is not None and plc_bit is not None and computed != plc_bit:
        g.disagree = (f"The PLC's open permissive is {'given' if plc_bit else 'withheld'}, but by the conditions "
                      f"listed it should be {'withheld' if plc_bit else 'given'}: the PLC program may differ from "
                      "its description.")
    return g


def _source_cold_group(r: _Read) -> Group:
    conds = []
    for name, label in (("extraction", "Extraction"), ("target", "Target (cathode)")):
        tag = f"{SRC}.{name}.rb_voltage"
        kv = r.num(tag)
        cond_label = f"{label} below {HV_OFF_KV:g} kV"
        if kv is None:
            conds.append(Condition(cond_label, UNKNOWN, NO_PLC, tag=tag))
        else:
            conds.append(Condition(cond_label, OK if abs(kv) < HV_OFF_KV else HOLDING, f"{kv:.2f} kV", tag=tag,
                                   why=f"the {name} is at {kv:.2f} kV"))
    est = r.num(f"{CESIUM}.estimated_temp")
    temp = f", {est:.0f} °C estimated" if est is not None else ""
    conds.append(_flag_cond(r, "Filament not hot (the PLC's model)", FIL_HOT, "cold", "hot", "the filament is hot",
                            invert=True))
    conds.append(_flag_cond(r, "Caesium not hot (the PLC's model)", CS_HOT, "cold" + temp, "hot" + temp,
                            "the caesium is hot" + (f" ({est:.0f} °C estimated)" if est is not None else ""),
                            invert=True))
    computed = _all_ok(conds)
    bit, relay = r.flag(GV_CLOSE_PERMISSIVE), r.flag(RELAY)
    if bit is None:
        released = NO_PLC
    elif bit:
        released = "yes: the relay can be switched off and the gate valve closed"
    elif relay is False:
        released = "no: it keeps asking for the relay, and for the gate valve to open"
    else:
        released = "no: it holds the relay on and the gate valve open"
    output = Condition("Released by the PLC", UNKNOWN if bit is None else ON if bit else OFF, released, kind=OUTPUT,
                       tag=GV_CLOSE_PERMISSIVE)
    g = Group("source_cold", "Source cold",
              "While the source is hot or at high voltage the PLC keeps asking for the safety relay and for the gate "
              "valve to open, whatever the PC asks (each still needs its own permissive). Cold means all of these:",
              conds + [output])
    if bit is None:
        g.status, g.level, g.summary = "UNKNOWN", LEVEL_UNKNOWN, "No data from the PLC."
    elif bit:
        g.status, g.level, g.summary = "COLD", LEVEL_NORMAL, "The relay can be switched off and the gate valve closed."
    else:
        first = next((c for c in conds if c.state == HOLDING), None)
        why = f": {first.reason()}" if first else ""
        if relay:
            g.status, g.level = "HOT: HELD ON", LEVEL_AUTO
            g.summary = f"The PLC holds the relay on and the gate valve open{why}. To switch off, use INITIATE SHUTDOWN."
        elif relay is False:
            g.status, g.level = "HOT", LEVEL_AUTO
            g.summary = (f"Not cold yet{why}. The relay is open, so the source deck has no power; until the source "
                         "is cold the PLC keeps asking for the relay and for the gate valve to open.")
        else:
            g.status, g.level = "HOT", LEVEL_AUTO
            g.summary = f"Not cold yet{why}."
    if computed is not None and bit is not None and computed != bit:
        g.disagree = (f"The PLC's close permissive is {'given' if bit else 'withheld'}, but by the conditions listed it "
                      f"should be {'withheld' if bit else 'given'}: the PLC's thresholds may differ from its "
                      "description.")
    return g


# --- The source supplies and the caesium oven --------------------------------------------------------
def _supplies_group(r: _Read) -> Group:
    remote, remote_fault, relay = r.flag(REMOTE_IO_LOST), r.fault("Remote_IO_Lost"), r.flag(RELAY)
    remote_label = "Remote I/O on the source deck"
    latched = f" (Remote_IO_Lost latched: {RESET} clears it)" if remote_fault else ""
    if remote is None or relay is None:
        remote_cond = Condition(remote_label, UNKNOWN, NO_PLC, tag=REMOTE_IO_LOST)
    elif not relay:
        # The PLC reports a loss only once the relay has been closed for 25 s, and clears it as the relay opens
        remote_cond = Condition(remote_label, UNKNOWN, "not known with the relay open (the PLC reports a loss only "
                                "once the relay has been closed 25 s)" + latched, tag=REMOTE_IO_LOST)
    elif remote:
        remote_cond = Condition(remote_label, HOLDING, "lost", tag=REMOTE_IO_LOST,
                                why="the remote I/O on the source deck is lost")
    else:
        remote_cond = Condition(remote_label, OK, "not reported lost" + latched, tag=REMOTE_IO_LOST)
    conds = [
        _flag_cond(r, "Safety relay closed", RELAY, "closed", "open: the supplies have no power",
                   "the safety relay is open", see="relay"),
        remote_cond,
        _switch(r, "Source coolant flow", SRC_COOLANT, "flowing", "flow low", "the source coolant flow is low"),
        _flag_cond(r, "Vacuum good (the PC's flag)", VAC_OK, "good", "not good", "the vacuum isn't good",
                   see="gv_open"),
    ]
    permissive = _all_ok(conds)
    conds.append(Condition("Contactors may close", UNKNOWN if permissive is None else ON if permissive else OFF,
                           "not all known" if permissive is None else "yes" if permissive else "no", kind=OUTPUT))
    section = "Each supply, as the PLC has it"
    # Not a gate on the contactors: while a sequence runs the PLC drives the supplies' setpoints itself
    auto, step = r.flag(AUTO_SHUTDOWN), r.step()
    running = None if auto is None or step is None else bool(auto or step)
    at = f"at {_step_name(step)}" if step else "running"
    conds.append(Condition("Shutdown sequence", INFO, NO_PLC if running is None else
                           f"{at}: the PLC drives the supplies itself" if running else "none", kind=PLC,
                           tag=AUTO_SHUTDOWN, section=section))
    for name, label in SOURCE_SUPPLIES:
        base = f"{SRC}.{name}"
        lockout, mode, on = r.flag(f"{base}.stat_lockout"), r.num(f"{base}.ctrl_mode"), r.flag(f"{base}.stat_enabled")
        if lockout is None:
            conds.append(Condition(label, UNKNOWN, NO_PLC, kind=PLC, tag=f"{base}.stat_lockout", section=section))
            continue
        state = "on" if on else "off" if on is not None else "?"
        if lockout:
            conds.append(Condition(label, INFO, f"{state}; locked out by the PLC", kind=PLC,
                                   tag=f"{base}.stat_lockout", section=section))
        elif mode and int(mode) in CTRL_MODES:
            conds.append(Condition(label, INFO, f"{state}; the PLC drives it ({CTRL_MODES[int(mode)]})", kind=PLC,
                                   tag=f"{base}.ctrl_mode", section=section))
        else:
            conds.append(Condition(label, INFO, f"{state}; free", kind=PLC, tag=f"{base}.stat_lockout",
                                   section=section))
    g = Group("supplies", "Source supplies",
              "The target (cathode), filament and thermionic contactors can close only while all of these hold; the "
              "extraction supply needs only the relay.", conds)
    first = next((c for c in conds if c.gates and c.state == HOLDING), None)
    if first is not None:
        g.status = "HELD"
        g.level = LEVEL_NORMAL if first.tag == RELAY else LEVEL_ATTENTION
        g.summary = f"Held off: {first.reason()}."
    elif permissive is None:
        g.status, g.level, g.summary = "UNKNOWN", LEVEL_UNKNOWN, "Not all of the conditions are known."
    else:
        g.status, g.level, g.summary = "AVAILABLE", LEVEL_NORMAL, "The PLC lets the contactors close."
        if running:
            g.summary += f" A shutdown sequence is {at}: the PLC drives the supplies itself."
    return g


def _cesium_group(r: _Read) -> Group:
    auto, force = r.flag(AUTO_SHUTDOWN), r.flag(FORCE_COOLER)
    seq_label = "No shutdown or forced cooling"
    if auto:
        seq = Condition(seq_label, HOLDING, "a shutdown sequence holds the oven at 20 °C and cools it",
                        tag=AUTO_SHUTDOWN, why="a shutdown sequence holds the oven at 20 °C and cools it")
    elif force:
        # A shutdown cancelled at step 10 leaves the sequencer's forced cooling on (plc.md, known issues)
        seq = Condition(seq_label, HOLDING, f"forced cooling left on by a shutdown sequence ({RESET} clears it)",
                        tag=FORCE_COOLER, why=f"forced cooling is still on from a shutdown sequence ({RESET} "
                        "clears it)")
    elif auto is None or force is None:
        seq = Condition(seq_label, UNKNOWN, NO_PLC, tag=AUTO_SHUTDOWN if auto is None else FORCE_COOLER)
    else:
        seq = Condition(seq_label, OK, "none", tag=AUTO_SHUTDOWN)
    conds = [
        _flag_cond(r, "Safety relay closed", RELAY, "closed", "open: the oven is on the source deck",
                   "the safety relay is open", see="relay"),
        _no_fault(r, "No caesium oven fault", ["Cesium_Oven_OverTemp", "Cesium_Oven_HeatFail", "Cesium_Oven_CoolFail"]),
        seq,
    ]
    lockout_computed = _all_ok(conds[:2])
    lockout_computed = None if lockout_computed is None else not lockout_computed
    heater = r.num(f"{CESIUM}.cmd_ctrl_mode")
    request = Condition("HEATER ON", UNKNOWN if heater is None else ON if int(heater) == 1 else OFF,
                        NO_PLC if heater is None else "requested" if int(heater) == 1 else
                        "not requested (TESS Ion Source panel)", kind=REQUEST, tag=f"{CESIUM}.cmd_ctrl_mode")
    lockout, permissive = r.flag(f"{CESIUM}.stat_lockout"), r.flag(CS_HEATING_PERMISSIVE)
    bits = Condition("The PLC's verdict", INFO, NO_PLC if lockout is None else (
        "locked out" if lockout else "not locked out") + (
        "" if permissive is None else f"; heating permissive {'on' if permissive else 'off'}"), kind=PLC,
        tag=f"{CESIUM}.stat_lockout")
    on, cooling = r.flag(f"{CESIUM}.stat_enabled"), r.flag(f"{CESIUM}.stat_cooling_active")
    est = r.num(f"{CESIUM}.estimated_temp")
    output = Condition("Heater", UNKNOWN if on is None else ON if on else OFF, (
        NO_PLC if on is None else "heating" if on else "off") + ("; cooling air on" if cooling else "") + (
        f" ({est:.0f} °C estimated)" if est is not None else ""), kind=OUTPUT, tag=f"{CESIUM}.stat_enabled")
    g = Group("cesium", "Caesium oven", "The PLC heats the oven only while all of these hold.",
              conds + [request, bits, output])
    first = next((c for c in conds if c.state == HOLDING), None)
    if on is None and first is None:
        g.status, g.level, g.summary = "UNKNOWN", LEVEL_UNKNOWN, "No data from the PLC."
    elif on:
        g.status, g.level, g.summary = "HEATING", LEVEL_ON, "Heating."
    elif first is not None:
        g.status = "LOCKED OUT" if first.kind == FAULT else "HELD"
        g.level = LEVEL_FAULT if first.kind == FAULT else (LEVEL_ATTENTION if request.state == ON else LEVEL_NORMAL)
        g.summary = f"Can't heat: {first.reason()}."
    elif request.state == ON:
        g.status, g.level, g.summary = "ON", LEVEL_ON, "Heater on: the PLC heats towards the setpoint."
    else:
        g.status, g.level, g.summary = "OFF", LEVEL_NORMAL, "The heater can be switched on."
    if lockout_computed is not None and lockout is not None and lockout_computed != lockout:
        g.disagree = (f"The PLC's lockout is {'set' if lockout else 'clear'}, but by the conditions listed it should "
                      f"be {'clear' if lockout else 'set'}: the PLC program may differ from its description.")
    return g


# --- Faults, and the PC's side ----------------------------------------------------------------------
def _reset_group(r: _Read, fault_text: Dict[str, str]) -> Group:
    conds = []
    for label, tag, allowed in (("Remote I/O sequence at step 0, 3 or 99", REMOTE_STEP, RESET_REMOTE_STEPS),
                                ("PC sequence at step 0, 2, 3 or 99", PC_STEP, RESET_PC_STEPS)):
        value = r.num(tag)
        if value is None:
            conds.append(Condition(label, UNKNOWN, NO_PLC, tag=tag))
            continue
        v = int(value)
        text = f"step {v}: {_step_name(v)}" if v else "not running"
        conds.append(Condition(label, OK if v in allowed else HOLDING, text, tag=tag,
                               why=f"a shutdown sequence is at {_step_name(v)}"))
    conds.append(_flag_cond(r, "PC watchdog present", COMMS_LOST, "present", "lost", "the PLC has lost the PC's watchdog",
                            invert=True))
    accepted = _all_ok(conds)
    conds.append(Condition("RESET FAULTS", UNKNOWN if accepted is None else ON if accepted else OFF,
                           NO_PLC if accepted is None else "accepted now" if accepted else "refused now",
                           kind=OUTPUT))
    latched = sorted(k[len("faults."):] for k in r.d if k.startswith("faults.") and r.flag(k))
    conds += [Condition(name, INFO, fault_text.get(name, ""), kind=PLC, tag=f"faults.{name}", section="Latched now")
              for name in latched]
    g = Group("reset", "Fault reset", f"{RESET} clears the PLC's latched faults only while all of these hold.", conds)
    first = next((c for c in conds if c.gates and c.state == HOLDING), None)
    n = len(latched)
    if accepted is None:
        g.status, g.level, g.summary = "UNKNOWN", LEVEL_UNKNOWN, "No data from the PLC."
    elif first is not None:
        g.status, g.level = "REFUSED NOW", LEVEL_ATTENTION if n else LEVEL_NORMAL
        g.summary = f"A reset would be refused: {first.reason()}."
    else:
        g.status = f"{n} LATCHED" if n else "NONE LATCHED"
        g.level = LEVEL_FAULT if n else LEVEL_NORMAL
        g.summary = (f"{n} fault{'s' if n != 1 else ''} latched; a reset would be accepted." if n else
                     "No PLC faults latched.")
        pc_step = r.num(PC_STEP)
        if pc_step is not None and int(pc_step) == 2:
            g.notes.append((f"A reset now would also end the PC sequence's cool-down ({_step_name(2)}), with the "
                            "caesium possibly still hot: the PLC accepts a reset at that step (see the PLC page's "
                            "known issues).", "warn"))
    return g


def _hmi_group(r: _Read) -> Group:
    """What the services say about operator commands (src/core/device_service.py): one line per
    reason, with the subsystems it applies to, and the sequences holding controls (leases)."""
    from src.core.recipe_catalog import subsystem_name
    subs = sorted(k[:-len(".permit")] for k in r.d if k.endswith(".permit"))
    by_reason, held, free = {}, {}, []
    for sub in subs:
        holder = r.d.get(f"{sub}.lease_holder")
        if holder:
            held.setdefault(str(r.d.get(f"{sub}.lease_purpose") or holder), []).append(sub)
        elif r.flag(f"{sub}.permit"):
            free.append(sub)
        else:
            by_reason.setdefault(str(r.d.get(f"{sub}.permit_reason") or "not available"), []).append(sub)

    def names(group) -> str:
        found = []
        for s in group:
            n = subsystem_name(s) or s.rsplit(".", 1)[-1].replace("_", " ")
            if n not in found:
                found.append(n)
        text = ", ".join(found)
        return text[:1].upper() + text[1:]

    conds = [Condition(names(group), INFO, f"held by {purpose}", kind=LEASE)
             for purpose, group in sorted(held.items())]
    conds += [Condition(names(group), HOLDING, reason)
              for reason, group in sorted(by_reason.items(), key=lambda kv: (-len(kv[1]), kv[0]))]
    if free:
        label = f"{len(free)} other{'s' if len(free) != 1 else ''}" if conds else f"All {len(free)}"
        conds.append(Condition(label, OK, "take commands"))
    g = Group("hmi", "Control from the HMI",
              "Each service says whether it would take an operator's command now, and which sequence holds which "
              "controls.", conds, chain=False)
    if not subs:
        g.status, g.level, g.summary = "UNKNOWN", LEVEL_UNKNOWN, "No service is reporting."
        return g
    blocked = sum(len(v) for v in by_reason.values())
    n_held = sum(len(v) for v in held.values())
    g.status = f"{len(free)} OF {len(subs)} FREE"
    parts = [f"{blocked} refuse commands" if blocked else "", f"{n_held} held by a sequence" if n_held else ""]
    text = "; ".join(p for p in parts if p)
    g.summary = f"{text[:1].upper()}{text[1:]}." if text else "Every service takes commands."
    return g


def evaluate(data: dict, loop: LoopConfig = None, fault_text: Dict[str, str] = None, now: float = None) -> Overview:
    """Every group, from one fresh telemetry view (TelemetryCache.view()). `now`: epoch seconds
    (time.time() if not given), to judge how old the gauge relays' readings are."""
    r = _Read(data or {}, now)
    loop = loop if loop is not None else LoopConfig()
    online = bool(r.d.get(PLC_ONLINE))
    state = r.num(MACHINE_STATE)
    if not online or state is None:
        name, machine, level = "PLC OFFLINE", "No data from the PLC: every condition it reports is unknown.", \
            LEVEL_UNKNOWN
    else:
        s = int(state)
        name = MACHINE_STATES.get(s, f"STATE {s}").upper()
        machine = f"Machine state {s}. {MACHINE_MEANS.get(s, '')}".strip()
        level = LEVEL_FAULT if s == 99 else LEVEL_ATTENTION if s == 3 else LEVEL_NORMAL
        if r.flag(COMMS_LOST):
            machine += " The PLC has lost the PC's watchdog."
            level = LEVEL_FAULT
        step = r.step()
        if step:
            machine += f" Shutdown sequence at {_step_name(step)}."
            level = LEVEL_FAULT if level == LEVEL_FAULT else LEVEL_ATTENTION
    groups = [_relay_group(r, loop, r.flag(GV_CLOSE_PERMISSIVE)), _gv_open_group(r), _source_cold_group(r),
              _supplies_group(r), _cesium_group(r), _reset_group(r, fault_text or {}), _hmi_group(r)]
    if loop.problem:
        groups[0].notes.append((f"The loop's gauge relay: {loop.problem}.", "warn"))
    return Overview(online, name, machine, level, groups)


def fault_descriptions(fault_structures: dict) -> Dict[str, str]:
    """{fault name: description} from fault_config.json (FaultRegistry.fault_structures)."""
    out = {}
    for udt in (fault_structures or {}).values():
        bits = udt.get("bits") if isinstance(udt, dict) else None
        for bit in (bits or {}).values():
            if isinstance(bit, dict) and bit.get("name"):
                out[bit["name"]] = str(bit.get("description", ""))
    return out


class Disagreements:
    """A group whose PLC verdict and conditions disagree is flagged only once they have disagreed for
    `hold_s` in a row: the PLC's bits and the PC's flags arrive from different services, and differ
    for a moment while something changes."""

    def __init__(self, hold_s: float = 3.0, clock=time.monotonic):
        self.hold_s = hold_s
        self.clock = clock
        self._since = {}
        self._last = None

    def filter(self, overview: Overview) -> Overview:
        now = self.clock()
        if self._last is None or now - self._last > 1.0:
            self._since.clear()         # not called for a while (the panel was hidden): start again
        self._last = now
        for g in overview.groups:
            if g.disagree:
                if now - self._since.setdefault(g.key, now) < self.hold_s:
                    g.disagree = ""
            else:
                self._since.pop(g.key, None)
        return overview


def config_mtime(path: str = None) -> Optional[float]:
    try:
        return os.path.getmtime(path or paths.config_path("vac_gauges_config.json"))
    except OSError:
        return None
