"""Sequencer service: runs recipes, mass scans and the optimiser (src/sequencer/).

Automation used to run inside the HMI, so closing or restarting the HMI stopped a recipe or a
scan half way. Here it runs in its own process under the service manager:

  - listens to every device service's telemetry (TelemetryCache: automation only acts on
    live values)
  - takes commands on the "sequencer" endpoint (src/sequencer/protocol.py): start / stop /
    pause / resume / acknowledge a prompt / fetch scan points
  - sends its jobs' commands to the device services through one CommandClient, each job
    under its own control lease (src/core/leases.py, src/sequencer/port.py)
  - publishes the state of every slot (recipe, mass scan, optimiser) on SEQUENCER_DATA.

One job per slot at a time; jobs in different slots may run together - their leases stop
them from driving the same subsystem.
"""
import itertools
import json
import logging
import time
import uuid

import orjson
import zmq

from src.core import paths
from src.core.commands import CommandClient, CommandServer, bind_retrying
from src.core.event_helper import EventHelper
from src.core.log_setup import Throttle, setup_logging
from src.core.network_map import (
    COMMAND_ENDPOINTS, ZMQ_PORT_ADAM_PUB, ZMQ_PORT_HEARTBEAT, ZMQ_PORT_MAGNET_PUB, ZMQ_PORT_PLC_PUB,
    ZMQ_PORT_SEQUENCER_CMD, ZMQ_PORT_SEQUENCER_PUB, ZMQ_PORT_SMU_PUB, ZMQ_PORT_SPELLMAN_PUB,
    ZMQ_PORT_SRC_TURBO_PUB, ZMQ_PORT_STEERER_MAGNET_PUB, ZMQ_PORT_VACUUM_PUB, TOPIC_SEQUENCER_DATA)
from src.core.os_helper import harden_windows_process
from src.core.recipe_catalog import Catalog
from src.core.recipe_files import RECIPES_DIR
from src.core.telemetry_cache import TelemetryCache
from src.sequencer import protocol as P
from src.sequencer.jobs import JobContext
from src.sequencer.mass_scan import SP_TAG, MassScanJob, ScanConfig
from src.sequencer.optimiser import OptimiserConfig, OptimiserJob, starting_value
from src.sequencer.port import Dispatcher, SubsystemMap
from src.sequencer.recipe_job import RecipeJob, resolve_recipe

DEVICE_TELEMETRY = (ZMQ_PORT_PLC_PUB, ZMQ_PORT_VACUUM_PUB, ZMQ_PORT_SRC_TURBO_PUB, ZMQ_PORT_SPELLMAN_PUB,
                    ZMQ_PORT_MAGNET_PUB, ZMQ_PORT_STEERER_MAGNET_PUB, ZMQ_PORT_SMU_PUB, ZMQ_PORT_ADAM_PUB)
TICK_S = 0.02
PUBLISH_ACTIVE_S = 0.1
PUBLISH_IDLE_S = 0.5
HEARTBEAT_S = 0.5
STARTUP_GRACE_S = 2.0           # collect telemetry before accepting a start (lease subsystems)
SHUTDOWN_GRACE_S = 5.0          # on shutdown, let scans put the SMU back before letting go
MAX_CMD_AGE = 1.0

log = logging.getLogger(P.SERVICE)


def load_registry(path: str = None) -> dict:
    try:
        with open(path or paths.REGISTRY_PATH, "r") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        log.error("Tag registry not loaded (%s): scans and the optimiser will be refused", e)
        return {}


class SequencerService:
    def __init__(self, pub_endpoint=ZMQ_PORT_SEQUENCER_PUB, cmd_endpoint=ZMQ_PORT_SEQUENCER_CMD,
                 telemetry_endpoints=DEVICE_TELEMETRY, command_endpoints=None, events=None,
                 recipes_dir=RECIPES_DIR, registry=None, clock=time.monotonic):
        self.clock = clock
        self.context = zmq.Context.instance()
        self.events = events if events is not None else EventHelper(P.SERVICE)
        self.recipes_dir = recipes_dir
        self.registry = load_registry() if registry is None else registry
        self.throttle = Throttle(60.0)
        self.running = True
        self._closed = False

        self.sub = self.context.socket(zmq.SUB)
        self.sub.setsockopt(zmq.LINGER, 0)
        self.sub.setsockopt(zmq.SUBSCRIBE, b"")
        for endpoint in telemetry_endpoints:
            self.sub.connect(endpoint)
        self.pub = self.context.socket(zmq.PUB)
        self.pub.setsockopt(zmq.LINGER, 0)
        self.pub.setsockopt(zmq.SNDHWM, 20)
        bind_retrying(self.pub, pub_endpoint)
        self.hb = self.context.socket(zmq.PUB)
        self.hb.setsockopt(zmq.LINGER, 0)
        self.hb.connect(ZMQ_PORT_HEARTBEAT)
        self.commands = CommandServer(cmd_endpoint, P.SERVICE, context=self.context, events=self.events,
                                      max_age=MAX_CMD_AGE)

        endpoints = dict(COMMAND_ENDPOINTS if command_endpoints is None else command_endpoints)
        endpoints.pop(P.ROUTE, None)
        client = CommandClient(endpoints, context=self.context, connect_wait=0.0)
        client.connect_all()
        self.dispatcher = Dispatcher(client)
        self.telemetry = TelemetryCache()
        self.subsystems = SubsystemMap(self.telemetry.keys, clock=clock)
        self.ctx = JobContext(self.dispatcher, self.telemetry, self.subsystems, events=self.events, clock=clock,
                              instance=uuid.uuid4().hex[:4], registry=self.registry,
                              catalog=Catalog.for_registry(self.registry))
        self.jobs = {slot: None for slot in P.SLOTS}
        self._runs = itertools.count(int(time.time()))   # run numbers stay unique across restarts
        self.started_at = clock()
        self._last_pub = -1e9
        self._last_hb = -1e9

    # ------------------------------------------------------------------ loop
    def run(self):
        self.events.log_general("Sequencer online (nothing running)")
        try:
            while self.running:
                self.run_cycle()
        except KeyboardInterrupt:
            log.info("Interrupted - shutting down")
        finally:
            self.shutdown()

    def stop(self):
        self.running = False

    def run_cycle(self, wait_s: float = TICK_S):
        self._receive_telemetry(wait_s)
        self.dispatcher.pump()
        self._process_commands()
        for job in self.jobs.values():
            if job is not None:
                try:
                    job.service_tick()
                except Exception as e:   # a bug in one job ends that job, never the sequencer
                    log.exception("%s: tick failed", job.purpose)
                    self._fail_job(job, f"internal error in the sequencer: {type(e).__name__}: {e}")
        self._publish()
        self._heartbeat()

    @staticmethod
    def _fail_job(job, message: str):
        """End a job that raised, and let go of its leases."""
        try:
            if job.state not in P.FINAL:
                job.finish(P.FAILED, message)
        except Exception:   # recording it failed too: end it all the same
            log.exception("%s: could not record the failure", job.purpose)
            job.state, job.status, job.message = P.FAILED, message, message
        try:
            job.release()
        except Exception:
            log.exception("%s: releasing its leases failed", job.purpose)

    def _receive_telemetry(self, wait_s):
        if not self.sub.poll(int(wait_s * 1000)):
            return
        while True:
            try:
                topic, payload = self.sub.recv_multipart(zmq.NOBLOCK)
            except zmq.Again:
                return
            except ValueError:          # unexpected number of frames
                continue
            try:
                data = orjson.loads(payload)
            except ValueError as e:
                if (n := self.throttle.ready(f"telemetry:{topic!r}")) is not None:
                    log.warning("Undecodable telemetry on %r: %s%s", topic, e, Throttle.suffix(n))
                continue
            if isinstance(data, dict):
                self.telemetry.ingest(topic, data)

    # ------------------------------------------------------------------ commands
    def _process_commands(self):
        for cmd in self.commands.poll():
            try:
                self._handle(cmd)
            except Exception as e:   # one bad command must neither go unanswered nor stop the sequencer
                log.exception("Handling %s failed", cmd)
                cmd.fail(f"internal error in the sequencer: {type(e).__name__}: {e}")
        self.commands.finish()

    def _handle(self, cmd):
        slot, verb = P.parse_command(cmd.tag)
        if slot not in P.SLOTS:
            cmd.reject("not a sequencer command")
            return
        if verb == "start":
            self._start(slot, cmd)
            return
        job = self.jobs[slot]
        if job is None:
            cmd.reject(f"no {slot.replace('_', ' ')} has run since the sequencer started")
            return
        ok, reason, data = job.handle(verb, cmd.value)
        if ok:
            cmd.ok(reason, data=data)
        else:
            cmd.reject(reason)

    def _start(self, slot, cmd):
        if self.clock() - self.started_at < STARTUP_GRACE_S:
            cmd.reject("the sequencer is starting up - try again in a moment")
            return
        current = self.jobs[slot]
        if current is not None and current.active:
            cmd.reject(f"{current.purpose} is already running - stop it first")
            return
        try:
            job = self._make_job(slot, cmd.value, next(self._runs))
        except (ValueError, OSError, TypeError) as e:      # RecipeError is a ValueError
            cmd.reject(str(e))
            return
        self.jobs[slot] = job
        log.info("Started %s (run %d, %s)", job.purpose, job.run, cmd.origin)
        cmd.ok(f"started {job.purpose}", data={"run": job.run})

    def _make_job(self, slot, value, run):
        if slot == P.RECIPE:
            name = value.get("file") if isinstance(value, dict) else value
            return RecipeJob(self.ctx, run, resolve_recipe(name, self.recipes_dir), self.recipes_dir)
        if slot == P.MASS_SCAN:
            meta = self.registry.get(SP_TAG, {})
            return MassScanJob(self.ctx, run, ScanConfig.from_value(value, (meta.get("min_val"), meta.get("max_val"))))
        cfg = OptimiserConfig.from_value(value, self.registry)
        restore = []
        for tag in (cfg.x_tag, cfg.y_tag):
            v = starting_value(tag, self.telemetry.get_fresh, self.registry)
            if v is None:
                raise ValueError(f"no live value for {tag} (the optimiser puts it back afterwards) - "
                                 f"is its service running?")
            restore.append(float(v))
        return OptimiserJob(self.ctx, run, cfg, tuple(restore))

    # ------------------------------------------------------------------ telemetry
    def snapshot(self) -> dict:
        state = {"timestamp": time.time(), "system.connected": 1.0, f"{P.PREFIX}.instance": self.ctx.instance}
        for slot, job in self.jobs.items():
            if job is None:
                state[P.key(slot, "state")] = P.IDLE
                state[P.key(slot, "run")] = None
                state[P.key(slot, "status")] = ""
                state[P.key(slot, "holder")] = None
            else:
                state.update(job.telemetry())
        return state

    def _publish(self, force: bool = False):
        now = self.clock()
        busy = any(job is not None and job.active for job in self.jobs.values())
        if not force and now - self._last_pub < (PUBLISH_ACTIVE_S if busy else PUBLISH_IDLE_S):
            return
        self._last_pub = now
        try:
            self.pub.send_multipart([TOPIC_SEQUENCER_DATA, orjson.dumps(self.snapshot())], zmq.NOBLOCK)
        except (zmq.ZMQError, TypeError) as e:
            if (n := self.throttle.ready("publish")) is not None:
                log.error("Publish failed: %s%s", e, Throttle.suffix(n))

    def _heartbeat(self):
        now = self.clock()
        if now - self._last_hb >= HEARTBEAT_S:
            self._last_hb = now
            try:
                self.hb.send_json({"service": P.SERVICE, "ts": time.time()}, zmq.NOBLOCK)
            except zmq.ZMQError:
                pass    # nobody listening (manager restarting): the next beat will do

    # ------------------------------------------------------------------ shutdown
    def shutdown(self):
        """Stop what is running (scans put the SMU back), release every lease, close."""
        if self._closed:
            return
        self._closed = True
        for job in self.jobs.values():
            if job is not None and job.active:
                job.abort("Stopped: the sequencer is shutting down")
        deadline = self.clock() + SHUTDOWN_GRACE_S
        while self.clock() < deadline and any(j is not None and j.active for j in self.jobs.values()):
            self.run_cycle(wait_s=0.02)
        for job in self.jobs.values():
            if job is not None:
                job.release()
        deadline = self.clock() + 1.0   # let the releases reach the services before closing
        while self.dispatcher.inflight and self.clock() < deadline:
            self.dispatcher.pump(20)
        self._publish(force=True)
        self.commands.close()
        self.dispatcher.close()
        for sock in (self.sub, self.pub, self.hb):
            sock.close(0)


if __name__ == "__main__":
    setup_logging(P.SERVICE)
    harden_windows_process()
    SequencerService().run()
