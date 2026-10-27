"""The source turbo protection end to end: the turbo service's load guard on the simulated pump with
its backing valve shut (src/sim/turbo.py: turbo_backing_closed), and service_plc shutting the source
down on its trip - with stages minutes long here instead of 2, 10 and 20 minutes."""
import os
import threading
import time

import orjson
import pytest
import zmq

from src.core import commands as C
from src.core import turbo_guard as tg
from src.core.commands import bind_retrying
from src.core.network_map import TOPIC_SRC_TURBO_DATA, ZMQ_PORT_SRC_TURBO_PUB
from tests.service_harness import ServiceThread, TelemetryTap

B = "ion_beam.source.turbo_pump"
FAST = tg.GuardConfig(settle_s=0.0, smooth_s=2.0, warn_after_s=2.0, critical_after_s=2.0, trip_after_s=5.0,
                      pump_warning_trip_s=60.0, falling_window_s=60.0, clear_after_s=2.0)


@pytest.fixture
def turbo(monkeypatch):
    """The turbo service on the simulated pump, already at full speed, with FAST stages."""
    monkeypatch.setenv("SINDIE_SIMULATE", "1")
    monkeypatch.delenv("SINDIE_SIM_FAULTS", raising=False)
    from src.sim import turbo as simturbo
    pump = simturbo._PUMP
    pump.hz, pump.error_code, pump.load_a, pump.heat_c = float(simturbo.MAX_HZ), 0, 0.0, 0.0
    pump.clock = simturbo.Clock()           # (its last tick may be an earlier test's: no coasting down meanwhile)
    from src.services import service_source_turbo as st
    from src.core import paths
    state_file = os.path.join(paths.DATA_DIR, st.GUARD_STATE)
    if os.path.exists(state_file):
        os.remove(state_file)
    svc = st.TurbovacMicroservice()
    svc.guard = tg.TurboGuard(FAST)
    runner = ServiceThread(svc)
    tap = TelemetryTap(ZMQ_PORT_SRC_TURBO_PUB, prefix=B)
    client = C.CommandClient(default_timeout=3.0)
    yield svc, tap, client, monkeypatch, state_file
    client.close()
    runner.stop()
    tap.close()
    if os.path.exists(state_file):
        os.remove(state_file)
    pump.hz, pump.error_code, pump.load_a, pump.heat_c = 0.0, 0, 0.0, 0.0     # (the next test's pump as new)


def test_the_guard_warns_then_trips_on_the_turbos_own_service(turbo):
    svc, tap, client, mp, state_file = turbo
    d = tap.wait_for(lambda d: d[f"{B}.rb_current"] is not None and abs(d[f"{B}.rb_current"] - 0.8) < 0.11
                     and d[f"{B}.rb_temp_bearing"] is not None and d[f"{B}.rb_voltage"] is not None, timeout=10)
    assert d[f"{B}.rb_voltage"] == pytest.approx(24.2, abs=0.2)           # P4 is in 0.1 V (it read 242)
    assert d[f"{B}.rb_current"] == pytest.approx(0.8, abs=0.11)
    assert d[f"{B}.rb_status_word"] is not None and d[f"{B}.rb_guard_stage"] == 0.0
    assert d[f"{B}.stat_load_warning"] == d[f"{B}.stat_load_trip"] == 0.0 and d[f"{B}.guard_shuts_down_source"] == 1.0

    mp.setenv("SINDIE_SIM_FAULTS", "turbo_backing_closed")              # +0.4 A at once
    d = tap.wait_for(lambda d: d[f"{B}.stat_load_warning"] == 1.0, timeout=15)
    assert d[f"{B}.guard_message"].startswith("Source turbo working harder than normal: the source will be shut down")
    assert d[f"{B}.guard_shutdown_at"] > time.time()
    d = tap.wait_for(lambda d: d[f"{B}.stat_load_critical"] == 1.0, timeout=10)
    assert d[f"{B}.stat_load_warning"] == 0.0 and d[f"{B}.rb_guard_stage"] == 2.0     # one alarm at a time
    d = tap.wait_for(lambda d: d[f"{B}.stat_load_trip"] == 1.0, timeout=15)
    assert d[f"{B}.rb_guard_stage"] == 3.0 and d[f"{B}.guard_shutdown_at"] is None
    assert d[f"{B}.guard_message"].startswith("Source turbo protection tripped")
    assert svc.link.connected and d[f"{B}.rb_speed_hz"] > 0              # the turbo itself left running

    # RESET FAULTS: refused while it is overloaded; once the valve is open again, accepted
    r = client.request("src_turbo", "ion_beam.system.cmd_fault_reset", True)
    assert r["status"] == C.REJECTED and "still overloaded" in r["reason"]
    mp.delenv("SINDIE_SIM_FAULTS")
    tap.wait_for(lambda d: d[f"{B}.rb_current"] is not None and d[f"{B}.rb_current"] < 0.95, timeout=20)
    time.sleep(2.5)                                                     # (the guard's median catches up)
    r = client.request("src_turbo", "ion_beam.system.cmd_fault_reset", True)
    assert r["status"] == C.OK, r
    tap.wait_for(lambda d: d[f"{B}.stat_load_trip"] == 0.0 and d[f"{B}.rb_guard_stage"] == 0.0, timeout=5)
    assert client.request("src_turbo", "ion_beam.system.cmd_fault_reset", True)["reason"] == "nothing to reset here"


def test_a_trip_survives_a_restart_of_the_turbo_service(turbo):
    svc, tap, client, mp, state_file = turbo
    tap.wait_for(lambda d: d[f"{B}.rb_current"] is not None, timeout=10)
    mp.setenv("SINDIE_SIM_FAULTS", "turbo_backing_closed")
    tap.wait_for(lambda d: d[f"{B}.stat_load_trip"] == 1.0, timeout=30)
    time.sleep(0.3)
    with open(state_file) as f:
        assert orjson.loads(f.read())["stage"] == tg.TRIPPED
    from src.services.service_source_turbo import TurbovacMicroservice
    again = TurbovacMicroservice.__new__(TurbovacMicroservice)      # only its guard, as a restart builds it
    again.events = svc.events
    again.guard, again.guard_state_file = tg.TurboGuard(FAST), state_file
    again.guard.restore(again._load_guard_state())
    assert again.guard.stage == tg.TRIPPED and again.guard.message.startswith("Source turbo protection tripped")


# ---------------------------------------------------------------------------------------------- service_plc
class FakeTurbo:
    """Publishes the turbo service's guard flags, as service_source_turbo would."""

    def __init__(self):
        self.payload = {f"{B}.stat_load_trip": 0.0, f"{B}.guard_shuts_down_source": 1.0,
                        f"{B}.guard_message": "", f"{B}.stat_comms_fail": 0.0}
        self.running = True
        self.sock = zmq.Context.instance().socket(zmq.PUB)
        self.sock.setsockopt(zmq.LINGER, 0)
        bind_retrying(self.sock, ZMQ_PORT_SRC_TURBO_PUB)
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        while self.running:
            self.sock.send_multipart([TOPIC_SRC_TURBO_DATA, orjson.dumps(self.payload)])
            time.sleep(0.05)
        self.sock.close(0)

    def trip(self, on=True):
        self.payload = {**self.payload, f"{B}.stat_load_trip": 1.0 if on else 0.0,
                        f"{B}.guard_message": "Source turbo protection tripped at 10:45: ..." if on else ""}

    def stop(self):
        self.running = False
        self.t.join(2)


@pytest.fixture
def plc(monkeypatch):
    monkeypatch.setenv("SINDIE_SIMULATE", "1")
    monkeypatch.delenv("SINDIE_SIM_FAULTS", raising=False)
    from src.services.service_plc import PlcMicroservice
    from src.sim import plc as simplc
    simplc._PLC = None
    turbo = FakeTurbo()
    svc = PlcMicroservice()
    svc.SERVICE_TIMEOUT_SEC = 1e9               # no vacuum service here: the test sets the permissive
    t = threading.Thread(target=svc.run, daemon=True)
    t.start()
    deadline = time.time() + 5
    while not svc.connected and time.time() < deadline:
        time.sleep(0.05)
    assert svc.connected
    yield svc, simplc.shared_plc(), turbo, C.CommandClient(default_timeout=2.0)
    svc.stop()
    t.join(3)
    turbo.stop()


def wait(cond, timeout=5.0, what="condition"):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError(what)


def energise(sim, c):
    sim.set("ion_beam.source.chamber.stat_vac_ok_for_gv", 1)
    assert c.request("plc", "ion_beam.source.chamber.cmd_open_gv", True)["status"] == C.OK
    wait(lambda: sim.get("ion_beam.source.chamber.stat_gv_open") == 1.0, what="gate valve open")
    assert c.request("plc", "ion_beam.system.cmd_enable_safety", True)["status"] == C.OK
    wait(lambda: sim.get("ion_beam.facilities.safety_relay_active") == 1.0, what="relay closed")


def test_a_trip_shuts_the_source_down_and_keeps_it_off_until_reset(plc):
    svc, sim, turbo, c = plc
    energise(sim, c)
    assert svc.state["ion_beam.system.turbo_protection"] == 0.0
    turbo.trip()
    wait(lambda: sim.get("ion_beam.system.cmd_source_shutdown") == 1.0, what="INITIATE SHUTDOWN sent")
    wait(lambda: sim.get("ion_beam.source.cesium.cmd_force_cooling") == 1.0, what="forced cooling on")
    # (the simulated PLC has no shutdown sequence: INITIATE SHUTDOWN drops its relay)
    wait(lambda: sim.get("ion_beam.facilities.safety_relay_active") == 0.0, what="relay dropped")
    wait(lambda: svc._turbo_shutdown is None, what="the shutdown seen through")
    assert svc.state["ion_beam.system.turbo_protection"] == 1.0          # the trip still stands
    sim.set("ion_beam.system.cmd_source_shutdown", 0)
    for tag in ("ion_beam.system.cmd_enable_safety", "ion_beam.source.filament.cmd_enable"):
        r = c.request("plc", tag, True)
        assert r["status"] == C.REJECTED and "source turbo protection has tripped" in r["reason"], (tag, r)
    assert svc.state["ion_beam.source.filament.permit"] == 0.0
    assert "source turbo protection" in svc.state["ion_beam.source.filament.permit_reason"]
    turbo.trip(False)                                                    # RESET FAULTS on the turbo service
    wait(lambda: svc.state["ion_beam.system.turbo_protection"] == 0.0, what="protection lifted")
    r = c.request("plc", "ion_beam.system.cmd_enable_safety", True)
    assert r["status"] == C.OK, r


def test_a_shutdown_it_started_cant_be_aborted(plc):
    """While the PLC's sequence runs (here set by hand: the simulated PLC has none), ABORT SHUTDOWN,
    withdrawing the request and the forced cooling off are refused - even after a reset, until it ends."""
    svc, sim, turbo, c = plc
    energise(sim, c)
    sim.set("ion_beam.system.src_auto_sdown", 1)
    sim.set("ion_beam.system.pc_shutdown_step", 10)                      # e.g. an operator's shutdown, running
    wait(lambda: svc.state.get("ion_beam.system.pc_shutdown_step") == 10.0, what="step 10 read")
    turbo.trip()
    wait(lambda: svc._turbo_shutdown is not None and svc._turbo_shutdown["seen_running"], what="hold")
    assert sim.get("ion_beam.system.cmd_source_shutdown") in (0, 0.0, None, False)    # already running: not sent
    wait(lambda: sim.get("ion_beam.source.cesium.cmd_force_cooling") == 1.0, what="forced cooling on")
    for tag, value in (("ion_beam.system.cmd_cancel_shutdown", True), ("ion_beam.system.cmd_source_shutdown", False),
                       ("ion_beam.source.cesium.cmd_force_cooling", False)):
        r = c.request("plc", tag, value)
        assert r["status"] == C.REJECTED and "turbo protection" in r["reason"], (tag, r)
    turbo.trip(False)
    time.sleep(0.5)
    r = c.request("plc", "ion_beam.system.cmd_cancel_shutdown", True)
    assert r["status"] == C.REJECTED and "can't be aborted" in r["reason"]
    sim.set("ion_beam.system.pc_shutdown_step", 0)
    sim.set("ion_beam.system.src_auto_sdown", 0)
    wait(lambda: svc._turbo_shutdown is None, what="the shutdown has finished")
    assert svc.state["ion_beam.system.turbo_protection"] == 0.0
    assert c.request("plc", "ion_beam.source.cesium.cmd_force_cooling", False)["status"] == C.OK


def test_alarms_only_does_nothing_to_the_source(plc):
    svc, sim, turbo, c = plc
    energise(sim, c)
    turbo.payload = {**turbo.payload, f"{B}.guard_shuts_down_source": 0.0}
    turbo.trip()
    time.sleep(1.0)
    assert sim.get("ion_beam.system.cmd_source_shutdown") in (0, 0.0, None, False)
    assert sim.get("ion_beam.facilities.safety_relay_active") == 1.0
    assert svc.state["ion_beam.system.turbo_protection"] == 0.0
