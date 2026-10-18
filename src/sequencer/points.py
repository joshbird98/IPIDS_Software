"""Scan points between the sequencer and the HMI.

The job keeps the points of the current pass (and of the last finished one) in a PointLog. Its
telemetry carries the number of points so far and the last TAIL of them, each prefixed with
its index in the pass. The HMI's PointFollower appends what it has not seen; if it falls more
than TAIL behind (it joined in the middle of a scan, or missed messages) it fetches the whole
pass with the slot's cmd_fetch command.
"""
import time

TAIL = 50
FETCH_RETRY_S = 2.0


class PointLog:
    def __init__(self):
        self.pass_no = 0
        self.current = []
        self.previous = (0, [])         # (pass number, points) of the last finished pass

    def start_pass(self, pass_no: int):
        if self.pass_no:
            self.previous = (self.pass_no, self.current)
        self.pass_no = pass_no
        self.current = []

    def add(self, point):
        self.current.append([float(v) for v in point])

    def tail(self, n: int = TAIL) -> list:
        first = max(0, len(self.current) - n)
        return [[i] + p for i, p in enumerate(self.current[first:], first)]

    def get(self, pass_no: int):
        """Every point of that pass, or None if it is not kept any more."""
        if pass_no == self.pass_no:
            return [list(p) for p in self.current]
        if pass_no == self.previous[0]:
            return [list(p) for p in self.previous[1]]
        return None


class PointFollower:
    """Rebuilds the points of the current pass from telemetry.

    update(run, pass_no, count, tail) returns what changed:
        ("new", None)       a new run or pass started: the points so far are gone
        ("append", points)  points to add, in order
        ("replace", points) the whole pass so far (after a fetch)
    fetch(run, pass_no) must return a ticket (done(), ok, data) for the slot's cmd_fetch.
    """

    def __init__(self, fetch, clock=time.monotonic):
        self._fetch = fetch
        self._clock = clock
        self.key = None             # (run, pass)
        self.points = []
        self._ticket = None
        self._ticket_key = None
        self._last_fetch = None

    def update(self, run, pass_no, count, tail) -> list:
        changes = []
        key = (run, pass_no)
        if run is None or not pass_no:
            return changes
        if key != self.key:
            self.key = key
            self.points = []
            self._ticket = None
            self._last_fetch = None
            changes.append(("new", None))
        added = []
        for entry in tail or []:
            index, values = int(entry[0]), list(entry[1:])
            if index == len(self.points):
                self.points.append(values)
                added.append(values)
        if added:
            changes.append(("append", added))
        now = self._clock()
        if count is not None and len(self.points) < count and self._ticket is None and \
                (self._last_fetch is None or now - self._last_fetch >= FETCH_RETRY_S):
            self._last_fetch = now
            self._ticket = self._fetch(run, pass_no)
            self._ticket_key = key
        return changes + self.poll()

    def poll(self) -> list:
        """A fetch that has been answered (call it from update(), or on its own)."""
        if self._ticket is None or not self._ticket.done():
            return []
        ticket, key, self._ticket = self._ticket, self._ticket_key, None
        data = ticket.data if ticket.ok else None
        if key != self.key or not isinstance(data, list) or len(data) < len(self.points):
            return []
        self.points = [list(p) for p in data]
        return [("replace", [list(p) for p in self.points])]
