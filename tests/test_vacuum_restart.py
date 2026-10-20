"""A vacuum-service restart doesn't close the gate valve (Josh, October 2026).

Until a restarted vacuum service has judged the vacuum itself (ion_beam.vacuum.stat_judged), the
PLC service keeps giving the PLC its last judgment - the gate valve's vacuum permissive, the GRAPHIX
comms flags and the gauge flags - for at most VACUUM_HOLD_S, and then counts the vacuum as unknown
as before. The handover back writes nothing when nothing changed: no momentary drop."""
import time

import pytest

from src.core.network_map import ZMQ_PORT_PLC_PUB, ZMQ_PORT_VACUUM_PUB
from tests.service_harness import ServiceThread, TelemetryTap

OK_TAG = "ion_beam.source.chamber.stat_vac_ok_for_gv"
G1 = "ion_beam.facilities.graphix1.stat_comms_fail"
G2 = "ion_beam.facilities.graphix2.stat_comms_fail"
HOLD_LEFT = "ion_beam.source.chamber.vac_hold_left_s"
JUDGED = "ion_beam.vacuum.stat_judged"
PERMISSIVE = "ion_beam.vacuum.gv_permissive_ready"


class Clock:
    def __init__(self):
        self.t = 5000.0

    def __call__(self):
        return self.t


def vacuum_message(judged=True, ok=True, comms_fail=False, **extra):
    message = {PERMISSIVE: 1.0 if ok else 0.0, G1: 1.0 if comms_fail else 0.0, G2: 1.0 if comms_fail else 0.0,
               "system.connected": 0.0 if comms_fail else 1.0, **extra}
    if judged is not None:
        message[JUDGED] = 1.0 if judged else 0.0
    return message


@pytest.fixture
def plc(monkeypatch, fake_events):
    """The PLC service connected to a simulated PLC, not running: the test hands it vacuum messages
    and moves its clock."""
    monkeypatch.setenv("SINDIE_SIMULATE", "1")
    monkeypatch.delenv("SINDIE_SIM_FAULTS", raising=False)
    from src.services import service_plc as sp
    from src.sim import plc as simplc
    simplc._PLC = None
    svc = sp.PlcMicroservice()
    svc.events = fake_events
    svc.clock = Clock()
    svc.last_seen = dict.fromkeys(svc.last_seen, svc.clock())
    svc.client = sp._new_client()
    svc.client.connect("sim", 0, 1)
    svc.connected = True
    svc.written = []                     # every value written to the PLC's vacuum tags, in order
    real_write = svc._write_tags_batched

    def spy(updates):
        before = dict(svc.mailbox_cache)
        real_write(updates)
        svc.written += [(t, v) for t, v in updates.items() if t in (OK_TAG, G1, G2) and before.get(t) != v]
    svc._write_tags_batched = spy
    yield svc
    svc.shutdown()


def receive(svc, message):
    """As _process_external_telemetry does with one message from the vacuum service."""
    svc.last_seen["VACUUM"] = svc.clock()
    svc._write_tags_batched(svc._vacuum_updates(message))
    svc._enforce_it_watchdogs()


def silence(svc, seconds, step=0.5):
    for _ in range(int(seconds / step)):
        svc.clock.t += step
        svc._enforce_it_watchdogs()


def plc_value(tag):
    from src.sim.plc import shared_plc
    return shared_plc().get(tag)


def test_a_restart_keeps_the_last_judgment_and_hands_back_without_a_drop(plc):
    receive(plc, vacuum_message())
    assert plc_value(OK_TAG) == 1.0 and plc_value(G1) == 0.0
    silence(plc, 12)                                        # restarting: its start-up check, silent
    assert plc.state[HOLD_LEFT] == pytest.approx(48.0, abs=0.6)
    for _ in range(5):                                      # publishing again, not judged yet
        plc.clock.t += 0.5
        receive(plc, vacuum_message(judged=False, ok=False))
    plc.clock.t += 0.5
    receive(plc, vacuum_message())                          # judged: the vacuum is good
    assert sorted(plc.written) == sorted([(OK_TAG, True), (G1, False), (G2, False)])   # the first message's
                                                                                       # only: no drop
    assert plc_value(OK_TAG) == 1.0 and plc.state[HOLD_LEFT] == 0.0
    warning, info = plc.events.events[-2:]
    assert warning["severity"] == "WARNING" and "keeps its last judgment" in warning["message"]
    assert "permissive given" in warning["message"] and "judging the vacuum again" in info["message"]


def test_the_hold_ends_after_its_time(plc):
    from src.services.service_plc import VACUUM_HOLD_S
    receive(plc, vacuum_message())
    silence(plc, VACUUM_HOLD_S - 1)
    assert plc_value(OK_TAG) == 1.0
    silence(plc, 2)
    assert plc_value(OK_TAG) == 0.0 and plc_value(G1) == 1.0 and plc_value(G2) == 1.0
    assert plc.state[HOLD_LEFT] == 0.0
    assert plc.events.events[-1]["severity"] == "ERROR" and "unknown" in plc.events.events[-1]["message"]


def test_a_service_still_starting_past_the_hold_is_followed(plc):
    receive(plc, vacuum_message())
    silence(plc, 61)
    receive(plc, vacuum_message(judged=False, ok=False))
    assert plc_value(OK_TAG) == 0.0


def test_a_restarted_service_that_finds_the_vacuum_bad_is_followed_at_once(plc):
    receive(plc, vacuum_message())
    silence(plc, 5)
    receive(plc, vacuum_message(judged=False, ok=False))
    assert plc_value(OK_TAG) == 1.0
    receive(plc, vacuum_message(judged=True, ok=False))
    assert plc_value(OK_TAG) == 0.0


def test_no_hold_for_a_running_service_that_loses_its_controllers(plc):
    """A judged comms loss is real: the PLC gets it at once, as before."""
    receive(plc, vacuum_message())
    receive(plc, vacuum_message(ok=False, comms_fail=True))
    assert plc_value(OK_TAG) == 0.0 and plc_value(G1) == 1.0
    assert plc.state[HOLD_LEFT] == 0.0


def test_nothing_to_hold_without_a_judgment(plc):
    """The PLC service started with the vacuum service down: nothing judged, nothing held."""
    silence(plc, 3)
    assert plc_value(OK_TAG) == 0.0 and plc_value(G1) == 1.0


def test_an_older_vacuum_service_without_the_flag_counts_as_judged(plc):
    receive(plc, vacuum_message(judged=None, ok=False))
    assert plc_value(OK_TAG) == 0.0


# ------------------------------------------------------------------ the vacuum service's judgment
@pytest.fixture
def vacuum(monkeypatch, fake_events):
    monkeypatch.setenv("SINDIE_SIMULATE", "1")
    monkeypatch.delenv("SINDIE_SIM_FAULTS", raising=False)
    from src.sim import vacuum as simvac
    simvac._GRAPHIX = None
    import src.services.service_vac_gauge_controllers as vac
    monkeypatch.setattr(vac.time, "sleep", lambda s: None)
    svc = vac.VacuumMicroservice()
    svc.events = fake_events
    svc.link.connect()
    yield svc, simvac.shared_graphix()
    svc.shutdown()


def test_judged_once_the_permissive_is_given(vacuum):
    svc, graphix = vacuum
    assert svc.state[JUDGED] == 0.0
    svc.after_connect(0)                                    # the start-up check
    assert svc.state[JUDGED] == 0.0
    svc.poll_device()
    assert svc.state[PERMISSIVE] == 1.0 and svc.state[JUDGED] == 1.0


def test_not_ok_is_a_judgment_only_after_the_grace(vacuum, monkeypatch):
    """VG1 not answering after a restart: maybe not read yet, so not judged at once; after
    JUDGE_GRACE_S it is (the permissive withheld), and stays judged through a link loss."""
    import src.services.service_vac_gauge_controllers as vac
    svc, graphix = vacuum
    real_read = graphix.read
    monkeypatch.setattr(graphix, "read", lambda node, group, param:
                        None if (node, group) == (10, "1") else real_read(node, group, param))
    svc.after_connect(0)
    svc.poll_device()
    assert svc.state[PERMISSIVE] == 0.0 and svc.state[JUDGED] == 0.0
    svc._polling_since -= vac.JUDGE_GRACE_S + 1
    svc.poll_device()
    assert svc.state[PERMISSIVE] == 0.0 and svc.state[JUDGED] == 1.0
    svc.publish_unknown()
    assert svc.state[JUDGED] == 1.0


# ------------------------------------------------------------------ both services, a real restart
@pytest.mark.slow
def test_restarting_the_vacuum_service_keeps_the_gate_valve_permissive(monkeypatch):
    """Both services running against the simulators; the vacuum service is stopped and a new one
    started whose start-up check takes 4 s (the real bus is slow): the PLC's vacuum permissive and
    GRAPHIX flags never change, and the PLC service says it is holding them meanwhile."""
    import threading
    monkeypatch.setenv("SINDIE_SIMULATE", "1")
    monkeypatch.delenv("SINDIE_SIM_FAULTS", raising=False)
    from src.services.service_plc import PlcMicroservice
    import src.services.service_vac_gauge_controllers as vac
    from src.sim import plc as simplc, vacuum as simvac
    simplc._PLC, simvac._GRAPHIX = None, None
    plc_svc = PlcMicroservice()
    plc_run = ServiceThread(plc_svc)
    vac_run = ServiceThread(vac.VacuumMicroservice())
    plc_tap = TelemetryTap(ZMQ_PORT_PLC_PUB)
    vac_tap = TelemetryTap(ZMQ_PORT_VACUUM_PUB)
    seen, stop = [], threading.Event()
    try:
        vac_tap.wait_for(lambda d: d.get(JUDGED) == 1.0 and d.get(PERMISSIVE) == 1.0, timeout=15)
        plc_tap.wait_for(lambda d: d.get(OK_TAG) == 1.0 and d.get(G1) == 0.0, timeout=10)

        def watch():
            while not stop.is_set():
                seen.append((plc_value(OK_TAG), plc_value(G1), plc_value(G2)))
                time.sleep(0.05)
        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        vac_run.stop()                                      # the restart
        slow = vac.VacuumMicroservice()
        real_static = slow.read_static_data

        def slow_static():
            for _ in range(40):                             # 4 s, heartbeating as the real one does
                slow.heartbeat()
                time.sleep(0.1)
            real_static()
        slow.read_static_data = slow_static
        vac_run = ServiceThread(slow)
        held = plc_tap.wait_for(lambda d: (d.get(HOLD_LEFT) or 0) > 0, timeout=10)
        assert held[HOLD_LEFT] <= 60
        vac_tap.wait_for(lambda d: d.get(JUDGED) == 1.0 and d.get(PERMISSIVE) == 1.0, timeout=20)
        plc_tap.wait_for(lambda d: d.get(HOLD_LEFT) == 0.0, timeout=5)
        stop.set()
        watcher.join(2)
        assert seen and set(seen) == {(1.0, 0.0, 0.0)}, sorted(set(seen))
    finally:
        stop.set()
        plc_tap.close()
        vac_tap.close()
        vac_run.stop()
        plc_run.stop()
