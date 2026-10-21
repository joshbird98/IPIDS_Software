"""The launcher (main_launcher.py, src/launcher/): which Python, programs started so that they
outlive it, one copy of each program, the service manager stopped only on a confirmed request,
and the window's view of the services."""
import os
import socket
import subprocess
import sys
import threading
import time

import pytest
from PyQt6.QtWidgets import QApplication

from src.launcher import programs


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def test_which_python_and_what_to_run():
    assert programs.python_for(gui=False) == sys.executable
    if os.name != "nt":
        assert programs.python_for(gui=True) == sys.executable
    name = programs.instance_name("hmi")
    assert name.startswith("sindie-hmi-") and str(programs.PORT_OFFSET) in name
    assert ("-sim-" in name) == programs.simulation()
    target, args = programs.shortcut_command("launcher", ("--tray", "--keep-running"))
    assert target == programs.python_for(gui=True)
    assert args.endswith("--tray --keep-running") and "main_launcher.py" in args
    assert programs._ps_quote("C:\\Josh's folder") == "'C:\\Josh''s folder'"


def test_a_helper_that_hangs_is_an_oserror(monkeypatch):
    def hang(*a, **k):
        raise subprocess.TimeoutExpired("python", 60)
    monkeypatch.setattr(programs.subprocess, "run", hang)
    with pytest.raises(OSError, match="didn't finish"):
        programs.spawn([sys.executable, "-c", "pass"], "probe_console.log")


def test_a_console_of_its_own_only_on_windows(tmp_path):
    """The simulator's console window (Windows); elsewhere the output still goes to the log."""
    from src.launcher import detach
    log_path = str(tmp_path / "c.log")
    proc = detach.start([sys.executable, "-c", "print('to the log')"], log_path, console=True)
    proc.wait(10)
    if os.name != "nt":
        assert "to the log" in open(log_path).read()
        with pytest.raises(OSError, match="Windows only"):
            programs.open_simulator()


def test_a_program_started_outlives_and_logs(tmp_path, monkeypatch):
    import psutil
    monkeypatch.setattr(programs.paths, "LOG_DIR", str(tmp_path))
    pid = programs.spawn([sys.executable, "-c", "print('hello from a child')"], "probe_console.log")
    assert isinstance(pid, int)
    deadline = time.time() + 10
    while "hello from a child" not in (tmp_path / "probe_console.log").read_text() and time.time() < deadline:
        time.sleep(0.1)
    text = (tmp_path / "probe_console.log").read_text()
    assert "=====" in text and "hello from a child" in text
    pid = programs.spawn([sys.executable, "-c", "import time; time.sleep(30)"], "probe_console.log")
    try:                                                # nobody's child, in a session of its own
        assert pid not in {p.pid for p in psutil.Process().children(recursive=True)}
        if os.name != "nt":
            assert os.getsid(pid) == pid != os.getsid(0)
    finally:
        psutil.Process(pid).kill()


def test_the_manager_counts_as_running_while_its_lock_is_held():
    assert not programs.manager_running()
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lock.bind(("localhost", programs.MANAGER_LOCK_PORT))
    try:
        assert programs.manager_running()
    finally:
        lock.close()
    assert not programs.manager_running()


def test_the_manager_stops_only_on_a_confirmed_request(fake_events):
    """Stop services: the manager takes the request only with its confirmation text, then stops
    every service and itself (its lock port is free again)."""
    from src.core import commands as C
    from src.services import service_manager as sm
    manager = sm.SindieServiceManager()
    manager.events = fake_events
    t = threading.Thread(target=manager.run, daemon=True)
    t.start()
    client = C.CommandClient(default_timeout=5.0)
    try:
        assert programs.manager_running()
        reply = client.request("manager", "manager_cmd", {"command": "shutdown"})
        assert reply["status"] == C.REJECTED and "confirmation" in reply["reason"]
        time.sleep(0.3)
        assert t.is_alive()
        assert programs.stop_manager(timeout=10.0, client=client) == ""
        t.join(10)
        assert not t.is_alive() and not programs.manager_running()
        assert any("stopped every service" in e["message"] for e in fake_events.events)
    finally:
        manager.stop()
        t.join(10)
        client.close()
    assert sm.SHUTDOWN_CONFIRM == programs.SHUTDOWN_CONFIRM


def test_the_services_stop_last_started_first_and_the_event_log_last(fake_events, monkeypatch):
    from src.services import service_manager as sm
    manager = sm.SindieServiceManager()
    manager.events = fake_events
    try:
        killed = []
        monkeypatch.setattr(manager, "_kill_service", lambda name: killed.append(name)
                            or manager.running_processes.pop(name))
        monkeypatch.setattr(sm.time, "sleep", lambda s: None)
        for i, name in enumerate(["service_events", "service_logger", "service_plc", "service_magnet_psu",
                                  "service_spellman_mpd", "service_sequencer"]):
            manager.running_processes[name] = object()
            manager.launch_times[name] = 100.0 + i
        manager.stop_all_services()
        assert killed == ["service_sequencer", "service_spellman_mpd", "service_magnet_psu", "service_plc",
                          "service_logger", "service_events"]
        assert fake_events.events[-1]["message"] == "All systems offline. Goodbye."
    finally:
        manager.close()


def test_stopping_waits_for_the_lock_even_if_the_reply_is_lost(monkeypatch):
    from src.core import commands as C

    class Client:
        def __init__(self, status):
            self.status = status

        def request(self, *a, **k):
            return {"status": self.status, "reason": "no reply" if self.status == C.TIMEOUT else "no"}

    gone_at = time.monotonic() + 0.6
    monkeypatch.setattr(programs, "manager_running", lambda: time.monotonic() < gone_at)
    assert programs.stop_manager(timeout=5.0, client=Client(C.TIMEOUT)) == ""      # it stopped all the same
    monkeypatch.setattr(programs, "manager_running", lambda: True)
    assert "didn't answer" in programs.stop_manager(timeout=0.5, client=Client(C.TIMEOUT))
    assert "refused" in programs.stop_manager(timeout=5.0, client=Client(C.REJECTED))     # (at once)


PROBE = """
import os, sys, time
sys.path.insert(0, {root!r})
from PyQt6.QtCore import QCoreApplication, QTimer
from src.launcher.instance import SingleInstance
app = QCoreApplication([])
guard = SingleInstance("probe")
print("claimed" if guard.claim(notify=False) else "deferred", flush=True)
mode = sys.argv[1]
if mode == "hang":
    time.sleep(30)
elif mode == "crash":
    os._exit(0)
guard.activated.connect(lambda m: print("heard", m, flush=True))
QTimer.singleShot(int(float(mode) * 1000), app.quit)
app.exec()
"""


def run_probe(mode):
    """Another copy of a program, in a process of its own: it claims the name 'probe', then serves
    (mode: for that many seconds), hangs, or crashes."""
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    proc = subprocess.Popen([sys.executable, "-c", PROBE.format(root=root), mode], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "claimed"
    return proc


def test_one_copy_of_each_program(app):
    from src.launcher import instance
    first = run_probe("6")
    try:
        second = instance.SingleInstance("probe")
        assert not second.claim(notify=False) and second.answer == instance.ANSWERED
    finally:
        out, _ = first.communicate(timeout=20)
    assert "heard activate" in out                      # the first was asked to come forward
    assert instance.ask_running("probe") == instance.NONE           # gone with it
    third = instance.SingleInstance("probe")
    assert third.claim(notify=False)
    third.close()


def test_a_copy_that_doesnt_answer_isnt_doubled(app):
    from src.launcher import instance
    hung = run_probe("hang")
    try:
        assert instance.ask_running("probe", reply_ms=500) == instance.SILENT
        second = instance.SingleInstance("probe")
        assert not second.claim(notify=False) and second.answer == instance.SILENT
    finally:
        hung.kill()
        hung.wait(10)


@pytest.mark.skipif(os.name == "nt", reason="named pipes leave nothing behind")
def test_a_copy_that_crashed_leaves_nothing_in_the_way(app):
    from src.launcher import instance
    run_probe("crash").wait(10)                         # (its socket file is left behind)
    guard = instance.SingleInstance("probe")
    assert guard.claim(notify=False)
    guard.close()


def test_open_or_raise_starts_only_when_none_is_running(app, monkeypatch):
    from src.launcher import instance
    started = []
    monkeypatch.setattr(programs, "open_program", lambda p: started.append(p))
    answers = {"hmi": instance.ANSWERED, "viewer": instance.NONE, "launcher": instance.SILENT}
    monkeypatch.setattr(instance, "ask_running", lambda p, *a, **k: answers[p])
    assert instance.open_or_raise("hmi") == "" and started == []
    assert instance.open_or_raise("viewer") == "" and started == ["viewer"]
    assert "isn't answering" in instance.open_or_raise("launcher") and started == ["viewer"]

    def fail(p):
        raise OSError("no such file")
    monkeypatch.setattr(programs, "open_program", fail)
    assert "no such file" in instance.open_or_raise("viewer")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def marker(tmp_path, monkeypatch):
    path = str(tmp_path / "launcher_state.json")
    monkeypatch.setattr(programs, "stop_marker_path", lambda: path)
    return path


@pytest.fixture
def launcher(app, tmp_path, monkeypatch, marker):
    monkeypatch.setenv("SINDIE_SETTINGS_DIR", str(tmp_path))
    from src.launcher import window as lw
    running = {"on": False}
    started = []
    monkeypatch.setattr(programs, "manager_running", lambda: running["on"])
    monkeypatch.setattr(programs, "start_manager", lambda: started.append(1))
    clock = Clock()
    w = lw.LauncherWindow(telemetry=False, clock=clock)
    w.tray = None                                       # (no tray in the tests; offscreen has none either)
    yield w, running, started, clock
    w._quitting = True
    w.close()
    w.deleteLater()


def wait_for(app, done, seconds=5.0):
    end = time.time() + seconds
    while not done() and time.time() < end:
        app.processEvents()
        time.sleep(0.01)
    return done()


ALL_ONLINE = {f"service_{n}": "ONLINE" for n in ("plc", "logger", "alarms")}


def test_the_services_state(launcher):
    w, running, started, clock = launcher
    w.refresh()
    assert w.services_chip.text() == "NOT RUNNING" and w.btn_start.isEnabled() and not w.btn_stop.isEnabled()
    running["on"] = True
    w.refresh()
    assert w.services_chip.text() == "STARTING"
    w.cache.ingest("MANAGER", {"manager.services": dict(ALL_ONLINE)})
    w.refresh()
    assert w.services_chip.text() == "3 OF 3 ONLINE" and w.btn_stop.isEnabled() and not w.btn_start.isEnabled()
    w.cache.ingest("MANAGER", {"manager.services": dict(ALL_ONLINE, service_smu="BACKOFF")})
    w.refresh()
    assert w.services_chip.text() == "3 OF 4 ONLINE" and w.services_text.text() == "Waiting for: smu (BACKOFF)."


def test_keep_running_starts_it_again_with_growing_waits(launcher, marker):
    w, running, started, clock = launcher
    w.chk_keep.setChecked(False)
    w.refresh()
    clock.t += 60
    w.refresh()
    assert started == []                                # not asked to keep it running
    w.chk_keep.setChecked(True)                         # ticked: started at once
    assert len(started) == 1 and w.services_chip.text() == "STARTING"
    clock.t += 29
    w.refresh()
    assert len(started) == 1                            # just started: not started again for 30 s
    clock.t += 2
    w.refresh()                                         # (it never came up: down from now)
    clock.t += 4
    w.refresh()
    assert len(started) == 1                            # the first restart waits 5 s ...
    clock.t += 2
    w.refresh()
    assert len(started) == 2
    clock.t += 31
    w.refresh()
    clock.t += 29
    w.refresh()
    assert len(started) == 2                            # ... the next 30 s
    clock.t += 2
    w.refresh()
    assert len(started) == 3
    running["on"] = True
    w.refresh()
    programs.record_services_stopped(True)              # Stop services (here or in another launcher)
    running["on"] = False
    clock.t += 500
    w.refresh()
    assert len(started) == 3 and w.services_chip.text() == "STOPPED"
    assert "Stopped on purpose" in w.services_text.text()
    w.chk_keep.setChecked(False)
    w.chk_keep.setChecked(True)                         # ticking doesn't undo a deliberate stop
    assert len(started) == 3
    w.start_services()                                  # Start services: started, and kept running again
    assert len(started) == 4 and programs.services_stopped() is None


def test_a_stop_is_honoured_by_the_next_launcher(app, launcher, marker, monkeypatch):
    from src.launcher import window as lw
    w, running, started, clock = launcher
    programs.record_services_stopped(True)
    again = lw.LauncherWindow(keep_running=True, telemetry=False, clock=clock)  # (log-on: --tray --keep-running)
    try:
        again.tray = None
        clock.t += 600
        again.refresh()
        assert started == [] and again.services_chip.text() == "STOPPED"
    finally:
        again._quitting = True
        again.close()
        again.deleteLater()
    programs.record_services_stopped(False)
    fresh = lw.LauncherWindow(keep_running=True, telemetry=False, clock=clock)   # not stopped: started at once
    try:
        assert started == [1]
    finally:
        fresh._quitting = True
        fresh.close()
        fresh.deleteLater()
    programs.record_services_stopped(True)              # ... and seen running after all: no longer stopped
    running["on"] = True
    w.refresh()
    assert programs.services_stopped() is None


def test_slow_calls_report_back_on_the_window_thread(app, launcher):
    w, running, started, clock = launcher
    seen = []
    w._background(lambda: threading.current_thread() is not threading.main_thread(),
                  lambda result, error: seen.append((result, error, threading.current_thread() is threading.main_thread())))
    assert wait_for(app, lambda: seen) and seen == [(True, None, True)]
    w._background(lambda: 1 / 0, lambda result, error: seen.append(type(error)))
    assert wait_for(app, lambda: len(seen) == 2) and seen[1] is ZeroDivisionError and not w._jobs


def test_the_timer_never_opens_a_dialog(launcher, monkeypatch):
    from src.launcher import window as lw
    w, running, started, clock = launcher
    dialogs = []
    monkeypatch.setattr(lw.QMessageBox, "warning", lambda *a, **k: dialogs.append(a))

    def fail():
        raise OSError("no Python")
    monkeypatch.setattr(programs, "start_manager", fail)
    w.chk_keep.setChecked(True)
    for _ in range(5):
        clock.t += 60
        w.refresh()
    assert dialogs == []                                # (logged, and told in the tray)
    w.start_services()
    assert len(dialogs) == 1                            # asked for: told at once


def test_stop_asks_for_stop_to_be_typed(app, launcher, monkeypatch):
    from src.launcher import window as lw
    w, running, started, clock = launcher
    dlg = lw.StopDialog({"ion_beam.facilities.safety_relay_active": 1.0,
                         "ion_beam.source.chamber.stat_gv_close_permissive": 0.0})
    assert not dlg.btn_stop.isEnabled()
    dlg.edit.setText("stop")
    assert not dlg.btn_stop.isEnabled()
    dlg.edit.setText("STOP")
    assert dlg.btn_stop.isEnabled()
    texts = " ".join(lbl.text() for lbl in dlg.findChildren(lw.QLabel))
    assert "Shut the source down first" in texts and "PC_Watchdog_Timeout" in texts
    dlg.deleteLater()
    warning = lw.StopDialog.warning
    assert warning({lw.RELAY: 1.0, lw.SOURCE_COLD: 1.0}) == "The safety relay is on. Switch it off first (System " \
                                                            "Health panel)."
    assert warning({lw.RELAY: 0.0, lw.SOURCE_COLD: 0.0}).startswith("The safety relay is off, but the source isn't")
    assert warning({lw.RELAY: 0.0, lw.SOURCE_COLD: 1.0}) == ""
    assert "isn't known" in warning({})
    running["on"] = True
    asked = []
    monkeypatch.setattr(lw.StopDialog, "exec", lambda self: lw.QDialog.DialogCode.Rejected)
    monkeypatch.setattr(programs, "stop_manager", lambda: asked.append(1) or "")
    w.stop_services()
    assert asked == [] and programs.services_stopped() is None      # cancelled: nothing sent
    monkeypatch.setattr(lw.StopDialog, "exec", lambda self: lw.QDialog.DialogCode.Accepted)
    gate = threading.Event()
    monkeypatch.setattr(programs, "stop_manager", lambda: gate.wait(5) and (asked.append(1) or ""))
    w.stop_services()                                   # (in the background: the window keeps answering)
    assert w.services_chip.text() == "STOPPING..." and not w.btn_stop.isEnabled()
    assert programs.services_stopped() is not None
    running["on"] = False
    gate.set()
    assert wait_for(app, lambda: not w._stopping)
    assert asked == [1] and w.services_chip.text() == "STOPPED"


def test_the_hmi_and_viewer_open_each_other(app, tmp_path, monkeypatch):
    monkeypatch.setenv("SINDIE_SETTINGS_DIR", str(tmp_path))
    from src.launcher import instance
    opened = []
    monkeypatch.setattr(instance, "open_or_raise", lambda p: opened.append(p) or "")
    from src.gui.control.control_main import ControlMainWindow
    hmi = ControlMainWindow()
    try:
        hmi.watchdog_timer.stop()
        hmi.open_viewer_action.trigger()
        assert opened == ["viewer"]
    finally:
        hmi.close()
        hmi.deleteLater()
        app.processEvents()
