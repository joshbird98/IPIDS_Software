"""The source turbo's load guard (src/core/turbo_guard.py), on a fake clock.

26 October 2026: the valve to the turbo's backing pump was shut at 10:22. The turbo's current went
from 0.8 A to 1.2 A at once and crept up for 13 hours (bearing 43 -> 65 degC, converter 28 -> 53 degC);
its own warning came at 22:37 and it failed at 23:20. The guard is to warn within minutes and shut
the source down 20 minutes later - not 43 minutes before the pump fails."""
from datetime import datetime

import pytest

from src.core import turbo_guard as tg
from src.core.turbo_guard import CRITICAL, NORMAL, TRIPPED, WARNING, GuardConfig, Reading, TurboGuard

DAY = datetime(2026, 10, 26)


def at(h, m, s=0.0):
    return DAY.replace(hour=h, minute=m).timestamp() + s


class Pump:
    """Feeds a guard one reading a second, like the service (which polls faster: the guard looks once a
    second)."""

    def __init__(self, config=None, start=None):
        self.t = start if start is not None else at(9, 0)
        self.guard = TurboGuard(config or GuardConfig(), clock=lambda: self.t)
        self.events = []                # (time, severity, text)
        self.stages = []                # (time, stage) at each change

    def run(self, seconds, current=0.8, bearing=43.0, converter=28.0, speed=100.0, warning=False, error=False,
            step=1.0):
        """current etc. may be functions of the time."""
        end = self.t + seconds
        while self.t < end:
            v = {k: (f(self.t) if callable(f) else f) for k, f in
                 (("current", current), ("bearing", bearing), ("converter", converter), ("speed", speed))}
            before = self.guard.stage
            for severity, text in self.guard.update(self.t, Reading(
                    speed_pct=v["speed"], current_a=v["current"], bearing_c=v["bearing"],
                    converter_c=v["converter"], pump_warning=warning, pump_error=error,
                    turning=v["speed"] is not None and v["speed"] > 1)):
                self.events.append((self.t, severity, text))
            if self.guard.stage != before:
                self.stages.append((self.t, self.guard.stage))
            self.t += step
        return self

    def when(self, stage):
        return next((t for t, s in self.stages if s == stage), None)


def october_26(t):
    """The source turbo's current on 26 October 2026, near enough (0.1 A steps, as it reads)."""
    if t < at(10, 22, 50):
        return 0.8
    if t < at(10, 23, 5):
        return 2.3 if int(t) % 3 == 0 else 1.4          # the burst as the valve shut
    hours = (t - at(10, 23)) / 3600
    if t < at(22, 0):
        amps = 1.2 + 0.2 * hours                         # 1.2 A creeping up...
    else:
        amps = 1.2 + 0.2 * 11.6 + 1.6 * (t - at(22, 0)) / 3600 * 2.4    # ...then climbing fast
    return round(amps, 1)


def temperature(base, rise):
    return lambda t: base if t < at(10, 23) else round(base + rise * (t - at(10, 23)) / (13 * 3600))


def test_26_october_the_source_is_shut_down_12_hours_before_the_pump_failed():
    p = Pump().run(at(10, 22, 50) - at(9, 0))
    assert p.events == [] and p.guard.stage == NORMAL                # the rod change: nothing
    p.run(at(12, 0) - p.t, current=october_26, bearing=temperature(43, 22), converter=temperature(28, 25))
    warn, critical, trip = p.when(WARNING), p.when(CRITICAL), p.when(TRIPPED)
    assert at(10, 24) < warn < at(10, 27)                            # within minutes of the valve shutting
    assert critical - warn == pytest.approx(600, abs=3)
    assert trip - warn == pytest.approx(1200, abs=3)                 # 10:45, not 22:37 or 23:20
    assert [s for _, s, _ in p.events] == ["WARNING", "ERROR", "CRITICAL"]
    first = p.events[0][2]
    assert first.startswith("Source turbo working harder than normal: the source will be shut down at 10:45")
    assert "1.2 A, normally 0.8 A" in first and "backing pump" in first
    assert "can't be aborted" in p.events[-1][2] and "left running" in p.events[-1][2]
    assert p.guard.shutdown_at is None and p.guard.stage == TRIPPED


def test_a_gas_burst_or_a_run_up_is_not_an_overload():
    p = Pump(start=at(9, 0)).run(600, current=lambda t: 0.8, speed=lambda t: min(100.0, (t - at(9, 0)) * 2))
    p.run(1800, current=lambda t: 2.3 if int(t) % 60 < 5 else 0.8)       # 5 s bursts every minute
    assert p.events == []
    # running up: high current below full speed, and for settle_s after reaching it
    p = Pump(start=at(9, 0)).run(5, speed=0.0, current=0.0)
    p.run(300, speed=lambda t: min(100.0, (t - at(9, 0)) / 3), current=3.0)
    p.run(500, current=2.0)
    assert p.events == []


def test_already_at_full_speed_when_the_service_starts():
    """A restart of the service while the turbo is overloaded mustn't wait settle_s again."""
    p = Pump().run(200, current=1.4)
    assert p.guard.stage == WARNING


def test_the_valve_opened_after_the_warning_means_no_shutdown():
    p = Pump().run(900, current=1.3)                                  # warning, then critical (at 12 min)
    assert p.guard.stage == CRITICAL
    p.run(1200, current=0.8)                                          # opened at 15 min: back to normal
    assert p.when(TRIPPED) is None and p.guard.stage == NORMAL
    texts = [text for _, _, text in p.events]
    assert any("back to normal" in t and "no source shutdown" in t for t in texts)
    assert texts[-1].startswith("Source turbo back to normal") and "alarm has cleared" in texts[-1]


def test_a_current_falling_back_holds_off_the_shutdown_until_it_stops_falling():
    peak = at(9, 0) + 1100
    p = Pump().run(1300, current=lambda t: 2.0 if t < peak else 1.4)       # 1.2 A excess -> 0.6 A: falling back
    assert p.guard.stage == CRITICAL and p.guard.falling()
    p.run(600, current=1.4)            # stays 0.6 A over: once the peak is out of the window, not falling
    assert p.when(TRIPPED) == pytest.approx(peak + 600, abs=40)    # (the 60 s median lags the drop)


def test_temperatures_alone_only_warn():
    p = Pump().run(4 * 3600, bearing=lambda t: 43 + min(10, (t - at(9, 0)) / 600))
    assert p.guard.stage == WARNING and p.when(CRITICAL) is None and p.when(TRIPPED) is None
    assert p.events[0][2].startswith("Source turbo running hot.")
    assert p.guard.shutdown_at is None


def test_the_pumps_own_warning_brings_the_shutdown_forward():
    config = GuardConfig(trip_after_s=7200)
    p = Pump(config).run(300, current=1.4)
    assert p.guard.stage == WARNING
    p.run(5, current=1.4, warning=True)
    assert p.guard.stage == CRITICAL and "it reports a warning" in p.guard.message
    p.run(700, current=1.4, warning=True)
    assert p.when(TRIPPED) == pytest.approx(at(9, 0) + 300 + 600, abs=5)
    assert "the pump's own warning on for 10 min" in p.events[-1][2]


def test_the_pumps_warning_alone_only_warns():
    p = Pump().run(3600, warning=True)
    assert p.guard.stage == WARNING and p.events[0][2].startswith("Source turbo reports a warning")
    assert p.when(TRIPPED) is None


def test_an_overloaded_pump_that_fails_or_slows_trips_at_once():
    p = Pump().run(200, current=1.4)
    p.run(3, current=1.4, error=True)
    assert p.guard.stage == TRIPPED and "it reports an error" in p.guard.trip_reason
    p = Pump().run(200, current=1.4)
    p.run(3, current=1.4, speed=80.0)
    assert p.guard.stage == TRIPPED and "down to 80 % speed" in p.guard.trip_reason


def test_a_trip_is_latched_until_the_load_is_back_to_normal():
    p = Pump().run(1500, current=1.5)
    assert p.guard.stage == TRIPPED
    done, why = p.guard.reset(p.t)
    assert not done and "still overloaded (1.5 A, normally 0.8 A" in why
    p.run(120, current=0.8, bearing=55.0)                 # current normal; the bearing still warm
    assert p.guard.reset(p.t) == (True, "source turbo protection reset")
    assert p.guard.stage == NORMAL and p.guard.message == ""
    # a pump that has stopped, or isn't answering, doesn't hold it either
    p = Pump().run(1500, current=1.5)
    p.run(5, current=0.0, speed=0.0)
    assert p.guard.reset(p.t)[0]
    p = Pump().run(1500, current=1.5)
    assert p.guard.reset(p.t, answering=False)[0]


def test_nothing_changes_while_the_pump_doesnt_answer():
    p = Pump().run(200, current=1.4)
    assert p.guard.stage == WARNING
    p.run(3600, current=None, bearing=None, converter=None, speed=None)
    assert p.guard.stage == WARNING and p.when(TRIPPED) is None


def test_off_or_without_the_shutdown():
    p = Pump(GuardConfig(enabled=False)).run(3600, current=3.0)
    assert p.events == [] and p.guard.stage == NORMAL
    # alarms only: the same stages and times, but no shutdown (to try the guard out)
    p = Pump(GuardConfig(shut_down_source=False)).run(1500, current=1.5)
    assert p.guard.stage == TRIPPED and p.when(TRIPPED) == pytest.approx(at(9, 0) + 1320, abs=3)
    assert "no source shutdown (shut_down_source is false" in p.events[-1][2]
    assert all("will be shut down" not in text for _, _, text in p.events)
    assert "it would trip at 09:22" in p.events[0][2] and p.guard.shutdown_at is None


def test_a_trip_survives_a_restart_and_an_overloads_clock_carries_on():
    p = Pump().run(1500, current=1.5)
    saved = p.guard.dump()
    later = TurboGuard(clock=lambda: p.t + 86400)
    later.restore(saved)
    assert later.stage == TRIPPED and later.message.startswith("Source turbo protection tripped")
    p = Pump().run(400, current=1.5)                       # in WARNING, the clock at 400 s
    saved = p.guard.dump()
    soon = Pump(start=p.t + 30)
    soon.guard.restore(saved)
    assert soon.guard.stage == WARNING
    soon.run(1000, current=1.5)
    assert soon.when(TRIPPED) == pytest.approx(at(9, 0) + 1320, abs=5)    # 22 min after the overload began
    stale = TurboGuard(clock=lambda: p.t + 3600)
    stale.restore(saved)
    assert stale.stage == NORMAL
    for junk in (None, "x", {"stage": 1, "saved_at": "soon"}, {"stage": 2}):
        TurboGuard().restore(junk)


def test_settings():
    c, problems = tg.parse({"normal_current_a": 1.0, "enabled": True, "_note": "x"})
    assert problems == [] and c.normal_current_a == 1.0 and c.trip_after_s == 1200
    c, problems = tg.parse({"normal_current_a": "1", "warn_after_s": 5, "enabled": 1, "nonsense": 2})
    assert c == GuardConfig()                              # every bad value left at its default
    assert len(problems) == 4 and any("nonsense: not a setting" in p for p in problems)
    c, problems = tg.parse({"critical_after_s": 1300})
    assert c.critical_after_s == 1200 and "at most trip_after_s" in problems[0]
    assert tg.parse("x")[1] and tg.parse(None) == (GuardConfig(), [])
    assert "normal 0.8 A" in GuardConfig().describe() and "off" in GuardConfig(enabled=False).describe()


def test_the_shipped_settings_are_the_defaults():
    import json
    from src.core import paths
    with open(paths.config_path(tg.CONFIG_FILE)) as f:
        config, problems = tg.parse(json.load(f))
    assert problems == [] and config == GuardConfig()
