"""The recipe builder (src/gui/control/dialogs/recipe_builder/) and recipe saving
(src/core/recipe_store.py): blocks, forms made for each kind of step, numbering, moving and undo,
the checks, stop conditions, templates, and saving that never writes over another file or over a
change made meanwhile."""
import json
import os

import pytest

from src.core.recipe_engine import Recipe, create_step
from src.core.recipe_store import read_recipe, recipe_filename, save_recipe

EXT = "ion_beam.source.extraction"
MAG_SP = "ion_beam.beamline.magnet.sp_requested_current"
CUP = "ion_beam.beamline.diagnostics.cmd_insert_fc"


@pytest.fixture
def app():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def folder(tmp_path):
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    (recipes / "sub_startup.json").write_text(json.dumps({"recipe_name": "Standard Startup", "steps": [
        {"step_id": 1, "type": "WAIT_TIME", "duration_sec": 1}]}))
    return recipes


@pytest.fixture
def make(app, folder, monkeypatch):
    """make(load=None) -> a builder on the test folder; its messages are recorded, not shown."""
    from src.gui.control.dialogs.recipe_builder import dialog as D

    def tell(self, title, text, kind="information"):
        self.__dict__.setdefault("messages", []).append((kind, title, text))

    def ask_unsaved(self):
        answers = self.__dict__.setdefault("answers", [])
        return answers.pop(0) if answers else "cancel"

    # (patched on the class: a message while the window is being made would otherwise wait for a click)
    monkeypatch.setattr(D.RecipeBuilderDialog, "_tell", tell)
    monkeypatch.setattr(D.RecipeBuilderDialog, "_ask_unsaved", ask_unsaved)
    monkeypatch.setattr(D.RecipeBuilderDialog, "_ask_save_details",
                        lambda self, where, version, suggested, n: (suggested, "test"))
    made = []

    def build(load=None):
        dlg = D.RecipeBuilderDialog(None, str(folder), load_filepath=str(folder / load) if load else None)
        dlg.__dict__.setdefault("messages", [])
        dlg.__dict__.setdefault("answers", [])
        made.append(dlg)
        return dlg

    yield build
    for dlg in made:
        try:
            dlg._closing_ok = True
            dlg.close()
        except RuntimeError:
            pass
    app.processEvents()


def find(dlg, cls, name):
    from PyQt6.QtCore import Qt
    return dlg.editor.widget().findChild(cls, name, Qt.FindChildOption.FindChildrenRecursively)


def add(dlg, key, select=None):
    if select is not None:
        dlg.outline.select_path(select, quiet=False)
    dlg.add_block(key)
    return dlg._get(dlg._selected)


# ============================================================================ blocks and forms
def test_every_block_adds_a_step_with_its_own_form(make):
    from PyQt6.QtWidgets import QLabel
    from src.gui.control.dialogs.recipe_builder.blocks import BLOCKS
    dlg = make()
    for block in BLOCKS:
        dlg.outline.select_path(None, quiet=False)
        dlg._selected = None
        step = add(dlg, block.key)
        assert step["type"] == block.step["type"]
        heading = dlg.editor.widget().findChild(QLabel, "StepHeading").text()
        assert heading.endswith(block.title), heading
    assert [s["step_id"] for s in dlg.data["steps"]] == list(range(1, len(BLOCKS) + 1))


def test_typing_part_of_a_name_completes_the_tag_and_the_value_follows_its_type(make):
    from PyQt6.QtWidgets import QComboBox, QLineEdit
    dlg = make()
    step = add(dlg, "set")
    tag = find(dlg, QLineEdit, "TagEdit")
    completer = tag.completer()
    completer.setCompletionPrefix("extraction volt")
    found = [completer.completionModel().index(i, 0).data() for i in range(completer.completionCount())]
    assert any("extraction voltage setpoint" in f for f in found)
    tag.setText(f"{EXT}.sp_requested_voltage")
    from src.gui.control.dialogs.recipe_builder.fields import NumberField
    number = find(dlg, NumberField, "ValueNumber")
    assert number.isVisibleTo(dlg) and number.unit.text() == "kV"
    number.edit.setText("15")
    assert step == {"step_id": 1, "type": "ACTION", "target": f"{EXT}.sp_requested_voltage", "value": 15.0}
    number.edit.setText("15 kV")                        # kept as typed: the check says it isn't a number
    assert step["value"] == "15 kV"
    dlg._check()
    assert any("needs a number, not '15 kV'" in p.message for p in dlg.problems)
    tag.setText(CUP)                                    # a switch: On/Off becomes In/Out
    choice = dlg.editor.widget().findChild(QComboBox, "ValueChoice")
    items = [choice.itemText(i) for i in range(choice.count())]
    assert items == ["In (insert)", "Out (retract)", "'15 kV' (not on or off)"]     # the odd value stays visible
    choice.setCurrentIndex(choice.findData(1))
    assert step["value"] is True and describe(dlg) == "Insert the Faraday cup"


def describe(dlg):
    from src.core.recipe_text import describe_step
    return describe_step(dlg._get(dlg._selected), dlg.catalog)


def test_a_prompt_message_stays_text(make):
    """The old builder turned anything typed into a number if it could ('1' -> 1, 'true' -> True)."""
    from PyQt6.QtWidgets import QPlainTextEdit
    dlg = make()
    step = add(dlg, "ask")
    find(dlg, QPlainTextEdit, "PromptText").setPlainText("1")
    assert step["prompt_text"] == "1"


def test_set_and_check_defaults_to_checking_the_value_it_sets(make):
    """A new step used to have an empty verify tag, so it always timed out."""
    from src.gui.control.dialogs.recipe_builder.fields import TagField
    dlg = make()
    step = add(dlg, "set_check")
    find(dlg, TagField, "SetTag").edit.setText(MAG_SP)
    step["value"] = 5.0
    engine = create_step(step)
    assert engine.verify_target == MAG_SP and engine.verify_value == 5.0
    dlg._check()
    assert any("checks the command itself" in p.message for p in dlg.problems)


def test_choosing_a_supply_fills_in_its_tags_units_and_range(make):
    from PyQt6.QtWidgets import QComboBox
    dlg = make()
    step = add(dlg, "supply")
    supply = dlg.editor.widget().findChild(QComboBox, "Supply")
    supply.setCurrentIndex(supply.findText("Extraction"))
    p = step["parameters"]
    assert (p["cmd_enable"], p["stat_enabled"], p["sp_tag"], p["rb_tag"]) == (
        f"{EXT}.cmd_enable", f"{EXT}.stat_enabled", f"{EXT}.sp_requested_voltage", f"{EXT}.rb_voltage")
    from src.gui.control.dialogs.recipe_builder.fields import NumberField
    target = find(dlg, NumberField, "param_target_val")
    assert target.unit.text() == "kV"
    target.edit.setText("25")
    dlg._check()
    assert any("the value to set: 25 kV is outside the extraction voltage setpoint's range" in p.message
               for p in dlg.problems)
    supply.setCurrentIndex(supply.findText("Thermionic (ioniser)"))
    assert step["parameters"]["wait_for_readback"] is False      # its current follows the filament


def test_an_unknown_routine_or_step_type_is_shown_as_such(make, folder):
    """An unknown routine used to be shown (and saved) as a mass scan."""
    from PyQt6.QtWidgets import QLabel
    (folder / "odd.json").write_text(json.dumps({"recipe_name": "Odd", "steps": [
        {"step_id": 1, "type": "ROUTINE_CALL", "routine_name": "teleport", "parameters": {"x": 1}},
        {"step_id": 2, "type": "BEAM_ME_UP"}]}))
    dlg = make("odd.json")
    texts = " ".join(l.text() for l in dlg.editor.widget().findChildren(QLabel))
    assert "no routine 'teleport'" in texts
    assert dlg._get((0,))["routine_name"] == "teleport"
    assert {p.message for p in dlg.problems} >= {"unknown routine 'teleport' (one of hv_conditioning, mass_scan, "
                                                 "psu_control, safety_relay_init)"}


def test_sub_recipe_choices_are_the_other_recipes_by_name(make, folder):
    from PyQt6.QtWidgets import QComboBox
    (folder / "main.json").write_text(json.dumps({"recipe_name": "Main", "steps": []}))
    dlg = make("main.json")
    add(dlg, "sub")
    combo = dlg.editor.widget().findChild(QComboBox, "RecipeFile")
    items = [combo.itemText(i) for i in range(combo.count())]
    assert "Standard Startup (sub_startup.json)" in items and not any("main.json" in i for i in items)


# ============================================================================ the list of steps
def test_numbering_moving_and_parallel_steps(make):
    dlg = make()
    add(dlg, "wait_time")
    add(dlg, "set")
    add(dlg, "parallel", select=(0,))                    # after the selected step
    add(dlg, "wait_for")                                 # the PARALLEL selected: it goes inside
    add(dlg, "ask")                                      # after the sub-step, still inside
    add(dlg, "sub")                                      # can't go inside: after the PARALLEL
    steps = dlg.data["steps"]
    assert [s["type"] for s in steps] == ["WAIT_TIME", "PARALLEL", "SUB_RECIPE", "ACTION"]
    assert [s["step_id"] for s in steps[1]["steps"]] == [201, 202]
    dlg.move_to((2,), (1,), 0)                           # a SUB_RECIPE dragged into the PARALLEL: refused
    assert len(steps[1]["steps"]) == 2 and "can't go inside" in dlg.lbl_status.text()
    dlg.move_to((0,), (1,), 2)                           # WAIT_TIME dragged into it
    steps = dlg.data["steps"]
    assert [s["type"] for s in steps] == ["PARALLEL", "SUB_RECIPE", "ACTION"]
    assert [s["step_id"] for s in steps[0]["steps"]] == [101, 102, 103] and dlg._selected == (0, 2)
    dlg.move_to((0, 0), (), 3)                           # out again, to the end
    assert dlg.data["steps"][-1]["type"] == "WAIT_TELEMETRY" and dlg.data["steps"][-1]["step_id"] == 4
    dlg.outline.select_path((1,), quiet=False)
    dlg.move_step(1)
    assert [s["type"] for s in dlg.data["steps"]] == ["PARALLEL", "ACTION", "SUB_RECIPE", "WAIT_TELEMETRY"]
    dlg.duplicate_step()
    assert [s["step_id"] for s in dlg.data["steps"]] == [1, 2, 3, 4, 5] and dlg._selected == (3,)
    dlg.delete_step()
    assert len(dlg.data["steps"]) == 4 and dlg._selected == (3,)


def test_undo_and_redo(make):
    from PyQt6.QtWidgets import QLineEdit
    dlg = make()
    add(dlg, "wait_time")
    add(dlg, "ask")
    dlg.le_name.setText("Ramp")
    dlg._record()
    dlg.undo()
    assert dlg.data["recipe_name"] == "New recipe" and dlg.le_name.text() == "New recipe"
    dlg.undo()
    assert [s["type"] for s in dlg.data["steps"]] == ["WAIT_TIME"]
    dlg.redo()
    dlg.redo()
    assert [s["type"] for s in dlg.data["steps"]] == ["WAIT_TIME", "USER_PROMPT"] and dlg.data["recipe_name"] == "Ramp"
    assert isinstance(dlg.le_name, QLineEdit)


def test_old_files_with_repeated_numbers_are_renumbered(make, folder):
    (folder / "old.json").write_text(json.dumps({"recipe_name": "Old", "version": 2, "steps": [
        {"step_id": 1, "type": "WAIT_TIME", "duration_sec": 1}, {"step_id": 1, "type": "WAIT_TIME", "duration_sec": 2}]}))
    dlg = make("old.json")
    assert [s["step_id"] for s in dlg.data["steps"]] == [1, 2] and dlg.is_dirty()
    assert "renumbered" in dlg.lbl_status.text() and dlg.le_version.text() == "2"


def test_a_file_that_cant_be_read_is_reported(make, folder):
    (folder / "bad.json").write_text("[1, 2]")
    dlg = make("bad.json")
    assert dlg.messages[-1][0] == "critical" and dlg.data["steps"] == [] and dlg.filename is None


# ============================================================================ stop conditions
def test_stop_conditions_keep_a_fault_the_config_doesnt_have(make, folder):
    """The old fault policy window swapped such a fault for another one, unseen."""
    retired = {"type": "BIT_FAULT", "registry_tag": "ion_beam.faults.word_9", "bit_str": "0.1",
               "name": "Retired_Fault", "action": "ABORT"}
    (folder / "p.json").write_text(json.dumps({"recipe_name": "P", "fault_policy": [retired], "steps": []}))
    dlg = make("p.json")
    from PyQt6.QtWidgets import QComboBox
    combo = dlg.policy.host.findChild(QComboBox, "Fault")
    assert "Retired_Fault" in combo.currentText() and "not a fault in fault_config.json" in combo.currentText()
    assert dlg.data["fault_policy"][0] == retired
    dlg.policy._add_recommended()
    assert [e["name"] for e in dlg.data["fault_policy"]] == ["Retired_Fault", "Safety_Relay_Tripped"]
    dlg.policy._add_recommended()                            # not twice
    assert len(dlg.data["fault_policy"]) == 2
    dlg.policy._add_condition()
    assert dlg.data["fault_policy"][-1]["type"] == "CONDITION"


def test_relay_outputs_without_the_relay_trip_stop_are_flagged(make):
    dlg = make()
    step = add(dlg, "set")
    step.update({"target": f"{EXT}.cmd_enable", "value": True})
    dlg._check()
    assert any(p.path == ("policy",) and "Safety_Relay_Tripped" in p.message for p in dlg.problems)


# ============================================================================ saving
def test_edit_then_save_writes_the_file_it_came_from(make, folder):
    """Saving after a rename used to write <new name>.json, a second copy, and leave the file alone."""
    (folder / "ramp.json").write_text(json.dumps({"recipe_name": "Ramp", "version": "1.1", "steps": [
        {"step_id": 1, "type": "WAIT_TIME", "duration_sec": 1}]}, indent=2))
    dlg = make("ramp.json")
    saved = []
    dlg.saved.connect(saved.append)
    dlg.le_name.setText("Ramp to 15 kV")
    assert dlg.save()
    data, _ = read_recipe(str(folder / "ramp.json"))
    assert data["recipe_name"] == "Ramp to 15 kV" and data["version"] == "1.2"     # changed: one version up
    assert sorted(os.listdir(folder)) == ["ramp.json", "sub_startup.json"] and saved == ["ramp.json"]
    assert not dlg.is_dirty() and "ramp.json, version 1.2" in dlg.lbl_status.text()
    backups = os.listdir(folder.parent / "backups" / "recipes")
    assert len(backups) == 1 and backups[0].startswith("ramp.")
    Recipe(str(folder / "ramp.json"))                        # the sequencer loads it


def test_a_new_recipe_never_overwrites_another(make, folder):
    dlg = make()
    dlg.le_name.setText("Sub startup")
    add(dlg, "wait_time")
    assert dlg.save()
    assert dlg.filename == "sub_startup_2.json"
    assert read_recipe(str(folder / "sub_startup.json"))[0]["recipe_name"] == "Standard Startup"


def test_a_file_changed_since_it_was_opened_is_not_overwritten(make, folder):
    (folder / "ramp.json").write_text(json.dumps({"recipe_name": "Ramp", "steps": []}))
    dlg = make("ramp.json")
    add(dlg, "wait_time")
    (folder / "ramp.json").write_text(json.dumps({"recipe_name": "Ramp (edited elsewhere)", "steps": []}))
    assert not dlg.save()
    assert "has changed since it was opened here" in dlg.messages[-1][2]
    assert read_recipe(str(folder / "ramp.json"))[0]["recipe_name"] == "Ramp (edited elsewhere)"


def test_a_recipe_with_errors_is_not_saved(make, folder):
    dlg = make()
    dlg.le_name.setText("Broken")
    step = add(dlg, "set")
    step.update({"target": "ion_beam.nope.cmd_x", "value": 1})
    assert not dlg.save()
    assert dlg.messages[-1][1] == "Not saved" and "isn't a tag the machine knows" in dlg.messages[-1][2]
    assert not (folder / "broken.json").exists()


def test_save_as_new_keeps_the_original(make, folder):
    (folder / "ramp.json").write_text(json.dumps({"recipe_name": "Ramp", "steps": []}))
    dlg = make("ramp.json")
    add(dlg, "wait_time")
    assert dlg.save(as_new=True)
    assert dlg.filename == "ramp_2.json" and read_recipe(str(folder / "ramp.json"))[0]["steps"] == []


def test_store_rules_without_the_window(folder):
    data = {"recipe_name": "Ramp 1/2", "version": "1", "steps": [{"step_id": 1, "type": "WAIT_TIME", "duration_sec": 1}]}
    r = save_recipe(data, str(folder))
    assert r.saved and r.filename == "ramp_1_2.json" and r.backup is None
    r = save_recipe(data, str(folder), filename="ramp_1_2.json")
    assert not r.saved and "there is a recipe ramp_1_2.json already" in r.refused
    r = save_recipe(data, str(folder), filename="../x.json")
    assert not r.saved and "isn't a recipe file name" in r.refused
    assert recipe_filename("Ramp 1/2") == "ramp_1_2.json" and recipe_filename("../../etc/passwd") == "etc_passwd.json"
    assert recipe_filename("  ") == "recipe.json"


# ============================================================================ the window
def test_closing_with_unsaved_changes_asks_first(make, app):
    """Escape used to throw the whole recipe away without a word."""
    dlg = make()
    dlg.show()
    add(dlg, "wait_time")
    dlg.answers = ["cancel"]
    dlg.reject()                                             # Escape
    assert dlg.isVisible()
    dlg.answers = ["discard"]
    dlg.close()
    app.processEvents()
    from PyQt6 import sip
    assert sip.isdeleted(dlg) or not dlg.isVisible()


def test_the_window_isnt_modal_and_is_deleted_when_closed(make, app):
    from PyQt6.QtCore import Qt
    dlg = make()
    assert not dlg.isModal() and dlg.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)


def test_every_template_loads_with_no_problems(make):
    from src.gui.control.dialogs.recipe_builder import templates
    found = templates()
    assert len(found) >= 6
    for path, name, _description in found:
        dlg = make()
        assert dlg.new_from_template(path)
        assert dlg.problems == [], (name, [p.text() for p in dlg.problems])
        assert dlg.is_dirty() and dlg.filename is None


# ============================================================================ found in review
def test_a_stop_condition_added_after_an_undo_is_kept(make):
    """After an undo to a state without stop conditions, the tab edited a list that wasn't the recipe's."""
    dlg = make()
    add(dlg, "wait_time")
    dlg.undo()
    dlg.policy._add_recommended()
    assert [e["name"] for e in dlg.data["fault_policy"]] == ["Safety_Relay_Tripped"]
    dlg._check()
    assert dlg.tabs.tabText(1) == "Stop conditions (1)"


def test_showing_a_step_doesnt_change_the_recipe(make, folder):
    (folder / "plain.json").write_text(json.dumps({"recipe_name": "Plain", "steps": [
        {"step_id": 1, "type": "USER_PROMPT", "prompt_text": "Check it"},
        {"step_id": 2, "type": "ROUTINE_CALL", "routine_name": "safety_relay_init"}]}))
    dlg = make("plain.json")
    for path in ((0,), (1,), (0,)):
        dlg.outline.select_path(path, quiet=False)
    assert not dlg.is_dirty() and not dlg.windowTitle().endswith("*")
    assert "verify_conditions" not in dlg.data["steps"][0] and "parameters" not in dlg.data["steps"][1]


def test_a_malformed_file_opens_with_its_errors_shown(make, folder):
    (folder / "odd.json").write_text(json.dumps({"recipe_name": "Odd", "steps": [
        {"step_id": 1, "type": "ROUTINE_CALL", "routine_name": "psu_control", "parameters": ["x"]},
        {"step_id": 2, "type": "PARALLEL", "steps": 5}]}))
    dlg = make("odd.json")
    assert dlg.filename == "odd.json" and len([p for p in dlg.problems if p.level == "error"]) == 2


def test_redo_from_the_keyboard_and_after_typing(make, app):
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    dlg = make()
    dlg.show()
    app.processEvents()
    add(dlg, "wait_time")
    add(dlg, "ask")
    dlg.outline.setFocus()
    QTest.keyClick(dlg.outline, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert len(dlg.data["steps"]) == 1
    QTest.keyClick(dlg.outline, Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
    assert len(dlg.data["steps"]) == 2
    dlg.undo()
    dlg.le_name.setText("Typed after the undo")            # (not recorded yet when Redo comes)
    dlg.redo()
    assert dlg.data["recipe_name"] == "Typed after the undo" and len(dlg.data["steps"]) == 1


def test_a_file_with_a_step_type_that_isnt_text_opens_with_its_errors(make, folder):
    (folder / "odd.json").write_text(json.dumps({"recipe_name": "Odd", "version": "1.0", "fault_policy": ["x"],
                                                 "steps": [{"type": ["WAIT_TIME"]}, {"type": "WAIT_TIME",
                                                                                     "duration_sec": 2}]}))
    dlg = make("odd.json")
    assert dlg.outline.topLevelItemCount() == 2
    assert any("unknown step type" in p.text() for p in dlg.problems)
    dlg.outline.select_path((0,), quiet=False)
    dlg._show_selected((0,))
    from PyQt6.QtWidgets import QLabel
    assert any("doesn't know the step type" in w.text() for w in dlg.editor.findChildren(QLabel))
