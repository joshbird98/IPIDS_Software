import threading
import time

import orjson
import pytest
import zmq

from src.core import commands as C
from src.core.network_map import ZMQ_PORT_HEARTBEAT, ZMQ_PORT_PLC_PUB


@pytest.fixture
def plc_service(monkeypatch):
    monkeypatch.setenv("SINDIE_SIMULATE", "1")
    monkeypatch.delenv("SINDIE_SIM_FAULTS", raising=False)
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


def test_plc_control_mode_locks_out_everyone(plc_service):
    """ctrl_mode > 0 means the PLC is controlling it: refused whoever sends it (the old
    origin='optimizer' bypass is gone - sequences use control leases instead)."""
    c = _client()
    assert c.request("plc", "ion_beam.source.extraction.cmd_ctrl_mode", 1)["status"] == C.OK
    deadline = time.time() + 3
    while time.time() < deadline and (plc_service.state.get("ion_beam.source.extraction.ctrl_mode") or 0) < 1:
        time.sleep(0.05)
    for origin in ("hmi", "optimizer"):
        r = c.request("plc", "ion_beam.source.extraction.sp_requested_voltage", 5.0, origin=origin)
        assert r["status"] == C.REJECTED and "locked out" in r["reason"]
    assert plc_service.state["ion_beam.source.extraction.permit"] == 0.0
    assert plc_service.state["ion_beam.source.extraction.permit_reason"]   # (the relay is off here too)
    c.request("plc", "ion_beam.source.extraction.cmd_ctrl_mode", 0)


def test_control_lease_admits_only_its_holder(plc_service):
    c = _client()
    sub = "ion_beam.source.extraction"
    lease = {"op": "claim", "holder": "seq-1", "purpose": "recipe 'Test'", "ttl": 5}
    assert c.request("plc", f"{sub}.cmd_lease", lease)["status"] == C.OK
    r = c.request("plc", f"{sub}.sp_requested_voltage", 5.0)
    assert r["status"] == C.REJECTED and "recipe 'Test'" in r["reason"]
    assert c.request("plc", f"{sub}.sp_requested_voltage", 5.0, holder="seq-1")["status"] == C.OK
    assert _wait_state(plc_service, f"{sub}.lease_holder", "seq-1") == "seq-1"
    assert plc_service.state[f"{sub}.permit"] == 0.0
    # A second sequence can't take it; the operator can, and the holder's renewal then fails
    assert c.request("plc", f"{sub}.cmd_lease", dict(lease, holder="seq-2"))["status"] == C.REJECTED
    assert c.request("plc", f"{sub}.cmd_lease", {"op": "take"}, origin="hmi")["status"] == C.OK
    r = c.request("plc", f"{sub}.cmd_lease", dict(lease, op="renew"))
    assert r["status"] == C.REJECTED and "taken by the operator" in r["reason"]
    assert c.request("plc", f"{sub}.sp_requested_voltage", 6.0)["status"] == C.OK
    # The relay, shutdown, fault reset and gate valve can never be leased
    for unleasable in ("ion_beam.system", "ion_beam.source.chamber"):
        assert c.request("plc", f"{unleasable}.cmd_lease", lease)["status"] == C.REJECTED


def test_enable_refused_without_the_relay(plc_service):
    r = _client().request("plc", "ion_beam.source.target.cmd_enable", True)
    assert r["status"] == C.REJECTED and "relay" in r["reason"]
    assert _client().request("plc", "ion_beam.source.target.sp_requested_voltage", 4.0)["status"] == C.OK


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
        monkeypatch.setenv("SINDIE_SIM_FAULTS", "plc_disconnect")
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
        monkeypatch.delenv("SINDIE_SIM_FAULTS")
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
    assert _wait_state(plc_service, "ion_beam.facilities.safety_relay_active", 1.0) == 1.0
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


def test_mode_lockout_rules(plc_service):
    """A subsystem in an automatic mode refuses setpoints and switching off alike (the PLC holds
    e.g. the target on while the cesium is hot); the cesium oven's mode 1 (heater on) is not a
    lockout - its setpoint stays adjustable."""
    from src.sim.plc import shared_plc
    c = _client()
    plc = shared_plc()
    with plc.lock:
        plc.set("ion_beam.source.extraction.cmd_ctrl_mode", 2)          # e.g. held by a recipe
        plc.set("ion_beam.source.cesium.cmd_ctrl_mode", 1)              # heater on
    deadline = time.time() + 3
    while time.time() < deadline and plc_service.state.get("ion_beam.source.extraction.ctrl_mode") != 2.0:
        time.sleep(0.05)
    r = c.request("plc", "ion_beam.source.extraction.sp_requested_voltage", 5.0)
    assert r["status"] == C.REJECTED and "automatic control mode 2" in r["reason"]
    assert c.request("plc", "ion_beam.source.extraction.cmd_enable", False)["status"] == C.REJECTED
    assert c.request("plc", "ion_beam.source.extraction.cmd_enable", True)["status"] == C.REJECTED
    assert c.request("plc", "ion_beam.source.cesium.sp_requested_temp", 90.0)["status"] == C.OK
    assert c.request("plc", "ion_beam.source.cesium.cmd_force_cooling", True)["status"] == C.OK


def _wait_state(plc_service, tag, value, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline and plc_service.state.get(tag) != value:
        time.sleep(0.05)
    return plc_service.state.get(tag)


def test_source_deck_readbacks_unknown_while_relay_off(plc_service):
    """The deck (cesium oven, filament, ioniser) is powered through the relay: without it the PLC
    has no readback from it, so those are published as unknown; the estimated temperature stays."""
    assert _wait_state(plc_service, "ion_beam.facilities.safety_relay_active", 0.0) == 0.0
    time.sleep(0.3)
    for tag in ("ion_beam.source.cesium.rb_temp", "ion_beam.source.thermionic.rb_current",
                "ion_beam.source.filament.rb_voltage"):
        assert plc_service.state.get(tag, "missing") is None, tag
    assert plc_service.state.get("ion_beam.source.cesium.estimated_temp") is not None
    assert plc_service.state.get("ion_beam.source.extraction.rb_voltage") is not None   # not on the deck


def test_hot_cesium_refusals_carry_the_reason(plc_service):
    from src.sim.plc import shared_plc
    plc = shared_plc()
    with plc.lock:
        plc.set("ion_beam.source.cesium.estimated_temp", 90.0)
    assert _wait_state(plc_service, "ion_beam.internal.cs_is_hot", 1.0) == 1.0
    c = _client()
    r = c.request("plc", "ion_beam.source.target.cmd_enable", False)
    assert r["status"] == C.REJECTED and "cesium is hot" in r["reason"] and "ceramics" in r["reason"]
    r = c.request("plc", "ion_beam.source.target.sp_requested_voltage", 0.1)
    assert r["status"] == C.REJECTED and "at or above" in r["reason"]
    assert c.request("plc", "ion_beam.source.target.sp_requested_voltage", 6.0)["status"] == C.OK
    assert c.request("plc", "ion_beam.source.extraction.cmd_enable", False)["status"] == C.OK   # not held


def test_undecodable_tag_does_not_drop_the_connection(monkeypatch, caplog):
    """A registry tag whose bytes can't be decoded shows '---' and is reported once; it used to
    drop the whole PLC link and reconnect in a loop, which looked like a comms fault."""
    monkeypatch.setenv("SINDIE_SIMULATE", "1")
    from src.services import service_plc as sp
    from src.sim import plc as simplc
    simplc._PLC = None
    svc = sp.PlcMicroservice()
    try:
        svc.client = sp._new_client()
        svc.client.connect("sim", 0, 1)
        svc.connected = True
        svc.tags["ion_beam.test.bogus"] = {"db_number": sp.DB_INTERFACE_NUM, "datatype": "REAL",
                                           "byte_offset": svc.db_interface_size + 100}
        word = next(iter(svc.fault_map))                       # a fault word the PLC can't be read for
        svc.tags[word] = dict(svc.tags[word], byte_offset=svc.db_interface_size + 200)
        fault_names = [m["name"] for m in svc.fault_map[word].values()]
        svc._read_and_parse_dbs()
        assert svc.connected and svc.state["ion_beam.test.bogus"] is None
        assert svc.state[word] is None and all(svc.state[f"faults.{n}"] is None for n in fault_names)
        assert svc.state.get("ion_beam.system.plc_cycle_count") is not None      # the rest decoded
        assert "Cannot decode ion_beam.test.bogus" in caplog.text
        caplog.clear()
        svc._read_and_parse_dbs()
        assert "Cannot decode" not in caplog.text                                 # reported once

        def unreachable(*a):
            raise RuntimeError("TCP : Unreachable peer")
        svc.client.db_read = unreachable
        svc._read_and_parse_dbs()                                                 # the link itself failing
        assert not svc.connected and svc.state["system.connected"] == 0.0
    finally:
        svc.shutdown()


def test_bad_fault_map_entries_are_left_out(monkeypatch, tmp_path):
    monkeypatch.setenv("SINDIE_SIMULATE", "1")
    from src.services import service_plc as sp
    svc = sp.PlcMicroservice()
    try:
        assert all(0 <= int(b.split(".")[0]) <= 3 for bits in svc.fault_map.values() for b in bits)
        import json
        bad = {"W": {"registry_tag": "ion_beam.faults.w", "bits": {"0.1": {"name": "Good"}, "4.0": {"name": "Bad"},
                                                                    "x": {"name": "Worse"}, "1.1": {}}}}
        cfg = tmp_path / "fault_config.json"
        cfg.write_text(json.dumps(bad))
        assert svc._load_fault_map(str(cfg)) == {"ion_beam.faults.w": {"0.1": {"name": "Good"}}}
        # fault edges come from the checked map: the bad bits can't crash the poll loop any more
        svc.fault_map = svc._load_fault_map(str(cfg))
        edges = []
        svc.events.log_fault = lambda name, active, metadata=None: edges.append((name, active))
        for word in (0.0, float(1 << 25), float(1 << 25), 0.0):    # byte 0 bit 1 = bit 25 of the DWORD
            svc.state["ion_beam.faults.w"] = word
            svc._evaluate_word_faults()
        assert edges == [("Good", True), ("Good", False)]
    finally:
        svc.shutdown()


def test_a_gate_valve_shut_by_its_interlock_stays_shut(plc_service):
    """OPEN stays set in the PLC; when the vacuum permissive went the valve closed, and opened again by
    itself 2 s after it came back. Shut by the interlock for 3 s, the request is now cleared (an event
    says so): the valve stays shut until OPEN is pressed again - which then works."""
    from src.sim.plc import shared_plc
    plc, base = shared_plc(), "ion_beam.source.chamber"
    plc_service.SERVICE_TIMEOUT_SEC = 1e9           # no vacuum service here: the test sets the permissive
    plc.set(f"{base}.stat_vac_ok_for_gv", 1)
    c = _client()

    def wait(cond, timeout, what):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return
            time.sleep(0.05)
        raise AssertionError(what)
    assert c.request("plc", f"{base}.cmd_open_gv", True)["status"] == C.OK
    wait(lambda: plc.get(f"{base}.stat_gv_open") == 1.0, 5, "valve open")
    time.sleep(3.5)
    assert plc.get(f"{base}.cmd_open_gv") == 1.0                       # open and permitted: left alone
    plc.set(f"{base}.stat_vac_ok_for_gv", 0)                           # vacuum permissive gone
    wait(lambda: plc.get(f"{base}.stat_gv_closed") == 1.0, 3, "valve closed")
    time.sleep(1.5)
    assert plc.get(f"{base}.cmd_open_gv") == 1.0                       # not before the wait
    wait(lambda: plc.get(f"{base}.cmd_open_gv") == 0.0, 4, "OPEN request cleared")
    plc.set(f"{base}.stat_vac_ok_for_gv", 1)                           # permissive back
    time.sleep(3.0)
    assert plc.get(f"{base}.stat_gv_open") == 0.0                      # stays shut
    assert c.request("plc", f"{base}.cmd_open_gv", True)["status"] == C.OK
    wait(lambda: plc.get(f"{base}.stat_gv_open") == 1.0, 5, "valve opens on a new OPEN")
