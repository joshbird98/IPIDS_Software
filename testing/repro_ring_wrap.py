"""
Headless reproduction of the "all live lines suddenly disappear" bug (in the ORIGINAL
single-ring live cache; that cache has since been replaced by src/core/live_buffer.py).

No Qt needed: this replays the exact index arithmetic from
  - DualPipelineCache._on_live_data        (src/core/data_cache.py)
  - DataViewerApp._refresh_plot_live       (src/gui/data_viewer/viewer_main.py)
  - DataViewerApp._render_curves (live)    (src/gui/data_viewer/viewer_main.py)
against a simulated ring buffer, and reports what fraction of the visible
window actually has data drawn in it.

Run:  python testing/repro_ring_wrap.py
"""
import numpy as np

CAP = 100_000          # DualPipelineCache.live_capacity
RATE_HZ = 110.0        # sum of all ZMQ publishers (PLC 10, SMU 10, MPD 10, magnet 20,
                       # steerer 20, turbo 20, vacuum 20+) -> one ring row per message
VIEW_SPAN = 300.0      # default live view width (s)


class FakeCache:
    def __init__(self):
        self.live_capacity = CAP
        self._x_live = np.zeros(CAP)
        self._y = np.zeros(CAP)
        self.live_ptr = 0
        self.buffer_wrapped = False

    def ingest(self, t):
        # verbatim logic from _on_live_data
        idx = self.live_ptr % self.live_capacity
        self._x_live[idx] = t
        self._y[idx] = np.sin(t / 10.0)
        self.live_ptr += 1
        if self.live_ptr >= self.live_capacity:
            self.buffer_wrapped = True
            self.live_ptr = self.live_ptr % self.live_capacity


def refresh_plot_live_slice(cache, view_start, view_end, t0):
    """Verbatim slicing from _refresh_plot_live -> (x_slice, y_slice)."""
    x_raw = cache._x_live
    cap = cache.live_capacity
    ptr = cache.live_ptr % cap
    span = view_end - view_start
    a0 = (view_start - span) + t0
    a1 = (view_end + span) + t0
    if not cache.buffer_wrapped:
        xv = x_raw[:ptr]
        s = np.searchsorted(xv, a0); e = np.searchsorted(xv, a1, side='right')
        return xv[s:e] - t0, cache._y[s:e]
    x_old, x_new = x_raw[ptr:], x_raw[:ptr]
    so = np.searchsorted(x_old, a0); eo = np.searchsorted(x_old, a1, side='right')
    sn = np.searchsorted(x_new, a0); en = np.searchsorted(x_new, a1, side='right')
    xs = np.concatenate((x_old[so:eo], x_new[sn:en])) - t0
    ys = np.concatenate((cache._y[ptr:][so:eo], cache._y[:ptr][sn:en]))
    return xs, ys


def render_curves_live_x(cache, x_array, view_start, view_end, fixed=False):
    """Verbatim X handling from _render_curves(is_historical=False).

    NOTE: x_array here is the *already sliced* window from _refresh_plot_live,
    but this code indexes it as if it were the full ring buffer.
    """
    if fixed:
        x_chron = x_array  # proposed fix: the slice is already chronological
    else:
        ptr = cache.live_ptr
        cap = cache.live_capacity
        N_total = min(ptr, cap)
        start_idx = (ptr - N_total) % cap
        end_idx = ptr % cap
        if start_idx < end_idx:
            x_chron = x_array[start_idx:end_idx]
        else:
            x_chron = np.concatenate((x_array[start_idx:], x_array[:end_idx]))
    span = view_end - view_start
    v_min = view_start - span * 0.25
    v_max = view_end + span * 0.25
    s = np.searchsorted(x_chron, v_min, side='left')
    e = np.searchsorted(x_chron, v_max, side='right')
    if s >= e:
        s, e = 0, len(x_chron)  # "failsafe"
    return x_chron[s:e]


def visible_fraction(x_plot, view_start, view_end):
    """Fraction of the visible window covered by plotted data."""
    if len(x_plot) == 0:
        return 0.0
    lo = max(view_start, x_plot.min())
    hi = min(view_end, x_plot.max())
    return max(0.0, hi - lo) / (view_end - view_start)


def run(fixed=False, minutes=60, report_every_s=30):
    cache = FakeCache()
    t0 = 1_760_000_000.0   # viewer start time
    dt = 1.0 / RATE_HZ
    n_total = int(minutes * 60 * RATE_HZ)
    next_report = report_every_s
    rows = []
    for i in range(n_total):
        t = t0 + i * dt
        cache.ingest(t)
        elapsed = i * dt
        if elapsed >= next_report:
            next_report += report_every_s
            if cache.live_ptr == 0:
                continue  # viewer returns early on this exact frame
            # auto-scroll: view right edge pinned to newest sample
            view_end = t - t0
            view_start = view_end - VIEW_SPAN
            xs, _ = refresh_plot_live_slice(cache, view_start, view_end, t0)
            xp = render_curves_live_x(cache, xs, view_start, view_end, fixed=fixed)
            rows.append((elapsed / 60, cache.buffer_wrapped, cache.live_ptr,
                         visible_fraction(xp, view_start, view_end)))
    return rows


if __name__ == "__main__":
    wrap_min = CAP / RATE_HZ / 60
    print(f"Ring buffer: {CAP} rows at {RATE_HZ:.0f} rows/s -> wraps every {wrap_min:.1f} min")
    print(f"View span: {VIEW_SPAN:.0f} s\n")
    cur = run(fixed=False)
    fix = run(fixed=True)
    print(f"{'t (min)':>8} {'wrapped':>8} {'live_ptr':>9} {'visible (current)':>18} {'visible (fixed)':>16}")
    for (m, w, p, v), (_, _, _, vf) in zip(cur, fix):
        flag = "  <-- LINES GONE" if v < 0.05 else ("  <-- partial" if v < 0.95 else "")
        print(f"{m:8.1f} {str(w):>8} {p:9d} {v:17.0%} {vf:16.0%}{flag}")
    blank = sum(1 for r in cur if r[3] < 0.05) / len(cur)
    print(f"\nCurrent code: lines fully missing in {blank:.0%} of frames sampled over 60 min.")
    print(f"Fixed code:   lines fully missing in {sum(1 for r in fix if r[3] < 0.05)/len(fix):.0%}.")
