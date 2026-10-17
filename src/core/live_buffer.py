"""
Per-source live telemetry buffers.

Why this exists
---------------
The old live cache kept ONE ring buffer with a column for every registry tag
(336) and wrote a full row for every ZMQ message from ANY service, forward-
filling the ~300 tags that message didn't contain. At ~120 msg/s that is
~40,000 stored values/s, of which only ~4,000 are real samples; the 100k-row
ring (~270 MB) covered only ~15 minutes, and ingest ran on the GUI thread.

Here each publisher (ZMQ topic) gets its own ring holding only its own tags at
its own timestamps:

  * memory/ingest scale with real samples (~8x less), so the same RAM keeps
    ~30 min of live data for every channel - nothing is lost when a channel is
    added to a plot;
  * each ring auto-sizes to `keep_seconds` at that source's actual rate;
  * timestamps are taken in the network thread at receipt (not when the GUI
    thread got round to the queued signal), and ingest runs in that thread,
    so the GUI thread only renders.

Thread-safety: ingest() runs on the ZMQ listener thread, readers on the GUI
thread. Each ring has a lock held only for the row write / the window copy.
"""
import threading
import time

import numpy as np

NAN = np.nan


class SourceRing:
    """Ring buffer of (t, values[n_tags]) for one publisher."""

    def __init__(self, name, initial_capacity=4096, keep_seconds=1800.0, max_capacity=400_000,
                 stale_seconds=1.0):
        self.name = name
        self.keep_seconds = float(keep_seconds)
        self.max_capacity = int(max_capacity)
        self.stale_seconds = float(stale_seconds)
        self.lock = threading.Lock()

        self.tags = []                 # column order
        self.col = {}                  # tag -> column index
        self.cap = int(initial_capacity)
        self.t = np.full(self.cap, NAN)
        self.v = np.full((0, self.cap), NAN)   # (n_tags, cap): per-tag reads are contiguous
        self.n = 0                     # rows written since last (re)layout; idx = n % cap
        self.total = 0                 # rows ever written (stats)
        self.last_row = np.full(0, NAN)
        self.last_seen = np.full(0, -np.inf)

    # ---------------- writing (listener thread) ----------------
    def _add_tags(self, new_tags):
        with self.lock:
            k = len(new_tags)
            self.v = np.vstack([self.v, np.full((k, self.cap), NAN)])
            self.last_row = np.concatenate([self.last_row, np.full(k, NAN)])
            self.last_seen = np.concatenate([self.last_seen, np.full(k, -np.inf)])
            for t in new_tags:
                self.col[t] = len(self.tags)
                self.tags.append(t)

    def _grow(self, new_cap):
        """Re-layout chronologically into a larger ring (called with lock held)."""
        k = min(self.n, self.cap)
        i = self.n % self.cap
        order = np.r_[i:self.cap, 0:i] if self.n > self.cap else np.arange(k)
        t = np.full(new_cap, NAN)
        v = np.full((len(self.tags), new_cap), NAN)
        t[:k] = self.t[order]
        v[:, :k] = self.v[:, order]
        self.t, self.v, self.cap, self.n = t, v, new_cap, k

    def ingest(self, ts, data, allowed=None):
        new = [k for k in data if k not in self.col and (allowed is None or k in allowed)]
        if new:
            self._add_tags(new)
        row = self.last_row
        col = self.col
        seen = self.last_seen
        for k, val in data.items():
            j = col.get(k)
            if j is None:
                continue
            try:
                row[j] = float(val)
            except (TypeError, ValueError):
                row[j] = NAN
            seen[j] = ts
        # Keys this message didn't carry are held for up to stale_seconds (same
        # rule as the old cache), then shown as gaps.
        row[(ts - seen) > self.stale_seconds] = NAN

        with self.lock:
            if self.n >= self.cap and self.n % self.cap == 0 and self.cap < self.max_capacity:
                oldest = self.t[self.n % self.cap]
                newest = self.t[(self.n - 1) % self.cap]
                span = newest - oldest
                if span < self.keep_seconds:
                    # Size to the measured rate (+5%) rather than doubling, which
                    # could overshoot by up to 2x; at least x1.25 so it converges.
                    need = self.cap * self.keep_seconds / max(span, 1e-3) * 1.05
                    self._grow(int(min(self.max_capacity, max(self.cap * 1.25, need))))
            i = self.n % self.cap
            self.v[:, i] = row
            self.t[i] = ts
            self.n += 1
            self.total += 1

    # ---------------- reading (GUI thread) ----------------
    def _segments(self):
        k = min(self.n, self.cap)
        if k == 0:
            return []
        if self.n <= self.cap:
            return [(0, k)]
        i = self.n % self.cap
        return [(i, self.cap), (0, i)]

    def window(self, t0, t1, tags):
        """Chronological copies of samples with t0 <= t <= t1 for `tags`.
        Returns {tag: (x, y)}; tags this source doesn't carry are omitted."""
        with self.lock:
            cols = [(t, self.col[t]) for t in tags if t in self.col]
            if not cols:
                return {}
            idx = []
            for a, b in self._segments():
                seg = self.t[a:b]
                s = np.searchsorted(seg, t0, side='left')
                e = np.searchsorted(seg, t1, side='right')
                if e > s:
                    idx.append((a + s, a + e))
            if not idx:
                return {}
            x = np.concatenate([self.t[s:e] for s, e in idx])
            out = {}
            for tag, j in cols:
                out[tag] = (x, np.concatenate([self.v[j, s:e] for s, e in idx]))
            return out

    def latest_time(self):
        with self.lock:
            return None if self.n == 0 else float(self.t[(self.n - 1) % self.cap])

    def oldest_time(self):
        with self.lock:
            if self.n == 0:
                return None
            return float(self.t[0] if self.n <= self.cap else self.t[self.n % self.cap])

    def latest_value(self, tag):
        with self.lock:
            j = self.col.get(tag)
            if j is None or self.n == 0:
                return None
            return float(self.v[j, (self.n - 1) % self.cap])

    def nbytes(self):
        return self.t.nbytes + self.v.nbytes


class LiveStore:
    """All live data, one SourceRing per publisher (ZMQ topic)."""

    def __init__(self, allowed_tags=None, keep_seconds=1800.0, max_capacity=400_000, initial_capacity=4096):
        self.allowed = set(allowed_tags) if allowed_tags else None
        self.initial_capacity = initial_capacity
        self.keep_seconds = keep_seconds
        self.max_capacity = max_capacity
        self.sources = {}          # name -> SourceRing
        self.tag_source = {}       # tag -> SourceRing (first publisher seen for it)
        self._lock = threading.Lock()
        self._rate_mark = (time.time(), {})

    def ingest(self, source, ts, data):
        """Called from the ZMQ listener thread for every message."""
        if self.allowed is not None and not self.allowed:
            return
        ring = self.sources.get(source)
        if ring is None:
            with self._lock:
                ring = self.sources.get(source)
                if ring is None:
                    ring = SourceRing(source, initial_capacity=self.initial_capacity,
                                      keep_seconds=self.keep_seconds, max_capacity=self.max_capacity)
                    self.sources[source] = ring
        before = len(ring.tags)
        ring.ingest(ts, data, self.allowed)
        if len(ring.tags) != before:
            with self._lock:
                for t in ring.tags[before:]:
                    self.tag_source.setdefault(t, ring)

    # ---- queries used by the viewer ----
    def has_data(self):
        return any(r.n for r in list(self.sources.values()))

    def window(self, t0, t1, tags):
        """{tag: (x_abs, y)} for each requested tag that has live samples in [t0, t1]."""
        by_ring = {}
        for t in tags:
            r = self.tag_source.get(t)
            if r is not None:
                by_ring.setdefault(r, []).append(t)
        out = {}
        for r, ts in by_ring.items():
            out.update(r.window(t0, t1, ts))
        return out

    def latest_time(self):
        vals = [v for v in (r.latest_time() for r in list(self.sources.values())) if v is not None]
        return max(vals) if vals else None

    def oldest_time(self):
        """Start of the span where every *active* source has live data.
        (Sources silent for longer than keep_seconds are ignored.)"""
        newest = self.latest_time()
        if newest is None:
            return None
        olds = []
        for r in list(self.sources.values()):
            lt, ot = r.latest_time(), r.oldest_time()
            if lt is not None and newest - lt < self.keep_seconds:
                olds.append(ot)
        return max(olds) if olds else None

    def latest_value(self, tag):
        r = self.tag_source.get(tag)
        return None if r is None else r.latest_value(tag)

    def nbytes(self):
        return sum(r.nbytes() for r in list(self.sources.values()))

    def stats(self):
        """Per-source summary for the health printout: rate, tags, buffer span, MB."""
        now = time.time()
        t_prev, counts_prev = self._rate_mark
        dt = max(now - t_prev, 1e-6)
        rows = []
        counts = {}
        for name, r in sorted(list(self.sources.items())):
            counts[name] = r.total
            rate = (r.total - counts_prev.get(name, r.total)) / dt
            lt, ot = r.latest_time(), r.oldest_time()
            span = 0.0 if lt is None else lt - ot
            rows.append((name, rate, len(r.tags), span, r.nbytes() / 2**20))
        self._rate_mark = (now, counts)
        return rows


def break_gaps(x, y, min_gap):
    """Insert a NaN wherever consecutive samples are more than `min_gap` apart,
    so connect='finite' breaks the line across a dropout instead of drawing a
    straight segment over it."""
    if len(x) < 2:
        return x, y
    gi = np.nonzero(np.diff(x) > min_gap)[0]
    if gi.size == 0:
        return x, y
    ins = gi + 1
    return np.insert(x, ins, x[gi] + 1e-3), np.insert(y.astype(np.float64, copy=False), ins, NAN)


if __name__ == "__main__":
    # Self-test (no Qt needed): python -m src.core.live_buffer
    rng = np.random.default_rng(0)
    store = LiveStore(allowed_tags=[f"a{i}" for i in range(5)] + [f"b{i}" for i in range(3)],
                      keep_seconds=60.0, max_capacity=50_000, initial_capacity=256)
    t = 1000.0
    # source A at 20 Hz with 5 tags, source B at 10 Hz with 3 tags, for 200 s
    for k in range(200 * 20):
        t = 1000.0 + k / 20
        store.ingest("A", t, {f"a{i}": t + i for i in range(5)} | {"not_in_registry": 1})
        if k % 2 == 0:
            store.ingest("B", t, {f"b{i}": -t for i in range(3)})
    A, B = store.sources["A"], store.sources["B"]
    assert A.tags == [f"a{i}" for i in range(5)], A.tags
    span_A = A.latest_time() - A.oldest_time()
    assert 60 <= span_A <= 130, span_A        # grew to cover >= keep_seconds, then wrapped
    w = store.window(1150.0, 1160.0, ["a0", "a3", "b1", "zz"])
    xa, ya = w["a3"]
    assert np.all(np.diff(xa) > 0) and abs(xa[0] - 1150.0) < 0.06 and abs(xa[-1] - 1160) < 0.06
    assert np.allclose(ya, xa + 3)
    xb, yb = w["b1"]
    assert np.allclose(yb, -xb) and abs(np.median(np.diff(xb)) - 0.1) < 1e-9
    assert "zz" not in w
    # wrap boundary: window across the ring seam must stay sorted and complete
    w2 = store.window(A.oldest_time(), A.latest_time(), ["a0"])
    x2 = w2["a0"][0]
    assert np.all(np.diff(x2) > 0) and len(x2) == min(A.n, A.cap)
    # stale rule: key missing for > 1 s becomes NaN
    store.ingest("A", t + 0.05, {"a0": 1.0})
    store.ingest("A", t + 2.0, {"a0": 2.0})
    assert np.isnan(store.latest_value("a1")) and store.latest_value("a0") == 2.0
    # gap breaking
    xg, yg = break_gaps(np.array([0, 1, 2, 10, 11.0]), np.arange(5.0), 3.0)
    assert len(xg) == 6 and np.isnan(yg[3])
    old_bytes = 100_000 * 8 * (336 + 1)
    print(f"OK  A: cap={A.cap} span={span_A:.0f}s  B: cap={B.cap}  store={store.nbytes()/2**20:.1f} MB "
          f"oldest_time={store.oldest_time():.1f} latest={store.latest_time():.1f}")
