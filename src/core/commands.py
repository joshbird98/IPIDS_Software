"""Acknowledged commands between clients (HMI, recipes, scripts) and the services.

Telemetry stays publish/subscribe - many listeners, nobody needs to know who
is listening. Commands are different: the sender needs to know whether the
command arrived and what happened to it. So every service binds a ZeroMQ
ROUTER socket on its existing *_cmd port, clients connect DEALER sockets, and
every request gets exactly one reply.

Request   {"id": "...", "tag": "...", "value": ..., "ts": <unix time of the click>, "origin": "hmi"}
Reply     {"id": "...", "status": ..., "reason": "...", "tag": "...", "service": "...", "ts": ...}

Status values
    ok          executed (written to the PLC / power supply)
    accepted    received and passed every check; the service did not report a separate result
    rejected    refused before touching hardware: out of limits, locked out, interlock,
                stale (sent too long ago), unknown tag...
    failed      the service tried but the device write failed / the device is not answering
Produced on the client side:
    timeout     no reply in time (service busy, hung, or restarted mid-request)
    unreachable the service is not running / not connected, nothing was sent

Freshness: a command whose `ts` is older than the service's max_age when the
service gets to it is rejected as stale, so clicks made while a service (or
the HMI) was frozen are not all applied in a burst when it recovers.
"""
import itertools
import logging
import os
import threading
import time
import uuid

import orjson
import zmq

OK = "ok"
ACCEPTED = "accepted"
REJECTED = "rejected"
FAILED = "failed"
TIMEOUT = "timeout"
UNREACHABLE = "unreachable"
SUCCESS = frozenset({OK, ACCEPTED})

DEFAULT_MAX_AGE = 0.5      # seconds; matches the services' old MAX_CMD_AGE
DEFAULT_TIMEOUT = 3.0      # seconds a client waits for a reply

log = logging.getLogger("commands")


def is_success(status: str) -> bool:
    return status in SUCCESS


def describe(result: dict) -> str:
    """One-line human summary of a reply, e.g. for the HMI or a recipe failure message."""
    tag = result.get("tag", "?")
    short = ".".join(tag.split(".")[-2:]) if tag.count(".") >= 2 else tag
    value = result.get("value")
    status = result.get("status", "?")
    reason = result.get("reason") or ""
    text = f"{short} = {value!r}: {status}"
    return f"{text} - {reason}" if reason else text


# --------------------------------------------------------------------------------------
# Server side (inside each service)
# --------------------------------------------------------------------------------------
class Command:
    """One received request. Call exactly one of ok() / accept() / reject() / fail()."""

    __slots__ = ("id", "tag", "value", "ts", "origin", "received", "replied", "status", "reason",
                 "raw", "_server", "_envelope")

    def __init__(self, server, envelope, msg, received):
        self._server = server
        self._envelope = envelope
        self.raw = msg
        self.id = str(msg.get("id") or "")
        self.tag = str(msg.get("tag") or "")
        self.value = msg.get("value")
        try:
            self.ts = float(msg.get("ts") or 0.0)
        except (TypeError, ValueError):
            self.ts = 0.0
        self.origin = str(msg.get("origin") or "unknown")
        self.received = received
        self.replied = False
        self.status = None
        self.reason = ""

    @property
    def age(self) -> float:
        return self.received - self.ts

    @property
    def short_tag(self) -> str:
        return self.tag.split(".")[-1]

    def ok(self, note: str = ""):
        self._reply(OK, note)

    def accept(self, note: str = ""):
        self._reply(ACCEPTED, note)

    def reject(self, reason: str):
        self._reply(REJECTED, reason)

    def fail(self, reason: str):
        self._reply(FAILED, reason)

    def _reply(self, status, reason):
        if self.replied:
            return
        self.replied = True
        self.status = status
        self.reason = reason or ""
        self._server._send_reply(self)

    def __repr__(self):
        return f"Command({self.tag}={self.value!r}, origin={self.origin}, id={self.id})"


class CommandServer:
    """ROUTER socket a service polls once per loop.

        self.commands = CommandServer(ZMQ_PORT_PLC_CMD, "service_plc", events=self.events)
        for cmd in self.commands.poll():
            if cmd.tag not in self.tags:
                cmd.reject("unknown tag"); continue
            ...
            cmd.ok() if written else cmd.fail("write failed")

    Commands older than max_age are rejected as stale before they reach the
    service code. A command the service forgets to answer is answered
    'accepted' at the next poll() (or finish()), so a client never waits for a
    reply that will not come. Every reply is also written to events.db
    (event type COMMAND) when an EventHelper is given - the audit trail.
    """

    def __init__(self, endpoint: str, service: str, context=None, events=None,
                 max_age=DEFAULT_MAX_AGE, audit=True):
        self.service = service
        self.max_age = max_age
        self.events = events
        self.audit = audit
        self._log = logging.getLogger(f"{service}.commands")
        self._audit_log = logging.getLogger(f"{service}.audit")
        ctx = context or zmq.Context.instance()
        self.sock = ctx.socket(zmq.ROUTER)
        self.sock.setsockopt(zmq.LINGER, 0)
        self.sock.setsockopt(zmq.RCVHWM, 1000)
        self.sock.bind(endpoint)
        self._pending = []

    def poll(self, max_messages: int = 200):
        """Non-blocking: returns the fresh, well-formed commands that arrived since the last call."""
        self.finish()
        fresh = []
        for _ in range(max_messages):
            try:
                frames = self.sock.recv_multipart(zmq.NOBLOCK)
            except zmq.Again:
                break
            now = time.time()
            if len(frames) < 2:
                continue
            envelope, payload = frames[:-1], frames[-1]
            try:
                msg = orjson.loads(payload)
                if not isinstance(msg, dict):
                    raise ValueError("request is not a JSON object")
            except ValueError as e:
                self._log.warning("Malformed command dropped: %s", e)
                bad = Command(self, envelope, {}, now)
                bad.reject(f"malformed request: {e}")
                continue
            cmd = Command(self, envelope, msg, now)
            if not cmd.tag:
                cmd.reject("request has no tag")
                continue
            if self.max_age is not None and (cmd.ts <= 0 or cmd.age > self.max_age):
                cmd.reject(f"stale: sent {cmd.age:.2f} s ago (limit {self.max_age:.2f} s)")
                continue
            fresh.append(cmd)
        self._pending = fresh
        return fresh

    def finish(self):
        """Answer anything from the last poll() that the service did not answer."""
        for cmd in self._pending:
            if not cmd.replied:
                cmd.accept("received (no explicit result reported)")
        self._pending = []

    def _send_reply(self, cmd: Command):
        reply = {"id": cmd.id, "status": cmd.status, "reason": cmd.reason, "tag": cmd.tag,
                 "value": cmd.value, "service": self.service, "ts": time.time()}
        try:
            self.sock.send_multipart(cmd._envelope + [orjson.dumps(reply, default=str)], zmq.NOBLOCK)
        except zmq.ZMQError as e:  # client vanished / HWM - it will time out on its side
            self._log.debug("Could not send reply for %s: %s", cmd, e)
        self._record(cmd)

    def _record(self, cmd: Command):
        if not self.audit or not cmd.tag:
            return
        ok = cmd.status in SUCCESS
        text = f"{cmd.tag} = {cmd.value!r} ({cmd.origin}): {cmd.status}"
        if cmd.reason and not (ok and cmd.reason.startswith("received")):
            text += f" - {cmd.reason}"
        self._audit_log.log(logging.INFO if ok else logging.WARNING, text)
        if self.events is not None:
            try:
                self.events.log_command(
                    text, ok=ok,
                    metadata={"id": cmd.id, "tag": cmd.tag, "value": cmd.value, "origin": cmd.origin,
                              "status": cmd.status, "reason": cmd.reason,
                              "latency_ms": round((time.time() - cmd.ts) * 1000.0, 1) if cmd.ts else None})
            except Exception:  # auditing must never break command handling
                self._log.exception("Audit write failed")

    def close(self):
        self.finish()
        self.sock.close(0)


# --------------------------------------------------------------------------------------
# Client side
# --------------------------------------------------------------------------------------
_counter = itertools.count(1)
_prefix = f"{os.getpid():x}-{uuid.uuid4().hex[:6]}"


def new_command_id() -> str:
    return f"{_prefix}-{next(_counter)}"


class CommandClient:
    """Sends commands and collects replies. Use it from ONE thread only (ZeroMQ
    sockets are not thread-safe); the HMI wraps it in ZMQCommandThread.

        client = CommandClient()
        reply = client.request("plc", "ion_beam.source.chamber.cmd_open_gv", True)
        if reply["status"] not in SUCCESS: print(describe(reply))
    """

    def __init__(self, endpoints: dict = None, context=None, default_timeout: float = DEFAULT_TIMEOUT,
                 connect_wait: float = 0.3):
        if endpoints is None:
            from src.core.network_map import COMMAND_ENDPOINTS
            endpoints = COMMAND_ENDPOINTS
        self.endpoints = dict(endpoints)
        self.default_timeout = default_timeout
        self.connect_wait = connect_wait
        self._ctx = context or zmq.Context.instance()
        self._socks = {}
        self._poller = zmq.Poller()
        self._inflight = {}   # id -> request dict (+ deadline, subsystem, sent)
        self._ready = []      # replies (real or synthesised) not yet handed out

    def _socket(self, subsystem):
        sock = self._socks.get(subsystem)
        if sock is None:
            endpoint = self.endpoints[subsystem]
            sock = self._ctx.socket(zmq.DEALER)
            sock.setsockopt(zmq.LINGER, 0)
            sock.setsockopt(zmq.IMMEDIATE, 1)   # queue only to a live connection: fail fast if the service is down
            sock.setsockopt(zmq.SNDHWM, 1000)
            sock.setsockopt(zmq.RECONNECT_IVL, 100)
            sock.setsockopt(zmq.RECONNECT_IVL_MAX, 1000)
            sock.connect(endpoint)
            self._socks[subsystem] = sock
            self._poller.register(sock, zmq.POLLIN)
        return sock

    def send(self, subsystem: str, tag: str, value, origin: str = "hmi", timeout: float = None,
             cmd_id: str = None, ts: float = None, extra: dict = None) -> str:
        """Queue one command. Returns its id; the outcome arrives through poll()."""
        cmd_id = cmd_id or new_command_id()
        now = time.time()
        req = {"id": cmd_id, "tag": tag, "value": value, "ts": ts if ts is not None else now, "origin": origin}
        if extra:
            req.update(extra)
        meta = {"subsystem": subsystem, "tag": tag, "value": value, "origin": origin, "id": cmd_id,
                "sent": now, "deadline": now + (timeout or self.default_timeout)}
        if subsystem not in self.endpoints:
            self._ready.append(self._synth(meta, UNREACHABLE, f"no command endpoint for subsystem '{subsystem}'"))
            return cmd_id
        sock = self._socket(subsystem)
        try:
            payload = orjson.dumps(req, default=str)
        except TypeError as e:
            self._ready.append(self._synth(meta, REJECTED, f"value cannot be serialised: {e}"))
            return cmd_id
        if not self._send_now(sock, payload):
            self._ready.append(self._synth(meta, UNREACHABLE, f"{subsystem} service is not running or not connected"))
            return cmd_id
        self._inflight[cmd_id] = meta
        return cmd_id

    def _send_now(self, sock, payload) -> bool:
        try:
            sock.send(payload, zmq.NOBLOCK)
            return True
        except zmq.Again:
            pass
        # Freshly created sockets need a moment to finish connecting
        if self.connect_wait > 0 and sock.poll(int(self.connect_wait * 1000), zmq.POLLOUT):
            try:
                sock.send(payload, zmq.NOBLOCK)
                return True
            except zmq.Again:
                return False
        return False

    @staticmethod
    def _synth(meta, status, reason):
        return {"id": meta["id"], "status": status, "reason": reason, "tag": meta["tag"], "value": meta["value"],
                "subsystem": meta["subsystem"], "origin": meta["origin"],
                "latency_s": round(time.time() - meta["sent"], 3), "service": None}

    def poll(self, timeout_ms: int = 0):
        """Replies that arrived (plus timeouts / unreachables). Waits at most timeout_ms."""
        if self._socks and not self._ready:
            events = dict(self._poller.poll(timeout_ms))
        elif self._socks:
            events = dict(self._poller.poll(0))
        else:
            events = {}
            if timeout_ms and not self._ready:
                time.sleep(timeout_ms / 1000.0)
        for sock in events:
            while True:
                try:
                    payload = sock.recv(zmq.NOBLOCK)
                except zmq.Again:
                    break
                try:
                    reply = orjson.loads(payload)
                except ValueError:
                    log.warning("Unreadable reply dropped")
                    continue
                meta = self._inflight.pop(reply.get("id"), None)
                if meta is None:
                    continue   # reply to a request we already timed out
                reply.setdefault("value", meta["value"])
                reply["subsystem"] = meta["subsystem"]
                reply["origin"] = meta["origin"]
                reply["latency_s"] = round(time.time() - meta["sent"], 3)
                self._ready.append(reply)
        now = time.time()
        for cmd_id in [k for k, m in self._inflight.items() if now > m["deadline"]]:
            meta = self._inflight.pop(cmd_id)
            self._ready.append(self._synth(meta, TIMEOUT,
                                           f"no reply from {meta['subsystem']} within "
                                           f"{meta['deadline'] - meta['sent']:.1f} s"))
        out, self._ready = self._ready, []
        return out

    def request(self, subsystem: str, tag: str, value, origin: str = "script", timeout: float = None) -> dict:
        """Blocking convenience for scripts and tests: send and wait for this command's reply."""
        cmd_id = self.send(subsystem, tag, value, origin=origin, timeout=timeout)
        deadline = time.time() + (timeout or self.default_timeout) + 1.0
        held = []
        try:
            while time.time() < deadline:
                for reply in self.poll(50):
                    if reply.get("id") == cmd_id:
                        return reply
                    held.append(reply)
            return {"id": cmd_id, "status": TIMEOUT, "reason": "no reply", "tag": tag, "value": value,
                    "subsystem": subsystem, "origin": origin}
        finally:
            self._ready = held + self._ready

    @property
    def inflight(self) -> int:
        return len(self._inflight)

    def close(self):
        for sock in self._socks.values():
            try:
                self._poller.unregister(sock)
            except KeyError:
                pass
            sock.close(0)
        self._socks.clear()


class CommandTicket:
    """Thread-safe handle for one command sent through ZMQCommandThread.
    Callers that don't care can ignore it; automation waits on it."""

    def __init__(self, subsystem, tag, value, origin, timeout=None):
        self.id = new_command_id()
        self.subsystem = subsystem
        self.tag = tag
        self.value = value
        self.origin = origin
        self.timeout = timeout
        self.created = time.time()
        self.result = None
        self._event = threading.Event()

    def done(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float = None):
        self._event.wait(timeout)
        return self.result

    @property
    def status(self):
        return self.result.get("status") if self.result else None

    @property
    def ok(self) -> bool:
        return bool(self.result) and self.result.get("status") in SUCCESS

    @property
    def reason(self) -> str:
        return (self.result or {}).get("reason", "")

    def resolve(self, result: dict):
        self.result = result
        self._event.set()

    def __repr__(self):
        return f"CommandTicket({self.subsystem}:{self.tag}={self.value!r} -> {self.status})"


# --------------------------------------------------------------------------------------
# Which service owns which tag
# --------------------------------------------------------------------------------------
_REGISTRY_SOURCES = {
    "service_plc_snap7": "plc",
    "service_spellman": "spellman",
    "service_magnet": "magnet",
    "service_steerer_magnet": "steerer_magnet",
    "service_faraday_smu": "smu",
    "service_vacuum": "vacuum",
    "service_source_turbo": "src_turbo",
    "service_adam_monitor": "adam",
}
_registry_cache = None
_registry_lock = threading.Lock()


def _registry_sources():
    global _registry_cache
    with _registry_lock:
        if _registry_cache is None:
            from src.core import paths
            try:
                with open(paths.config_path("system_tags.json"), "r") as f:
                    reg = orjson.loads(f.read())
                _registry_cache = {tag: _REGISTRY_SOURCES.get(meta.get("source")) for tag, meta in reg.items()}
            except (OSError, ValueError) as e:
                log.warning("Tag registry unavailable for command routing (%s); using name rules only", e)
                _registry_cache = {}
        return _registry_cache


def route_for_tag(tag: str) -> str:
    """Name of the subsystem that accepts commands for `tag` (registry first, then naming rules)."""
    subsystem = _registry_sources().get(tag)
    if subsystem:
        return subsystem
    t = tag.lower()
    if "einzel" in t or "neutral_trap" in t or "spellman" in t:
        return "spellman"
    if "steerer_magnet" in t:
        return "steerer_magnet"
    if ".magnet." in t or t.endswith(".magnet"):
        return "magnet"
    if "faraday.smu" in t or ".smu." in t:
        return "smu"
    if "turbo_pump" in t and "source" in t:
        return "src_turbo"
    if ".vacuum." in t or "graphix" in t:
        return "vacuum"
    return "plc"
