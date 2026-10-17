import threading
import time

import orjson
import pytest
import zmq

from src.core import commands as C
from tests.conftest import free_tcp_endpoint


def _serve(server, handler, stop, period=0.01):
    while not stop.is_set():
        for cmd in server.poll():
            handler(cmd)
        time.sleep(period)
    server.close()


@pytest.fixture
def running_server(endpoint, fake_events):
    """A CommandServer in a thread; the test supplies the handler."""
    started = []

    def start(handler, max_age=C.DEFAULT_MAX_AGE, period=0.01):
        server = C.CommandServer(endpoint, "service_test", context=zmq.Context.instance(),
                                 events=fake_events, max_age=max_age)
        stop = threading.Event()
        t = threading.Thread(target=_serve, args=(server, handler, stop, period), daemon=True)
        t.start()
        started.append((stop, t))
        return C.CommandClient({"test": endpoint}, default_timeout=1.0)

    yield start
    for stop, t in started:
        stop.set()
        t.join(2)


def test_ok_reply_round_trip(running_server, fake_events):
    client = running_server(lambda cmd: cmd.ok())
    reply = client.request("test", "ion_beam.x.cmd_enable", True, origin="hmi")
    assert reply["status"] == C.OK
    assert reply["tag"] == "ion_beam.x.cmd_enable"
    assert reply["subsystem"] == "test"
    assert reply["latency_s"] < 1.0
    # audit row written with the distinct COMMAND type
    assert fake_events.commands and fake_events.commands[-1]["ok"]
    assert fake_events.commands[-1]["metadata"]["origin"] == "hmi"


def test_rejection_reason_reaches_client(running_server, fake_events):
    client = running_server(lambda cmd: cmd.reject("above max 22.0 kV"))
    reply = client.request("test", "ion_beam.source.einzel.sp_requested_voltage", 30.0)
    assert reply["status"] == C.REJECTED
    assert "above max" in reply["reason"]
    assert not fake_events.commands[-1]["ok"]
    assert "above max" in C.describe(reply)


def test_unanswered_command_is_accepted_not_lost(running_server):
    client = running_server(lambda cmd: None)  # handler forgets to reply
    reply = client.request("test", "a.b.cmd_x", 1)
    assert reply["status"] == C.ACCEPTED


def test_stale_command_rejected_before_handler(running_server):
    seen = []
    client = running_server(lambda cmd: (seen.append(cmd), cmd.ok()), max_age=0.5)
    cmd_id = client.send("test", "a.b.cmd_x", 1, ts=time.time() - 5.0)
    deadline = time.time() + 2
    replies = []
    while time.time() < deadline and not replies:
        replies = client.poll(50)
    assert replies[0]["id"] == cmd_id
    assert replies[0]["status"] == C.REJECTED
    assert "stale" in replies[0]["reason"]
    assert seen == []


def test_unreachable_when_service_not_running():
    client = C.CommandClient({"ghost": free_tcp_endpoint()}, default_timeout=0.5, connect_wait=0.1)
    t0 = time.time()
    reply = client.request("ghost", "a.b.cmd_x", 1)
    assert reply["status"] == C.UNREACHABLE
    assert time.time() - t0 < 0.5


def test_unknown_subsystem_is_unreachable():
    client = C.CommandClient({}, default_timeout=0.5)
    reply = client.request("nowhere", "a.b.c", 1)
    assert reply["status"] == C.UNREACHABLE
    assert "nowhere" in reply["reason"]


def test_timeout_when_service_hangs(endpoint):
    # A bound ROUTER that never reads: the connection exists but nobody answers
    ctx = zmq.Context.instance()
    router = ctx.socket(zmq.ROUTER)
    router.bind(endpoint)
    try:
        client = C.CommandClient({"slow": endpoint}, default_timeout=0.3)
        reply = client.request("slow", "a.b.cmd_x", 1, timeout=0.3)
        assert reply["status"] == C.TIMEOUT
    finally:
        router.close(0)


def test_many_commands_each_get_one_reply(running_server):
    client = running_server(lambda cmd: cmd.ok() if cmd.value % 2 else cmd.reject("odd only"))
    ids = {client.send("test", "a.b.sp_x", i) for i in range(200)}
    got = {}
    deadline = time.time() + 5
    while len(got) < 200 and time.time() < deadline:
        for r in client.poll(50):
            assert r["id"] not in got
            got[r["id"]] = r
    assert set(got) == ids
    assert sum(r["status"] == C.OK for r in got.values()) == 100


def test_req_socket_clients_are_supported(running_server, endpoint):
    """Simple scripts may use a REQ socket; the empty delimiter frame must be echoed."""
    running_server(lambda cmd: cmd.ok("done"))
    s = zmq.Context.instance().socket(zmq.REQ)
    s.setsockopt(zmq.LINGER, 0)
    s.connect(endpoint)
    s.send(orjson.dumps({"id": "r1", "tag": "a.b.cmd_x", "value": 1, "ts": time.time(), "origin": "script"}))
    assert s.poll(2000)
    reply = orjson.loads(s.recv())
    assert reply["id"] == "r1" and reply["status"] == C.OK
    s.close(0)


def test_malformed_request_is_answered(running_server, endpoint):
    running_server(lambda cmd: cmd.ok())
    s = zmq.Context.instance().socket(zmq.DEALER)
    s.setsockopt(zmq.LINGER, 0)
    s.connect(endpoint)
    s.send(b"not json at all")
    assert s.poll(2000)
    reply = orjson.loads(s.recv())
    assert reply["status"] == C.REJECTED and "malformed" in reply["reason"]
    s.close(0)


def test_ticket_resolves_and_reports():
    t = C.CommandTicket("plc", "a.b.cmd_x", 1, "hmi")
    assert not t.done() and t.status is None and not t.ok
    threading.Timer(0.05, t.resolve, args=({"status": C.REJECTED, "reason": "locked"},)).start()
    assert t.wait(1)["status"] == C.REJECTED
    assert t.done() and not t.ok and t.reason == "locked"


@pytest.mark.parametrize("tag,expected", [
    ("ion_beam.source.einzel.sp_requested_voltage", "spellman"),
    ("ion_beam.beamline.neutral_trap_pos.cmd_enable", "spellman"),
    ("ion_beam.beamline.steerer_magnet.sp_requested_current", "steerer_magnet"),
    ("ion_beam.beamline.magnet.cmd_degauss", "magnet"),
    ("ion_beam.beamline.faraday.smu.cmd_nplc", "smu"),
    ("ion_beam.source.turbo_pump.cmd_enable", "src_turbo"),
    ("ion_beam.source.extraction.sp_requested_voltage", "plc"),
    ("ion_beam.source.chamber.cmd_open_gv", "plc"),
    ("ion_beam.vacuum.controller_10.relay_1_on", "vacuum"),
])
def test_route_for_tag(tag, expected):
    assert C.route_for_tag(tag) == expected
