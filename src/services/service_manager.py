import time
import os
import sys
import subprocess
import psutil
import zmq
import socket
from src.core.event_helper import EventHelper

# --- Configuration ---
from src.core.network_config import ZMQ_PORT_HEARTBEAT

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))

# Master Registry of IPIDS Services.
# Tiers dictate the boot order (T0 boots first, T3 last).
SERVICES_CONFIG = {
    # T-0: Infrastructure & Data Pipelines
    "service_events": {"script": os.path.join(CURRENT_DIR, "service_events.py"), "tier": 0, "timeout": 2.0},
    "service_logger": {"script": os.path.join(CURRENT_DIR, "service_logger.py"), "tier": 0, "timeout": 2.0},
    "service_data_compactor": {"script": os.path.join(CURRENT_DIR, "service_data_compactor.py"), "tier": 0, "timeout": 215.0},

    # T-1: Core Safety & Master Data Broker
    "service_plc": {"script": os.path.join(CURRENT_DIR, "service_plc.py"), "tier": 1, "timeout": 2.0},

    # T-2: Hardware Peripherals
    "service_vac_gauge_controllers": {"script": os.path.join(CURRENT_DIR, "service_vac_gauge_controllers.py"), "tier": 2, "timeout": 2.0},
    "service_source_turbo": {"script": os.path.join(CURRENT_DIR, "service_source_turbo.py"), "tier": 2, "timeout": 2.0},
    "service_magnet_psu": {"script": os.path.join(CURRENT_DIR, "service_magnet_psu.py"), "tier": 2, "timeout": 2.0},
    "service_spellman_mpd": {"script": os.path.join(CURRENT_DIR, "service_spellman_mpd.py"), "tier": 2, "timeout": 2.0},

    # T-3: Automation & Orchestration
    # "service_conductor":    {"script": os.path.join(CURRENT_DIR, "service_conductor.py"),    "tier": 3, "timeout": 2.0}

    #"service_dummy": {"script": os.path.join(CURRENT_DIR, "service_dummy.py"), "args": ["crash"], "tier": 2, "timeout": 2.0},
}


class IpidsServiceManager:
    def __init__(self):
        self._enforce_singleton()

        # ZMQ Heartbeat Setup
        self.context = zmq.Context()
        self.heartbeat_sub = self.context.socket(zmq.SUB)
        self.heartbeat_sub.bind(ZMQ_PORT_HEARTBEAT)  # Manager acts as the server here
        self.heartbeat_sub.setsockopt_string(zmq.SUBSCRIBE, "")

        self.poller = zmq.Poller()
        self.poller.register(self.heartbeat_sub, zmq.POLLIN)

        # State tracking
        self.running_processes = {}  # {service_name: subprocess.Popen}
        self.last_heartbeats = {}  # {service_name: timestamp}

        self.events = EventHelper("service_manager")

    def _enforce_singleton(self):
        """Prevents multiple instances of the Service Manager from binding to the ports."""
        self.lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # Bind to an obscure port. If it fails, another manager is holding it.
            self.lock_socket.bind(('localhost', 65432))
        except socket.error:
            self.events.log_general("CRITICAL: Another instance is already running. Aborting.")
            sys.exit(1)

    def _purge_zombies(self):
        """Hunts down and terminates any orphaned services from previous crashes."""
        #print("[IPIDS Manager] Executing Pre-Flight Zombie Purge...")
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
            time.sleep(1.0)  # Give the OS a moment to free up the COM ports

    def _start_service(self, name):
        """Spawns a service using the exact same Python interpreter running this manager."""
        cfg = SERVICES_CONFIG[name]
        script_path = cfg["script"]

        if not os.path.exists(script_path):
            self.events.log_general(f"WARNING: {script_path} not found. Skipping.")
            return

        self.events.log_general(f"Launching {name}...")

        # Launch independently. (In the future, you can pipe stdout/stderr to a master log here)
        args = cfg.get("args", [])
        proc = subprocess.Popen([sys.executable, script_path] + args)

        self.running_processes[name] = proc
        self.last_heartbeats[name] = time.time() + cfg["timeout"]

    def _kill_service(self, name):
        """Forcefully terminates a managed service."""
        proc = self.running_processes.get(name)
        if proc:
            self.events.log_general(f"❌ Terminating {name} (PID {proc.pid})...")
            proc.kill()
            proc.wait()  # Block until the OS confirms the PID is dead
            del self.running_processes[name]

    def start_all(self):
        self._purge_zombies()
        self.events.log_general("Commencing Staggered Boot Sequence...")

        # Group services by tier
        tiers = {}
        for name, cfg in SERVICES_CONFIG.items():
            t = cfg["tier"]
            if t not in tiers:
                tiers[t] = []
            tiers[t].append(name)

        # Boot in order T-0 to T-3
        for current_tier in sorted(tiers.keys()):
            self.events.log_general(f"--- Booting Tier {current_tier} ---")
            for name in tiers[current_tier]:
                self._start_service(name)

            # Wait for the current tier to initialize before starting the next
            if current_tier < max(tiers.keys()):
                time.sleep(2.0)

    def run(self):
        self.events.log_general("Infrastructure Online. Monitoring Heartbeats...\n")
        try:
            while True:
                # 1. Listen for heartbeats (1000ms timeout)
                socks = dict(self.poller.poll(timeout=1000))
                if self.heartbeat_sub in socks:
                    # Drain the queue of all pending heartbeats
                    while True:
                        try:
                            msg = self.heartbeat_sub.recv_json(zmq.NOBLOCK)
                            svc_name = msg.get("service")
                            if svc_name in self.last_heartbeats:
                                self.last_heartbeats[svc_name] = time.time()
                        except zmq.Again:
                            break

                            # 2. Audit Process Health
                current_time = time.time()
                for name, cfg in SERVICES_CONFIG.items():
                    if name not in self.running_processes:
                        continue  # Skipped during boot (e.g., file not found)

                    proc = self.running_processes[name]

                    # Scenario A: Fatal Exception (Process died)
                    if proc.poll() is not None:
                        self.events.log_general(f"🚨 CRASH DETECTED: {name} (PID {proc.pid}) died. Restarting...")
                        self._start_service(name)
                        continue

                    # Scenario B: Silent Hang (Process alive, but 10Hz loop is blocked)
                    time_since_beat = current_time - self.last_heartbeats[name]
                    if time_since_beat > cfg["timeout"]:
                        self.events.log_general(
                            f"🚨 HANG DETECTED: {name} unresponsive for {time_since_beat:.1f}s. Restarting...")
                        self._kill_service(name)
                        self._start_service(name)

        except KeyboardInterrupt:
            self.events.log_general("Shutdown signal received. Terminating all services...")
            for name in list(self.running_processes.keys()):
                self._kill_service(name)
            self.events.log_general("All systems offline. Goodbye.")


if __name__ == "__main__":
    manager = IpidsServiceManager()
    manager.start_all()
    manager.run()