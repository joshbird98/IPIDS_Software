"""Spellman service against simulated supplies - including the 'enable state stays
latched after the safety relay trips' bug reported on the real machine, and the
software arc detection (output collapsed below 10 % of the setpoint)."""
import threading
import time

import pytest
import zmq

from src.core import commands as C
from src.core.network_map import ZMQ_PORT_HEARTBEAT, ZMQ_PORT_SPELLMAN_PUB
from src.services.service_spellman_mpd import ARC_DEFAULTS, ArcDetector, arc_settings
from tests.service_harness import FakePlcPublisher, ServiceThread, TelemetryTap

EINZEL = "ion_beam.source.einzel"
ARC = f"{EINZEL}.stat_arc_exceeded"


@pytest.fixture
def stack(monkeypatch):
    monkeypatch.setenv("SINDIE_SIMULATE", "1")
    monkeypatch.delenv("SINDIE_SIM_FAULTS", raising=False)
    from src.sim import spellman as simsp
    simsp._BUSES.clear()
    plc = FakePlcPublisher()
    from src.services.service_spellman_mpd import SpellmanMicroservice
    svc = SpellmanMicroservice()
    runner = ServiceThread(svc)
    tap = TelemetryTap(ZMQ_PORT_SPELLMAN_PUB, prefix=EINZEL)
    yield plc, svc, tap
    runner.stop()
    plc.stop()
    tap.close()


def wait_powered(tap):
    """Supply seen powered and idle. (stat_enabled is 0.0 while unpowered too, so waiting for
    that alone could start a test before the service has seen the relay energise.)"""
    return tap.wait_for(lambda d: d[f"{EINZEL}.rb_voltage"] is not None and d[f"{EINZEL}.stat_enabled"] == 0.0)


def bring_up(c, tap, kv):
    """What the HMI does: the voltage, then enable (the service sets the current limit)."""
    assert c.request("spellman", f"{EINZEL}.sp_requested_voltage", kv)["status"] == C.OK
    assert c.request("spellman", f"{EINZEL}.cmd_enable", True)["status"] == C.OK
    return tap.wait_for(lambda d: d[f"{EINZEL}.stat_enabled"] == 1.0
                        and abs((d[f"{EINZEL}.rb_voltage"] or 0) - kv) < 0.05 * kv + 0.01, timeout=6)


def test_enable_and_setpoint_acknowledged(stack):
    plc, svc, tap = stack
    wait_powered(tap)
    c = C.CommandClient(default_timeout=3.0)
    d = bring_up(c, tap, 10.0)
    assert d[f"{EINZEL}.sp_actual_voltage"] == pytest.approx(10.0)
    assert d[f"{EINZEL}.sp_actual_current"] == pytest.approx(50.0)
    r = c.request("spellman", f"{EINZEL}.sp_requested_voltage", 99.0)
    assert r["status"] == C.REJECTED and r["reason"].startswith("Hard cap") and "22 kV cap" in r["reason"]
    r = c.request("spellman", f"{EINZEL}.sp_requested_voltage", -1.0)
    assert r["status"] == C.REJECTED and "outside the allowed range" in r["reason"]


def test_current_limit_comes_from_mpd_config(stack):
    """max_current_ua in mpd_config.json (the MPD30's 330 uA); before it existed every
    current-limit command - including the HMI's automatic 50 uA, as it was then - was refused."""
    plc, svc, tap = stack
    wait_powered(tap)
    c = C.CommandClient(default_timeout=3.0)
    assert c.request("spellman", f"{EINZEL}.sp_requested_current", 330.0)["status"] == C.OK
    r = c.request("spellman", f"{EINZEL}.sp_requested_current", 400.0)
    assert r["status"] == C.REJECTED and "outside the allowed range" in r["reason"]


def test_the_service_sets_a_current_limit_when_the_supply_holds_none(stack):
    """Every power-up leaves a supply's current limit at 0, and with none its output can't rise. The
    HMI's source panel used to send 50 uA with each voltage - but only when its registry had current
    limits: with a system_tags.json from before them (Josh's simulator session, Oct 2026) the einzel
    took 10 kV, stayed at 0 V and latched an arc. The service now sets it, whoever sends the voltage."""
    plc, svc, tap = stack
    d = wait_powered(tap)
    assert d[f"{EINZEL}.sp_actual_current"] == 0.0                     # as it comes from power-up
    c = C.CommandClient(default_timeout=3.0)
    r = c.request("spellman", f"{EINZEL}.sp_requested_voltage", 10.0, origin="recipe")
    assert r["status"] == C.OK and "current limit was 0, set to 50 uA" in r["reason"], r
    assert c.request("spellman", f"{EINZEL}.cmd_enable", True)["status"] == C.OK
    d = tap.wait_for(lambda d: abs((d[f"{EINZEL}.rb_voltage"] or 0) - 10.0) < 0.5, timeout=6)
    assert d[f"{EINZEL}.sp_actual_current"] == pytest.approx(50.0)
    assert tap.holds_for(lambda d: d[ARC] == 0.0, 3.5)                 # longer than settle_s
    # A limit someone chose is left alone
    assert c.request("spellman", f"{EINZEL}.sp_requested_current", 120.0)["status"] == C.OK
    r = c.request("spellman", f"{EINZEL}.sp_requested_voltage", 12.0)
    assert r["status"] == C.OK and "current limit" not in r["reason"]
    tap.wait_for(lambda d: d[f"{EINZEL}.sp_actual_current"] == pytest.approx(120.0), timeout=2)


def test_after_a_power_cycle_the_limit_is_set_again_and_the_old_setpoints_are_not_shown(stack):
    """Unpowered, the supplies forget their setpoints. The service's own record of what it last set is
    dropped with the relay (it stood in for a supply that didn't answer a setpoint query, and would have
    shown 50 uA - and the old voltage - for a supply holding 0)."""
    plc, svc, tap = stack
    c = C.CommandClient(default_timeout=3.0)
    wait_powered(tap)
    bring_up(c, tap, 8.0)
    plc.relay = False
    tap.wait_for(lambda d: d[f"{EINZEL}.stat_enabled"] == 0.0 and d[f"{EINZEL}.rb_voltage"] is None, timeout=3)
    assert svc.sp_cache == {}
    plc.relay = True
    d = tap.wait_for(lambda d: d[f"{EINZEL}.rb_voltage"] is not None, timeout=5)
    assert d[f"{EINZEL}.sp_actual_voltage"] == 0.0 and d[f"{EINZEL}.sp_actual_current"] == 0.0
    r = c.request("spellman", f"{EINZEL}.cmd_enable", True)            # enabling sets it too
    assert r["status"] == C.OK and "set to 50 uA" in r["reason"], r


def test_no_voltage_or_enable_without_a_current_limit(stack, monkeypatch):
    """If the supply won't take a current limit, the voltage (or enable) isn't sent either - it could
    only sit at 0 V and trip the arc detection - and the reply says why."""
    plc, svc, tap = stack
    wait_powered(tap)
    hw = next(iter(svc.buses.values()))
    real = hw.transaction

    def refuse_current(address, dev_type, cmd, operator="", data=""):
        if cmd == "I1" and operator == "=":
            return f"{address}{dev_type}{cmd}*"
        return real(address, dev_type, cmd, operator, data)
    monkeypatch.setattr(hw, "transaction", refuse_current)
    c = C.CommandClient(default_timeout=3.0)
    r = c.request("spellman", f"{EINZEL}.sp_requested_voltage", 5.0)
    assert r["status"] == C.FAILED and r["reason"].startswith("voltage not set: the supply holds no current limit")
    r = c.request("spellman", f"{EINZEL}.cmd_enable", True)
    assert r["status"] == C.FAILED and r["reason"].startswith("output not enabled")
    assert c.request("spellman", f"{EINZEL}.sp_requested_voltage", 0.0)["status"] == C.OK   # 0 needs none
    d = tap.wait_for(lambda d: d[f"{EINZEL}.sp_actual_voltage"] == 0.0, timeout=2)
    assert d[f"{EINZEL}.stat_enabled"] == 0.0


def test_default_current_limit_comes_from_the_config_within_its_range():
    from src.services.service_spellman_mpd import SpellmanMicroservice as S
    assert S.default_current_ua({}) == 50.0
    assert S.default_current_ua({"default_current_ua": 80.0, "max_current_ua": 330.0}) == 80.0
    assert S.default_current_ua({"default_current_ua": 500.0, "max_current_ua": 330.0}) == 330.0
    assert S.default_current_ua({"max_current_ua": 20.0}) == 20.0
    assert S.default_current_ua({"default_current_ua": "lots", "min_current_ua": 60.0}) == 60.0
    assert S.default_current_ua({"default_current_ua": float("nan")}) == 50.0


def test_relay_trip_reports_disabled_and_unknown_readbacks(stack):
    plc, svc, tap = stack
    c = C.CommandClient(default_timeout=3.0)
    wait_powered(tap)
    bring_up(c, tap, 8.0)

    plc.relay = False   # safety relay trips
    d = tap.wait_for(lambda d: d.get(f"{EINZEL}.stat_enabled") == 0.0, timeout=3)
    assert d[f"{EINZEL}.rb_voltage"] is None          # unknown, not the last value
    assert d[f"{EINZEL}.stat_comms_fail"] == 0.0      # expected, not a comms fault
    assert d[ARC] == 0.0                              # losing power is not an arc
    r = c.request("spellman", f"{EINZEL}.cmd_enable", True)
    assert r["status"] == C.REJECTED and "relay" in r["reason"]
    assert c.request("spellman", f"{EINZEL}.cmd_enable", False)["status"] == C.OK

    plc.relay = True    # power restored: the supply comes back disabled, as the real units do
    d = tap.wait_for(lambda d: d.get(f"{EINZEL}.rb_voltage") is not None, timeout=5)
    assert d[f"{EINZEL}.stat_enabled"] == 0.0


def test_unresponsive_supply_is_unknown_and_service_keeps_heartbeating(stack, monkeypatch):
    plc, svc, tap = stack
    wait_powered(tap)
    hb = zmq.Context.instance().socket(zmq.SUB)
    hb.setsockopt(zmq.SUBSCRIBE, b"")
    hb.bind(ZMQ_PORT_HEARTBEAT)
    try:
        monkeypatch.setenv("SINDIE_SIM_FAULTS", "spellman_no_reply")
        d = tap.wait_for(lambda d: d.get(f"{EINZEL}.stat_comms_fail") == 1.0, timeout=6)
        assert d[f"{EINZEL}.stat_enabled"] is None    # don't claim 'disabled' when HV might be on
        beats = []
        t_end = time.time() + 4
        while time.time() < t_end:
            if hb.poll(100):
                hb.recv()
                beats.append(time.time())
        gaps = [b - a for a, b in zip(beats, beats[1:])]
        assert beats and max(gaps) < 2.0, gaps         # manager timeout is 2.5 s
    finally:
        hb.close(0)


def test_fault_reset_reaches_spellman(stack):
    plc, svc, tap = stack
    tap.wait_for(lambda d: f"{EINZEL}.stat_enabled" in d)
    svc.arc_fault_latches["source_einzel"] = True
    r = C.CommandClient().request("spellman", "ion_beam.system.cmd_fault_reset", True)
    assert r["status"] == C.OK
    assert not svc.arc_fault_latches["source_einzel"]


def test_arc_is_detected_latched_and_cleared_by_fault_reset(stack, monkeypatch):
    plc, svc, tap = stack
    c = C.CommandClient(default_timeout=3.0)
    wait_powered(tap)
    bring_up(c, tap, 6.0)
    assert tap.holds_for(lambda d: d[ARC] == 0.0, 1.0)            # running normally: no arc

    monkeypatch.setenv("SINDIE_SIM_FAULTS", "spellman_arc")         # output collapses to ~10 V
    d = tap.wait_for(lambda d: d[ARC] == 1.0, timeout=3)
    assert d[f"{EINZEL}.rb_voltage"] < 0.6

    monkeypatch.delenv("SINDIE_SIM_FAULTS")                         # arc over, output recovers ...
    tap.wait_for(lambda d: abs(d[f"{EINZEL}.rb_voltage"] - 6.0) < 0.3, timeout=5)
    assert tap.holds_for(lambda d: d[ARC] == 1.0, 0.5)             # ... but the latch holds

    assert c.request("spellman", "ion_beam.system.cmd_fault_reset", True)["status"] == C.OK
    tap.wait_for(lambda d: d[ARC] == 0.0, timeout=3)


def test_no_arc_below_the_minimum_setpoint(stack, monkeypatch):
    """At a setpoint of a few hundred volts a near-zero reading isn't treated as an arc."""
    plc, svc, tap = stack
    c = C.CommandClient(default_timeout=3.0)
    wait_powered(tap)
    bring_up(c, tap, 0.3)
    monkeypatch.setenv("SINDIE_SIM_FAULTS", "spellman_arc")
    tap.wait_for(lambda d: d[f"{EINZEL}.rb_voltage"] < 0.03, timeout=3)
    assert tap.holds_for(lambda d: d[ARC] == 0.0, 4.0)   # longer than settle_s


def test_arc_is_caught_while_the_setpoint_keeps_stepping(stack, monkeypatch):
    """An arc during an optimiser-style sweep (a new setpoint every 0.25 s) is caught straight
    away. (Each setpoint change used to restart a 3 s settle wait, so nothing latched until
    3 s after the sweep stopped - by which time the arc may have cleared.)"""
    plc, svc, tap = stack
    c = C.CommandClient(default_timeout=3.0)
    wait_powered(tap)
    bring_up(c, tap, 6.0)
    stop = threading.Event()

    def sweep():
        cl = C.CommandClient(default_timeout=2.0)
        kv = 6.0
        while not stop.is_set():
            kv = 6.5 if kv == 6.0 else 6.0
            cl.request("spellman", f"{EINZEL}.sp_requested_voltage", kv, origin="optimizer")
            time.sleep(0.25)
        cl.close()

    t = threading.Thread(target=sweep, daemon=True)
    t.start()
    try:
        time.sleep(0.5)
        assert tap.holds_for(lambda d: d[ARC] == 0.0, 1.5)       # sweeping a healthy output: no arc
        monkeypatch.setenv("SINDIE_SIM_FAULTS", "spellman_arc")
        tap.wait_for(lambda d: d[ARC] == 1.0, timeout=2.5)        # caught while the sweep goes on
    finally:
        stop.set()
        t.join(3)


# ---------------------------------------------------------------- ArcDetector logic


def det1(**settings):
    """A detector that latches on the first poll showing an arc (the confirmation is tested separately)."""
    return ArcDetector({"confirm_polls": 1, **settings})


def feed(det, samples, enabled=True, ua=1.0):
    """samples: (t, sp_kv, rb_kv) -> list of results."""
    return [det.update(enabled, sp, rb, ua, t) for t, sp, rb in samples]


def test_collapse_after_the_output_was_up():
    det = det1()
    assert feed(det, [(0.0, 6.0, 0.2), (0.3, 6.0, 6.0), (0.6, 6.0, 5.99)]) == [None, None, None]
    why = det.update(True, 6.0, 0.4, 50.0, 0.9)       # 0.4 kV < 10 % of 6 kV
    assert why and "collapsed" in why and "6.000 kV" in why
    assert det.update(True, 6.0, 0.7, 50.0, 1.2) is None   # 0.7 kV > 0.6 kV: not collapsed


def test_short_at_switch_on_is_caught_after_settle_time():
    det = det1()
    assert all(r is None for r in feed(det, [(t / 10, 6.0, 0.01) for t in range(30)]))   # 0 .. 2.9 s
    assert det.update(True, 6.0, 0.01, 50.0, 3.0)


def test_large_step_up_does_not_trip_while_ramping():
    det = det1()
    assert feed(det, [(0.0, 0.6, 0.6), (1.0, 0.6, 0.6)]) == [None, None]
    # 0.6 kV -> 10 kV: the output (0.6 kV) is below 10 % of the new setpoint while it ramps
    assert feed(det, [(1.1, 10.0, 0.6), (1.4, 10.0, 0.9), (1.7, 10.0, 5.5), (2.0, 10.0, 9.9)]) == [None] * 4
    assert det.update(True, 10.0, 0.5, 50.0, 2.3)     # up and then collapsed: arc


def test_collapse_during_a_ramp_is_judged_against_the_lower_setpoint():
    det = det1()
    feed(det, [(0.0, 0.6, 0.6)])
    why = det.update(True, 10.0, 0.01, 50.0, 0.3)     # stepped to 10 kV and collapsed at once
    assert why and "0.600 kV" in why


def test_small_steps_keep_watching():
    det = det1()
    feed(det, [(0.0, 6.0, 6.0)])
    assert det.update(True, 6.1, 6.0, 50.0, 0.1) is None   # new setpoint, output already near it
    assert det.update(True, 6.1, 0.2, 50.0, 0.2)            # collapse caught on the next sample


def test_arc_during_setpoint_steps_is_caught_at_once():
    """The review's case: the arc starts with a setpoint step and the steps keep coming."""
    det = ArcDetector()                                     # default: 2 polls in a row
    feed(det, [(0.0, 6.0, 6.0)])
    assert det.update(True, 6.25, 0.01, 50.0, 0.25) is None   # first collapsed poll
    assert det.update(True, 6.5, 0.01, 50.0, 0.5)             # second: arc, though the setpoint moved again


def test_blind_window_after_enable_is_capped_while_stepping():
    """Stepping up from 0 kV with the output collapsed from the start: there is no lower setpoint
    to judge against, but further steps don't extend the wait beyond settle_s."""
    det = det1()
    assert det.update(True, 0.0, 0.0, 0.0, 0.0) is None
    results = {round(t * 0.25, 2): det.update(True, t * 0.25, 0.01, 50.0, t * 0.25) for t in range(1, 20)}
    first = min(t for t, r in results.items() if r)
    assert 3.0 <= first <= 3.5, results


def test_one_bad_reading_does_not_trip():
    """e.g. '21000.0' read as '1000.0' (a lost digit on the RS485 line): needs a second poll."""
    det = ArcDetector()
    feed(det, [(0.0, 21.0, 21.0)])
    assert det.update(True, 21.0, 1.0, 5.0, 0.5) is None
    assert det.update(True, 21.0, 21.0, 5.0, 1.0) is None
    assert det.update(True, 21.0, 1.0, 5.0, 1.5) is None
    assert det.update(True, 21.0, 0.9, 5.0, 2.0)


def test_low_setpoint_is_not_judged():
    det = det1()
    assert all(r is None for r in feed(det, [(t, 0.4, 0.0) for t in range(10)]))
    det2 = det1(min_setpoint_kv=0.1)
    assert det2.update(True, 0.4, 0.0, 1.0, 0.0) is None
    assert det2.update(True, 0.4, 0.0, 1.0, 5.0)


def test_disable_and_restart_reset_the_watch():
    det = det1()
    feed(det, [(0.0, 6.0, 6.0)])
    assert det.update(False, 6.0, 0.0, 0.0, 1.0) is None      # switched off: output falls, not an arc
    assert det.update(True, 6.0, 0.0, 0.0, 1.1) is None       # switched on again: wait for it
    det.restart()                                             # (enable command accepted)
    assert det.update(True, 6.0, 0.05, 0.0, 1.2) is None


def test_missing_readings_are_skipped_not_judged():
    det = det1()
    feed(det, [(0.0, 6.0, 6.0)])
    assert det.update(True, 6.0, None, None, 0.5) is None
    assert det.update(True, None, 0.0, None, 0.6) is None
    assert det.update(True, 6.0, 0.1, None, 0.7)              # still watching afterwards


def test_overcurrent_counts_as_arc():
    det = det1()
    feed(det, [(0.0, 6.0, 6.0)])
    why = det.update(True, 6.0, 5.9, 600.0, 0.3)
    assert why and "current" in why


def test_arc_settings_merge_and_validation():
    cfg = {"arc_detection": {"_comment": "x", "collapse_fraction": 0.2, "settle_s": "fast", "bogus": 1}}
    s = arc_settings(cfg, {"arc_detection": {"min_setpoint_kv": 1.0}})
    assert s["collapse_fraction"] == 0.2 and s["min_setpoint_kv"] == 1.0
    assert s["settle_s"] == ARC_DEFAULTS["settle_s"] and "bogus" not in s
    assert arc_settings({}, {}) == ARC_DEFAULTS


def test_refused_enable_is_reported_as_failed(stack, monkeypatch):
    """A '*' reply is a refusal; it used to be acknowledged as 'output enabled/disabled'."""
    plc, svc, tap = stack
    wait_powered(tap)
    c = C.CommandClient(default_timeout=3.0)
    assert c.request("spellman", f"{EINZEL}.sp_requested_current", 50.0)["status"] == C.OK   # (else that
    tap.wait_for(lambda d: d[f"{EINZEL}.sp_actual_current"] == 50.0, timeout=2)              # is refused first)
    monkeypatch.setenv("SINDIE_SIM_FAULTS", "spellman_refuse")
    r = c.request("spellman", f"{EINZEL}.cmd_enable", True)
    assert r["status"] == C.FAILED and "supply refused the enable" in r["reason"]


def test_enable_refused_while_the_supply_holds_a_setpoint_past_the_cap(stack):
    """A setpoint put in at the supply's front panel past the 22 kV cap: ENABLE would drive it."""
    plc, svc, tap = stack
    wait_powered(tap)
    from src.sim import spellman as simsp
    device = next(iter(next(iter(simsp._BUSES.values())).devices.values()))
    device.v_set = 25000.0                                             # volts, held by the supply
    tap.wait_for(lambda d: d[f"{EINZEL}.sp_actual_voltage"] == pytest.approx(25.0), timeout=5)
    c = C.CommandClient(default_timeout=3.0)
    r = c.request("spellman", f"{EINZEL}.cmd_enable", True)
    assert r["status"] == C.REJECTED and "voltage setpoint the supply holds 25 kV" in r["reason"]
    assert "22 kV cap" in r["reason"] and not device.enabled


def test_a_setpoint_held_at_the_cap_reads_as_at_it(stack):
    """The supply reports its setpoint through its DAC: exactly 22 kV may read back a hair over."""
    plc, svc, tap = stack
    wait_powered(tap)
    from src.sim import spellman as simsp
    device = next(iter(next(iter(simsp._BUSES.values())).devices.values()))
    device.v_set = 22003.0
    tap.wait_for(lambda d: d[f"{EINZEL}.sp_actual_voltage"] == pytest.approx(22.003), timeout=5)
    c = C.CommandClient(default_timeout=3.0)
    assert c.request("spellman", f"{EINZEL}.cmd_enable", True)["status"] == C.OK
