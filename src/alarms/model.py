"""Alarm states, acknowledgement, shelving and first-out.

    NORMAL      condition clear, nothing to acknowledge
    UNACK       condition active, not acknowledged yet          -> annunciated (sound, flashing)
    ACKED       condition active, acknowledged
    RTN_UNACK   cleared before anyone acknowledged it           -> listed until acknowledged

An alarm becomes active only after its condition has held for delay_on seconds and clears only
after delay_off seconds clear (chattering inputs). A shelved alarm keeps tracking its condition
but is not annunciated until the shelf time runs out or it is unshelved. Suppressed alarms (by
another active alarm that explains them, e.g. every gauge's comms by the controller's) are
listed greyed and not annunciated. In a first-out group (the PLC trips) the first alarm to go
active since the last reset is marked, so the cause of a cascade stands out.

State that matters to the operator (acknowledgements, shelves, first-out) survives a restart
of the alarm service: AlarmTable.dump() / restore().
"""
import time
from dataclasses import dataclass, field

NORMAL, UNACK, ACKED, RTN_UNACK = "NORMAL", "UNACK", "ACKED", "RTN_UNACK"
PRIORITY_NAMES = {1: "critical", 2: "high", 3: "medium", 4: "low"}


@dataclass
class Alarm:
    id: str
    message: str
    priority: int = 3
    group: str = ""
    delay_on: float = 0.0
    delay_off: float = 0.0
    state: str = NORMAL
    active: bool = False
    since: float = None            # wall time it last went active
    cleared: float = None          # wall time it last cleared
    count: int = 0                 # activations
    acked_by: str = None
    acked_at: float = None
    shelved_until: float = None
    shelve_reason: str = ""
    shelved_by: str = ""
    first_out: bool = False
    suppressed: bool = False
    _pending: tuple = field(default=None, repr=False)     # (wanted condition, since)

    @property
    def shelved(self) -> bool:
        return self.shelved_until is not None

    @property
    def annunciated(self) -> bool:
        """Needs the operator's attention now."""
        return self.state in (UNACK, RTN_UNACK) and not self.shelved and not self.suppressed

    def view(self) -> dict:
        return {"id": self.id, "message": self.message, "priority": self.priority, "group": self.group,
                "state": self.state, "active": self.active, "since": self.since, "cleared": self.cleared,
                "count": self.count, "acked_by": self.acked_by, "acked_at": self.acked_at,
                "shelved_until": self.shelved_until, "shelve_reason": self.shelve_reason,
                "shelved_by": self.shelved_by, "first_out": self.first_out, "suppressed": self.suppressed}


class AlarmTable:
    def __init__(self, first_out_groups=(), clock=time.time, on_event=None):
        self.alarms = {}
        self.first_out_groups = set(first_out_groups)
        self._clock = clock
        self._on_event = on_event or (lambda kind, alarm, detail: None)
        self._first_out = {}           # group -> alarm id
        self._saved = {}               # restored state of alarms not defined yet
        self.changed = False           # something worth saving / publishing happened

    def _event(self, kind, alarm, detail=""):
        self.changed = True
        self._on_event(kind, alarm, detail)

    # ------------------------------------------------------------------ definitions
    def define(self, alarm_id, message, priority=3, group="", delay_on=0.0, delay_off=0.0) -> Alarm:
        alarm = self.alarms.get(alarm_id)
        if alarm is None:
            alarm = self.alarms[alarm_id] = Alarm(alarm_id, message, int(priority), group, float(delay_on),
                                                  float(delay_off))
            self.apply_saved(alarm_id)
        else:
            if alarm.message != message or alarm.priority != int(priority):
                self.changed = True
            alarm.message, alarm.priority, alarm.group = message, int(priority), group
            alarm.delay_on, alarm.delay_off = float(delay_on), float(delay_off)
        return alarm

    # ------------------------------------------------------------------ conditions
    def update(self, alarm_id, condition, now=None):
        """condition: True / False, or None when unknown (stale data): no change."""
        alarm = self.alarms[alarm_id]
        if condition is None:
            alarm._pending = None
            return
        now = self._clock() if now is None else now
        condition = bool(condition)
        if condition == alarm.active:
            alarm._pending = None
            return
        delay = alarm.delay_on if condition else alarm.delay_off
        if delay > 0:
            if alarm._pending is None or alarm._pending[0] != condition:
                alarm._pending = (condition, now)
                return
            if now - alarm._pending[1] < delay:
                return
        alarm._pending = None
        if condition:
            self._activate(alarm, now)
        else:
            self._clear(alarm, now)

    def _activate(self, alarm, now):
        alarm.active = True
        alarm.since = now
        alarm.count += 1
        if alarm.state in (NORMAL, RTN_UNACK):
            alarm.state = UNACK
            alarm.acked_by = alarm.acked_at = None
        if alarm.group in self.first_out_groups and alarm.group not in self._first_out:
            self._first_out[alarm.group] = alarm.id
            alarm.first_out = True
        self._event("ACTIVE", alarm, "first out" if alarm.first_out else "")

    def _clear(self, alarm, now):
        alarm.active = False
        alarm.cleared = now
        alarm.state = NORMAL if alarm.state == ACKED else RTN_UNACK
        self._event("CLEARED", alarm)

    def set_suppressed(self, alarm_id, suppressed: bool):
        alarm = self.alarms[alarm_id]
        if alarm.suppressed != bool(suppressed):
            alarm.suppressed = bool(suppressed)
            self.changed = True

    # ------------------------------------------------------------------ operator actions
    def ack(self, ids=None, by="operator", now=None) -> list:
        """Acknowledge these alarms (None: every one waiting). Returns the ids acknowledged."""
        now = self._clock() if now is None else now
        done = []
        for alarm in (self.alarms.values() if ids is None else [self.alarms[i] for i in ids if i in self.alarms]):
            if alarm.state == UNACK:
                alarm.state = ACKED
            elif alarm.state == RTN_UNACK:
                alarm.state = NORMAL
            else:
                continue
            alarm.acked_by, alarm.acked_at = by, now
            done.append(alarm.id)
            self._event("ACKED", alarm, f"by {by}")
        return done

    def shelve(self, alarm_id, seconds, reason, by="operator", now=None):
        now = self._clock() if now is None else now
        alarm = self.alarms[alarm_id]
        alarm.shelved_until = now + float(seconds)
        alarm.shelve_reason = reason
        alarm.shelved_by = by
        self._event("SHELVED", alarm, f"for {float(seconds) / 60:.0f} min by {by}: {reason}")

    def unshelve(self, alarm_id, by="operator", why=""):
        alarm = self.alarms[alarm_id]
        if not alarm.shelved:
            return False
        alarm.shelved_until = None
        alarm.shelve_reason = alarm.shelved_by = ""
        self._event("UNSHELVED", alarm, why or f"by {by}")
        return True

    def expire_shelves(self, now=None):
        now = self._clock() if now is None else now
        for alarm in self.alarms.values():
            if alarm.shelved and now >= alarm.shelved_until:
                self.unshelve(alarm.id, why="shelf time over")

    def reset_first_out(self):
        for alarm in self.alarms.values():
            if alarm.first_out:
                alarm.first_out = False
                self.changed = True
        self._first_out.clear()

    # ------------------------------------------------------------------ views
    def listed(self) -> list:
        """Everything the operator should see: not NORMAL, or shelved. Most urgent first."""
        rank = {UNACK: 0, RTN_UNACK: 1, ACKED: 2, NORMAL: 3}
        shown = [a for a in self.alarms.values() if a.state != NORMAL or a.shelved]
        return sorted(shown, key=lambda a: (a.shelved or a.suppressed, rank[a.state], a.priority, -(a.since or 0)))

    def horn(self):
        """Priority (1 = most urgent) of the most urgent alarm that should sound, or None.
        Only unacknowledged active alarms sound; cleared-unacknowledged ones just flash."""
        sounding = [a.priority for a in self.alarms.values() if a.annunciated and a.state == UNACK]
        return min(sounding) if sounding else None

    def counts(self) -> dict:
        return {"unack": sum(a.annunciated for a in self.alarms.values()),
                "active": sum(a.active and not a.shelved for a in self.alarms.values()),
                "shelved": sum(a.shelved for a in self.alarms.values()),
                "suppressed": sum(a.suppressed and a.active for a in self.alarms.values())}

    # ------------------------------------------------------------------ persistence
    _SAVED = ("state", "active", "since", "cleared", "count", "acked_by", "acked_at", "shelved_until",
              "shelve_reason", "shelved_by", "first_out")

    def dump(self) -> dict:
        return {"alarms": {a.id: {k: getattr(a, k) for k in self._SAVED} for a in self.alarms.values()
                           if a.state != NORMAL or a.shelved or a.first_out},
                "first_out": dict(self._first_out)}

    def restore(self, saved: dict):
        """Bring back acknowledgements, shelves and first-out after a restart. Alarms not defined
        (yet) are kept aside and applied when they are."""
        self._saved = dict((saved or {}).get("alarms", {}))
        self._first_out = dict((saved or {}).get("first_out", {}))
        for alarm_id in list(self._saved):
            if alarm_id in self.alarms:
                self._apply_saved(self.alarms[alarm_id])

    def _apply_saved(self, alarm):
        for k, v in self._saved.pop(alarm.id, {}).items():
            if k in self._SAVED:
                setattr(alarm, k, v)

    def apply_saved(self, alarm_id):
        if alarm_id in self._saved:
            self._apply_saved(self.alarms[alarm_id])
