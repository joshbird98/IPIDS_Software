"""Readback tolerance monitor.

Listens to every service's telemetry, evaluates config/readback_tolerance.json
(see src/core/tolerance.py) and publishes one flag per rule on MONITOR_DATA:

    monitor.tolerance.<readback tag>  = 1.0 while out of tolerance, else 0.0
    monitor.rules_enabled             = number of active rules

Out-of-tolerance periods are written to events.db as TOLERANCE events. This is
information for the operator, not an interlock.
"""
import logging
import time

import orjson
import zmq

from src.core import paths
from src.core.event_helper import EventHelper
from src.core.log_setup import setup_logging
from src.core.network_map import (
    ZMQ_PORT_HEARTBEAT, ZMQ_PORT_MONITOR_PUB, TOPIC_MONITOR_DATA,
    ZMQ_PORT_PLC_PUB, ZMQ_PORT_VACUUM_PUB, ZMQ_PORT_SRC_TURBO_PUB, ZMQ_PORT_SPELLMAN_PUB, ZMQ_PORT_MAGNET_PUB,
    ZMQ_PORT_STEERER_MAGNET_PUB, ZMQ_PORT_SMU_PUB, ZMQ_PORT_ADAM_PUB)
from src.core.os_helper import harden_windows_process
from src.core.telemetry_cache import TelemetryCache
from src.core.tolerance import RuleFileWatcher, ToleranceMonitor

RULES_PATH = paths.config_path("readback_tolerance.json")
PUBLISH_INTERVAL = 0.5

log = logging.getLogger("service_monitor")


class MonitorService:
    def __init__(self, rules_path: str = RULES_PATH):
        self.context = zmq.Context()
        self.events = EventHelper("service_monitor")

        self.sub = self.context.socket(zmq.SUB)
        self.sub.setsockopt(zmq.SUBSCRIBE, b"")
        for ep in (ZMQ_PORT_PLC_PUB, ZMQ_PORT_VACUUM_PUB, ZMQ_PORT_SRC_TURBO_PUB, ZMQ_PORT_SPELLMAN_PUB,
                   ZMQ_PORT_MAGNET_PUB, ZMQ_PORT_STEERER_MAGNET_PUB, ZMQ_PORT_SMU_PUB, ZMQ_PORT_ADAM_PUB):
            self.sub.connect(ep)

        self.pub = self.context.socket(zmq.PUB)
        self.pub.setsockopt(zmq.SNDHWM, 5)
        self.pub.bind(ZMQ_PORT_MONITOR_PUB)

        self.hb = self.context.socket(zmq.PUB)
        self.hb.connect(ZMQ_PORT_HEARTBEAT)

        self.cache = TelemetryCache()
        self.monitor = ToleranceMonitor([], events=self.events)
        self.watcher = RuleFileWatcher(rules_path, self.monitor, events=self.events)
        self.running = True

    def stop(self):
        self.running = False

    def run(self):
        log.info("Readback tolerance monitor online (rules: %s)", self.watcher.path)
        last_pub = 0.0
        last_hb = 0.0
        while self.running:
            if self.sub.poll(100):
                while True:
                    try:
                        topic, payload = self.sub.recv_multipart(zmq.NOBLOCK)
                    except zmq.Again:
                        break
                    except ValueError:
                        continue
                    try:
                        data = orjson.loads(payload)
                    except ValueError:
                        continue
                    if isinstance(data, dict):
                        self.cache.ingest(topic, data)

            now = time.time()
            self.watcher.poll(now)
            if now - last_pub >= PUBLISH_INTERVAL:
                flags = self.monitor.evaluate(lambda tag: self.cache.get_fresh(tag, now=now), now)
                flags["monitor.rules_enabled"] = float(sum(r.enabled for r in self.monitor.rules))
                flags["timestamp"] = now
                self.pub.send_multipart([TOPIC_MONITOR_DATA, orjson.dumps(flags)])
                last_pub = now
            if now - last_hb >= 0.5:
                self.hb.send_json({"service": "service_monitor", "ts": now})
                last_hb = now

        for sock in (self.sub, self.pub, self.hb):
            sock.close(0)


if __name__ == "__main__":
    setup_logging("service_monitor")
    harden_windows_process()
    MonitorService().run()
