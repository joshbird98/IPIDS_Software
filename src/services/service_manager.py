import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import time

if __name__ == "__main__":
    if __package__ in (None, ""):
        # Started as a script (python src/services/service_manager.py): make 'src' importable
        sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
    from src.core.env import flag, use_simulator     # (reads nothing at import)
    if "--sim" in sys.argv[1:] or flag("SIMULATE"):
        # Simulated devices, data_sim/ and the simulator's ports: set before src.core.paths and
        # the network map are imported (they decide the data folder and the ports at import).
        # SINDIE_SIMULATE=1 alone gets the ports too (an offset already set is kept).
        use_simulator()

import psutil
import zmq

from src.core import paths
from src.core.env import flag, getenv
from src.core.commands import CommandServer
from src.core.event_helper import EventHelper
from src.core.log_setup import setup_logging
from src.core.network_map import (ZMQ_PORT_HEARTBEAT, ZMQ_PORT_MANAGER_PUB, ZMQ_PORT_MANAGER_CMD,
                                  MANAGER_LOCK_PORT, PORT_OFFSET, TOPIC_MANAGER)
from src.core.os_helper import harden_windows_process

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))

SERVICES_CONFIG = {
    "service_events": {"script": os.path.join(CURRENT_DIR, "service_events.py"), "tier": 0, "timeout": 2.0, "boot_grace": 15.0},
    "service_logger": {"script": os.path.join(CURRENT_DIR, "service_logger.py"), "tier": 0, "timeout": 2.0, "boot_grace": 15.0},
    "service_data_compactor": {"script": os.path.join(CURRENT_DIR, "service_data_compactor.py"), "tier": 0,
                               "timeout": 215.0, "boot_grace": 215.0},
    "service_plc": {"script": os.path.join(CURRENT_DIR, "service_plc.py"), "tier": 1, "timeout": 2.0, "boot_grace": 15.0},
    "service_monitor": {"script": os.path.join(CURRENT_DIR, "service_monitor.py"), "tier": 1, "timeout": 3.0, "boot_grace": 15.0},
    "service_alarms": {"script": os.path.join(CURRENT_DIR, "service_alarms.py"), "tier": 1, "timeout": 3.0, "boot_grace": 15.0},
    "service_vac_gauge_controllers": {"script": os.path.join(CURRENT_DIR, "service_vac_gauge_controllers.py"),
                                      "tier": 2, "timeout": 2.0, "boot_grace": 15.0},
    "service_source_turbo": {"script": os.path.join(CURRENT_DIR, "service_source_turbo.py"), "tier": 2, "timeout": 2.0, "boot_grace": 15.0},
    # The magnet PSUs: one unanswered Modbus request holds the service ~2 s (pymodbus: 4 tries x 0.5 s),
    # so allow 3 s before calling it hung
    "service_magnet_psu": {"script": os.path.join(CURRENT_DIR, "service_magnet_psu.py"), "tier": 2, "timeout": 3.0, "boot_grace": 15.0},
    "service_steerer_magnet_psu": {"script": os.path.join(CURRENT_DIR, "service_steerer_magnet_psu.py"), "tier": 2, "timeout": 3.0, "boot_grace": 15.0},
    "service_spellman_mpd": {"script": os.path.join(CURRENT_DIR, "service_spellman_mpd.py"), "tier": 2, "timeout": 2.5, "boot_grace": 15.0},
    "service_smu": {"script": os.path.join(CURRENT_DIR, "service_smu.py"), "tier": 2, "timeout": 10, "boot_grace": 15.0},
    # Recipes, mass scans and the optimiser: after the device services, whose telemetry it needs
    "service_sequencer": {"script": os.path.join(CURRENT_DIR, "service_sequencer.py"), "tier": 3, "timeout": 3.0,
                          "boot_grace": 15.0},
    # ADAM module is broken (hardware). Its heartbeat name is "service_adam_monitor", so use that key if re-enabled:
    # "service_adam_monitor": {"script": os.path.join(CURRENT_DIR, "service_current_monitor.py"), "tier": 2, "timeout": 2.5, "boot_grace": 15.0},
}

# The command that stops every service and the manager itself (the launcher's "Stop services...")
# must carry this, so that nothing sends it by accident (src/launcher/programs.py)
SHUTDOWN_CONFIRM = "stop every SINDIE service"
EVENTS_SERVICE = "service_events"   # stopped last, so that the way down is still in the event log
REPLY_FLUSH_S = 0.5                 # after answering a shutdown: time for the reply to leave before closing

# Seconds to wait before restart attempt 1, 2, 3, ... (stays at the last value).
# A service that has then run ONLINE for STABLE_RESET_S gets its count reset.
BACKOFF_STEPS = (2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0)
STABLE_RESET_S = 120.0

log = logging.getLogger("service_manager")


class RestartPolicy:
    """Exponential restart backoff for one service.

    Before this, a service that failed at start-up (e.g. a device unplugged) was
    restarted every 1-2 s forever, flooding events.db and the console."""

    def __init__(self, steps=BACKOFF_STEPS, stable_after=STABLE_RESET_S):
        self.steps = tuple(steps)
        self.stable_after = stable_after
        self.failures = 0
        self.next_restart_at = None
        self.online_since = None

    def on_failure(self, now: float) -> float:
        self.failures += 1
        self.online_since = None
        delay = self.steps[min(self.failures - 1, len(self.steps) - 1)]
        self.next_restart_at = now + delay
        return delay

    def on_online(self, now: float):
        if self.online_since is None:
            self.online_since = now
        elif self.failures and now - self.online_since >= self.stable_after:
            self.failures = 0

    def on_started(self):
        self.next_restart_at = None
        self.online_since = None

    def ready(self, now: float) -> bool:
        return self.next_restart_at is not None and now >= self.next_restart_at

    def retry_in(self, now: float) -> float:
        return max(0.0, self.next_restart_at - now) if self.next_restart_at is not None else 0.0

    def reset(self):
        self.failures = 0
        self.next_restart_at = None
        self.online_since = None


def instance_key(env) -> tuple:
    """Which SINDIE installation a process belongs to: (port offset, simulated?).
    Services inherit the manager's environment, so they carry the same key."""
    try:
        offset = int(getenv("PORT_OFFSET", "0", environ=env) or 0)
    except ValueError:
        offset = None
    return offset, flag("SIMULATE", environ=env)


MY_INSTANCE = (PORT_OFFSET, paths.simulation_enabled())
REAL_SYSTEM = (0, False)


def find_orphans(script_names, me=None, my_instance=MY_INSTANCE):
    """Service processes left behind by an earlier manager of THIS instance.

    Before, every python process running a service script was killed - so starting the
    simulator or the end-to-end tests on the lab PC would have killed the live system's
    services. Now a process only counts when its environment says it is the same
    instance (same port offset, same simulated/real mode). A process whose environment
    can't be read is only treated as ours by the real system's manager (as before)."""
    me = os.getpid() if me is None else me
    found = []
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            cmdline = proc.info.get('cmdline')
            if proc.info['pid'] == me or not cmdline or 'python' not in (proc.info['name'] or '').lower():
                continue
            script = next((s for s in script_names if any(arg.endswith(s) for arg in cmdline)), None)
            if script is None:
                continue
            try:
                theirs = instance_key(proc.environ())
            except (psutil.AccessDenied, psutil.ZombieProcess, OSError):
                theirs = REAL_SYSTEM if my_instance == REAL_SYSTEM else None
            if theirs == my_instance:
                found.append((proc, script))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return found


class SindieServiceManager:
    def __init__(self, services=None):
        self.services = services or SERVICES_CONFIG
        self.events = EventHelper("service_manager")   # needed by the singleton check
        self._enforce_singleton()

        self.context = zmq.Context()

        self.heartbeat_sub = self.context.socket(zmq.SUB)
        self.heartbeat_sub.bind(ZMQ_PORT_HEARTBEAT)
        self.heartbeat_sub.setsockopt_string(zmq.SUBSCRIBE, "")

        self.health_pub = self.context.socket(zmq.PUB)
        self.health_pub.bind(ZMQ_PORT_MANAGER_PUB)

        self.commands = CommandServer(ZMQ_PORT_MANAGER_CMD, "service_manager", context=self.context,
                                      events=self.events, max_age=2.0)

        self.poller = zmq.Poller()
        self.poller.register(self.heartbeat_sub, zmq.POLLIN)
        self.poller.register(self.commands.sock, zmq.POLLIN)

        self.running_processes = {}
        self.launch_times = {}  # Track when the process was spawned
        self.last_heartbeats = {}  # Track the last *actual* received heartbeat
        self.last_self_heartbeat = 0
        self.policies = {name: RestartPolicy() for name in self.services}
        self.stopped = set()          # stopped on purpose from the HMI: no automatic restart
        self.waiting = {}             # name -> reason, while waiting out the backoff delay
        self.last_exit = {}
        self.running = True
        self._shutdown_answered_at = None

    def _enforce_singleton(self):
        self.lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.lock_socket.bind(('localhost', MANAGER_LOCK_PORT))
        except OSError:
            self.events.log_general("CRITICAL: Another service manager is already running. Aborting.",
                                    severity="CRITICAL")
            time.sleep(0.5)   # let the event leave before exiting
            sys.exit(1)

    def _purge_zombies(self):
        script_names = [os.path.basename(cfg["script"]) for cfg in self.services.values()]
        killed_count = 0
        for proc, script in find_orphans(script_names):
            try:
                self.events.log_general(f"Killing orphaned '{script}' (PID {proc.pid})")
                proc.kill()
                killed_count += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if killed_count == 0:
            self.events.log_general("No orphaned service processes found.")
        else:
            self.events.log_general(f"Purged {killed_count} orphaned processes.")
            time.sleep(1.0)

    def _service_state(self, name, cfg, now):
        if name in self.stopped:
            return "STOPPED"
        if name in self.waiting:
            return "BACKOFF"
        if name not in self.running_processes:
            return "OFFLINE"
        proc = self.running_processes[name]
        if proc.poll() is not None:
            return "CRASHED"
        if self.last_heartbeats.get(name, 0) == 0.0:
            return "STARTING"
        if now - self.last_heartbeats.get(name, 0) > cfg["timeout"]:
            return "HANGING"
        return "ONLINE"

    def _broadcast_state(self):
        """Calculates current states and instantly pushes to GUI."""
        now = time.time()
        service_states = {}
        info = {}
        for name, cfg in self.services.items():
            state = self._service_state(name, cfg, now)
            service_states[name] = state
            pol = self.policies[name]
            info[name] = {"failures": pol.failures, "retry_in": round(pol.retry_in(now), 1),
                          "reason": self.waiting.get(name, ""), "last_exit": self.last_exit.get(name)}

        payload = json.dumps({"manager.services": service_states, "manager.restart_info": info,
                              "timestamp": now}).encode('utf-8')
        self.health_pub.send_multipart([TOPIC_MANAGER, payload])
        self.last_self_heartbeat = now

    def _child_env(self):
        env = os.environ.copy()
        # Services import 'src.*': make sure the project root is importable however we were started
        env["PYTHONPATH"] = paths.PROJECT_ROOT + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        return env

    def _start_service(self, name):
        cfg = self.services[name]
        script_path = cfg["script"]

        if not os.path.exists(script_path):
            self.events.log_general(f"WARNING: {script_path} not found. Skipping.", severity="ERROR")
            return

        self.events.log_general(f"Launching {name}...")
        args = cfg.get("args", [])
        proc = subprocess.Popen([sys.executable, script_path] + args, cwd=paths.PROJECT_ROOT, env=self._child_env())

        self.running_processes[name] = proc
        self.launch_times[name] = time.time()
        self.last_heartbeats[name] = 0.0  # Reset actual heartbeat tracker
        self.waiting.pop(name, None)
        self.policies[name].on_started()

        self._broadcast_state()  # Force GUI to instantly show 'STARTING'

    def _kill_service(self, name):
        proc = self.running_processes.get(name)
        if proc:
            self.events.log_general(f"Terminating {name} (PID {proc.pid})...")
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.events.log_general(f"{name} (PID {proc.pid}) did not exit after kill", severity="ERROR")
            del self.running_processes[name]
            self._broadcast_state()  # Force GUI to instantly show 'OFFLINE'

    def _schedule_restart(self, name, reason):
        delay = self.policies[name].on_failure(time.time())
        n = self.policies[name].failures
        self.waiting[name] = reason
        self.events.log_general(f"{name}: {reason}. Restart attempt {n} in {delay:.0f} s.",
                                severity="ERROR" if n == 1 else "WARNING")

    def start_all(self):
        self._purge_zombies()
        self.events.log_general("Commencing Staggered Boot Sequence..."
                                + (" (SIMULATION MODE: fake devices, data in data_sim/)"
                                   if paths.simulation_enabled() else ""))

        tiers = {}
        for name, cfg in self.services.items():
            t = cfg["tier"]
            if t not in tiers: tiers[t] = []
            tiers[t].append(name)

        for current_tier in sorted(tiers.keys()):
            self.events.log_general(f"--- Booting Tier {current_tier} ---")
            for name in tiers[current_tier]:
                self._start_service(name)
            if current_tier < max(tiers.keys()):
                time.sleep(2.0)

    def _handle_commands(self):
        for cmd in self.commands.poll():
            msg = cmd.value if isinstance(cmd.value, dict) else {}
            action = msg.get("command")
            target = msg.get("service")
            if action == "shutdown":
                # Every service, then the manager: from the launcher, after a typed confirmation
                if msg.get("confirm") != SHUTDOWN_CONFIRM:
                    cmd.reject("shutdown needs its confirmation text (see src/launcher/programs.py)")
                    continue
                self.events.log_general(f"Operator stopped every service and the service manager "
                                        f"(from {cmd.origin or 'unknown'})", severity="WARNING")
                cmd.ok("stopping every service and the service manager")
                self._shutdown_answered_at = time.monotonic()
                self.stop()
                continue
            if target not in self.services:
                cmd.reject(f"unknown service {target!r}")
                continue
            if action == "restart":
                self.events.log_general(f"Operator requested restart of {target}")
                self.stopped.discard(target)
                self.policies[target].reset()
                self._kill_service(target)
                self._start_service(target)
                cmd.ok(f"{target} restarted")
            elif action == "stop":
                self.events.log_general(f"Operator stopped {target} (no automatic restart until started again)")
                self.stopped.add(target)
                self.waiting.pop(target, None)
                self._kill_service(target)
                cmd.ok(f"{target} stopped")
            elif action == "start":
                self.stopped.discard(target)
                self.policies[target].reset()
                if target not in self.running_processes:
                    self._start_service(target)
                cmd.ok(f"{target} started")
            else:
                cmd.reject(f"unknown manager command {action!r} (use restart / stop / start)")
        self.commands.finish()

    def _audit(self):
        """Deterministic non-blocking health audit (called every loop)."""
        now = time.time()
        for name, cfg in self.services.items():
            if name in self.stopped:
                continue
            pol = self.policies[name]

            if name in self.waiting:
                if pol.ready(now):
                    self._start_service(name)
                continue

            if name not in self.running_processes:
                continue
            proc = self.running_processes[name]

            code = proc.poll()
            if code is not None:
                del self.running_processes[name]
                self.last_exit[name] = code
                self._schedule_restart(name, f"process exited with code {code} (details in SystemLogs/{name}.log)")
                continue

            last_hb = self.last_heartbeats.get(name, 0)
            if last_hb == 0.0:
                # Still booting: allow the service's own grace period (the compactor needs minutes)
                if now - self.launch_times.get(name, 0) > cfg.get("boot_grace", 15.0):
                    self._kill_service(name)
                    self._schedule_restart(name, f"no heartbeat within {cfg.get('boot_grace', 15.0):.0f} s of start")
            elif now - last_hb > cfg["timeout"]:
                self._kill_service(name)
                self._schedule_restart(name, f"heartbeat stopped for more than {cfg['timeout']} s (hung)")
            else:
                pol.on_online(now)

    def stop(self):
        self.running = False

    def run(self):
        self.events.log_general("Infrastructure Online. Monitoring Heartbeats...")
        try:
            while self.running:
                socks = dict(self.poller.poll(timeout=100))

                if self.commands.sock in socks:
                    self._handle_commands()

                # Process Heartbeats
                if self.heartbeat_sub in socks:
                    while True:
                        try:
                            msg = self.heartbeat_sub.recv_json(zmq.NOBLOCK)
                        except zmq.Again:
                            break
                        except ValueError:
                            continue
                        svc_name = msg.get("service")
                        if svc_name in self.running_processes:
                            self.last_heartbeats[svc_name] = time.time()

                self._audit()

                # Broadcast loop (5Hz status update)
                if time.time() - self.last_self_heartbeat > 0.2:
                    self._broadcast_state()

        except KeyboardInterrupt:
            self.events.log_general("Shutdown signal received. Terminating all services...")
        finally:
            self.commands.finish()          # (the shutdown's reply is out before anything slow)
            self.stop_all_services()
            if self._shutdown_answered_at is not None:      # (the socket drops what it hasn't sent)
                time.sleep(max(0.0, REPLY_FLUSH_S - (time.monotonic() - self._shutdown_answered_at)))
            self.close()

    def close(self):
        """Every socket, the lock among them (the end of run())."""
        self.commands.close()
        self.lock_socket.close()
        self.context.destroy(linger=0)

    def stop_all_services(self):
        """The last started first (the sequencer before the devices it drives, the devices before
        the PLC service and the loggers), and the event log last, so that the way down is still
        recorded."""
        order = sorted(self.running_processes, key=lambda n: (n == EVENTS_SERVICE,
                                                              -self.services.get(n, {}).get("tier", 0),
                                                              -self.launch_times.get(n, 0.0)))
        for name in order:
            if name != EVENTS_SERVICE:
                self._kill_service(name)
        self.events.log_general("All systems offline. Goodbye.")
        if EVENTS_SERVICE in self.running_processes:
            time.sleep(0.5)                 # (for the last events to reach the event log)
            self._kill_service(EVENTS_SERVICE)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SINDIE service manager")
    parser.add_argument("--sim", action="store_true",
                        help="simulation mode: fake devices, data in data_sim/, the simulator's ports "
                             "(SINDIE_SIMULATE=1 and, unless already set, SINDIE_PORT_OFFSET=10000)")
    parser.parse_args()

    setup_logging("service_manager")
    harden_windows_process()
    manager = SindieServiceManager()

    # Stop the services too when the manager itself is stopped (kill/Task Manager 'End task'
    # on Linux sends SIGTERM, Ctrl+Break on Windows sends SIGBREAK); previously they were orphaned
    # until the next start-up purged them.
    import signal

    def _graceful(signum, frame):
        manager.stop()

    for _sig in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, _sig):
            signal.signal(getattr(signal, _sig), _graceful)

    manager.start_all()
    manager.run()
