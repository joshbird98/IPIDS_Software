"""Sequencer jobs: one recipe, mass scan or optimiser run.

The service ticks every active job about 50 times a second (Job.service_tick): lease
bookkeeping first, then the job's own tick(). Jobs never block: a recipe advances its steps one
evaluation per tick; scans are written as generators (GeneratorJob) that yield whenever they
wait, so they read like the old worker threads but run in the service loop.

Job commands arrive as handle(verb, value) -> (ok, reason, data); a method cmd_<verb> handles
each verb (stop, pause, resume, ack, goto, fetch).
"""
import logging
import time

from src.sequencer.port import JobPort, LeaseKeeper
from src.sequencer.protocol import ACTIVE, COMPLETE, FAILED, FINAL, RUNNING, STOPPED, STOPPING, key


class JobFailed(Exception):
    """Raised inside a job to end it as failed, with this message."""


class JobContext:
    """What the service hands to every job."""

    def __init__(self, dispatcher, telemetry, subsystems, events=None, clock=time.monotonic, wall=time.time,
                 instance="", registry=None):
        self.dispatcher = dispatcher
        self.telemetry = telemetry          # TelemetryCache: get_fresh(tag), snapshot()
        self.subsystems = subsystems        # SubsystemMap
        self.events = events                # EventHelper (or None in tests)
        self.clock = clock
        self.wall = wall
        self.instance = instance            # distinguishes this sequencer process in holder ids
        self.registry = registry or {}


def leaf(subsystem: str) -> str:
    return subsystem.split(".")[-1].replace("_", " ")


class Job:
    KIND = "job"

    def __init__(self, ctx: JobContext, run: int, purpose: str):
        self.ctx = ctx
        self.run = run
        self.purpose = purpose
        self.holder = f"seq-{self.KIND}-{run}" + (f"-{ctx.instance}" if ctx.instance else "")
        self.log = logging.getLogger(f"sequencer.{self.KIND}")
        self.state = RUNNING
        self.status = "starting"
        self.message = ""
        self.started = ctx.wall()
        self.ended = None
        self.keeper = LeaseKeeper(ctx.dispatcher, self.holder, purpose, ctx.subsystems, origin=self.KIND,
                                  on_lost=self.lease_lost, clock=ctx.clock)
        self.port = JobPort(ctx.dispatcher, self.keeper, origin=self.KIND)
        self._released = False

    # ------------------------------------------------------------------ helpers
    @property
    def active(self) -> bool:
        return self.state in ACTIVE

    def now(self) -> float:
        return self.ctx.clock()

    def fresh(self, tag):
        """Live value of a tag, or None (stale, unknown or never seen)."""
        return self.ctx.telemetry.get_fresh(tag) if tag else None

    def fresh_float(self, tag):
        value = self.fresh(tag)
        try:
            return None if value is None else float(value)
        except (TypeError, ValueError):
            return None

    def send(self, tag, value, timeout=None):
        """Send a command under this job's lease (claimed first if need be); returns its ticket."""
        return self.port.send(tag, value, timeout=timeout)

    def event(self, message, severity="INFO", marker_color=None):
        if self.ctx.events is None:
            return
        self.ctx.events.log_general(message, severity=severity, metadata={"job": self.KIND, "run": self.run})
        if marker_color:
            self.ctx.events.log_user_marker(self.ctx.wall(), message, marker_color)

    # ------------------------------------------------------------------ service interface
    def service_tick(self):
        """Lease bookkeeping, then the job's own work; once it has ended, release its leases."""
        if self.state not in FINAL:
            self.port.tick()
            if self.state not in FINAL:
                self.tick()
        if self.state in FINAL:
            self.release()

    def release(self):
        """Release every lease still held (once)."""
        if not self._released:
            self._released = True
            self.keeper.release_all()

    def handle(self, verb: str, value):
        method = getattr(self, f"cmd_{verb}", None)
        if method is None:
            return False, f"'{verb}' is not a {self.KIND.replace('_', ' ')} command", None
        return method(value)

    def finish(self, state: str, message: str):
        self.state = state
        self.message = message
        self.status = message
        self.ended = self.ctx.wall()
        severity = {COMPLETE: "INFO", STOPPED: "WARNING", FAILED: "ERROR"}.get(state, "INFO")
        self.event(f"{self.purpose}: {message}", severity=severity)
        self.log.log(logging.INFO if state == COMPLETE else logging.WARNING, "%s: %s", self.purpose, message)

    def telemetry(self) -> dict:
        k = self.KIND
        return {key(k, "state"): self.state, key(k, "run"): self.run, key(k, "status"): self.status,
                key(k, "message"): self.message, key(k, "purpose"): self.purpose,
                key(k, "holder"): self.holder if self.active else None,
                key(k, "leases"): sorted(self.keeper.held), key(k, "started"): self.started,
                key(k, "ended"): self.ended}

    # ------------------------------------------------------------------ to implement
    def tick(self):
        raise NotImplementedError

    def lease_lost(self, subsystem: str, reason: str):
        raise NotImplementedError

    def cmd_stop(self, value=None):
        raise NotImplementedError

    def abort(self, reason: str):
        """Stop for a reason other than the operator's Stop (e.g. the sequencer shutting down)."""
        raise NotImplementedError


class GeneratorJob(Job):
    """A job written as a generator: body() yields whenever it waits; cleanup() runs after it
    whether it completed, was stopped, or failed (e.g. to put the SMU integration time back)."""

    CLEANUP_TIMEOUT_S = 15.0

    def __init__(self, ctx, run, purpose):
        super().__init__(ctx, run, purpose)
        self.stop_reason = None
        self._gen = None
        self._phase = "body"
        self._outcome = None
        self._cleanup_deadline = None

    @property
    def stopping(self) -> bool:
        return self.stop_reason is not None

    def body(self):
        raise NotImplementedError

    def cleanup(self):
        yield from ()

    def complete_message(self) -> str:
        return "complete"

    def running_status(self) -> str:
        """Status text while the body runs (updated every tick)."""
        return self.status

    def stop(self, reason: str):
        if self.stop_reason is None:
            self.stop_reason = reason

    def cmd_stop(self, value=None):
        if self.state in FINAL:
            return True, "not running", None
        self.stop("Stopped by operator")
        return True, "stopping", None

    def abort(self, reason: str):
        self.stop(reason)

    def lease_lost(self, subsystem, reason):
        self.stop(f"Stopped: {leaf(subsystem)} - {reason}")

    def tick(self):
        if self._phase == "body":
            if self._gen is None:
                self._gen = self.body()
            try:
                next(self._gen)
                self.status = self.running_status()
                return
            except StopIteration:
                self._outcome = (STOPPED, self.stop_reason) if self.stopping else (COMPLETE, self.complete_message())
            except JobFailed as e:
                self._outcome = (FAILED, str(e))
            except Exception as e:   # a bug in one job ends that job, never the sequencer
                self.log.exception("%s failed", self.purpose)
                self._outcome = (FAILED, f"internal error: {type(e).__name__}: {e}")
            self._phase = "cleanup"
            self.state = STOPPING
            self.status = "finishing: " + self._outcome[1]
            self._gen = self.cleanup()
            self._cleanup_deadline = self.now() + self.CLEANUP_TIMEOUT_S
        if self._phase == "cleanup":
            try:
                if self.now() < self._cleanup_deadline:
                    next(self._gen)
                    return
                self.log.warning("%s: clean-up did not finish within %.0f s", self.purpose, self.CLEANUP_TIMEOUT_S)
            except StopIteration:
                pass
            except Exception:   # as above: report it, and still finish (and release the leases)
                self.log.exception("%s: clean-up failed", self.purpose)
            self._gen.close()
            self._phase = "done"
            self.finish(*self._outcome)

    # ------------------------------------------------------------------ for bodies
    def sleep(self, seconds: float, interruptible: bool = True):
        """Wait (yielding); returns early if a stop is requested, unless interruptible=False."""
        end = self.now() + seconds
        while self.now() < end:
            if interruptible and self.stopping:
                return
            yield

    def wait_reply(self, ticket):
        """Until the service has answered (the client times out after a few seconds at most)."""
        while not ticket.done():
            yield
        return ticket

    def fail(self, message: str):
        raise JobFailed(message)
