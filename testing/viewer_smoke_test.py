"""
Functional smoke test for the data viewer (runs offscreen, no services needed).

Checks, against the real DataViewerApp + DualPipelineCache:
  1. live curves cover the visible window
  2. switching channels mid-run: the NEW channels show their full live history
     immediately (every service's tags are buffered, not just plotted ones)
  3. inspector/pin values (_values_at) equal the latest live samples
  4. CSV export: timestamps strictly increasing, every channel populated, values
     consistent with the live buffer
  5. historical view: data loads for a past window and has a legend

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

        # export
        tmp = tempfile.mkdtemp()
        win.edit_export_path.setText(os.path.join(tmp, "snap"))
        win._prepare_export()
        QTimer.singleShot(2500, lambda: phase3(tmp, sel))

    def phase3(tmp, sel):
        files = glob.glob(os.path.join(tmp, "snap_*.csv"))
        ok_file = len(files) == 1 and len(glob.glob(os.path.join(tmp, "snap_*.svg"))) == 1
        check("export wrote CSV + SVG", ok_file, str([os.path.basename(f) for f in os.listdir(tmp)]))
        if files:
            with open(files[0]) as f:
                rows = list(csv.reader(f))
            hdr, body = rows[0], rows[1:]
            ts = np.array([float(r[0]) for r in body])
            check("export timestamps strictly increasing", len(ts) > 100 and np.all(np.diff(ts) > 0),
                  f"{len(ts)} rows")
            cols = {h: np.array([float(r[i]) if r[i] not in ("", "nan") else np.nan for r in body])
                    for i, h in enumerate(hdr) if i >= 2}
            filled = {h: float(np.mean(np.isfinite(v))) for h, v in cols.items()}
            check("export: every channel populated", all(v > 0.9 for v in filled.values()),
                  f"{ {k: round(v, 2) for k, v in filled.items()} }")
            # spot-check every channel at several rows against the live buffer (step-hold)
            errs = []
            for t in sel:
                x, y = cache.live.window(ts[0] - 5, ts[-1], [t])[t]
                for r in np.linspace(0, len(ts) - 1, 7).astype(int):
                    k = np.searchsorted(x, ts[r], side='right') - 1
                    errs.append(abs(y[k] - cols[t][r]))
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
        legend_items = sum(len(l.items) for l in win.lane_legends.values())
        check("legend present without live data", legend_items == len(win.plot_config), f"{legend_items} entries")
        app.quit()

    QTimer.singleShot(40_000, phase1)    # let 40 s of live data accumulate
    app.exec()
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    os._exit(1 if failed else 0)


if __name__ == "__main__":
    main()
