"""Mass Scan plot: the log view is a symmetric log, so negative currents get their own
(red) logarithmic region below zero instead of being mirrored; Y refits on a scale change."""
import numpy as np
import pytest

from src.gui.control.widgets.mass_scan.axis import SmartCurrentAxis, format_current, symlog, symlog_inv


def test_symlog_round_trip_and_sign():
    y = np.array([-1e-6, -3e-11, 0.0, 2e-13, 5e-9, 1e-3])
    assert symlog_inv(symlog(y)) == pytest.approx(y, rel=1e-9, abs=1e-20)
    t = symlog(y)
    assert t[0] < t[1] < t[2] == 0.0 < t[3] < t[4] < t[5]
    assert symlog(1e-9) == pytest.approx(-symlog(-1e-9))


def test_a_decade_tick_reads_as_its_decade():
    """The symlog round trip puts 1 uA at 9.99999e-7 A: it reads 1 µA, not 1e+03 nA."""
    assert format_current(float(symlog_inv(symlog(1e-6)))) == "1 µA"
    assert [format_current(v) for v in (9.99999e-7, -9.999999e-13, 1e-6, 999e-9, 2.5e-11, 0.0)] == \
        ["1 µA", "-1 pA", "1 µA", "999 nA", "25 pA", "0 A"]


def test_axis_labels_decades_both_sides(scan):
    ax = SmartCurrentAxis(orientation="left")
    ax.symlog = True
    (major_spacing, major), _minor = ax.tickValues(float(symlog(-2e-10)), float(symlog(2e-7)), 500)
    labels = ax.tickStrings(major, 1.0, major_spacing)
    assert "0 A" in labels and "-10 pA" in labels and "-100 pA" in labels and "100 nA" in labels


@pytest.fixture
def scan():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from src.gui.control.widgets.mass_scan import PrecisionMassScannerWidget

    class Nothing:
        def __getattr__(self, name):
            return lambda *a, **k: None

    w = PrecisionMassScannerWidget(Nothing(), Nothing())
    yield w
    w.deleteLater()
    app.processEvents()


def test_negative_points_drawn_red_in_both_scales(scan):
    for amps, beam in ((19.0, 2e-11), (19.5, -3e-11), (20.0, 1e-7)):
        scan._update_plot(amps, beam, 20.0)
    for log in (False, True):
        scan.chk_log_y.setChecked(log)
        _x, y_all = scan.curve_live_pos.getOriginalDataset()
        _x, y_neg = scan.curve_live_neg.getOriginalDataset()
        assert np.isnan(y_neg[0]) and y_neg[1] < 0 and np.isnan(y_neg[2])
        assert y_all[1] == y_neg[1]
        expected = symlog(1e-7) if log else 1e-7
        assert y_all[2] == pytest.approx(expected)


def test_scale_change_refits_y(scan):
    vb = scan.plot_widget.getViewBox()
    scan._update_plot(20.0, 1e-7, 20.0)
    vb.setYRange(5.0, 6.0)                      # the operator zoomed: auto range off
    assert not vb.autoRangeEnabled()[1]
    scan.chk_log_y.setChecked(True)
    assert vb.autoRangeEnabled()[1]
