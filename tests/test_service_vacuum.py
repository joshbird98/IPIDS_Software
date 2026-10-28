"""Vacuum gauge service against the simulated GRAPHIX controllers."""
import time

import pytest

from src.core import commands as C
from src.core.network_map import ZMQ_PORT_VACUUM_PUB
from tests.service_harness import ServiceThread, TelemetryTap

VG1 = "ion_beam.source.vacuum_gauge_1"
PERMISSIVE = "ion_beam.vacuum.gv_permissive_ready"
MISMATCH = "ion_beam.vacuum.stat_relay_sp_mismatch"


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
    assert 1e-8 < d[f"{VG1}.rb_pressure"] < 1e-5 and d[MISMATCH] == 0.0      # relay set points confirmed
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
    tap.wait_for(lambda d: d.get(f"{VG1}.rb_pressure") is not None and d.get(PERMISSIVE) == 1.0, timeout=15)
    tag = "ion_beam.vacuum.controller_10.relay_1_on"
    svc.registry_limits.pop(tag, None)          # whatever the registry says, the write path is tested
    r = client.request("vacuum", tag, 4e-6)
    assert r["status"] == C.OK and "4.0E-06" in r["reason"]
    from src.sim.vacuum import shared_graphix
    assert shared_graphix().relays[(10, 1)]["on"] == pytest.approx(4e-6)
    # a deliberate change is what the set point check expects from then on (until a restart)
    assert tap.holds_for(lambda d: d.get(PERMISSIVE) == 1.0 and d.get(MISMATCH) == 0.0, 6.0)   # > 3 round-robins
    r = client.request("vacuum", "ion_beam.vacuum.controller_10.relay_9_on", 1e-6)
    assert r["status"] == C.REJECTED


def relays_judge_vg1(tmp_path, monkeypatch):
    """The config as it was before VG1's software trip (22 October 2026): VG1 judged by its relays at
    5.5E-06 / 8.0E-06 mbar, like VG2 still is. The relay-checking tests below are written for it."""
    import json
    import shutil
    from src.core import paths
    with open(paths.config_path("vac_gauges_config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["system_interlocks"].pop("software_trips", None)
    for relay in ("1", "4"):
        cfg["10"]["relays"][relay].update(on_val=5.5e-6, off_val=8e-6)
    folder = tmp_path / "config"
    shutil.copytree(paths.CONFIG_DIR, folder, ignore=shutil.ignore_patterns("recipes", "backups"))
    (folder / "vac_gauges_config.json").write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(paths, "CONFIG_DIR", str(folder))


@pytest.fixture
def idle(monkeypatch, fake_events, tmp_path):
    """The service built but not running: its relay checks driven directly against the simulated
    controllers (whose answers a test can change). VG1 judged by its relays (relays_judge_vg1)."""
    relays_judge_vg1(tmp_path, monkeypatch)
    yield from _idle(monkeypatch, fake_events)


@pytest.fixture
def idle_trip(monkeypatch, fake_events):
    """The same with the shipped config: VG1 judged by its software trip, its relays a backstop."""
    yield from _idle(monkeypatch, fake_events)


def _idle(monkeypatch, fake_events):
    monkeypatch.setenv("SINDIE_SIMULATE", "1")
    monkeypatch.delenv("SINDIE_SIM_FAULTS", raising=False)
    from src.sim import vacuum as simvac
    simvac._GRAPHIX = None
    import src.services.service_vac_gauge_controllers as vac
    monkeypatch.setattr(vac.time, "sleep", lambda s: None)
    svc = vac.VacuumMicroservice()
    svc.events = fake_events
    svc.link.connect()
    yield svc, simvac.shared_graphix(), fake_events
    svc.shutdown()


def errors(events):
    return [e["message"] for e in events.events if e["severity"] == "ERROR"]


def check_all_relays(svc):
    svc.enforce_startup_config()
    svc.poll_device()                   # the pressures too: the permissive needs VG1's and VG2's


def reads(svc, node, relay, kind, n=3):
    """n routine (round-robin) reads of one relay value - three in a row are needed for a change."""
    for _ in range(n):
        svc.read_slow_item(node, f"relay_{kind}", relay)
    svc.evaluate_gv_permissive()


def test_start_up_check_writes_what_differs_and_reads_it_back(idle, monkeypatch):
    """Only differing relays are written (channel, then the set point moving away from the other
    first); an unreadable 'off' value doesn't stop the check; what is read back afterwards counts."""
    svc, graphix, events = idle
    graphix.relays[(10, 1)].update(ch=2, on=9.9e-6)
    real_read, real_write, written, garbled = graphix.read, graphix.write, [], []

    def read(node, group, param):
        if (node, group, param) == (10, "4", "3") and len(garbled) < 4:
            garbled.append(1)
            return "garbage"                        # relay 1's 'off', for the whole first look
        return real_read(node, group, param)

    def write(node, group, param, value):
        written.append((node, param, value))
        return real_write(node, group, param, value)
    monkeypatch.setattr(graphix, "read", read)
    monkeypatch.setattr(graphix, "write", write)
    check_all_relays(svc)
    assert written == [(10, "1", "1"), (10, "2", "5.5E-06"), (10, "3", "8.0E-06")]
    assert errors(events) == [] and svc.relay_state(10, 1) == "match"
    assert svc.state[PERMISSIVE] == 1.0 and svc.state[MISMATCH] == 0.0


def test_refused_set_point_holds_back_the_gate_valve_permissive(idle, monkeypatch):
    """A set point the controller refuses is an ERROR naming the relay and the values, and the
    gate-valve permissive waits until the controller holds what the config says - judged by three
    reads in a row, as one garbled answer could equal the expected value."""
    svc, graphix, events = idle
    graphix.relays[(10, 1)]["on"] = 9.9e-6
    real_write = graphix.write
    monkeypatch.setattr(graphix, "write", lambda node, group, param, value:
                        "NACK" if (node, param) == (10, "2") else real_write(node, group, param, value))
    check_all_relays(svc)
    [error] = errors(events)
    assert "Node 10 relay 1 (VG1)" in error and "on 9.9E-06 (should be 5.5E-06)" in error
    assert "refused" in error and "gate-valve permissive withheld" in error
    assert svc.state[PERMISSIVE] == 0.0 and svc.state[MISMATCH] == 1.0
    svc.relay_reading(10, 1, "on", "5.5E-06")              # one (garbled) answer saying it matches
    svc.evaluate_gv_permissive()
    assert svc.state[PERMISSIVE] == 0.0
    graphix.relays[(10, 1)]["on"] = 5.5e-6                 # put right at the controller
    reads(svc, 10, 1, "on")
    assert svc.state[PERMISSIVE] == 1.0 and svc.state[MISMATCH] == 0.0
    assert "match vac_gauges_config.json again" in events.events[-1]["message"]


def test_acknowledged_but_not_taken_at_start_up(idle, monkeypatch):
    svc, graphix, events = idle
    graphix.relays[(10, 2)]["off"] = 9.0e-6                 # VG2's relay 2 (config: 6.0E-06)
    monkeypatch.setattr(graphix, "write", lambda node, group, param, value: "ACK")       # and nothing done
    check_all_relays(svc)
    [error] = errors(events)
    assert "Node 10 relay 2 (VG2)" in error and "acknowledged the writes but holds these" in error
    assert svc.state[PERMISSIVE] == 0.0 and svc.state[MISMATCH] == 1.0


def test_a_changed_set_point_is_caught_but_not_garbled_answers(idle):
    """Losing the permissive can close the gate valve (and trip the relay), so routine reads only
    count as a change when three in a row give the same other value."""
    svc, graphix, events = idle
    check_all_relays(svc)
    assert svc.state[PERMISSIVE] == 1.0
    for garbled in ("5.5E-08", "2.1E-07", "5.5E-08", "5.5E-08"):   # noise / a stale answer, never 3 alike
        svc.relay_reading(10, 1, "on", garbled)
    svc.relay_reading(10, 1, "on", "5.5E-06")
    for garbled in ("5.5E-08", "5.5E-08"):
        svc.relay_reading(10, 1, "on", garbled)
    assert svc.gv_relays_confirmed() and errors(events) == []
    svc.relay_reading(10, 1, "on", "5.5E-06")
    graphix.relays[(10, 1)]["on"] = 7e-1                    # changed on the controller's front panel
    reads(svc, 10, 1, "on", n=2)
    assert svc.gv_relays_confirmed()                       # twice could still be noise
    reads(svc, 10, 1, "on", n=1)
    assert svc.state[PERMISSIVE] == 0.0 and svc.state[MISMATCH] == 1.0
    assert "on 7.0E-01 (should be 5.5E-06)" in errors(events)[-1]


class Cmd:
    def __init__(self, tag, value):
        self.tag, self.value, self.result = tag, value, None

    def ok(self, note="", data=None):
        self.result = ("ok", note)

    def reject(self, reason):
        self.result = ("rejected", reason)

    def fail(self, reason):
        self.result = ("failed", reason)


def command(svc, leaf, value, node=10):
    cmd = Cmd(f"ion_beam.vacuum.controller_{node}.{leaf}", value)
    svc.handle_command(cmd, "ion_beam.vacuum", leaf)
    return cmd.result


def test_relay_command_is_expected_from_then_on_and_still_checked(idle, monkeypatch):
    """A set point command changes what the check expects (no permissive drop for it, and no read-back
    that a garbled answer could spoil); if the controller acknowledged it but didn't take it, the
    routine reads catch that."""
    svc, graphix, events = idle
    check_all_relays(svc)
    assert command(svc, "relay_1_on", 4e-6)[0] == "ok" and svc.gv_relays_confirmed()
    assert "vac_gauges_config.json says 5.5E-06" in events.events[-1]["message"]
    monkeypatch.setattr(graphix, "write", lambda node, group, param, value: "ACK")      # ...and does nothing
    assert command(svc, "relay_1_on", 3e-6)[0] == "ok" and svc.gv_relays_confirmed()
    reads(svc, 10, 1, "on")                                 # the controller still holds 4.0E-06
    assert not svc.gv_relays_confirmed()
    assert "what was last set" in errors(events)[-1] and "on 4.0E-06 (should be 3.0E-06)" in errors(events)[-1]


def test_command_on_an_unconfirmed_relay_needs_its_read_back(idle, monkeypatch):
    svc, graphix, events = idle
    graphix.relays[(10, 1)]["on"] = 9.9e-6
    real_write = graphix.write
    monkeypatch.setattr(graphix, "write", lambda node, group, param, value:
                        "NACK" if (node, param) == (10, "2") else real_write(node, group, param, value))
    check_all_relays(svc)
    assert svc.state[PERMISSIVE] == 0.0
    monkeypatch.setattr(graphix, "write", lambda node, group, param, value: "ACK")      # acknowledged, not done
    assert command(svc, "relay_1_on", 5.5e-6)[0] == "ok"
    svc.evaluate_gv_permissive()
    assert svc.state[PERMISSIVE] == 0.0                     # the read-back still says 9.9E-06
    monkeypatch.setattr(graphix, "write", real_write)
    assert command(svc, "relay_1_on", 5.5e-6)[0] == "ok"
    svc.evaluate_gv_permissive()
    assert svc.state[PERMISSIVE] == 1.0 and "match vac_gauges_config.json again" in events.events[-2]["message"]


def test_relay_commands_are_limited(idle):
    svc, graphix, events = idle
    check_all_relays(svc)
    status, reason = command(svc, "relay_1_ch", 3)
    assert status == "rejected" and "channel comes from vac_gauges_config.json" in reason
    status, reason = command(svc, "relay_1_on", 2000.0)
    assert status == "rejected" and reason.startswith("Hard cap")                # the cap is said first
    status, reason = command(svc, "relay_1_on", 2e-5)                         # VG1: the gate valve's relay
    assert status == "rejected" and reason.startswith("Hard cap") and "gate valve may open" in reason
    status, reason = command(svc, "relay_1_on", 1.5, node=20)                  # VG4: the other relays' cap
    assert status == "rejected" and "1 mbar cap" in reason
    status, reason = command(svc, "relay_1_on", 9e-6)                          # off is 8.0E-06
    assert status == "rejected" and "below the off point" in reason
    assert graphix.relays[(10, 1)]["on"] == pytest.approx(5.5e-6) and svc.gv_relays_confirmed()


def test_other_gauges_relays_alarm_without_holding_the_gate_valve(idle, monkeypatch):
    svc, graphix, events = idle
    graphix.relays[(20, 1)]["on"] = 0.5                     # VG4's relay
    monkeypatch.setattr(graphix, "write", lambda node, group, param, value: "NACK")
    check_all_relays(svc)
    [error] = errors(events)
    assert "Node 20 relay 1 (VG4)" in error and "withheld" not in error
    assert svc.state[PERMISSIVE] == 1.0 and svc.state[MISMATCH] == 1.0


def test_no_relay_for_a_gate_valve_gauge_withholds_the_permissive(monkeypatch, fake_events):
    """The permissive judges VG1's and VG2's pressure by their relays: a config that gives one of
    them none (or an unreadable config) mustn't let it ignore that gauge."""
    monkeypatch.setenv("SINDIE_SIMULATE", "1")
    monkeypatch.delenv("SINDIE_SIM_FAULTS", raising=False)
    from src.sim import vacuum as simvac
    simvac._GRAPHIX = None
    import src.services.service_vac_gauge_controllers as vac
    from src.core import config_store
    monkeypatch.setattr(vac.time, "sleep", lambda s: None)
    config = config_store.read("vac_gauges_config.json")
    config["10"]["relays"] = {r: v for r, v in config["10"]["relays"].items() if v["channel"] != 2}   # none for VG2
    monkeypatch.setattr(vac.VacuumMicroservice, "load_config", lambda self, name, default: config)
    svc = vac.VacuumMicroservice()
    try:
        svc.events = fake_events
        svc.link.connect()
        check_all_relays(svc)
        assert svc.state[PERMISSIVE] == 0.0 and svc.state[MISMATCH] == 1.0
        assert "VG2 has no relay in vac_gauges_config.json" in errors(fake_events)[0]
    finally:
        svc.shutdown()


def test_a_stale_gate_valve_gauge_withholds_the_permissive(idle, monkeypatch):
    """No good reading of VG1 or VG2 for GV_STALE_S counts against the permissive (it used to keep
    its last value); one garbled answer doesn't - nor does it flag the gauge as not answering (on the
    real bus about one read in seven goes unanswered: the flag flickered on every 3 s or so)."""
    svc, graphix, events = idle
    import src.services.service_vac_gauge_controllers as vac
    check_all_relays(svc)
    assert svc.state[PERMISSIVE] == 1.0
    real_read = graphix.read
    monkeypatch.setattr(graphix, "read", lambda node, group, param:
                        None if (node, group) == (10, "1") else real_read(node, group, param))
    svc.read_pressure(10, 1)                                 # one failed read
    svc.evaluate_gv_permissive()
    assert svc.state[PERMISSIVE] == 1.0 and svc.state[f"{VG1}.stat_comms_fail"] == 0.0
    svc._last_answer[VG1] -= vac.PRESSURE_STALE_S + 0.1      # no answer for 3 s: not answering
    svc.read_pressure(10, 1)
    assert svc.state[f"{VG1}.stat_comms_fail"] == 1.0
    svc._last_good_read[VG1] -= vac.GV_STALE_S + 1          # ...and no reading for longer
    svc.evaluate_gv_permissive()
    assert svc.state[PERMISSIVE] == 0.0
    monkeypatch.setattr(graphix, "read", real_read)
    svc.read_pressure(10, 1)                                 # answering again
    assert svc.state[f"{VG1}.stat_comms_fail"] == 0.0


def test_a_controller_that_doesnt_answer_is_left_out_of_the_check(idle, monkeypatch):
    """Node 20 switched off: no writes to it (a timed-out write drops the link and re-runs the whole
    connect), VG1/VG2 still checked and the permissive given; node 20's relays alarm as unread."""
    svc, graphix, events = idle
    real_read, real_write, writes = graphix.read, graphix.write, []
    monkeypatch.setattr(graphix, "read", lambda node, group, param:
                        None if node == 20 else real_read(node, group, param))
    monkeypatch.setattr(graphix, "write", lambda node, group, param, value:
                        writes.append(node) or ("ERROR" if node == 20 else real_write(node, group, param, value)))
    svc.read_static_data()
    assert svc.silent_nodes == {20} and "node 20 doesn't answer" in errors(events)[0]
    check_all_relays(svc)
    assert 20 not in writes and svc.link.connected
    assert svc.state[PERMISSIVE] == 1.0 and svc.state[MISMATCH] == 1.0


def test_no_writes_to_a_relay_that_doesnt_answer(idle, monkeypatch):
    svc, graphix, events = idle
    real_read, writes = graphix.read, []
    monkeypatch.setattr(graphix, "read", lambda node, group, param:
                        None if (node, group, param) in {(10, "4", p) for p in ("1", "2", "3")}
                        else real_read(node, group, param))
    monkeypatch.setattr(graphix, "write", lambda *a: writes.append(a) or "ACK")
    svc.enforce_relay(10, 1)
    assert writes == [] and "didn't answer" in errors(events)[0] and "withheld" in errors(events)[0]


def test_a_controller_that_comes_back_gets_the_check_it_missed(idle, monkeypatch):
    """Silent at connect, answering later: its names and relays are checked (and corrected) then,
    not only picked up from routine reads - which need three agreeing answers even from unknown."""
    svc, graphix, events = idle
    real_read = graphix.read
    dead = {"node": 20}
    monkeypatch.setattr(graphix, "read", lambda node, group, param:
                        None if node == dead["node"] else real_read(node, group, param))
    svc.read_static_data()
    check_all_relays(svc)
    assert svc.silent_nodes == {20}
    svc.relay_reading(20, 1, "on", "7.0E-01")                # one routine answer from unknown: not enough
    assert svc.relay_state(20, 1) is None
    graphix.relays[(20, 1)]["on"] = 0.5                     # came back with a wrong set point
    dead["node"] = None
    svc.poll_device()
    assert svc.silent_nodes == set() and graphix.relays[(20, 1)]["on"] == pytest.approx(0.7)   # corrected
    assert "node 20 answers again" in " ".join(e["message"] for e in events.events)
    svc.evaluate_gv_permissive()
    assert svc.state[MISMATCH] == 0.0 and svc.state[PERMISSIVE] == 1.0


def test_a_relay_status_not_read_lately_is_unknown(idle, monkeypatch):
    """Each relay's on/off status (the Interlocks panel rules the loop's gauge relay in or out with
    it) is withdrawn when it hasn't been read for a while, or the controllers can't be reached:
    never an old reading."""
    import src.services.service_vac_gauge_controllers as vac
    svc, graphix, events = idle
    tag, at = vac.relay_status_tag(10, 4), vac.relay_read_at_tag(10, 4)
    assert svc.state[tag] is None and svc.state[at] is None    # this run hasn't read it yet
    svc.read_slow_item(10, "relay_status", 4)
    assert svc.state[tag] == 1.0                                # VG1 well below the relay's set point
    assert abs(svc.state[at] - time.time()) < 5
    svc.withdraw_stale_relay_status()
    assert svc.state[tag] == 1.0
    svc._relay_status_at[(10, 4)] -= vac.RELAY_STATUS_STALE_S + 1     # ...and not read since
    svc.withdraw_stale_relay_status()
    assert svc.state[tag] is None and svc.state[at] is None
    svc.read_slow_item(10, "relay_status", 4)
    assert svc.state[tag] == 1.0
    svc.publish_unknown()                                       # the controllers unreachable
    assert svc.state[tag] is None


def test_config_values_past_a_hard_cap_are_never_written(monkeypatch, fake_events):
    """A vac_gauges_config.json edited past the editor: those relay values aren't written to the
    controller; for VG1 / VG2 the gate-valve permissive is withheld, with the reason."""
    monkeypatch.setenv("SINDIE_SIMULATE", "1")
    monkeypatch.delenv("SINDIE_SIM_FAULTS", raising=False)
    from src.sim import vacuum as simvac
    simvac._GRAPHIX = None
    import src.services.service_vac_gauge_controllers as vac
    from src.core import config_store
    monkeypatch.setattr(vac.time, "sleep", lambda s: None)
    config = config_store.read("vac_gauges_config.json")
    config["10"]["relays"]["2"].update(on_val=0.7, off_val=0.8)      # VG2's (VG1 has a software trip)
    monkeypatch.setattr(vac.VacuumMicroservice, "load_config", lambda self, name, default: config)
    svc = vac.VacuumMicroservice()
    try:
        svc.events = fake_events
        svc.link.connect()
        written = []
        real_write = simvac.shared_graphix().write
        monkeypatch.setattr(simvac.shared_graphix(), "write",
                            lambda node, group, param, value: written.append((node, param)) or real_write(node, group, param, value))
        check_all_relays(svc)
        assert (10, "6") not in written and (10, "7") not in written         # relay 2's on / off untouched
        assert svc.state[PERMISSIVE] == 0.0 and svc.state[MISMATCH] == 1.0
        assert "Hard cap" in errors(fake_events)[0] and "gate valve may open" in errors(fake_events)[0]
    finally:
        svc.shutdown()


@pytest.mark.parametrize("relay, values, gate_valve", [
    ("2", {"on_val": "7e-1", "off_val": "8e-1"}, True),     # VG2; text, as a hand edit might leave it
    ("1", {"on_val": 15.0, "off_val": 25.0}, True),         # VG1's backstop relay: past its 20 mbar cap
    ("3", {"on_val": 1.5, "off_val": 2.0}, False),         # VG3: the other relays' 1 mbar cap
])
def test_config_values_past_a_cap_are_never_written(monkeypatch, fake_events, relay, values, gate_valve):
    monkeypatch.setenv("SINDIE_SIMULATE", "1")
    monkeypatch.delenv("SINDIE_SIM_FAULTS", raising=False)
    from src.sim import vacuum as simvac
    simvac._GRAPHIX = None
    import src.services.service_vac_gauge_controllers as vac
    from src.core import config_store
    monkeypatch.setattr(vac.time, "sleep", lambda s: None)
    config = config_store.read("vac_gauges_config.json")
    config["10"]["relays"][relay].update(values)
    monkeypatch.setattr(vac.VacuumMicroservice, "load_config", lambda self, name, default: config)
    svc = vac.VacuumMicroservice()
    try:
        svc.events = fake_events
        svc.link.connect()
        params = vac.RELAY_PARAMS[int(relay)]
        written = []
        real_write = simvac.shared_graphix().write
        monkeypatch.setattr(simvac.shared_graphix(), "write", lambda node, group, param, value:
                            written.append((node, param)) or real_write(node, group, param, value))
        check_all_relays(svc)
        assert not {(10, params["on"]), (10, params["off"])} & set(written)
        assert any("Hard cap" in e for e in errors(fake_events))
        assert svc.state[MISMATCH] == 1.0 and svc.state[PERMISSIVE] == (0.0 if gate_valve else 1.0)
    finally:
        svc.shutdown()


# --- VG1's software trip: short spikes ridden through (powdered targets, Josh, 22 October 2026) ------
VG1_BASE = "ion_beam.source.vacuum_gauge_1"


def test_vg1_rides_through_a_short_spike_and_trips_on_a_long_one(idle_trip):
    svc, graphix, events = idle_trip
    trip = svc.trips[(10, 1)]
    assert (trip.trip_mbar, trip.reset_mbar, trip.ride_through_s) == (8e-6, 5.5e-6, 2.5)

    def at(t, p, rapid=False):
        svc.judge_trip(trip, VG1_BASE, t, p, rapid)
        return {k: svc.state[f"{VG1_BASE}.{k}"] for k in ("stat_above_sp", "stat_rapid_rise", "stat_riding_through")}

    quiet = {"stat_above_sp": 0.0, "stat_rapid_rise": 0.0, "stat_riding_through": 0.0}
    riding = dict(quiet, stat_riding_through=1.0)
    assert at(0.0, 2e-7) == quiet
    assert at(1.0, 1.0, rapid=True) == riding                       # a 1 mbar spike: not reported
    assert at(2.4, 0.5) == riding
    assert at(3.0, 3e-7) == riding                                  # back down - once the next reading agrees
    assert at(3.4, 3e-7) == quiet
    assert any("ridden through: up to 1.0E+00 mbar for 2.0 s" in e["message"] for e in events.events)
    # Not down within 2.5 s: reported, with the rapid rise, even with no reading in between
    at(10.0, 0.8, rapid=True)
    assert at(12.4, None)["stat_above_sp"] == 0.0
    assert at(12.6, None) == {"stat_above_sp": 1.0, "stat_rapid_rise": 1.0, "stat_riding_through": 0.0}
    assert any("above 8.0E-06 mbar for 2.5 s" in m for m in errors(events))
    assert at(14.0, 7e-6)["stat_above_sp"] == 1.0           # between the points: still tripped
    assert at(15.0, 5e-6)["stat_above_sp"] == 1.0           # at or below the reset point once...
    assert at(15.4, 5e-6) == quiet                          # ... and again: clear
    assert any("back below 5.5E-06 mbar" in e["message"] for e in events.events)
    # A slow rise over the trip point counts the same
    at(20.0, 9e-6)
    assert at(22.6, 9e-6) == {"stat_above_sp": 1.0, "stat_rapid_rise": 0.0, "stat_riding_through": 0.0}
    at(23.0, 2e-7), at(23.4, 2e-7)
    # The excursion ends only at the reset point, as the relay's hysteresis: a spike that settles
    # between the points, or a pressure hovering about the trip point, trips after 2.5 s all the same
    for start, readings in ((30.0, (1.0, 7e-6, 7e-6)), (40.0, (9e-6, 7.9e-6, 8.5e-6, 7.9e-6))):
        for k, p in enumerate(readings):
            assert at(start + 0.8 * k, p) == riding
        assert at(start + 2.5, 7.5e-6)["stat_above_sp"] == 1.0
        at(start + 3.0, 5.5e-6)
        assert at(start + 3.4, 5.5e-6)["stat_above_sp"] == 0.0      # (at the reset point)
    # One low reading (garbled, or stale: the driver can't tell) doesn't restart the clock
    assert at(50.0, 1.0) == riding
    assert at(50.8, 1e-7) == riding
    assert at(51.2, 0.9) == riding
    assert at(52.5, 0.9)["stat_above_sp"] == 1.0
    at(53.0, 2e-7), at(53.4, 2e-7)
    # A spike judged as over when it came down, if the next reading agrees
    at(60.0, 1.0)
    at(62.3, 2e-7)
    assert at(62.7, 2e-7) == quiet
    assert any("ridden through: up to 1.0E+00 mbar for 2.3 s" in e["message"] for e in events.events)


def test_vg1_backstop_relay_open_reports_at_once(idle_trip):
    """A backstop relay read open counts at once when VG1 agrees (above the relay's on point, or no
    live reading); with VG1 reading low, at the next read that says open too."""
    svc, graphix, events = idle_trip
    svc.poll_device()
    svc.read_slow_item(10, "relay_status", 1)
    assert svc.state[f"{VG1_BASE}.stat_above_sp"] == 0.0
    graphix.relay_closed[(10, 1)] = False
    graphix.relays[(10, 1)]["on"] = graphix.relays[(10, 1)]["off"] = 1e-12     # (the simulated relay opens)
    svc.read_slow_item(10, "relay_status", 1)               # VG1 reads ~2e-7: a garbled answer?
    assert svc.state[f"{VG1_BASE}.stat_above_sp"] == 0.0 and not errors(events)
    svc.read_slow_item(10, "relay_status", 1)
    assert svc.state[f"{VG1_BASE}.stat_above_sp"] == 1.0
    assert any("backstop relay 1" in m for m in errors(events))
    svc.evaluate_gv_permissive()
    assert svc.state["ion_beam.vacuum.gv_permissive_ready"] == 0.0
    # Relay 4 open with VG1 above its on point: at once
    svc.state[f"{VG1_BASE}.rb_pressure"] = 20.0
    graphix.relay_closed[(10, 4)] = False
    graphix.relays[(10, 4)]["on"] = graphix.relays[(10, 4)]["off"] = 1e-12
    svc.read_slow_item(10, "relay_status", 4)
    assert any("backstop relay 4" in m for m in errors(events))


def test_vg1_relays_may_be_a_backstop_only_with_a_valid_trip(idle_trip):
    """VG1's relays at 10 mbar are accepted (written, and confirmed) with its software trip; the
    gate-valve permissive is given; without the trip they would be past the 1e-05 mbar cap."""
    svc, graphix, events = idle_trip
    check_all_relays(svc)
    assert graphix.relays[(10, 1)]["off"] == 10.0 and not svc.relay_cap_problems
    svc.poll_device()
    assert svc.state["ion_beam.vacuum.gv_permissive_ready"] == 1.0
    from src.core import hard_caps
    cfg = dict(svc.config)
    cfg["system_interlocks"] = {k: v for k, v in cfg["system_interlocks"].items() if k != "software_trips"}
    assert [v for v in hard_caps.violations("vac_gauges_config.json", cfg) if "relay 1 off point" in v.message]


def test_vg1_answers_that_are_no_pressure_are_not_readings(idle_trip):
    """For VG1 and VG2 (the gate valve's gauges) 0, below 0, nan and inf aren't pressures: a stale
    "0" meant for another request would look like a perfect vacuum. A trailing '>' is the GRAPHIX's."""
    svc, graphix, events = idle_trip
    replies = iter(["0", "-1.0E-07", "nan", "inf", "1.00E-07>"])
    svc.link.read = lambda node, group, param: next(replies)
    for _ in range(2):                  # (each read that gives no pressure is made again at once)
        svc.state[f"{VG1_BASE}.rb_pressure"] = None
        assert svc.read_pressure(10, 1) is True and svc.state[f"{VG1_BASE}.rb_pressure"] is None
    svc.read_pressure(10, 1)
    assert svc.state[f"{VG1_BASE}.rb_pressure"] == pytest.approx(1e-7)


def test_vg1_unread_for_6_s_loses_the_permissive(idle_trip, monkeypatch):
    """With only a 10 mbar backstop behind it, VG1 without a good reading for 6 s loses the gate-valve
    permissive; it shows STALE from 3 s - VG2, with its relays, after 10 s as before. (It was 3 s: on
    the lab's bus VG1 went that long unread a few times a day with the vacuum fine, and each time with
    the safety relay on, the gate valve began to close and the relay dropped - October 2026.)"""
    import src.services.service_vac_gauge_controllers as vac
    svc, graphix, events = idle_trip
    check_all_relays(svc)
    assert svc.state[PERMISSIVE] == 1.0
    clock = [time.monotonic()]
    monkeypatch.setattr(vac.time, "monotonic", lambda: clock[0])
    svc._last_good_read[VG1_BASE] = svc._last_answer[VG1_BASE] = clock[0]
    monkeypatch.setattr(graphix, "read", lambda node, group, param: None)      # no answers from now on
    clock[0] += 3.1
    svc.read_pressure(10, 1)
    svc.evaluate_gv_permissive()
    assert svc.state[f"{VG1_BASE}.rb_pressure"] is None                         # STALE...
    assert svc.state[f"{VG1_BASE}.stat_comms_fail"] == 1.0 and svc.state[PERMISSIVE] == 1.0   # ...still given
    clock[0] += 2.8
    svc.evaluate_gv_permissive()
    assert svc.state[PERMISSIVE] == 1.0
    clock[0] += 0.2
    svc.evaluate_gv_permissive()
    assert svc.state[PERMISSIVE] == 0.0


def test_a_vg1_read_that_fails_is_made_again_at_once(idle_trip, monkeypatch):
    """About one read in five fails on the lab's bus, at random: VG1's (the gauge with a software trip,
    whose permissive goes after 6 s without a good reading) is made again at once, once. Other gauges'
    aren't: each retry is bus time, and they have their relays."""
    svc, graphix, events = idle_trip
    real_read, attempts, replies = graphix.read, [], {}

    def read(node, group, param):
        if param == "29":
            attempts.append((node, int(group)))
            queue = replies.get((node, int(group)))
            if queue:
                return queue.pop(0)
        return real_read(node, group, param)
    monkeypatch.setattr(graphix, "read", read)
    replies[(10, 1)] = [None]                                   # no answer, then a pressure
    assert svc.read_pressure(10, 1) is True
    assert attempts == [(10, 1)] * 2 and 1e-8 < svc.state[f"{VG1_BASE}.rb_pressure"] < 1e-5
    first = svc._last_good_read[VG1_BASE]
    attempts.clear()
    replies[(10, 1)] = ["0"]                                    # an answer that isn't a pressure, then one
    svc.read_pressure(10, 1)
    assert attempts == [(10, 1)] * 2 and svc._last_good_read[VG1_BASE] >= first
    attempts.clear()
    replies[(10, 1)] = [None, None]                             # both fail: no third try
    good = svc._last_good_read[VG1_BASE]
    assert svc.read_pressure(10, 1) is False
    assert attempts == [(10, 1)] * 2 and svc._last_good_read[VG1_BASE] == good
    attempts.clear()
    replies[(10, 2)] = [None]                                   # VG2: once
    assert svc.read_pressure(10, 2) is False
    assert attempts == [(10, 2)]


def test_a_trip_during_a_controller_check_is_published_at_once(idle_trip, monkeypatch):
    """A controller check (here node 20 answering again) runs outside the loop, with nothing
    published - service_plc holds the last judgment meanwhile. VG1 is still read between its reads
    and writes, and a trip is published at once, as this run's own judgment."""
    import src.services.service_vac_gauge_controllers as vac
    svc, graphix, events = idle_trip
    check_all_relays(svc)
    published = []
    monkeypatch.setattr(svc, "publish", lambda: published.append(dict(svc.state)))
    clock = [time.monotonic()]
    monkeypatch.setattr(vac.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(graphix, "pressure", lambda node, ch: 1.0 if (node, ch) == (10, 1) else 2e-7)
    real_read = svc.link.read

    def slow_read(node, group, param):          # each read of the check takes 0.4 s
        if group == "4" or param == "5":
            clock[0] += 0.4
        return real_read(node, group, param)
    monkeypatch.setattr(svc.link, "read", slow_read)
    svc.check_node(20)
    tripped = [d for d in published if d.get(f"{VG1_BASE}.stat_above_sp") == 1.0]
    assert tripped and tripped[0][PERMISSIVE] == 0.0 and tripped[0]["ion_beam.vacuum.stat_judged"] == 1.0
    assert any("above 8.0E-06 mbar for 2.5 s" in m for m in errors(events))


def test_the_spike_fault_is_ridden_through_by_the_running_service(stack):
    """The simulator's powdered-target bursts (VG1 at ~1 mbar for 0.8 s every 6 s): the gate-valve
    permissive stays given throughout."""
    svc, tap, client, mp = stack
    tap.wait_for(lambda d: d.get(PERMISSIVE) == 1.0, timeout=15)
    mp.setenv("SINDIE_SIM_FAULTS", "vacuum_spike_source")
    d = tap.wait_for(lambda d: d.get(f"{VG1}.stat_riding_through") == 1.0, timeout=10)
    assert d[PERMISSIVE] == 1.0 and d[f"{VG1}.rb_pressure"] > 0.1
    end = time.time() + 7
    while time.time() < end:
        d = tap.wait_for(lambda d: True, timeout=2)
        assert d[PERMISSIVE] == 1.0 and d[f"{VG1}.stat_above_sp"] == 0.0


def test_the_permissive_holds_through_the_lab_bus_dropouts(stack):
    """The lab's gauge bus loses about one read in five, and now and then answers nothing for a few
    seconds: VG1 went 3 s without a good reading a few times a day, which dropped the safety relay with
    the vacuum fine (October 2026). Simulated (vacuum_lossy_bus, here a 3.5 s silence 2.5 s in, as the
    longest gaps seen): VG1 goes longer than the old 3 s without a good reading, and the gate-valve
    permissive holds throughout."""
    import threading
    svc, tap, client, mp = stack
    from src.sim import vacuum as simvac
    mp.setattr(simvac, "LOSSY_SILENT_EVERY_S", 6.0)
    mp.setattr(simvac, "LOSSY_SILENT_FOR_S", 3.5)
    tap.wait_for(lambda d: d.get(PERMISSIVE) == 1.0, timeout=15)
    mp.setenv("SINDIE_SIM_FAULTS", "vacuum_lossy_bus")
    worst, stop = [0.0], threading.Event()

    def watch_vg1():                    # the longest VG1 has gone without a good reading
        while not stop.is_set():
            last = svc._last_good_read.get(VG1)
            if last is not None:
                worst[0] = max(worst[0], time.monotonic() - last)
            time.sleep(0.02)
    watcher = threading.Thread(target=watch_vg1, daemon=True)
    watcher.start()
    try:
        assert tap.holds_for(lambda d: d[PERMISSIVE] == 1.0, 8.0)
    finally:
        stop.set()
        watcher.join(1)
    assert 3.2 < worst[0] < 6.0
