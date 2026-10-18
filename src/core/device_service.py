"""DeviceService: what every hardware service has in common.

A device service owns one device (or one bus of devices) and, once per POLL_INTERVAL:

    connect      if the device is not connected, try again - with back-off, logging the first
                 failure and then at most once a minute, and heartbeating meanwhile
    relay        (WATCH_RELAY) read the PLC telemetry: is the safety relay confirmed energised?
    cycle()      per-cycle work of the subclass (interlocks, a degauss sequence, ...)
    commands     answer every command: ok / rejected + reason / failed + reason
    poll         read the device into self.state - or publish unknowns (null) if it is not connected
    control      publish, per subsystem, who holds its control lease and whether an operator
                 command would be accepted now (permit + reason)
    publish      the flat telemetry dict on the PUB socket, and a heartbeat to the manager

Commands are pre-checked here before the subclass sees them: system-wide fault reset
(reset_faults), control leases (<subsystem>.cmd_lease, src/core/leases.py), the tag belonging to
one of SUBSYSTEMS, and the lease (a leased subsystem only takes commands carrying the holder's
id). An exception raised while handling a command fails that command with the reason; it does
not stop the service.

A subclass sets the class attributes (NAME, endpoints, SUBSYSTEMS, ...) and implements:

    connect_device() -> bool         try to connect (exceptions count as a failed attempt)
    is_connected() -> bool           the driver's view
    poll_device()                    read the device into self.state (publish unknowns itself if
                                     a read fails)
    publish_unknown()                device not connected: null for everything it measures
    handle_command(cmd, sub, leaf)   one pre-checked command; must answer it

and optionally:

    after_connect(previous_failures) e.g. restore or reset outputs after a reconnect
    on_relay_change(ok, why)         the relay permissive changed (log it, force outputs off ...)
    cycle()                          per-cycle work before the commands
    permit(sub) -> (bool, reason)    may an operator command `sub` now (default: connected, relay)
    reset_faults(cmd)                ion_beam.system.cmd_fault_reset
    close_device()                   on shutdown
"""
import json
import logging
import os
import time

import orjson
import zmq

from src.core import paths
from src.core.commands import CommandServer, bind_retrying
from src.core.event_helper import EventHelper
from src.core.leases import LEASE_SUFFIX, LeaseTable
from src.core.log_setup import Throttle
from src.core.network_map import ZMQ_PORT_HEARTBEAT, ZMQ_PORT_PLC_PUB
from src.core.relay_watch import RelayWatch

if os.name == "nt":   # 1 ms timer resolution for the loop pacing
    import ctypes

    ctypes.windll.winmm.timeBeginPeriod(1)

FAULT_RESET_TAG = "ion_beam.system.cmd_fault_reset"
HEARTBEAT_S = 0.5
CTRL_MODE_RETIRED = ("control modes were replaced by control leases: a running sequence holds what "
                     "it drives (stop it or take control in the HMI)")


class ReconnectBackoff:
    """When the next connection attempt is due: RECONNECT_DELAYS[n-1] after the n-th failure in a row."""

    def __init__(self, delays):
        self.delays = tuple(delays) or (1.0,)
        self.failures = 0
        self.next_at = 0.0

    def due(self, now: float) -> bool:
        return now >= self.next_at

    def failed(self, now: float) -> int:
        self.failures += 1
        self.next_at = now + self.delays[min(self.failures - 1, len(self.delays) - 1)]
        return self.failures

    def succeeded(self) -> int:
        previous, self.failures, self.next_at = self.failures, 0, 0.0
        return previous


class DeviceService:
    NAME = "service"                 # manager key: heartbeats, events, logs, command audit
    DEVICE_LABEL = "device"          # in messages: "magnet PSU", "SMU", ...
    PUB_ENDPOINT = None
    CMD_ENDPOINT = None
    TOPIC = b""
    POLL_INTERVAL = 0.1
    MAX_CMD_AGE = 0.5
    RECONNECT_DELAYS = (1.0, 2.0, 5.0, 10.0)
    WATCH_RELAY = False              # follow the safety relay (from the PLC service's telemetry)
    RELAY_MAX_AGE = 1.0              # no PLC data for this long = relay unknown = off
    SUBSYSTEMS = ()                  # tag prefixes this service controls, e.g. ("ion_beam.beamline.magnet",)
    UNLEASABLE = ()                  # subsystems that must stay operable (never leased)
    REGISTRY_SOURCE = None           # "source" of this service's tags in system_tags.json
    RETIRE_CTRL_MODE = True          # refuse the old software cmd_ctrl_mode (leases replace it)
    address = None                   # "ip:port" of the device, for messages

    def __init__(self):
        self.log = logging.getLogger(self.NAME)
        self.context = zmq.Context.instance()
        self.events = EventHelper(self.NAME)
        self.throttle = Throttle(60.0)
        self.backoff = ReconnectBackoff(self.RECONNECT_DELAYS)
        self._connect_error_types = set()
        self.state = {"timestamp": 0.0, "system.cycle_time_ms": 0.0}
        self.running = True
        self._last_hb = 0.0
        self._closed = False

        self.pub_socket = self.context.socket(zmq.PUB)
        self.pub_socket.setsockopt(zmq.SNDHWM, 5)
        self.pub_socket.setsockopt(zmq.LINGER, 0)
        bind_retrying(self.pub_socket, self.PUB_ENDPOINT)
        self.commands = CommandServer(self.CMD_ENDPOINT, self.NAME, context=self.context,
                                      events=self.events, max_age=self.MAX_CMD_AGE)
        self.hb_socket = self.context.socket(zmq.PUB)
        self.hb_socket.setsockopt(zmq.LINGER, 0)
        self.hb_socket.connect(ZMQ_PORT_HEARTBEAT)

        self.plc_socket = None
        self.relay = None
        self.relay_ok = False
        if self.WATCH_RELAY:
            self.plc_socket = self.context.socket(zmq.SUB)
            self.plc_socket.setsockopt(zmq.LINGER, 0)
            self.plc_socket.connect(ZMQ_PORT_PLC_PUB)
            self.plc_socket.setsockopt_string(zmq.SUBSCRIBE, "")
            self.relay = RelayWatch(self.plc_socket, max_age=self.RELAY_MAX_AGE)
            self.state["system.safe_to_run"] = 0.0

        self.leases = LeaseTable(self.SUBSYSTEMS, unleasable=self.UNLEASABLE)
        self.registry_limits = self.load_registry_limits() if self.REGISTRY_SOURCE else {}

    # ================================================================== hooks
    def connect_device(self) -> bool:
        raise NotImplementedError

    def is_connected(self) -> bool:
        raise NotImplementedError

    def poll_device(self):
        raise NotImplementedError

    def publish_unknown(self):
        raise NotImplementedError

    def handle_command(self, cmd, subsystem: str, leaf: str):
        cmd.reject(f"unsupported command '{leaf}'")

    def after_connect(self, previous_failures: int):
        self.events.log_general(f"{self.DEVICE_LABEL} connected"
                                + (f" after {previous_failures} failed attempt(s)" if previous_failures else ""))

    def on_relay_change(self, ok: bool, why: str):
        self.events.log_general(f"{self.DEVICE_LABEL}: safety permissive restored" if ok
                                else f"{self.DEVICE_LABEL}: {why}", severity="INFO" if ok else "WARNING")

    def cycle(self):
        pass

    def permit(self, subsystem: str):
        if not self.is_connected():
            return False, f"{self.DEVICE_LABEL} not connected"
        if self.WATCH_RELAY and not self.relay_ok:
            return False, "safety relay de-energised (or no PLC data)"
        return True, ""

    def reset_faults(self, cmd):
        cmd.ok("nothing to reset here")

    def close_device(self):
        pass

    # ================================================================== loop
    def run(self):
        next_tick = time.perf_counter() + self.POLL_INTERVAL
        try:
            while self.running:
                self.run_cycle()
                next_tick = self._pace(next_tick)
        except KeyboardInterrupt:
            self.log.info("Interrupted - shutting down")
        finally:
            self.shutdown()

    def run_cycle(self):
        """One pass of the loop (the tests drive it directly)."""
        started = time.perf_counter()
        if not self.is_connected():
            self._try_connect()
        if self.WATCH_RELAY:
            self.update_relay()
        self.cycle()
        self.process_commands()
        if self.is_connected():
            self.poll_device()
        else:
            self.publish_unknown()
        self.publish_control_state()
        self.state["timestamp"] = time.time()
        self.state["system.cycle_time_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
        self.publish()
        self.heartbeat()

    def _pace(self, next_tick: float) -> float:
        """Wait for next_tick (sleep, then spin the last 2 ms); after an overrun, skip to the next
        boundary instead of running a burst of catch-up cycles. Returns the tick after it."""
        now = time.perf_counter()
        if next_tick <= now:
            next_tick += (int((now - next_tick) / self.POLL_INTERVAL) + 1) * self.POLL_INTERVAL
        remaining = next_tick - time.perf_counter()
        if remaining > 0.002:
            time.sleep(remaining - 0.002)
        while time.perf_counter() < next_tick:
            pass
        return next_tick + self.POLL_INTERVAL

    def stop(self):
        self.running = False

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.close_device()
        except Exception:   # whatever the driver does on the way out, the sockets still get closed
            self.log.exception("close_device failed")
        self.commands.close()
        for sock in (self.pub_socket, self.hb_socket, self.plc_socket):
            if sock is not None:
                sock.close(0)

    # ================================================================== connection
    def _try_connect(self):
        now = time.time()
        if not self.backoff.due(now):
            return
        error = None
        try:
            ok = bool(self.connect_device())
        except Exception as e:   # drivers raise socket / Modbus / SCPI errors alike: a failed attempt
            ok, error = False, e
            if not isinstance(e, OSError) and type(e) not in self._connect_error_types:
                self._connect_error_types.add(type(e))      # the traceback once per kind (a bug shows here)
                self.log.warning("connect_device raised %s", type(e).__name__, exc_info=True)
        if ok:
            previous = self.backoff.succeeded()
            self.throttle.reset("connect")
            self.after_connect(previous)
        else:
            self.backoff.failed(time.time())
            if (n := self.throttle.ready("connect")) is not None:
                where = f" at {self.address}" if self.address else ""
                detail = f": {type(error).__name__}: {error}" if error else ""
                self.events.log_general(f"{self.DEVICE_LABEL}{where} not reachable{detail}{Throttle.suffix(n)}",
                                        severity="WARNING")
        self.heartbeat()    # a slow connection attempt must not look like a hung service

    # ================================================================== safety relay
    def update_relay(self):
        """Every PLC message since the last check counts (src/core/relay_watch.py)."""
        ok = self.relay.check()
        if ok != self.relay_ok:
            why = "" if ok else ("no PLC telemetry" if time.time() - self.relay.last_rx >= self.RELAY_MAX_AGE
                                 else "safety relay de-energised (or its state unknown)")
            self.relay_ok = ok
            self.on_relay_change(ok, why)
        self.relay_ok = ok
        self.state["system.safe_to_run"] = 1.0 if ok else 0.0
        return ok

    # ================================================================== commands
    def process_commands(self):
        for cmd in self.commands.poll():
            try:
                self.dispatch(cmd)
            except Exception as e:   # one bad command must neither go unanswered nor stop the service
                self.log.exception("Handling %s failed", cmd)
                cmd.fail(f"internal error in {self.NAME}: {type(e).__name__}: {e}")
        self.commands.finish()

    def dispatch(self, cmd):
        tag = cmd.tag
        if tag == FAULT_RESET_TAG:
            if cmd.value:
                self.reset_faults(cmd)
            else:
                cmd.ok("nothing to do")
            return
        if tag.endswith(LEASE_SUFFIX):
            self.handle_lease(cmd, tag[:-len(LEASE_SUFFIX)])
            return
        subsystem = self.subsystem_of(tag)
        if subsystem is None:
            cmd.reject(f"not a {self.DEVICE_LABEL} tag")
            return
        leaf = tag[len(subsystem) + 1:]
        if leaf == "cmd_ctrl_mode" and self.RETIRE_CTRL_MODE:
            cmd.reject(CTRL_MODE_RETIRED)
            return
        refusal = self.leases.refusal(subsystem, cmd.holder)
        if refusal:
            cmd.reject(refusal)
            return
        self.handle_command(cmd, subsystem, leaf)

    def handle_lease(self, cmd, subsystem):
        ok, reason, event = self.leases.handle(subsystem, cmd.value, origin=cmd.origin)
        if event:
            taken = isinstance(cmd.value, dict) and cmd.value.get("op") == "take"
            self.events.log_general(event, severity="WARNING" if taken else "INFO",
                                    metadata={"subsystem": subsystem, "lease": cmd.value})
        if ok:
            cmd.ok(reason)
        else:
            cmd.reject(reason)

    def subsystem_of(self, tag: str):
        best = None
        for sub in self.SUBSYSTEMS:
            if tag.startswith(sub + ".") and (best is None or len(sub) > len(best)):
                best = sub
        return best

    @staticmethod
    def number(cmd):
        """The command value as a float, or None after rejecting the command."""
        try:
            return float(cmd.value)
        except (TypeError, ValueError):
            cmd.reject(f"value {cmd.value!r} is not a number")
            return None

    def within_limits(self, cmd, value: float, unit: str = "") -> bool:
        """Registry min/max for the command's tag; rejects the command (and returns False) if out."""
        lim = self.registry_limits.get(cmd.tag, {})
        lo, hi = lim.get("min_val"), lim.get("max_val")
        if lo is None or hi is None:
            cmd.reject("no min/max limits configured for this tag in system_tags.json")
            return False
        if not lo <= value <= hi:
            u = f" {unit}" if unit else ""
            cmd.reject(f"{value:g}{u} is outside the allowed range [{lo:g}, {hi:g}]{u}")
            return False
        return True

    # ================================================================== telemetry
    def publish_control_state(self):
        """Per subsystem: lease holder, and whether an operator command would be accepted now."""
        self.leases.publish_into(self.state)
        for sub in self.SUBSYSTEMS:
            try:
                ok, reason = self.permit(sub)
            except Exception as e:   # a permit bug shows as 'not permitted', never as permitted
                self.log.exception("permit(%s) failed", sub)
                ok, reason = False, f"permit check failed: {e}"
            if ok:
                refusal = self.leases.refusal(sub, None)
                if refusal:
                    ok, reason = False, refusal
            self.state[f"{sub}.permit"] = 1.0 if ok else 0.0
            self.state[f"{sub}.permit_reason"] = "" if ok else reason

    def publish(self):
        try:
            self.pub_socket.send_multipart([self.TOPIC, orjson.dumps(self.state)])
        except (zmq.ZMQError, TypeError) as e:
            if (n := self.throttle.ready("publish")) is not None:
                self.log.error("Publish failed: %s%s", e, Throttle.suffix(n))

    def heartbeat(self, force: bool = False):
        now = time.time()
        if force or now - self._last_hb >= HEARTBEAT_S:
            try:
                self.hb_socket.send_json({"service": self.NAME, "ts": now}, zmq.NOBLOCK)
            except zmq.ZMQError:
                pass    # nobody listening (manager restarting): next beat will do
            self._last_hb = now

    # ================================================================== config
    def load_config(self, filename: str, defaults: dict = None) -> dict:
        config = dict(defaults or {})
        path = paths.config_path(filename)
        try:
            with open(path, "r") as f:
                config.update(json.load(f))
        except (OSError, ValueError) as e:
            self.events.log_general(f"{filename} not loaded ({e}) - using built-in defaults", severity="WARNING")
        return config

    def load_registry_limits(self) -> dict:
        limits = {}
        try:
            with open(paths.REGISTRY_PATH, "r") as f:
                registry = json.load(f)
        except (OSError, ValueError) as e:
            self.events.log_general(f"Tag registry not loaded - setpoints will be refused: {e}", severity="ERROR")
            return limits
        for tag, meta in registry.items():
            if meta.get("source") == self.REGISTRY_SOURCE and meta.get("auto_controllable"):
                limits[tag] = {"min_val": meta.get("min_val"), "max_val": meta.get("max_val")}
        return limits
