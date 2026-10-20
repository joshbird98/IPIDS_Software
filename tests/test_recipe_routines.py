"""The recipe routines (src/core/routines.py) run by the sequencer, against in-process fake
services (tests/sequencer_world.py): what they refuse, how they fail, and that a pause freezes
their timers."""
import json

import pytest

from src.core.recipe_engine import RecipeError
from src.sequencer.protocol import COMPLETE, FAILED, PAUSED, PROMPT, RUNNING
from src.sequencer.recipe_job import RecipeJob
from tests.sequencer_world import World

TARGET = "ion_beam.source.target"
RELAY = "ion_beam.facilities.safety_relay_active"


class _FakeTime:
    def __init__(self, clock):
        self.time = clock


@pytest.fixture
def world(monkeypatch):
    import src.core.recipe_engine as engine
    import src.core.routines as routines
    w = World()
    monkeypatch.setattr(engine, "time", _FakeTime(w.clock))
    monkeypatch.setattr(routines, "time", _FakeTime(w.clock))
    return w


def start(world, tmp_path, routine, **params):
    path = tmp_path / "r.json"
    path.write_text(json.dumps({"recipe_name": "r", "steps": [
        {"step_id": 1, "type": "ROUTINE_CALL", "routine_name": routine, "parameters": params}]}))
    return RecipeJob(world.ctx, 3, str(path), recipes_dir=str(tmp_path))


def psu(**extra):
    params = {"cmd_enable": f"{TARGET}.cmd_enable", "stat_enabled": f"{TARGET}.stat_enabled",
              "sp_tag": f"{TARGET}.sp_requested_voltage", "rb_tag": f"{TARGET}.rb_voltage", "target_val": 4.0,
              "tolerance": 0.1, "timeout_sec": 5.0}
    params.update(extra)
    return params


def test_psu_control_switches_on_sets_and_waits_for_the_readback(world, tmp_path):
    world.set("PLC_DATA", RELAY, 1.0)
    plc = world.devices["plc"]

    def on(tag, value):
        if tag == f"{TARGET}.cmd_enable":
            world.set("PLC_DATA", f"{TARGET}.stat_enabled", 1.0 if value else 0.0)

    plc.handler = on
    job = start(world, tmp_path, "psu_control", **psu())
    world.run_until(job, lambda: job.state == COMPLETE, timeout=3)     # the world's target reads 4.0 kV
    assert [(t, v) for t, v, _, s in plc.log if not t.endswith("cmd_lease")] == [
        (f"{TARGET}.cmd_enable", 1.0), (f"{TARGET}.sp_requested_voltage", 4.0)]


def test_psu_control_without_the_readback_waits_for_the_setpoint_to_be_accepted(world, tmp_path):
    """A refused setpoint used to go unseen: the step was done before the answer came."""
    world.set("PLC_DATA", RELAY, 1.0)
    world.set("PLC_DATA", f"{TARGET}.stat_enabled", 1.0)
    world.devices["plc"].handler = lambda tag, value: "4 is outside the allowed range [0, 3]" \
        if tag.endswith("sp_requested_voltage") else None
    job = start(world, tmp_path, "psu_control", **psu(wait_for_readback=False))
    world.run_until(job, lambda: job.state in (COMPLETE, FAILED), timeout=3)
    assert job.state == FAILED and "outside the allowed range" in job.message


def test_psu_control_fails_at_once_without_the_relay(world, tmp_path):
    world.set("PLC_DATA", RELAY, 0.0)
    job = start(world, tmp_path, "psu_control", **psu())
    world.run_until(job, lambda: job.state == FAILED, timeout=1)
    assert "safety relay is not active" in job.message
    assert all(t.endswith("cmd_lease") for t, *_ in world.devices["plc"].log)


def test_a_pause_freezes_a_routines_timer(world, tmp_path):
    """Pausing used to leave the routines' timers running, so a resumed step could time out at once."""
    world.set("PLC_DATA", RELAY, 1.0)
    world.set("PLC_DATA", f"{TARGET}.stat_enabled", 1.0)
    job = start(world, tmp_path, "psu_control", **psu(target_val=6.0))      # the readback stays at 4 kV
    world.step(job, n=100)                         # 2 s of the 5 s
    assert job.handle("pause", None)[0] and job.state == PAUSED
    world.step(job, n=500)                         # 10 s paused
    assert job.handle("resume", None)[0]
    world.step(job, n=100)                         # 4 s running in all
    assert job.state == RUNNING
    world.run_until(job, lambda: job.state == FAILED, timeout=2)
    assert "did not reach 6.0 +/- 0.1 within 5 s" in job.message


def test_hv_conditioning_refuses_a_supply_that_is_off(world, tmp_path):
    """It used to 'ramp' a supply that was off, setpoint after setpoint, to the end."""
    world.set("PLC_DATA", f"{TARGET}.stat_enabled", 0.0)
    world.set("PLC_DATA", "ion_beam.source.vacuum_gauge_1.rb_pressure", 1e-7)
    job = start(world, tmp_path, "hv_conditioning", cmd_tag=f"{TARGET}.sp_requested_voltage",
                rb_tag=f"{TARGET}.rb_voltage", enabled_tag=f"{TARGET}.stat_enabled", max_val=8.0)
    world.run_until(job, lambda: job.state == FAILED, timeout=2)
    assert "the supply is off" in job.message
    assert not any(t.endswith("sp_requested_voltage") for t, *_ in world.devices["plc"].log)


def test_hv_conditioning_fails_if_the_readback_does_not_follow(world, tmp_path):
    world.set("PLC_DATA", f"{TARGET}.stat_enabled", 1.0)
    world.set("PLC_DATA", "ion_beam.source.vacuum_gauge_1.rb_pressure", 1e-7)
    job = start(world, tmp_path, "hv_conditioning", cmd_tag=f"{TARGET}.sp_requested_voltage",
                rb_tag=f"{TARGET}.rb_voltage", enabled_tag=f"{TARGET}.stat_enabled", max_val=8.0, step_size=1.0,
                hold_time_sec=1.0)                 # the world's target stays at 4 kV
    world.run_until(job, lambda: job.state == FAILED, timeout=5)
    assert "the readback (4.0) didn't follow the setpoint (6) within 1" in job.message     # 5 is within a step


def test_safety_relay_routine_asks_for_the_vacuum_first(world, tmp_path):
    world.set("PLC_DATA", RELAY, 0.0)
    world.set("PLC_DATA", "ion_beam.source.chamber.stat_gv_open", 0.0)
    world.set("PLC_DATA", "ion_beam.source.vacuum_gauge_1.rb_pressure", 3e-6)
    job = start(world, tmp_path, "safety_relay_init")
    world.run_until(job, lambda: job.state == PROMPT, timeout=1)
    assert "The source pressure is 3.00e-06 mbar" in job.prompt["text"]
    world.set("PLC_DATA", "ion_beam.source.vacuum_gauge_1.rb_pressure", 2e-7)
    assert job.handle("ack", {"seq": job.prompt["seq"]})[0]
    world.run_until(job, lambda: job.state == PROMPT and "attempt an automatic Open" in job.prompt["text"], timeout=1)


@pytest.mark.parametrize("routine, params, why", [
    ("psu_control", {"stat_enabled": f"{TARGET}.stat_enabled"}, r"cmd_enable \(On/off command\) is needed"),
    ("psu_control", psu(target_val="high"), "target_val must be a number"),
    ("psu_control", psu(wait_for_readback=2), "wait_for_readback must be true or false"),
    ("hv_conditioning", {"cmd_tag": f"{TARGET}.sp_requested_voltage", "rb_tag": f"{TARGET}.rb_voltage"},
     r"max_val \(Up to\) is needed"),
    ("hv_conditioning", {"cmd_tag": "a.b", "rb_tag": "a.c", "max_val": 5, "pressure_max": 1e-6,
                         "pressure_recover": 2e-6}, "pressure_recover .* must not be above pressure_max"),
    ("safety_relay_init", {"timeout_sec": 0}, "timeout_sec can't be below 1"),
    ("teleport", {}, "unknown routine 'teleport'"),
])
def test_routine_parameters_are_checked_when_the_recipe_loads(world, tmp_path, routine, params, why):
    with pytest.raises(RecipeError, match=why):
        start(world, tmp_path, routine, **params)
