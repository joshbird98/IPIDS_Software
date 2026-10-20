"""The precision mass scan as a sequencer job and as a recipe routine (src/sequencer/mass_scan.py)."""
import csv

import pytest

from src.core import commands as C
from src.sequencer.mass_scan import (NPLC_TAG, SP_TAG, MassScanJob, ScanConfig, export_csv, strongest_peak)
from src.sequencer.protocol import COMPLETE, FAILED, STOPPED, key
from tests.sequencer_world import MAGNET, SMU, World, beam_at

LIMITS = (0.0, 95.0)


def config(**kw):
    value = {"start_a": 24.0, "stop_a": 26.0, "step_a": 0.5, "samples": 3, "sample_interval": 0.05, "nplc": 1.0,
             "settle_time": 0.05}
    value.update(kw)
    return ScanConfig.from_value(value, LIMITS)


def nplc_commands(world):
    return [v for t, v, _, s in world.devices["smu"].log if t == NPLC_TAG and s == C.OK]


def test_scan_measures_every_point_and_puts_the_smu_back():
    world = World()
    job = MassScanJob(world.ctx, 3, config(nplc=10.0))
    world.run_until(job, lambda: job.state == COMPLETE)
    sps = [p[0] for p in job.points.current]
    assert sps == [24.0, 24.5, 25.0, 25.5, 26.0]
    for sp, mag, beam, t_kv, e_kv in job.points.current:
        assert mag == pytest.approx(sp) and beam == pytest.approx(beam_at(sp)) and (t_kv, e_kv) == (4.0, 10.0)
    assert nplc_commands(world) == [10.0, 1.0]           # integration time set, then restored
    world.step(job)
    assert world.devices["magnet"].leases.active(MAGNET) is None and world.devices["smu"].leases.active(SMU) is None
    t = job.telemetry()
    assert t[key("mass_scan", "points")] == 5 and t[key("mass_scan", "passes_done")] == 1
    assert [row[0] for row in t[key("mass_scan", "tail")]] == [0, 1, 2, 3, 4]
    assert job.message == "complete (5 points)"


def test_the_scan_holds_the_magnet_against_the_operator_until_taken():
    world = World()
    job = MassScanJob(world.ctx, 3, config(stop_a=30.0))
    world.run_until(job, lambda: len(job.points.current) >= 2)
    lease = world.devices["magnet"].leases.active(MAGNET)
    assert lease.holder == job.holder and lease.purpose == "mass scan"
    assert world.devices["magnet"].leases.refusal(MAGNET, None).startswith("magnet is controlled by mass scan")
    world.take("magnet", MAGNET)
    n = len(world.devices["magnet"].log)
    world.run_until(job, lambda: job.state == STOPPED, timeout=5)
    assert "control was taken by the operator" in job.message
    assert nplc_commands(world)[-1] == 1.0                # the SMU (still held) is put back
    after = [e for e in world.devices["magnet"].log[n:] if e[0] == SP_TAG]
    assert all(status == C.REJECTED for *_, status in after)   # at most one in flight, and refused


def test_loop_until_stopped():
    world = World()
    job = MassScanJob(world.ctx, 3, config(step_a=1.0, loop=True))
    world.run_until(job, lambda: job.scan.passes_done >= 2 and job.points.pass_no == 3)
    ok, _, previous = job.handle("fetch", {"pass": 2})
    assert ok and [p[0] for p in previous] == [24.0, 25.0, 26.0]     # the finished pass is kept
    assert job.handle("fetch", {"pass": 1})[1] == "pass 1 is not kept any more"
    assert job.handle("stop", None)[0]
    world.run_until(job, lambda: job.state == STOPPED, timeout=10)
    assert job.message == "Stopped by operator" and nplc_commands(world)[-1] == 1.0


def test_refused_setpoint_fails_the_scan_and_still_restores_the_smu():
    world = World()
    world.devices["magnet"].handler = lambda tag, value: "interlock" if tag == SP_TAG and value > 24.6 else None
    job = MassScanJob(world.ctx, 3, config())
    world.run_until(job, lambda: job.state == FAILED)
    assert "magnet refused 25.000 A: interlock" in job.message
    assert nplc_commands(world)[-1] == 1.0


def test_dead_faraday_cup_telemetry_fails_the_scan():
    world = World()
    job = MassScanJob(world.ctx, 3, config(stop_a=30.0))
    world.run_until(job, lambda: len(job.points.current) >= 1)
    world.silent.add("SMU_DATA")
    world.run_until(job, lambda: job.state == FAILED, timeout=20)
    assert "not live" in job.message or "stopped updating" in job.message


def test_fetch_gives_the_whole_pass():
    world = World()
    job = MassScanJob(world.ctx, 3, config())
    world.run_until(job, lambda: job.state == COMPLETE)
    ok, _, data = job.handle("fetch", {"run": 3, "pass": 1})
    assert ok and len(data) == 5 and data[2][0] == 25.0
    assert not job.handle("fetch", {"pass": 9})[0]


@pytest.mark.parametrize("change, why", [
    ({"stop_a": 120.0}, "Hard cap: stop_a 120 A is above the 95 A cap"), ({"stop_a": -1.0}, "outside the magnet's range"),
    ({"step_a": 0.0}, "more than 0 A"),
    ({"stop_a": 24.0}, "the same"), ({"nplc": 500}, "NPLC"), ({"samples": 0}, "at least one"),
    ({"samples": 2.5}, "whole number"), ({"bogus": 1}, "unknown"), ({"step_a": 1e-6}, "bigger step"),
])
def test_bad_settings_are_refused(change, why):
    with pytest.raises(ValueError, match=why):
        config(**change)


def test_strongest_peak_and_export(tmp_path):
    sps = [20 + 0.1 * i for i in range(101)]
    points = [(sp, sp, beam_at(sp), 4.0, 10.0) for sp in sps]
    assert strongest_peak(points) == pytest.approx(25.0, abs=0.06)
    assert strongest_peak(points[:2]) is None
    assert strongest_peak([(sp, sp, 1e-9, 0, 0) for sp in sps]) is None       # flat
    path = export_csv(points[:3], str(tmp_path / "scans" / "MassScan"))
    rows = list(csv.reader(open(path)))
    assert rows[0][1] == "Requested_Magnet_A" and len(rows) == 4 and float(rows[1][1]) == 20.0


CUP_IN = "ion_beam.beamline.diagnostics.stat_fc_inserted"


def _routine_world(monkeypatch):
    import src.core.recipe_engine as engine
    import src.core.routines as routines

    class FakeTime:
        def __init__(self, clock):
            self.time = clock

    world = World()
    monkeypatch.setattr(engine, "time", FakeTime(world.clock))
    monkeypatch.setattr(routines, "time", FakeTime(world.clock))
    monkeypatch.setattr(routines, "_magnet_limits", lambda: LIMITS)
    return world


def _scan_recipe(world, tmp_path, **params):
    import json
    from src.sequencer.recipe_job import RecipeJob
    settings = {"start_a": 23.0, "stop_a": 27.0, "step_a": 0.25, "samples": 2, "settle_time": 0.0}
    settings.update(params)
    (tmp_path / "tune.json").write_text(json.dumps({"recipe_name": "tune", "steps": [
        {"step_id": 1, "type": "ROUTINE_CALL", "routine_name": "mass_scan", "parameters": settings}]}))
    return RecipeJob(world.ctx, 1, str(tmp_path / "tune.json"), recipes_dir=str(tmp_path))


def test_mass_scan_routine_in_a_recipe_scans_and_sets_the_magnet_on_the_peak(tmp_path, monkeypatch):
    world = _routine_world(monkeypatch)
    world.set("MAGNET_DATA", f"{MAGNET}.stat_enabled", 1.0)
    world.set("PLC_DATA", CUP_IN, 1.0)
    job = _scan_recipe(world, tmp_path)
    world.run_until(job, lambda: job.state in (COMPLETE, FAILED), timeout=120)
    assert job.state == COMPLETE, job.message
    assert world.devices["magnet"].values[SP_TAG] == pytest.approx(25.0)
    assert nplc_commands(world) == [1.0, 1.0]


@pytest.mark.parametrize("magnet, cup, why", [
    (0.0, 1.0, "the analyser magnet is off"),
    (1.0, 0.0, "the Faraday cup isn't in"),
    (None, 1.0, "the analyser magnet is off (or its state isn't known)"),
])
def test_mass_scan_routine_needs_the_magnet_on_and_the_cup_in(tmp_path, monkeypatch, magnet, cup, why):
    """It used to scan noise for minutes, then set the magnet on a 'peak' in it."""
    world = _routine_world(monkeypatch)
    if magnet is not None:
        world.set("MAGNET_DATA", f"{MAGNET}.stat_enabled", magnet)
    world.set("PLC_DATA", CUP_IN, cup)
    job = _scan_recipe(world, tmp_path)
    world.run_until(job, lambda: job.state == FAILED, timeout=2)
    assert why in job.message
    assert SP_TAG not in world.devices["magnet"].values


def test_mass_scan_routine_saves_each_scan_as_csv_when_asked(tmp_path, monkeypatch):
    world = _routine_world(monkeypatch)
    world.set("MAGNET_DATA", f"{MAGNET}.stat_enabled", 1.0)
    world.set("PLC_DATA", CUP_IN, 1.0)
    job = _scan_recipe(world, tmp_path, start_a=24.0, stop_a=25.0, step_a=0.5, export=True,
                       export_path=str(tmp_path / "scans" / "Tune"))
    world.run_until(job, lambda: job.state in (COMPLETE, FAILED), timeout=120)
    assert job.state == COMPLETE, job.message
    files = list((tmp_path / "scans").glob("Tune*.csv"))
    assert len(files) == 1 and len(list(csv.reader(open(files[0])))) == 4      # header and 3 points


@pytest.mark.parametrize("params, why", [
    ({"loop": True}, "loop isn't allowed in a recipe"),
    ({"stop_a": 23.0}, "start and stop currents are the same"),
    ({"step_a": 0.0}, "step_a can't be below"),
    ({"samples": 2.5}, "samples must be a whole number"),
    ({"colour": "red"}, "unknown setting"),
])
def test_mass_scan_routine_settings_are_checked_when_the_recipe_loads(tmp_path, monkeypatch, params, why):
    from src.core.recipe_engine import RecipeError
    world = _routine_world(monkeypatch)
    with pytest.raises(RecipeError, match=why):
        _scan_recipe(world, tmp_path, **params)
