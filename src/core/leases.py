"""Control leases: which automation currently owns a subsystem.

A sequence (recipe, mass scan, optimiser - run by the sequencer service) claims the subsystems
it drives, e.g. "ion_beam.beamline.magnet". While it holds the lease, the service that owns
the subsystem only accepts commands for it that carry the holder's id, so two sequences - or a
sequence and a stray click - can't drive the same thing at once. Leases expire unless renewed
(the holder renews about once a second), so a sequence that dies frees its subsystems within
a few seconds.

The operator can always take control: a "take" clears the lease at once, and the holder's next
renewal is refused ("control was taken by the operator"), which makes the sequence stop or
pause with that message instead of carrying on. The holder may claim again later (e.g. when
the operator resumes a paused recipe).

Lease commands are ordinary acknowledged commands to the owning service:
    tag   "<subsystem>.cmd_lease"
    value {"op": "claim" | "renew" | "release" | "take", "holder": "...", "purpose": "...", "ttl": s}
Every service publishes, per subsystem, "<subsystem>.lease_holder" and ".lease_purpose"
(null when free).
"""
import time
from dataclasses import dataclass

LEASE_SUFFIX = ".cmd_lease"
DEFAULT_TTL = 5.0
MAX_TTL = 60.0
OPS = ("claim", "renew", "release", "take")


@dataclass
class Lease:
    subsystem: str
    holder: str
    purpose: str
    expires: float      # on the table's clock (monotonic)
    since: float        # wall-clock time it was claimed


class LeaseTable:
    """The leases of one service's subsystems. Not thread-safe: used from the service loop."""

    def __init__(self, subsystems=(), clock=time.monotonic, wall=time.time, unleasable=()):
        self.subsystems = set(subsystems)
        self.unleasable = set(unleasable)
        self._clock = clock
        self._wall = wall
        self._leases = {}
        self._revoked = {}      # subsystem -> holder whose lease the operator took

    # ------------------------------------------------------------------ queries
    def active(self, subsystem):
        lease = self._leases.get(subsystem)
        if lease is not None and self._clock() >= lease.expires:
            del self._leases[subsystem]
            return None
        return lease

    def leases(self) -> dict:
        return {sub: lease for sub in list(self._leases) if (lease := self.active(sub)) is not None}

    def refusal(self, subsystem, holder):
        """Why a command from `holder` (None = no lease, e.g. the operator) may not touch
        `subsystem` now, or None if it may."""
        lease = self.active(subsystem)
        if lease is None or lease.holder == holder:
            return None
        return (f"{subsystem.split('.')[-1]} is controlled by {lease.purpose or lease.holder} - "
                f"stop it, or take control, first")

    # ------------------------------------------------------------------ operations
    def handle(self, subsystem, value, origin=""):
        """Apply a lease command. Returns (ok, reason, event_text or None)."""
        if subsystem not in self.subsystems:
            return False, f"no subsystem {subsystem} here", None
        if subsystem in self.unleasable:
            return False, f"{subsystem} can't be leased (operator controls must stay available)", None
        if not isinstance(value, dict):
            return False, "lease value must be {op, holder, purpose, ttl}", None
        op = value.get("op")
        holder = str(value.get("holder") or "")
        purpose = str(value.get("purpose") or holder)
        try:
            ttl = min(MAX_TTL, max(0.5, float(value.get("ttl", DEFAULT_TTL))))
        except (TypeError, ValueError):
            return False, f"bad ttl {value.get('ttl')!r}", None
        if op not in OPS:
            return False, f"unknown lease op {op!r} (expected one of {', '.join(OPS)})", None
        if op != "take" and not holder:
            return False, "lease commands need a holder id", None
        return getattr(self, op)(subsystem, holder=holder, purpose=purpose, ttl=ttl, origin=origin)

    def claim(self, subsystem, holder, purpose="", ttl=DEFAULT_TTL, origin=""):
        lease = self.active(subsystem)
        if lease is not None and lease.holder != holder:
            return False, f"already controlled by {lease.purpose or lease.holder}", None
        self._revoked.pop(subsystem, None)
        new = lease is None
        self._leases[subsystem] = Lease(subsystem, holder, purpose, self._clock() + ttl,
                                        lease.since if lease else self._wall())
        return True, "claimed" if new else "renewed", (f"{purpose or holder} took control of {subsystem}"
                                                       if new else None)

    def renew(self, subsystem, holder, purpose="", ttl=DEFAULT_TTL, origin=""):
        lease = self.active(subsystem)
        if lease is not None and lease.holder == holder:
            lease.expires = self._clock() + ttl
            return True, "renewed", None
        if self._revoked.get(subsystem) == holder:
            return False, "control was taken by the operator", None
        if lease is not None:
            return False, f"controlled by {lease.purpose or lease.holder}", None
        # Free again (the lease expired, or this service restarted): carry on holding it
        return self.claim(subsystem, holder, purpose, ttl, origin)

    def release(self, subsystem, holder, purpose="", ttl=0.0, origin=""):
        lease = self.active(subsystem)
        if lease is None:
            self._revoked.pop(subsystem, None)
            return True, "not held", None
        if lease.holder != holder:
            return False, f"held by {lease.purpose or lease.holder}, not by {holder}", None
        del self._leases[subsystem]
        return True, "released", f"{lease.purpose or holder} released {subsystem}"

    def take(self, subsystem, holder="", purpose="", ttl=0.0, origin=""):
        """Operator take-over: always succeeds."""
        lease = self.active(subsystem)
        if lease is None:
            return True, "not held by anything", None
        del self._leases[subsystem]
        self._revoked[subsystem] = lease.holder
        who = origin or "operator"
        return True, f"took control from {lease.purpose or lease.holder}", \
            f"{who} took control of {subsystem} from {lease.purpose or lease.holder}"

    # ------------------------------------------------------------------ telemetry
    def publish_into(self, state: dict):
        for sub in self.subsystems:
            if sub in self.unleasable:
                continue
            lease = self.active(sub)
            state[f"{sub}.lease_holder"] = lease.holder if lease else None
            state[f"{sub}.lease_purpose"] = (lease.purpose or lease.holder) if lease else None
