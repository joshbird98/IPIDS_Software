"""
Headless test harness for the real DataViewerApp (src/gui/data_viewer/viewer_main.py).

Runs the actual viewer offscreen (no display needed), feeds it synthetic live
telemetry by calling DualPipelineCache._on_live_data at a realistic aggregate
message rate (no ZMQ services required), and every second checks how much of
the visible window each live curve actually covers. Optionally saves PNG
snapshots of the window and logs RSS memory.

Examples
--------
  # Fast wrap test: small ring buffer so it wraps every ~3 minutes
  python testing/headless_viewer_harness.py --cap 20000 --span 30 --duration 300

  # Faithful run: production buffer size, 20 minutes
  python testing/headless_viewer_harness.py --duration 1200

Works on Windows too (omit nothing; offscreen is forced via QT_QPA_PLATFORM).
"""
import argparse
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import numpy as np
import psutil
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QTimer

from src.core.data_cache import DualPipelineCache
from src.gui.data_viewer.viewer_main import DataViewerApp


def parse():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap", type=int, default=100_000, help="live ring buffer capacity")
    ap.add_argument("--rate", type=float, default=110.0, help="aggregate ZMQ messages/s")
    ap.add_argument("--span", type=float, default=300.0, help="visible window (s)")
    ap.add_argument("--duration", type=float, default=300.0, help="test length (s)")
    ap.add_argument("--n-tags", type=int, default=8, help="channels to plot")
    ap.add_argument("--lanes", type=int, default=2)
    ap.add_argument("--snap-dir", default=None, help="save PNGs here")
    ap.add_argument("--snap-every", type=float, default=15.0)
    ap.add_argument("--label", default="")
    ap.add_argument("--log-lane", action="store_true",
                    help="put the last lane on log scale with pressure-like values")
    return ap.parse_args()


def main():
    a = parse()
    app = QApplication(sys.argv)
    proc = psutil.Process()

    cache = DualPipelineCache(os.path.join(ROOT, "data"))
    # Do NOT start the ZMQ listener: we feed the cache directly.
    cache.health_timer.stop()  # keep stdout clean; we log our own stats

    if a.cap != cache.live_capacity:
        cache.live_capacity = a.cap
        cache._x_live = np.zeros(a.cap, dtype=np.float64)
        cache._y_live = {k: np.zeros(a.cap, dtype=np.float64) for k in cache.engine.channel_keys}

    keys = cache.engine.channel_keys
    if not keys:
        sys.exit("No channel keys: run src/utils/build_registry.py first to create config/system_tags.json")

    win = DataViewerApp(cache, window_title="harness")

    # Select a handful of channels across lanes
    plot_tags = keys[: a.n_tags]
    cfg = {}
    log_tags = set()
    for i, t in enumerate(plot_tags):
        lane_no = i % a.lanes + 1
        is_log = a.log_lane and lane_no == a.lanes
        if is_log:
            log_tags.add(t)
        cfg[t] = {"selected": True, "lane": f"Lane {lane_no}", "label": t.split(".")[-1],
                  "scale": "log" if is_log else "linear", "multiplier": 1.0}
    win.plot_config = cfg
    cache.register_client_tags(win.client_id, list(cfg.keys()))
    win.request_ui_refresh()
    QTimer.singleShot(300, lambda: win._set_timespan(a.span / 60.0))

    # --- synthetic publisher: one cache row per "message", like ZMQ ---
    t_start = time.time()
    sent = [0]
    phases = {t: np.random.rand() * 6.28 for t in plot_tags}

    def feed():
        now = time.time()
        due = int((now - t_start) * a.rate) - sent[0]
        for _ in range(max(0, due)):
            msg = {}
            for t in plot_tags:
                s = float(np.sin(now / 5.0 + phases[t]) + 0.05 * np.random.randn())
                msg[t] = 10 ** (-6 + s) if t in log_tags else s
            cache._on_live_data(msg)
            sent[0] += 1

    feeder = QTimer()
    feeder.timeout.connect(feed)
    feeder.start(20)

    # --- checker ---
    worst = {"cov": 1.0, "blank_s": 0}
    last_snap = [0.0]
    if a.snap_dir:
        os.makedirs(a.snap_dir, exist_ok=True)

    def coverage():
        if not win.lanes:
            return None
        v0, v1 = list(win.lanes.values())[0].viewRange()[0]
        covs = []
        for tag, c in win.curves.items():
            if not c.isVisible() or c.xData is None or len(c.xData) == 0:
                covs.append(0.0)
                continue
            x = c.xData
            lo, hi = max(v0, np.nanmin(x)), min(v1, np.nanmax(x))
            covs.append(max(0.0, hi - lo) / (v1 - v0))
        return min(covs) if covs else None

    def check():
        el = time.time() - t_start
        cov = coverage()
        # Only judge once the buffer holds more than one view-span of data
        settled = sent[0] / a.rate > a.span * 1.1
        rss = proc.memory_info().rss / 2**20
        flag = ""
        if cov is not None and settled:
            worst["cov"] = min(worst["cov"], cov)
            if cov < 0.05:
                worst["blank_s"] += 1
                flag = "  <-- LINES GONE"
            elif cov < 0.9:
                flag = "  <-- partial"
        if int(el) % 5 == 0 or flag:
            print(f"{a.label} t={el:6.1f}s ptr={cache.live_ptr:7d} wrapped={cache.buffer_wrapped!s:5} "
                  f"coverage={'' if cov is None else f'{cov:4.0%}':>4} rss={rss:7.1f}MB{flag}", flush=True)
        if a.snap_dir and el - last_snap[0] >= a.snap_every:
            last_snap[0] = el
            win.grab().save(os.path.join(a.snap_dir, f"{a.label or 'snap'}_{int(el):05d}s.png"))
        if el >= a.duration:
            print(f"\n{a.label} RESULT: worst coverage after settling = {worst['cov']:.0%}, "
                  f"seconds fully blank = {worst['blank_s']}, final RSS = {rss:.0f} MB", flush=True)
            app.quit()

    checker = QTimer()
    checker.timeout.connect(check)
    checker.start(1000)

    app.exec()
    os._exit(0)  # skip slow teardown of background threads


if __name__ == "__main__":
    main()
