"""The last live value of each readout, for showing it once the reading stops.

When a reading stops (its service restarting or stopped, a controller not answering) a panel
keeps showing the last value it had, greyed and marked STALE with how long ago it was read,
rather than a blank or an old number that looks live. The panels are given only live values
(src/core/telemetry_cache.py: view), so they remember the last one here.

    last = LastKnown()
    last.update("vg1", 2.3e-7)       # a live value
    last.value("vg1")                # 2.3e-07, also once the reading has stopped
    last.age("vg1")                  # seconds since it was live (inf if never)
    last.read_at("vg1")              # "14:02:31": the clock time it was read
    stale_text(last.age("vg1"))      # "STALE 12 s"
"""
import time

STALE = "STALE"


def age_text(seconds: float) -> str:
    """'4 s', '3 min', '1 h 05 min', '2 d 3 h': how long ago, rounded down."""
    s = int(max(0.0, seconds))
    if s < 60:
        return f"{s} s"
    m = s // 60
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h} h {m:02d} min"
    d, h = divmod(h, 24)
    return f"{d} d {h} h"


def stale_text(seconds: float) -> str:
    return f"{STALE} {age_text(seconds)}"


class LastKnown:
    def __init__(self, clock=time.monotonic, wall=time.time):
        self._clock = clock          # for the age: a clock step mustn't change it
        self._wall = wall            # for the time of day shown
        self._seen = {}              # key -> (value, clock, wall)

    def update(self, key, value):
        """A live value: remember it."""
        self._seen[key] = (value, self._clock(), self._wall())

    def __contains__(self, key) -> bool:
        return key in self._seen

    def value(self, key, default=None):
        got = self._seen.get(key)
        return default if got is None else got[0]

    def age(self, key) -> float:
        got = self._seen.get(key)
        return float("inf") if got is None else max(0.0, self._clock() - got[1])

    def read_at(self, key) -> str:
        """The time of day it was last live: '14:02:31', or '19 Oct 14:02' if not today."""
        got = self._seen.get(key)
        if got is None:
            return "never"
        then = time.localtime(got[2])
        if time.strftime("%Y%m%d", then) == time.strftime("%Y%m%d", time.localtime(self._wall())):
            return time.strftime("%H:%M:%S", then)
        return time.strftime("%d %b %H:%M", then)
