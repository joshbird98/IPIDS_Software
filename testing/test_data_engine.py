import sys
import time
import os
import numpy as np
import polars as pl
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QTimer
import pyqtgraph as pg

from src.core.data_engine import TimeSeriesEngine
from src.core.data_cache import InfiniteDataCache

LOG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '../data'))


def run_benchmarks():
    print("=" * 60)
    print("1. TESTING RAW POLARS QUERY SPEED & DATA QUALITY")
    print("=" * 60)

    # We need a single QApplication for both the visual test and the headless test
    app = QApplication.instance() or QApplication(sys.argv)

    engine = TimeSeriesEngine(LOG_DIR)

    start_ts, end_ts = engine.get_global_bounds()
    if start_ts is None:
        print("[!] No Parquet files found in directory. Run your logger first!")
        return

    duration = end_ts - start_ts
    print(f"-> Found {duration:.1f} seconds of total data on disk.")

    # --- TEST 1: Full Query & Memory Load ---
    t0 = time.perf_counter()
    ts_array, vals_dict = engine.query(start_ts, end_ts)
    t1 = time.perf_counter()

    query_time_ms = (t1 - t0) * 1000
    rows = len(ts_array)
    cols = len(vals_dict)

    bytes_used = ts_array.nbytes + sum(arr.nbytes for arr in vals_dict.values())
    mb_used = bytes_used / (1024 * 1024)

    print(f"[SUCCESS] Loaded {rows} rows x {cols} columns in {query_time_ms:.2f} ms.")
    print(f"[METRIC]  RAM Footprint of queried data: {mb_used:.2f} MB")

    if rows == 0:
        return

    # --- DYNAMIC TARGETING ---
    vacuum_channels = [col for col in vals_dict.keys() if "vacuum_gauge" in col and "pressure" in col]
    test_col = vacuum_channels[0] if vacuum_channels else next(iter(vals_dict.keys()))
    print(f"\n-> Targeting Channel: {test_col}")

    # --- TEST 2: Data Quality (Watchdog NaN Integrity) ---
    nan_count = np.isnan(vals_dict[test_col]).sum()
    clean_pct = ((rows - nan_count) / rows) * 100

    print(f"[METRIC]  Watchdog NaNs found: {nan_count} out of {rows} rows")
    print(f"[METRIC]  Service Uptime: {clean_pct:.2f}% Clean Data")

    # --- TEST 3: Multi-Channel Decimation Stress Test ---
    print("\n-> Stress-testing decimation for 30FPS GUI performance...")

    channels_to_test = vacuum_channels.copy()
    if len(channels_to_test) < 10:
        for col in vals_dict.keys():
            if col not in channels_to_test:
                channels_to_test.append(col)
            if len(channels_to_test) == 10:
                break

    t4 = time.perf_counter()
    for col in channels_to_test:
        engine.downsample_minmax(ts_array, vals_dict[col], target_points=2000)
    t5 = time.perf_counter()
    multi_dec_ms = (t5 - t4) * 1000

    print(f"[SUCCESS] 10-channel decimate: {multi_dec_ms:.2f} ms.")

    # --- TEST 4: 10-Channel Visual FPS Benchmark ---
    print("\n" + "=" * 60)
    print("2. 10-CHANNEL VISUAL RENDER TEST (PyQtGraph)")
    print("=" * 60)
    print("-> Downsampling 10 channels to 5000 points each (50,000 total points)...")

    win = pg.GraphicsLayoutWidget(show=True, title="10-Channel FPS Benchmark")
    win.resize(1200, 800)
    plot = win.addPlot(title="Drag and Scroll to Measure FPS")
    plot.setLabel('bottom', 'UNIX Timestamp')
    plot.setLabel('left', 'Raw Value')

    # Plot all 10 channels with different colors
    colors = ['#ff0000', '#00ff00', '#0000ff', '#ffff00', '#ff00ff',
              '#00ffff', '#ffffff', '#ff8800', '#8800ff', '#00ff88']

    t_plot_start = time.perf_counter()
    for idx, col in enumerate(channels_to_test):
        d_ts, d_vals = engine.downsample_minmax(ts_array, vals_dict[col], target_points=5000)
        plot.plot(d_ts, d_vals, pen=pg.mkPen(colors[idx % len(colors)], width=1.5), connect='finite')
    t_plot_end = time.perf_counter()

    print(f"[METRIC] Initial 10-Channel Render Time: {(t_plot_end - t_plot_start) * 1000:.2f} ms")

    # --- FPS Tracking Logic ---
    repaint_times = []
    last_paint = time.perf_counter()

    def on_range_changed():
        """Fires continuously while panning/zooming to calculate frame rate."""
        nonlocal last_paint
        now = time.perf_counter()
        delta = now - last_paint
        last_paint = now

        # Prevent division by zero on rapid-fire signals
        if delta > 0.001:
            repaint_times.append(1.0 / delta)
            if len(repaint_times) > 20:  # Keep a rolling average of the last 20 frames
                repaint_times.pop(0)

            avg_fps = sum(repaint_times) / len(repaint_times)
            plot.setTitle(f"10-Channel Stress Test | Panning FPS: {avg_fps:.0f}")

    plot.getViewBox().sigRangeChanged.connect(on_range_changed)

    print("-> Try clicking and dragging the plot window to test your CPU!")
    print("-> (Close the window to continue to Test 5)")
    app.exec()

    # --- TEST 5: Headless Cache Simulation ---
    print("\n" + "=" * 60)
    print("3. TESTING LIVE UI CACHE & ZMQ STREAM")
    print("=" * 60)

    cache = InfiniteDataCache(LOG_DIR)
    update_count = 0

    def on_ui_update_triggered():
        nonlocal update_count
        update_count += 1
        ptr = cache.write_ptr
        if ptr > 0:
            latest_time = cache.x_time[-1]
            lag_ms = (time.time() - latest_time) * 1000
            print(f"[UI Redraw {update_count:>2}] RAM Buffer: {ptr} rows | Live Data Latency: {lag_ms:.1f} ms")

    cache.data_updated.connect(on_ui_update_triggered)

    print("-> Booting ZeroMQ Listeners...")
    cache.start()

    now = time.time()
    cache.request_history(now - 60.0, now)

    print("-> Event loop running for 3 seconds...")
    # This restarts the Qt Event Loop for exactly 3 seconds, then kills it.
    QTimer.singleShot(3000, app.quit)
    app.exec()

    print("\n-> Shutting down cache...")
    cache.stop()
    print("[SUCCESS] Full benchmark suite complete.")


if __name__ == "__main__":
    run_benchmarks()