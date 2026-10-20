"""The sequencer service over real sockets, against a real DeviceService (the test Dummy):
start a recipe by command, see it hold the device's lease while it runs, refuse the operator
meanwhile, finish, release."""
import json
import time

import orjson
import pytest
import zmq

import src.services.service_sequencer as S
from src.core import commands as C
from src.sequencer import protocol as P
from tests.conftest import free_tcp_endpoint
from tests.service_harness import ServiceThread
from tests.test_device_service import SUB_A, Dummy, endpoint

TAG = f"{SUB_A}.sp_requested_value"


class Tap:
    def __init__(self, ep):
        self.sock = zmq.Context.instance().socket(zmq.SUB)
        self.sock.setsockopt(zmq.SUBSCRIBE, b"")
        self.sock.connect(ep)
        self.last = {}

    def wait(self, predicate, timeout=10.0, what="condition"):
        end = time.time() + timeout
        while time.time() < end:
            if self.sock.poll(50):
                self.last = orjson.loads(self.sock.recv_multipart()[1])
                if predicate(self.last):
                    return self.last
        raise AssertionError(f"timed out waiting for {what}; last: { {k: v for k, v in self.last.items() if 'recipe' in k} }")

    def close(self):
        self.sock.close(0)


@pytest.fixture
def rig(tmp_path, monkeypatch, fake_events):
    monkeypatch.setattr(S, "STARTUP_GRACE_S", 0.5)
    device = Dummy()
    device.registry_limits = {TAG: {"min_val": 0.0, "max_val": 10.0}}
    dev_thread = ServiceThread(device)
    (tmp_path / "unit.json").write_text(json.dumps({"recipe_name": "unit", "steps": [
        {"step_id": 1, "type": "ACTION", "target": TAG, "value": 4.0},
        {"step_id": 2, "type": "WAIT_TIME", "duration_sec": 1.5}]}))
    pub, cmd = free_tcp_endpoint(), free_tcp_endpoint()
    seq = S.SequencerService(pub_endpoint=pub, cmd_endpoint=cmd, telemetry_endpoints=[endpoint(device.pub_socket)],
                             command_endpoints={"plc": endpoint(device.commands.sock)}, events=fake_events,
                             recipes_dir=str(tmp_path), registry={})
    seq_thread = ServiceThread(seq)
    client = C.CommandClient({"sequencer": cmd, "unit": endpoint(device.commands.sock)}, default_timeout=3.0)
    tap = Tap(pub)
    yield seq, device, client, tap
    tap.close()
    client.close()
    seq_thread.stop()
    dev_thread.stop()


def wait(predicate, timeout=10.0, what="condition"):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def test_recipe_runs_in_the_service_under_a_lease(rig):
    seq, device, client, tap = rig
    early = client.request("sequencer", P.command(P.RECIPE, "start"), {"file": "unit.json"})
    assert early["status"] == C.REJECTED and "starting up" in early["reason"]
    wait(lambda: f"{SUB_A}.permit" in seq.telemetry.keys(), what="device telemetry in the sequencer")
    time.sleep(0.5)

    reply = client.request("sequencer", P.command(P.RECIPE, "start"), {"file": "unit.json"})
    assert reply["status"] == C.OK and reply["data"]["run"] > 0
    again = client.request("sequencer", P.command(P.RECIPE, "start"), {"file": "unit.json"})
    assert again["status"] == C.REJECTED and "already running" in again["reason"]

    wait(lambda: device.dev.writes == [(SUB_A, 4.0)], what="the recipe's setpoint")
    holder = device.state[f"{SUB_A}.lease_holder"]
    assert holder and holder.startswith("seq-recipe-")
    refused = client.request("unit", TAG, 5.0, origin="hmi")            # the operator, meanwhile
    assert refused["status"] == C.REJECTED and "recipe 'unit'" in refused["reason"]

    t = tap.wait(lambda m: m.get(P.key(P.RECIPE, "state")) == P.RUNNING, what="running in telemetry")
    assert t[P.key(P.RECIPE, "holder")] == holder and t[P.key(P.RECIPE, "leases")] == [SUB_A]
    tap.wait(lambda m: m.get(P.key(P.RECIPE, "state")) == P.COMPLETE, what="complete")
    wait(lambda: device.state[f"{SUB_A}.lease_holder"] is None, what="lease released")
    assert client.request("unit", TAG, 5.0, origin="hmi")["status"] == C.OK


def test_commands_the_service_refuses(rig):
    seq, device, client, tap = rig
    time.sleep(0.6)
    r = client.request("sequencer", "sequencer.nothing.cmd_start", 1)
    assert r["status"] == C.REJECTED and r["reason"] == "not a sequencer command"
    r = client.request("sequencer", P.command(P.RECIPE, "pause"), None)
    assert r["status"] == C.REJECTED and "no recipe has run" in r["reason"]
    r = client.request("sequencer", P.command(P.RECIPE, "start"), {"file": "../../etc/passwd"})
    assert r["status"] == C.REJECTED and "not in the recipes folder" in r["reason"]
    r = client.request("sequencer", P.command(P.MASS_SCAN, "start"), {"start_a": 1, "stop_a": 2, "step_a": 0.1})
    assert r["status"] == C.REJECTED and "limits" in r["reason"]           # no registry in this rig
    r = client.request("sequencer", P.command(P.OPTIMISER, "start"), {"x_tag": TAG})
    assert r["status"] == C.REJECTED
    idle = tap.wait(lambda m: P.key(P.MASS_SCAN, "state") in m, what="telemetry")
    assert idle[P.key(P.MASS_SCAN, "state")] == P.IDLE


def test_shutdown_stops_the_recipe_and_releases(rig):
    seq, device, client, tap = rig
    wait(lambda: f"{SUB_A}.permit" in seq.telemetry.keys(), what="device telemetry")
    time.sleep(0.5)
    assert client.request("sequencer", P.command(P.RECIPE, "start"), {"file": "unit.json"})["status"] == C.OK
    wait(lambda: device.state[f"{SUB_A}.lease_holder"] is not None, what="lease")
    seq.stop()
    wait(lambda: seq._closed, what="sequencer closed")
    assert seq.jobs[P.RECIPE].state == P.STOPPED and "shutting down" in seq.jobs[P.RECIPE].message
    wait(lambda: device.state[f"{SUB_A}.lease_holder"] is None, timeout=3, what="lease released at shutdown")


def test_a_job_that_raises_fails_alone_and_lets_go_of_its_leases(rig):
    """An exception outside a step (in the stop conditions, entering a sub-recipe ...) used to
    end the sequencer's loop, and with it every running job."""
    seq, device, client, tap = rig
    wait(lambda: f"{SUB_A}.permit" in seq.telemetry.keys(), what="device telemetry")
    time.sleep(0.5)
    assert client.request("sequencer", P.command(P.RECIPE, "start"), {"file": "unit.json"})["status"] == C.OK
    wait(lambda: device.state[f"{SUB_A}.lease_holder"] is not None, what="lease")
    job = seq.jobs[P.RECIPE]

    def broken():
        raise KeyError("bug")

    job.tick = broken
    wait(lambda: job.state == P.FAILED, what="the job failed")
    assert job.message == "internal error in the sequencer: KeyError: 'bug'"
    wait(lambda: device.state[f"{SUB_A}.lease_holder"] is None, timeout=3, what="lease released")
    r = client.request("sequencer", P.command(P.RECIPE, "start"), {"file": "unit.json"})
    assert r["status"] == C.OK                     # the sequencer carries on
    tap.wait(lambda m: m.get(P.key(P.RECIPE, "state")) == P.COMPLETE, what="the next run complete")
