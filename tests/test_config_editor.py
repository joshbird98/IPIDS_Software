"""The HMI config editor (src/gui/control/dialogs/config_editor_dialog.py): edits come back with
their types and full precision, problems are shown on load and block a save, changes are
confirmed (large ones twice) and recorded, a file changed on disk meanwhile isn't overwritten,
the previous file can be put back, and the right things are restarted."""
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
    confirm = {"accept": True, "reason": "test change", "shown": [], "large": [], "tick": True}

    def fake_exec(dlg):
        confirm["shown"].append([dlg.list.item(i).text() for i in range(dlg.list.count())])
        if dlg.large is not None:
            confirm["large"].append(" | ".join(w.text() for w in dlg.findChildren(ced.QLabel)))
            if not confirm["tick"]:
                return 0                       # OK stays disabled until the box is ticked
        dlg.reason.setText(confirm["reason"])
        return 1 if confirm["accept"] else 0
    monkeypatch.setattr(ced.ConfirmChangesDialog, "exec", fake_exec)
    rebuilds = []

    def rebuild():
        rebuilds.append(1)
        return env_state["rebuild_error"]
    env_state = {"rebuild_error": None,
                 "services": {"service_plc": "ONLINE", "service_smu": "BACKOFF", "service_monitor": "STOPPED"}}
    cmds = FakeCommands()

    def make(**kw):
        return ced.ConfigEditorDialog(cmds, config_dir=str(cfg), events=fake_events, rebuild=rebuild, commit=False,
                                      services=lambda: env_state["services"], **kw)
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


def restarted(env):
    return [v["service"] for _, _, v in env.cmds.sent]


def test_untouched_file_comes_back_identical(env):
    dlg = env.make()
    for name in cs.config_files(str(env.cfg)):
        open_file(dlg, name)
        assert dlg.edited_data() == cs.read(name, str(env.cfg)), name


def test_full_precision_nulls_empty_lists_and_number_types(env):
    (env.cfg / "x.json").write_text(json.dumps({"a": 0.123456789012, "b": None, "c": [], "d": {}, "e": 7,
                                                "f": [1.5, 2.5]}))
    dlg = env.make()
    open_file(dlg, "x.json")
    assert widget_at(dlg, "a").text() == "0.123456789012"           # was rounded to 0.123457 on save
    assert dlg.edited_data() == {"a": 0.123456789012, "b": None, "c": [], "d": {}, "e": 7, "f": [1.5, 2.5]}
    widget_at(dlg, "f", 1).setText("3")
    widget_at(dlg, "b").setText("12.5")
    widget_at(dlg, "e").setText("7.5")                                # a whole number may become a fraction
    data = dlg.edited_data()
    assert data["f"] == [1.5, 3.0] and isinstance(data["f"][1], float) and data["b"] == 12.5 and data["e"] == 7.5
    widget_at(dlg, "e").setText("8")
    assert dlg.edited_data()["e"] == 8 and isinstance(dlg.edited_data()["e"], int)


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
    assert env.confirm["shown"][-1] == ["max_current: 95.0 -> 90.0"] and env.confirm["large"] == []
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


def test_large_changes_need_a_tick(env):
    dlg = open_file(env.make(), "magnet_config.json")
    widget_at(dlg, "short_circuit_threshold").setText("0.005")       # /10: allowed, but is it meant?
    env.confirm["tick"] = False
    dlg._save_and_restart()
    assert "short_circuit_threshold: 0.05 -&gt; 0.005 (/10)" in env.confirm["large"][-1]
    assert cs.read("magnet_config.json", str(env.cfg))["short_circuit_threshold"] == 0.05      # not applied
    env.confirm["tick"] = True
    dlg._save_and_restart()
    assert cs.read("magnet_config.json", str(env.cfg))["short_circuit_threshold"] == 0.005


def test_the_tick_box_gates_ok(app):
    from PyQt6.QtWidgets import QDialogButtonBox
    from src.gui.control.dialogs.config_editor_dialog import ConfirmChangesDialog
    d = ConfirmChangesDialog("t", "i", ["a: 1 -> 10"], large=["a: 1 -> 10 (x10)"])
    ok = d.buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert not ok.isEnabled()
    d.large.setChecked(True)
    assert ok.isEnabled()
    plain = ConfirmChangesDialog("t", "i", ["a: 1 -> 2"])
    assert plain.large is None and plain.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()


def test_problems_block_the_save(env):
    dlg = open_file(env.make(), "vac_gauges_config.json")
    widget_at(dlg, "10", "relays", "1", "on_val").setText("9e-06")     # above off_val 8e-06
    before = (env.cfg / "vac_gauges_config.json").read_text()
    dlg._save_and_restart()
    assert env.boxes[-1][0] == "warning" and "must be below" in env.boxes[-1][1]
    assert (env.cfg / "vac_gauges_config.json").read_text() == before and env.cmds.sent == []


def test_a_file_changed_on_disk_meanwhile_is_not_overwritten(env):
    dlg = open_file(env.make(), "smu_config.json")
    data = cs.read("smu_config.json", str(env.cfg))
    (env.cfg / "smu_config.json").write_text(json.dumps(dict(data, max_v=180)))    # a hand edit / git pull
    widget_at(dlg, "min_v").setText("-150")
    dlg._save_and_restart()
    kind, text = env.boxes[-1]
    assert kind == "critical" and "changed on disk" in text
    assert cs.read("smu_config.json", str(env.cfg)) == dict(data, max_v=180) and env.cmds.sent == []


def test_cancel_and_no_change(env):
    dlg = open_file(env.make(), "smu_config.json")
    dlg._save_and_restart()
    assert env.boxes[-1] == ("information", "No values were changed.")
    widget_at(dlg, "max_v").setText("150")
    env.confirm["accept"] = False
    dlg._save_and_restart()
    assert cs.read("smu_config.json", str(env.cfg))["max_v"] == 200 and env.cmds.sent == []


def test_failed_rebuild_rolls_back(env):
    env.state["rebuild_error"] = "rebuilding system_tags.json failed (exit 1): KeyError"
    before = (env.cfg / "smu_config.json").read_bytes()
    dlg = open_file(env.make(), "smu_config.json")
    widget_at(dlg, "max_v").setText("150")
    dlg._save_and_restart()
    kind, text = env.boxes[-1]
    assert kind == "critical" and "KeyError" in text and "put back" in text
    assert (env.cfg / "smu_config.json").read_bytes() == before and env.cmds.sent == []


def test_keys_are_not_editable(env):
    """The services look supplies, gauges and ports up by name: a renamed Spellman supply lost its
    telemetry and commands, and the PLC saw it as healthy for ever."""
    from PyQt6.QtCore import Qt
    dlg = open_file(env.make(), "mpd_config.json")
    items = [dlg.tree.topLevelItem(i) for i in range(dlg.tree.topLevelItemCount())]
    seen = 0
    while items:
        item = items.pop()
        seen += 1
        assert not item.flags() & Qt.ItemFlag.ItemIsEditable, item.text(0)
        items += [item.child(i) for i in range(item.childCount())]
    assert seen > 10


def reword_a_fault(dlg):
    udt = next(iter(dlg.original))
    bit = next(iter(dlg.original[udt]["bits"]))
    widget_at(dlg, udt, "bits", bit, "description").setText("reworded")


def test_fault_config_restarts_the_services_that_read_it(env):
    """Only the PLC and alarm services use fault_config.json (and the HMI): the others aren't
    restarted for it any more."""
    env.state["services"]["service_alarms"] = "ONLINE"
    dlg = open_file(env.make(), "fault_config.json")
    assert dlg.cb_service.currentText() == "service_plc + service_alarms"
    reword_a_fault(dlg)
    dlg._save_and_restart()
    assert restarted(env) == ["service_plc", "service_alarms"]
    assert "Restart the operator HMI too" in env.boxes[-1][1]
    env.state["services"]["service_alarms"] = "STOPPED"
    dlg = open_file(env.make(), "fault_config.json")
    widget_at(dlg, *next(p for _, p, _ in dlg._leaves if p[-1] == "description")).setText("reworded again")
    dlg._save_and_restart()
    assert restarted(env)[2:] == ["service_plc"] and "Left stopped: service_alarms" in env.boxes[-1][1]


def test_all_services_means_the_running_ones(env):
    (env.cfg / "x.json").write_text(json.dumps({"a": 1.5}))        # a file no service is known to read
    dlg = open_file(env.make(), "x.json")
    assert dlg.cb_service.currentText() == "ALL SERVICES"
    widget_at(dlg, "a").setText("2.5")
    dlg._save_and_restart()
    assert restarted(env) == ["service_plc", "service_smu"]          # not the one stopped on purpose
    assert "Left stopped: service_monitor" in env.boxes[-1][1]


def test_all_services_with_every_service_stopped(env):
    env.state["services"] = {"service_plc": "STOPPED", "service_smu": "STOPPED"}
    (env.cfg / "x.json").write_text(json.dumps({"a": 1.5}))
    dlg = open_file(env.make(), "x.json")
    widget_at(dlg, "a").setText("2.5")
    dlg._save_and_restart()
    assert env.cmds.sent == [] and "every service is stopped" in env.boxes[-1][1]


def test_network_changes_restart_nothing_automatically(env):
    """Every program - the manager and the HMI too - reads the ports at start-up: restarting only the
    services left them heartbeating to a port the manager wasn't listening on."""
    dlg = open_file(env.make(), "network_config.json")
    widget_at(dlg, "smu", "port").setText("5026")
    dlg._save_and_restart()
    assert cs.read("network_config.json", str(env.cfg))["smu"]["port"] == 5026
    assert env.cmds.sent == [] and "Restart the service manager and the HMI" in env.boxes[-1][1]
    assert env.rebuilds == []                                         # not a registry input


def test_no_service_list_no_blind_restarts(env):
    env.state["services"] = {}
    (env.cfg / "x.json").write_text(json.dumps({"a": 1.5}))        # -> ALL SERVICES: which ones run?
    dlg = open_file(env.make(), "x.json")
    widget_at(dlg, "a").setText("2.5")
    dlg._save_and_restart()
    assert env.cmds.sent == [] and "Nothing was restarted" in env.boxes[-1][1]


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
    before = (env.cfg / "smu_config.json").read_bytes()
    dlg = open_file(env.make(), "smu_config.json")
    widget_at(dlg, "max_v").setText("150")
    dlg._save_and_restart()
    dlg = open_file(env.make(), "smu_config.json")
    assert widget_at(dlg, "max_v").text() == "150"
    monkeypatch.setattr(ced.QInputDialog, "getItem", staticmethod(lambda *a, **k: (a[3][0], True)))
    dlg._show_history()
    assert (env.cfg / "smu_config.json").read_bytes() == before        # exactly as it was
    assert widget_at(dlg, "max_v").text() == "200"                      # reloaded
    assert env.confirm["shown"][-1] == ["max_v: 150 -> 200"]
    assert len(cs.backups("smu_config.json", str(env.cfg))) == 2       # the 150 version is kept too


def test_history_rescues_an_unreadable_file(env, monkeypatch):
    from src.gui.control.dialogs import config_editor_dialog as ced
    dlg = open_file(env.make(), "smu_config.json")
    widget_at(dlg, "max_v").setText("150")
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


def test_a_service_stopped_on_purpose_stays_stopped(env):
    env.state["services"]["service_magnet_psu"] = "STOPPED"            # stopped for work on the PSU
    dlg = open_file(env.make(), "magnet_config.json")
    widget_at(dlg, "max_current").setText("90")
    dlg._save_and_restart()
    assert cs.read("magnet_config.json", str(env.cfg))["max_current"] == 90.0
    assert env.cmds.sent == [] and "was not restarted: it is stopped" in env.boxes[-1][1]


def test_a_hard_cap_is_refused_and_shown_on_the_field(env):
    """Past a hard cap nothing gets applied - there is no tick to confirm it - and the field turns red
    with the reason as its tooltip."""
    from src.gui.control.dialogs import config_editor_dialog as ced
    dlg = open_file(env.make(), "vac_gauges_config.json")
    before = (env.cfg / "vac_gauges_config.json").read_bytes()
    widget_at(dlg, "10", "relays", "1", "on_val").setText("0.7")
    widget_at(dlg, "10", "relays", "1", "off_val").setText("0.8")
    dlg._save_and_restart()
    kind, text = env.boxes[-1]
    assert kind == "warning" and "over a hard cap" in text and "gate valve may open" in text
    assert env.confirm["shown"] == []                                  # no confirmation offered at all
    assert (env.cfg / "vac_gauges_config.json").read_bytes() == before
    field = widget_at(dlg, "10", "relays", "1", "on_val")
    assert field.property("tone") == ced.CAP_TONE and "1e-05 mbar cap" in field.toolTip()
    field.setText("5.5e-06")
    widget_at(dlg, "10", "relays", "1", "off_val").setText("1e-05")    # at the cap is allowed
    dlg._save_and_restart()
    assert not field.property("tone") and env.boxes[-1][0] == "information" and "Saved" in env.boxes[-1][1]
    assert cs.read("vac_gauges_config.json", str(env.cfg))["10"]["relays"]["1"]["off_val"] == 1e-05


def test_history_wont_put_back_a_version_past_a_cap(env, monkeypatch):
    from src.gui.control.dialogs import config_editor_dialog as ced
    data = json.loads((env.cfg / "magnet_config.json").read_text())
    (env.cfg / "magnet_config.json").write_text(json.dumps(dict(data, max_current=120.0)))   # edited by hand
    dlg = open_file(env.make(), "magnet_config.json")
    widget_at(dlg, "max_current").setText("90")
    dlg._save_and_restart()                                           # the 120 A file is kept as a backup
    assert cs.read("magnet_config.json", str(env.cfg))["max_current"] == 90.0
    monkeypatch.setattr(ced.QInputDialog, "getItem", staticmethod(lambda *a, **k: (a[3][0], True)))
    dlg._show_history()
    assert cs.read("magnet_config.json", str(env.cfg))["max_current"] == 90.0     # not put back
    assert "Hard cap: max_current 120 A is above the 95 A cap" in env.boxes[-1][1]


def test_a_file_already_past_a_cap_is_shown_when_opened(env):
    from src.gui.control.dialogs import config_editor_dialog as ced
    data = json.loads((env.cfg / "magnet_config.json").read_text())
    (env.cfg / "magnet_config.json").write_text(json.dumps(dict(data, max_current=120.0)))   # edited by hand
    dlg = env.make()
    dlg.cb_files.setCurrentText("magnet_config.json")
    assert not dlg.lbl_problems.isHidden() and "Hard cap: max_current 120 A" in dlg.lbl_problems.text()
    assert widget_at(dlg, "max_current").property("tone") == ced.CAP_TONE
