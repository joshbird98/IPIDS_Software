"""Spellman service against simulated supplies - including the 'enable state stays
latched after the safety relay trips' bug reported on the real machine, and the
software arc detection (output collapsed below 10 % of the setpoint)."""
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
    monkeypatch.setenv("IPIDS_SIMULATE", "1")
    monkeypatch.delenv("IPIDS_SIM_FAULTS", raising=False)
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
    """What the HMI does: voltage, the automatic 50 uA current limit, then enable."""
    assert c.request("spellman", f"{EINZEL}.sp_requested_voltage", kv)["status"] == C.OK
    assert c.request("spellman", f"{EINZEL}.sp_requested_current", 50.0)["status"] == C.OK
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
    assert r["status"] == C.REJECTED and "outside the allowed range" in r["reason"]


def test_current_limit_comes_from_mpd_config(stack):
    """max_current_ua in mpd_config.json (the MPD30's 330 uA); before it existed every
    current-limit command - including the HMI's automatic 50 uA - was refused."""
    plc, svc, tap = stack
    wait_powered(tap)
    c = C.CommandClient(default_timeout=3.0)
    assert c.request("spellman", f"{EINZEL}.sp_requested_current", 330.0)["status"] == C.OK
    r = c.request("spellman", f"{EINZEL}.sp_requested_current", 400.0)
    assert r["status"] == C.REJECTED and "outside the allowed range" in r["reason"]


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
        monkeypatch.setenv("IPIDS_SIM_FAULTS", "spellman_no_reply")
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

    monkeypatch.setenv("IPIDS_SIM_FAULTS", "spellman_arc")         # output collapses to ~10 V
    d = tap.wait_for(lambda d: d[ARC] == 1.0, timeout=3)
    assert d[f"{EINZEL}.rb_voltage"] < 0.6

    monkeypatch.delenv("IPIDS_SIM_FAULTS")                         # arc over, output recovers ...
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
    monkeypatch.setenv("IPIDS_SIM_FAULTS", "spellman_arc")
    tap.wait_for(lambda d: d[f"{EINZEL}.rb_voltage"] < 0.03, timeout=3)
    assert tap.holds_for(lambda d: d[ARC] == 0.0, 4.0)   # longer than settle_s


# ---------------------------------------------------------------- ArcDetector logic


def feed(det, samples, enabled=True, ua=1.0):
    """samples: (t, sp_kv, rb_kv) -> list of results."""
    return [det.update(enabled, sp, rb, ua, t) for t, sp, rb in samples]


def test_collapse_after_the_output_was_up():
    det = ArcDetector()
    assert feed(det, [(0.0, 6.0, 0.2), (0.3, 6.0, 6.0), (0.6, 6.0, 5.99)]) == [None, None, None]
    why = det.update(True, 6.0, 0.4, 50.0, 0.9)       # 0.4 kV < 10 % of 6 kV
    assert why and "collapsed" in why and "6.000 kV" in why
    assert det.update(True, 6.0, 0.7, 50.0, 1.2) is None   # 0.7 kV > 0.6 kV: not collapsed


def test_short_at_switch_on_is_caught_after_settle_time():
    det = ArcDetector()
    assert all(r is None for r in feed(det, [(t / 10, 6.0, 0.01) for t in range(30)]))   # 0 .. 2.9 s
    assert det.update(True, 6.0, 0.01, 50.0, 3.0)


def test_large_step_up_does_not_trip_while_ramping():
    det = ArcDetector()
    assert feed(det, [(0.0, 0.6, 0.6), (1.0, 0.6, 0.6)]) == [None, None]
    # 0.6 kV -> 10 kV: the output (0.6 kV) is below 10 % of the new setpoint while it ramps
    assert feed(det, [(1.1, 10.0, 0.6), (1.4, 10.0, 0.9), (1.7, 10.0, 5.5), (2.0, 10.0, 9.9)]) == [None] * 4
    assert det.update(True, 10.0, 0.5, 50.0, 2.3)     # up and then collapsed: arc


def test_small_steps_keep_watching():
    """The optimiser moves the setpoint in small steps; detection must not pause for each."""
    det = ArcDetector()
    feed(det, [(0.0, 6.0, 6.0)])
    assert det.update(True, 6.1, 6.0, 50.0, 0.1) is None   # new setpoint, output already near it
    assert det.update(True, 6.1, 0.2, 50.0, 0.2)            # collapse caught on the next sample


def test_low_setpoint_is_not_judged():
    det = ArcDetector()
    assert all(r is None for r in feed(det, [(t, 0.4, 0.0) for t in range(10)]))
    det2 = ArcDetector({"min_setpoint_kv": 0.1})
    assert det2.update(True, 0.4, 0.0, 1.0, 0.0) is None
    assert det2.update(True, 0.4, 0.0, 1.0, 5.0)


def test_disable_and_restart_reset_the_watch():
    det = ArcDetector()
    feed(det, [(0.0, 6.0, 6.0)])
    assert det.update(False, 6.0, 0.0, 0.0, 1.0) is None      # switched off: output falls, not an arc
    assert det.update(True, 6.0, 0.0, 0.0, 1.1) is None       # switched on again: wait for it
    det.restart()                                             # (enable command accepted)
    assert det.update(True, 6.0, 0.05, 0.0, 1.2) is None


def test_missing_readings_are_skipped_not_judged():
    det = ArcDetector()
    feed(det, [(0.0, 6.0, 6.0)])
    assert det.update(True, 6.0, None, None, 0.5) is None
    assert det.update(True, None, 0.0, None, 0.6) is None
    assert det.update(True, 6.0, 0.1, None, 0.7)              # still armed afterwards


def test_overcurrent_counts_as_arc():
    det = ArcDetector()
    feed(det, [(0.0, 6.0, 6.0)])
    why = det.update(True, 6.0, 5.9, 600.0, 0.3)
    assert why and "current" in why


def test_arc_settings_merge_and_validation():
    cfg = {"arc_detection": {"_comment": "x", "collapse_fraction": 0.2, "settle_s": "fast", "bogus": 1}}
    s = arc_settings(cfg, {"arc_detection": {"min_setpoint_kv": 1.0}})
    assert s["collapse_fraction"] == 0.2 and s["min_setpoint_kv"] == 1.0
    assert s["settle_s"] == ARC_DEFAULTS["settle_s"] and "bogus" not in s
    assert arc_settings({}, {}) == ARC_DEFAULTS
