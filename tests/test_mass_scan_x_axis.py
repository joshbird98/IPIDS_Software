"""Mass Scan plot's x axis: against mass (AMU) or against the measured magnet current (A), the
other unit on the top axis if wanted, the pointer's position in both, and the choice remembered."""
import math

import numpy as np
import pytest
from PyQt6.QtCore import QPointF

from src.core.mass_calibration import mass_for_current
from src.gui.control.widgets.mass_scan.axis import OtherUnitAxis, decimals_for
from src.sequencer.mass_scan import SP_TAG

K, I_OFF, E = 1391.9, 0.223, 30000.0        # the panel's default calibration; 30 keV


def amu_of(amps):
    return mass_for_current(amps, E, K, I_OFF)


def amps_of(amu):
    return math.sqrt(amu * E / K) - I_OFF


def test_decimals_for_the_tick_spacing():
    assert [decimals_for(s) for s in (5, 2, 1, 0.5, 0.2, 0.05, 0.002)] == [0, 0, 0, 1, 1, 2, 3]
    assert decimals_for(0) == decimals_for(float("nan")) == 0


@pytest.fixture
def qapp():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_the_other_unit_axis_ticks_are_even_in_its_own_unit(qapp):
    """A mass plot with the current on top: the ticks fall on round currents, so on the mass
    scale they spread out as the mass goes up (AMU goes with the square of the current)."""
    def to_plot(a):
        if a < -I_OFF:
            raise ValueError("below -I_off")
        return amu_of(a)

    ax = OtherUnitAxis(orientation="top")
    assert ax.tickValues(10.0, 60.0, 600) == []                 # no conversion yet: no ticks
    ax.set_conversion(lambda m: amps_of(max(m, 0.0)), to_plot)
    (spacing, positions), *_ = ax.tickValues(10.0, 60.0, 600)
    amps = [amps_of(x) for x in positions]
    assert len(positions) >= 3 and all(10.0 <= x <= 60.0 for x in positions)
    assert np.allclose(np.diff(amps), spacing)                  # even in amps...
    assert np.all(np.diff(np.diff(positions)) > 0)              # ...wider apart in AMU as the mass rises
    places = decimals_for(spacing)
    assert ax.tickStrings(positions, 1.0, spacing) == [f"{a:.{places}f}" for a in amps]
    # Across zero mass (the view's edge): no error, no tick below the domain
    levels = ax.tickValues(-5.0, 5.0, 400)
    assert all(x >= 0.0 for _s, xs in levels for x in xs)


class Recorder:
    def __init__(self):
        self.sent = []

    def send_command(self, subsystem, tag, value, origin="hmi", timeout=None, holder=None):
        self.sent.append((subsystem, tag, value))

        class Done:
            ok, data, reason = True, None, ""

            def done(self):
                return True
        return Done()


@pytest.fixture
def make(monkeypatch, tmp_path):
    """Panels whose saved settings live in tmp_path (the x axis choice is remembered there)."""
    monkeypatch.setenv("SINDIE_SETTINGS_DIR", str(tmp_path / "settings"))
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from src.gui.control.widgets.mass_scan import PrecisionMassScannerWidget

    class Events:
        def __getattr__(self, name):
            return lambda *a, **k: None

    made = []

    def build():
        w = PrecisionMassScannerWidget(Recorder(), Events())
        w.resize(1200, 700)                 # (not shown: the plot maps positions all the same)
        app.processEvents()
        made.append(w)
        return w
    yield build
    for w in made:
        w.deleteLater()
    app.processEvents()


def scanned(w):
    """A scan from 20 to 30 A at 30 keV, with a peak at 25 A."""
    for amps in np.arange(20.0, 30.0001, 0.1):
        w._update_plot(float(amps), 1e-12 + 1e-7 * math.exp(-0.5 * ((amps - 25.0) / 0.15) ** 2), 30.0,
                       refresh=False)
    w._refresh_curves()
    w._update_plot_limits()
    return w


def view_x(w):
    return w.plot_widget.getViewBox().viewRange()[0]


def scene_pos(w, x):
    view = w.plot_widget.getViewBox()
    y0, y1 = view.viewRange()[1]
    return view.mapViewToScene(QPointF(x, (y0 + y1) / 2))


def test_starts_on_mass_with_the_current_on_top(make):
    w = make()
    item = w.plot_widget.getPlotItem()
    assert w.x_unit == "amu" and w.btn_x_amu.isChecked() and not w.btn_x_amps.isChecked()
    assert w.chk_x_both.isChecked() and item.getAxis("top").isVisible()
    assert item.getAxis("bottom").labelText == "Mass (AMU)" and item.getAxis("top").labelText == "Magnet current (A)"


def test_plot_against_current_and_back_keeps_the_view(make):
    w = scanned(make())
    x, _ = w.curve_live_pos.getOriginalDataset()
    assert np.allclose(x, w.amu_data)
    w.plot_widget.getViewBox().setXRange(amu_of(24.0), amu_of(26.0), padding=0)
    w.btn_x_amps.click()
    x, _ = w.curve_live_pos.getOriginalDataset()
    assert w.x_unit == "amps" and np.allclose(x, w.amps_data)
    item = w.plot_widget.getPlotItem()
    assert item.getAxis("bottom").labelText == "Magnet current (A)" and item.getAxis("top").labelText == "Mass (AMU)"
    assert view_x(w) == pytest.approx([24.0, 26.0], abs=1e-6)         # the same part of the scan in view
    w.btn_x_amu.click()
    assert view_x(w) == pytest.approx([amu_of(24.0), amu_of(26.0)], abs=1e-6)


def test_the_top_axis_can_be_hidden(make):
    w = make()
    w.chk_x_both.setChecked(False)
    assert not w.plot_widget.getPlotItem().getAxis("top").isVisible() and not w.x_both
    w.chk_x_both.setChecked(True)
    assert w.plot_widget.getPlotItem().getAxis("top").isVisible()


def test_the_choice_is_remembered(make):
    w = make()
    w.btn_x_amps.click()
    w.chk_x_both.setChecked(False)
    again = make()
    assert again.x_unit == "amps" and again.btn_x_amps.isChecked() and not again.chk_x_both.isChecked()
    assert not again.plot_widget.getPlotItem().getAxis("top").isVisible()


def test_the_pointer_reads_both_units(make):
    w = scanned(make())
    w.set_x_axis("amps")
    w.plot_widget.getViewBox().setXRange(20.0, 30.0, padding=0)
    w._on_mouse_moved(scene_pos(w, 25.0))
    amu, amps = w.lbl_cursor.text().split(" AMU = ")
    assert float(amu) == pytest.approx(amu_of(25.0), abs=0.01) and float(amps.rstrip(" A")) == pytest.approx(25.0,
                                                                                                         abs=0.01)
    w.set_x_axis("amu")
    w._on_mouse_moved(scene_pos(w, amu_of(22.0)))
    assert w.lbl_cursor.text().endswith(f"= {22.0:.3f} A")
    w._on_mouse_moved(QPointF(-50.0, -50.0))                   # off the plot
    assert w.lbl_cursor.text() == ""


def test_peaks_and_calibration_points_sit_on_the_current_plot(make, monkeypatch):
    pytest.importorskip("scipy")
    from src.gui.control.widgets.mass_scan import widget as mod
    w = scanned(make())
    w.set_x_axis("amps")
    w.btn_detect.setChecked(True)
    x, _ = w.peak_markers.getData()
    assert len(x) == 1 and x[0] == pytest.approx(25.0, abs=0.05)
    assert w.peak_table.item(0, 0).text() == f"{amu_of(25.0):.2f}"
    assert w.peak_table.item(0, 0).toolTip().startswith("Magnet current 25.0")
    tails, _ = w.tail_markers.getData()
    assert tails[0] < 25.0 < tails[1]
    # Ctrl+click on the current plot: the peak nearest that current
    monkeypatch.setattr(mod.QInputDialog, "getDouble", staticmethod(lambda *a, **k: (28.0, True)))
    w._add_calibration_point(25.02)
    assert w.calib_points[0]["amps"] == pytest.approx(25.0) and w.calib_points[0]["true_amu"] == 28.0
    x, _ = w.calib_scatter.getData()
    assert x[0] == pytest.approx(25.0)
    w.set_x_axis("amu")                                         # and back on the mass plot
    x, _ = w.calib_scatter.getData()
    assert x[0] == pytest.approx(amu_of(25.0))
    x, _ = w.peak_markers.getData()
    assert x[0] == pytest.approx(amu_of(25.0), abs=0.3)


def test_double_click_on_the_current_plot_sets_that_current(make):
    w = scanned(make())
    w.set_x_axis("amps")
    w.plot_widget.getViewBox().setXRange(20.0, 30.0, padding=0)

    class DoubleClick:
        def __init__(self, pos):
            self.pos = pos

        def scenePos(self):
            return self.pos

        def double(self):
            return True

        def button(self):
            from PyQt6.QtCore import Qt
            return Qt.MouseButton.LeftButton

        def accept(self):
            pass

    w._on_mouse_clicked(DoubleClick(scene_pos(w, 23.4)))
    (subsystem, tag, value), = w.cmd_thread.sent
    assert subsystem == "magnet" and tag == SP_TAG and value == pytest.approx(23.4, abs=0.02)
    assert "23.4" in w.lbl_status.text() and f"{amu_of(value):.1f} AMU" in w.lbl_status.text()
