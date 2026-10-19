"""Ion source panel: setpoint boxes limited to what the services accept; the filament
setpoint locked while auto-emission drives the filament."""
import pytest


class FakeCmd:
    def __init__(self):
        self.sent = []

    def send_command(self, target, tag, value, *args, **kwargs):
        self.sent.append((target, tag, value))


@pytest.fixture
def panel():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from src.gui.control.widgets.source_widget import IonSourceWidget
    w = IonSourceWidget(FakeCmd(), fault_engine=None)
    yield w
    w.deleteLater()
    app.processEvents()


LIVE = {"system.connected": True, "ion_beam.facilities.safety_relay_active": 1.0,
        "manager.services": {"service_spellman_mpd": "ONLINE"},
        **{f"ion_beam.source.{s}.permit": 1.0 for s in ("einzel", "extraction", "target", "filament", "thermionic",
                                                         "cesium")}}


def test_setpoint_boxes_use_the_registry_limits(panel):
    from src.gui.control.widgets.source_widget import setpoint_limits
    limits = setpoint_limits()
    for name, tag in (("Source Einzel", "ion_beam.source.einzel.sp_requested_voltage"),
                      ("Filament", "ion_beam.source.filament.sp_requested_current"),
                      ("Thermionic", "ion_beam.source.thermionic.sp_requested_current"),
                      ("Cesium", "ion_beam.source.cesium.sp_requested_temp")):
        box = panel.psu_controls[name]["sp_box"]
        assert (box.minimum(), box.maximum()) == pytest.approx(limits[tag]), name


def test_filament_setpoint_locked_during_auto_emission(panel):
    fil = panel.psu_controls["Filament"]
    panel.update_telemetry(dict(LIVE, **{"ion_beam.source.thermionic.stat_auto_emission": 0.0}))
    assert fil["sp_box"].isEnabled() and fil["lbl_badge"].isHidden()
    panel.update_telemetry(dict(LIVE, **{"ion_beam.source.thermionic.stat_auto_emission": 1.0,
                                         "ion_beam.source.filament.sp_actual_current": 27.4}))
    assert not fil["sp_box"].isEnabled()
    assert "AUTO-EMIS" in fil["lbl_badge"].text()
    assert fil["sp_box"].value() == pytest.approx(27.4)       # shows what the PLC is driving
    assert fil["btn_enable"].isEnabled()                       # switching it off stays possible
    panel.update_telemetry(dict(LIVE, **{"ion_beam.source.thermionic.stat_auto_emission": 0.0}))
    assert fil["sp_box"].isEnabled()


def test_locks_come_from_the_services_permits(panel):
    fil = panel.psu_controls["Filament"]
    panel.update_telemetry(dict(LIVE, **{"ion_beam.source.filament.permit": 0.0,
                                         "ion_beam.source.filament.permit_reason": "remote I/O to the source deck lost"}))
    assert not fil["sp_box"].isEnabled() and "remote I/O" in fil["sp_box"].toolTip()
    held = dict(LIVE, **{"ion_beam.source.target.permit": 0.0,
                         "ion_beam.source.target.permit_reason": "target is controlled by recipe 'Ramp' - stop it, "
                                                                 "or take control, first",
                         "ion_beam.source.target.lease_purpose": "recipe 'Ramp'"})
    panel.update_telemetry(held)
    tgt = panel.psu_controls["Target"]
    assert not tgt["sp_box"].isEnabled() and "SEQUENCE" in tgt["lbl_badge"].text()
    assert "recipe 'Ramp'" in tgt["lbl_badge"].toolTip()
    no_data = {k: v for k, v in LIVE.items() if not k.startswith("ion_beam.source.cesium")}
    panel.update_telemetry(no_data)
    assert "no live data from the PLC service" in panel.psu_controls["Cesium"]["sp_box"].toolTip()


def _focused():
    from PyQt6.QtWidgets import QApplication
    return QApplication.focusWidget()


def _shown(panel):
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance()
    panel.show()
    panel.activateWindow()
    QTest.qWaitForWindowActive(panel, 2000)
    return app


def test_start_up_focus_and_locks_send_nothing(panel):
    """October 2026, run_simulation.py: the einzel box had the window's first focus, still showing its
    default 0 (a focused box didn't follow telemetry). The first telemetry - relay off - locked it, Qt
    moved the focus on, and every box it passed sent its 0: einzel, extraction, target, filament,
    thermionic, cesium. On the real machine with the source running, those would have been obeyed."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    app = _shown(panel)
    panel.psu_controls["Source Einzel"]["sp_box"].setFocus(Qt.FocusReason.OtherFocusReason)
    app.processEvents()
    relay_off = {"system.connected": True, "ion_beam.facilities.safety_relay_active": 0.0,
                 "manager.services": {"service_spellman_mpd": "ONLINE"},
                 **{f"ion_beam.source.{s}.permit": 0.0 for s in ("einzel", "extraction", "target", "filament",
                                                                  "thermionic", "cesium")},
                 **{f"ion_beam.source.{s}.permit_reason": "safety relay de-energised"
                    for s in ("einzel", "extraction", "target", "filament", "thermionic", "cesium")}}
    panel.update_telemetry(relay_off)
    app.processEvents()
    running = dict(LIVE, **{"ion_beam.source.einzel.sp_actual_voltage": 10.0,
                            "ion_beam.source.extraction.sp_actual_voltage": 15.0,
                            "ion_beam.source.filament.sp_actual_current": 28.0})
    panel.update_telemetry(running)
    box = panel.psu_controls["Source Einzel"]["sp_box"]
    box.setFocus(Qt.FocusReason.MouseFocusReason)
    for _ in range(25):                                       # the operator tabs through the whole panel
        QTest.keyClick(_focused(), Qt.Key.Key_Tab)
        panel.update_telemetry(running)
    assert panel.cmd_thread.sent == []
    assert box.value() == 10.0 and panel.psu_controls["Extraction"]["sp_box"].value() == 15.0


def test_a_typed_setpoint_is_sent_alone(panel):
    """Typed + Enter: the setpoint, nothing else (the einzel's current limit is the Spellman service's job)."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    app = _shown(panel)
    panel.update_telemetry(LIVE)
    for name, text, expected in (("Source Einzel", "4", ("spellman", "ion_beam.source.einzel.sp_requested_voltage", 4.0)),
                                 ("Extraction", "12.5", ("plc", "ion_beam.source.extraction.sp_requested_voltage", 12.5)),
                                 ("Cesium", "95", ("plc", "ion_beam.source.cesium.sp_requested_temp", 95.0))):
        box = panel.psu_controls[name]["sp_box"]
        box.setFocus(Qt.FocusReason.MouseFocusReason)
        box.lineEdit().selectAll()
        QTest.keyClicks(box.lineEdit(), text)
        QTest.keyClick(box.lineEdit(), Qt.Key.Key_Return)
        app.processEvents()
        assert panel.cmd_thread.sent[-1] == expected, name
    assert len(panel.cmd_thread.sent) == 3
