"""The alarm service over real sockets: a PLC fault becomes an unacknowledged alarm that sounds,
the operator acknowledges it, it clears; shelving needs a reason; acknowledgements survive a
restart."""
import json
import threading
import time

import orjson
import pytest
import zmq

from src.core import commands as C
from src.services.service_alarms import AlarmService
from tests.conftest import free_tcp_endpoint
from tests.test_alarms import FAULTS


class Feed:
    """Publishes PLC_DATA as the PLC service would."""

    def __init__(self):
        self.endpoint = free_tcp_endpoint()
        self.sock = zmq.Context.instance().socket(zmq.PUB)
        self.sock.setsockopt(zmq.LINGER, 0)
        self.sock.bind(self.endpoint)
        self.values = {"faults.Safety_Relay_Tripped": 0.0, "faults.Src_Coolant_Flow_Low": 0.0,
                       "system.connected": 1.0}
        self.running = True
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        while self.running:
            self.sock.send_multipart([b"PLC_DATA", orjson.dumps(dict(self.values, timestamp=time.time()))])
            time.sleep(0.1)

    def close(self):
        self.running = False
        self.t.join(2)
        self.sock.close(0)


@pytest.fixture
def rig(tmp_path, fake_events):
    feed = Feed()
    pub, cmd = free_tcp_endpoint(), free_tcp_endpoint()

    def make():
        svc = AlarmService(pub_endpoint=pub, cmd_endpoint=cmd, telemetry_endpoints=[feed.endpoint],
                           events=fake_events, fault_config=FAULTS, state_file=str(tmp_path / "alarm_state.json"))
        thread = threading.Thread(target=svc.run, daemon=True)
        thread.start()
        return svc, thread

    sub = zmq.Context.instance().socket(zmq.SUB)
    sub.setsockopt(zmq.SUBSCRIBE, b"")
    sub.connect(pub)
    client = C.CommandClient({"alarms": cmd}, default_timeout=3.0)
    state = {"svc": make()}

    def latest(predicate, timeout=8.0):
        end = time.time() + timeout
        last = None
        while time.time() < end:
            if sub.poll(100):
                last = orjson.loads(sub.recv_multipart()[1])
                if predicate(last):
                    return last
        raise AssertionError(f"not reached; last: {last}")

    def restart():
        svc, thread = state["svc"]
        svc.stop()
        thread.join(5)
        state["svc"] = make()

    yield feed, client, latest, fake_events, restart
    svc, thread = state["svc"]
    svc.stop()
    thread.join(5)
    client.close()
    sub.close(0)
    feed.close()


def by_id(msg):
    return {a["id"]: a for a in msg["alarms.list"]}


def test_fault_to_alarm_ack_and_clear(rig):
    feed, client, latest, events, _ = rig
    feed.values["faults.Safety_Relay_Tripped"] = 1.0
    msg = latest(lambda m: "plc.Safety_Relay_Tripped" in by_id(m))
    alarm = by_id(msg)["plc.Safety_Relay_Tripped"]
    assert alarm["state"] == "UNACK" and alarm["priority"] == 1 and alarm["first_out"]
    assert msg["alarms.horn"] == 1 and msg["alarms.counts"]["unack"] == 1
    r = client.request("alarms", "alarms.cmd_ack", {"all": True}, origin="hmi")
    assert r["status"] == C.OK and r["data"] == ["plc.Safety_Relay_Tripped"]
    msg = latest(lambda m: by_id(m).get("plc.Safety_Relay_Tripped", {}).get("state") == "ACKED")
    assert msg["alarms.horn"] is None and by_id(msg)["plc.Safety_Relay_Tripped"]["acked_by"] == "hmi"
    feed.values["faults.Safety_Relay_Tripped"] = 0.0
    latest(lambda m: "plc.Safety_Relay_Tripped" not in by_id(m))
    kinds = [e["metadata"]["transition"] for e in events.of_type("ALARM")]
    assert kinds == ["ACTIVE", "ACKED", "CLEARED"]


def test_shelving_needs_a_reason_and_an_alarm(rig):
    feed, client, latest, _, _ = rig
    feed.values["faults.Src_Coolant_Flow_Low"] = 1.0
    latest(lambda m: "plc.Src_Coolant_Flow_Low" in by_id(m))
    r = client.request("alarms", "alarms.cmd_shelve", {"id": "plc.Src_Coolant_Flow_Low", "minutes": 30})
    assert r["status"] == C.REJECTED and "reason" in r["reason"]
    r = client.request("alarms", "alarms.cmd_shelve", {"id": "plc.Src_Coolant_Flow_Low", "minutes": 9999,
                                                       "reason": "flow meter replaced"})
    assert r["status"] == C.REJECTED and "minutes" in r["reason"]
    r = client.request("alarms", "alarms.cmd_shelve", {"id": "nope", "minutes": 5, "reason": "x"})
    assert r["status"] == C.REJECTED
    r = client.request("alarms", "alarms.cmd_shelve", {"id": "plc.Src_Coolant_Flow_Low", "minutes": 30,
                                                       "reason": "flow meter replaced"})
    assert r["status"] == C.OK
    msg = latest(lambda m: by_id(m).get("plc.Src_Coolant_Flow_Low", {}).get("shelved_until"))
    assert msg["alarms.horn"] is None and msg["alarms.counts"]["shelved"] == 1
    assert client.request("alarms", "alarms.cmd_unshelve", {"id": "plc.Src_Coolant_Flow_Low"})["status"] == C.OK
    latest(lambda m: m["alarms.horn"] == 3)
    assert client.request("alarms", "alarms.cmd_reset_first_out", None)["status"] == C.OK
    assert client.request("alarms", "alarms.cmd_ack", {})["status"] == C.REJECTED


def test_acknowledged_alarms_stay_acknowledged_after_a_restart(rig):
    feed, client, latest, _, restart = rig
    feed.values["faults.Safety_Relay_Tripped"] = 1.0
    latest(lambda m: "plc.Safety_Relay_Tripped" in by_id(m))
    client.request("alarms", "alarms.cmd_ack", {"ids": ["plc.Safety_Relay_Tripped"]})
    latest(lambda m: by_id(m).get("plc.Safety_Relay_Tripped", {}).get("state") == "ACKED")
    time.sleep(2.5)                     # saved at most every 2 s
    restart()
    msg = latest(lambda m: "plc.Safety_Relay_Tripped" in by_id(m), timeout=10)
    assert by_id(msg)["plc.Safety_Relay_Tripped"]["state"] == "ACKED" and msg["alarms.horn"] is None
