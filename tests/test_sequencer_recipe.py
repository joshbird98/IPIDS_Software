"""Recipes run by the sequencer (src/sequencer/recipe_job.py), against in-process fake services."""
import json

import pytest

from src.core.recipe_engine import RecipeError
from src.sequencer.protocol import COMPLETE, FAILED, PAUSED, PROMPT, RUNNING, STOPPED, key
from src.sequencer.recipe_job import RecipeJob, resolve_recipe
from tests.sequencer_world import MAGNET, World

SP = f"{MAGNET}.sp_requested_current"
FAULT_WORD = "ion_beam.faults.word_2_source"


def write(folder, name, steps, **extra):
    path = folder / name
    path.write_text(json.dumps({"recipe_name": name.split(".")[0], "steps": steps, **extra}))
    return path


def action(step_id, target, value):
    return {"step_id": step_id, "type": "ACTION", "target": target, "value": value}


class _FakeTime:
    """Stands in for the time module inside the step classes, so their timers follow the
    simulated clock."""

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


def start(world, tmp_path, steps, name="r.json", **extra):
    path = write(tmp_path, name, steps, **extra)
    return RecipeJob(world.ctx, 7, str(path), recipes_dir=str(tmp_path))


def test_actions_run_under_the_recipes_lease_which_is_released_at_the_end(world, tmp_path):
    job = start(world, tmp_path, [action(1, SP, 20.0), action(2, SP, 21.0)])
    world.run_until(job, lambda: job.state == COMPLETE)
    assert job.message == "Recipe completed successfully."
    magnet = world.devices["magnet"]
    assert magnet.values[SP] == 21.0
    assert all(holder == job.holder for tag, _, holder, _ in magnet.log)
    world.step(job)
    assert magnet.leases.active(MAGNET) is None and magnet.log[-1][0].endswith("cmd_lease")
    t = job.telemetry()
    assert t[key("recipe", "state")] == COMPLETE and t[key("recipe", "holder")] is None
    assert [s["state"] for s in t[key("recipe", "steps")]] == ["DONE", "DONE"]


def test_a_refused_step_fails_the_recipe_with_the_services_reason(world, tmp_path):
    world.devices["magnet"].handler = lambda tag, value: "95 A is the limit" if value > 95 else None
    job = start(world, tmp_path, [action(1, SP, 120.0), action(2, SP, 1.0)])
    world.run_until(job, lambda: job.state == FAILED)
    assert job.message.startswith("Step 1 failed") and "95 A is the limit" in job.message
    assert SP not in world.devices["magnet"].values


def test_prompt_needs_its_conditions_and_the_current_number(world, tmp_path):
    job = start(world, tmp_path, [
        {"step_id": 1, "type": "USER_PROMPT", "prompt_text": "Insert the cup",
         "verify_conditions": [{"target": "ion_beam.beamline.diagnostics.stat_fc_inserted", "condition": "==",
                                "value": 1.0}]},
        action(2, SP, 20.0)])
    world.run_until(job, lambda: job.state == PROMPT)
    prompt = job.telemetry()[key("recipe", "prompt")]
    assert prompt["step"] == 1 and prompt["text"] == "Insert the cup"
    ok, reason, _ = job.handle("ack", {"seq": prompt["seq"]})
    assert not ok and "stat_fc_inserted = no live value" in reason
    world.set("PLC_DATA", "ion_beam.beamline.diagnostics.stat_fc_inserted", 0.0)
    world.step(job)
    ok, reason, _ = job.handle("ack", {"seq": prompt["seq"]})
    assert not ok and "= 0.0, needs == 1.0" in reason
    world.set("PLC_DATA", "ion_beam.beamline.diagnostics.stat_fc_inserted", 1.0)
    world.step(job)
    assert not job.handle("ack", {"seq": prompt["seq"] + 1})[0]        # not the prompt on screen
    assert job.handle("ack", {"seq": prompt["seq"]})[0]
    assert not job.handle("ack", {"seq": prompt["seq"]})[0]            # already answered
    world.run_until(job, lambda: job.state == COMPLETE)


def test_pause_freezes_the_wait_and_resume_carries_on(world, tmp_path):
    job = start(world, tmp_path, [{"step_id": 1, "type": "WAIT_TIME", "duration_sec": 1.0}, action(2, SP, 3.0)])
    world.step(job, n=10)                         # 0.2 s into the wait
    assert job.handle("pause", None)[0] and job.state == PAUSED
    world.step(job, n=100)                        # 2 s paused
    assert job.state == PAUSED and SP not in world.devices["magnet"].values
    assert job.handle("resume", None)[0]
    world.step(job, n=30)                         # 0.8 s run in total: still waiting
    assert job.state == RUNNING and job.step.step_id == 1
    world.run_until(job, lambda: job.state == COMPLETE, timeout=1)


def test_operator_take_pauses_and_resume_repeats_the_step_under_a_new_claim(world, tmp_path):
    job = start(world, tmp_path, [action(1, SP, 20.0), {"step_id": 2, "type": "WAIT_TIME", "duration_sec": 0.3},
                                  action(3, SP, 22.0)])
    world.run_until(job, lambda: job.step is not None and job.step.step_id == 2)
    world.take("magnet", MAGNET)
    world.run_until(job, lambda: job.state == PAUSED, timeout=3)
    assert "operator took control of the magnet" in job.status
    world.step(job, n=10)
    assert job.state == PAUSED and MAGNET not in job.keeper.held
    assert job.handle("resume", None)[0]
    world.run_until(job, lambda: job.state == COMPLETE, timeout=5)
    assert world.devices["magnet"].values[SP] == 22.0


def test_a_take_while_a_command_is_on_its_way_pauses_instead_of_failing(world, tmp_path):
    job = start(world, tmp_path, [action(1, SP, 20.0), action(2, SP, 21.0), action(3, SP, 22.0)])
    world.step(job)                               # step 1 sent with its claim
    world.take("magnet", MAGNET)                  # operator takes before step 2 gets there
    world.run_until(job, lambda: job.state in (PAUSED, FAILED), timeout=3)
    assert job.state == PAUSED
    assert job.handle("resume", None)[0]
    world.run_until(job, lambda: job.state == COMPLETE, timeout=5)


def test_stop_leaves_outputs_and_releases(world, tmp_path):
    job = start(world, tmp_path, [action(1, SP, 20.0), {"step_id": 2, "type": "WAIT_TIME", "duration_sec": 30}])
    world.run_until(job, lambda: job.step is not None and job.step.step_id == 2)
    assert job.handle("stop", None)[0]
    assert job.state == STOPPED and "Stopped by operator at step 2 - outputs left as they were." == job.message
    world.step(job)
    assert world.devices["magnet"].values[SP] == 20.0 and world.devices["magnet"].leases.active(MAGNET) is None
    assert job.handle("stop", None) == (True, "not running", None)


def test_fault_policy(world, tmp_path):
    policy = [{"type": "BIT_FAULT", "registry_tag": FAULT_WORD, "bit_str": "0.3", "name": "Cs overtemp"}]
    world.set("PLC_DATA", FAULT_WORD, 0)
    job = start(world, tmp_path, [{"step_id": 1, "type": "WAIT_TIME", "duration_sec": 30}], fault_policy=policy)
    world.step(job, n=5)
    assert job.state == RUNNING
    world.set("PLC_DATA", FAULT_WORD, 1 << 27)          # byte 0 bit 3 of the DWORD
    world.step(job)
    assert job.state == FAILED and "Cs overtemp" in job.message


def test_fault_policy_unverifiable_stops_the_recipe(world, tmp_path):
    policy = [{"type": "BIT_FAULT", "registry_tag": FAULT_WORD, "bit_str": "0.3", "name": "Cs overtemp"}]
    job = start(world, tmp_path, [{"step_id": 1, "type": "WAIT_TIME", "duration_sec": 30}], fault_policy=policy)
    world.run_until(job, lambda: job.state == FAILED, timeout=5)
    assert "fault status unknown" in job.message and FAULT_WORD in job.message


def test_sub_recipes(world, tmp_path):
    write(tmp_path, "sub.json", [action(1, SP, 30.0)])
    job = start(world, tmp_path, [action(1, SP, 20.0), {"step_id": 2, "type": "SUB_RECIPE", "target_file": "sub.json"},
                                  action(3, SP, 25.0)])
    seen = set()
    world.run_until(job, lambda: seen.add(job.breadcrumb()) or job.state == COMPLETE)
    assert "r > sub" in seen and world.devices["magnet"].values[SP] == 25.0
    assert 30.0 in [v for t, v, _, s in world.devices["magnet"].log if t == SP]


def test_sub_recipe_outside_the_folder_and_endless_recursion_fail(world, tmp_path):
    job = start(world, tmp_path, [{"step_id": 1, "type": "SUB_RECIPE", "target_file": "../evil.json"}])
    world.run_until(job, lambda: job.state == FAILED)
    assert "not in the recipes folder" in job.message
    job = start(world, tmp_path, [{"step_id": 1, "type": "SUB_RECIPE", "target_file": "loop.json"}], name="loop.json")
    world.run_until(job, lambda: job.state == FAILED)
    assert "nested more than" in job.message


def test_prompt_inside_parallel_reaches_the_operator(world, tmp_path):
    job = start(world, tmp_path, [{"step_id": 1, "type": "PARALLEL", "steps": [
        {"step_id": 11, "type": "USER_PROMPT", "prompt_text": "Check the shutter"}, action(12, SP, 5.0)]}])
    world.run_until(job, lambda: job.state == PROMPT)
    assert job.prompt["text"] == "Check the shutter"
    assert job.handle("ack", {"seq": job.prompt["seq"]})[0]
    world.run_until(job, lambda: job.state == COMPLETE)


def test_bad_recipes_are_refused_at_start(world, tmp_path):
    write(tmp_path, "bad.json", [{"step_id": 1, "type": "TELEPORT"}])
    with pytest.raises(RecipeError, match="unknown step type 'TELEPORT'"):
        RecipeJob(world.ctx, 1, str(tmp_path / "bad.json"), recipes_dir=str(tmp_path))
    with pytest.raises(ValueError, match="not in the recipes folder"):
        resolve_recipe("../x.json", str(tmp_path))
    with pytest.raises(ValueError, match="no recipe"):
        resolve_recipe("missing.json", str(tmp_path))


def test_every_shipped_recipe_loads():
    import glob
    import os
    from src.core import paths
    from src.core.recipe_engine import Recipe
    files = glob.glob(os.path.join(paths.CONFIG_DIR, "recipes", "*.json"))
    assert files
    for path in files:
        Recipe(path)
