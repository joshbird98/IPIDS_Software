"""Helpers for running one real service in a thread against simulated hardware."""
import threading
import time

import orjson
import zmq

from src.core.network_map import ZMQ_PORT_PLC_PUB, TOPIC_PLC_DATA

RELAY_TAG = "ion_beam.facilities.safety_relay_active"


class FakePlcPublisher:
    """Publishes just the safety-relay state, as the PLC service would.
    relay=None publishes null (PLC disconnected); paused=True stops publishing altogether."""

    def __init__(self, relay=True):
        self.relay = relay
        self.paused = False
        self.running = True
        self.sock = zmq.Context.instance().socket(zmq.PUB)
        self.sock.bind(ZMQ_PORT_PLC_PUB)
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        while self.running:
            if not self.paused:
                relay = None if self.relay is None else (1.0 if self.relay else 0.0)
                self.sock.send_multipart([TOPIC_PLC_DATA, orjson.dumps({RELAY_TAG: relay, "timestamp": time.time()})])
            time.sleep(0.1)
        self.sock.close(0)

    def stop(self):
        self.running = False
        self.t.join(2)


class TelemetryTap:
    """Subscribes to a service's telemetry and waits for a condition on it."""

    def __init__(self, endpoint, prefix=""):
        self.sock = zmq.Context.instance().socket(zmq.SUB)
        self.sock.setsockopt(zmq.SUBSCRIBE, b"")
        self.sock.connect(endpoint)
        self.prefix = prefix
        self.last = {}

    def wait_for(self, predicate, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.sock.poll(100):
                self.last = orjson.loads(self.sock.recv_multipart()[1])
                try:
                    if predicate(self.last):
                        return self.last
                except (TypeError, KeyError):
                    pass
        raise AssertionError("condition not met; last telemetry: "
                             f"{ {k: v for k, v in self.last.items() if k.startswith(self.prefix)} }")

    def holds_for(self, predicate, seconds):
        """True if the condition stays true on every message for `seconds`."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self.sock.poll(100):
                self.last = orjson.loads(self.sock.recv_multipart()[1])
                if not predicate(self.last):
                    return False
        return True

    def close(self):
        self.sock.close(0)


class ServiceThread:
    """Runs service.run() in a daemon thread; stop() asks it to finish and waits."""

    def __init__(self, service):
        self.service = service
        self.t = threading.Thread(target=service.run, daemon=True)
        self.t.start()

    def stop(self):
        self.service.stop()
        self.t.join(5)
