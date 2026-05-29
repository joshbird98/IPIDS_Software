import time


class PerfTracker:
    times = {"ingest": [], "slice": [], "render": []}
    slice_size = 0

    @classmethod
    def log(cls, name, t_start):
        cls.times[name].append(time.perf_counter() - t_start)

    @classmethod
    def get_stats(cls):
        res = []
        for k, v in cls.times.items():
            if v:
                avg_ms = (sum(v) / len(v)) * len(v) * 1000  # Just showing total time impact
                res.append(f"{k}: {(sum(v) / len(v)) * 1000:.2f}ms")
        # Reset lists
        cls.times = {k: [] for k in cls.times}
        return " | ".join(res) + f" | Slice size: {cls.slice_size} pts"