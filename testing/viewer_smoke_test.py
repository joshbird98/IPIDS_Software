"""
Functional smoke test for the data viewer (runs offscreen, no services needed).

Checks, against the real DataViewerApp + DualPipelineCache:
  1. live curves cover the visible window
  2. switching channels mid-run: the NEW channels show their full live history
     immediately (every service's tags are buffered, not just plotted ones)
  3. inspector/pin values (_values_at) equal the latest live samples
  4. CSV export: timestamps strictly increasing, every channel populated, values
     consistent with the live buffer
  5. historical view: data loads for a past window and has a legend; markers drawn
  6. export: exact export of a past window equals every logged sample (including a
     channel with no data in that period - this used to crash the export), time-averaged
     bins, figure + standalone make_figure.py, summary + caption, export dialog

Run:  python testing/viewer_smoke_test.py
Exit code 0 = all passed.
"""
import csv
import glob
import json
import os
import sys
import tempfile
import time
from collections import defaultdict

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import numpy as np
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from src.core.data_cache import DualPipelineCache
from src.gui.data_viewer.viewer_main import DataViewerApp

HZ = {"service_plc_snap7": 10, "service_adam_monitor": 10, "service_faraday_smu": 10, "service_magnet": 20,
      "service_steerer_magnet": 20, "service_vacuum": 20, "service_spellman": 10, "service_source_turbo": 20}

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)


def coverage(win, curves):
    v0, v1 = list(win.lanes.values())[0].viewRange()[0]
    out = {}
    for tag, c in curves.items():
        if not c.isVisible() or c.xData is None or len(c.xData) == 0:
            out[tag] = 0.0
            continue
        x = c.xData[np.isfinite(c.yData)] if c.yData is not None else c.xData
        if len(x) == 0:
            out[tag] = 0.0
            continue
        out[tag] = max(0.0, min(v1, x.max()) - max(v0, x.min())) / (v1 - v0)
    return out


def main():
    app = QApplication(sys.argv)
    cache = DualPipelineCache(os.path.join(ROOT, "data"))
    cache.health_timer.stop()
    reg = json.load(open(os.path.join(ROOT, "config", "system_tags.json")))
    by = defaultdict(list)
    for t, i in reg.items():
        by[i.get("source")].append(t)

    win = DataViewerApp(cache, window_title="smoke")
    win.showNormal()
    win.resize(1600, 900)

    def set_channels(tags):
        win.plot_config = {t: {"selected": True, "lane": f"Lane {i % 2 + 1}", "label": t,
                               "scale": "linear", "multiplier": 2.0 if i == 0 else 1.0}
                           for i, t in enumerate(tags)}
        cache.register_client_tags(win.client_id, list(win.plot_config))
        win.request_ui_refresh()

    def export_options(t0, t1, mode, dest, bin_s=10.0, spread=False, screen_svg=False):
        return {"t0": t0, "t1": t1, "mode": mode, "bin_s": bin_s, "spread": spread, "csv": True,
                "parquet": True, "markers": True, "screen_svg": screen_svg, "figure": True,
                "width_cm": 15.0, "lane_height_cm": 4.5, "font": "serif", "time_axis": "elapsed",
                "dest": dest, "name": "snap"}

    def wait_for_export(then, timeout_s=90):
        t_end = time.time() + timeout_s

        def poll():
            w = getattr(win, "export_worker", None)
            if w is not None and w.isFinished():
                then(getattr(win, "last_export_folder", None) if win.btn_export.text() != "❌ Export failed" else None)
                win.last_export_folder = None
            elif time.time() > t_end:
                then(None)
            else:
                QTimer.singleShot(200, poll)
        QTimer.singleShot(300, poll)

    first = [sorted(by["service_plc_snap7"])[0], sorted(by["service_vacuum"])[0],
             sorted(by["service_magnet"])[0], sorted(by["service_adam_monitor"])[0]]
    second = [sorted(by["service_source_turbo"])[0], sorted(by["service_smu" if "service_smu" in by
                                                                 else "service_faraday_smu"])[0],
              sorted(by["service_steerer_magnet"])[0], sorted(by["service_plc_snap7"])[5]]
    set_channels(first)
    QTimer.singleShot(300, lambda: win._set_timespan(0.5))   # 30 s view

    t_start = time.time()
    sent = {s: 0 for s in HZ}

    def feed():
        now = time.time()
        for s, hz in HZ.items():
            due = int((now - t_start) * hz) - sent[s]
            for _ in range(max(0, due)):
                cache.live.ingest(s, now, {t: float(np.sin(now / 3 + k)) for k, t in enumerate(by[s])})
                sent[s] += 1
    feeder = QTimer()
    feeder.timeout.connect(feed)
    feeder.start(20)

    def phase1():
        cov = coverage(win, win.curves)
        check("live curves cover the view", all(v > 0.95 for v in cov.values()) and len(cov) == 4,
              f"{ {k.split('.')[-2] + '.' + k.split('.')[-1]: round(float(v), 2) for k, v in cov.items()} }")
        set_channels(second)
        QTimer.singleShot(700, phase2)   # a few frames after switching

    def phase2():
        cov = coverage(win, win.curves)
        sel = [t for t in second]
        ok = all(cov.get(t, 0) > 0.95 for t in sel)
        check("new channels show full live history right after switching", ok,
              f"{ {k.split('.')[-2] + '.' + k.split('.')[-1]: round(float(cov.get(k, 0)), 2) for k in sel} }")

        # inspector / pin values: the inspector reads the frame on screen
        # (win.current_live), so compare at the newest time in that frame
        shown = win.current_live
        abs_t = min(shown[t][0][-1] for t in sel)
        _, vals = win._values_at(abs_t - win.t0)
        ref = cache.live.window(abs_t - 5, abs_t, sel)
        diffs = [abs(vals[t] - ref[t][1][np.searchsorted(ref[t][0], abs_t, side='right') - 1])
                 for t in sel if t in vals]
        check("inspector values match latest live samples", len(diffs) == len(sel) and max(diffs) < 1e-9,
              f"{len(diffs)}/{len(sel)} channels")

        # export (as displayed, live) - options passed directly, no dialog
        tmp = tempfile.mkdtemp()
        win._export_quiet = True
        v0, v1 = list(win.lanes.values())[0].viewRange()[0]
        win._prepare_export(export_options(v0 + win.t0, v1 + win.t0, "displayed", tmp, screen_svg=True))
        wait_for_export(lambda folder: phase3(folder, sel))

    def phase3(folder, sel):
        names = sorted(os.listdir(folder)) if folder else []
        want = {"data.csv", "data.parquet", "metadata.json", "figure.pdf", "figure.svg", "figure.png",
                "make_figure.py", "screen.svg", "summary.csv", "caption.txt"}
        check("export folder has data, metadata, figure, script, screen snapshot", want <= set(names), str(names))
        if folder and "data.csv" in names:
            with open(os.path.join(folder, "data.csv")) as f:
                rows = list(csv.reader(f))
            hdr, body = rows[0], rows[1:]
            ts = np.array([float(r[0]) for r in body])
            check("export timestamps strictly increasing", len(ts) > 100 and np.all(np.diff(ts) > 0),
                  f"{len(ts)} rows")
            meta = json.load(open(os.path.join(folder, "metadata.json")))
            colmap = {c["tag"]: c["column"] for c in meta["channels"]}
            cols = {h: np.array([float(r[i]) if r[i] not in ("", "nan") else np.nan for r in body])
                    for i, h in enumerate(hdr) if i >= 3}
            filled = {h: float(np.mean(np.isfinite(v))) for h, v in cols.items()}
            check("export: every channel populated", all(v > 0.9 for v in filled.values()),
                  f"{len(filled)} columns")
            errs = []
            for t in sel:
                x, y = cache.live.window(ts[0] - 5, ts[-1], [t])[t]
                for r in np.linspace(0, len(ts) - 1, 7).astype(int):
                    k = np.searchsorted(x, ts[r], side='right') - 1
                    errs.append(abs(y[k] - cols[colmap[t]][r]))
            check("export values match live buffer", max(errs) < 1e-9, f"{len(errs)} cells, max err {max(errs):.2e}")

        # historical: park on July data and wait for the query
        feeder.stop()
        win._toggle_scroll_lock(manual_break=True)
        end = time.mktime((2026, 7, 10, 12, 0, 0, 0, 0, -1))
        set_channels(sorted(by["service_plc_snap7"])[10:14])
        QTimer.singleShot(300, lambda: list(win.lanes.values())[0].setXRange(end - 6 * 3600 - win.t0,
                                                                              end - win.t0, padding=0))
        QTimer.singleShot(3000, phase4)

    def phase4():
        n = {t: (0 if c.xData is None else int(np.isfinite(c.yData).sum())) for t, c in win.hist_curves.items()}
        check("historical data loaded for a past window", len(n) > 0 and all(v > 100 for v in n.values()),
              f"{ {k.split('.')[-1]: v for k, v in n.items()} }")
        # markers: every USER_MARKER in the DB inside the view is drawn
        db = os.path.join(ROOT, "data", "events.db")
        if os.path.exists(db):
            import sqlite3
            v0, v1 = list(win.lanes.values())[0].viewRange()[0]
            win._refresh_event_markers()
            with sqlite3.connect(db) as conn:
                expect = {f"{r[0]:.3f}" for r in conn.execute(
                    "SELECT timestamp FROM events WHERE event_type='USER_MARKER' AND timestamp BETWEEN ? AND ?",
                    (v0 + win.t0, v1 + win.t0))}
            shown = {k for k in win.marker_items if v0 <= float(k) - win.t0 <= v1}
            check("user markers in view are drawn", expect == shown, f"{len(shown)}/{len(expect)}")
        legend_items = sum(len(l.items) for l in win.lane_legends.values())
        check("legend present without live data", legend_items == len(win.plot_config), f"{legend_items} entries")

        # historical EXACT export incl. a channel with no data in July (crashed the original export)
        set_channels(sorted(by["service_magnet"])[:2] + ["ion_beam.beamline.steerer_magnet.rb_current"])
        QTimer.singleShot(1500, phase5)

    def phase5():
        end = time.mktime((2026, 7, 10, 12, 0, 0, 0, 0, -1))
        tmp = tempfile.mkdtemp()
        win._prepare_export(export_options(end - 2 * 3600, end, "exact", tmp))
        wait_for_export(lambda folder: phase6(folder, end))

    def phase6(folder, end):
        import polars as pl
        ok = folder is not None and os.path.exists(os.path.join(folder, "data.parquet"))
        check("historical exact export succeeds with a channel that has no data", ok, str(folder))
        if ok:
            d = pl.read_parquet(os.path.join(folder, "data.parquet"))
            meta = json.load(open(os.path.join(folder, "metadata.json")))
            missing_tag = "ion_beam.beamline.steerer_magnet.rb_current"
            present = [c for c in meta["channels"] if c["tag"] != missing_tag]
            raw = pl.read_parquet(os.path.join(ROOT, "data/daily_logs/day_20260710.parquet"),
                                  columns=["timestamp"] + [c["tag"] for c in present]).filter(
                (pl.col("timestamp") >= end - 2 * 3600) & (pl.col("timestamp") <= end)).sort("timestamp")
            same = d.height == raw.height and all(
                np.allclose(d[c["column"]].to_numpy(), raw[c["tag"]].cast(pl.Float64).to_numpy(), equal_nan=True)
                for c in present)
            check("exact export == every logged sample in the day file", same, f"{d.height} vs {raw.height} rows")
            miss = next(c["column"] for c in meta["channels"] if c["tag"] == missing_tag)
            check("channel without data exported as empty column", d[miss].null_count() == d.height, miss)
            # the standalone figure script regenerates the figure from the export folder
            import subprocess
            for f_ in ("figure.pdf", "figure.png", "figure.svg"):
                os.remove(os.path.join(folder, f_))
            r = subprocess.run([sys.executable, os.path.join(folder, "make_figure.py"), "--formats", "pdf,png"],
                               capture_output=True, text=True, timeout=120)
            check("make_figure.py regenerates the figure on its own", r.returncode == 0 and
                  os.path.exists(os.path.join(folder, "figure.pdf")), (r.stderr or "")[-200:])
        tmp = tempfile.mkdtemp()
        win._prepare_export(export_options(end - 6 * 3600, end, "mean", tmp, bin_s=60.0, spread=True))
        wait_for_export(lambda folder: phase7(folder))

    def phase7(folder):
        import polars as pl
        ok = folder is not None
        if ok:
            d = pl.read_parquet(os.path.join(folder, "data.parquet"))
            dt = np.diff(d["Timestamp_UNIX"].to_numpy())
            ok = d.height == 360 and np.allclose(dt, 60.0) and any(c.endswith(" std") for c in d.columns)
        check("time-averaged export: 6 h at 60 s = 360 bins with spread columns", ok,
              f"{d.height if folder else 0} rows")
        # the dialog itself builds and returns sane options
        from src.gui.data_viewer.export_dialog import ExportDialog
        dlg = ExportDialog(win, 1.0e9, 1.0e9 + 3600, 4, 0.05, 1000, tempfile.gettempdir(), "x")
        o = dlg.options()
        check("export dialog defaults", abs(o["t1"] - o["t0"] - 3600) < 1e-3 and o["bin_s"] > 0, str(o["mode"]))

        # the real button: click -> dialog -> export (dialog auto-accepted with test options)
        import src.gui.data_viewer.export_dialog as ed
        tmp = tempfile.mkdtemp()
        end = time.mktime((2026, 7, 10, 12, 0, 0, 0, 0, -1))
        shown = []
        ed.ExportDialog.exec = lambda self: (shown.append(1), 1)[1]
        ed.ExportDialog.options = lambda self: export_options(end - 600, end, "exact", tmp)
        win.btn_export.click()
        wait_for_export(lambda folder: phase8(folder, shown))

    def phase8(folder, shown):
        check("clicking the Export button opens the dialog and exports", bool(shown) and folder is not None
              and os.path.exists(os.path.join(folder, "figure.pdf")), str(folder))
        app.quit()

    QTimer.singleShot(40_000, phase1)    # let 40 s of live data accumulate
    app.exec()
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    os._exit(1 if failed else 0)


if __name__ == "__main__":
    main()
