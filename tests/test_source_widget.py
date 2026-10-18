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
