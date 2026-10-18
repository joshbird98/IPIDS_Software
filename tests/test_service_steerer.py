"""X-steerer magnet service: software safety-relay interlock (the PSU is not on the
relay circuit, so the service has to hold its output off itself)."""
import pytest

from src.core import commands as C
from src.core.network_map import ZMQ_PORT_STEERER_MAGNET_PUB
from tests.service_harness import FakePlcPublisher, ServiceThread, TelemetryTap

BASE = "ion_beam.beamline.steerer_magnet"
SAFE = "system.safe_to_run"


@pytest.fixture
def stack(monkeypatch):
    monkeypatch.setenv("SINDIE_SIMULATE", "1")
    monkeypatch.delenv("SINDIE_SIM_FAULTS", raising=False)
    from src.sim import magnet as simmag
    simmag._STATE.clear()
    plc = FakePlcPublisher(relay=True)
    from src.services.service_steerer_magnet_psu import SteererMagnetMicroservice
    svc = SteererMagnetMicroservice()
    runner = ServiceThread(svc)
    tap = TelemetryTap(ZMQ_PORT_STEERER_MAGNET_PUB, prefix=BASE)
    client = C.CommandClient(default_timeout=3.0)
    yield plc, svc, tap, client
    client.close()
    runner.stop()
    plc.stop()
    tap.close()


def steer(client, tap, amps):
    assert client.request("steerer_magnet", f"{BASE}.sp_requested_current", amps)["status"] == C.OK
    assert client.request("steerer_magnet", f"{BASE}.cmd_enable", True)["status"] == C.OK
    return tap.wait_for(lambda d: d[f"{BASE}.stat_enabled"] == 1.0
                        and abs(d[f"{BASE}.rb_current"] - amps) < 0.1, timeout=8)


def test_relay_trip_forces_output_off_and_stays_off(stack):
    plc, svc, tap, client = stack
    tap.wait_for(lambda d: d[SAFE] == 1.0 and d[f"{BASE}.stat_comms_fail"] == 0.0, timeout=5)
    steer(client, tap, 5.0)

    plc.relay = False
    d = tap.wait_for(lambda d: d[SAFE] == 0.0 and d[f"{BASE}.stat_enabled"] == 0.0, timeout=3)
    assert d[f"{BASE}.sp_actual_current"] == 0.0
    tap.wait_for(lambda d: d[f"{BASE}.rb_current"] < 0.05, timeout=8)
    r = client.request("steerer_magnet", f"{BASE}.cmd_enable", True)
    assert r["status"] == C.REJECTED and "relay" in r["reason"]
    r = client.request("steerer_magnet", f"{BASE}.sp_requested_current", 3.0)
    assert r["status"] == C.REJECTED
    assert client.request("steerer_magnet", f"{BASE}.cmd_enable", False)["status"] == C.OK
    # control leases never touch the hardware, so they work while interlocked
    lease = {"op": "claim", "holder": "test", "purpose": "test", "ttl": 5}
    assert client.request("steerer_magnet", f"{BASE}.cmd_lease", lease)["status"] == C.OK
    assert client.request("steerer_magnet", f"{BASE}.cmd_lease", dict(lease, op="release"))["status"] == C.OK

    plc.relay = True    # permissive back: the output does NOT come back on by itself
    tap.wait_for(lambda d: d[SAFE] == 1.0, timeout=3)
    assert tap.holds_for(lambda d: d[f"{BASE}.stat_enabled"] == 0.0, 1.0)
    steer(client, tap, 2.0)


@pytest.mark.parametrize("how", ["plc_silent", "relay_unknown"])
def test_no_or_unknown_plc_data_counts_as_tripped(stack, how):
    plc, svc, tap, client = stack
    tap.wait_for(lambda d: d[SAFE] == 1.0 and d[f"{BASE}.stat_comms_fail"] == 0.0, timeout=5)
    steer(client, tap, 4.0)
    if how == "plc_silent":
        plc.paused = True       # PLC service stopped publishing
    else:
        plc.relay = None        # PLC service up, PLC itself unreachable (publishes null)
    tap.wait_for(lambda d: d[SAFE] == 0.0 and d[f"{BASE}.stat_enabled"] == 0.0, timeout=3)


def test_output_switched_on_behind_its_back_is_switched_off_again(stack):
    """If something else turns the PSU output on while interlocked, it is turned off again."""
    plc, svc, tap, client = stack
    plc.relay = False
    tap.wait_for(lambda d: d[SAFE] == 0.0 and d[f"{BASE}.stat_comms_fail"] == 0.0, timeout=5)
    svc.hw.psu.output = True    # e.g. the PSU's front panel
    tap.wait_for(lambda d: d[f"{BASE}.stat_enabled"] == 1.0, timeout=2)
    tap.wait_for(lambda d: d[f"{BASE}.stat_enabled"] == 0.0, timeout=2)
