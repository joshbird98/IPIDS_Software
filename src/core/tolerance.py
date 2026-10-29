"""Setpoint-versus-readback tolerance checks (config/readback_tolerance.json).

Deliberately NOT a fault: some readbacks are expected to sit slightly off their
setpoint. A rule whose readback stays outside its tolerance for `delay_s`
produces one TOLERANCE event in events.db (and one more when it comes back),
and its flag 'monitor.tolerance.<readback tag>' goes to 1 so the HMI can
colour that readback. Nothing is tripped or stopped.

Rule fields (unset ones come from "defaults"):
    name          label used in events
    setpoint      tag of the setpoint the device reports it is using
    readback      tag of the measured value
    abs_tol       allowed |readback - setpoint| in the tags' units ...
    rel_tol       ... or this fraction of |setpoint|, whichever is larger
    delay_s       how long it must stay out before it counts
    only_when     tag that must be 1 for the rule to apply (e.g. output enabled)
    min_setpoint  ignore the rule while |setpoint| is below this
    enabled       false to switch a rule off
"""
import logging
import os
import time

from src.core import lab_config

log = logging.getLogger("tolerance")

FLAG_PREFIX = "monitor.tolerance."
_DEFAULTS = {"abs_tol": 0.0, "rel_tol": 0.05, "delay_s": 5.0, "only_when": None, "min_setpoint": 0.0,
             "enabled": True}


class ToleranceRule:
    __slots__ = ("name", "setpoint", "readback", "abs_tol", "rel_tol", "delay_s", "only_when", "min_setpoint",
                 "enabled", "out_since", "alarmed")

    def __init__(self, spec: dict, defaults: dict = None):
        merged = dict(_DEFAULTS)
        merged.update(defaults or {})
        merged.update(spec)
        self.setpoint = merged["setpoint"]
        self.readback = merged["readback"]
        self.name = merged.get("name") or self.readback
        self.abs_tol = float(merged["abs_tol"])
        self.rel_tol = float(merged["rel_tol"])
        self.delay_s = float(merged["delay_s"])
        self.only_when = merged.get("only_when")
        self.min_setpoint = float(merged.get("min_setpoint") or 0.0)
        self.enabled = bool(merged.get("enabled", True))
        self.out_since = None
        self.alarmed = False

    def tolerance(self, setpoint: float) -> float:
        return max(self.abs_tol, self.rel_tol * abs(setpoint))


def load_rules(path: str):
    cfg = lab_config.load(path)             # (with this machine's lab settings)
    defaults = cfg.get("defaults", {})
    rules = []
    for spec in cfg.get("rules", []):
        try:
            rules.append(ToleranceRule(spec, defaults))
        except (KeyError, TypeError, ValueError) as e:
            log.error("Ignoring malformed tolerance rule %r: %s", spec, e)
    return rules


class ToleranceMonitor:
    def __init__(self, rules, events=None, clock=time.time):
        self.rules = list(rules)
        self.events = events
        self._clock = clock

    def replace_rules(self, rules):
        """Swap in an edited rule set, keeping the state of rules that did not change."""
        old = {(r.setpoint, r.readback): r for r in self.rules}
        for r in rules:
            prev = old.get((r.setpoint, r.readback))
            if prev is not None:
                r.out_since, r.alarmed = prev.out_since, prev.alarmed
        self.rules = list(rules)

    def _event(self, rule, severity, message, **meta):
        if self.events is not None:
            self.events.log_general(message, severity=severity, event_type="TOLERANCE",
                                    metadata={"rule": rule.name, "setpoint_tag": rule.setpoint,
                                              "readback_tag": rule.readback, **meta})

    def evaluate(self, get_fresh, now: float = None) -> dict:
        """get_fresh(tag) -> current value or None if unknown/stale. Returns the flag dict to publish."""
        now = self._clock() if now is None else now
        flags = {}
        for rule in self.rules:
            flag_key = FLAG_PREFIX + rule.readback
            if not rule.enabled:
                flags[flag_key] = 0.0
                continue
            gate_open = True
            if rule.only_when:
                gate = get_fresh(rule.only_when)
                gate_open = bool(gate) if gate is not None else False
            sp = get_fresh(rule.setpoint)
            rb = get_fresh(rule.readback)

            if not gate_open or sp is None or rb is None or abs(float(sp)) < rule.min_setpoint:
                # Not applicable right now (output off, data unknown): no alarm, no events
                if rule.alarmed and gate_open is False:
                    self._event(rule, "INFO", f"Readback tolerance cleared ({rule.name}): output disabled")
                rule.out_since = None
                rule.alarmed = False
                flags[flag_key] = 0.0
                continue

            sp, rb = float(sp), float(rb)
            tol = rule.tolerance(sp)
            deviation = rb - sp
            if abs(deviation) > tol:
                if rule.out_since is None:
                    rule.out_since = now
                if not rule.alarmed and now - rule.out_since >= rule.delay_s:
                    rule.alarmed = True
                    self._event(rule, "WARNING",
                                f"Readback out of tolerance ({rule.name}): readback {rb:.4g} vs setpoint {sp:.4g} "
                                f"(deviation {deviation:+.3g}, allowed +/-{tol:.3g}, for {now - rule.out_since:.0f} s)",
                                setpoint=sp, readback=rb, deviation=deviation, tolerance=tol)
            else:
                rule.out_since = None
                if rule.alarmed:
                    rule.alarmed = False
                    self._event(rule, "INFO", f"Readback back within tolerance ({rule.name}): {rb:.4g} vs {sp:.4g}",
                                setpoint=sp, readback=rb)
            flags[flag_key] = 1.0 if rule.alarmed else 0.0
        return flags


class RuleFileWatcher:
    """Reloads the rule file when it changes, so tolerances can be tuned without a restart."""

    def __init__(self, path: str, monitor: ToleranceMonitor, events=None, check_every: float = 2.0):
        self.path = path
        self.monitor = monitor
        self.events = events
        self.check_every = check_every
        self._mtime = None
        self._next_check = 0.0

    def poll(self, now: float = None):
        now = time.time() if now is None else now
        if now < self._next_check:
            return False
        self._next_check = now + self.check_every
        mtime = lab_config.mtime(self.path)     # (the newer of the file and its lab settings)
        if not mtime:
            if self._mtime is not None:
                log.warning("%s disappeared - keeping the last rules", self.path)
                self._mtime = None
            return False
        if mtime == self._mtime:
            return False
        try:
            rules = load_rules(self.path)
        except (OSError, ValueError) as e:
            msg = f"Could not load {os.path.basename(self.path)} ({e}) - keeping the previous rules"
            log.error(msg)
            if self.events is not None:
                self.events.log_general(msg, severity="WARNING")
            self._mtime = mtime   # don't retry a broken file every 2 s; wait for the next edit
            return False
        self.monitor.replace_rules(rules)
        first = self._mtime is None
        self._mtime = mtime
        active = sum(r.enabled for r in rules)
        log.info("%s %d tolerance rule(s) (%d enabled)", "Loaded" if first else "Reloaded", len(rules), active)
        if self.events is not None and not first:
            self.events.log_general(f"Readback tolerance rules reloaded ({active} enabled)")
        return True
