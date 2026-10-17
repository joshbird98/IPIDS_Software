"""Latest telemetry from every service, with freshness.

The HMI used to keep a plain dict of the last value of every tag. When a
service died its last values stayed there forever, so the screens - and the
recipe engine, optimiser and mass scan - carried on using them as if they were
live, and missing tags were read as 0.0.

This cache remembers which topic (service) each tag came from and when that
topic last published. A topic is stale when it has been silent for longer than
max(stale_min, stale_factor x its usual interval); every tag from a stale topic
is then unknown. Services also publish null for values they cannot currently
measure (device unreachable, supply unpowered), which count as unknown too.

  cache.view()        -> dict of only fresh, known values (for widgets: a missing
                         key shows as '---' instead of a stale number)
  cache.snapshot()    -> TelemetrySnapshot for worker threads (recipes, scans):
                         .get_fresh(tag), .is_fresh(tag), .age(tag)
  cache.reader()      -> function tag -> fresh value or None
"""
import threading
import time

# Keys every service publishes under the same name; kept per topic ("PLC_DATA/system.connected")
GENERIC_PREFIXES = ("system.",)
GENERIC_KEYS = frozenset({"timestamp"})

# Legacy "<x>.connected" flags the widgets use, derived from topic freshness
LEGACY_CONNECTED = {
    "pump.connected": "SRC_TURBO_DATA",
    "vacuum.connected": "VACUUM_DATA",
    "magnet.connected": "MAGNET_DATA",
    "smu.connected": "SMU_DATA",
    "current_mon.connected": "ADAM_DATA",
}


def _is_generic(key: str) -> bool:
    return key in GENERIC_KEYS or key.startswith(GENERIC_PREFIXES)


def fresh_value(telemetry, tag):
    """Live value of `tag` or None if stale/unknown. Accepts a TelemetrySnapshot (freshness
    aware) or a plain dict (unit tests, old callers)."""
    if not tag:
        return None
    getter = getattr(telemetry, "get_fresh", None)
    return getter(tag) if getter is not None else telemetry.get(tag)


class TelemetrySnapshot(dict):
    """A frozen copy of the cache values (raw, possibly stale) plus freshness info."""

    def __init__(self, values: dict, fresh: set, ages: dict, taken: float):
        super().__init__(values)
        self._fresh = fresh
        self._ages = ages
        self.taken = taken

    def is_fresh(self, tag: str) -> bool:
        return tag in self._fresh

    def get_fresh(self, tag: str, default=None):
        return self.get(tag, default) if tag in self._fresh else default

    def age(self, tag: str) -> float:
        """Seconds since the tag's source last published (inf if never seen)."""
        return self._ages.get(tag, float("inf"))


class TelemetryCache:
    def __init__(self, stale_min: float = 2.0, stale_factor: float = 5.0, clock=time.time):
        self.stale_min = stale_min
        self.stale_factor = stale_factor
        self._clock = clock
        self._lock = threading.Lock()
        self._values = {}
        self._tag_topic = {}
        self._topic_seen = {}
        self._topic_interval = {}

    # ---------------------------------------------------------------- ingest
    def ingest(self, topic, payload: dict, now: float = None):
        if isinstance(topic, bytes):
            topic = topic.decode("utf-8", "replace")
        now = self._clock() if now is None else now
        with self._lock:
            last = self._topic_seen.get(topic)
            if last is not None:
                dt = now - last
                prev = self._topic_interval.get(topic)
                # Exponential average of the publish interval (ignore huge gaps: those are outages)
                if dt < 60.0:
                    self._topic_interval[topic] = dt if prev is None else 0.9 * prev + 0.1 * dt
            self._topic_seen[topic] = now
            for key, value in payload.items():
                if _is_generic(key):
                    key = f"{topic}/{key}"
                self._values[key] = value
                self._tag_topic[key] = topic

    # ---------------------------------------------------------------- freshness
    def stale_after(self, topic: str) -> float:
        interval = self._topic_interval.get(topic)
        return self.stale_min if interval is None else max(self.stale_min, self.stale_factor * interval)

    def topic_age(self, topic: str, now: float = None) -> float:
        seen = self._topic_seen.get(topic)
        if seen is None:
            return float("inf")
        return (self._clock() if now is None else now) - seen

    def topic_fresh(self, topic: str, now: float = None) -> bool:
        return self.topic_age(topic, now) <= self.stale_after(topic)

    def _fresh_topics(self, now):
        return {t for t, seen in self._topic_seen.items() if now - seen <= self.stale_after(t)}

    def is_fresh(self, tag: str, now: float = None) -> bool:
        now = self._clock() if now is None else now
        with self._lock:
            topic = self._tag_topic.get(tag)
            return (topic is not None and self._values.get(tag) is not None
                    and now - self._topic_seen.get(topic, -1e18) <= self.stale_after(topic))

    def get(self, tag: str, default=None):
        """Raw last value, even if stale (prefer get_fresh)."""
        with self._lock:
            return self._values.get(tag, default)

    def get_fresh(self, tag: str, default=None, now: float = None):
        return self.get(tag, default) if self.is_fresh(tag, now) else default

    def topic_of(self, tag: str):
        return self._tag_topic.get(tag)

    @property
    def topics(self):
        return dict(self._topic_seen)

    # ---------------------------------------------------------------- views
    def view(self, now: float = None) -> dict:
        """Fresh, known values only, plus the legacy flat keys the widgets expect."""
        now = self._clock() if now is None else now
        with self._lock:
            fresh_topics = self._fresh_topics(now)
            out = {k: v for k, v in self._values.items()
                   if v is not None and self._tag_topic.get(k) in fresh_topics}
        # 'system.connected' always meant "PLC reachable" to the widgets; other services also
        # publish a system.connected of their own, which used to overwrite it.
        plc_ok = "PLC_DATA" in fresh_topics and bool(out.get("PLC_DATA/system.connected"))
        out["system.connected"] = plc_ok
        for key, topic in LEGACY_CONNECTED.items():
            out[key] = topic in fresh_topics
        return out

    def snapshot(self, now: float = None) -> TelemetrySnapshot:
        now = self._clock() if now is None else now
        with self._lock:
            values = dict(self._values)
            fresh_topics = self._fresh_topics(now)
            fresh = {k for k, t in self._tag_topic.items() if t in fresh_topics and values.get(k) is not None}
            ages = {k: now - self._topic_seen.get(t, now) for k, t in self._tag_topic.items()}
        plc_ok = "PLC_DATA" in fresh_topics and bool(values.get("PLC_DATA/system.connected"))
        values["system.connected"] = plc_ok
        fresh.add("system.connected")
        return TelemetrySnapshot(values, fresh, ages, now)

    def reader(self):
        """For worker threads: tag -> fresh value, or None if stale/unknown/missing."""
        return lambda tag: self.get_fresh(tag)
