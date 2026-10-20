"""Source turbo pump service against the simulated TURBOVAC."""
import pytest

from src.core import commands as C
from src.core.network_map import ZMQ_PORT_SRC_TURBO_PUB
from tests.service_harness import ServiceThread, TelemetryTap

B = "ion_beam.source.turbo_pump"


@pytest.fixture
def stack(monkeypatch):
    monkeypatch.setenv("SINDIE_SIMULATE", "1")
    monkeypatch.delenv("SINDIE_SIM_FAULTS", raising=False)
    from src.sim import turbo as simturbo
    simturbo._PUMP.hz, simturbo._PUMP.error_code = 0.0, 0
    from src.services.service_source_turbo import TurbovacMicroservice
    svc = TurbovacMicroservice()
    runner = ServiceThread(svc)
    tap = TelemetryTap(ZMQ_PORT_SRC_TURBO_PUB, prefix=B)
    client = C.CommandClient(default_timeout=3.0)
    yield svc, tap, client, monkeypatch
    client.close()
    runner.stop()
    tap.close()


def test_strategy_checked_and_start(stack):
    svc, tap, client, mp = stack
    d = tap.wait_for(lambda d: d[f"{B}.stat_comms_fail"] == 0.0 and d["system.safety_synced"] == 1.0, timeout=8)
    assert d[f"{B}.permit"] == 1.0 and d[f"{B}.rb_speed_hz"] == 0.0
    assert d[f"{B}.stat_ready"] == 0.0                                     # stopped is not "at speed"
    assert client.request("src_turbo", f"{B}.cmd_enable", True)["status"] == C.OK
    tap.wait_for(lambda d: d[f"{B}.rb_speed_hz"] > 20 and d[f"{B}.stat_turning"] == 1.0, timeout=5)
    r = client.request("src_turbo", f"{B}.cmd_spin_faster", 1)
    assert r["status"] == C.REJECTED and "unsupported" in r["reason"]


def test_the_software_never_stops_the_pump(stack):
    """Trained staff stop the source turbo pump by hand, at the pump (Josh, October 2026). A stop is
    refused, and neither a start nor a fault reset sends the reset word (which has the run bit clear)
    to a pump running without a fault."""
    svc, tap, client, mp = stack
    tap.wait_for(lambda d: d[f"{B}.stat_comms_fail"] == 0.0 and d["system.safety_synced"] == 1.0, timeout=8)
    assert client.request("src_turbo", f"{B}.cmd_enable", True)["status"] == C.OK
    tap.wait_for(lambda d: d[f"{B}.rb_speed_hz"] > 20, timeout=5)
    words = []
    send = svc.link.transaction

    def spy(pnu, index, value=0, ak=1, control_word=0x0400):
        words.append(control_word)
        return send(pnu, index, value, ak, control_word)
    mp.setattr(svc.link, "transaction", spy)
    for value in (False, 0):
        r = client.request("src_turbo", f"{B}.cmd_enable", value)
        assert r["status"] == C.REJECTED and "by hand" in r["reason"]
    r = client.request("src_turbo", f"{B}.cmd_enable", True)
    assert r["status"] == C.OK and "already running" in r["reason"]
    r = client.request("src_turbo", f"{B}.cmd_reset", True)
    assert r["status"] == C.OK and "no fault" in r["reason"]
    first = tap.last[f"{B}.rb_speed_hz"]
    tap.wait_for(lambda d: d[f"{B}.rb_speed_hz"] > first + 5, timeout=5)    # still running up
    assert words and all(w & 0x0001 for w in words), [hex(w) for w in words]   # never a word without RUN


def test_unreachable_controller_is_unknown_then_recovers(stack):
    svc, tap, client, mp = stack
    tap.wait_for(lambda d: d[f"{B}.stat_comms_fail"] == 0.0, timeout=8)
    mp.setenv("SINDIE_SIM_FAULTS", "turbo_disconnect")
    d = tap.wait_for(lambda d: d[f"{B}.stat_comms_fail"] == 1.0, timeout=8)
    assert d[f"{B}.rb_speed_hz"] is None and d[f"{B}.permit"] == 0.0
    r = client.request("src_turbo", f"{B}.cmd_enable", True)
    assert r["status"] in (C.REJECTED, C.FAILED)
    mp.delenv("SINDIE_SIM_FAULTS")
    tap.wait_for(lambda d: d[f"{B}.stat_comms_fail"] == 0.0 and d[f"{B}.rb_speed_hz"] is not None, timeout=15)


def test_pump_error_shows_and_resets(stack):
    svc, tap, client, mp = stack
    tap.wait_for(lambda d: d[f"{B}.stat_comms_fail"] == 0.0, timeout=8)
    mp.setenv("SINDIE_SIM_FAULTS", "turbo_error")
    tap.wait_for(lambda d: d[f"{B}.stat_error"] == 1.0 and d[f"{B}.stat_trip"] == 1.0, timeout=5)
    mp.delenv("SINDIE_SIM_FAULTS")
    assert client.request("src_turbo", f"{B}.cmd_reset", True)["status"] == C.OK
    tap.wait_for(lambda d: d[f"{B}.stat_error"] == 0.0, timeout=5)
