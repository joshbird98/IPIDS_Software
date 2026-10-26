"""Mass Scan panel's current shift: up to 20 pA either way added to every beam current shown, to take
out the offset of the current measurement - by hand, or with Auto-zero from the trace's quietest
readings. The plot, its peaks and its exported figures use the shifted readings; the readings
themselves, saved traces and the figures' data files stay as measured."""
import csv
import json
import math
import os
import subprocess
import sys

import numpy as np
import pytest

from src.gui.control.widgets.mass_scan.traces import estimate_zero

OFFSET = -6e-12             # what the cup reads with no beam, in these scans


def readings(n=301, offset=OFFSET, noise=1.5e-12, width=0.15, seed=3):
    """(magnet A, beam A): 20 to 30 A, a 100 nA peak at 25 A (width: its sigma in A), on the
    offset with a little noise."""
    rng = np.random.default_rng(seed)
    amps = np.linspace(20.0, 30.0, n)
    beam = offset + rng.normal(0.0, noise, n) + 1e-7 * np.exp(-0.5 * ((amps - 25.0) / width) ** 2)
    return amps, beam


# --------------------------------------------------------------------------- the estimate
def test_the_zero_is_the_quietest_readings_median():
    _amps, beam = readings()
    assert estimate_zero(beam) == pytest.approx(OFFSET, abs=0.5e-12)
    assert estimate_zero(np.full(40, 3e-12)) == pytest.approx(3e-12)         # all quiet, all alike
    assert estimate_zero(np.append(beam, [np.nan, np.inf])) == pytest.approx(OFFSET, abs=0.5e-12)


def test_no_zero_from_too_few_readings_or_a_trace_that_is_all_peak():
    assert estimate_zero([]) is None and estimate_zero([1e-12] * 5) is None
    amps = np.linspace(24.5, 25.5, 60)
    flanks = 5e-10 + 1e-7 * np.exp(-0.5 * ((amps - 25.0) / 0.15) ** 2)
    assert estimate_zero(flanks) > 1e-10            # (the panel refuses it: beyond the shift's reach)


# --------------------------------------------------------------------------- the panel
@pytest.fixture
def make(monkeypatch, tmp_path):
    monkeypatch.setenv("SINDIE_SETTINGS_DIR", str(tmp_path / "settings"))
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from src.gui.control.widgets.mass_scan import PrecisionMassScannerWidget

    class Nothing:
        def __getattr__(self, name):
            return lambda *a, **k: None

    made = []

    def build(scan=True, **kw):
        w = PrecisionMassScannerWidget(Nothing(), Nothing())
        w.resize(1200, 700)
        if scan:
            for a, b in zip(*readings(**kw)):
                w._update_plot(float(a), float(b), 30.0, refresh=False)
            w._refresh_curves()
            w._update_plot_limits()
        app.processEvents()
        made.append(w)
        return w
    yield build
    for w in made:
        w.deleteLater()
    app.processEvents()


def plotted(w):
    """The beam currents on the plot (linear view): the positive curve has every point."""
    _x, y = w.curve_live_pos.getOriginalDataset()
    return np.asarray(y, dtype=float)


def left_label(w):
    return w.plot_widget.getPlotItem().getAxis("left").labelText


def test_the_shift_moves_every_reading_shown_but_not_the_readings(make):
    w = make()
    raw = list(w.y_data)
    assert w.sp_shift.value() == 0.0 and left_label(w) == "Beam Current"
    w.sp_shift.setValue(6.0)
    assert w.current_shift_a == pytest.approx(6e-12)
    assert np.allclose(plotted(w), np.array(raw) + 6e-12, rtol=0, atol=1e-18)
    assert w.y_data == raw                                          # the readings themselves untouched
    assert left_label(w) == "Beam Current (shifted +6.0 pA)"
    w.sp_shift.setValue(-2.5)
    assert left_label(w) == "Beam Current (shifted -2.5 pA)"
    w.sp_shift.setValue(0.0)
    assert np.allclose(plotted(w), raw, rtol=0, atol=1e-18) and left_label(w) == "Beam Current"


def test_the_shift_goes_no_further_than_20_pA_and_is_not_kept(make):
    w = make()
    w.set_current_shift(35.0)
    assert w.sp_shift.value() == 20.0 and w.current_shift_a == pytest.approx(20e-12)
    w.set_current_shift(-35.0)
    assert w.sp_shift.value() == -20.0 and w.current_shift_a == pytest.approx(-20e-12)
    w.set_current_shift(4.04)
    assert w.current_shift_a == pytest.approx(4.0e-12)              # (to the box's 0.1 pA)
    assert make(scan=False).sp_shift.value() == 0.0                 # a new panel (HMI) starts unshifted


def test_the_negative_stretches_go_once_zeroed(make):
    w = make(noise=0.2e-12)
    _x, neg = w.curve_live_neg.getOriginalDataset()
    assert np.sum(np.isfinite(neg)) > 200                           # the offset: most points below zero
    w.btn_auto_zero.click()
    assert w.sp_shift.value() == pytest.approx(6.0, abs=0.2)
    _x, neg = w.curve_live_neg.getOriginalDataset()
    assert np.sum(np.isfinite(np.asarray(neg, dtype=float))) < 200  # about half the quiet points: noise either side
    quiet = plotted(w)[np.abs(np.linspace(20.0, 30.0, 301) - 25.0) > 1.0]
    assert abs(np.median(quiet)) < 0.1e-12                          # the floor now reads zero
    assert "shifted by +6" in w.lbl_status.text()


def test_auto_zero_refuses_a_trace_with_nothing_quiet_and_an_empty_plot(make, monkeypatch):
    from src.gui.control.widgets.mass_scan import widget as mod
    told = []
    monkeypatch.setattr(mod.QMessageBox, "information", staticmethod(lambda *a: told.append(a[2])))
    w = make(offset=5e-10)                                         # all readings 500 pA or more
    w.sp_shift.setValue(3.0)
    w.btn_auto_zero.click()
    assert told and "further from zero than the shift can go" in told[0]
    assert w.sp_shift.value() == 3.0                                # left as it was
    empty = make(scan=False)
    empty.btn_auto_zero.click()
    assert len(told) == 2 and "too few readings" in told[1] and empty.sp_shift.value() == 0.0


def test_peaks_and_tails_use_the_shifted_readings(make):
    pytest.importorskip("scipy")
    w = make(n=1001, noise=0.0, width=0.03)                         # (a narrow peak: its own tails are nil)
    w.btn_detect.setChecked(True)
    (peak,) = w._found_peaks
    assert peak.tail_left == 0.0                                    # -6 pA counts as nothing (no negative tails)
    w.sp_shift.setValue(16.0)                                       # the floor now reads +10 pA
    (peak,) = w._found_peaks
    assert peak.tail_left == pytest.approx(10e-12, abs=0.1e-12)
    assert peak.current == pytest.approx(1e-7 + 10e-12, rel=1e-9)
    assert w.peak_table.item(0, 2).text() == "100 ppm"              # 10 pA on the 100 nA peak
    _x, y = w.peak_markers.getData()
    assert y[0] == pytest.approx(peak.current, rel=1e-9)


def test_calibration_stars_move_with_the_shift(make, monkeypatch):
    from src.gui.control.widgets.mass_scan import widget as mod
    monkeypatch.setattr(mod.QInputDialog, "getDouble", staticmethod(lambda *a, **k: (28.0, True)))
    w = make(noise=0.0)
    w.set_x_axis("amps")
    w._add_calibration_point(25.0)
    raw = w.calib_points[0]["peak_y"]
    w.sp_shift.setValue(12.0)
    _x, y = w.calib_scatter.getData()
    assert w.calib_points[0]["peak_y"] == raw and y[0] == pytest.approx(raw + 12e-12, rel=1e-9)


def test_a_saved_trace_keeps_the_readings_as_measured(make, tmp_path):
    w = make()
    amps, beam = readings()
    w.last_scan_data = [(float(a), float(a), float(b), 0.0, 30.0) for a, b in zip(amps, beam)]
    w.edit_export_path.setText(str(tmp_path / "MassScan"))
    w.sp_shift.setValue(6.0)
    w._manual_save_csv()
    (path,) = list(tmp_path.glob("MassScan_*.csv"))
    with open(path, newline="") as f:
        saved = [float(r["Measured_Beam_A"]) for r in csv.DictReader(f)]
    assert np.allclose(saved, beam, rtol=1e-9, atol=1e-20)


# --------------------------------------------------------------------------- the figures
def test_the_export_records_the_shift_and_draws_with_it(make, tmp_path):
    pytest.importorskip("matplotlib")
    pytest.importorskip("scipy")
    from src.gui.control.widgets.mass_scan import thesis_figure as tf
    w = make(noise=0.0)
    w.btn_detect.setChecked(True)
    w.sp_shift.setValue(6.0)
    w.plot_widget.getViewBox().setXRange(min(w.amu_data), max(w.amu_data), padding=0)
    folder, points, peaks, settings = w._figure_snapshot()
    assert settings["current_shift_A"] == pytest.approx(6e-12)
    assert [p[2] for p in points] == pytest.approx([w.y_data[i] for i in range(len(points))], rel=1e-12)
    assert peaks[0]["current"] == pytest.approx(1e-7 + OFFSET, rel=1e-9)     # as measured (the plot: +6 pA)

    out = tmp_path / "fig"
    settings["software"] = "test"
    tf.export(str(out), points, peaks, settings, formats=("svg",))
    with open(out / "data.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [float(r["Beam_A"]) for r in rows] == pytest.approx([p[2] for p in points], rel=1e-6, abs=1e-18)
    with open(out / "peaks.csv", newline="", encoding="utf-8") as f:
        assert float(next(csv.DictReader(f))["Beam_A"]) == pytest.approx(1e-7 + OFFSET, rel=1e-6)
    meta = json.loads((out / "figure.json").read_text(encoding="utf-8"))
    assert meta["current_shift_A"] == pytest.approx(6e-12)
    caption = (out / "caption.txt").read_text(encoding="utf-8")
    assert r"shifted by $+6.0$~pA" in caption
    assert "shifted by +6.0 pA" in (out / "README.txt").read_text(encoding="utf-8")

    # The figures draw the shifted readings: read back as measured, shifted for drawing
    data, pk, s = tf.read_folder(str(out))
    assert np.allclose(data["Beam_A"], [p[2] for p in points], rtol=1e-6, atol=1e-18)
    drawn, drawn_peaks = tf.shifted(data, pk, tf.shift_of(s))
    assert np.allclose(drawn["Beam_A"] - data["Beam_A"], 6e-12, rtol=0, atol=1e-18)
    assert drawn_peaks[0]["current"] == pytest.approx(1e-7, rel=1e-6)          # -6 pA + 6 pA
    # make_figure.py, with another shift
    run = subprocess.run([sys.executable, "-I", "make_figure.py", "--only", "full", "--formats", "pdf",
                          "--shift-pA", "5.5"], cwd=out, capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr
    assert (out / "mass_scan_full.pdf").exists()


def test_an_unshifted_export_and_an_older_folder(tmp_path):
    pytest.importorskip("matplotlib")
    from src.gui.control.widgets.mass_scan import thesis_figure as tf
    settings = {"made": "2026-10-26 14:00:00", "software": "abc1234",
                "source": {"file": None, "description": "A scan not saved to a file"},
                "calibration": {"k": 1391.9, "i_off": 0.223, "source": "default values", "energy_kev_mean": 30.0,
                                "energy_recorded": True},
                "screen": {"x_unit": "amps", "top_axis": False, "y_scale": "linear", "xlim": [20.0, 30.0],
                           "ylim": [0.0, 1.2e-7]},
                "points": 0}
    amps, beam = readings()
    points = [(float(a), float(a), float(b), 30.0) for a, b in zip(amps, beam)]
    tf.export(str(tmp_path / "old"), points, [], settings, formats=("svg",))      # (no current_shift_A at all)
    assert "shifted" not in (tmp_path / "old" / "caption.txt").read_text(encoding="utf-8")
    assert "as measured (no shift)" in (tmp_path / "old" / "README.txt").read_text(encoding="utf-8")
    data, _pk, s = tf.read_folder(str(tmp_path / "old"))
    assert tf.shift_of(s) == 0.0 and np.allclose(data["Beam_A"], beam, rtol=1e-6, atol=1e-18)
    assert math.isclose(tf.shift_of({"current_shift_A": None}), 0.0)
    assert os.path.exists(tmp_path / "old" / "mass_scan_full.svg")
