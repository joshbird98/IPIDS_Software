"""HMI panels as clients of the sequencer: the Recipe panel and the Automation bar (offscreen,
with a fake command thread)."""
import json

import pytest

from src.gui.control.sequencer_link import slot_values


class Ticket:
    def __init__(self, ok=True, reason="", data=None):
        self.ok, self.reason, self.data = ok, reason, data
        self.status = "ok" if ok else "rejected"

    def done(self):
        return True


class FakeCommands:
    def __init__(self):
        self.sent = []
        self.replies = {}           # tag suffix -> Ticket

    def send_command(self, subsystem, tag, value, origin="hmi", timeout=None, holder=None):
        self.sent.append((subsystem, tag, value, origin))
        for suffix, ticket in self.replies.items():
            if tag.endswith(suffix):
                return ticket
        return Ticket()


@pytest.fixture
def app():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel(app, tmp_path, monkeypatch):
    import json
    import src.gui.control.widgets.recipe_widget as rw
    (tmp_path / "ramp.json").write_text(json.dumps({"recipe_name": "Ramp", "steps": [
        {"step_id": 1, "type": "WAIT_TIME", "duration_sec": 1, "comment": "settle"},
        {"step_id": 2, "type": "USER_PROMPT", "prompt_text": "Check it", "comment": "check"}]}))
    monkeypatch.setattr(rw, "RECIPES_DIR", str(tmp_path))
    monkeypatch.setattr(rw, "migrate_legacy_recipes", lambda: [])
    cmds = FakeCommands()
    w = rw.RecipeExecutionWidget(cmds)
    yield w, cmds
    w.deleteLater()
    app.processEvents()


def run_telemetry(state, steps_state=("RUNNING", "IDLE"), current=1, prompt=None, run=7, **extra):
    data = {"sequencer.recipe.state": state, "sequencer.recipe.run": run, "sequencer.recipe.file": "ramp.json",
            "sequencer.recipe.name": "Ramp", "sequencer.recipe.breadcrumb": "Ramp", "sequencer.recipe.step": current,
            "sequencer.recipe.status": f"Step {current}", "sequencer.recipe.prompt": prompt,
            "sequencer.recipe.steps": [{"id": i + 1, "name": f"[{i + 1}] step", "desc": "", "type": "X", "state": s}
                                       for i, s in enumerate(steps_state)]}
    data.update({f"sequencer.recipe.{k}": v for k, v in extra.items()})
    return data


def test_slot_values():
    assert slot_values({"sequencer.recipe.state": "idle", "sequencer.mass_scan.state": "x", "other": 1},
                       "recipe") == {"state": "idle"}


def test_load_start_and_follow_a_run(panel):
    w, cmds = panel
    w.update_telemetry({"sequencer.recipe.state": "idle", "sequencer.recipe.run": None})
    assert not w.btn_start.isEnabled()                      # nothing loaded
    w.select_recipe("ramp.json")
    w.btn_load.click()
    assert w.list_steps.count() == 2 and "READY: Ramp" in w.lbl_status.text()
    w.update_telemetry({"sequencer.recipe.state": "idle", "sequencer.recipe.run": None})
    assert w.btn_start.isEnabled()
    cmds.replies["cmd_start"] = Ticket(data={"run": 7})
    w.btn_start.click()
    assert cmds.sent[-1][1:3] == ("sequencer.recipe.cmd_start", {"file": "ramp.json"})
    w.update_telemetry(run_telemetry("running"))
    assert not w.btn_start.isEnabled() and w.btn_pause.isEnabled() and w.btn_abort.isEnabled()
    assert w.btn_start.text() == "▶ START" and "RUNNING (Ramp)" in w.lbl_status.text()
    w.btn_pause.click()
    assert cmds.sent[-1][1] == "sequencer.recipe.cmd_pause"
    w.update_telemetry(run_telemetry("paused"))
    assert w.btn_pause.text() == "▶ RESUME"
    w.btn_pause.click()
    assert cmds.sent[-1][1] == "sequencer.recipe.cmd_resume"
    w.update_telemetry(run_telemetry("complete", ("DONE", "DONE"), message="Recipe completed successfully."))
    assert "Recipe completed successfully." in w.lbl_status.text() and w.btn_start.text() == "↻ RESTART"
    assert all("✓" in w.list_steps.item(i).text() for i in range(2))


def test_a_run_that_ends_before_it_is_seen_running_is_still_shown(panel):
    w, cmds = panel
    w.select_recipe("ramp.json")
    w.btn_load.click()
    w.update_telemetry({"sequencer.recipe.state": "idle", "sequencer.recipe.run": None})
    cmds.replies["cmd_start"] = Ticket(data={"run": 8})
    w.btn_start.click()
    w.update_telemetry(run_telemetry("failed", ("FAILED", "IDLE"), run=8, message="Step 1 failed: refused"))
    assert "Step 1 failed" in w.lbl_status.text() and "✗" in w.list_steps.item(0).text()


def test_prompt_banner(panel):
    w, cmds = panel
    prompt = {"seq": 3, "step": 2, "text": "Check it"}
    cmds.replies["cmd_ack"] = Ticket(ok=False, reason="not yet: x = 0.0, needs == 1.0")
    w.update_telemetry(run_telemetry("prompt", ("DONE", "WAITING_FOR_USER"), current=2, prompt=prompt))
    assert not w.prompt_frame.isHidden() and "Step 2: Check it" in w.lbl_prompt.text()
    w.btn_ack.click()
    assert cmds.sent[-1][1:3] == ("sequencer.recipe.cmd_ack", {"seq": 3})
    w.update_telemetry(run_telemetry("prompt", ("DONE", "WAITING_FOR_USER"), current=2, prompt=prompt))
    assert "not yet: x = 0.0" in w.lbl_prompt_feedback.text()
    w.update_telemetry(run_telemetry("prompt", ("DONE", "WAITING_FOR_USER"), current=2,
                                     prompt={"seq": 4, "step": 2, "text": "Check it"}))
    assert w.lbl_prompt_feedback.text() == ""               # a new prompt starts clean
    w.update_telemetry(run_telemetry("running", ("DONE", "RUNNING"), current=2))
    assert w.prompt_frame.isHidden()


def test_offline_sequencer(panel):
    w, _ = panel
    w.update_telemetry({})
    assert not w.btn_start.isEnabled() and "SEQUENCER OFFLINE" in w.lbl_status.text()


def test_back_online_the_status_line_says_what_is_loaded_again(panel):
    """It used to keep saying SEQUENCER OFFLINE after the sequencer came back."""
    w, _ = panel
    w.select_recipe("ramp.json")
    w.btn_load.click()
    w.update_telemetry({})
    assert "SEQUENCER OFFLINE" in w.lbl_status.text()
    w.update_telemetry({"sequencer.recipe.state": "idle", "sequencer.recipe.run": None})
    assert w.lbl_status.text() == "READY: Ramp (version 1.0)" and w.btn_start.isEnabled()


def test_steps_are_listed_as_sentences_with_their_notes(panel):
    w, _ = panel
    w.select_recipe("ramp.json")
    w.btn_load.click()
    assert [w.list_steps.item(i).text() for i in range(2)] == ["1. Wait 1 s", "2. Ask the operator: “Check it”"]
    assert w.list_steps.item(0).toolTip() == "Note: settle"


def test_load_checks_the_recipe_and_an_error_keeps_start_off(panel, tmp_path):
    w, cmds = panel
    (tmp_path / "bad.json").write_text(json.dumps({"recipe_name": "Bad", "steps": [
        {"step_id": 1, "type": "ACTION", "target": "ion_beam.source.extraction.sp_requested_voltage", "value": 50.0},
        {"step_id": 2, "type": "WAIT_TELEMETRY", "target": "ion_beam.source.extraction.rb_voltage", "condition": "==",
         "value": 15.0}]}))
    w.select_recipe("bad.json")
    w.btn_load.click()
    w.update_telemetry({"sequencer.recipe.state": "idle", "sequencer.recipe.run": None})
    assert not w.btn_start.isEnabled() and "The checks found errors" in w.btn_start.toolTip()
    assert "NOT READY: Bad" in w.lbl_status.text()
    text = w.lbl_check.text()
    assert "Can't be started" in text and "50 kV is outside the extraction voltage setpoint's range" in text
    assert "2 warnings (it can still run)" in text            # == on an analogue value, and no time limit
    w._start_recipe()
    assert not any(c[1].endswith("cmd_start") for c in cmds.sent)


@pytest.mark.parametrize("content, why", [
    ("{ not json", "is not valid JSON"),
    ('{"recipe_name": "x", "steps": [{"step_id": 1, "type": "WAIT_TIME", "duration_sec": {"a": 1}}]}',
     "duration_sec must be a number"),
    ('{"recipe_name": "x", "steps": [{"step_id": 1, "type": "PARALLEL", "steps": 5}]}', "must be a list"),
    ("[1, 2]", "a recipe must be a JSON object"),
])
def test_a_file_that_cant_be_run_is_reported_not_raised(panel, tmp_path, content, why):
    """A value of the wrong type used to raise out of LOAD (only OSError and RecipeError were caught)."""
    w, _ = panel
    (tmp_path / "broken.json").write_text(content)
    w.select_recipe("broken.json")
    w.btn_load.click()
    w.update_telemetry({"sequencer.recipe.state": "idle", "sequencer.recipe.run": None})
    assert "NOT LOADED: broken.json" in w.lbl_status.text() and why in w.lbl_check.text()
    assert not w.btn_start.isEnabled()


def test_a_recipe_saved_in_the_builder_is_loaded_again(panel, tmp_path):
    """The panel used to keep showing the steps it had loaded before the edit."""
    w, _ = panel
    w.select_recipe("ramp.json")
    w.btn_load.click()
    (tmp_path / "ramp.json").write_text(json.dumps({"recipe_name": "Ramp", "version": "1.1", "steps": [
        {"step_id": 1, "type": "WAIT_TIME", "duration_sec": 5}]}))
    w._recipe_saved("ramp.json")
    assert w.list_steps.count() == 1 and w.list_steps.item(0).text() == "1. Wait 5 s"
    assert w.lbl_status.text() == "READY: Ramp (version 1.1)"


def test_automation_bar_lists_holders_and_takes_control(app):
    from src.gui.control.widgets.automation_bar import AutomationBar
    cmds = FakeCommands()
    bar = AutomationBar(cmds)
    bar.update_telemetry({"ion_beam.beamline.magnet.lease_holder": None})
    assert not bar.groups and bar.idle_action.isVisible()
    held = {"ion_beam.beamline.magnet.lease_holder": "seq-mass_scan-1", "ion_beam.beamline.magnet.lease_purpose":
            "mass scan", "ion_beam.beamline.faraday.smu.lease_holder": "seq-mass_scan-1",
            "ion_beam.beamline.faraday.smu.lease_purpose": "mass scan"}
    bar.update_telemetry(held)
    assert bar.groups == {"mass scan": ["ion_beam.beamline.magnet", "ion_beam.beamline.faraday.smu"]}
    assert not bar.idle_action.isVisible()
    label, button = bar.rows["mass scan"][1:3]
    assert label.text() == "mass scan holds: magnet, smu"
    button.click()
    assert cmds.sent == []                                  # armed only
    button.click()
    assert sorted((s, t, v["op"]) for s, t, v, _o in cmds.sent) == [
        ("magnet", "ion_beam.beamline.magnet.cmd_lease", "take"),
        ("smu", "ion_beam.beamline.faraday.smu.cmd_lease", "take")]
    bar.update_telemetry({})
    assert not bar.rows and bar.idle_action.isVisible()
    bar.deleteLater()
