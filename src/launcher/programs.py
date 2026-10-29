"""Starting and stopping SINDIE's programs without PyCharm. No Qt here.

The programs run with the same Python as whatever started them: from PyCharm that is the project's
own interpreter (its virtual environment), which already has every library SINDIE needs, so
nothing has to be installed again. The shortcuts made here point at that Python, as pythonw.exe
(no console window).

    python_for(gui=True)        the Python to run a program with
    open_program("hmi")         start the HMI or data viewer (or bring a running one to the front: Qt side)
    manager_running()           is the service manager running (its lock port is taken)?
    other_manager_running()     ... and the other system's on this PC (the simulator's, or the real one's)?
    sindie_processes()          every SINDIE service manager and service running on this PC, by system
    start_manager()             start it in the background, outliving the launcher
    stop_manager()              ask it to stop every service and itself
    services_stopped()          stopped on purpose from a launcher (left stopped until Start services)?
    make_shortcuts(), set_start_with_windows(on)     (Windows)
"""
import json
import logging
import os
import socket
import subprocess
import sys
import time
from typing import Optional

from src.core import paths
from src.core.env import SIM_PORT_OFFSET
from src.core.network_map import MANAGER_LOCK_PORT, PORT_OFFSET, manager_lock_port

log = logging.getLogger("launcher")

ROOT = paths.PROJECT_ROOT
SCRIPTS = {
    "manager": os.path.join("src", "services", "service_manager.py"),
    "hmi": os.path.join("src", "hmi.py"),
    "viewer": os.path.join("src", "gui", "data_viewer", "viewer_main.py"),
    "launcher": "main_launcher.py",
    "simulator": "run_simulation.py",
}
TITLES = {"manager": "service manager", "hmi": "Operator HMI", "viewer": "Data viewer", "launcher": "SINDIE",
          "simulator": "Simulator"}
# The service manager's command to stop every service and itself must carry this, so that nothing
# sends it by accident (src/services/service_manager.py)
SHUTDOWN_CONFIRM = "stop every SINDIE service"
STARTUP_SHORTCUT = "SINDIE services.lnk"
DESKTOP_SHORTCUTS = (("SINDIE.lnk", "launcher", "SINDIE: the launcher, the services' state, the HMI and the data viewer"),
                     ("SINDIE HMI.lnk", "hmi", "SINDIE operator HMI"),
                     ("SINDIE Data Viewer.lnk", "viewer", "SINDIE data viewer"))
ICON_FILE = os.path.join(ROOT, "src", "launcher", "sindie.ico")

# Windows process creation flags (for the helper that starts the programs: src/launcher/detach.py)
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def simulation() -> bool:
    return paths.simulation_enabled()


def mode_args() -> list:
    """What makes a program join this launcher's system: the simulator's ports and data, or the
    real ones."""
    return ["--sim"] if simulation() else []


def _user() -> str:
    try:
        import getpass
        return getpass.getuser()
    except (ImportError, KeyError, OSError):
        return "user"


def instance_name(program: str, sim: bool = None) -> str:
    """A name per program, user and system (the simulator and the real system apart, each port
    offset apart): a second copy of a program only defers to a first of the same system. sim: the
    name in the simulator's system (True) or the real one's (False) instead of this one's."""
    here = simulation()
    sim = here if sim is None else bool(sim)
    offset = PORT_OFFSET if sim == here else (SIM_PORT_OFFSET if sim else 0)
    safe = "".join(ch if ch.isalnum() else "-" for ch in _user())
    return f"sindie-{program}-{'sim' if sim else 'live'}-{offset}-{safe}"


def python_for(gui: bool = True) -> str:
    """The Python running this program; on Windows its pythonw.exe for a program that needs no
    console window (the HMI, the data viewer, the service manager)."""
    exe = sys.executable
    if os.name == "nt":
        folder, name = os.path.split(exe)
        want = "pythonw.exe" if gui else "python.exe"
        if name.lower() != want and os.path.exists(os.path.join(folder, want)):
            return os.path.join(folder, want)
    return exe


def script(program: str) -> str:
    return os.path.join(ROOT, SCRIPTS[program])


_REGISTRY_SET_HERE = []         # SINDIE_REGISTRY was set by use_simulator_registry (not by whoever started this)


def child_env(own_registry: bool = True) -> dict:
    """This program's environment for one it starts. own_registry=False: without the simulator's tag
    registry this one chose (use_simulator_registry) - the launcher started again after an update
    builds its own, with the new version."""
    env = os.environ.copy()
    env["PYTHONPATH"] = ROOT + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    if not own_registry and _REGISTRY_SET_HERE:
        env.pop("SINDIE_REGISTRY", None)
    return env


def spawn(args: list, log_name: str, console: bool = False, env: dict = None) -> int:
    """Start a program that outlives this one and is nobody's child: through src/launcher/detach.py,
    which starts it (on Windows with no console window, or a new one; in its own process group, out of
    any job object) and exits at once. PyCharm's Stop button ends a run's whole process tree: a service
    manager started as a child of a launcher run from PyCharm would go with it, and the PLC would then
    shut the source down. Its output is appended to SystemLogs/<log_name>. Returns its process id."""
    os.makedirs(paths.LOG_DIR, exist_ok=True)
    log_path = os.path.join(paths.LOG_DIR, log_name)
    with open(log_path, "a", encoding="utf-8", errors="replace") as out:
        out.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(args)}\n")
    helper = [python_for(gui=True), "-m", "src.launcher.detach"] + (["--console"] if console else []) + [log_path] + args
    kw = dict(cwd=ROOT, env=env or child_env(), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    if os.name == "nt":
        kw["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        done = subprocess.run(helper, **kw)
    except subprocess.SubprocessError as e:             # (the helper didn't finish within the minute)
        raise OSError(f"the helper that starts it didn't finish ({e})") from e
    if done.returncode != 0 or not done.stdout.strip().isdigit():
        raise OSError((done.stderr or done.stdout or f"exit code {done.returncode}").strip()[-400:])
    return int(done.stdout.strip())


def open_program(program: str) -> int:
    """Start the HMI, the data viewer or the launcher for this system. (A copy already running
    comes to the front instead: the program itself sees to that, src/launcher/instance.py.)"""
    log.info("Starting the %s", TITLES.get(program, program))
    if simulation():
        use_simulator_registry()
    return spawn([python_for(gui=True), script(program)] + mode_args(), f"{program}_console.log")


def open_simulator() -> int:
    """run_simulation.py in a console window of its own, where it shows its fault menu (Windows:
    elsewhere there is no console to give it)."""
    if os.name != "nt":
        raise OSError("the simulator opens in a console window of its own on Windows only: run "
                      "run_simulation.py in a terminal")
    return spawn([python_for(gui=False), script("simulator")], "simulator_console.log", console=True)


# --- The service manager ----------------------------------------------------------------------------
def _lock_taken(port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("localhost", port))
    except OSError:
        return True
    finally:
        probe.close()
    return False


def manager_running() -> bool:
    """Whether this system's service manager is running: it holds its lock port while it does."""
    return _lock_taken(MANAGER_LOCK_PORT)


def other_manager_running() -> bool:
    """Whether the other system's service manager on this PC is running: the simulator's (or, from
    the simulator's launcher, the real system's). Both run the same software."""
    return _lock_taken(manager_lock_port(0 if simulation() else SIM_PORT_OFFSET))


REAL = (0, False)               # the real system's (port offset, simulated?)


def this_system() -> tuple:
    """This program's system, as service_manager.instance_key names one: (port offset, simulated?)."""
    return PORT_OFFSET, simulation()


def system_name(key) -> str:
    """'the real system', 'the simulator', 'a simulator on port offset 20000' ..."""
    if key is None:
        return "a SINDIE system whose ports can't be told"
    offset, sim = key
    if (offset, bool(sim)) == REAL:
        return "the real system"
    if sim and offset == SIM_PORT_OFFSET:
        return "the simulator"
    return f"{'a simulator' if sim else 'a copy of SINDIE'} on port offset {offset}"


def sindie_processes() -> dict:
    """{system: [names]} of every SINDIE service manager and service running on this PC, the
    system a process belongs to read from its environment (service_manager.instance_key: None if it
    can't be read). It takes a moment (every process is looked at): not for the window's thread."""
    import psutil
    from src.services.service_manager import SERVICES_CONFIG, instance_key
    scripts = [os.path.basename(SCRIPTS["manager"])] + [os.path.basename(c["script"]) for c in SERVICES_CONFIG.values()]
    found, me = {}, os.getpid()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmdline = proc.info.get("cmdline")
            if proc.info["pid"] == me or not cmdline or "python" not in (proc.info["name"] or "").lower():
                continue
            name = next((s for s in scripts if any(str(arg).endswith(s) for arg in cmdline)), None)
            if name is None:
                continue
            try:
                key = instance_key(proc.environ())
            except (psutil.AccessDenied, psutil.ZombieProcess, OSError):
                key = None
            found.setdefault(key, set()).add(os.path.splitext(name)[0])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return {key: sorted(names) for key, names in found.items()}


def start_manager() -> int:
    """The service manager in the background, with no console window to close by mistake. It
    starts every service, and keeps running when the launcher or the HMI is closed."""
    log.info("Starting the service manager in the background")
    if simulation():
        use_simulator_registry()
    return spawn([python_for(gui=True), script("manager")] + mode_args(), "service_manager_console.log")


def use_simulator_registry():
    """The simulator's own tag registry, built from the config files as run_simulation.py builds it
    (data_sim/system_tags.json), for the programs this launcher starts - unless one is set."""
    if os.environ.get("SINDIE_REGISTRY", "").strip():
        return
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    import run_simulation
    os.environ["SINDIE_REGISTRY"] = run_simulation.prepare_registry(paths.DATA_DIR)
    _REGISTRY_SET_HERE.append(True)


def rebuild_simulator_registry(python: str = None) -> str:
    """After an update, from the simulator's launcher: the simulator's tag registry built again by the
    version now on disk (this program runs the version before), on a copy of the real system's as
    run_simulation.py builds it. '' or what went wrong."""
    if not _REGISTRY_SET_HERE:
        return ""                           # (not chosen here: whoever set SINDIE_REGISTRY looks after it)
    from src.launcher import updater
    out = os.environ["SINDIE_REGISTRY"]
    real = os.path.join(ROOT, "config", "system_tags.json")
    if os.path.exists(real):
        import shutil
        shutil.copyfile(real, out)
    return updater.build_registry_into(out, ROOT, python)


def stop_manager(timeout: float = 20.0, client=None) -> str:
    """Ask the service manager to stop every service and then itself; '' once it has, otherwise
    what went wrong. (Its reply can be lost as it closes down: what counts is that its lock goes.)"""
    from src.core import commands as C
    own = client is None
    client = client or C.CommandClient(default_timeout=5.0)
    try:
        reply = client.request("manager", "manager_cmd", {"command": "shutdown", "confirm": SHUTDOWN_CONFIRM},
                               origin="launcher")
    finally:
        if own:
            client.close()
    status = reply.get("status")
    if status in (C.REJECTED, C.FAILED):
        return f"The service manager refused: {C.describe(reply)}"
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not manager_running():
            return ""
        time.sleep(0.25)
    if status not in C.SUCCESS:
        return f"The service manager didn't answer ({C.describe(reply)}) and is still running"
    return f"The service manager said it would stop, but is still running after {timeout:.0f} s"


# --- Stopped on purpose -------------------------------------------------------------------------------
def stop_marker_path() -> str:
    """Where a launcher records that the services were stopped on purpose (Stop services...), so
    that no launcher of this system - this one restarted, or the one Windows starts at log-on -
    starts them again until someone presses Start services."""
    return os.path.join(paths.DATA_DIR, f"launcher_state_{PORT_OFFSET}.json")


def services_stopped() -> Optional[dict]:
    """{'at': when, 'by': who} while the services are stopped on purpose; None otherwise."""
    try:
        with open(stop_marker_path(), "r", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) and state.get("services_stopped") else None


def record_services_stopped(stopped: bool):
    """Stop services... (True) and Start services (False). The file is rewritten, never deleted."""
    path = stop_marker_path()
    state = {"services_stopped": bool(stopped), "at": time.strftime("%Y-%m-%d %H:%M"), "by": _user()}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(path + ".tmp", path)
    except OSError as e:
        log.error("Couldn't record that the services were %s (%s): %s", "stopped" if stopped else "started", path, e)


def end_manager_processes() -> int:
    """The last resort when the service manager doesn't answer: end it and every service it
    started (this system's only). Returns how many processes were ended."""
    import psutil
    from src.services.service_manager import MY_INSTANCE, SERVICES_CONFIG, find_orphans
    names = [os.path.basename(SCRIPTS["manager"])] + [os.path.basename(c["script"]) for c in SERVICES_CONFIG.values()]
    ended = 0
    for proc, name in find_orphans(names, my_instance=MY_INSTANCE):
        try:
            log.warning("Ending %s (PID %s): the service manager didn't answer", name, proc.pid)
            proc.kill()
            ended += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return ended


# --- Windows shortcuts ------------------------------------------------------------------------------
def _ps_quote(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def _powershell(command: str, timeout: float = 20.0) -> str:
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                          command], capture_output=True, text=True, timeout=timeout,
                         creationflags=0x08000000 if os.name == "nt" else 0)        # (no console flash)
    if out.returncode != 0:
        raise OSError((out.stderr or out.stdout or "PowerShell failed").strip()[:500])
    return out.stdout.strip()


def windows_folder(kind: str) -> str:
    """'Desktop' or 'Startup', where Windows keeps it for this user (a desktop moved to OneDrive
    included)."""
    folder = _powershell(f"[Environment]::GetFolderPath({_ps_quote(kind)})")
    if not folder:
        raise OSError(f"Windows didn't say where the {kind} folder is")
    return folder


def shortcut_command(program: str, extra=()) -> tuple:
    """(target, arguments) of a shortcut that starts a program for this system."""
    args = [script(program)] + mode_args() + list(extra)
    return python_for(gui=True), " ".join(f'"{a}"' if " " in a else a for a in args)


def make_shortcut(path: str, program: str, description: str, extra=()):
    target, arguments = shortcut_command(program, extra)
    lines = [f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({_ps_quote(path)})",
             f"$s.TargetPath = {_ps_quote(target)}", f"$s.Arguments = {_ps_quote(arguments)}",
             f"$s.WorkingDirectory = {_ps_quote(ROOT)}", f"$s.Description = {_ps_quote(description)}"]
    if os.path.exists(ICON_FILE):
        lines.append(f"$s.IconLocation = {_ps_quote(ICON_FILE + ',0')}")
    lines.append("$s.Save()")
    _powershell("; ".join(lines))
    log.info("Shortcut %s -> %s %s", path, target, arguments)


def make_desktop_shortcuts() -> list:
    """SINDIE, SINDIE HMI and SINDIE Data Viewer on the desktop; returns their paths."""
    desktop = windows_folder("Desktop")
    made = []
    for name, program, description in DESKTOP_SHORTCUTS:
        if simulation():
            name = name.replace(".lnk", " (simulator).lnk")
        path = os.path.join(desktop, name)
        make_shortcut(path, program, description)
        made.append(path)
    return made


def start_with_windows() -> bool:
    try:
        return os.path.exists(os.path.join(windows_folder("Startup"), STARTUP_SHORTCUT))
    except (OSError, subprocess.SubprocessError):
        return False


def set_start_with_windows(on: bool):
    """A shortcut in the Startup folder that starts the launcher in the tray when this user logs
    on, and the services with it. The real system's only: never the simulator's."""
    if simulation():
        raise OSError("only the real system's launcher starts with Windows")
    path = os.path.join(windows_folder("Startup"), STARTUP_SHORTCUT)
    if on:
        make_shortcut(path, "launcher", "SINDIE: start the services and keep them running (tray icon)",
                      extra=("--tray", "--keep-running"))
    elif os.path.exists(path):
        os.remove(path)             # (only this shortcut, which the launcher made)
        log.info("Removed %s", path)
