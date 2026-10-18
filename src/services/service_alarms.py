"""Alarm service: one list of alarms for the whole machine (src/alarms/).

Listens to every service's telemetry (and the manager's), evaluates the alarm rules twice a
second, keeps the alarm states (ISA-18.2: unacknowledged / acknowledged / cleared-unacknowledged,
shelving, first-out) and publishes them on ALARMS_DATA:

    alarms.list      the alarms to show, most urgent first (see Alarm.view)
    alarms.counts    {"unack", "active", "shelved", "suppressed"}
    alarms.horn      priority (1 critical .. 3 medium) the HMI should sound, or null

Commands ("alarms" endpoint):
    alarms.cmd_ack              {"ids": [...]} or {"all": true}
    alarms.cmd_shelve           {"id": "...", "minutes": 30, "reason": "..."}
    alarms.cmd_unshelve         {"id": "..."}
    alarms.cmd_reset_first_out

Every transition is written to events.db (event type ALARM). Acknowledgements, shelves and
first-out survive a restart (data/alarm_state.json).
"""
import json
import logging
import os
import time

import orjson
import zmq

from src.alarms.model import PRIORITY_NAMES, AlarmTable
from src.alarms.rules import AlarmRules, load_config, load_fault_config, suppressed
from src.core import paths
from src.core.commands import CommandServer, bind_retrying
from src.core.event_helper import EventHelper
from src.core.log_setup import Throttle, setup_logging
from src.core.network_map import (
    TOPIC_ALARMS_DATA, ZMQ_PORT_ADAM_PUB, ZMQ_PORT_ALARMS_CMD, ZMQ_PORT_ALARMS_PUB, ZMQ_PORT_HEARTBEAT,
    ZMQ_PORT_MAGNET_PUB, ZMQ_PORT_MANAGER_PUB, ZMQ_PORT_MONITOR_PUB, ZMQ_PORT_PLC_PUB, ZMQ_PORT_SEQUENCER_PUB,
    ZMQ_PORT_SMU_PUB, ZMQ_PORT_SPELLMAN_PUB, ZMQ_PORT_SRC_TURBO_PUB, ZMQ_PORT_STEERER_MAGNET_PUB,
    ZMQ_PORT_VACUUM_PUB)
from src.core.os_helper import harden_windows_process
from src.core.telemetry_cache import TelemetryCache

SERVICE = "service_alarms"
TELEMETRY = (ZMQ_PORT_PLC_PUB, ZMQ_PORT_VACUUM_PUB, ZMQ_PORT_SRC_TURBO_PUB, ZMQ_PORT_SPELLMAN_PUB,
             ZMQ_PORT_MAGNET_PUB, ZMQ_PORT_STEERER_MAGNET_PUB, ZMQ_PORT_SMU_PUB, ZMQ_PORT_ADAM_PUB,
             ZMQ_PORT_MANAGER_PUB, ZMQ_PORT_MONITOR_PUB, ZMQ_PORT_SEQUENCER_PUB)
STATE_FILE = os.path.join(paths.DATA_DIR, "alarm_state.json")
EVALUATE_S = 0.5
PUBLISH_S = 0.5
HEARTBEAT_S = 0.5
SAVE_S = 2.0

log = logging.getLogger(SERVICE)


class AlarmService:
    def __init__(self, pub_endpoint=ZMQ_PORT_ALARMS_PUB, cmd_endpoint=ZMQ_PORT_ALARMS_CMD,
                 telemetry_endpoints=TELEMETRY, events=None, config=None, fault_config=None,
                 state_file=STATE_FILE, clock=time.time):
        self.clock = clock
        self.events = events if events is not None else EventHelper(SERVICE)
        self.config = config if config is not None else load_config()
        self.rules = AlarmRules(self.config, fault_config if fault_config is not None else load_fault_config(),
                                started=clock())
        self.table = AlarmTable(self.config.get("first_out_groups", ()), clock=clock, on_event=self._log_event)
        self.state_file = state_file
        self._restore()
        self.cache = TelemetryCache(clock=clock)
        self.throttle = Throttle(60.0)
        self.running = True
        self._closed = False
        self._defs = {}
        self._last = {"evaluate": -1e18, "publish": -1e18, "heartbeat": -1e18, "save": -1e18}
        self._publish_due = self._save_due = False

        ctx = zmq.Context.instance()
        self.sub = ctx.socket(zmq.SUB)
        self.sub.setsockopt(zmq.LINGER, 0)
        self.sub.setsockopt(zmq.SUBSCRIBE, b"")
        for endpoint in telemetry_endpoints:
            self.sub.connect(endpoint)
        self.pub = ctx.socket(zmq.PUB)
        self.pub.setsockopt(zmq.LINGER, 0)
        bind_retrying(self.pub, pub_endpoint)
        self.hb = ctx.socket(zmq.PUB)
        self.hb.setsockopt(zmq.LINGER, 0)
        self.hb.connect(ZMQ_PORT_HEARTBEAT)
        self.commands = CommandServer(cmd_endpoint, SERVICE, context=ctx, events=self.events, max_age=2.0)

    # ------------------------------------------------------------------ persistence / history
    def _restore(self):
        try:
            with open(self.state_file, "r") as f:
                self.table.restore(json.load(f))
            log.info("Alarm acknowledgements and shelves restored from %s", self.state_file)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.warning("Alarm state not restored (%s) - starting fresh", e)

    def save(self):
        tmp = self.state_file + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            with open(tmp, "w") as f:
                json.dump(self.table.dump(), f)
            os.replace(tmp, self.state_file)
        except OSError as e:
            if (n := self.throttle.ready("save")) is not None:
                log.error("Alarm state not saved: %s%s", e, Throttle.suffix(n))

    def _log_event(self, kind, alarm, detail):
        severity = {"ACTIVE": "WARNING" if alarm.priority > 1 else "ERROR"}.get(kind, "INFO")
        text = f"ALARM {kind}: {alarm.message} [{PRIORITY_NAMES.get(alarm.priority, alarm.priority)}]"
        self.events.log_general(text + (f" ({detail})" if detail else ""), severity=severity, event_type="ALARM",
                                metadata={"alarm": alarm.id, "transition": kind, "priority": alarm.priority,
                                          "state": alarm.state, "detail": detail})

    # ------------------------------------------------------------------ loop
    def run(self):
        self.events.log_general("Alarm service online")
        try:
            while self.running:
                self.run_cycle()
        except KeyboardInterrupt:
            log.info("Interrupted - shutting down")
        finally:
            self.shutdown()

    def stop(self):
        self.running = False

    def run_cycle(self, wait_s: float = 0.05):
        self._receive(wait_s)
        self._process_commands()
        now = self.clock()
        if now - self._last["evaluate"] >= EVALUATE_S:
            self._last["evaluate"] = now
            self.evaluate(now)
        if self.table.changed:            # publish at once, save at most every SAVE_S
            self.table.changed = False
            self._publish_due = self._save_due = True
        if self._publish_due or now - self._last["publish"] >= PUBLISH_S:
            self._last["publish"] = now
            self._publish_due = False
            self.publish()
        if self._save_due and now - self._last["save"] >= SAVE_S:
            self._last["save"] = now
            self._save_due = False
            self.save()
        if now - self._last["heartbeat"] >= HEARTBEAT_S:
            self._last["heartbeat"] = now
            try:
                self.hb.send_json({"service": SERVICE, "ts": time.time()}, zmq.NOBLOCK)
            except zmq.ZMQError:
                pass

    def _receive(self, wait_s):
        if not self.sub.poll(int(wait_s * 1000)):
            return
        while True:
            try:
                topic, payload = self.sub.recv_multipart(zmq.NOBLOCK)
            except zmq.Again:
                return
            except ValueError:
                continue
            try:
                data = orjson.loads(payload)
            except ValueError:
                continue
            if isinstance(data, dict):
                self.cache.ingest(topic, data)

    def evaluate(self, now=None):
        now = self.clock() if now is None else now
        defs = self.rules.evaluate(self.cache, now)
        for d in defs:
            self.table.define(d.id, d.message, d.priority, d.group, d.delay_on, d.delay_off)
            self.table.update(d.id, d.condition, now)
        active = {a.id for a in self.table.alarms.values() if a.active}
        for d in defs:
            if d.suppressed_by:
                self.table.set_suppressed(d.id, suppressed(d, active))
        self.table.expire_shelves(now)
        self._defs = {d.id: d for d in defs}

    def snapshot(self) -> dict:
        return {"timestamp": time.time(), "system.connected": 1.0,
                "alarms.list": [a.view() for a in self.table.listed()],
                "alarms.counts": self.table.counts(), "alarms.horn": self.table.horn()}

    def publish(self):
        try:
            self.pub.send_multipart([TOPIC_ALARMS_DATA, orjson.dumps(self.snapshot())], zmq.NOBLOCK)
        except (zmq.ZMQError, TypeError) as e:
            if (n := self.throttle.ready("publish")) is not None:
                log.error("Publish failed: %s%s", e, Throttle.suffix(n))

    # ------------------------------------------------------------------ commands
    def _process_commands(self):
        for cmd in self.commands.poll():
            try:
                self._handle(cmd)
            except Exception as e:   # one bad command must neither go unanswered nor stop the alarm service
                log.exception("Handling %s failed", cmd)
                cmd.fail(f"internal error in the alarm service: {type(e).__name__}: {e}")
        self.commands.finish()

    def _handle(self, cmd):
        value = cmd.value if isinstance(cmd.value, dict) else {}
        who = cmd.origin or "operator"
        if cmd.tag == "alarms.cmd_ack":
            ids = None if value.get("all") else list(value.get("ids") or [])
            if ids is not None and not ids:
                cmd.reject("nothing to acknowledge: give ids, or all")
                return
            done = self.table.ack(ids, by=who)
            cmd.ok(f"{len(done)} acknowledged", data=done)
        elif cmd.tag == "alarms.cmd_shelve":
            alarm_id, reason = value.get("id"), str(value.get("reason") or "").strip()
            limit = float(self.config.get("shelve_max_minutes", 480))
            try:
                minutes = float(value.get("minutes", 0))
            except (TypeError, ValueError):
                minutes = 0.0
            if alarm_id not in self.table.alarms:
                cmd.reject(f"no alarm {alarm_id!r}")
            elif not reason:
                cmd.reject("shelving needs a reason")
            elif not 0 < minutes <= limit:
                cmd.reject(f"shelve for 1 to {limit:g} minutes")
            else:
                self.table.shelve(alarm_id, minutes * 60.0, reason, by=who)
                cmd.ok(f"shelved for {minutes:g} min")
        elif cmd.tag == "alarms.cmd_unshelve":
            alarm_id = value.get("id")
            if alarm_id not in self.table.alarms:
                cmd.reject(f"no alarm {alarm_id!r}")
            else:
                cmd.ok("unshelved" if self.table.unshelve(alarm_id, by=who) else "was not shelved")
        elif cmd.tag == "alarms.cmd_reset_first_out":
            self.table.reset_first_out()
            cmd.ok("first-out reset")
        else:
            cmd.reject("not an alarm command")

    # ------------------------------------------------------------------ shutdown
    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        self.save()
        self.commands.close()
        for sock in (self.sub, self.pub, self.hb):
            sock.close(0)


if __name__ == "__main__":
    setup_logging(SERVICE)
    harden_windows_process()
    AlarmService().run()
