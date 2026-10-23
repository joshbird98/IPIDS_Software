"""Mass Scan panel's Export figure (src/gui/control/widgets/mass_scan/thesis_figure.py): the plot as
framed, drawn as thesis figures at the thesis's widths, with its data and the script that draws it."""
import csv
import json
import math
import os
import re
import struct
import subprocess
import sys
import time

import numpy as np
import pytest

pytest.importorskip("matplotlib")

from src.core.mass_calibration import mass_for_current  # noqa: E402
from src.gui.control.widgets.mass_scan import thesis_figure as tf  # noqa: E402

K, I_OFF = 1391.9, 0.223
MM_TO_PT = 72.0 / 25.4


def spectrum(lo=15.0, hi=35.0, step=0.05, energy_kev=30.0):
    """(points, peaks): Si-28 and C-12 peaks on a 2 pA floor with a little negative noise."""
    points = []
    for i, amps in enumerate(np.arange(lo, hi, step)):
        amu = mass_for_current(amps, energy_kev * 1000.0, K, I_OFF)
        beam = 2e-12 + 1e-7 * math.exp(-0.5 * ((amu - 28.0) / 0.12) ** 2) \
            + 3e-8 * math.exp(-0.5 * ((amu - 24.0) / 0.12) ** 2) - (3e-12 if i % 17 == 0 else 0.0)
        points.append((float(amps), float(amu), beam, energy_kev))
    peaks = [{"amu": 24.0, "amps": float(math.sqrt(24.0 * 30000 / K) - I_OFF), "current": 3e-8,
              "html": "<sup>12</sup>C<sub>2</sub>", "labelled": True},
             {"amu": 28.0, "amps": float(math.sqrt(28.0 * 30000 / K) - I_OFF), "current": 1e-7,
              "html": "<sup>12</sup>C<sup>16</sup>O<br><sup>28</sup>Si", "labelled": True}]
    return points, peaks


def settings(y_scale="symlog", x_unit="amu", top_axis=True, xlim=(20.0, 32.0), ylim=(-1e-11, 3e-7)):
    return {"made": "2026-10-23 14:00:00", "software": "abc1234",
            "source": {"file": None, "description": "A scan not saved to a file"},
            "calibration": {"k": K, "i_off": I_OFF, "source": "default values", "energy_kev_mean": 30.0,
                            "energy_recorded": True},
            "screen": {"x_unit": x_unit, "top_axis": top_axis, "y_scale": y_scale, "xlim": list(xlim),
                       "ylim": list(ylim)},
            "points": 0}


def pdf_size_mm(path):
    box = re.search(rb"MediaBox \[ *0 0 ([\d.]+) ([\d.]+) *\]", open(path, "rb").read())
    return float(box.group(1)) / MM_TO_PT, float(box.group(2)) / MM_TO_PT


def png_size(path):
    return struct.unpack(">II", open(path, "rb").read(24)[16:24])


def test_peak_labels_for_matplotlib_and_plain_text():
    html = "<sup>12</sup>C<sub>2</sub><br><sup>28</sup>Si"
    assert tf.html_to_mathtext(html) == "$^{12}$C$_{2}$\n$^{28}$Si"
    assert tf.html_to_plain(html) == "12C2 / 28Si"
    assert tf.html_to_mathtext("") == ""


def test_the_mass_and_current_conversions_invert():
    amps = np.array([0.0, 10.0, 24.3])
    assert tf.current_of(tf.mass_of(amps, K, I_OFF, 30000.0), K, I_OFF, 30000.0) == pytest.approx(amps)
    assert tf.mass_of(-5.0, K, I_OFF, 30000.0) == 0.0                 # below -I_off: its edge, not rising again


def test_a_folder_of_figures_at_the_thesis_sizes(tmp_path):
    points, peaks = spectrum()
    folder = tmp_path / "MassScan_test_20-32amu"
    written = tf.export(str(folder), points, peaks, settings())
    meta = json.loads((folder / "figure.json").read_text(encoding="utf-8"))
    names = [v["name"] for v in meta["variants"]]
    assert names == ["full", "half", "full_linear", "full_by_current", "full_unlabelled"]
    for name in names:
        for fmt in tf.FORMATS:
            assert str(folder / f"mass_scan_{name}.{fmt}") in written and (folder / f"mass_scan_{name}.{fmt}").exists()
    # The page is the figure: 150 mm (\textwidth) and 72 mm (0.48\textwidth) wide, so 1:1 in LaTeX
    assert pdf_size_mm(folder / "mass_scan_full.pdf") == pytest.approx((150.0, 80.0), abs=0.05)
    assert pdf_size_mm(folder / "mass_scan_half.pdf") == pytest.approx((72.0, 58.0), abs=0.05)
    assert png_size(folder / "mass_scan_full.png")[0] == 2700 and png_size(folder / "mass_scan_half.png")[0] == 1600
    assert os.path.getsize(folder / "mass_scan_full.pdf") < 200_000                  # small enough to embed
    svg = (folder / "mass_scan_full.svg").read_text(encoding="utf-8")
    assert "<text" in svg and "Mass (amu)" in svg and "Magnet current (A)" in svg   # text kept as text
    # Its data, peaks, a caption and the script, beside it
    with open(folder / "data.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == len(points) and set(rows[0]) == {"Magnet_A", "Mass_amu", "Beam_A", "Energy_keV"}
    with open(folder / "peaks.csv", newline="", encoding="utf-8") as f:
        peak_rows = list(csv.DictReader(f))
    assert peak_rows[1]["Label_matplotlib"] == "$^{12}$C$^{16}$O\n$^{28}$Si" and peak_rows[1]["Label_plain"] == \
        "12C16O / 28Si"
    caption = (folder / "caption.txt").read_text(encoding="utf-8")
    assert "against mass, 20.0 to 32.0~amu, at a beam energy of 30.0~keV" in caption
    assert r"$m = k\,(I + I_\mathrm{off})^2/E$" in caption
    readme = (folder / "README.txt").read_text(encoding="utf-8")
    assert r"\includegraphics[width=\textwidth]{mass_scan_full.pdf}" in readme and "default values" in readme
    assert (folder / "make_figure.py").read_text(encoding="utf-8") == open(tf.__file__, encoding="utf-8").read()


def test_a_linear_screen_gets_a_log_variant_and_a_current_screen_a_mass_one(tmp_path):
    points, peaks = spectrum()
    s = settings(y_scale="linear", x_unit="amps", top_axis=False, xlim=(21.0, 25.5), ylim=(0.0, 1.2e-7))
    data = {"Magnet_A": np.array([p[0] for p in points]), "Mass_amu": np.array([p[1] for p in points]),
            "Beam_A": np.array([p[2] for p in points]), "Energy_keV": np.array([p[3] for p in points])}
    rows = [dict(p, label="", labelled=False) for p in peaks]
    v = {x["name"]: x for x in tf.variants(s, data, rows)}
    assert list(v) == ["full", "half", "full_log", "full_by_mass"]               # no labels: no unlabelled one
    assert v["full_log"]["y_scale"] == "log" and v["full_log"]["ylim"][0] > 0
    lo, hi = v["full_by_mass"]["xlim"]
    assert lo == pytest.approx(mass_for_current(21.0, 30000.0, K, I_OFF))
    assert hi == pytest.approx(mass_for_current(25.5, 30000.0, K, I_OFF))
    out = tf.render(data, rows, s, v["full"], str(tmp_path / "f"), formats=("svg",))
    assert "Beam current (nA)" in open(out[0], encoding="utf-8").read()            # an SI prefix when linear


def test_make_figure_draws_them_again_on_its_own(tmp_path):
    points, peaks = spectrum()
    folder = tmp_path / "fig"
    tf.export(str(folder), points, peaks, settings(), formats=("pdf",))
    (folder / "mass_scan_half.pdf").unlink()
    run = subprocess.run([sys.executable, "-I", "make_figure.py", "--only", "half", "--formats", "pdf,png",
                          "--xlim", "26", "30", "--sans"], cwd=folder, capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr
    assert (folder / "mass_scan_half.pdf").exists() and (folder / "mass_scan_half.png").exists()
    assert not (folder / "mass_scan_full.png").exists()


# --------------------------------------------------------------------------- the button on the panel
@pytest.fixture
def panel(monkeypatch, tmp_path):
    monkeypatch.setenv("SINDIE_SETTINGS_DIR", str(tmp_path / "settings"))
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from src.gui.control.widgets.mass_scan import PrecisionMassScannerWidget
    from src.gui.control.widgets.mass_scan import widget as mod

    class Nothing:
        def __getattr__(self, name):
            return lambda *a, **k: None

    monkeypatch.setattr(mod, "FIGURES_DIR", str(tmp_path / "figures"))
    real_export = tf.export
    monkeypatch.setattr(tf, "export", lambda *a, **k: real_export(*a, formats=("pdf",)))     # (quicker)
    w = PrecisionMassScannerWidget(Nothing(), Nothing())
    w.resize(1200, 700)
    shown = []
    w._show_folder = shown.append
    for amps, amu, beam, e in spectrum()[0]:
        w._update_plot(amps, beam, e, refresh=False)
    w._refresh_curves()
    w._update_plot_limits()
    yield w, shown, app, mod
    w.deleteLater()
    app.processEvents()


def wait_done(w, app, timeout=120):
    end = time.time() + timeout
    while w._figure_busy and time.time() < end:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()
    assert not w._figure_busy


def test_the_button_exports_what_is_in_view(panel):
    w, shown, app, mod = panel
    from src.gui.control.widgets.mass_scan.axis import symlog
    w.chk_log_y.setChecked(True)
    view = w.plot_widget.getViewBox()
    view.setXRange(26.0, 30.0, padding=0)
    view.setYRange(float(symlog(-1e-11)), float(symlog(3e-7)), padding=0)
    w.btn_figure.click()
    assert w._figure_busy and not w.btn_figure.isEnabled()            # drawn off the HMI's thread
    wait_done(w, app)
    (folder,) = shown
    assert os.path.basename(folder).startswith("mass_scan_") and folder.endswith("_26-30amu")
    assert w.btn_figure.isEnabled() and folder in w.lbl_status.text()
    meta = json.loads(open(os.path.join(folder, "figure.json"), encoding="utf-8").read())
    assert meta["screen"]["x_unit"] == "amu" and meta["screen"]["y_scale"] == "symlog"
    assert meta["screen"]["xlim"] == pytest.approx([26.0, 30.0])
    assert meta["screen"]["ylim"] == pytest.approx([-1e-11, 3e-7], rel=1e-6)
    with open(os.path.join(folder, "data.csv"), newline="", encoding="utf-8") as f:
        masses = [float(r["Mass_amu"]) for r in csv.DictReader(f)]
    inside = [m for m in masses if 26.0 <= m <= 30.0]
    assert len(masses) == len(inside) + 2                              # the points in view, and one either side
    assert os.path.exists(os.path.join(folder, "mass_scan_full.pdf"))


def test_a_loaded_trace_names_the_folder_and_nothing_in_view_is_refused(panel, monkeypatch, tmp_path):
    w, shown, app, mod = panel
    w._trace_path = str(tmp_path / "MassScan_20261023_101500.csv")
    w.set_x_axis("amps")
    w.plot_widget.getViewBox().setXRange(23.0, 25.0, padding=0)
    w.export_figure()
    wait_done(w, app)
    lo, hi = mass_for_current(23.0, 30000, K, I_OFF), mass_for_current(25.0, 30000, K, I_OFF)
    assert os.path.basename(shown[0]) == f"MassScan_20261023_101500_{lo:.0f}-{hi:.0f}amu"   # (the masses framed)
    meta = json.loads(open(os.path.join(shown[0], "figure.json"), encoding="utf-8").read())
    assert meta["screen"]["x_unit"] == "amps" and "MassScan_20261023_101500.csv" in meta["source"]["description"]
    # The same again: a new folder, never over the last one
    w.export_figure()
    wait_done(w, app)
    assert len(shown) == 2 and shown[1] == shown[0] + "_2"
    told = []
    monkeypatch.setattr(mod.QMessageBox, "information", staticmethod(lambda *a: told.append(a[2])))
    w._clear_plot()                                                   # nothing on the plot
    w.export_figure()
    assert told and "no scan in view" in told[0] and len(shown) == 2 and not w._figure_busy
