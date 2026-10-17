"""Spellman service against simulated supplies - including the 'enable state stays
latched after the safety relay trips' bug reported on the real machine."""
import threading
import time

import orjson
import pytest
import zmq

from src.core import commands as C
from src.core.network_map import ZMQ_PORT_HEARTBEAT, ZMQ_PORT_PLC_PUB, ZMQ_PORT_SPELLMAN_PUB, TOPIC_PLC_DATA

EINZEL = "ion_beam.source.einzel"


class FakePlcPublisher:
    """Publishes just the safety-relay state, as the PLC service would."""

    def __init__(self):
        self.relay = True
        self.running = True
        self.sock = zmq.Context.instance().socket(zmq.PUB)
        self.sock.bind(ZMQ_PORT_PLC_PUB)
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        while self.running:
            self.sock.send_multipart([TOPIC_PLC_DATA, orjson.dumps(
                {"ion_beam.facilities.safety_relay_active": 1.0 if self.relay else 0.0, "timestamp": time.time()})])
            time.sleep(0.1)
        self.sock.close(0)

    def stop(self):
        self.running = False
        self.t.join(2)


class TelemetryTap:
    def __init__(self, endpoint):
        self.sock = zmq.Context.instance().socket(zmq.SUB)
        self.sock.setsockopt(zmq.SUBSCRIBE, b"")
        self.sock.connect(endpoint)
        self.last = {}

    def wait_for(self, predicate, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.sock.poll(100):
                self.last = orjson.loads(self.sock.recv_multipart()[1])
                if predicate(self.last):
                    return self.last
        raise AssertionError(f"condition not met; last telemetry: { {k: v for k, v in self.last.items() if k.startswith(EINZEL)} }")

    def close(self):
        self.sock.close(0)


@pytest.fixture
def stack(monkeypatch):
    monkeypatch.setenv("IPIDS_SIMULATE", "1")
    monkeypatch.delenv("IPIDS_SIM_FAULTS", raising=False)
    from src.sim import spellman as simsp
    simsp._BUSES.clear()
    plc = FakePlcPublisher()
    from src.services.service_spellman_mpd import SpellmanMicroservice
    svc = SpellmanMicroservice()
    t = threading.Thread(target=svc.run, daemon=True)
    t.start()
    tap = TelemetryTap(ZMQ_PORT_SPELLMAN_PUB)
    yield plc, svc, tap
    svc.stop()
    t.join(5)
    plc.stop()
    tap.close()


def test_enable_and_setpoint_acknowledged(stack):
    plc, svc, tap = stack
    tap.wait_for(lambda d: d.get(f"{EINZEL}.stat_enabled") == 0.0)
    c = C.CommandClient(default_timeout=3.0)
    assert c.request("spellman", f"{EINZEL}.sp_requested_voltage", 10.0)["status"] == C.OK
    # No current limits are configured in the registry, so current-limit commands fail safe
    # (before acknowledgements existed, the HMI's automatic 50 uA write was silently dropped)
    r = c.request("spellman", f"{EINZEL}.sp_requested_current", 50.0)
    assert r["status"] == C.REJECTED and "no min/max limits" in r["reason"]
    assert c.request("spellman", f"{EINZEL}.cmd_enable", True)["status"] == C.OK
    d = tap.wait_for(lambda d: d.get(f"{EINZEL}.stat_enabled") == 1.0
                     and abs((d.get(f"{EINZEL}.rb_voltage") or 0) - 10.0) < 0.1, timeout=6)
    assert d[f"{EINZEL}.sp_actual_voltage"] == pytest.approx(10.0)
    r = c.request("spellman", f"{EINZEL}.sp_requested_voltage", 99.0)
    assert r["status"] == C.REJECTED and "outside the allowed range" in r["reason"]


def test_relay_trip_reports_disabled_and_unknown_readbacks(stack):
    plc, svc, tap = stack
    c = C.CommandClient(default_timeout=3.0)
    tap.wait_for(lambda d: d.get(f"{EINZEL}.stat_enabled") == 0.0)
    c.request("spellman", f"{EINZEL}.sp_requested_voltage", 8.0)
    c.request("spellman", f"{EINZEL}.cmd_enable", True)
    tap.wait_for(lambda d: d.get(f"{EINZEL}.stat_enabled") == 1.0)

    plc.relay = False   # safety relay trips
    d = tap.wait_for(lambda d: d.get(f"{EINZEL}.stat_enabled") == 0.0, timeout=3)
    assert d[f"{EINZEL}.rb_voltage"] is None          # unknown, not the last value
    assert d[f"{EINZEL}.stat_comms_fail"] == 0.0      # expected, not a comms fault
    r = c.request("spellman", f"{EINZEL}.cmd_enable", True)
    assert r["status"] == C.REJECTED and "relay" in r["reason"]
    assert c.request("spellman", f"{EINZEL}.cmd_enable", False)["status"] == C.OK

    plc.relay = True    # power restored: the supply comes back disabled, as the real units do
    d = tap.wait_for(lambda d: d.get(f"{EINZEL}.rb_voltage") is not None, timeout=5)
    assert d[f"{EINZEL}.stat_enabled"] == 0.0


def test_unresponsive_supply_is_unknown_and_service_keeps_heartbeating(stack, monkeypatch):
    plc, svc, tap = stack
    tap.wait_for(lambda d: d.get(f"{EINZEL}.stat_enabled") == 0.0)
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
