import time
import os
import sys
import subprocess
import psutil
import zmq
import socket
import json
from src.core.event_helper import EventHelper
from src.core.network_map import ZMQ_PORT_HEARTBEAT, ZMQ_PORT_MANAGER_PUB, ZMQ_PORT_MANAGER_CMD
from src.core.os_helper import harden_windows_process

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))

SERVICES_CONFIG = {
    "service_events": {"script": os.path.join(CURRENT_DIR, "service_events.py"), "tier": 0, "timeout": 2.0, "boot_grace": 15.0},
    "service_logger": {"script": os.path.join(CURRENT_DIR, "service_logger.py"), "tier": 0, "timeout": 2.0, "boot_grace": 15.0},
    "service_data_compactor": {"script": os.path.join(CURRENT_DIR, "service_data_compactor.py"), "tier": 0,
                               "timeout": 215.0, "boot_grace": 215.0},
    "service_plc": {"script": os.path.join(CURRENT_DIR, "service_plc.py"), "tier": 1, "timeout": 2.0, "boot_grace": 15.0},
    "service_vac_gauge_controllers": {"script": os.path.join(CURRENT_DIR, "service_vac_gauge_controllers.py"),
                                      "tier": 2, "timeout": 2.0, "boot_grace": 15.0},
    "service_source_turbo": {"script": os.path.join(CURRENT_DIR, "service_source_turbo.py"), "tier": 2, "timeout": 2.0, "boot_grace": 15.0},
    "service_magnet_psu": {"script": os.path.join(CURRENT_DIR, "service_magnet_psu.py"), "tier": 2, "timeout": 2.0, "boot_grace": 15.0},
    "service_spellman_mpd": {"script": os.path.join(CURRENT_DIR, "service_spellman_mpd.py"), "tier": 2, "timeout": 2.5, "boot_grace": 15.0},
    "service_smu": {"script": os.path.join(CURRENT_DIR, "service_smu.py"), "tier": 2, "timeout": 10, "boot_grace": 15.0},
    #"service_current_mon": {"script": os.path.join(CURRENT_DIR, "service_current_monitor.py"), "tier": 2, "timeout": 2.5, "boot_grace": 15.0},
}


class IpidsServiceManager:
    def __init__(self):
        self._enforce_singleton()

        self.context = zmq.Context()

        self.heartbeat_sub = self.context.socket(zmq.SUB)
        self.heartbeat_sub.bind(ZMQ_PORT_HEARTBEAT)
        self.heartbeat_sub.setsockopt_string(zmq.SUBSCRIBE, "")

        self.health_pub = self.context.socket(zmq.PUB)
        self.health_pub.bind(ZMQ_PORT_MANAGER_PUB)

        self.cmd_sub = self.context.socket(zmq.SUB)
        self.cmd_sub.bind(ZMQ_PORT_MANAGER_CMD)
        self.cmd_sub.setsockopt_string(zmq.SUBSCRIBE, "")

        self.poller = zmq.Poller()
        self.poller.register(self.heartbeat_sub, zmq.POLLIN)
        self.poller.register(self.cmd_sub, zmq.POLLIN)

        self.running_processes = {}
        self.launch_times = {}  # Track when the process was spawned
        self.last_heartbeats = {}  # Track the last *actual* received heartbeat
        self.last_self_heartbeat = 0

        self.events = EventHelper("service_manager")

    def _enforce_singleton(self):
        self.lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.lock_socket.bind(('localhost', 65432))
        except socket.error:
            self.events.log_general("CRITICAL: Another instance is already running. Aborting.")
            sys.exit(1)

    def _purge_zombies(self):
        script_names = [os.path.basename(cfg["script"]) for cfg in SERVICES_CONFIG.values()]
        killed_count = 0
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                cmdline = proc.info.get('cmdline')
                if cmdline and 'python' in proc.info['name'].lower():
                    for script in script_names:
                        if any(script in arg for arg in cmdline):
                            self.events.log_general(f"Assassinating Zombie: '{script}' (PID {proc.info['pid']})")
                            proc.kill()
                            killed_count += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if killed_count == 0:
            self.events.log_general("Airspace clear. No zombies found.")
        else:
            self.events.log_general(f"Purged {killed_count} orphaned processes.")
            time.sleep(1.0)

    def _broadcast_state(self):
        """Calculates current states and instantly pushes to GUI."""
        current_time = time.time()
        service_states = {}

        for name, cfg in SERVICES_CONFIG.items():
            if name not in self.running_processes:
                service_states[name] = "OFFLINE"
            else:
                proc = self.running_processes[name]
                if proc.poll() is not None:
                    service_states[name] = "CRASHED"
                elif self.last_heartbeats.get(name, 0) == 0.0:
                    # Process is running, but hasn't sent a heartbeat yet
                    service_states[name] = "STARTING"
                elif current_time - self.last_heartbeats.get(name, 0) > cfg["timeout"]:
                    service_states[name] = "HANGING"
                else:
                    service_states[name] = "ONLINE"

        payload = json.dumps({"manager.services": service_states}).encode('utf-8')
        self.health_pub.send_multipart([b"MANAGER", payload])
        self.last_self_heartbeat = current_time

    def _start_service(self, name):
        cfg = SERVICES_CONFIG[name]
        script_path = cfg["script"]

        if not os.path.exists(script_path):
            self.events.log_general(f"WARNING: {script_path} not found. Skipping.")
            return

        self.events.log_general(f"Launching {name}...")
        args = cfg.get("args", [])
        proc = subprocess.Popen([sys.executable, script_path] + args)

        self.running_processes[name] = proc
        self.launch_times[name] = time.time()
        self.last_heartbeats[name] = 0.0  # Reset actual heartbeat tracker

        self._broadcast_state()  # Force GUI to instantly show 'STARTING'

    def _kill_service(self, name):
        proc = self.running_processes.get(name)
        if proc:
            self.events.log_general(f"❌ Terminating {name} (PID {proc.pid})...")
            proc.kill()
            proc.wait()
            del self.running_processes[name]
            self._broadcast_state()  # Force GUI to instantly show 'OFFLINE'

    def start_all(self):
        self._purge_zombies()
        self.events.log_general("Commencing Staggered Boot Sequence...")

        tiers = {}
        for name, cfg in SERVICES_CONFIG.items():
            t = cfg["tier"]
            if t not in tiers: tiers[t] = []
            tiers[t].append(name)

        for current_tier in sorted(tiers.keys()):
            self.events.log_general(f"--- Booting Tier {current_tier} ---")
            for name in tiers[current_tier]:
                self._start_service(name)
            if current_tier < max(tiers.keys()):
                time.sleep(2.0)

    def run(self):
        self.events.log_general("Infrastructure Online. Monitoring Heartbeats...\n")
        try:
            while True:
                socks = dict(self.poller.poll(timeout=100))

                # GUI Commands
                if self.cmd_sub in socks:
                    while True:
                        try:
                            msg = self.cmd_sub.recv_json(zmq.NOBLOCK)
                            if msg.get("command") == "restart":
                                target_svc = msg.get("service")
                                if target_svc in SERVICES_CONFIG:
                                    self.events.log_general(f"🛠️ GUI requested manual restart of {target_svc}")
                                    self._kill_service(target_svc)
                                    self._start_service(target_svc)
                        except zmq.Again:
                            break

                # Process Heartbeats
                if self.heartbeat_sub in socks:
                    while True:
                        try:
                            msg = self.heartbeat_sub.recv_json(zmq.NOBLOCK)
                            svc_name = msg.get("service")
                            if svc_name in self.running_processes:
                                self.last_heartbeats[svc_name] = time.time()
                        except zmq.Again:
                            break

                # --- DETERMINISTIC NON-BLOCKING HEALTH AUDIT ---
                current_time = time.time()
                for name, cfg in SERVICES_CONFIG.items():
                    if name not in self.running_processes: continue
                    proc = self.running_processes[name]

                    if proc.poll() is not None:
                        self.events.log_general(f"🚨 CRASH DETECTED: {name} (PID {proc.pid}) died. Restarting...")
                        self._start_service(name)
                        continue

                    last_hb = self.last_heartbeats.get(name, 0)

                    if last_hb == 0.0:
                        # Service is still booting up during the tiered launch sequence
                        # Evaluate strictly against a loose boot grace time (15s)
                        if current_time - self.launch_times.get(name, 0) > 15.0:
                            self.events.log_general(
                                f"🚨 INITIALIZATION FAILURE: {name} failed to spin up within grace period. Restarting...")
                            self._kill_service(name)
                            self._start_service(name)
                    else:
                        # Service is alive and actively cycling.
                        # Enforce your high-responsiveness runtime watchdog limit natively.
                        if current_time - last_hb > cfg["timeout"]:
                            self.events.log_general(
                                f"🚨 HANG DETECTED: {name} failed fast watchdog threshold ({cfg['timeout']}s). Restarting...")
                            self._kill_service(name)
                            self._start_service(name)

                # Broadcast loop (5Hz status update)
                if current_time - self.last_self_heartbeat > 0.2:
                    self._broadcast_state()

        except KeyboardInterrupt:
            self.events.log_general("Shutdown signal received. Terminating all services...")
            for name in list(self.running_processes.keys()):
                self._kill_service(name)
            self.events.log_general("All systems offline. Goodbye.")


if __name__ == "__main__":
    harden_windows_process()
    manager = IpidsServiceManager()
    manager.start_all()
    manager.run()