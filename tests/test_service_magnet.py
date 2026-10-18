"""Analyser magnet service (the same module runs the steerer: test_service_steerer.py)."""
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
