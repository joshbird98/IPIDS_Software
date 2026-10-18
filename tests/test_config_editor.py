"""The HMI config editor (src/gui/control/dialogs/config_editor_dialog.py): edits come back with
their types and full precision, problems are shown on load and block a save, changes are
confirmed and recorded, the previous file can be put back, and services are restarted."""
import json
import os
import shutil

import pytest

from src.core import config_store as cs
from src.core import paths


class FakeCommands:
    def __init__(self):
        self.sent = []

    def send_command(self, subsystem, tag, value, origin="hmi", timeout=None, holder=None):
        self.sent.append((subsystem, tag, value))


@pytest.fixture
def app():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def env(app, tmp_path, monkeypatch, fake_events):
    from src.gui.control.dialogs import config_editor_dialog as ced
    cfg = tmp_path / "config"
    cfg.mkdir()
    for name in cs.config_files(paths.CONFIG_DIR):
        shutil.copy(os.path.join(paths.CONFIG_DIR, name), cfg / name)
    boxes = []
    for kind in ("information", "warning", "critical"):
        monkeypatch.setattr(ced.QMessageBox, kind, staticmethod(lambda *a, k=kind: boxes.append((k, a[2]))))
    confirm = {"accept": True, "reason": "test change", "shown": []}

    def fake_exec(dlg):
        confirm["shown"].append([dlg.list.item(i).text() for i in range(dlg.list.count())])
        dlg.reason.setText(confirm["reason"])
        return 1 if confirm["accept"] else 0
    monkeypatch.setattr(ced.ConfirmChangesDialog, "exec", fake_exec)
    rebuilds = []

    def rebuild():
        rebuilds.append(1)
        return env_state["rebuild_error"]
    env_state = {"rebuild_error": None}
    cmds = FakeCommands()

    def make(**kw):
        return ced.ConfigEditorDialog(cmds, config_dir=str(cfg), events=fake_events, rebuild=rebuild, commit=False,
                                      services=lambda: ["service_plc", "service_smu"], **kw)
    yield type("Env", (), dict(cfg=cfg, boxes=boxes, confirm=confirm, rebuilds=rebuilds, state=env_state,
                               cmds=cmds, events=fake_events, make=staticmethod(make)))


def open_file(dlg, name):
    dlg.cb_files.setCurrentText(name)
    assert dlg.original is not None
    return dlg


def widget_at(dlg, *path):
    for item, p, _ in dlg._leaves:
        if p == path:
            return dlg.tree.itemWidget(item, 1)
    raise KeyError(path)


def test_untouched_file_comes_back_identical(env):
    dlg = env.make()
    for name in cs.config_files(str(env.cfg)):
        open_file(dlg, name)
        assert dlg.edited_data() == cs.read(name, str(env.cfg)), name
        assert type(dlg.edited_data()) is dict


def test_full_precision_nulls_and_empty_lists_survive(env):
    (env.cfg / "x.json").write_text(json.dumps({"a": 0.123456789012, "b": None, "c": [], "d": {}, "e": 7,
                                                "f": [1.5, 2.5]}))
    dlg = env.make()
    open_file(dlg, "x.json")
    assert widget_at(dlg, "a").text() == "0.123456789012"           # was rounded to 0.123457 on save
    assert dlg.edited_data() == {"a": 0.123456789012, "b": None, "c": [], "d": {}, "e": 7, "f": [1.5, 2.5]}
    widget_at(dlg, "f", 1).setText("3")
    widget_at(dlg, "b").setText("12.5")
    data = dlg.edited_data()
    assert data["f"] == [1.5, 3.0] and isinstance(data["f"][1], float) and data["b"] == 12.5


def test_bad_input_names_the_field(env):
    dlg = open_file(env.make(), "magnet_config.json")
    widget_at(dlg, "max_power").setText("lots")
    with pytest.raises(ValueError, match="max_power: 'lots' is not a number"):
        dlg.edited_data()
    dlg._save_and_restart()
    assert env.boxes[-1][0] == "warning" and "max_power" in env.boxes[-1][1]


def test_apply_checks_confirms_records_and_restarts(env):
    dlg = open_file(env.make(), "magnet_config.json")
    widget_at(dlg, "max_current").setText("90")
    dlg._save_and_restart()
    assert env.confirm["shown"][-1] == ["max_current: 95.0 -> 90.0"]
    saved = cs.read("magnet_config.json", str(env.cfg))
    assert saved["max_current"] == 90.0 and isinstance(saved["max_current"], float)
    assert env.rebuilds == [1]                                        # it feeds system_tags.json
    assert env.cmds.sent == [("manager", "manager_cmd", {"command": "restart", "service": "service_magnet_psu"})]
    kind, text = env.boxes[-1]
    assert kind == "information" and "Previous version kept" in text
    event = env.events.of_type("CONFIG_CHANGE")[-1]
    assert event["message"] == "Config magnet_config.json changed: max_current: 95.0 -> 90.0 (test change)"
    assert event["metadata"]["backup"] and event["metadata"]["reason"] == "test change"
    assert len(cs.backups("magnet_config.json", str(env.cfg))) == 1


def test_problems_block_the_save(env):
    dlg = open_file(env.make(), "vac_gauges_config.json")
    widget_at(dlg, "10", "relays", "1", "on_val").setText("9e-06")     # above off_val 8e-06
    before = (env.cfg / "vac_gauges_config.json").read_text()
    dlg._save_and_restart()
    assert env.boxes[-1][0] == "warning" and "must be below" in env.boxes[-1][1]
    assert (env.cfg / "vac_gauges_config.json").read_text() == before and env.cmds.sent == []


def test_cancel_and_no_change(env):
    dlg = open_file(env.make(), "smu_config.json")
    dlg._save_and_restart()
    assert env.boxes[-1] == ("information", "No values were changed.")
    widget_at(dlg, "max_v").setValue(150)
    env.confirm["accept"] = False
    dlg._save_and_restart()
    assert cs.read("smu_config.json", str(env.cfg))["max_v"] == 200 and env.cmds.sent == []


def test_failed_rebuild_rolls_back(env):
    env.state["rebuild_error"] = "rebuilding system_tags.json failed (exit 1): KeyError"
    dlg = open_file(env.make(), "smu_config.json")
    widget_at(dlg, "max_v").setValue(150)
    dlg._save_and_restart()
    kind, text = env.boxes[-1]
    assert kind == "critical" and "KeyError" in text and "put back" in text
    assert cs.read("smu_config.json", str(env.cfg))["max_v"] == 200 and env.cmds.sent == []


def test_device_rename(env):
    dlg = open_file(env.make(), "mpd_config.json")
    item = next(i for i, _, key in dlg._renamable if key == "source_einzel")
    item.setText(0, "einzel_1")
    dlg._save_and_restart()
    assert list(cs.read("mpd_config.json", str(env.cfg))["buses"][0]["devices"]) == ["einzel_1"]
    assert "renamed to 'einzel_1'" in env.confirm["shown"][-1][0]


def test_all_services_uses_the_managers_list(env):
    dlg = open_file(env.make(), "network_config.json")
    assert dlg.cb_service.currentText() == "ALL SERVICES"
    widget_at(dlg, "smu", "port").setValue(5026)
    dlg._save_and_restart()
    assert [v["service"] for _, _, v in env.cmds.sent] == ["service_plc", "service_smu"]
    assert env.rebuilds == []                                         # not a registry input


def test_unreadable_file_offers_history_only(env):
    (env.cfg / "smu_config.json").write_text("{broken")
    dlg = env.make()
    dlg.cb_files.setCurrentText("smu_config.json")
    assert dlg.original is None and not dlg.btn_save.isEnabled()
    assert not dlg.lbl_problems.isHidden() and "History" in dlg.lbl_problems.text()


def test_problems_in_the_current_file_are_shown(env):
    data = cs.read("smu_config.json", str(env.cfg))
    (env.cfg / "smu_config.json").write_text(json.dumps(dict(data, min_v=500)))
    dlg = env.make()
    dlg.cb_files.setCurrentText("smu_config.json")
    assert not dlg.lbl_problems.isHidden() and "min_v (500) must be below max_v" in dlg.lbl_problems.text()


def test_history_puts_a_previous_version_back(env, monkeypatch):
    from src.gui.control.dialogs import config_editor_dialog as ced
    dlg = open_file(env.make(), "smu_config.json")
    widget_at(dlg, "max_v").setValue(150)
    dlg._save_and_restart()
    dlg = open_file(env.make(), "smu_config.json")
    assert widget_at(dlg, "max_v").value() == 150
    monkeypatch.setattr(ced.QInputDialog, "getItem", staticmethod(lambda *a, **k: (a[3][0], True)))
    dlg._show_history()
    assert cs.read("smu_config.json", str(env.cfg))["max_v"] == 200
    assert widget_at(dlg, "max_v").value() == 200                      # reloaded
    assert env.confirm["shown"][-1] == ["max_v: 150 -> 200"]
    assert len(cs.backups("smu_config.json", str(env.cfg))) == 2       # the 150 version is kept too


def test_scrolling_past_a_number_does_not_change_it(env):
    from PyQt6.QtCore import QPoint, QPointF, Qt
    from PyQt6.QtGui import QWheelEvent
    from PyQt6.QtWidgets import QApplication
    dlg = open_file(env.make(), "smu_config.json")
    box = widget_at(dlg, "max_v")
    ev = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, 120), Qt.MouseButton.NoButton,
                     Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(box, ev)
    assert box.value() == 200 and not ev.isAccepted()      # passed on to the tree (which scrolls)


def test_history_rescues_an_unreadable_file(env, monkeypatch):
    from src.gui.control.dialogs import config_editor_dialog as ced
    dlg = open_file(env.make(), "smu_config.json")
    widget_at(dlg, "max_v").setValue(150)
    dlg._save_and_restart()                                           # keeps a backup of the good file
    (env.cfg / "smu_config.json").write_text("{broken")                 # then someone breaks it by hand
    dlg = env.make()
    dlg.cb_files.setCurrentText("smu_config.json")
    assert dlg.original is None and not dlg.btn_save.isEnabled()
    monkeypatch.setattr(ced.QInputDialog, "getItem", staticmethod(lambda *a, **k: (a[3][0], True)))
    dlg._show_history()
    assert cs.read("smu_config.json", str(env.cfg))["max_v"] == 200      # the version before the 150 save
    assert dlg.original is not None and dlg.btn_save.isEnabled()
    broken = [p for p in cs.backups("smu_config.json", str(env.cfg)) if open(p).read() == "{broken"]
    assert broken                                                      # the broken file was kept too
