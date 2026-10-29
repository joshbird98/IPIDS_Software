"""Data viewer event markers against the simulated stack: saved markers reach events.db and
exports (figure included, even after the newest sample); unsaved ones are flagged; deleting is a
deliberate right-click choice; zoomed out, nearby events are drawn as one labelled group."""
import glob
import json
import os
import pathlib
import time

import pytest

from src.core import paths
from tests.sim_stack import start_stack, stop_stack
from tests.test_hmi_sim import pump, wait_for

pytestmark = pytest.mark.slow

TAGS = ["ion_beam.source.cesium.rb_temp", "ion_beam.facilities.safety_relay_active"]


@pytest.fixture(scope="module")
def app():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def stack():
    # the viewer in this process reads paths.EVENTS_DB, so the stack writes into the same folder
    os.makedirs(paths.DATA_DIR, exist_ok=True)
    s = start_stack(pathlib.Path(paths.DATA_DIR))
    yield s
    stop_stack(s)


@pytest.fixture(scope="module")
def viewer(stack, app):
    from src.core.data_cache import DualPipelineCache
    from src.gui.data_viewer.viewer_main import DataViewerApp
    cache = DualPipelineCache(paths.DATA_DIR)
    cache.start()
    w = DataViewerApp(cache)
    w.plot_config = {t: {"selected": True, "favorite": False, "color": "#FF0000", "lane": "Lane 1", "label": t,
                         "scale": "linear", "multiplier": 1.0} for t in TAGS}
    cache.register_client_tags(w.client_id, TAGS)
    w.request_ui_refresh()
    w.show()
    wait_for(app, lambda: w.lanes and len(cache.tags) == len(TAGS), 20, "viewer lanes")
    pump(app, 3)
    yield w
    w.close()
    cache.stop()


def _db_has(ts):
    import sqlite3
    con = sqlite3.connect(paths.EVENTS_DB)
    try:
        return con.execute("SELECT count(*) FROM events WHERE event_type='USER_MARKER' AND abs(timestamp-?)<0.002",
                           (ts,)).fetchone()[0] > 0
    finally:
        con.close()


def test_marker_saved_and_exported_even_after_the_newest_sample(viewer, app, tmp_path):
    ts = time.time() + 4.0                       # beyond the newest data row when exporting
    viewer._save_markers(ts, {"text": "beam tuned", "color": "#00E5FF"})
    wait_for(app, lambda: _db_has(ts), 10, "marker in events.db")
    viewer._export_quiet = True
    viewer._prepare_export({"t0": ts - 30, "t1": ts + 2, "mode": "exact", "bin_s": 1.0, "spread": False,
                            "csv": True, "parquet": False, "markers": True, "screen_svg": False, "figure": True,
                            "width_cm": 15, "lane_height_cm": 4.5, "font": "sans", "time_axis": "elapsed",
                            "dest": str(tmp_path), "name": "m"})
    wait_for(app, lambda: not viewer.export_worker.isRunning(), 90, "export finished")
    pump(app, 0.5)
    folder = glob.glob(str(tmp_path / "m_*"))[0]
    meta = json.load(open(os.path.join(folder, "metadata.json")))
    assert [m["text"] for m in meta["markers"]] == ["beam tuned"]
    with open(os.path.join(folder, "figure.svg"), encoding="utf-8") as f:
        assert "beam tuned" in f.read()          # drawn although after the last data row


def test_unsaved_marker_is_flagged_and_removed(viewer, app, monkeypatch):
    warned = []
    monkeypatch.setattr(viewer, "_marker_in_db", lambda ts: False)
    monkeypatch.setattr(viewer, "_marker_save_failed", lambda text: warned.append(text))
    ts = time.time() - 5
    key = f"{ts:.3f}"
    viewer._pending_markers[key] = {"text": "lost", "color": "#fff", "type": "USER_MARKER"}
    viewer.event_data[key] = viewer._pending_markers[key]
    viewer._verify_marker_saved(ts, {"text": "lost"}, attempt=3)
    assert warned == ["lost"] and key not in viewer.event_data and key not in viewer._pending_markers


def test_delete_from_the_menu_choice(viewer, app):
    ts = time.time() - 2
    viewer._save_markers(ts, {"text": "delete me", "color": "#00E5FF"})
    wait_for(app, lambda: _db_has(ts), 10, "marker saved")
    viewer._delete_marker(f"{ts:.3f}")
    wait_for(app, lambda: not _db_has(ts), 10, "marker deleted")


def test_zoomed_out_events_are_grouped_and_labels_do_not_overlap(viewer, app):
    from src.gui.data_viewer import event_overlay as eo
    base = list(viewer.lanes.values())[0]
    if viewer.auto_scroll:
        viewer._toggle_scroll_lock(manual_break=True)
    now = time.time() - viewer.t0
    events = {f"{viewer.t0 + now - 100 + i * 0.05:.3f}": {"text": f"burst {i}", "color": "#F44336", "type": "FAULT"}
              for i in range(200)}
    events.update({f"{viewer.t0 + now - 50 + i * 4:.3f}": {"text": f"marker number {i} with a long description",
                                                             "color": "#00E5FF", "type": "USER_MARKER"}
                   for i in range(10)})
    base.setXRange(now - 120, now, padding=0)
    pump(app, 0.3)
    ov = viewer.event_overlay
    ov.update(list(viewer.lanes.values()), None, events, viewer.t0, base.viewRange()[0])
    groups = [it for it in ov.items.values() if len(it["members"]) > 1]
    assert any(len(it["members"]) >= 100 for it in groups)        # the burst is one line
    assert len(ov.items) < 60
    shown = [it["lines"][0].label for it in ov.items.values() if it["lines"][0].label.isVisible()]
    rects = [lab.sceneBoundingRect() for lab in shown]
    for i, a in enumerate(rects):
        for b in rects[i + 1:]:
            assert not a.intersects(b.adjusted(1, 1, -1, -1)), "labels overlap"
    assert all(len(it["text"]) <= eo.MAX_LABEL_CHARS for it in ov.items.values())


def test_zooming_out_stops_at_the_data_plus_ten_percent(viewer, app):
    """A quick zoom-out used to run on to a century of empty axis."""
    base = list(viewer.lanes.values())[0]
    if viewer.auto_scroll:
        viewer._toggle_scroll_lock(manual_break=True)
    base.setXRange(-100 * 365 * 86400, 0, padding=0)
    pump(app, 0.5)
    x0, x1 = base.viewRange()[0]
    lo, hi = viewer.time_limits()
    assert x0 >= lo - 1 and x1 <= hi + 1 and (x1 - x0) <= (hi - lo) + 1
    assert (x1 - x0) < 365 * 86400                     # (the simulator's record is minutes long)


def test_line_width_from_the_view_menu(viewer, app):
    """Chosen in View > Line width, applied to the lines drawn, remembered for next time."""
    from src.gui.settings import gui_settings
    pens = lambda: {c.opts["pen"].widthF() for c in [*viewer.curves.values(), *viewer.hist_curves.values()]}  # noqa: E731
    assert viewer.line_width_actions["Thin (fastest)"].isChecked() and pens() == {1.0}
    try:
        viewer.line_width_actions["Thick"].trigger()
        pump(app, 1)
        assert pens() == {2.0} and viewer.RENDER_LIMIT == 3000
        assert gui_settings("SINDIE", "DataViewer").value("view/line_width") == "Thick"
    finally:
        viewer.line_width_actions["Thin (fastest)"].trigger()
    assert pens() == {1.0} and viewer.RENDER_LIMIT == 10000
