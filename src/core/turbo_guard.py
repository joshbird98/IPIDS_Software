"""The source turbo pump's guard: an early warning of an overload, then a source shutdown in good time.

On 26 October 2026 the valve between the source turbo and its backing pump was closed by mistake,
at 10:22. The turbo ran on at full speed for 13 hours, unaware of it, while gas built up behind it:
its motor current went from 0.8 A to 1.2 A at once, then crept up (1.5 A by 12:15, 2 A by 15:45,
4 A by 22:00) and climbed fast at the end (6.2 A at 23:19); the bearing went from 43 to 65 degC and
the converter from 28 to 53 degC, slowly for about 8 hours and then quickly. The source vacuum (VG1)
hardly moved. The pump's own warning came at 22:37, 43 minutes before it failed (23:20) and the
source went up to about 1 mbar - too late for the source to cool before it got air.

The guard watches for that kind of load, at full speed, and acts in stages (the times are
turbo_guard.json's: the warning's from the start of the overload, the others from the warning):

    WARNING    the current `current_margin_a` above normal for `warn_after_s` (2 min) - or a bearing /
               converter `temp_margin_c` above normal, or the pump's own warning, for as long. An
               alarm: check the backing valve and the backing pump.
    CRITICAL   the current still above normal `critical_after_s` (10 min) after the warning, or the
               pump's own warning on top of it: a critical alarm, with the time of the shutdown.
    TRIPPED    `trip_after_s` (20 min) after the warning - or `pump_warning_trip_s` (10 min) after the
               pump's own warning, if that is sooner - unless the current is falling back; or at once,
               from WARNING or CRITICAL, if the overloaded pump reports an error or slows down. The
               source is shut down (the PLC's shutdown sequence, which then can't be aborted) and the
               caesium oven's cooling air forced on, while the source still has hours in hand.
               Latched: RESET FAULTS clears it once the load is back to normal (or the pump stopped).

The turbo itself is never stopped: trained staff stop it by hand, at the pump.

Temperatures alone (a warm room, the cooling water) only warn: a shutdown needs the current - the
load itself. Falling back means the current's excess over normal down by at least `falling_fraction`
from its peak in the last `falling_window_s` (reopening the valve does that within a minute; the
temperatures follow much more slowly). Readings are judged on their median over `smooth_s`, so a gas
burst (2.3 A for a second at the rod change) is ignored, and only once the pump has been at full speed
for `settle_s` (a run-up draws more) - or at once if it already was when the service started. While
the pump isn't answering, the guard holds where it is.

No Qt, no other SINDIE imports: used by service_source_turbo and the config editor's checks.
"""
import math
import statistics
import time
from collections import deque
from dataclasses import dataclass, fields, replace
from datetime import datetime
from typing import Optional

NORMAL, WARNING, CRITICAL, TRIPPED = 0, 1, 2, 3
STAGE_NAMES = {NORMAL: "normal", WARNING: "warning", CRITICAL: "critical", TRIPPED: "tripped"}
CONFIG_FILE = "turbo_guard.json"
SAMPLE_S = 1.0          # the readings are looked at once a second
HISTORY_S = 10.0        # the smoothed current is kept every 10 s, for "falling back"
OVER_GRACE_S = 60.0     # the current dipping under the line for less than this doesn't restart the clock


@dataclass(frozen=True)
class GuardConfig:
    enabled: bool = True
    shut_down_source: bool = True       # TRIPPED shuts the source down; false: the alarms only
    normal_current_a: float = 0.8       # at full speed, backing line open (26 Oct, overnight: 0.7-0.8 A)
    normal_bearing_c: float = 43.0
    normal_converter_c: float = 28.0
    current_margin_a: float = 0.3       # this far above normal is an overload (26 Oct: +0.4 A at once)
    temp_margin_c: float = 5.0          # the bearing or converter this far above normal: running hot
    at_speed_pct: float = 98.0
    settle_s: float = 600.0
    smooth_s: float = 60.0
    warn_after_s: float = 120.0
    critical_after_s: float = 600.0     # after the warning
    trip_after_s: float = 1200.0        # after the warning
    pump_warning_trip_s: float = 600.0  # after the pump's own warning, while overloaded
    falling_fraction: float = 0.3
    falling_window_s: float = 600.0
    clear_after_s: float = 300.0

    def describe(self) -> str:
        if not self.enabled:
            return "off (enabled is false in turbo_guard.json)"
        action = (f"the source shut down {self.trip_after_s / 60:g} min after the warning unless it is falling back"
                  if self.shut_down_source else "no source shutdown (shut_down_source is false)")
        return (f"normal {self.normal_current_a:g} A, bearing {self.normal_bearing_c:g} degC, converter "
                f"{self.normal_converter_c:g} degC; a warning at +{self.current_margin_a:g} A (or "
                f"+{self.temp_margin_c:g} degC) for {self.warn_after_s / 60:g} min, critical "
                f"{self.critical_after_s / 60:g} min later, {action}")


# (key, kind, lowest, highest): the range of each turbo_guard.json setting
LIMITS = (
    ("enabled", bool, None, None), ("shut_down_source", bool, None, None),
    ("normal_current_a", float, 0.1, 10.0), ("normal_bearing_c", float, 10.0, 90.0),
    ("normal_converter_c", float, 10.0, 90.0), ("current_margin_a", float, 0.1, 5.0),
    ("temp_margin_c", float, 1.0, 40.0), ("at_speed_pct", float, 50.0, 100.0),
    ("settle_s", float, 0.0, 3600.0), ("smooth_s", float, 5.0, 600.0),
    ("warn_after_s", float, 10.0, 3600.0), ("critical_after_s", float, 0.0, 7200.0),
    ("trip_after_s", float, 60.0, 14400.0), ("pump_warning_trip_s", float, 0.0, 7200.0),
    ("falling_fraction", float, 0.05, 0.95), ("falling_window_s", float, 60.0, 7200.0),
    ("clear_after_s", float, 0.0, 3600.0),
)


def parse(cfg) -> tuple:
    """(GuardConfig, [problems]) from turbo_guard.json's contents. A missing setting takes its
    default; so does one with a problem (the problem says which)."""
    problems, values = [], {}
    if cfg is None:
        return GuardConfig(), problems
    if not isinstance(cfg, dict):
        return GuardConfig(), [f"{CONFIG_FILE}: must be an object such as {{\"normal_current_a\": 0.8}}"]
    known = {name for name, *_ in LIMITS}
    for key in cfg:
        if not str(key).startswith("_") and key not in known:
            problems.append(f"{key}: not a setting (known: {', '.join(sorted(known))})")
    for key, kind, lo, hi in LIMITS:
        if key not in cfg:
            continue
        v = cfg[key]
        if kind is bool:
            if isinstance(v, bool):
                values[key] = v
            else:
                problems.append(f"{key}: must be true or false (is {v!r})")
        elif isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            problems.append(f"{key}: must be a number (is {v!r})")
        elif not lo <= v <= hi:
            problems.append(f"{key}: must be {lo:g} to {hi:g} (is {v:g})")
        else:
            values[key] = float(v)
    c = replace(GuardConfig(), **values)
    if c.critical_after_s > c.trip_after_s:
        problems.append(f"critical_after_s ({c.critical_after_s:g}) must be at most trip_after_s "
                        f"({c.trip_after_s:g}): the critical alarm comes before the shutdown")
        c = replace(c, critical_after_s=c.trip_after_s)
    return c, problems


def config_dict(c: GuardConfig) -> dict:
    return {f.name: getattr(c, f.name) for f in fields(c)}


@dataclass
class Reading:
    """One look at the pump. None: not known (not read yet, or the pump not answering)."""
    speed_pct: Optional[float] = None
    current_a: Optional[float] = None
    bearing_c: Optional[float] = None
    converter_c: Optional[float] = None
    pump_warning: bool = False          # its collective, overload or temperature warning
    pump_error: bool = False
    turning: bool = False


class _Median:
    """The median of the values seen in the last `span` seconds."""

    def __init__(self, span: float):
        self.span = span
        self.items = deque()

    def add(self, t: float, v):
        if v is not None and math.isfinite(v):
            self.items.append((t, float(v)))
        while self.items and self.items[0][0] < t - self.span:
            self.items.popleft()

    def value(self):
        return statistics.median(v for _, v in self.items) if self.items else None


def _clock(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%H:%M")


class TurboGuard:
    """update(now, Reading) every cycle (it looks once a second) returns the events worth logging,
    as (severity, text); .stage, .message and .shutdown_at say where it is."""

    def __init__(self, config: GuardConfig = None, clock=time.time):
        self.config = config or GuardConfig()
        self.clock = clock
        c = self.config
        self.current, self.bearing, self.converter = _Median(c.smooth_s), _Median(c.smooth_s), _Median(c.smooth_s)
        self.history = deque()              # (t, smoothed current), for falling back
        self.values = {}                    # the latest smoothed readings
        self.stage = NORMAL
        self.message = ""                   # what the stage is about: the alarm's text and the panel's
        self.shutdown_at = None             # when the source will be shut down if nothing changes
        self.tripped_at = None
        self.trip_reason = ""
        self._last_look = None
        self._first = True
        self._at_speed_since = None
        self._over_since = None             # the current above the line since (dips < 60 s forgiven)
        self._under_since = None
        self._hot_since = None
        self._pump_since = None             # the pump's own warning, at speed
        self._pump_over_since = None        # ...while the pump is overloaded
        self._clear_since = None
        self._peak_excess = 0.0

    # ------------------------------------------------------------------ evidence
    def _look(self, now: float, r: Reading):
        c = self.config
        at_speed = r.speed_pct >= c.at_speed_pct and not r.pump_error
        if at_speed and self._at_speed_since is None:
            # already at full speed when the service started: it hasn't just run up
            self._at_speed_since = now - c.settle_s if self._first else now
        elif not at_speed:
            self._at_speed_since = None
        self._first = False
        self.current.add(now, r.current_a)
        self.bearing.add(now, r.bearing_c)
        self.converter.add(now, r.converter_c)
        cur = self.current.value()
        if cur is not None and (not self.history or now - self.history[-1][0] >= HISTORY_S):
            self.history.append((now, cur))
        while self.history and self.history[0][0] < now - c.falling_window_s:
            self.history.popleft()
        self.values = {"current": cur, "bearing": self.bearing.value(), "converter": self.converter.value()}

    def settled(self, now: float) -> bool:
        return self._at_speed_since is not None and now - self._at_speed_since >= self.config.settle_s

    def excess(self):
        cur = self.values.get("current")
        return None if cur is None else cur - self.config.normal_current_a

    def overloaded(self, now: float) -> bool:
        e = self.excess()
        return self.settled(now) and e is not None and e >= self.config.current_margin_a - 1e-9

    def hot(self, now: float) -> bool:
        c, v = self.config, self.values
        return self.settled(now) and any(
            x is not None and x >= normal + c.temp_margin_c - 1e-9
            for x, normal in ((v.get("bearing"), c.normal_bearing_c), (v.get("converter"), c.normal_converter_c)))

    def falling(self) -> bool:
        """The current's excess down by falling_fraction from its peak in the last falling_window_s."""
        e = self.excess()
        if e is None or not self.history:
            return False
        peak = max(v for _, v in self.history) - self.config.normal_current_a
        return peak > 0 and e <= (1.0 - self.config.falling_fraction) * peak + 1e-9

    def due(self):
        """When it trips - the source shut down - if the current doesn't fall back (None: not overloaded)."""
        c = self.config
        if self._over_since is None or self.stage == TRIPPED:
            return None
        due = self._over_since + c.warn_after_s + c.trip_after_s
        if self._pump_over_since is not None:
            due = min(due, self._pump_over_since + c.pump_warning_trip_s)
        return due

    # ------------------------------------------------------------------ stages
    def update(self, now: float = None, reading: Reading = None) -> list:
        now = self.clock() if now is None else now
        if not self.config.enabled or reading is None or reading.speed_pct is None:
            return []                       # (the pump not answering: hold where it is)
        if self.stage == TRIPPED or (self._last_look is not None and now - self._last_look < SAMPLE_S):
            if self.stage == TRIPPED:
                self._look(now, reading)    # (the readings stay current, for the reset)
            return []
        self._last_look = now
        self._look(now, reading)
        c = self.config
        over, hot = self.overloaded(now), self.hot(now)
        if over:
            self._under_since = None
            self._over_since = self._over_since or now
            self._peak_excess = max(self._peak_excess, self.excess())
        elif self._over_since is not None:
            self._under_since = self._under_since or now
            if now - self._under_since >= OVER_GRACE_S:
                self._over_since = self._under_since = None
        self._hot_since = (self._hot_since or now) if hot else None
        pump = reading.pump_warning and self.settled(now)
        self._pump_since = (self._pump_since or now) if pump else None
        self._pump_over_since = ((self._pump_over_since or now) if pump and self._over_since is not None
                                 else None)
        load_for = None if self._over_since is None else now - self._over_since
        events = []

        if self.stage in (WARNING, CRITICAL) and self._peak_excess >= c.current_margin_a - 1e-9:
            failing = reading.pump_error or (reading.turning and reading.speed_pct < c.at_speed_pct - 5.0)
            if failing:
                return self._trip(now, "the overloaded pump is failing: " + (
                    "it reports an error" if reading.pump_error else f"down to {reading.speed_pct:.0f} % speed"))
            due = self.due()
            if due is not None and now >= due and over and not self.falling():
                if self._pump_over_since is not None and due < self._over_since + c.warn_after_s + c.trip_after_s:
                    why = (f"overloaded for {load_for / 60:.0f} min, the pump's own warning on for "
                           f"{(now - self._pump_over_since) / 60:.0f} min, and the current not falling back")
                else:
                    why = f"overloaded for {load_for / 60:.0f} min and the current not falling back"
                return self._trip(now, why)

        if self.stage == NORMAL:
            if load_for is not None and load_for >= c.warn_after_s:
                events += self._to(WARNING, now)
            elif self._hot_since is not None and now - self._hot_since >= c.warn_after_s:
                events += self._to(WARNING, now)
            elif self._pump_since is not None and now - self._pump_since >= c.warn_after_s:
                events += self._to(WARNING, now)
        if self.stage == WARNING and load_for is not None and (
                load_for >= c.warn_after_s + c.critical_after_s or self._pump_over_since is not None):
            events += self._to(CRITICAL, now)
        elif self.stage == CRITICAL and self._over_since is None:
            events += self._to(WARNING, now)    # the current is back, but it is still running hot
        if self.stage in (WARNING, CRITICAL):
            if over or hot or pump or self._over_since is not None:
                self._clear_since = None
            else:
                self._clear_since = self._clear_since or now
                if now - self._clear_since >= c.clear_after_s:
                    events += self._to(NORMAL, now)
        self.shutdown_at = self.due() if self.stage in (WARNING, CRITICAL) and c.shut_down_source else None
        if self.stage in (WARNING, CRITICAL):
            self.message = self._describe(now)
        return events

    def _to(self, stage: int, now: float) -> list:
        old, self.stage = self.stage, stage
        if stage == NORMAL:
            self._clear_since, self._peak_excess, self.message, self.shutdown_at = None, 0.0, "", None
            return [("INFO", f"Source turbo back to normal ({self._readings()}): its {STAGE_NAMES[old]} "
                             f"alarm has cleared")]
        self.message = self._describe(now)
        if stage == WARNING and old == CRITICAL:
            return [("WARNING", f"Source turbo current back to normal ({self._readings()}): no source shutdown")]
        return [("WARNING" if stage == WARNING else "ERROR", self.message)]

    def _trip(self, now: float, why: str) -> list:
        self.stage, self.tripped_at, self.trip_reason, self.shutdown_at = TRIPPED, now, why, None
        action = ("the source is being shut down (the PLC's shutdown sequence: it can't be aborted) and the "
                  "caesium oven's cooling air forced on" if self.config.shut_down_source
                  else "no source shutdown (shut_down_source is false in turbo_guard.json)")
        self.message = (f"Source turbo protection tripped at {_clock(now)}: {action}. Why: {why} "
                        f"({self._readings()}). The turbo is left running - it is stopped only by hand, at the "
                        f"pump. Check the valve between it and its backing pump, and the backing pump; RESET "
                        f"FAULTS once its load is back to normal.")
        return [("CRITICAL", self.message)]

    def _readings(self) -> str:
        v, c = self.values, self.config
        parts = []
        if v.get("current") is not None:
            parts.append(f"{v['current']:.1f} A, normally {c.normal_current_a:g} A")
        temps = [f"{name} {v[name]:.0f} degC ({normal:g})" for name, normal in
                 (("bearing", c.normal_bearing_c), ("converter", c.normal_converter_c)) if v.get(name) is not None]
        if temps:
            parts.append(", ".join(temps))
        return "; ".join(parts) or "no readings"

    def _describe(self, now: float) -> str:
        """The alarm's text: what, the shutdown time (first, as a narrow alarm list cuts it short),
        what to check, the readings."""
        c, due = self.config, self.due()
        if self._over_since is not None:
            minutes = (now - self._over_since) / 60
            what = "Source turbo still overloaded" if self.stage == CRITICAL else "Source turbo working harder than normal"
            if self._pump_over_since is not None:
                what += ", and it reports a warning"
            if due is not None and c.shut_down_source:
                what += (f": the source will be shut down at {_clock(due)} (in {max(0.0, due - now) / 60:.0f} min) "
                         f"unless its current falls back")
            elif due is not None:
                what += (f": it would trip at {_clock(due)} unless its current falls back (no automatic source "
                         f"shutdown: shut_down_source is false in turbo_guard.json)")
            readings = f"{self._readings()}; for {minutes:.0f} min" if minutes >= 1 else self._readings()
        elif self._hot_since is not None:
            what, readings = "Source turbo running hot", self._readings()
        elif self._pump_since is not None:
            what, readings = "Source turbo reports a warning", self._readings()
        else:
            return (f"Source turbo back to normal ({self._readings()}): the alarm clears once it has stayed so for "
                    f"{c.clear_after_s / 60:g} min")
        return f"{what}. Check the valve between it and its backing pump, and the backing pump. ({readings})"

    # ------------------------------------------------------------------ reset, restart
    def reset(self, now: float = None, answering: bool = True) -> tuple:
        """(done, reason) for RESET FAULTS: a trip is cleared once the current is back to normal (the
        temperatures take longer: they don't hold it), or the pump has stopped or isn't answering.
        The alarms of an overload still going on can't be reset away."""
        now = self.clock() if now is None else now
        if self.stage != TRIPPED:
            return True, "nothing to reset"
        if answering and self.overloaded(now):
            return False, (f"the source turbo is still overloaded ({self._readings()}): its protection stays "
                           f"tripped until its load is back to normal")
        self.stage, self.message, self.tripped_at, self.trip_reason = NORMAL, "", None, ""
        self._over_since = self._under_since = self._hot_since = self._pump_since = self._pump_over_since = None
        self._clear_since, self._peak_excess = None, 0.0
        return True, "source turbo protection reset"

    def dump(self) -> dict:
        """What must survive a restart of the service: a trip, and an overload's clock."""
        return {"stage": self.stage, "saved_at": self.clock(), "tripped_at": self.tripped_at,
                "trip_reason": self.trip_reason, "message": self.message if self.stage == TRIPPED else "",
                "over_since": self._over_since, "pump_over_since": self._pump_over_since,
                "peak_excess": self._peak_excess}

    def restore(self, saved: dict, max_age_s: float = 900.0):
        """A trip comes back however old it is (it is latched); an overload's clock only if it was
        saved recently (the service restarted), so it carries on rather than starting again."""
        if not isinstance(saved, dict):
            return
        stage = saved.get("stage")
        if stage == TRIPPED:
            self.stage, self.tripped_at = TRIPPED, saved.get("tripped_at")
            self.trip_reason, self.message = saved.get("trip_reason") or "", saved.get("message") or ""
            return
        try:
            age = self.clock() - float(saved.get("saved_at") or 0.0)
        except (TypeError, ValueError):
            return
        if stage in (WARNING, CRITICAL) and 0 <= age <= max_age_s and saved.get("over_since"):
            self.stage = stage
            self._over_since = float(saved["over_since"])
            po = saved.get("pump_over_since")
            self._pump_over_since = float(po) if po else None
            self._peak_excess = float(saved.get("peak_excess") or 0.0)
