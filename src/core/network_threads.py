import logging
import queue
import time

import orjson
import zmq
from PyQt6.QtCore import QThread, pyqtSignal

from src.core.commands import (CommandClient, CommandTicket, DEFAULT_TIMEOUT, SUCCESS, UNREACHABLE, describe,
                               route_for_tag)
from src.core.log_setup import Throttle

log = logging.getLogger("hmi.network")

# --- Background Network Workers ---


class ZMQTelemetryThread(QThread):
    """Receives every service's telemetry. Emits (topic, payload): the topic tells the
    HMI's TelemetryCache which service a value came from, so it can tell when it goes stale."""
    data_received = pyqtSignal(str, dict)

    def __init__(self, ports: list[str]):
        super().__init__()
        self.ports = ports
        self.running = True
        self._throttle = Throttle(30.0)

    def run(self):
        ctx = zmq.Context.instance()
        sub_socket = ctx.socket(zmq.SUB)
        for port in self.ports:
            sub_socket.connect(port)
        sub_socket.setsockopt(zmq.SUBSCRIBE, b"")

        poller = zmq.Poller()
        poller.register(sub_socket, zmq.POLLIN)

        try:
            while self.running:
                socks = dict(poller.poll(timeout=100))
                if sub_socket in socks and socks[sub_socket] == zmq.POLLIN:
                    while True:
                        try:
                            topic, payload = sub_socket.recv_multipart(flags=zmq.NOBLOCK)
                        except zmq.Again:
                            break
                        except ValueError:
                            continue   # unexpected number of frames
                        try:
                            data = orjson.loads(payload)
                        except ValueError as e:
                            if (n := self._throttle.ready(topic)) is not None:
                                log.warning("Undecodable telemetry on %r: %s%s", topic, e, Throttle.suffix(n))
                            continue
                        if isinstance(data, dict):
                            self.data_received.emit(topic.decode("utf-8", "replace"), data)
        finally:
            sub_socket.setsockopt(zmq.LINGER, 0)
            sub_socket.close()

    def stop(self):
        self.running = False
        self.wait()


class ZMQCommandThread(QThread):
    """Sends commands to the services and collects their acknowledgements.

    send_command() keeps its old signature, so every widget works unchanged, and
    now returns a CommandTicket: automation code can wait on it, and every
    outcome is also emitted as command_result(dict) for the HMI to display
    (rejections, failures, timeouts and services that are not running).
    """
    command_result = pyqtSignal(dict)

    def __init__(self, endpoints: dict = None, default_timeout: float = DEFAULT_TIMEOUT):
        super().__init__()
        self.cmd_queue = queue.Queue()
        self.running = True
        self.endpoints = endpoints
        self.default_timeout = default_timeout
        self._tickets = {}

    def send_command(self, subsystem: str, tag: str, value: any, origin: str = "hmi",
                     timeout: float = None, holder: str = None) -> CommandTicket:
        if not subsystem:
            subsystem = route_for_tag(tag)
        ticket = CommandTicket(subsystem, tag, value, origin, timeout, holder=holder)
        self.cmd_queue.put(ticket)
        return ticket

    def send_command_sync(self, subsystem: str, tag: str, value, origin: str = "hmi",
                          timeout: float = None, holder: str = None) -> dict:
        """Blocking - for worker threads only, never the GUI thread."""
        ticket = self.send_command(subsystem, tag, value, origin=origin, timeout=timeout, holder=holder)
        result = ticket.wait((timeout or self.default_timeout) + 2.0)
        return result or {"status": "timeout", "reason": "command thread did not respond", "tag": tag,
                          "value": value, "subsystem": subsystem, "origin": origin}

    def _resolve(self, result: dict):
        ticket = self._tickets.pop(result.get("id"), None)
        if ticket is not None:
            ticket.resolve(result)
        if result.get("status") not in SUCCESS:
            log.warning("Command %s via %s: %s", result.get("origin"), result.get("subsystem"), describe(result))
        self.command_result.emit(result)

    def run(self):
        client = CommandClient(self.endpoints, default_timeout=self.default_timeout, connect_wait=0.2)
        try:
            while self.running:
                # 1. Send everything queued since the last pass
                try:
                    while True:
                        ticket = self.cmd_queue.get_nowait()
                        self._tickets[ticket.id] = ticket
                        try:
                            client.send(ticket.subsystem, ticket.tag, ticket.value, origin=ticket.origin,
                                        timeout=ticket.timeout, cmd_id=ticket.id, ts=ticket.created,
                                        holder=ticket.holder)
                        except Exception as e:  # never let one bad command kill the command thread
                            log.exception("Could not send %r", ticket)
                            self._resolve({"id": ticket.id, "status": UNREACHABLE, "reason": f"send failed: {e}",
                                           "tag": ticket.tag, "value": ticket.value,
                                           "subsystem": ticket.subsystem, "origin": ticket.origin})
                except queue.Empty:
                    pass
                # 2. Collect replies, timeouts and unreachables
                for result in client.poll(20):
                    self._resolve(result)
        finally:
            client.close()
            for ticket in list(self._tickets.values()):
                ticket.resolve({"id": ticket.id, "status": "timeout", "reason": "HMI closing", "tag": ticket.tag,
                                "value": ticket.value, "subsystem": ticket.subsystem, "origin": ticket.origin})

    def stop(self):
        self.running = False
        self.wait()
