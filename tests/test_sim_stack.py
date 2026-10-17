"""End-to-end: the real service manager starts every service against simulated devices
(own ports and data folder, so this can run next to the live system).

Covers: all services come up; acknowledged commands through the real services;
safety relay -> Spellman power; rejections with reasons; the command audit trail in
events.db; readback tolerance events; restart back-off after a crash; operator
stop/start; the logger's crash journal; and that stopping the manager stops
everything.
"""
import sqlite3
import time

import orjson
import psutil
import pytest

from src.core import commands as C
from tests.sim_stack import start_stack, stop_stack

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    s = start_stack(tmp_path_factory.mktemp("simstack"))
    try:
        yield s
    finally:
        alive = stop_stack(s)
        assert not alive, f"services left running after the manager stopped: {[p.pid for p in alive]}"


def test_relay_then_spellman_einzel(stack):
    r = stack.cmd("plc", "ion_beam.system.cmd_enable_safety", True)
    assert r["status"] == C.OK, r
    stack.wait(lambda l: l["PLC_DATA"]["ion_beam.facilities.safety_relay_active"] == 1.0, 10, "safety relay")
    # Spellman only accepts commands once it has seen the relay energised
    stack.wait(lambda l: l["SPELLMAN_DATA"]["ion_beam.source.einzel.rb_voltage"] is not None, 10, "einzel powered")
    assert stack.cmd("spellman", "ion_beam.source.einzel.sp_requested_voltage", 6.0)["status"] == C.OK
    # current limit from mpd_config.json; with it at 0 the (simulated) supply can't raise its voltage
    assert stack.cmd("spellman", "ion_beam.source.einzel.sp_requested_current", 50.0)["status"] == C.OK
    assert stack.cmd("spellman", "ion_beam.source.einzel.cmd_enable", True)["status"] == C.OK
    stack.wait(lambda l: abs(l["SPELLMAN_DATA"]["ion_beam.source.einzel.rb_voltage"] - 6.0) < 0.1, 10,
               "einzel at 6 kV")


def _fault_events(stack, key):
    db = sqlite3.connect(stack.data_dir / "events.db")
    rows = db.execute("SELECT metadata FROM events WHERE event_type='FAULT' ORDER BY id").fetchall()
    db.close()
    return [m["is_active"] for m in (orjson.loads(r[0]) for r in rows) if m.get("fault_key") == key]


def test_einzel_arc_latches_a_plc_fault_until_reset(stack):
    """Spellman service -> stat_arc_exceeded -> PLC service -> PLC fault word -> FAULT event."""
    import src.sim as sim
    sim.FAULTS_FILE = str(stack.data_dir / "sim_faults.txt")   # this test process -> the stack's data dir
    sp = "SPELLMAN_DATA"
    stack.wait(lambda l: abs(l[sp]["ion_beam.source.einzel.rb_voltage"] - 6.0) < 0.1, 10, "einzel at 6 kV")
    assert stack.latest[sp]["ion_beam.source.einzel.stat_arc_exceeded"] == 0.0
    sim.set_faults({"spellman_arc"})
    try:
        stack.wait(lambda l: l[sp]["ion_beam.source.einzel.stat_arc_exceeded"] == 1.0, 10, "arc latched")
    finally:
        sim.set_faults(set())
    stack.wait(lambda l: abs(l[sp]["ion_beam.source.einzel.rb_voltage"] - 6.0) < 0.2, 10, "output back")
    stack.wait(lambda l: _fault_events(stack, "Unit1_Arc_Exceeded")[-1:] == [True], 10, "PLC arc fault raised")
    assert stack.latest[sp]["ion_beam.source.einzel.stat_arc_exceeded"] == 1.0    # still latched
    assert stack.cmd("spellman", "ion_beam.system.cmd_fault_reset", True)["status"] == C.OK
    stack.wait(lambda l: l[sp]["ion_beam.source.einzel.stat_arc_exceeded"] == 0.0, 5, "latch cleared")
    stack.wait(lambda l: _fault_events(stack, "Unit1_Arc_Exceeded")[-1:] == [False], 10, "PLC arc fault cleared")


def test_rejections_carry_reasons(stack):
    r = stack.cmd("spellman", "ion_beam.source.einzel.sp_requested_voltage", 99.0)
    assert r["status"] == C.REJECTED and "range" in r["reason"]
    r = stack.cmd("plc", "ion_beam.source.chamber.stat_vac_ok_for_gv", True)
    assert r["status"] == C.REJECTED
    r = stack.cmd("magnet", "ion_beam.beamline.magnet.sp_requested_voltage", 3.0)
    assert r["status"] == C.REJECTED and "automatically" in r["reason"]


def test_audit_trail_in_events_db(stack):
    time.sleep(1.0)
    db = sqlite3.connect(stack.data_dir / "events.db")
    rows = db.execute("SELECT service, severity, message, metadata FROM events WHERE event_type='COMMAND'").fetchall()
    db.close()
    assert len(rows) >= 5
    metas = [orjson.loads(m) for _, _, _, m in rows]
    assert any(m["status"] == "rejected" and m["origin"] == "test" for m in metas)
    assert any(r[0] == "service_spellman_mpd" and r[1] == "INFO" for r in rows)


def test_tolerance_event_when_magnet_sags(stack):
    from src.sim import set_faults
    import src.sim as sim
    sim.FAULTS_FILE = str(stack.data_dir / "sim_faults.txt")   # this test process -> the stack's data dir
    assert stack.cmd("magnet", "ion_beam.beamline.magnet.sp_requested_current", 20.0)["status"] == C.OK
    assert stack.cmd("magnet", "ion_beam.beamline.magnet.cmd_enable", True)["status"] == C.OK
    stack.wait(lambda l: abs(l["MAGNET_DATA"]["ion_beam.beamline.magnet.rb_current"] - 20.0) < 0.1, 15,
               "magnet at 20 A")
    flag = "monitor.tolerance.ion_beam.beamline.magnet.rb_current"
    assert stack.latest["MONITOR_DATA"][flag] == 0.0
    set_faults({"magnet_sag"})
    try:
        stack.wait(lambda l: l["MONITOR_DATA"][flag] == 1.0, 20, "magnet tolerance flag")
    finally:
        set_faults(set())
    stack.wait(lambda l: l["MONITOR_DATA"][flag] == 0.0, 20, "flag clears")
    db = sqlite3.connect(stack.data_dir / "events.db")
    rows = db.execute("SELECT severity, metadata FROM events WHERE event_type='TOLERANCE' ORDER BY id").fetchall()
    db.close()
    sev = [s for s, m in rows if orjson.loads(m)["readback_tag"] == "ion_beam.beamline.magnet.rb_current"]
    assert sev[:2] == ["WARNING", "INFO"]


def test_crash_is_restarted_with_backoff(stack):
    pid = None
    for p in psutil.Process(stack.proc.pid).children():
        if any(a.endswith("service_smu.py") for a in p.cmdline()):
            pid = p.pid
    assert pid
    psutil.Process(pid).kill()
    stack.wait(lambda l: l["MANAGER"]["manager.services"]["service_smu"] == "BACKOFF", 5, "SMU in back-off")
    info = stack.latest["MANAGER"]["manager.restart_info"]["service_smu"]
    assert info["failures"] == 1 and "exited" in info["reason"]
    stack.wait(lambda l: l["MANAGER"]["manager.services"]["service_smu"] == "ONLINE", 30, "SMU back")


def test_operator_stop_and_start(stack):
    r = stack.cmd("manager", "manager_cmd", {"command": "stop", "service": "service_steerer_magnet_psu"})
    assert r["status"] == C.OK
    stack.wait(lambda l: l["MANAGER"]["manager.services"]["service_steerer_magnet_psu"] == "STOPPED", 5, "stopped")
    time.sleep(3)
    stack.pump()
    assert stack.services()["service_steerer_magnet_psu"] == "STOPPED"     # no automatic restart
    r = stack.cmd("steerer_magnet", "ion_beam.beamline.steerer_magnet.cmd_enable", True)
    assert r["status"] == C.UNREACHABLE
    assert stack.cmd("manager", "manager_cmd", {"command": "start", "service": "service_steerer_magnet_psu"})[
               "status"] == C.OK
    stack.wait(lambda l: l["MANAGER"]["manager.services"]["service_steerer_magnet_psu"] == "ONLINE", 30, "back")
    r = stack.cmd("manager", "manager_cmd", {"command": "explode", "service": "service_smu"})
    assert r["status"] == C.REJECTED


def test_logger_journal_and_logs(stack):
    assert (stack.data_dir / "parquet_logs" / "logger_journal.bin").exists()
    logs = stack.data_dir / "logs"
    for name in ("service_manager", "service_plc", "service_spellman_mpd", "service_logger", "service_monitor"):
        assert (logs / f"{name}.log").exists(), name
