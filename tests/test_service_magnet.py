"""Analyser magnet service (the same module runs the steerer: test_service_steerer.py)."""
import time

import pytest

from src.core import commands as C
from src.core.network_map import ZMQ_PORT_MAGNET_PUB
from tests.service_harness import FakePlcPublisher, ServiceThread, TelemetryTap

BASE = "ion_beam.beamline.magnet"


@pytest.fixture
def stack(monkeypatch):
    monkeypatch.setenv("SINDIE_SIMULATE", "1")
    monkeypatch.delenv("SINDIE_SIM_FAULTS", raising=False)
    from src.sim import magnet as simmag
    simmag._STATE.clear()
    plc = FakePlcPublisher(relay=True)
    from src.services.service_magnet_psu import MagnetPsuService
    svc = MagnetPsuService("magnet")
    runner = ServiceThread(svc)
    tap = TelemetryTap(ZMQ_PORT_MAGNET_PUB, prefix=BASE)
    client = C.CommandClient(default_timeout=3.0)
    yield plc, svc, tap, client
    client.close()
    runner.stop()
    plc.stop()
    tap.close()


def test_voltage_limit_leads_the_current_up_and_follows_it_down(stack):
    plc, svc, tap, client = stack
    tap.wait_for(lambda d: d["system.safe_to_run"] == 1.0 and d[f"{BASE}.stat_comms_fail"] == 0.0, timeout=5)
    order = []
    hw = svc.hw
    real_v, real_i = hw.set_voltage, hw.set_current
    hw.set_voltage = lambda v: (order.append(("V", v)), real_v(v))[1]
    hw.set_current = lambda i: (order.append(("I", i)), real_i(i))[1]
    assert client.request("magnet", f"{BASE}.sp_requested_current", 20.0)["status"] == C.OK
    assert [k for k, _ in order[:2]] == ["V", "I"]                      # up: voltage first
    assert order[0][1] == pytest.approx(max(1.0, 20.0 * 0.16 * 1.5))
    order.clear()
    assert client.request("magnet", f"{BASE}.sp_requested_current", 5.0)["status"] == C.OK
    assert [k for k, _ in order[:2]] == ["I", "V"]                      # down: current first
    r = client.request("magnet", f"{BASE}.sp_requested_voltage", 5.0)
    assert r["status"] == C.REJECTED and "automatically" in r["reason"]


def test_degauss_steps_through_the_sequence(stack):
    plc, svc, tap, client = stack
    tap.wait_for(lambda d: d["system.safe_to_run"] == 1.0 and d[f"{BASE}.stat_comms_fail"] == 0.0, timeout=5)
    svc.limits["degauss_steps_amps"] = [6.0, 0.0, 3.0, 0.0]
    svc.limits["degauss_step_time_sec"] = 0.1
    assert client.request("magnet", f"{BASE}.cmd_degauss", True)["status"] == C.OK
    tap.wait_for(lambda d: d[f"{BASE}.stat_degaussing"] == 1.0, timeout=2)
    tap.wait_for(lambda d: d[f"{BASE}.stat_degaussing"] == 0.0, timeout=5)
    assert svc.sp_cache[f"{BASE}.sp_actual_current"] == 0.0


def test_relay_trip_stops_degauss_and_holds_off(stack):
    plc, svc, tap, client = stack
    tap.wait_for(lambda d: d["system.safe_to_run"] == 1.0 and d[f"{BASE}.stat_comms_fail"] == 0.0, timeout=5)
    svc.limits["degauss_step_time_sec"] = 0.2
    assert client.request("magnet", f"{BASE}.cmd_degauss", True)["status"] == C.OK
    plc.relay = False
    d = tap.wait_for(lambda d: d["system.safe_to_run"] == 0.0 and d[f"{BASE}.stat_degaussing"] == 0.0
                     and d[f"{BASE}.stat_enabled"] == 0.0, timeout=3)
    assert d[f"{BASE}.permit"] == 0.0 and "relay" in d[f"{BASE}.permit_reason"]
    r = client.request("magnet", f"{BASE}.cmd_degauss", True)
    assert r["status"] == C.REJECTED and "relay" in r["reason"]


def test_unknown_ratings_allow_only_zero_and_off(stack, fake_events):
    """With the PSU's nominal ratings unknown (see test_magnet_driver.py; the simulated PSU refuses the
    same things) the service refuses anything that needs them, but zero and output off still go
    through - the steerer's only interlock - and the link is not dropped over it."""
    plc, svc, tap, client = stack
    tap.wait_for(lambda d: d["system.safe_to_run"] == 1.0 and d[f"{BASE}.stat_comms_fail"] == 0.0, timeout=5)
    assert client.request("magnet", f"{BASE}.sp_requested_current", 5.0)["status"] == C.OK
    assert client.request("magnet", f"{BASE}.cmd_enable", True)["status"] == C.OK
    svc.events = fake_events
    svc.hw.ratings_ok = False                   # as after a reconnect that couldn't read them
    d = tap.wait_for(lambda d: d[f"{BASE}.rb_current"] is None, timeout=2)
    assert d[f"{BASE}.rb_resistance"] is None and d[f"{BASE}.stat_comms_fail"] == 0.0
    warned = [e for e in fake_events.events if "nominal ratings" in e["message"]]
    assert len(warned) == 1 and warned[0]["severity"] == "WARNING" and "every few seconds" in warned[0]["message"]
    for tag, value in (("sp_requested_current", 5.0), ("cmd_enable", True), ("cmd_degauss", True)):
        r = client.request("magnet", f"{BASE}.{tag}", value)
        assert r["status"] == C.REJECTED and "nominal ratings" in r["reason"], (tag, r)
    assert client.request("magnet", f"{BASE}.sp_requested_current", 0.0)["status"] == C.OK
    assert svc.sp_cache[f"{BASE}.sp_actual_voltage"] == 0.0                # 0 A: 0 V, not the 1 V minimum
    assert client.request("magnet", f"{BASE}.cmd_enable", False)["status"] == C.OK
    assert svc.hw.connected and svc.hw.psu.i_set == 0.0 and svc.hw.psu.v_set == 0.0 and not svc.hw.psu.output
    svc.hw.ratings_retrying = False             # the PSU didn't answer a retry: only a reconnect asks again
    deadline = time.time() + 2
    while len(warned := [e for e in fake_events.events if "nominal ratings" in e["message"]]) < 2 \
            and time.time() < deadline:
        time.sleep(0.05)
    assert len(warned) == 2 and "restart the service" in warned[-1]["message"]
    svc.hw.ratings_ok = True
    tap.wait_for(lambda d: d[f"{BASE}.rb_current"] is not None, timeout=2)
    assert any("setpoints accepted again" in e["message"] for e in fake_events.events)


def test_degauss_cut_short_by_missing_ratings_ends_at_zero(stack, fake_events):
    """A degauss carries on through a reconnect. If the PSU's ratings can't be read after it, the next
    step is refused: the sequence then stops at 0 A, where it would have ended, not at the last step."""
    plc, svc, tap, client = stack
    tap.wait_for(lambda d: d["system.safe_to_run"] == 1.0 and d[f"{BASE}.stat_comms_fail"] == 0.0, timeout=5)
    svc.events = fake_events
    svc.limits["degauss_steps_amps"] = [6.0, 3.0, 0.0]
    svc.limits["degauss_step_time_sec"] = 1.0
    assert client.request("magnet", f"{BASE}.cmd_degauss", True)["status"] == C.OK
    tap.wait_for(lambda d: d[f"{BASE}.sp_actual_current"] == 6.0, timeout=2)
    svc.hw.ratings_ok = False
    tap.wait_for(lambda d: d[f"{BASE}.stat_degaussing"] == 0.0, timeout=3)
    errors = [e for e in fake_events.events if e["severity"] == "ERROR"]
    assert len(errors) == 1 and "Degauss aborted" in errors[0]["message"] and "set to 0 A" in errors[0]["message"]
    assert svc.sp_cache[f"{BASE}.sp_actual_current"] == 0.0 and svc.hw.psu.i_set == 0.0 and svc.hw.connected
