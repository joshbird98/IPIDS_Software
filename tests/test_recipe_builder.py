"""Recipe Builder (src/gui/control/dialogs/recipe_builder_dialog.py and its parts): every step
type gets a properties form, steps stay numbered 1..n through deletes and drags, USER_PROMPT verify
conditions can be added and removed, what it saves is what the sequencer loads, and what the
sequencer would refuse is not saved."""
import json

import pytest

from src.core.recipe_engine import Recipe, RecipeError

STEP_TYPES = ["ACTION", "WAIT_TIME", "WAIT_TELEMETRY", "USER_PROMPT", "SUB_RECIPE", "ROUTINE_CALL",
              "VERIFIED_ACTION", "PARALLEL"]


class OfflineAssistant:
    available = False

    def __init__(self, config_dir):
        pass


@pytest.fixture
def app():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def builder(app, tmp_path, monkeypatch):
    from src.gui.control.dialogs import recipe_builder_dialog as rbd
    monkeypatch.setattr(rbd, "RecipeAIAssistant", OfflineAssistant)       # no API key, no network
    boxes = []
    for kind in ("information", "warning", "critical"):
        monkeypatch.setattr(rbd.QMessageBox, kind, staticmethod(lambda *a, k=kind: boxes.append((k, a[2]))))
    (tmp_path / "sub_startup.json").write_text("{}")
    dlg = rbd.RecipeBuilderDialog(config_dir=str(tmp_path))
    dlg.boxes = boxes
    yield dlg
    dlg.deleteLater()
    app.processEvents()


def add(dlg, step_type):
    from PyQt6.QtWidgets import QListWidgetItem
    from src.gui.control.dialogs.recipe_builder_defaults import STEP_ICONS
    dlg._add_step_from_toolbox(QListWidgetItem(f"{STEP_ICONS[step_type]} {step_type}"))


def select(dlg, row):
    dlg.list_sequence.setCurrentRow(row)
    dlg._on_step_selected()


def form_labels(dlg):
    from PyQt6.QtWidgets import QFormLayout, QLabel
    form = dlg.prop_form
    labels = []
    for r in range(form.rowCount()):
        item = form.itemAt(r, QFormLayout.ItemRole.LabelRole)
        if item is not None and isinstance(item.widget(), QLabel):
            labels.append(item.widget().text())
    return labels


def buttons(dlg, text):
    from PyQt6.QtWidgets import QPushButton
    return [b for b in dlg.prop_container.findChildren(QPushButton) if text in b.text()]


def test_file_names_stay_in_the_recipes_folder():
    from src.gui.control.dialogs.recipe_builder_dialog import recipe_filename
    assert recipe_filename("Ramp 1/2") == "ramp_1_2.json"
    assert recipe_filename("../../etc/passwd") == "etc_passwd.json"
    assert recipe_filename("  ") == "recipe.json"
    assert recipe_filename("Auto-Tune v2") == "auto-tune_v2.json"


def test_every_step_type_gets_a_form(builder):
    for st in STEP_TYPES:
        add(builder, st)
    assert [s["step_id"] for s in builder.current_steps] == list(range(1, len(STEP_TYPES) + 1))
    expected = {"ACTION": "Target Tag:", "WAIT_TIME": "Duration (sec):", "WAIT_TELEMETRY": "Condition:",
                "USER_PROMPT": "Popup Text:", "SUB_RECIPE": "Target File:", "ROUTINE_CALL": "Routine:",
                "VERIFIED_ACTION": "Verify Tag:"}
    for row, st in enumerate(STEP_TYPES):
        select(builder, row)
        labels = form_labels(builder)
        assert "Comment/Label:" in labels, st
        if st in expected:
            assert expected[st] in labels, (st, labels)


def test_verify_conditions_can_be_added_and_removed(builder):
    add(builder, "USER_PROMPT")
    select(builder, 0)
    buttons(builder, "Add Verify Condition")[0].click()       # used to crash (missing arguments)
    buttons(builder, "Add Verify Condition")[0].click()
    conds = builder.current_steps[0]["verify_conditions"]
    assert len(conds) == 2 and "Condition 2:" in form_labels(builder)
    conds[0]["target"] = "a"
    conds[1]["target"] = "b"
    buttons(builder, "❌")[0].click()                          # used to crash (_del_verify_cond missing)
    assert [c["target"] for c in builder.current_steps[0]["verify_conditions"]] == ["b"]
    assert "Condition 2:" not in form_labels(builder)


def test_parallel_sub_steps(builder):
    from PyQt6.QtWidgets import QComboBox
    add(builder, "ACTION")
    add(builder, "PARALLEL")
    select(builder, 1)
    kinds = [c for c in builder.prop_container.findChildren(QComboBox) if c.findText("ROUTINE_CALL") >= 0][0]
    add_btn = buttons(builder, "Add Sub-Step")[0]
    for i in range(kinds.count()):
        kinds.setCurrentIndex(i)
        add_btn.click()
    subs = builder.current_steps[1]["steps"]
    assert [s["type"] for s in subs] == [kinds.itemText(i) for i in range(kinds.count())]
    assert [s["step_id"] for s in subs] == [201, 202, 203, 204, 205]
    assert "SUB_RECIPE" not in [kinds.itemText(i) for i in range(kinds.count())]   # not allowed in PARALLEL


def test_numbering_survives_delete_add_and_drag(builder):
    for st in ("ACTION", "WAIT_TIME", "WAIT_TELEMETRY"):
        add(builder, st)
    first, second, third = builder.current_steps
    select(builder, 0)
    builder._remove_selected_step()
    add(builder, "USER_PROMPT")                                # used to reuse number 3
    fourth = builder.current_steps[-1]
    assert [s["step_id"] for s in builder.current_steps] == [1, 2, 3]
    # drag the last row to the top: the list widget reorders its items, then order_changed fires
    lw = builder.list_sequence
    lw.insertItem(0, lw.takeItem(2))
    builder._sync_list_order()
    assert [id(s) for s in builder.current_steps] == [id(fourth), id(second), id(third)]   # none lost or doubled
    assert [s["step_id"] for s in builder.current_steps] == [1, 2, 3]
    assert lw.item(0).text().startswith("[1] 🙋")


def test_saved_recipe_loads_in_the_sequencer(builder, tmp_path):
    add(builder, "ACTION")
    add(builder, "WAIT_TIME")
    add(builder, "PARALLEL")
    builder.current_steps[0].update({"target": "ion_beam.beamline.magnet.sp_requested_current", "value": 5.0})
    builder.le_recipe_name.setText("Ramp 1/2")
    builder._save_recipe()
    assert builder.boxes[-1][0] == "information"
    path = tmp_path / "ramp_1_2.json"
    recipe = Recipe(str(path))
    assert recipe.name == "Ramp 1/2" and [s.step_id for s in recipe.steps] == [1, 2, 3]
    assert not list(tmp_path.glob("*.tmp"))


def test_what_the_sequencer_refuses_is_not_saved(builder, tmp_path):
    add(builder, "ACTION")
    builder.current_steps.append({"step_id": 2, "type": "BOGUS", "comment": "?"})
    builder.le_recipe_name.setText("Broken")
    builder._save_recipe()
    kind, text = builder.boxes[-1]
    assert kind == "warning" and "BOGUS" in text
    assert not (tmp_path / "broken.json").exists()


def test_old_files_with_repeated_numbers_are_renumbered_on_load(builder, tmp_path):
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"recipe_name": "Old", "version": 2, "steps": [
        {"step_id": 1, "type": "WAIT_TIME", "duration_sec": 1, "comment": "a"},
        {"step_id": 1, "type": "WAIT_TIME", "duration_sec": 2, "comment": "b"}]}))
    with pytest.raises(RecipeError, match="used more than once"):
        Recipe(str(path))                                      # the sequencer refuses it...
    builder._load_existing_recipe(str(path))                   # ...the builder fixes it
    assert [s["step_id"] for s in builder.current_steps] == [1, 2]
    assert builder.le_recipe_version.text() == "2"


def test_load_errors_are_reported_not_raised(builder, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2]")
    builder._load_existing_recipe(str(bad))
    assert builder.boxes[-1][0] == "critical" and builder.current_steps == []


def test_ai_worker_always_answers():
    from src.gui.control.dialogs.recipe_builder_dialog import AIWorker

    class Broken:
        def __init__(self, result=None, exc=None):
            self.result, self.exc = result, exc

        def generate(self, prompt):
            if self.exc:
                raise self.exc
            return self.result

    for assistant, words in ((Broken(exc=ConnectionError("offline")), "offline"),
                             (Broken(result=None), "no usable answer"),
                             (Broken(result=["x"]), "no usable answer")):
        got = []
        w = AIWorker(assistant, "make a recipe", {})
        w.sig_finished.connect(got.append)
        w.run()                                                # synchronously: same code as the thread
        assert got and got[0]["status"] == "error" and words in got[0]["message"]


def test_ai_recipe_is_renumbered_and_checked(builder):
    builder._on_ai_finished({"status": "success", "recipe": {"recipe_name": "AI", "version": 1.1, "steps": [
        {"step_id": 5, "type": "WAIT_TIME", "duration_sec": 1, "comment": "w"},
        {"step_id": 5, "type": "TELEPORT", "comment": "?"}, "junk"]}})
    assert [s["step_id"] for s in builder.current_steps] == [1, 2]
    assert builder.le_recipe_version.text() == "1.1"
    assert "TELEPORT" in builder.txt_ai_history.toPlainText()
    builder._on_ai_finished({"status": "success", "recipe": "nonsense"})
    assert builder.current_steps == []


def test_fault_policy_dialog_keeps_unknown_faults_visible(app, tmp_path):
    from src.gui.control.dialogs.fault_policy_dialog import FaultPolicyDialog
    policy = [{"type": "BIT_FAULT", "registry_tag": "ion_beam.faults.word_9", "bit_str": "0.1",
               "name": "Retired_Fault", "action": "ABORT"}]
    d = FaultPolicyDialog(policy)
    from PyQt6.QtWidgets import QComboBox
    combo = d.container.findChildren(QComboBox)[0]
    assert "Retired_Fault" in combo.currentText() and "ion_beam.faults.word_9 bit 0.1" in combo.currentText()
    assert policy[0]["name"] == "Retired_Fault"                # not silently swapped for another fault
    d._add_fault()
    assert len(policy) == 2 and policy[1]["registry_tag"]       # a new trip starts on a real fault
    assert FaultPolicyDialog._load_fault_config(str(tmp_path / "missing.json")) == []
    d.deleteLater()
