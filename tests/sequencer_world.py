"""An in-process stand-in for the device services, for driving sequencer jobs tick by tick.

FakeClient replaces the CommandClient: every command is answered at the next poll() by a
FakeDevice that has a real LeaseTable, so lease claims, renewals, takes and refusals behave as
in a DeviceService. World ties it together with a TelemetryCache and a fake clock; world.step()
publishes the devices' telemetry, pumps the replies and ticks the job.
"""
import math

from src.core import commands as C
from src.core.leases import LEASE_SUFFIX, LeaseTable
from src.core.telemetry_cache import TelemetryCache
from src.sequencer.jobs import JobContext
from src.sequencer.port import Dispatcher, SubsystemMap


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeDevice:
    def __init__(self, topic, subsystems, unleasable=(), handler=None, clock=None):
        self.topic = topic
        self.subsystems = tuple(subsystems)
        self.leases = LeaseTable(subsystems, unleasable=unleasable, clock=clock, wall=clock)
        self.handler = handler            # (tag, value) -> None (ok) or a refusal reason
        self.values = {}                  # tag -> last value commanded
        self.log = []                     # (tag, value, holder, status)
        self.online = True
        self.late = 0                     # the next n requests reach it late: refused as stale (CommandServer)

    def subsystem_of(self, tag):
        best = None
        for sub in self.subsystems:
            if tag.startswith(sub + ".") and (best is None or len(sub) > len(best)):
                best = sub
        return best

    def handle(self, tag, value, holder, origin):
        if self.late > 0:
            self.late -= 1
            self.log.append((tag, value, holder, C.REJECTED))
            return C.REJECTED, f"{C.STALE} sent 0.73 s ago (limit 0.50 s)"
        if tag.endswith(LEASE_SUFFIX):
            ok, reason, _ = self.leases.handle(tag[:-len(LEASE_SUFFIX)], value, origin)
            status = C.OK if ok else C.REJECTED
        else:
            sub = self.subsystem_of(tag)
            reason = None
            if sub is None:
                reason = "not a fake device tag"
            else:
                reason = self.leases.refusal(sub, holder)
            if reason is None and self.handler is not None:
                reason = self.handler(tag, value)
            status = C.REJECTED if reason else C.OK
            if status == C.OK:
                self.values[tag] = value
        self.log.append((tag, value, holder, status))
        return status, reason or ""

    def telemetry(self) -> dict:
        state = {}
        self.leases.publish_into(state)
        for sub in self.subsystems:
            state[f"{sub}.permit"] = 1.0
            state[f"{sub}.permit_reason"] = ""
        return state


class FakeClient:
    def __init__(self, devices: dict):
        self.devices = devices            # route -> FakeDevice
        self._queue = []
        self.sent = []

    def connect_all(self):
        pass

    def send(self, subsystem, tag, value, origin="hmi", timeout=None, cmd_id=None, ts=None, extra=None,
             holder=None):
        self.sent.append((subsystem, tag, value, holder))
        self._queue.append((subsystem, tag, value, origin, holder, cmd_id))
        return cmd_id

    def poll(self, timeout_ms=0):
        out = []
        queue, self._queue = self._queue, []
        for route, tag, value, origin, holder, cmd_id in queue:
            device = self.devices.get(route)
            if device is None or not device.online:
                out.append({"id": cmd_id, "status": C.UNREACHABLE, "reason": f"{route} service is not running",
                            "tag": tag, "value": value, "subsystem": route, "origin": origin})
                continue
            status, reason = device.handle(tag, value, holder, origin)
            out.append({"id": cmd_id, "status": status, "reason": reason, "tag": tag, "value": value,
                        "subsystem": route, "origin": origin})
        return out

    def close(self):
        pass


MAGNET = "ion_beam.beamline.magnet"
SMU = "ion_beam.beamline.faraday.smu"
STEERER = "ion_beam.beamline.steerer_magnet"


def beam_at(amps: float) -> float:
    """A fake mass spectrum: two peaks, the bigger at 25 A."""
    return 1e-9 + 2e-7 * math.exp(-((amps - 25.0) / 0.3) ** 2) + 5e-8 * math.exp(-((amps - 22.0) / 0.3) ** 2)


class World:
    """Magnet, steerer and SMU services plus a PLC that publishes voltages, all in-process."""

    def __init__(self, registry=None):
        self.clock = Clock()
        self.devices = {
            "magnet": FakeDevice("MAGNET_DATA", [MAGNET], clock=self.clock),
            "steerer_magnet": FakeDevice("STEERER_DATA", [STEERER], clock=self.clock),
            "smu": FakeDevice("SMU_DATA", [SMU], clock=self.clock),
            "plc": FakeDevice("PLC_DATA", ["ion_beam.system", "ion_beam.source.chamber", "ion_beam.source.target",
                                           "ion_beam.beamline.diagnostics"],
                              unleasable=["ion_beam.system", "ion_beam.source.chamber"], clock=self.clock),
        }
        self.client = FakeClient(self.devices)
        self.dispatcher = Dispatcher(self.client)
        self.telemetry = TelemetryCache(clock=self.clock)
        self.subsystems = SubsystemMap(self.telemetry.keys, clock=self.clock)
        self.events = []
        self.ctx = JobContext(self.dispatcher, self.telemetry, self.subsystems, events=_Events(self.events),
                              clock=self.clock, wall=self.clock, instance="t", registry=registry or {})
        self.extra = {}                   # topic -> {tag: value} published every step
        self.silent = set()               # topics not publishing
        self.publish()

    # ------------------------------------------------------------------ telemetry
    def physics(self) -> dict:
        mag = self.devices["magnet"].values.get(f"{MAGNET}.sp_requested_current", 0.0)
        return {
            "MAGNET_DATA": {f"{MAGNET}.rb_current": float(mag), f"{MAGNET}.sp_actual_current": float(mag)},
            "SMU_DATA": {f"{SMU}.rb_current": beam_at(float(mag)), f"{SMU}.stat_range_settled": 1.0},
            "PLC_DATA": {"ion_beam.source.target.rb_voltage": 4.0, "ion_beam.source.extraction.rb_voltage": 10.0},
            "STEERER_DATA": {f"{STEERER}.sp_actual_current": float(
                self.devices["steerer_magnet"].values.get(f"{STEERER}.sp_requested_current", 5.0))},
        }

    def publish(self):
        physics = self.physics()
        for device in self.devices.values():
            if device.topic in self.silent:
                continue
            payload = device.telemetry()
            payload.update(physics.get(device.topic, {}))
            payload.update(self.extra.get(device.topic, {}))
            self.telemetry.ingest(device.topic, payload)

    def set(self, topic, tag, value):
        self.extra.setdefault(topic, {})[tag] = value

    # ------------------------------------------------------------------ running
    def step(self, job, dt=0.02, n=1):
        for _ in range(n):
            self.clock.t += dt
            self.publish()
            self.dispatcher.pump()
            job.service_tick()
        self.dispatcher.pump()            # the replies to what this tick sent

    def run_until(self, job, predicate, timeout=60.0, dt=0.02):
        end = self.clock.t + timeout
        while self.clock.t < end:
            self.step(job, dt)
            if predicate():
                return
        raise AssertionError(f"not reached in {timeout} s (simulated); job: {job.state} - {job.status}")

    def take(self, route, subsystem):
        """The operator takes control (what the HMI's Take control button sends)."""
        return self.devices[route].leases.handle(subsystem, {"op": "take"}, origin="hmi")


class _Events:
    def __init__(self, sink):
        self.sink = sink

    def log_general(self, message, severity="INFO", event_type="GENERAL", metadata=None, timestamp_override=None):
        self.sink.append((event_type, severity, message))

    def log_user_marker(self, ts, text, color):
        self.sink.append(("USER_MARKER", "INFO", text))
