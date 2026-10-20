"""Faraday Cup & SMU panel: the cup button shows UNKNOWN without live PLC data, not its last
position as if it were live (its tooltip says what the position was, and when)."""
import pytest

D = "ion_beam.beamline.diagnostics"


class FakeCmd:
    def __init__(self):
        self.sent = []

    def send_command(self, target, tag, value, *args, **kwargs):
        self.sent.append((target, tag, value))


@pytest.fixture
def panel():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from src.gui.control.widgets.faraday_smu_widget import FaradaySMUWidget
    w = FaradaySMUWidget(FakeCmd())
    yield w
    w.deleteLater()
    app.processEvents()


def test_the_cup_is_unknown_without_plc_data(panel):
    live = {"system.connected": True, f"{D}.stat_fc_inserted": 1.0, f"{D}.stat_fc_retracted": 0.0,
            f"{D}.permit": 1.0, "ion_beam.facilities.stat_air_press_ok": 1.0}
    panel.update_telemetry(live)
    assert panel.btn_fc.text() == "FARADAY CUP: INSERTED" and panel.btn_fc.isEnabled()
    panel.update_telemetry({"system.connected": False})
    assert panel.btn_fc.text() == "FARADAY CUP: UNKNOWN" and panel.btn_fc.property("tone") == "unknown"
    assert not panel.btn_fc.isEnabled() and "It was INSERTED at" in panel.btn_fc.toolTip()
    panel.update_telemetry(live)
    assert panel.btn_fc.text() == "FARADAY CUP: INSERTED" and panel.btn_fc.property("tone") == "info"
