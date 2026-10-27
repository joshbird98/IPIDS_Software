"""The source turbo pump's guard: an early warning of an overload, then a source shutdown in good time.

On 26 October 2026 the valve between the source turbo and its backing pump was closed by mistake,
at 10:22. The turbo ran on at full speed for 13 hours, unaware of it, while gas built up behind it:
its motor current went from 0.8 A to 1.2 A at once, then crept up (1.3 A by 10:45, 1.5 A by 12:15,
2 A by 15:45, 4 A by 22:00) and climbed fast at the end (6.2 A at 23:19); the bearing went from 43 to
65 degC and the converter from 28 to 53 degC, slowly for about 8 hours and then quickly. The source
vacuum (VG1) hardly moved. The pump's own warning came at 22:37, 43 minutes before it failed (23:20)
and the source went up to about 1 mbar - too late for the source to cool before it got air.

The guard watches for that kind of load and acts in stages (the times are turbo_guard.json's; Josh
set the overload line and its time, 27 October 2026):

    WARNING    the current above `overload_current_a` (1.2 A) at full speed for `warn_after_s` (15 min)
               without a break; or the bearing above `bearing_limit_c` (65 degC) or the converter above
               `converter_limit_c` (50 degC) - too hot, whatever the room - or the pump's own warning,
               for `hot_after_s` (1 min). An alarm: check the backing valve and the backing pump (and
               the cooling, if it is too hot).
    CRITICAL   still overloaded `critical_after_s` (10 min) after the warning, or the pump's own warning
               on top of an overload: a critical alarm, with the time of the shutdown.
    TRIPPED    `trip_after_s` (20 min) after the warning - or `pump_warning_trip_s` (10 min) after the
               pump's own warning, if that is sooner - unless the current is falling back; or at once,
               from WARNING or CRITICAL, if the overloaded pump reports an error or slows down. The
               source is shut down (the PLC's shutdown sequence, which then can't be aborted) and the
               caesium oven's cooling air forced on, while the source still has hours in hand.
               Latched: RESET FAULTS clears it once the current is back to normal (or the pump stopped).

The turbo itself is never stopped: trained staff stop it by hand, at the pump.

Too hot, or the pump's own warning, on their own only warn: a shutdown needs the current - the load
itself. Falling back means the current's excess over normal (`normal_current_a`, 0.8 A) down by at
least `falling_fraction` from its peak in the last `falling_window_s` (reopening the valve does that
within a minute; the temperatures follow much more slowly). Readings are judged on their median over
`smooth_s`, so a gas burst (2.3 A for a second at the rod change) is ignored; a dip of the median under
the line for less than a minute isn't a break. The current counts only once the pump has been at full
speed for `settle_s` (a run-up draws 8 A for about 6 minutes) - or at once if it already was when the
service started; the temperature limits count at any speed. While the pump isn't answering, the guard
holds where it is.

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
    overload_current_a: float = 1.2     # above this at full speed: overloaded (26 Oct: 1.2 A at once, 1.3 by 10:45)
    bearing_limit_c: float = 65.0       # above either: too hot, whatever the room (normally 43 and 28 degC)
    converter_limit_c: float = 50.0
    at_speed_pct: float = 98.0
    settle_s: float = 600.0
    smooth_s: float = 60.0
    warn_after_s: float = 900.0         # overloaded this long, without a break: the warning
    hot_after_s: float = 60.0           # too hot, or the pump's own warning, this long: the warning
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
        return (f"a warning after {self.warn_after_s / 60:g} min above {self.overload_current_a:g} A at full "
                f"speed (normally {self.normal_current_a:g} A), or the bearing above {self.bearing_limit_c:g} degC or "
                f"the converter above {self.converter_limit_c:g} degC for {self.hot_after_s / 60:g} min; critical "
                f"{self.critical_after_s / 60:g} min later, {action}")


# (key, kind, lowest, highest): the range of each turbo_guard.json setting
LIMITS = (
    ("enabled", bool, None, None), ("shut_down_source", bool, None, None),
    ("normal_current_a", float, 0.1, 10.0), ("overload_current_a", float, 0.2, 10.0),
    ("bearing_limit_c", float, 30.0, 120.0), ("converter_limit_c", float, 20.0, 100.0),
    ("at_speed_pct", float, 50.0, 100.0), ("settle_s", float, 0.0, 3600.0), ("smooth_s", float, 5.0, 600.0),
    ("warn_after_s", float, 10.0, 7200.0), ("hot_after_s", float, 0.0, 3600.0),
    ("critical_after_s", float, 0.0, 7200.0), ("trip_after_s", float, 60.0, 14400.0),
    ("pump_warning_trip_s", float, 0.0, 7200.0), ("falling_fraction", float, 0.05, 0.95),
    ("falling_window_s", float, 60.0, 7200.0), ("clear_after_s", float, 0.0, 3600.0),
)
# Settings of the first version (27 October 2026, morning), replaced that afternoon
RETIRED = {"current_margin_a": "the overload line is overload_current_a now, an absolute current",
           "temp_margin_c": "the temperatures have absolute limits now: bearing_limit_c, converter_limit_c",
           "normal_bearing_c": "replaced by bearing_limit_c", "normal_converter_c": "replaced by converter_limit_c"}


def parse(cfg) -> tuple:
    """(GuardConfig, [problems]) from turbo_guard.json's contents. A missing setting takes its
    default; so does one with a problem. Each problem says what is used instead."""
    problems, values = [], {}
    if cfg is None:
        return GuardConfig(), problems
    if not isinstance(cfg, dict):
        return GuardConfig(), [f"{CONFIG_FILE}: must be an object such as {{\"overload_current_a\": 1.2}} - "
                               f"the defaults are used"]
    known = {name for name, *_ in LIMITS}
    for key in cfg:
        if str(key).startswith("_") or key in known:
            continue
        problems.append(f"{key}: no longer used, ignored ({RETIRED[key]})" if key in RETIRED else
                        f"{key}: not a setting, ignored (known: {', '.join(sorted(known))})")
    default = " - its default is used"
    for key, kind, lo, hi in LIMITS:
        if key not in cfg:
            continue
        v = cfg[key]
        if kind is bool:
            if isinstance(v, bool):
                values[key] = v
            else:
                problems.append(f"{key}: must be true or false (is {v!r}){default}")
        elif isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            problems.append(f"{key}: must be a number (is {v!r}){default}")
        elif not lo <= v <= hi:
            problems.append(f"{key}: must be {lo:g} to {hi:g} (is {v:g}){default}")
        else:
            values[key] = float(v)
    c = replace(GuardConfig(), **values)
    if c.overload_current_a <= c.normal_current_a:
        line = c.normal_current_a + 0.4
        problems.append(f"overload_current_a ({c.overload_current_a:g} A) must be above normal_current_a "
                        f"({c.normal_current_a:g} A) - {line:g} A is used")
        c = replace(c, overload_current_a=line)
    if c.critical_after_s > c.trip_after_s:
        problems.append(f"critical_after_s ({c.critical_after_s:g}) must be at most trip_after_s "
                        f"({c.trip_after_s:g}): the critical alarm comes before the shutdown - "
                        f"{c.trip_after_s:g} is used")
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
    as (severity, text); .stage, .message, .reason and .shutdown_at say where it is."""

    def __init__(self, config: GuardConfig = None, clock=time.time):
        self.config = config or GuardConfig()
        self.clock = clock
        c = self.config
        self.current, self.bearing, self.converter = _Median(c.smooth_s), _Median(c.smooth_s), _Median(c.smooth_s)
        self.history = deque()              # (t, smoothed current), for falling back
        self.values = {}                    # the latest smoothed readings
        self.stage = NORMAL
        self.message = ""                   # what the stage is about: the alarm's text and the panel's
        self.reason = ""                    # why it isn't normal: "overload", "hot" or "pump"
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
        self._peak_current = None           # the highest (smoothed) current of this overload

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

    def overloaded(self, now: float) -> bool:
        cur = self.values.get("current")
        return self.settled(now) and cur is not None and cur > self.config.overload_current_a + 1e-6

    def too_hot(self) -> list:
        """[(name, degC, limit)] for the bearing and converter above their limits (at any speed)."""
        c, v = self.config, self.values
        return [(name, v[name], limit) for name, limit in (("bearing", c.bearing_limit_c),
                                                           ("converter", c.converter_limit_c))
                if v.get(name) is not None and v[name] > limit + 1e-6]

    def falling(self) -> bool:
        """The current's excess over normal down by falling_fraction from its peak in the last
        falling_window_s."""
        cur, normal = self.values.get("current"), self.config.normal_current_a
        if cur is None or not self.history:
            return False
        peak = max(v for _, v in self.history) - normal
        return peak > 0 and cur - normal <= (1.0 - self.config.falling_fraction) * peak + 1e-9

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
        over, hot = self.overloaded(now), bool(self.too_hot())
        if over:
            self._under_since = None
            self._over_since = self._over_since or now
            self._peak_current = max(self._peak_current or 0.0, self.values["current"])
        elif self._over_since is not None:
            self._under_since = self._under_since or now
            if now - self._under_since >= OVER_GRACE_S:
                self._over_since = self._under_since = None
                if self.stage == NORMAL:
                    self._peak_current = None   # (over before it came to a warning: forgotten)
        self._hot_since = (self._hot_since or now) if hot else None
        pump = reading.pump_warning and self.settled(now)
        self._pump_since = (self._pump_since or now) if pump else None
        self._pump_over_since = ((self._pump_over_since or now) if pump and self._over_since is not None
                                 else None)
        load_for = None if self._over_since is None else now - self._over_since
        events = []

        if self.stage in (WARNING, CRITICAL) and self._peak_current is not None:
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
            if over and load_for >= c.warn_after_s:         # (over now: not in a dip that may yet last)
                events += self._to(WARNING, now)
            elif self._hot_since is not None and now - self._hot_since >= c.hot_after_s:
                events += self._to(WARNING, now)
            elif self._pump_since is not None and now - self._pump_since >= c.hot_after_s:
                events += self._to(WARNING, now)
        if self.stage == WARNING and over and (
                load_for >= c.warn_after_s + c.critical_after_s or self._pump_over_since is not None):
            events += self._to(CRITICAL, now)
        elif self.stage == CRITICAL and self._over_since is None:
            events += self._to(WARNING, now)    # the current is back, but it is still too hot
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
            self._clear_since, self._peak_current, self.message, self.reason, self.shutdown_at = (
                None, None, "", "", None)
            return [("INFO", f"Source turbo's load and temperatures back to normal ({self._readings()}): its "
                             f"{STAGE_NAMES[old]} alarm has cleared")]
        self.message = self._describe(now)
        if stage == WARNING and old == CRITICAL:
            return [("WARNING", f"Source turbo current back to normal ({self._readings()}): no source shutdown")]
        return [("WARNING" if stage == WARNING else "ERROR", self.message)]

    def _trip(self, now: float, why: str) -> list:
        self.stage, self.tripped_at, self.trip_reason, self.shutdown_at = TRIPPED, now, why, None
        self.reason = "overload"
        action = ("the source is being shut down (the PLC's shutdown sequence: it can't be aborted) and the "
                  "caesium oven's cooling air forced on" if self.config.shut_down_source
                  else "no source shutdown (shut_down_source is false in turbo_guard.json)")
        self.message = (f"Source turbo protection tripped at {_clock(now)}: {action}. Why: {why} "
                        f"({self._readings()}). The turbo is left running - it is stopped only by hand, at the "
                        f"pump. Check the valve between it and its backing pump, and the backing pump; RESET "
                        f"FAULTS once its current is back to normal.")
        return [("CRITICAL", self.message)]

    def _readings(self) -> str:
        v, c = self.values, self.config
        parts = []
        if v.get("current") is not None:
            parts.append(f"{v['current']:.1f} A, normally {c.normal_current_a:g} A")
        temps = [f"{name} {v[name]:.0f} degC" for name in ("bearing", "converter") if v.get(name) is not None]
        if temps:
            parts.append(", ".join(temps))
        return "; ".join(parts) or "no readings"

    def _describe(self, now: float) -> str:
        """The alarm's text: what, the shutdown time (first, as a narrow alarm list cuts it short),
        what to check, the readings."""
        c, due = self.config, self.due()
        check = "Check the valve between it and its backing pump, and the backing pump."
        if self._over_since is not None:
            self.reason = "overload"
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
            readings = f"{self._readings()}; above {c.overload_current_a:g} A for {minutes:.0f} min"
        elif self._hot_since is not None:
            self.reason = "hot"
            what = "Source turbo too hot: " + " and ".join(
                f"its {name} at {value:.0f} degC, over {limit:g}" for name, value, limit in self.too_hot())
            check = "Check its cooling, the valve between it and its backing pump, and the backing pump."
            readings = self._readings()
        elif self._pump_since is not None:
            self.reason = "pump"
            what, readings = "Source turbo reports a warning", self._readings()
        else:
            self.reason = ""
            return (f"Source turbo's load and temperatures back to normal ({self._readings()}): the alarm clears "
                    f"once they have stayed so for {c.clear_after_s / 60:g} min")
        return f"{what}. {check} ({readings})"

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
                           f"tripped until its current is back under {self.config.overload_current_a:g} A")
        self.stage, self.message, self.reason, self.tripped_at, self.trip_reason = NORMAL, "", "", None, ""
        self._over_since = self._under_since = self._hot_since = self._pump_since = self._pump_over_since = None
        self._clear_since, self._peak_current = None, None
        return True, "source turbo protection reset"

    def dump(self) -> dict:
        """What must survive a restart of the service: a trip, and an overload's clock."""
        return {"stage": self.stage, "saved_at": self.clock(), "tripped_at": self.tripped_at,
                "trip_reason": self.trip_reason, "message": self.message if self.stage == TRIPPED else "",
                "over_since": self._over_since, "pump_over_since": self._pump_over_since,
                "peak_current": self._peak_current}

    def restore(self, saved: dict, max_age_s: float = 900.0):
        """A trip comes back however old it is (it is latched); an overload's clock only if it was
        saved recently (the service restarted), so it carries on rather than starting again."""
        if not isinstance(saved, dict):
            return
        stage = saved.get("stage")
        if stage == TRIPPED:
            self.stage, self.tripped_at, self.reason = TRIPPED, saved.get("tripped_at"), "overload"
            self.trip_reason, self.message = saved.get("trip_reason") or "", saved.get("message") or ""
            return
        try:
            age = self.clock() - float(saved.get("saved_at") or 0.0)
        except (TypeError, ValueError):
            return
        if stage in (WARNING, CRITICAL) and 0 <= age <= max_age_s and saved.get("over_since"):
            self.stage, self.reason = stage, "overload"
            self._over_since = float(saved["over_since"])
            po = saved.get("pump_over_since")
            self._pump_over_since = float(po) if po else None
            peak = saved.get("peak_current")
            self._peak_current = float(peak) if peak else self.config.overload_current_a
