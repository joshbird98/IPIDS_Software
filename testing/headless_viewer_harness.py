"""
Headless test harness for the real DataViewerApp (src/gui/data_viewer/viewer_main.py).

Runs the actual viewer offscreen (no display needed), feeds it synthetic live
telemetry the way the real services publish it (each service its own tags at its own rate),
(directly, or over real ZMQ with --zmq), and every second checks how much of
the visible window each live curve actually covers. Optionally saves PNG
snapshots of the window and logs RSS memory.

Examples
--------
  # Fast wrap test: keep only 60 s per source so every ring wraps within ~1 min
  python testing/headless_viewer_harness.py --keep 60 --span 30 --duration 300

  # Same, but publish over real ZMQ sockets so the listener thread is exercised
  python testing/headless_viewer_harness.py --keep 60 --span 30 --duration 120 --zmq

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
    ap.add_argument("--keep", type=float, default=None,
                    help="live seconds kept per source (default: the cache's LIVE_KEEP_SECONDS)")
    ap.add_argument("--initial-cap", type=int, default=None,
                    help="starting rows per source ring (small values exercise growth + wrap quickly)")
    ap.add_argument("--rate", type=float, default=1.0,
                    help="scale on the real per-service publish rates (0 = no live data)")
    ap.add_argument("--zmq", action="store_true",
                    help="publish over real ZMQ PUB sockets on the configured ports (tests the listener)")
    ap.add_argument("--span", type=float, default=300.0, help="visible window (s)")
    ap.add_argument("--duration", type=float, default=300.0, help="test length (s)")
    ap.add_argument("--n-tags", type=int, default=8, help="channels to plot")
    ap.add_argument("--lanes", type=int, default=2)
    ap.add_argument("--snap-dir", default=None, help="save PNGs here")
    ap.add_argument("--snap-every", type=float, default=15.0)
    ap.add_argument("--label", default="")
    ap.add_argument("--hist-end", default=None,
                    help="ISO datetime: park the view on historical data ending here (use with --rate 0)")
    ap.add_argument("--exp", default="", help="comma list of experiments: noupdate,noaa,norender")
    ap.add_argument("--bench-redraw", action="store_true",
                    help="after data loads, time 60 small pans (each = one full redraw) and exit")
    ap.add_argument("--profile", default=None, help="write cProfile stats of the event loop here")
    ap.add_argument("--size", default="1920x1080", help="window size WxH (offscreen)")
    ap.add_argument("--zoom-every", type=float, default=0.0,
                    help="simulate a user zoom/pan step every N seconds (historical mode)")
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

    if a.keep is not None:
        cache.live.keep_seconds = a.keep
    if a.initial_cap is not None:
        cache.live.initial_capacity = a.initial_cap

    keys = cache.engine.channel_keys
    if not keys:
        sys.exit("No channel keys: run src/utils/build_registry.py first to create config/system_tags.json")

    if "grid0" in a.exp:
        DataViewerApp.GRID_TICK_LEVEL = 0
    win = DataViewerApp(cache, window_title="harness")
    w_, h_ = (int(v) for v in a.size.lower().split("x"))
    win.showNormal()
    win.resize(w_, h_)

    exps = set(filter(None, a.exp.split(",")))
    if "noaa" in exps:
        import pyqtgraph as _pg
        _pg.setConfigOptions(antialias=False)
    if "noupdate" in exps:
        win.layout_widget.scene().update = lambda *args: None
    # count real scene redraws per second
    _paints = [0]
    _orig_paint = type(win.layout_widget).paintEvent
    def _counting_paint(self_, ev, _o=_orig_paint):
        _paints[0] += 1
        return _o(self_, ev)
    type(win.layout_widget).paintEvent = _counting_paint
    from PyQt6.QtWidgets import QGraphicsView as _QGV
    for _name, _mode in (("fullvp", "FullViewportUpdate"), ("brvp", "BoundingRectViewportUpdate"),
                         ("minvp", "MinimalViewportUpdate"), ("smartvp", "SmartViewportUpdate")):
        if _name in exps:
            win.layout_widget.setViewportUpdateMode(getattr(_QGV.ViewportUpdateMode, _mode))
    if "fps5" in exps:
        win.render_timer.setInterval(200)
    if "inspector" in exps:
        QTimer.singleShot(1500, lambda: win.btn_inspector.setChecked(True))
    if "ylock" in exps:
        QTimer.singleShot(1500, win._toggle_y_lock)
    if "norender" in exps:
        win.render_timer.stop()

    # Select a handful of channels across lanes
    # Real services publish only their own tags at their own rate. Group registry
    # tags by 'source' and use each service's POLL_INTERVAL.
    import json as _json
    from collections import defaultdict as _dd
    with open(os.path.join(ROOT, "config", "system_tags.json")) as _f:
        _reg = _json.load(_f)
    by_source = _dd(list)
    for _t, _info in _reg.items():
        by_source[_info.get("source", "unknown")].append(_t)
    SOURCE_HZ = {"service_plc_snap7": 10, "service_adam_monitor": 10, "service_faraday_smu": 10,
                 "service_magnet": 20, "service_steerer_magnet": 20, "service_vacuum": 20,
                 "service_spellman": 10, "service_source_turbo": 20}
    from src.core import network_map as _nm
    SOURCE_ZMQ = {"service_plc_snap7": (_nm.ZMQ_PORT_PLC_PUB, _nm.TOPIC_PLC_DATA),
                  "service_adam_monitor": (_nm.ZMQ_PORT_ADAM_PUB, _nm.TOPIC_ADAM_DATA),
                  "service_faraday_smu": (_nm.ZMQ_PORT_SMU_PUB, _nm.TOPIC_SMU_DATA),
                  "service_magnet": (_nm.ZMQ_PORT_MAGNET_PUB, _nm.TOPIC_MAGNET_DATA),
                  "service_steerer_magnet": (_nm.ZMQ_PORT_STEERER_MAGNET_PUB, _nm.TOPIC_STEERER_MAGNET_DATA),
                  "service_vacuum": (_nm.ZMQ_PORT_VACUUM_PUB, _nm.TOPIC_VACUUM_DATA),
                  "service_spellman": (_nm.ZMQ_PORT_SPELLMAN_PUB, _nm.TOPIC_SPELLMAN_DATA),
                  "service_source_turbo": (_nm.ZMQ_PORT_SRC_TURBO_PUB, _nm.TOPIC_SRC_TURBO_DATA)}
    # Plot channels drawn round-robin from different services
    _iters = [sorted(v) for _, v in sorted(by_source.items())]
    plot_tags = []
    _k = 0
    while len(plot_tags) < a.n_tags and any(_iters):
        _lst = _iters[_k % len(_iters)]
        if _lst:
            plot_tags.append(_lst.pop(0))
        _k += 1
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

    # --- synthetic publishers: one message per service tick, that service's tags only ---
    t_start = time.time()
    sent = [0]
    phases = {t: np.random.rand() * 6.28 for t in _reg}
    src_sent = {src: 0 for src in by_source}

    def make_msg(src, now):
        msg = {}
        for t in by_source[src]:
            v = float(np.sin(now / 5.0 + phases[t]) + 0.05 * np.random.randn())
            msg[t] = 10 ** (-6 + v) if t in log_tags else v
        return msg

    if a.zmq and a.rate > 0:
        import threading as _th
        import zmq as _zmq
        import orjson as _oj
        _ctx = _zmq.Context.instance()
        _pubs = {}
        for src, (addr, topic) in SOURCE_ZMQ.items():
            sock = _ctx.socket(_zmq.PUB)
            sock.bind(addr)
            _pubs[src] = (sock, topic if isinstance(topic, bytes) else topic.encode())
        time.sleep(0.3)
        cache.start()   # the real listener thread (subscribes to all ports)

        def zmq_publisher():
            while True:
                now = time.time()
                for src, hz in SOURCE_HZ.items():
                    due = int((now - t_start) * hz * a.rate) - src_sent[src]
                    for _ in range(max(0, due)):
                        sock, topic = _pubs[src]
                        sock.send_multipart([topic, _oj.dumps(make_msg(src, now))])
                        src_sent[src] += 1
                        sent[0] += 1
                time.sleep(0.005)
        _th.Thread(target=zmq_publisher, daemon=True).start()

    def feed():
        now = time.time()
        for src, hz in SOURCE_HZ.items():
            due = int((now - t_start) * hz * a.rate) - src_sent[src]
            for _ in range(max(0, due)):
                cache.live.ingest(src, now, make_msg(src, now))
                src_sent[src] += 1
                sent[0] += 1

    feeder = QTimer()
    feeder.timeout.connect(feed)
    if a.rate > 0 and not a.zmq:
        feeder.start(20)

    # --- historical view + simulated zooming ---
    from datetime import datetime as _dt
    zoom_spans_h = [1, 6, 24, 72, 24 * 7, 24, 6, 1, 0.25]
    zstate = {"i": 0, "end": None}
    if a.hist_end:
        zstate["end"] = _dt.fromisoformat(a.hist_end).timestamp()

        def park():
            win._toggle_scroll_lock(manual_break=True)
            bp = list(win.lanes.values())[0]
            bp.setXRange(zstate["end"] - 3600 - win.t0, zstate["end"] - win.t0, padding=0)
        QTimer.singleShot(800, park)

    def zoom_step():
        if not win.lanes or zstate["end"] is None:
            return
        span = zoom_spans_h[zstate["i"] % len(zoom_spans_h)] * 3600
        zstate["i"] += 1
        bp = list(win.lanes.values())[0]
        # emulate a wheel-zoom gesture: several small range changes, like Qt delivers
        v0, v1 = bp.viewRange()[0]
        c1 = zstate["end"] - win.t0
        for k in range(1, 6):
            w = (v1 - v0) + ((span) - (v1 - v0)) * k / 5
            bp.setXRange(c1 - w, c1, padding=0)
            app.processEvents()

    zoomer = QTimer()
    zoomer.timeout.connect(zoom_step)
    if a.zoom_every > 0:
        zoomer.start(int(a.zoom_every * 1000))

    # query accounting
    qstats = {"req": 0, "applied": 0}
    _orig_req = cache.request_historical_window

    def _req(*args, **kw):
        qstats["req"] += 1
        return _orig_req(*args, **kw)
    cache.request_historical_window = _req
    _orig_apply = win._refresh_plot_historical

    def _apply(ts, vals, req_id=None):
        if req_id is None or req_id == win.pending_hist_id:
            qstats["applied"] += 1
        return _orig_apply(ts, vals, req_id)
    cache.historical_updated.disconnect(win._refresh_plot_historical)
    cache.historical_updated.connect(_apply)

    # event-loop lag probe: a 20 ms timer; lateness = GUI thread was busy
    lag = {"last": time.perf_counter(), "samples": []}

    def probe():
        now = time.perf_counter()
        lag["samples"].append((now - lag["last"] - 0.020) * 1000)
        lag["last"] = now
    prober = QTimer()
    prober.timeout.connect(probe)
    prober.start(20)
    proc.cpu_percent(None)

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
        settled = a.rate > 0 and (time.time() - t_start) > a.span * 1.1
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
            ls = np.array(lag["samples"]) if lag["samples"] else np.zeros(1)
            lag["samples"] = []
            _st = cache.live.stats()
            _span = min((r[3] for r in _st), default=0.0)
            print(f"{a.label} t={el:6.1f}s msgs={sent[0]:7d} live_span={_span:6.1f}s live_MB={cache.live.nbytes()/2**20:5.1f} "
                  f"coverage={'' if cov is None else f'{cov:4.0%}':>4} rss={rss:7.1f}MB "
                  f"cpu={proc.cpu_percent(None):5.1f}% loop_lag p95={np.percentile(ls, 95):5.0f}ms "
                  f"max={ls.max():5.0f}ms paints/s={_paints[0] / max(el, 1e-9):4.1f} "
                  f"queries req={qstats['req']} shown={qstats['applied']}{flag}",
                  flush=True)
        if a.snap_dir and el - last_snap[0] >= a.snap_every:
            last_snap[0] = el
            win.grab().save(os.path.join(a.snap_dir, f"{a.label or 'snap'}_{int(el):05d}s.png"))
        if el >= a.duration:
            print(f"\n{a.label} RESULT: worst coverage after settling = {worst['cov']:.0%}, "
                  f"seconds fully blank = {worst['blank_s']}, final RSS = {rss:.0f} MB", flush=True)
            app.quit()

    def bench_redraw():
        for _p in win.lanes.values():
            if "nogrid" in exps:
                _p.showGrid(x=False, y=False)
            for _lvl in (0, 1):
                if f"tick{_lvl}" in exps:
                    for _ax in ("bottom", "left"):
                        _p.getAxis(_ax).setStyle(maxTickLevel=_lvl)
            if "solidgrid" in exps:
                _p.showGrid(x=True, y=True, alpha=1.0)
        if "nolegend" in exps:
            for _l in win.lane_legends.values():
                _l.hide()
        app.processEvents()
        bp = list(win.lanes.values())[0]
        v0, v1 = bp.viewRange()[0]
        step = (v1 - v0) * 0.005
        times = []
        for k in range(60):
            t = time.perf_counter()
            bp.setXRange(v0 + k * step, v1 + k * step, padding=0)
            win.layout_widget.repaint()  # synchronous full redraw
            app.processEvents()
            times.append((time.perf_counter() - t) * 1000)
        if a.snap_dir:
            os.makedirs(a.snap_dir, exist_ok=True)
            win.grab().save(os.path.join(a.snap_dir, f"{a.label}_after_bench.png"))
        ts_ = np.array(times[5:])
        npts = sum(len(c.xData) for c in win.hist_curves.values() if c.xData is not None)
        print(f"{a.label} REDRAW: mean {ts_.mean():.1f} ms, p95 {np.percentile(ts_, 95):.1f} ms "
              f"({len(win.hist_curves)} curves, {npts} pts loaded)", flush=True)
        app.quit()
    if a.bench_redraw:
        QTimer.singleShot(8000, bench_redraw)

    checker = QTimer()
    checker.timeout.connect(check)
    checker.start(1000)

    if a.profile:
        import cProfile
        pr = cProfile.Profile()
        pr.enable()
        app.exec()
        pr.disable()
        pr.dump_stats(a.profile)
    else:
        app.exec()
    os._exit(0)  # skip slow teardown of background threads


if __name__ == "__main__":
    main()
