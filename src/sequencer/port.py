"""How a sequencer job sends commands.

Every job holds a control lease (src/core/leases.py) on each subsystem it drives, so nothing
else can drive it meanwhile and the operator sees what is in charge:

  - claimed lazily, just before the job's first command to that subsystem (the claim and the
    command go down the same connection, so the service handles the claim first)
  - renewed every RENEW_EVERY_S with a time-to-live of LEASE_TTL_S, so a sequencer that dies
    frees everything within a few seconds
  - released when the job ends.

If the operator takes control (or another sequence already holds the subsystem), the
LeaseKeeper reports the loss to the job (on_lost), which pauses (a recipe) or stops (a scan).
It does not claim that subsystem again until the job forgives the loss (a recipe on resume).
A lease operation that reached its service late is not a loss (the services don't refuse one
for that any more; an older one might): the lease is kept and renewed as usual.

A command a service refused as stale - it reached the service later than the service's limit
(0.5 s for the device services), because the service or this one was busy for a moment - was
never carried out, and the job still wants it: the Dispatcher sends it again, STALE_RESENDS
times at most, before the job sees the refusal.

Which subsystem a tag belongs to is learned from the services' own telemetry: every
DeviceService publishes "<subsystem>.permit" for each subsystem it controls and
"<subsystem>.lease_holder" for each one that can be leased (SubsystemMap).
"""
import logging
import time

import zmq

from src.core.commands import REJECTED, STALE, UNREACHABLE, TIMEOUT, CommandTicket, route_for_tag
from src.core.leases import LEASE_SUFFIX, TAKEN

RENEW_EVERY_S = 1.0
LEASE_TTL_S = 5.0
STALE_RESENDS = 2           # a command refused as stale is sent again this many times at most
_PERMIT = ".permit"
_HOLDER = ".lease_holder"

log = logging.getLogger("sequencer.port")


class Dispatcher:
    """Owns the sequencer's one CommandClient (one thread: the service loop). send() returns a
    CommandTicket at once; pump() collects the replies and resolves the tickets."""

    def __init__(self, client):
        self.client = client
        self._tickets = {}
        self._resent = {}           # ticket id -> times sent again after a stale refusal

    def send(self, route, tag, value, origin, holder=None, timeout=None) -> CommandTicket:
        ticket = CommandTicket(route, tag, value, origin, timeout, holder=holder)
        self._send(ticket, ticket.created)
        return ticket

    def _send(self, ticket, ts):
        try:
            self.client.send(ticket.subsystem, ticket.tag, ticket.value, origin=ticket.origin, timeout=ticket.timeout,
                             cmd_id=ticket.id, ts=ts, holder=ticket.holder)
        except (zmq.ZMQError, KeyError, ValueError, TypeError) as e:
            log.exception("Could not send %s = %r", ticket.tag, ticket.value)
            self._resent.pop(ticket.id, None)
            ticket.resolve({"id": ticket.id, "status": UNREACHABLE, "reason": f"send failed: {e}", "tag": ticket.tag,
                            "value": ticket.value, "subsystem": ticket.subsystem, "origin": ticket.origin})
            return
        self._tickets[ticket.id] = ticket

    def pump(self, timeout_ms: int = 0):
        for result in self.client.poll(timeout_ms):
            ticket = self._tickets.pop(result.get("id"), None)
            if ticket is None:
                continue
            resent = self._resent.get(ticket.id, 0)
            if (result.get("status") == REJECTED and str(result.get("reason") or "").startswith(STALE)
                    and resent < STALE_RESENDS):
                # (never carried out, and still wanted: the service or this one was busy for a moment)
                self._resent[ticket.id] = resent + 1
                log.warning("%s = %r reached %s late (%s): sent again", ticket.tag, ticket.value, ticket.subsystem,
                            result.get("reason"))
                self._send(ticket, time.time())
                continue
            self._resent.pop(ticket.id, None)
            ticket.resolve(result)

    @property
    def inflight(self) -> int:
        return len(self._tickets)

    def close(self):
        for ticket in self._tickets.values():
            ticket.resolve({"id": ticket.id, "status": TIMEOUT, "reason": "sequencer stopping", "tag": ticket.tag,
                            "value": ticket.value, "subsystem": ticket.subsystem, "origin": ticket.origin})
        self._tickets.clear()
        self._resent.clear()
        self.client.close()


class SubsystemMap:
    """tag -> (subsystem, leasable), from the telemetry keys the services publish.
    keys_fn returns every telemetry key seen so far (TelemetryCache.keys)."""

    def __init__(self, keys_fn, clock=time.monotonic, refresh_s: float = 1.0):
        self._keys = keys_fn
        self._clock = clock
        self.refresh_s = refresh_s
        self._refreshed = None
        self.subsystems = set()
        self.leasable = set()

    def refresh(self):
        keys = self._keys()
        self.subsystems = {k[:-len(_PERMIT)] for k in keys if k.endswith(_PERMIT)}
        self.leasable = {k[:-len(_HOLDER)] for k in keys if k.endswith(_HOLDER)}
        self._refreshed = self._clock()

    def _best(self, tag):
        best = None
        for sub in self.subsystems:
            if tag.startswith(sub + ".") and (best is None or len(sub) > len(best)):
                best = sub
        return best

    def lookup(self, tag: str):
        now = self._clock()
        if self._refreshed is None or now - self._refreshed >= self.refresh_s:
            self.refresh()
        best = self._best(tag)
        return best, best is not None and best in self.leasable


class LeaseKeeper:
    """The leases one job holds."""

    def __init__(self, dispatcher, holder, purpose, subsystems, origin, on_lost=None, clock=time.monotonic):
        self.dispatcher = dispatcher
        self.holder = holder
        self.purpose = purpose
        self.subsystems = subsystems
        self.origin = origin
        self.on_lost = on_lost or (lambda subsystem, reason: None)
        self.clock = clock
        self.held = {}          # subsystem -> route
        self.lost = {}          # subsystem -> why it may not be commanded now
        self._pending = []      # (subsystem, op, ticket)
        self._next_renew = 0.0

    def _lease(self, route, subsystem, op):
        value = {"op": op, "holder": self.holder, "purpose": self.purpose, "ttl": LEASE_TTL_S}
        ticket = self.dispatcher.send(route, subsystem + LEASE_SUFFIX, value, origin=self.origin, holder=self.holder)
        self._pending.append((subsystem, op, ticket))
        return ticket

    def ensure(self, tag: str, route: str):
        """Called before every command. Claims the tag's subsystem if it isn't held yet.
        Returns (subsystem or None, refusal reason or None)."""
        sub, leasable = self.subsystems.lookup(tag)
        if sub is None or not leasable:
            return sub, None
        if sub in self.lost:
            return sub, self.lost[sub]
        if sub not in self.held:
            if not self.held:
                self._next_renew = self.clock() + RENEW_EVERY_S
            self.held[sub] = route
            self._lease(route, sub, "claim")
        return sub, None

    def tick(self):
        """Process lease replies, and renew every RENEW_EVERY_S."""
        for item in list(self._pending):
            sub, op, ticket = item
            if not ticket.done():
                continue
            self._pending.remove(item)
            if op == "release" or ticket.ok:
                continue
            if ticket.status == REJECTED and ticket.reason.startswith(STALE):
                continue        # (late every time it was sent: not a refusal of the lease - renewed as usual)
            if ticket.status == REJECTED and sub in self.held:
                self.lose(sub, ticket.reason)
            # timeout / unreachable: the service is down or busy - keep holding, the renewals
            # carry on (a restarted service gets the lease back with the next renewal)
        now = self.clock()
        if self.held and now >= self._next_renew:
            self._next_renew = now + RENEW_EVERY_S
            waiting = {sub for sub, op, _ in self._pending if op in ("claim", "renew")}
            for sub, route in list(self.held.items()):
                if sub not in waiting:
                    self._lease(route, sub, "renew")

    def lose(self, subsystem: str, reason: str):
        if subsystem in self.lost:
            return
        self.held.pop(subsystem, None)
        self.lost[subsystem] = reason
        log.warning("%s lost %s: %s", self.holder, subsystem, reason)
        self.on_lost(subsystem, reason)

    def forgive(self):
        """The job may claim what it lost again (a recipe resumed by the operator)."""
        self.lost.clear()

    def release_all(self):
        for sub, route in self.held.items():
            self._lease(route, sub, "release")
        self.held.clear()


class JobPort:
    """What a job sends its commands through. The recipe steps and routines get it as their
    'cmd_thread': send_command() has the HMI command thread's signature."""

    def __init__(self, dispatcher, keeper: LeaseKeeper, origin: str):
        self.dispatcher = dispatcher
        self.keeper = keeper
        self.origin = origin
        self._watch = []        # (subsystem, ticket) of commands to leased subsystems

    def send_command(self, subsystem, tag, value, origin=None, timeout=None, holder=None) -> CommandTicket:
        route = subsystem or route_for_tag(tag)
        origin = origin or self.origin
        sub, refusal = self.keeper.ensure(tag, route)
        if refusal:
            return CommandTicket.answered(route, tag, value, origin, REJECTED, refusal, holder=self.keeper.holder)
        ticket = self.dispatcher.send(route, tag, value, origin=origin, holder=self.keeper.holder, timeout=timeout)
        if sub is not None and sub in self.keeper.held:
            self._watch.append((sub, ticket))
        return ticket

    def send(self, tag, value, timeout=None) -> CommandTicket:
        return self.send_command(None, tag, value, timeout=timeout)

    def tick(self):
        """After the replies are in. A command refused because the operator took control counts
        as losing that lease at once (not only at the next renewal)."""
        waiting = []
        for sub, ticket in self._watch:
            if not ticket.done():
                waiting.append((sub, ticket))
            elif ticket.status == REJECTED and ticket.reason == TAKEN:
                self.keeper.lose(sub, TAKEN)
        self._watch = waiting
        self.keeper.tick()
