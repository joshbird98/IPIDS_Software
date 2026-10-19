"""'Calculate & set amps' (Beamline Optics) uses the Mass Scan panel's calibration
AMU = k (I + I_off)^2 / E and the live beam energy (target + extraction voltage)."""
import math

import pytest

from src.core.mass_calibration import current_for_mass, mass_for_current

TAG = "ion_beam.beamline.magnet.sp_requested_current"
LIVE = {"ion_beam.source.target.rb_voltage": 20.0, "ion_beam.source.extraction.rb_voltage": 5.0}


def test_model_round_trip():
    amps = current_for_mass(28.0, 25000.0, 1391.9, 0.223)
    assert amps == pytest.approx(math.sqrt(28 * 25000 / 1391.9) - 0.223)
    assert mass_for_current(amps, 25000.0, 1391.9, 0.223) == pytest.approx(28.0)
    with pytest.raises(ValueError):
        current_for_mass(28.0, 0.0, 1391.9, 0.223)


class FakeCmd:
    def __init__(self):
        self.sent = []

    def send_command(self, target, tag, value, *args, **kwargs):
        self.sent.append((target, tag, value))


class FakeEvents:
    def __init__(self):
        self.markers = []

    def log_user_marker(self, ts, text, color):
        self.markers.append(text)


@pytest.fixture
def optics():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from src.gui.control.widgets.optics_widget import BeamlineOpticsWidget
    cmd, events = FakeCmd(), FakeEvents()
    w = BeamlineOpticsWidget(cmd, events)
    warnings = []
    w._warn = lambda title, text: warnings.append(title)       # instead of a modal box
    w.mass_calibration = lambda: {"k": 1391.9, "i_off": 0.223, "source": "fitted to 3 peaks at 10:00"}
    yield w, cmd, events, warnings
    w.deleteLater()
    app.processEvents()


def test_sets_the_current_from_the_mass_scan_calibration(optics):
    w, cmd, events, warnings = optics
    w._latest_telemetry = dict(LIVE)
    w.sp_mass.setValue(28.0)
    w._calculate_mass()
    assert not warnings
    target, tag, amps = cmd.sent[-1]
    assert (target, tag) == ("magnet", TAG)
    assert amps == pytest.approx(current_for_mass(28.0, 25000.0, 1391.9, 0.223))
    assert "fitted to 3 peaks" in events.markers[-1] and "25.000 keV" in events.markers[-1]


@pytest.mark.parametrize("telemetry, why", [
    ({"ion_beam.source.extraction.rb_voltage": 5.0}, "Beam energy unknown"),     # target readback stale
    ({"ion_beam.source.target.rb_voltage": 0.0, "ion_beam.source.extraction.rb_voltage": 0.0}, "Calculation error"),
    ({"ion_beam.source.target.rb_voltage": 95.0, "ion_beam.source.extraction.rb_voltage": 5.0}, "Hard cap"),
])
def test_refuses_without_a_usable_energy_or_in_range_result(optics, telemetry, why):
    w, cmd, events, warnings = optics
    w._latest_telemetry = telemetry
    w.sp_mass.setValue(250.0)
    w._calculate_mass()
    assert warnings == [why] and not cmd.sent


def test_a_current_below_zero_is_out_of_range(optics):
    w, cmd, events, warnings = optics
    w._latest_telemetry = {"ion_beam.source.target.rb_voltage": 0.0, "ion_beam.source.extraction.rb_voltage": 0.005}
    w.sp_mass.setValue(1.0)                                   # 5 eV: below the calibration's offset
    w._calculate_mass()
    assert warnings == ["Out of range"] and not cmd.sent


def test_refuses_without_a_calibration(optics):
    w, cmd, events, warnings = optics
    w.mass_calibration = None
    w._latest_telemetry = dict(LIVE)
    w._calculate_mass()
    assert warnings == ["No mass calibration"] and not cmd.sent
