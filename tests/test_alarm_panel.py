"""The HMI's Alarms panel and horn: flashing unacknowledged alarms, the horn for the most urgent
one until acknowledged or silenced, commands back to the alarm service, and its own alarm when
the alarm service is not running."""
import pytest


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
def panel(app, tmp_path):
    from src.gui.control.alarm_horn import AlarmHorn
    from src.gui.control.widgets.alarm_widget import AlarmPanel
    cmds = FakeCommands()
    w = AlarmPanel(cmds, horn=AlarmHorn(sounds_dir=str(tmp_path)))     # no sound files: tracks, plays nothing
    yield w, cmds
    w.flash_timer.stop()
    w.deleteLater()
    app.processEvents()


def alarm(alarm_id, priority=2, state="UNACK", **kw):
    a = {"id": alarm_id, "message": alarm_id.replace(".", " "), "priority": priority, "state": state,
         "since": 1000.0, "count": 1}
    a.update(kw)
    return a


def telemetry(alarms, horn, unack=None, active=None, shelved=0):
    return {"alarms.list": alarms, "alarms.horn": horn,
            "alarms.counts": {"unack": len([a for a in alarms if a["state"] in ("UNACK", "RTN_UNACK")])
                              if unack is None else unack,
                              "active": len(alarms) if active is None else active, "shelved": shelved}}


def test_horn_follows_the_most_urgent_unacknowledged_alarm(panel):
    w, _ = panel
    w.update_telemetry(telemetry([alarm("plc.Relay", 1), alarm("comms.magnet", 3)], horn=1))
    assert w.horn.playing == 1 and "2 alarms to acknowledge" in w.lbl_summary.text()
    assert w.table.rowCount() == 2 and w.table.item(0, 1).text() == "CRITICAL"
    w.silence()
    assert w.horn.playing is None and w.horn.silenced
    w.update_telemetry(telemetry([alarm("plc.Relay", 1), alarm("comms.magnet", 3)], horn=1))
    assert w.horn.playing is None and "silenced" in w.lbl_summary.text()
    w.update_telemetry(telemetry([alarm("plc.Relay", 1), alarm("comms.magnet", 3), alarm("vac.g1", 2)], horn=1))
    assert w.horn.playing == 1 and not w.horn.silenced          # a new alarm sounds again
    w.update_telemetry(telemetry([alarm("plc.Relay", 1, "ACKED")], horn=None, unack=0, active=1))
    assert w.horn.playing is None and "acknowledged" in w.lbl_summary.text()
    w.update_telemetry(telemetry([], horn=None))
    assert w.table.rowCount() == 0 and w.lbl_summary.text() == "No active alarms"


def test_low_priority_alarms_never_sound(panel):
    w, _ = panel
    w.update_telemetry(telemetry([alarm("service.service_monitor", 4)], horn=4))
    assert w.horn.playing is None


def test_acknowledge_shelve_commands(panel):
    w, cmds = panel
    w.update_telemetry(telemetry([alarm("a", 2), alarm("b", 3)], horn=2))
    w.btn_ack_all.click()
    assert cmds.sent[-1] == ("alarms", "alarms.cmd_ack", {"all": True})
    w.table.selectRow(1)
    w.btn_ack.click()
    assert cmds.sent[-1] == ("alarms", "alarms.cmd_ack", {"ids": ["b"]})
    w.btn_unshelve.click()
    assert cmds.sent[-1] == ("alarms", "alarms.cmd_unshelve", {"id": "b"})
    w.btn_first_out.click()
    assert cmds.sent[-1][1] == "alarms.cmd_reset_first_out"


def test_shelve_dialog_needs_a_reason(app):
    from PyQt6.QtWidgets import QDialogButtonBox
    from src.gui.control.widgets.alarm_widget import ShelveDialog
    d = ShelveDialog("gauge 1 comms")
    ok = d.buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert not ok.isEnabled()
    d.reason.setText("gauge replaced")
    assert ok.isEnabled()
    d.deleteLater()


def test_shelved_and_suppressed_rows_are_quiet_and_explained(panel):
    w, _ = panel
    w.update_telemetry(telemetry([alarm("a", 1, shelved_until=4e9, shelve_reason="maintenance"),
                                  alarm("b", 3, suppressed=True), alarm("c", 2, first_out=True)], horn=2,
                                 shelved=1))
    texts = [w.table.item(r, 4).text() for r in range(3)]
    assert "shelved until" in texts[0] and "maintenance" in texts[0] and "suppressed" in texts[1]
    assert w.table.item(2, 0).text() == "⭐" and w.table.item(0, 0).text() == "💤"
    assert w.table.item(0, 4).background().color().name() == "#9e9e9e"


def test_alarm_service_down_is_an_alarm_itself(panel):
    w, cmds = panel
    w.update_telemetry({})
    assert "ALARM SERVICE NOT RUNNING" in w.lbl_summary.text() and w.horn.playing == 1
    w.btn_ack_all.click()
    assert cmds.sent == []                                   # nothing to send it to
    w.silence()
    w.update_telemetry({})
    assert w.horn.playing is None
    w.update_telemetry(telemetry([], horn=None))
    assert w.lbl_summary.text() == "No active alarms"


def test_sound_files_exist():
    import os
    from src.gui.control.alarm_horn import FILES, SOUNDS_DIR
    for name in FILES.values():
        assert os.path.getsize(os.path.join(SOUNDS_DIR, name)) > 10000, name
