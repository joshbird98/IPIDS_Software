"""Vacuum gauge service against the simulated GRAPHIX controllers."""
import pytest

from src.core import commands as C
from src.core.network_map import ZMQ_PORT_VACUUM_PUB
from tests.service_harness import ServiceThread, TelemetryTap

VG1 = "ion_beam.source.vacuum_gauge_1"
PERMISSIVE = "ion_beam.vacuum.gv_permissive_ready"


@pytest.fixture
def stack(monkeypatch):
    monkeypatch.setenv("SINDIE_SIMULATE", "1")
    monkeypatch.delenv("SINDIE_SIM_FAULTS", raising=False)
    from src.sim import vacuum as simvac
    simvac._GRAPHIX = None
    from src.services.service_vac_gauge_controllers import VacuumMicroservice
    svc = VacuumMicroservice()
    runner = ServiceThread(svc)
    tap = TelemetryTap(ZMQ_PORT_VACUUM_PUB, prefix=VG1)
    client = C.CommandClient(default_timeout=5.0)
    yield svc, tap, client, monkeypatch
    client.close()
    runner.stop()
    tap.close()


def test_pressures_and_gate_valve_permissive(stack):
    svc, tap, client, mp = stack
    d = tap.wait_for(lambda d: d.get(f"{VG1}.rb_pressure") is not None and d.get(PERMISSIVE) == 1.0, timeout=15)
    assert 1e-8 < d[f"{VG1}.rb_pressure"] < 1e-5
    assert d["ion_beam.facilities.graphix1.stat_comms_fail"] == 0.0
    assert d[f"{VG1}.name"] == "Source VG1"                    # static data read on connect


def test_venting_source_loses_the_permissive(stack):
    svc, tap, client, mp = stack
    tap.wait_for(lambda d: d.get(PERMISSIVE) == 1.0, timeout=15)
    mp.setenv("SINDIE_SIM_FAULTS", "vacuum_vent_source")
    tap.wait_for(lambda d: d.get(PERMISSIVE) == 0.0, timeout=30)


def test_disconnect_is_unknown_and_recovers(stack):
    svc, tap, client, mp = stack
    tap.wait_for(lambda d: d.get(f"{VG1}.rb_pressure") is not None, timeout=15)
    mp.setenv("SINDIE_SIM_FAULTS", "vacuum_disconnect")
    d = tap.wait_for(lambda d: d.get(f"{VG1}.rb_pressure") is None
                     and d["ion_beam.facilities.graphix1.stat_comms_fail"] == 1.0, timeout=10)
    assert d[PERMISSIVE] == 0.0 and d["ion_beam.vacuum.permit"] == 0.0
    mp.delenv("SINDIE_SIM_FAULTS")
    tap.wait_for(lambda d: d.get(f"{VG1}.rb_pressure") is not None, timeout=20)


def test_relay_set_point_command(stack):
    svc, tap, client, mp = stack
    tap.wait_for(lambda d: d.get(f"{VG1}.rb_pressure") is not None, timeout=15)
    tag = "ion_beam.vacuum.controller_10.relay_1_on"
    svc.registry_limits.pop(tag, None)          # whatever the registry says, the write path is tested
    r = client.request("vacuum", tag, 4e-6)
    assert r["status"] == C.OK and "4.0E-06" in r["reason"]
    from src.sim.vacuum import shared_graphix
    assert shared_graphix().relays[(10, 1)]["on"] == pytest.approx(4e-6)
    r = client.request("vacuum", "ion_beam.vacuum.controller_10.relay_9_on", 1e-6)
    assert r["status"] == C.REJECTED
