"""DeviceService (src/core/device_service.py): the machinery every hardware service now shares.
A small fake device drives it cycle by cycle (run_cycle), plus a few real-thread checks."""
import threading
import time

import orjson
import pytest
import zmq

from src.core import commands as C
from src.core.device_service import CTRL_MODE_RETIRED, DeviceService, ReconnectBackoff
from src.core.network_map import ZMQ_PORT_HEARTBEAT
from tests.test_relay_watch import FakeSub

SUB_A, SUB_B = "test.unit.alpha", "test.unit.beta"


class FakeDevice:
    def __init__(self):
        self.reachable = True
        self.connected = False
        self.connects = 0
        self.value = 1.5
        self.writes = []
        self.raise_on_write = None

    def connect(self):
        self.connects += 1
        if not self.reachable:
            raise ConnectionError("cable out")
        self.connected = True
        return True


class Dummy(DeviceService):
    NAME = "service_unit_test"
    DEVICE_LABEL = "test device"
    PUB_ENDPOINT = "tcp://127.0.0.1:*"
    CMD_ENDPOINT = "tcp://127.0.0.1:*"
    TOPIC = b"UNIT_DATA"
    POLL_INTERVAL = 0.02
    RECONNECT_DELAYS = (0.2, 0.4)
    SUBSYSTEMS = (SUB_A, SUB_B)

    def __init__(self, watch_relay=False):
        self.WATCH_RELAY = watch_relay
        self.dev = FakeDevice()
        self.calls = []
        self.relay_changes = []
        self.permit_error = False
        super().__init__()

    def connect_device(self):
        return self.dev.connect()

    def is_connected(self):
        return self.dev.connected

    def after_connect(self, previous_failures):
        self.calls.append(("after_connect", previous_failures))

    def poll_device(self):
        self.calls.append("poll")
        self.state[f"{SUB_A}.rb_value"] = self.dev.value

    def publish_unknown(self):
        self.calls.append("unknown")
        self.state[f"{SUB_A}.rb_value"] = None

    def cycle(self):
        self.calls.append("cycle")

    def on_relay_change(self, ok, why):
        self.relay_changes.append((ok, why))

    def permit(self, subsystem):
        if self.permit_error and subsystem == SUB_B:
            raise RuntimeError("bug in permit")
        return super().permit(subsystem)

    def handle_command(self, cmd, subsystem, leaf):
        value = self.number(cmd)
        if value is None:
            return
        if leaf == "sp_requested_value":
            if self.within_limits(cmd, value, "V"):
                if self.dev.raise_on_write:
                    raise self.dev.raise_on_write
                self.dev.writes.append((subsystem, value))
                cmd.ok()
        else:
            cmd.reject(f"unsupported command '{leaf}'")


def endpoint(sock):
    return sock.getsockopt(zmq.LAST_ENDPOINT).decode()


@pytest.fixture
def svc():
    s = Dummy()
    s.registry_limits = {f"{SUB_A}.sp_requested_value": {"min_val": 0.0, "max_val": 10.0},
                         f"{SUB_B}.sp_requested_value": {"min_val": None, "max_val": 10.0}}
    yield s
    s.shutdown()


@pytest.fixture
def client(svc):
    c = C.CommandClient({"unit": endpoint(svc.commands.sock)}, default_timeout=2.0)
    yield c
    c.close()


def send(svc, client, tag, value, **kw):
    """Send one command and run the service until it has answered."""
    cmd_id = client.send("unit", tag, value, **kw)
    deadline = time.time() + 3
    while time.time() < deadline:
        svc.run_cycle()
        for reply in client.poll(20):
            if reply["id"] == cmd_id:
                return reply
    raise AssertionError("no reply")


# --------------------------------------------------------------------------- connection
def test_backoff_schedule():
    b = ReconnectBackoff((1.0, 5.0))
    assert b.due(0.0)
    assert b.failed(0.0) == 1 and not b.due(0.9) and b.due(1.0)
    b.failed(1.0)
    assert not b.due(5.9) and b.due(6.0)
    b.failed(6.0)
    assert b.due(11.0)            # stays at the last delay
    assert b.succeeded() == 3 and b.due(0.0)


def test_unreachable_device_publishes_unknown_and_backs_off(svc):
    svc.dev.reachable = False
    for _ in range(5):
        svc.run_cycle()
    assert svc.dev.connects == 1                      # next attempt only after 0.2 s
    assert "unknown" in svc.calls and "poll" not in svc.calls
    assert svc.state[f"{SUB_A}.rb_value"] is None
    assert svc.state[f"{SUB_A}.permit"] == 0.0 and "not connected" in svc.state[f"{SUB_A}.permit_reason"]
    time.sleep(0.25)
    svc.dev.reachable = True
    svc.run_cycle()
    assert svc.dev.connects == 2 and ("after_connect", 1) in svc.calls
    assert svc.calls[-1] == "poll" and svc.state[f"{SUB_A}.rb_value"] == 1.5
    assert svc.state[f"{SUB_A}.permit"] == 1.0 and svc.state[f"{SUB_A}.permit_reason"] == ""


def test_cycle_order(svc):
    svc.run_cycle()
    assert svc.calls == [("after_connect", 0), "cycle", "poll"]


def test_publishes_telemetry_and_heartbeats(svc):
    sub = zmq.Context.instance().socket(zmq.SUB)
    sub.setsockopt(zmq.SUBSCRIBE, b"")
    sub.connect(endpoint(svc.pub_socket))
    hb = zmq.Context.instance().socket(zmq.SUB)
    hb.setsockopt(zmq.SUBSCRIBE, b"")
    hb.bind(ZMQ_PORT_HEARTBEAT)
    try:
        time.sleep(0.2)
        got, beats = None, []
        for _ in range(40):
            svc.run_cycle()
            if sub.poll(10):
                topic, payload = sub.recv_multipart()
                got = (topic, orjson.loads(payload))
            if hb.poll(1):
                beats.append(hb.recv_json())
            time.sleep(0.02)
        assert got[0] == b"UNIT_DATA" and got[1][f"{SUB_A}.rb_value"] == 1.5
        assert got[1]["timestamp"] > 0 and "system.cycle_time_ms" in got[1]
        assert beats and beats[0]["service"] == "service_unit_test"
    finally:
        sub.close(0)
        hb.close(0)


# --------------------------------------------------------------------------- commands
def test_command_routing_and_checks(svc, client):
    svc.run_cycle()
    assert send(svc, client, f"{SUB_A}.sp_requested_value", 4.0)["status"] == C.OK
    assert svc.dev.writes == [(SUB_A, 4.0)]
    r = send(svc, client, "other.thing.sp_requested_value", 4.0)
    assert r["status"] == C.REJECTED and "not a test device tag" in r["reason"]
    r = send(svc, client, f"{SUB_A}.sp_requested_value", "four")
    assert r["status"] == C.REJECTED and "not a number" in r["reason"]
    r = send(svc, client, f"{SUB_A}.sp_requested_value", 12.0)
    assert r["status"] == C.REJECTED and "outside the allowed range [0, 10] V" in r["reason"]
    r = send(svc, client, f"{SUB_B}.sp_requested_value", 1.0)
    assert r["status"] == C.REJECTED and "no min/max" in r["reason"]
    r = send(svc, client, f"{SUB_A}.cmd_ctrl_mode", 1)
    assert r["status"] == C.REJECTED and r["reason"] == CTRL_MODE_RETIRED
    r = send(svc, client, "ion_beam.system.cmd_fault_reset", True)
    assert r["status"] == C.OK and "nothing to reset" in r["reason"]


def test_a_failing_handler_fails_its_command_and_the_service_carries_on(svc, client):
    svc.run_cycle()
    svc.dev.raise_on_write = OSError("bus fault")
    r = send(svc, client, f"{SUB_A}.sp_requested_value", 2.0)
    assert r["status"] == C.FAILED and "OSError: bus fault" in r["reason"]
    svc.dev.raise_on_write = None
    assert send(svc, client, f"{SUB_A}.sp_requested_value", 3.0)["status"] == C.OK


def test_leases_through_commands(svc, client):
    svc.run_cycle()
    lease = {"op": "claim", "holder": "seq-7", "purpose": "recipe 'Ramp'", "ttl": 5}
    assert send(svc, client, f"{SUB_A}.cmd_lease", lease)["status"] == C.OK
    r = send(svc, client, f"{SUB_A}.sp_requested_value", 1.0)
    assert r["status"] == C.REJECTED and "recipe 'Ramp'" in r["reason"]
    assert send(svc, client, f"{SUB_A}.sp_requested_value", 1.0, holder="seq-7")["status"] == C.OK
    assert send(svc, client, f"{SUB_B}.sp_requested_value", 1.0, holder="seq-7")["status"] == C.REJECTED  # limits
    svc.run_cycle()
    assert svc.state[f"{SUB_A}.lease_holder"] == "seq-7" and svc.state[f"{SUB_A}.permit"] == 0.0
    assert "recipe 'Ramp'" in svc.state[f"{SUB_A}.permit_reason"]
    assert svc.state[f"{SUB_B}.lease_holder"] is None
    r = send(svc, client, "nowhere.cmd_lease", lease)
    assert r["status"] == C.REJECTED
    assert send(svc, client, f"{SUB_A}.cmd_lease", {"op": "take"})["status"] == C.OK
    assert send(svc, client, f"{SUB_A}.sp_requested_value", 2.0)["status"] == C.OK


def test_permit_bug_shows_as_not_permitted(svc):
    svc.permit_error = True
    svc.run_cycle()
    assert svc.state[f"{SUB_B}.permit"] == 0.0 and "bug in permit" in svc.state[f"{SUB_B}.permit_reason"]
    assert svc.state[f"{SUB_A}.permit"] == 1.0


# --------------------------------------------------------------------------- relay
def test_relay_permissive_and_transitions():
    s = Dummy(watch_relay=True)
    try:
        fake = FakeSub()
        s.relay.sock = fake
        s.run_cycle()
        assert s.state["system.safe_to_run"] == 0.0 and s.relay_changes == []
        assert "relay" in s.state[f"{SUB_A}.permit_reason"]
        fake.push(1.0, time.time())
        s.run_cycle()
        assert s.relay_ok and s.relay_changes == [(True, "")]
        assert s.state[f"{SUB_A}.permit"] == 1.0
        fake.push(1.0, time.time())
        s.run_cycle()
        assert len(s.relay_changes) == 1                       # no repeat while unchanged
        fake.push(0.0, time.time())
        s.run_cycle()
        assert not s.relay_ok and s.relay_changes[-1] == (False, "safety relay de-energised (or its state unknown)")
        time.sleep(1.1)                                        # PLC goes quiet
        s.run_cycle()
        assert s.state["system.safe_to_run"] == 0.0
    finally:
        s.shutdown()


# --------------------------------------------------------------------------- loop
def test_pace_skips_missed_ticks_instead_of_bursting(svc):
    start = time.perf_counter()
    nxt = svc._pace(start - 0.5)          # 0.5 s late (25 ticks of 20 ms)
    assert time.perf_counter() - start < 0.05
    assert nxt - time.perf_counter() > 0.0 and nxt - time.perf_counter() <= 2 * svc.POLL_INTERVAL + 0.01


def test_run_and_stop_in_a_thread():
    s = Dummy()
    t = threading.Thread(target=s.run, daemon=True)
    t.start()
    time.sleep(0.3)
    s.stop()
    t.join(2)
    assert not t.is_alive() and s.calls.count("poll") >= 5
    assert s.pub_socket.closed and s.commands.sock.closed
    s.shutdown()                          # idempotent


def test_shutdown_survives_a_failing_close_device():
    s = Dummy()

    def boom():
        raise RuntimeError("driver exploded")
    s.close_device = boom
    s.shutdown()
    assert s.pub_socket.closed and s.hb_socket.closed


def test_load_config_falls_back_to_defaults(svc):
    cfg = svc.load_config("no_such_file.json", {"a": 1})
    assert cfg == {"a": 1}
