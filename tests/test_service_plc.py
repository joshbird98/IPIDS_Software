import threading
import time

import orjson
import pytest
import zmq

from src.core import commands as C
from src.core.network_map import ZMQ_PORT_HEARTBEAT, ZMQ_PORT_PLC_PUB


@pytest.fixture
def plc_service(monkeypatch):
    monkeypatch.setenv("IPIDS_SIMULATE", "1")
    monkeypatch.delenv("IPIDS_SIM_FAULTS", raising=False)
    from src.services.service_plc import PlcMicroservice
    from src.sim import plc as simplc
    simplc._PLC = None  # fresh simulated PLC for each test
    svc = PlcMicroservice()
    t = threading.Thread(target=svc.run, daemon=True)
    t.start()
    deadline = time.time() + 5
    while not svc.connected and time.time() < deadline:
        time.sleep(0.05)
    assert svc.connected, "simulated PLC did not connect"
    yield svc
    svc.stop()
    t.join(3)


def _client():
    return C.CommandClient(default_timeout=2.0)


def _plc_value(tag):
    from src.sim.plc import shared_plc
    return shared_plc().get(tag)


def test_setpoint_written_and_acknowledged(plc_service):
    c = _client()
    r = c.request("plc", "ion_beam.source.extraction.sp_requested_voltage", 10.0, origin="test")
    assert r["status"] == C.OK, r
    assert _plc_value("ion_beam.source.extraction.sp_requested_voltage") == pytest.approx(10.0)


def test_out_of_range_rejected_with_limits(plc_service):
    r = _client().request("plc", "ion_beam.source.extraction.sp_requested_voltage", 25.0)
    assert r["status"] == C.REJECTED
    assert "outside the allowed range" in r["reason"]
    assert _plc_value("ion_beam.source.extraction.sp_requested_voltage") != pytest.approx(25.0)


def test_unknown_and_status_tags_rejected(plc_service):
    c = _client()
    assert c.request("plc", "ion_beam.nonsense.cmd_x", 1)["status"] == C.REJECTED
    # A PC-computed permissive must not be settable by a command (it would bypass the vacuum interlock)
    r = c.request("plc", "ion_beam.source.chamber.stat_vac_ok_for_gv", True)
    assert r["status"] == C.REJECTED and "not a command" in r["reason"]
    # Read-only PLC output
    r = c.request("plc", "ion_beam.facilities.safety_relay_active", True)
    assert r["status"] == C.REJECTED


def test_automatic_mode_locks_out_hmi_but_not_optimiser(plc_service):
    c = _client()
    assert c.request("plc", "ion_beam.source.extraction.cmd_ctrl_mode", 1)["status"] == C.OK
    deadline = time.time() + 3
    while time.time() < deadline and (plc_service.state.get("ion_beam.source.extraction.ctrl_mode") or 0) < 1:
        time.sleep(0.05)
    r = c.request("plc", "ion_beam.source.extraction.sp_requested_voltage", 5.0, origin="hmi")
    assert r["status"] == C.REJECTED and "locked out" in r["reason"]
    r = c.request("plc", "ion_beam.source.extraction.sp_requested_voltage", 5.0, origin="optimizer")
    assert r["status"] == C.OK
    c.request("plc", "ion_beam.source.extraction.cmd_ctrl_mode", 0)


def test_batched_writes_skip_read_only_tags(plc_service):
    before = _plc_value("ion_beam.facilities.safety_relay_active")
    plc_service._write_tags_batched({"ion_beam.facilities.safety_relay_active": not bool(before),
                                     "ion_beam.facilities.graphix1.stat_comms_fail": True})
    assert _plc_value("ion_beam.facilities.safety_relay_active") == before
    assert _plc_value("ion_beam.facilities.graphix1.stat_comms_fail") == 1.0


def test_disconnect_keeps_heartbeat_and_publishes_unknown(plc_service, monkeypatch):
    ctx = zmq.Context.instance()
    sub = ctx.socket(zmq.SUB)
    sub.setsockopt(zmq.SUBSCRIBE, b"")
    sub.connect(ZMQ_PORT_PLC_PUB)
    hb = ctx.socket(zmq.SUB)
    hb.setsockopt(zmq.SUBSCRIBE, b"")
    hb.bind(ZMQ_PORT_HEARTBEAT)
    try:
        time.sleep(0.3)
        monkeypatch.setenv("IPIDS_SIM_FAULTS", "plc_disconnect")
        deadline = time.time() + 3
        while plc_service.connected and time.time() < deadline:
            time.sleep(0.05)
        assert not plc_service.connected
        time.sleep(0.3)
        # still publishing, now with connected=0 and unknown values
        last = None
        t_end = time.time() + 1.5
        while time.time() < t_end:
            if sub.poll(100):
                last = orjson.loads(sub.recv_multipart()[1])
        assert last is not None
        assert last["system.connected"] == 0.0
        assert last.get("ion_beam.facilities.safety_relay_active", None) is None
        # and still heartbeating, so the service manager does not restart it in a loop
        beats = 0
        t_end = time.time() + 1.6
        while time.time() < t_end:
            if hb.poll(100):
                hb.recv()
                beats += 1
        assert beats >= 2
        r = _client().request("plc", "ion_beam.source.extraction.cmd_enable", True)
        assert r["status"] == C.REJECTED and "not connected" in r["reason"]
        # recovery
        monkeypatch.delenv("IPIDS_SIM_FAULTS")
        deadline = time.time() + 5
        while not plc_service.connected and time.time() < deadline:
            time.sleep(0.05)
        assert plc_service.connected
    finally:
        sub.close(0)
        hb.close(0)


def test_simulated_supply_follows_setpoint(plc_service, monkeypatch):
    from src.sim.plc import SimulatedPLC
    # no vacuum service here to permit the gate valve, and the relay needs it open
    monkeypatch.setattr(SimulatedPLC, "_gate_valve",
                        lambda self, now: self.set("ion_beam.source.chamber.stat_gv_open", 1))
    c = _client()
    assert c.request("plc", "ion_beam.system.cmd_enable_safety", True)["status"] == C.OK
    assert c.request("plc", "ion_beam.source.target.sp_requested_voltage", 4.0)["status"] == C.OK
    assert c.request("plc", "ion_beam.source.target.cmd_enable", True)["status"] == C.OK
    deadline = time.time() + 6
    while time.time() < deadline:
        rb = plc_service.state.get("ion_beam.source.target.rb_voltage") or 0.0
        if abs(rb - 4.0) < 0.1:
            break
        time.sleep(0.1)
    assert plc_service.state.get("ion_beam.facilities.safety_relay_active") == 1.0
    assert plc_service.state.get("ion_beam.source.target.stat_enabled") == 1.0
    assert plc_service.state.get("ion_beam.source.target.rb_voltage") == pytest.approx(4.0, abs=0.1)
