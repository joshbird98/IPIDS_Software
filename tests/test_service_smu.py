"""SMU (Faraday cup bias) service: what happens to the bias when the SMU reconnects.

Rule: the previous bias is re-applied only when fresh PLC data confirms the safety
relay is on; otherwise the SMU comes back reset (0 V, bias off) and the old setpoint
is forgotten - including when the relay tripped while the SMU was disconnected.
"""
import pytest

from src.core import commands as C
from src.core.network_map import ZMQ_PORT_SMU_PUB
from tests.service_harness import FakePlcPublisher, ServiceThread, TelemetryTap

BASE = "ion_beam.beamline.faraday.smu"
EN, RB, SP, COMMS = f"{BASE}.stat_enabled", f"{BASE}.rb_voltage", f"{BASE}.sp_actual_voltage", f"{BASE}.stat_comms_fail"


@pytest.fixture
def stack(monkeypatch):
    monkeypatch.setenv("SINDIE_SIMULATE", "1")
    monkeypatch.delenv("SINDIE_SIM_FAULTS", raising=False)
    plc = FakePlcPublisher(relay=True)
    from src.services.service_smu import SmuMicroservice
    svc = SmuMicroservice()
    runner = ServiceThread(svc)
    tap = TelemetryTap(ZMQ_PORT_SMU_PUB, prefix=BASE)
    client = C.CommandClient(default_timeout=3.0)
    yield plc, svc, tap, client, monkeypatch
    client.close()
    runner.stop()
    plc.stop()
    tap.close()


def bias_on(client, tap, volts=50.0):
    tap.wait_for(lambda d: d[COMMS] == 0.0 and d["system.safe_to_run"] == 1.0, timeout=6)
    assert client.request("smu", f"{BASE}.cmd_enable", True)["status"] == C.OK
    assert client.request("smu", f"{BASE}.sp_requested_voltage", volts)["status"] == C.OK
    tap.wait_for(lambda d: d[EN] == 1.0 and abs(d[RB] - volts) < 0.01, timeout=5)


def drop_link(tap, monkeypatch):
    monkeypatch.setenv("SINDIE_SIM_FAULTS", "smu_disconnect")
    d = tap.wait_for(lambda d: d[COMMS] == 1.0, timeout=5)
    assert d[EN] is None and d[SP] is None            # unknown while disconnected


def restore_link(tap, monkeypatch):
    monkeypatch.delenv("SINDIE_SIM_FAULTS")
    return tap.wait_for(lambda d: d[COMMS] == 0.0 and d[EN] is not None and d[RB] is not None, timeout=15)


def test_first_connect_starts_with_bias_off(stack):
    plc, svc, tap, client, mp = stack
    d = tap.wait_for(lambda d: d[COMMS] == 0.0 and d[EN] is not None, timeout=6)
    assert d[EN] == 0.0 and d[SP] == 0.0


def test_bias_restored_after_reconnect_with_relay_confirmed_on(stack):
    plc, svc, tap, client, mp = stack
    bias_on(client, tap, 50.0)
    drop_link(tap, mp)
    d = restore_link(tap, mp)
    d = tap.wait_for(lambda d: d[EN] == 1.0 and abs(d[RB] - 50.0) < 0.01, timeout=3)
    assert d[SP] == 50.0


def test_bias_reset_when_relay_off_at_reconnect(stack):
    plc, svc, tap, client, mp = stack
    bias_on(client, tap, 50.0)
    drop_link(tap, mp)
    plc.relay = False
    tap.wait_for(lambda d: d["system.safe_to_run"] == 0.0, timeout=3)
    restore_link(tap, mp)
    d = tap.wait_for(lambda d: d[EN] == 0.0, timeout=3)
    assert abs(d[RB]) < 0.01 and d[SP] == 0.0
    plc.relay = True                                   # relay back: the bias stays off
    tap.wait_for(lambda d: d["system.safe_to_run"] == 1.0, timeout=3)
    assert tap.holds_for(lambda d: d[EN] == 0.0 and abs(d[RB]) < 0.01, 1.0)


def test_relay_trip_while_disconnected_forgets_the_bias(stack):
    """The relay tripped and came back while the SMU was unreachable: the trip reset the
    bias, so the reconnect must not bring back the value from before it."""
    plc, svc, tap, client, mp = stack
    bias_on(client, tap, 50.0)
    drop_link(tap, mp)
    plc.relay = False
    tap.wait_for(lambda d: d["system.safe_to_run"] == 0.0, timeout=3)
    plc.relay = True
    tap.wait_for(lambda d: d["system.safe_to_run"] == 1.0, timeout=3)
    restore_link(tap, mp)
    assert tap.holds_for(lambda d: d[EN] == 0.0 and abs(d[RB]) < 0.01, 1.0)


def test_silent_plc_at_reconnect_is_not_confirmation(stack):
    plc, svc, tap, client, mp = stack
    bias_on(client, tap, 50.0)
    drop_link(tap, mp)
    plc.paused = True                                  # PLC service stops publishing
    tap.wait_for(lambda d: d["system.safe_to_run"] == 0.0, timeout=3)
    restore_link(tap, mp)
    assert tap.holds_for(lambda d: d[EN] == 0.0, 1.0)


def test_bias_switched_off_by_operator_stays_off_after_reconnect(stack):
    """Before: the cached setpoint survived 'disable', so a reconnect switched the bias back on."""
    plc, svc, tap, client, mp = stack
    bias_on(client, tap, 50.0)
    assert client.request("smu", f"{BASE}.cmd_enable", False)["status"] == C.OK
    tap.wait_for(lambda d: d[EN] == 0.0 and d[SP] == 0.0, timeout=3)
    drop_link(tap, mp)
    restore_link(tap, mp)
    assert tap.holds_for(lambda d: d[EN] == 0.0 and abs(d[RB]) < 0.01, 1.0)
