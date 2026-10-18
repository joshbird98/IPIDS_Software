"""Run SINDIE without the implanter: every service against simulated hardware, plus the
operator HMI and the data viewer.

    python run_simulation.py               services + HMI + data viewer
    python run_simulation.py --no-viewer   (--no-hmi likewise)

It uses its own ZeroMQ ports (offset 10000) and keeps its data and logs in data_sim/,
so it is safe to start on the lab PC while the real system is running: nothing it does
reaches the real services, hardware or data.

This window then shows a menu for injecting faults (PLC lost, safety relay trip,
Spellman arc, magnet sag, ...) to see how the services and the HMI react.
Type q (or close this window) to stop everything.
"""
import argparse
import os
import signal
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
SIM_PORT_OFFSET = 10000


def _launch(cmd, log_path, env, new_group=False):
    flags = 0
    if new_group and os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP   # lets us send Ctrl+Break to the manager only
    log = open(log_path, "a", buffering=1)
    log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(cmd)}\n")
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
    proc.log_file = log
    return proc


def _stop_manager(proc, timeout=20.0):
    """Ask the manager to stop (it then stops every service), kill it if it won't."""
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
        proc.wait(timeout)
    except (OSError, subprocess.TimeoutExpired):
        proc.kill()
        proc.wait(5)


def _wait_for_services(timeout=90.0):
    """Print the services' states until they are all ONLINE (or the timeout)."""
    import orjson
    import zmq
    from src.core.network_map import ZMQ_PORT_MANAGER_PUB
    ctx = zmq.Context.instance()
    sub = ctx.socket(zmq.SUB)
    sub.setsockopt(zmq.SUBSCRIBE, b"")
    sub.connect(ZMQ_PORT_MANAGER_PUB)
    shown, end = None, time.time() + timeout
    try:
        while time.time() < end:
            if not sub.poll(500):
                continue
            states = orjson.loads(sub.recv_multipart()[1]).get("manager.services", {})
            summary = ", ".join(f"{n.replace('service_', '')}: {s}" for n, s in sorted(states.items())
                                if s != "ONLINE")
            if summary != shown:
                shown = summary
                online = sum(1 for s in states.values() if s == "ONLINE")
                print(f"  {online}/{len(states)} services online" + (f"  (waiting for {summary})" if summary else ""))
            if states and all(s == "ONLINE" for s in states.values()):
                return True
        return False
    finally:
        sub.close(0)


def _menu(sim, procs):
    names = list(sim.FAULT_DESCRIPTIONS)
    closed = set()
    while True:
        active = sim.injected_faults()
        print("\nSimulated faults - type a number to switch it on/off, c to clear all, q to quit:")
        for i, name in enumerate(names, 1):
            print(f"  {i:2d} [{'x' if name in active else ' '}] {name:20s} {sim.FAULT_DESCRIPTIONS[name]}")
        try:
            choice = input("> ").strip().lower()
        except EOFError:
            return
        dead = {n for n, p in procs.items() if p.poll() is not None}
        if "manager" in dead:
            print("The service manager has stopped - see service_manager.log in data_sim/SystemLogs")
            return
        for n in sorted(dead - closed):
            print(f"(the {n} window was closed)")
        closed |= dead
        if choice in ("q", "quit", "exit"):
            return
        if choice == "c":
            sim.set_faults(set())
        elif choice.isdigit() and 1 <= int(choice) <= len(names):
            sim.set_faults(set(active) ^ {names[int(choice) - 1]})
        elif choice:
            print("?")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--no-hmi", action="store_true", help="don't open the operator HMI")
    ap.add_argument("--no-viewer", action="store_true", help="don't open the data viewer")
    ap.add_argument("--port-offset", type=int, default=SIM_PORT_OFFSET,
                    help=f"ZeroMQ port offset (default {SIM_PORT_OFFSET}; 0 would clash with a running real system)")
    args = ap.parse_args()

    # Before anything from src is imported: they read these at import time
    os.environ["SINDIE_SIMULATE"] = "1"
    os.environ["SINDIE_PORT_OFFSET"] = str(args.port_offset)
    sys.path.insert(0, ROOT)
    env = dict(os.environ, PYTHONPATH=ROOT + (os.pathsep + os.environ["PYTHONPATH"]
                                               if os.environ.get("PYTHONPATH") else ""))

    from src import sim
    from src.core import paths
    from src.core.network_map import MANAGER_LOCK_PORT

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("localhost", MANAGER_LOCK_PORT))
    except OSError:
        sys.exit(f"A service manager is already running on port offset {args.port_offset} "
                 f"(another run_simulation.py?). Stop it first, or use --port-offset.")
    finally:
        probe.close()

    os.makedirs(paths.LOG_DIR, exist_ok=True)
    sim.set_faults(set())
    print(f"SINDIE simulation - data and logs in {paths.DATA_DIR}, ports offset by {args.port_offset}")
    print("Starting the services (simulated devices)...")
    procs = {"manager": _launch([sys.executable, os.path.join("src", "services", "service_manager.py"), "--sim"],
                                os.path.join(paths.LOG_DIR, "simulation_manager_console.log"), env, new_group=True)}
    try:
        if not _wait_for_services():
            print("Not every service came up - carrying on; check the services panel in the HMI.")
        if not args.no_hmi:
            procs["HMI"] = _launch([sys.executable, os.path.join("src", "hmi.py"), "--sim"],
                                   os.path.join(paths.LOG_DIR, "simulation_hmi_console.log"), env)
        if not args.no_viewer:
            procs["viewer"] = _launch([sys.executable, os.path.join("src", "gui", "data_viewer", "viewer_main.py"),
                                       "--sim"], os.path.join(paths.LOG_DIR, "simulation_viewer_console.log"), env)
        print("\nRunning. In the HMI: open the gate valve and energise the safety relay before the supplies "
              "accept commands - just as on the real machine.")
        _menu(sim, procs)
    except KeyboardInterrupt:
        pass
    finally:
        print("Stopping...")
        sim.set_faults(set())
        for name, proc in procs.items():
            if name != "manager" and proc.poll() is None:
                proc.terminate()
        _stop_manager(procs["manager"])
        for proc in procs.values():
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
            proc.log_file.close()
        print("Stopped.")


if __name__ == "__main__":
    main()
