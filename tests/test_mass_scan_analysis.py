"""Mass Scan panel analysis (src/gui/control/widgets/mass_scan/): isotope matching, peaks,
trace files and the calibration fit - and the panel following a scan run by the sequencer."""
import math

import numpy as np
import pytest

from src.core.mass_calibration import mass_for_current
from src.gui.control.widgets.mass_scan import isotopes, peaks, traces
from src.sequencer.mass_scan import export_csv


# --------------------------------------------------------------------------- isotopes
def test_mass_db_prefers_the_expected_elements():
    iso = isotopes.load_isotopes("/nonexistent.json")            # the built-in minimal set
    db = isotopes.build_mass_db(iso, {"Si"})
    assert isotopes.match(db, 28.05) == ("<sup>28</sup>Si", "expected")
    html, colour = isotopes.match(db, 56.1)                      # Si2 (expected) beats e.g. other pairs
    assert "<sup>28</sup>Si<sub>2</sub>" in html and colour == "expected"
    html, colour = isotopes.match(db, 29.0)                      # 29Si (expected) or 28Si+H
    assert colour == "expected"
    assert isotopes.match(db, 28.4) == ("", "none")              # too far from a whole mass
    plain = {c["plain"] for c in db[17]}
    assert "16O+H" in plain                                      # adducts
    assert all(m <= isotopes.MAX_MASS for m in db)


def test_compound_names_carbon_then_hydrogen():
    c12 = {"mass": 12, "plain": "12C", "html": "C", "sym": "C", "prob": 0.99}
    h1 = {"mass": 1, "plain": "1H", "html": "H", "sym": "H", "prob": 1.0}
    entry = isotopes.compound((h1, c12, h1, h1, h1))
    assert entry["plain"] == "12C1H4" and entry["mass"] == 16 and entry["prob"] == pytest.approx(0.99)


# --------------------------------------------------------------------------- peaks
def spectrum(masses, heights, width=0.15, step=0.02, lo=20.0, hi=40.0):
    amu = np.arange(lo, hi, step)
    current = np.full_like(amu, 1e-12)
    for m, h in zip(masses, heights):
        current += h * np.exp(-0.5 * ((amu - m) / width) ** 2)
    return amu, current


def test_peaks_resolution_tails_and_labels():
    db = isotopes.build_mass_db(isotopes.load_isotopes("/nonexistent.json"), {"Si"})
    amu, current = spectrum([28.0, 29.0, 30.0, 35.0], [1e-7, 5e-9, 3e-9, 2e-8])
    found = peaks.find(amu, current, 0.05, 15, db)
    assert [round(p.amu) for p in found] == [28, 29, 30, 35]
    si28 = found[0]
    assert si28.fwhm_amu == pytest.approx(2.355 * 0.15, rel=0.05)
    assert si28.resolution == pytest.approx(28 / (2.355 * 0.15), rel=0.05)
    assert si28.labelled and si28.match_html == "<sup>28</sup>Si"
    assert si28.crosstalk_ppm == pytest.approx(max(si28.tail_left, si28.tail_right) / si28.current * 1e6)
    assert peaks.average_resolution(found) > 0
    assert len(peaks.find(amu, current, 0.05, 2, db)) == 2          # max peaks
    assert peaks.find(amu[:5], current[:5], 0.05, 15, db) == []    # too few points


def test_downward_scans_give_the_same_tails():
    db = isotopes.build_mass_db(isotopes.load_isotopes("/nonexistent.json"), set())
    amu, current = spectrum([28.0], [1e-7])
    up = peaks.find(amu, current, 0.05, 5, db)[0]
    down = peaks.find(amu[::-1], current[::-1], 0.05, 5, db)[0]
    assert down.tail_left == pytest.approx(up.tail_left) and down.fwhm_amu == pytest.approx(up.fwhm_amu)


def test_tail_text_and_colour():
    assert peaks.tail_text(0.5) == "< 1 ppm" and peaks.tail_text(1234) == "1,234 ppm"
    assert peaks.tail_text(6e5) == "Overlapped"
    assert peaks.tail_colour(10) == (46, 125, 50) and peaks.tail_colour(1e5) == (198, 40, 40)


# --------------------------------------------------------------------------- traces
def test_trace_round_trip_with_calibration(tmp_path):
    points = [(20.0 + i, 20.0 + i, 1e-9 * i, 4.0, 10.0) for i in range(5)]
    path = export_csv(points, str(tmp_path / "MassScan"))
    trace = traces.load_trace(path)
    assert trace.amps == [20.0, 21.0, 22.0, 23.0, 24.0] and trace.has_energy and trace.energy_kev[0] == 14.0
    assert trace.k is None
    traces.save_calibration(path, 1400.5, 0.25)
    trace = traces.load_trace(path)
    assert (trace.k, trace.i_off) == (1400.5, 0.25) and len(trace.amps) == 5
    traces.save_calibration(path, 1401.0, 0.2)                     # columns reused, not added again
    assert open(path).readline().count("Calib_k") == 1


def test_not_a_trace(tmp_path):
    bad = tmp_path / "x.csv"
    bad.write_text("a,b\n1,2\n")
    with pytest.raises(ValueError, match="not a mass scan trace"):
        traces.load_trace(str(bad))


def test_calibration_fit_recovers_k_and_offset():
    k, i_off, e = 1391.9, 0.223, 14000.0
    pts = [{"amps": a, "energy_ev": e, "true_amu": mass_for_current(a, e, k, i_off)} for a in (20.0, 25.0, 31.0)]
    fk, fi, r2 = traces.fit_calibration(pts, 0.0)
    assert fk == pytest.approx(k) and fi == pytest.approx(i_off) and r2 == pytest.approx(1.0)
    fk, fi, r2 = traces.fit_calibration(pts[:1], i_off)             # one peak: k only
    assert fk == pytest.approx(k) and fi == i_off and r2 is None
    with pytest.raises(ValueError, match="negative slope"):
        traces.fit_calibration([{"amps": 20, "energy_ev": e, "true_amu": 40}, {"amps": 30, "energy_ev": e,
                                                                               "true_amu": 10}], 0.0)


# --------------------------------------------------------------------------- the panel
class Ticket:
    def __init__(self, data=None, ok=True):
        self.data, self.ok, self.reason, self.status = data, ok, "", "ok" if ok else "rejected"

    def done(self):
        return True


class FakeCommands:
    """Records commands; answers fetches from `passes`."""

    def __init__(self):
        self.sent = []
        self.passes = {}

    def send_command(self, subsystem, tag, value, origin="hmi", timeout=None, holder=None):
        self.sent.append((subsystem, tag, value, origin))
        if tag.endswith("cmd_fetch"):
            return Ticket(self.passes.get(value["pass"]))
        return Ticket()


@pytest.fixture
def panel():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from src.gui.control.widgets.mass_scan import PrecisionMassScannerWidget

    class Events:
        def __getattr__(self, name):
            return lambda *a, **k: None

    cmds = FakeCommands()
    w = PrecisionMassScannerWidget(cmds, Events())
    yield w, cmds
    w.deleteLater()
    app.processEvents()


def telemetry(state="running", run=5, pass_no=1, points=(), done=0, extra=None):
    pts = [list(p) for p in points]
    data = {"sequencer.mass_scan.state": state, "sequencer.mass_scan.run": run, "sequencer.mass_scan.pass": pass_no,
            "sequencer.mass_scan.points": len(pts), "sequencer.mass_scan.tail": [[i] + p for i, p in enumerate(pts)],
            "sequencer.mass_scan.passes_done": done, "sequencer.mass_scan.progress": [len(pts), 10],
            "sequencer.mass_scan.status": "scanning", "sequencer.mass_scan.config": {"export": False},
            "ion_beam.beamline.magnet.permit": 1.0, "ion_beam.beamline.faraday.smu.permit": 1.0,
            "ion_beam.beamline.magnet.stat_enabled": 1.0}
    data.update(extra or {})
    return data


def test_panel_draws_the_sequencers_points_and_analyses_finished_passes(panel):
    w, cmds = panel
    pts = [(24.0 + 0.5 * i, 24.0 + 0.5 * i, 1e-9 * (i + 1), 4.0, 10.0) for i in range(4)]
    w.update_telemetry(telemetry(points=pts[:2]))
    assert w.amps_data == [24.0, 24.5] and not w.btn_start.isEnabled() and w.btn_stop.isEnabled()
    w.update_telemetry(telemetry(points=pts))
    assert len(w.amps_data) == 4 and w.energy_data[0] == 14.0
    assert w.amu_data[0] == pytest.approx(mass_for_current(24.0, 14000.0, w.sp_k_factor.value(),
                                                           w.sp_i_offset.value()))
    cmds.passes[1] = [list(p) for p in pts]
    w.update_telemetry(telemetry(state="complete", points=pts, done=1))
    assert w.last_scan_data == [tuple(p) for p in pts] and w.btn_save_csv.isEnabled()
    assert w.btn_start.isEnabled() and not w.btn_stop.isEnabled()
    fetches = [c for c in cmds.sent if c[1].endswith("cmd_fetch")]
    assert fetches and all(c[3] == "hmi.fetch" for c in fetches)


def test_panel_start_sends_the_settings_and_refuses_without_permits(panel):
    w, cmds = panel
    w.update_telemetry(telemetry(state="idle", run=None, extra={
        "ion_beam.beamline.magnet.permit": 0.0, "ion_beam.beamline.magnet.permit_reason": "magnet is controlled by recipe 'x'"}))
    assert not w.btn_start.isEnabled() and "recipe 'x'" in w.btn_start.toolTip()
    w.update_telemetry(telemetry(state="idle", run=None))
    assert w.btn_start.isEnabled()
    w.sp_start.setValue(20.0)
    w.sp_stop.setValue(30.0)
    w._start_scan()
    _route, tag, value, _origin = cmds.sent[-1]
    assert tag == "sequencer.mass_scan.cmd_start" and value["start_a"] == 20.0 and value["stop_a"] == 30.0
    w.update_telemetry({})                                         # sequencer gone
    assert not w.btn_start.isEnabled() and "sequencer service is not running" in w.btn_start.toolTip()


def test_loop_pass_becomes_the_ghost_trace(panel):
    w, cmds = panel
    pts = [(24.0, 24.0, 1e-9, 4.0, 10.0), (25.0, 25.0, 2e-9, 4.0, 10.0)]
    w.update_telemetry(telemetry(points=pts))
    w.update_telemetry(telemetry(pass_no=2, points=pts[:1], done=1))
    assert w.ghost_amps == [24.0, 25.0] and w.amps_data == [24.0]
    assert math.isclose(w.ghost_y[1], 2e-9)
