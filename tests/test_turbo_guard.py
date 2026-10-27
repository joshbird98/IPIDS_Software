"""The source turbo's load guard (src/core/turbo_guard.py), on a fake clock.

26 October 2026: the valve to the turbo's backing pump was shut at 10:22. The turbo's current went
from 0.8 A to 1.2 A at once and crept up for 13 hours (bearing 43 -> 65 degC, converter 28 -> 53 degC);
its own warning came at 22:37 and it failed at 23:20. The guard warns once the current has been above
1.2 A for 15 minutes without a break, and shuts the source down 20 minutes after that - not 43
minutes before the pump fails. A bearing above 65 degC or a converter above 50 degC warns too."""
from datetime import datetime

import pytest

from src.core import turbo_guard as tg
from src.core.turbo_guard import CRITICAL, NORMAL, TRIPPED, WARNING, GuardConfig, Reading, TurboGuard

DAY = datetime(2026, 10, 26)


def at(h, m, s=0.0):
    return DAY.replace(hour=h, minute=m).timestamp() + s


T0 = at(9, 0)


class Pump:
    """Feeds a guard one reading a second, like the service (which polls faster: the guard looks once a
    second)."""

    def __init__(self, config=None, start=None):
        self.t = start if start is not None else T0
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
    p = Pump().run(at(10, 22, 50) - T0)
    assert p.events == [] and p.guard.stage == NORMAL                # the rod change: nothing
    p.run(at(12, 0) - p.t, current=october_26, bearing=temperature(43, 22), converter=temperature(28, 25))
    warn, critical, trip = p.when(WARNING), p.when(CRITICAL), p.when(TRIPPED)
    # 1.2 A is not above the line: the clock starts as it creeps to 1.3 A (10:38 here, 10:44 that day)
    assert at(10, 50) < warn < at(11, 2)
    assert critical - warn == pytest.approx(600, abs=3)
    assert trip - warn == pytest.approx(1200, abs=3)                 # about 11:15, not 22:37 or 23:20
    assert [s for _, s, _ in p.events] == ["WARNING", "ERROR", "CRITICAL"]
    first = p.events[0][2]
    shutdown = datetime.fromtimestamp(trip).strftime("%H:%M")
    assert first.startswith(f"Source turbo working harder than normal: the source will be shut down at {shutdown}")
    assert "1.3 A, normally 0.8 A" in first and "above 1.2 A for 15 min" in first and "backing pump" in first
    assert "can't be aborted" in p.events[-1][2] and "left running" in p.events[-1][2]
    assert p.guard.shutdown_at is None and p.guard.stage == TRIPPED


def test_a_run_up_a_gas_burst_or_a_short_overload_is_not_one():
    # running up: 8 A for 6 minutes below full speed, then a tail inside settle_s
    p = Pump().run(5, speed=0.0, current=0.0)
    p.run(360, speed=lambda t: min(100.0, (t - T0) / 3.6), current=8.0)
    p.run(300, current=1.5)
    p.run(1800, current=0.8)
    # bursts of 5 s every minute at full speed
    p.run(1800, current=lambda t: 2.3 if int(t) % 60 < 5 else 0.8)
    # 14 minutes over the line, then back
    p.run(14 * 60, current=1.5)
    p.run(600, current=0.8)
    assert p.events == []


def test_the_overload_has_to_last_15_minutes_without_a_break():
    p = Pump().run(600, current=1.5)
    p.run(180, current=1.0)                       # 3 min back under the line: the clock starts again
    p.run(600, current=1.5)
    assert p.events == []
    second = p.t - 600
    p.run(400, current=1.5)
    assert second + 900 < p.when(WARNING) < second + 960          # (the minute's median takes ~30 s to cross)
    # a dip of the minute's median for less than a minute isn't a break
    p = Pump().run(600, current=1.5)
    p.run(40, current=1.0)
    p.run(320, current=1.5)
    assert p.when(WARNING) == pytest.approx(T0 + 900, abs=3)


def test_already_at_full_speed_when_the_service_starts():
    """A restart of the service while the turbo is overloaded mustn't wait settle_s again."""
    p = Pump().run(910, current=1.4)
    assert p.guard.stage == WARNING


def test_the_valve_opened_after_the_warning_means_no_shutdown():
    p = Pump().run(1600, current=1.3)                                 # warning at 15 min, critical at 25
    assert p.guard.stage == CRITICAL
    p.run(1200, current=0.8)                                          # opened at 27 min: back to normal
    assert p.when(TRIPPED) is None and p.guard.stage == NORMAL
    texts = [text for _, _, text in p.events]
    assert any("current back to normal" in t and "no source shutdown" in t for t in texts)
    assert texts[-1].startswith("Source turbo's load and temperatures back to normal")
    assert "alarm has cleared" in texts[-1]


def test_a_current_falling_back_holds_off_the_shutdown_until_it_stops_falling():
    peak = T0 + 1900
    p = Pump().run(2000, current=lambda t: 2.0 if t < peak else 1.4)       # 1.2 A excess -> 0.6 A: falling back
    assert p.guard.stage == CRITICAL and p.guard.falling()
    p.run(800, current=1.4)            # stays 0.6 A over: once the peak is out of the window, not falling
    assert p.when(TRIPPED) == pytest.approx(peak + 600, abs=40)    # (the 60 s median lags the drop)


def test_too_hot_warns_whatever_the_room_but_only_warns():
    # a bearing up 7 degC, a converter up 17 degC: still under the limits (65, 50)
    p = Pump().run(2 * 3600, bearing=50.0, converter=45.0)
    assert p.events == []
    # the bearing past 65 degC
    p = Pump().run(4 * 3600, bearing=lambda t: 60 + min(10, (t - T0) / 120))
    assert p.when(WARNING) == pytest.approx(T0 + 600 + 60, abs=40) and p.guard.stage == WARNING
    assert p.when(CRITICAL) is None and p.when(TRIPPED) is None and p.guard.shutdown_at is None
    text = p.events[0][2]
    assert text.startswith("Source turbo too hot: its bearing at") and "over 65" in text and "cooling" in text
    assert p.guard.reason == "hot"
    # the converter past 50 degC, with the pump stopped
    p = Pump().run(300, speed=0.0, current=0.0, converter=55.0)
    assert p.guard.stage == WARNING and "its converter at 55 degC, over 50" in p.guard.message


def test_the_pumps_own_warning_brings_the_shutdown_forward():
    config = GuardConfig(trip_after_s=7200)
    p = Pump(config).run(960, current=1.4)
    assert p.guard.stage == WARNING
    on = p.t
    p.run(5, current=1.4, warning=True)
    assert p.guard.stage == CRITICAL and "it reports a warning" in p.guard.message
    p.run(700, current=1.4, warning=True)
    assert p.when(TRIPPED) == pytest.approx(on + 600, abs=5)
    assert "the pump's own warning on for 10 min" in p.events[-1][2]


def test_the_pumps_warning_alone_only_warns():
    p = Pump().run(3600, warning=True)
    assert p.guard.stage == WARNING and p.events[0][2].startswith("Source turbo reports a warning")
    assert p.when(WARNING) == pytest.approx(T0 + 60, abs=3) and p.guard.reason == "pump"
    assert p.when(TRIPPED) is None


def test_an_overloaded_pump_that_fails_or_slows_trips_at_once():
    p = Pump().run(960, current=1.4)
    p.run(3, current=1.4, error=True)
    assert p.guard.stage == TRIPPED and "it reports an error" in p.guard.trip_reason
    p = Pump().run(960, current=1.4)
    p.run(3, current=1.4, speed=80.0)
    assert p.guard.stage == TRIPPED and "down to 80 % speed" in p.guard.trip_reason
    # ...but not a pump that is only too hot, even if it was over the line for a while before
    p = Pump().run(600, current=1.4)
    p.run(1800, current=0.8)
    p.run(120, converter=55.0)
    assert p.guard.stage == WARNING and p.guard.reason == "hot"
    p.run(60, converter=55.0, error=True)
    assert p.guard.stage == WARNING and p.when(TRIPPED) is None


def test_a_trip_is_latched_until_the_current_is_back_to_normal():
    p = Pump().run(2200, current=1.5)
    assert p.guard.stage == TRIPPED and p.when(TRIPPED) == pytest.approx(T0 + 2100, abs=3)
    done, why = p.guard.reset(p.t)
    assert not done and "still overloaded (1.5 A, normally 0.8 A" in why and "back under 1.2 A" in why
    p.run(120, current=0.8, bearing=70.0)                 # the current normal; the bearing still too hot
    assert p.guard.reset(p.t) == (True, "source turbo protection reset")
    assert p.guard.stage == NORMAL and p.guard.message == ""
    # a pump that has stopped, or isn't answering, doesn't hold it either
    p = Pump().run(2200, current=1.5)
    p.run(5, current=0.0, speed=0.0)
    assert p.guard.reset(p.t)[0]
    p = Pump().run(2200, current=1.5)
    assert p.guard.reset(p.t, answering=False)[0]


def test_nothing_changes_while_the_pump_doesnt_answer():
    p = Pump().run(960, current=1.4)
    assert p.guard.stage == WARNING
    p.run(3600, current=None, bearing=None, converter=None, speed=None)
    assert p.guard.stage == WARNING and p.when(TRIPPED) is None


def test_off_or_without_the_shutdown():
    p = Pump(GuardConfig(enabled=False)).run(3600, current=3.0, bearing=80.0)
    assert p.events == [] and p.guard.stage == NORMAL
    # alarms only: the same stages and times, but no shutdown (to try the guard out)
    p = Pump(GuardConfig(shut_down_source=False)).run(2200, current=1.5)
    assert p.guard.stage == TRIPPED and p.when(TRIPPED) == pytest.approx(T0 + 2100, abs=3)
    assert "no source shutdown (shut_down_source is false" in p.events[-1][2]
    assert all("will be shut down" not in text for _, _, text in p.events)
    assert "it would trip at 09:35" in p.events[0][2] and p.guard.shutdown_at is None


def test_a_trip_survives_a_restart_and_an_overloads_clock_carries_on():
    p = Pump().run(2200, current=1.5)
    saved = p.guard.dump()
    later = TurboGuard(clock=lambda: p.t + 86400)
    later.restore(saved)
    assert later.stage == TRIPPED and later.message.startswith("Source turbo protection tripped")
    p = Pump().run(1000, current=1.5)                      # in WARNING, the clock at 1000 s
    saved = p.guard.dump()
    soon = Pump(start=p.t + 30)
    soon.guard.restore(saved)
    assert soon.guard.stage == WARNING and soon.guard.reason == "overload"
    soon.run(1200, current=1.5)
    assert soon.when(TRIPPED) == pytest.approx(T0 + 2100, abs=5)    # 35 min after the overload began
    stale = TurboGuard(clock=lambda: p.t + 3600)
    stale.restore(saved)
    assert stale.stage == NORMAL
    old = {k: v for k, v in saved.items() if k != "peak_current"}   # (saved by the first version)
    old["peak_excess"] = 0.7
    again = TurboGuard(clock=lambda: p.t + 30)
    again.restore(old)
    assert again.stage == WARNING
    for junk in (None, "x", {"stage": 1, "saved_at": "soon"}, {"stage": 2}):
        TurboGuard().restore(junk)


def test_settings():
    c, problems = tg.parse({"overload_current_a": 1.5, "enabled": True, "_note": "x"})
    assert problems == [] and c.overload_current_a == 1.5 and c.warn_after_s == 900
    c, problems = tg.parse({"normal_current_a": "1", "warn_after_s": 5, "enabled": 1, "nonsense": 2})
    assert c == GuardConfig()                              # every bad value left at its default
    assert len(problems) == 4 and any(p.startswith("nonsense: not a setting, ignored") for p in problems)
    assert "warn_after_s: must be 10 to 7200 (is 5) - its default is used" in problems
    c, problems = tg.parse({"current_margin_a": 0.3, "normal_bearing_c": 43})      # the first version's
    assert c == GuardConfig() and len(problems) == 2
    assert problems[0] == ("current_margin_a: no longer used, ignored (the overload line is overload_current_a "
                           "now, an absolute current)")
    c, problems = tg.parse({"normal_current_a": 1.3})
    assert c.overload_current_a == pytest.approx(1.7) and "must be above normal_current_a" in problems[0]
    assert problems[0].endswith("- 1.7 A is used")
    c, problems = tg.parse({"critical_after_s": 1300})
    assert c.critical_after_s == 1200 and "at most trip_after_s" in problems[0] and problems[0].endswith("1200 is used")
    assert tg.parse("x")[1] and tg.parse(None) == (GuardConfig(), [])
    text = GuardConfig().describe()
    assert "15 min above 1.2 A" in text and "bearing above 65 degC" in text and "converter above 50 degC" in text
    assert "off" in GuardConfig(enabled=False).describe()


def test_the_shipped_settings_are_the_defaults():
    import json
    from src.core import paths
    with open(paths.config_path(tg.CONFIG_FILE)) as f:
        config, problems = tg.parse(json.load(f))
    assert problems == [] and config == GuardConfig()
