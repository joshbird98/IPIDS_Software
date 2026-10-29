"""The launcher's Software box and update window (src/launcher/update_window.py): the daily check
that only checks, the conditions an update waits for, the update itself (with stand-ins for the
work: src/launcher/updater.py has its own tests), and the launcher starting again afterwards."""
import threading
import time

import pytest
from PyQt6.QtWidgets import QApplication

from src.launcher import programs, updater

NOW = time.time()
SAFE = {"system.connected": 1.0, updater.RELAY: 0.0, updater.SOURCE_COLD: 1.0}
ONLINE = {"service_plc": "ONLINE", "service_logger": "ONLINE"}


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def wait_for(app, done, seconds=5.0):
    end = time.time() + seconds
    while not done() and time.time() < end:
        app.processEvents()
        time.sleep(0.01)
    return done()


def plan(state=updater.READY, **kw) -> updater.Plan:
    commits = [updater.Commit("b" * 40, "bbbbbbb", "2026-10-29T09:12:00+00:00", "Josh", "VG1: retry at once",
                              "A lost read is made again.")]
    kw.setdefault("commits", commits if state in (updater.READY, updater.BLOCKED) else [])
    for key, value in (("current", "a" * 40), ("target", "b" * 40), ("current_date", "2026-10-28T16:00:00+00:00"),
                       ("target_date", "2026-10-29T09:12:00+00:00"), ("tests", updater.PASSED)):
        kw.setdefault(key, value)
    return updater.Plan(root="/sindie", checked_at=NOW, state=state, **kw)


@pytest.fixture
def launcher(app, tmp_path, monkeypatch):
    monkeypatch.setenv("SINDIE_SETTINGS_DIR", str(tmp_path))
    monkeypatch.setattr(updater, "updates_dir", lambda: str(tmp_path / "updates"))
    monkeypatch.setattr(programs, "stop_marker_path", lambda: str(tmp_path / "launcher_state.json"))
    monkeypatch.setattr(updater, "version", lambda *a, **k: {"sha": "a" * 40, "short": "aaaaaaa",
                                                             "date": "2026-10-28T16:00:00+00:00", "branch": "lab"})
    running = {"on": True, "other": False, "procs": {}}
    started = []
    monkeypatch.setattr(programs, "manager_running", lambda: running["on"])
    monkeypatch.setattr(programs, "other_manager_running", lambda: running["other"])
    monkeypatch.setattr(programs, "sindie_processes", lambda: dict(running["procs"]))
    monkeypatch.setattr(programs, "start_manager", lambda: started.append(1))
    from src.launcher import instance
    open_programs = set()
    monkeypatch.setattr(instance, "is_running", lambda name: any(p in name for p in open_programs))
    from src.launcher import window as lw
    clock = Clock()
    w = lw.LauncherWindow(telemetry=False, clock=clock, auto_check=False)
    w.tray = None
    told = []
    monkeypatch.setattr(w.updates, "notify", told.append)
    assert wait_for(app, lambda: w.updates.version)
    yield w, running, started, clock, open_programs, told
    w.updates.busy = False
    w._quitting = True
    w.close()
    w.deleteLater()


def report_on(w):
    w.cache.ingest("MANAGER", {"manager.services": dict(ONLINE)})
    w.cache.ingest("PLC_DATA", dict(SAFE))


# --------------------------------------------------------------------------- the box
def test_the_software_box_says_where_things_stand(launcher):
    w, *_ = launcher
    u = w.updates
    assert u.chip.text() == "NOT CHECKED" and not u.btn_update.isEnabled() and u.btn_check.isEnabled()
    assert u.version_text.text() == "Version aaaaaaa of 28 Oct 2026, on the lab branch."
    assert not u.btn_back.isVisibleTo(w)
    for state, chip, words in ((updater.UP_TO_DATE, "UP TO DATE", "Up to date (checked at "),
                               (updater.READY, "UPDATE AVAILABLE", "An update is available: 1 change"),
                               (updater.BLOCKED, "UPDATE AVAILABLE", "can't be applied from here"),
                               (updater.CANT_CHECK, "CAN'T CHECK", "Couldn't check for updates")):
        u.plan = plan(state, error="GitHub couldn't be reached")
        u.update_pane()
        assert u.chip.text() == chip and words in u.status_text.text()
        assert u.btn_update.isEnabled() == (state in (updater.READY, updater.BLOCKED))
    u.busy = True
    u.update_pane()
    w.refresh()
    assert u.chip.text() == "UPDATING..." and not u.btn_update.isEnabled() and not u.btn_check.isEnabled()
    assert w.services_chip.text() == "UPDATING..." and not w.btn_stop.isEnabled()
    u.busy = False


def test_a_check_for_another_version_is_forgotten(app, launcher):
    w, *_ = launcher
    updater.remember_check(plan(current="c" * 40))
    u = w.updates
    u.plan = updater.last_plan()
    u._version_known({"sha": "a" * 40, "short": "aaaaaaa", "date": "", "branch": "lab"}, None)
    assert u.plan is None and u.chip.text() == "NOT CHECKED"


def test_the_daily_check_only_checks_and_tells_once(app, launcher):
    w, running, started, clock, open_programs, told = launcher
    u = w.updates
    checks = []
    u.check_fn = lambda: checks.append(1) or plan()
    u.daily()
    assert wait_for(app, lambda: not u.checking) and checks == [1]
    assert u.chip.text() == "UPDATE AVAILABLE" and len(told) == 1 and "update when it suits" in told[0]
    assert updater.last_plan().target == "b" * 40
    u.daily()                                           # (checked less than a day ago)
    assert checks == [1]
    state = updater.load_state()
    state["last_check"] = NOW - updater.CHECK_EVERY_S - 1
    updater.save_state(state)
    u.daily()
    assert wait_for(app, lambda: not u.checking) and checks == [1, 1]
    assert len(told) == 1                               # (told once about each version)
    assert w.services_chip.text() != "UPDATING..." and started == []     # nothing applied


def test_a_check_that_fails_says_so(app, launcher):
    w, *_ = launcher
    u = w.updates

    def broken():
        raise OSError("disk full")
    u.check_fn = broken
    u.check_now()
    assert wait_for(app, lambda: not u.checking)
    assert u.chip.text() == "CAN'T CHECK" and "OSError: disk full" in u.status_text.text()


# --------------------------------------------------------------------------- the update window
def test_an_update_waits_for_a_safe_machine_and_a_typed_word(app, launcher, monkeypatch):
    from src.launcher import instance
    from src.launcher import window as lw
    w, running, started, clock, open_programs, told = launcher
    u = w.updates
    u.check_fn = plan
    gate, steps = threading.Event(), []

    def fake_apply(p, hooks, step, user, **kw):
        steps.append(p.target)
        step("Stopping the services")
        gate.wait(5)
        return updater.Report(ok=True, message="Updated to bbbbbbb.", advice="Press RESET FAULTS in System Health.",
                              now=p.target)
    u.apply_fn = fake_apply
    restarted = []
    monkeypatch.setattr(u, "restart_launcher", lambda: restarted.append(1))
    u.open_update()
    d = u.dialog
    assert d.state == "checking" and "Checking GitHub" in d.heading.text()
    assert wait_for(app, lambda: d.state == "review")
    assert d.heading.text() == "Update SINDIE to bbbbbbb?" and "VG1: retry at once" in d.changes.toPlainText()
    w.cache.ingest("PLC_DATA", {"system.connected": 1.0, updater.RELAY: 1.0, updater.SOURCE_COLD: 0.0})
    w.refresh()
    assert "safety relay is on" in d.conditions.text() and "source isn't cold" in d.conditions.text()
    d.edit.setText("UPDATE")
    assert not d.btn_go.isEnabled()
    report_on(w)
    open_programs.add("sindie-hmi-live")
    w.refresh()
    assert "Operator HMI is open" in d.conditions.text() and not d.btn_go.isEnabled()
    open_programs.clear()
    w.refresh()
    assert d.conditions.text().startswith("Ready: the PLC reports the safety relay off") and d.btn_go.isEnabled()
    d.edit.setText("update")
    assert not d.btn_go.isEnabled()
    d.edit.setText("UPDATE")
    d.btn_go.click()
    assert u.busy and d.state == "applying" and w.services_chip.text() == "UPDATING..."
    assert wait_for(app, lambda: "Stopping the services" in d.progress.text())
    d.reject()                                          # (it can't be closed while it runs)
    assert d.isVisible() and u.dialog is d
    running["on"] = False
    w.chk_keep.setChecked(True)
    clock.t += 600
    w.refresh()
    assert started == []                                # (keep running leaves the update alone)
    shown, opened = [], []
    monkeypatch.setattr(lw.QMessageBox, "information", lambda *a: shown.append(a[2]))
    monkeypatch.setattr(instance, "open_or_raise", lambda p: opened.append(p) or "")
    w.open_program("hmi")
    assert opened == [] and "being updated" in shown[0]     # (nothing is started meanwhile)
    gate.set()
    assert wait_for(app, lambda: d.state == "done")
    assert not u.busy and steps == ["b" * 40] and d.heading.text() == "Updated to bbbbbbb."
    assert d.btn_restart.isVisibleTo(d) and "starts again" in d.result.text()
    assert u.chip.text() == "UP TO DATE" and updater.last_plan().current == "b" * 40     # (what the next launcher shows)
    d.btn_restart.click()
    assert wait_for(app, lambda: restarted == [1]) and u.dialog is None


def test_a_blocked_update_shows_why_and_cannot_start(app, launcher):
    w, *_ = launcher
    u = w.updates
    u.check_fn = lambda: plan(updater.BLOCKED, blockers=["It changes the PC-PLC data layout (plc/generated/x)."])
    u.open_update()
    d = u.dialog
    assert wait_for(app, lambda: d.state == "review")
    assert d.blockers.isVisibleTo(d) and "PC-PLC data layout" in d.blockers.text()
    assert not d.edit.isVisibleTo(d) and not d.btn_go.isVisibleTo(d)
    d.reject()


def test_up_to_date_or_cant_check_in_the_window(app, launcher):
    w, *_ = launcher
    u = w.updates
    u.check_fn = lambda: plan(updater.UP_TO_DATE)
    u.open_update()
    assert wait_for(app, lambda: u.dialog.state == "done")
    assert "nothing to update" in u.dialog.heading.text() and u.dialog.btn_close.isVisibleTo(u.dialog)
    u.dialog.reject()
    u.check_fn = lambda: plan(updater.CANT_CHECK, error="GitHub couldn't be reached (timed out)")
    u.open_update()
    assert wait_for(app, lambda: u.dialog is not None and u.dialog.state == "done")
    assert u.dialog.versions.text() == "GitHub couldn't be reached (timed out)."


def test_a_failed_update_says_what_happened(app, launcher, monkeypatch):
    w, *_ = launcher
    u = w.updates
    report_on(w)
    restarted = []
    monkeypatch.setattr(u, "restart_launcher", lambda: restarted.append(1))
    u.check_fn = plan
    u.apply_fn = lambda p, hooks, step, user, **kw: updater.Report(
        message="it didn't pass its own check: x", rolled_back=True, advice="Press RESET FAULTS.", folder="/f")
    u.open_update()
    d = u.dialog
    assert wait_for(app, lambda: d.state == "review")
    w.refresh()
    d.edit.setText("UPDATE")
    d.btn_go.click()
    assert wait_for(app, lambda: d.state == "done")
    assert d.heading.text() == "The update didn't work"
    assert "It didn't pass its own check: x." in d.result.text() and "The version before is back." in d.result.text()
    d.btn_close.click()
    app.processEvents()
    assert restarted == []


def test_with_the_services_stopped_it_waits_a_minute(app, launcher):
    w, running, started, clock, *_ = launcher
    u = w.updates
    running["on"] = False
    w.refresh()
    assert "stopped for a minute" in " ".join(u.reasons(False))
    clock.t += 61
    assert u.reasons(False) == []
    running["other"] = True                                 # (the simulator's services)
    assert "Other SINDIE services are running on this PC (the simulator)" in u.reasons(False)[0]


def test_going_back_is_offered_after_an_update(app, launcher):
    w, *_ = launcher
    u = w.updates
    updater._history({"at": NOW, "kind": updater.UPDATE, "from": "9" * 40, "to": "a" * 40, "ok": True})
    u.update_pane()
    assert u.btn_back.isVisibleTo(w) and u.btn_back.isEnabled()
    u.back_fn = lambda: plan(kind=updater.BACK, target="9" * 40)
    u.open_back()
    assert wait_for(app, lambda: u.dialog.state == "review")
    assert u.dialog.heading.text() == "Go back to 9999999?" and u.dialog.word == "BACK"
    u.dialog.reject()


# --------------------------------------------------------------------------- what the update asks of the launcher
def test_the_services_are_stopped_on_purpose_during_an_update(launcher, monkeypatch):
    w, running, started, *_ = launcher
    hooks = w.updates.hooks()
    stops = []
    monkeypatch.setattr(programs, "stop_manager", lambda: stops.append(1) or "")
    assert hooks.stop_services(False) == "" and stops == [1] and programs.services_stopped()
    running["on"] = True
    assert hooks.start_services() == "" and started == [1] and programs.services_stopped() is None


def test_a_new_version_that_doesnt_answer_is_ended_only_when_going_back(launcher, monkeypatch):
    w, running, *_ = launcher
    hooks = w.updates.hooks()
    ended = []
    monkeypatch.setattr(programs, "stop_manager", lambda: "The service manager didn't answer")
    monkeypatch.setattr(programs, "end_manager_processes", lambda: ended.append(1) or running.update(on=False) or 3)
    assert hooks.stop_services(False) == "The service manager didn't answer" and ended == []    # (the update stops)
    assert hooks.stop_services(True) == "" and ended == [1]                                     # (going back)


def test_healthy_means_every_service_back_and_the_plc_answering(launcher):
    w, *_ = launcher
    u = w.updates
    since = time.time() - 1
    assert u.health_problem({"service_plc"}, since) == "the service manager hasn't reported"
    w.cache.ingest("MANAGER", {"manager.services": {"service_plc": "BACKOFF", "service_logger": "ONLINE"}})
    assert u.health_problem({"service_plc"}, since) == "not ONLINE: plc (BACKOFF)"
    assert u.health_problem({"service_plc", "service_gone"}, since) == "not run by the service manager now: gone"
    w.cache.ingest("MANAGER", {"manager.services": dict(ONLINE)})
    assert u.health_problem({"service_plc"}, since) == "the PLC isn't answering"
    w.cache.ingest("PLC_DATA", dict(SAFE))
    assert u.health_problem({"service_plc"}, since) == ""
    assert u.online() == set(ONLINE)
    assert u.wait_healthy(0.0, {"service_plc"}) == "the service manager hasn't reported"     # (nothing since)


# --------------------------------------------------------------------------- the launcher again
def test_the_launcher_starts_again_and_hands_over(app, launcher, monkeypatch):
    w, running, started, clock, *_ = launcher
    from src.launcher import instance
    spawned, quit_, guard = [], [], _Guard()
    w.guard = guard
    monkeypatch.setattr(programs, "spawn", lambda args, log_name, **kw: spawned.append(args) or 4242)
    monkeypatch.setattr(w, "quit", lambda: quit_.append(1))
    monkeypatch.setattr(instance, "is_running", lambda name: name == programs.instance_name("launcher"))
    w.chk_keep.setChecked(True)
    w.updates.restart_launcher()
    assert guard.calls == ["close"] and spawned[0][-2:] == ["--keep-running", "--after-update"]
    assert wait_for(app, lambda: quit_ == [1])


def test_a_launcher_that_doesnt_start_again_leaves_this_one(app, launcher, monkeypatch):
    w, *_ = launcher
    from src.launcher import update_window as uw
    warned, guard = [], _Guard()
    w.guard = guard
    monkeypatch.setattr(uw.QMessageBox, "warning", lambda *a: warned.append(a[2]))

    def fail(args, log_name, **kw):
        raise OSError("no Python")
    monkeypatch.setattr(programs, "spawn", fail)
    w.updates.restart_launcher()
    assert guard.calls == ["close", "claim"] and "no Python" in warned[0] and "runs the version before" in warned[0]


class _Guard:
    def __init__(self):
        self.calls = []

    def close(self):
        self.calls.append("close")

    def claim(self, notify=True):
        self.calls.append("claim")
        return True


def test_after_an_update_and_after_one_that_never_finished(app, launcher):
    w, *_ = launcher
    u = w.updates
    updater._history({"at": NOW, "kind": updater.UPDATE, "from": "9" * 40, "to": "a" * 40, "ok": True,
                      "message": "Updated to aaaaaaa.", "advice": "Press RESET FAULTS in System Health."})
    u.startup(after_update=True)
    assert u._box.text() == "Updated to aaaaaaa.\n\nPress RESET FAULTS in System Health."
    u._box.close()
    folder = updater.updates_dir() + "/20261029-101500"
    import os
    os.makedirs(folder)
    with open(folder + "/update.json", "w") as f:
        f.write('{"started": "20261029-101500"}')
    u.startup()
    assert "never finished" in u._box.text() and folder in u._box.text()
    u._box.close()
    assert updater.unfinished() is None                     # (said once)


# --------------------------------------------------------------------------- what could go wrong (reviewed 29 October 2026)
def test_the_services_stay_stopped_on_purpose_while_the_manager_stops(app, launcher):
    """The update's stop marks the services stopped on purpose; the launcher's own refresh, seeing the
    manager still shutting down, used to take that back - and keep running then started them on a half
    put back version."""
    w, running, started, clock, *_ = launcher
    w.updates.busy = True
    programs.record_services_stopped(True)              # (the update's stop hook)
    w.refresh()                                         # (the manager is still going down)
    assert programs.services_stopped()
    running["on"] = False
    w.updates.busy = False                              # (the update ended: it couldn't put everything back)
    w.chk_keep.setChecked(True)
    clock.t += 600
    w.refresh()
    assert started == [] and w.services_chip.text() == "STOPPED"


def test_an_update_that_didnt_finish_holds_the_services(app, launcher, monkeypatch):
    import json
    import os
    from src.launcher import window as lw
    w, running, started, clock, *_ = launcher
    folder = os.path.join(updater.updates_dir(), "20261029-101500")
    os.makedirs(folder)
    with open(os.path.join(folder, "update.json"), "w") as f:
        json.dump({"started": "20261029-101500", "failed": "it didn't pass its own check", "rolled_back": False}, f)
    running["on"] = False
    w.chk_keep.setChecked(True)
    clock.t += 600
    w.refresh()
    assert started == [] and w.services_chip.text() == "HELD" and "couldn't be put back" in w.services_text.text()
    again = lw.LauncherWindow(keep_running=True, telemetry=False, clock=clock, auto_check=False)   # (log-on)
    try:
        assert started == []
    finally:
        again._quitting = True
        again.close()
        again.deleteLater()
    answers = [lw.QMessageBox.StandardButton.No, lw.QMessageBox.StandardButton.Yes]
    asked = []
    monkeypatch.setattr(lw.QMessageBox, "warning", lambda *a, **k: asked.append(a[2]) or answers.pop(0))
    w.start_services()                                  # (asked; no)
    assert started == [] and "Has Josh checked the software?" in asked[0]
    w.start_services()                                  # (asked; yes: started, and held no more)
    assert started == [1] and updater.held_up() == ""


def test_the_launcher_doesnt_close_during_an_update(app, launcher, monkeypatch):
    from src.launcher import window as lw
    w, *_ = launcher
    shown, quits = [], []
    monkeypatch.setattr(lw.QMessageBox, "information", lambda *a: shown.append(a[2]))
    monkeypatch.setattr(lw.QApplication.instance(), "quit", lambda: quits.append(1))
    w.show()
    w.updates.busy = True
    w.quit()                                            # (the tray's Close the launcher)
    assert quits == [] and "can't close until the update has finished" in shown[0]
    w._quitting = True
    w.close()
    assert w.isVisible() and not w._quitting
    w.updates.busy = False


def test_services_running_without_the_service_manager(app, launcher):
    """A service manager ended by force leaves its services running, the PLC's watchdog going: no
    waiting a minute makes that safe."""
    w, running, started, clock, *_ = launcher
    u = w.updates
    running["on"] = False
    clock.t += 600
    running["procs"] = {programs.this_system(): ["service_logger", "service_plc"]}
    u.look_around(now=True)
    assert "without the service manager (service_logger, service_plc)" in " ".join(u.reasons(False))
    running["procs"] = {}
    u.look_around(now=True)
    w.cache.ingest("PLC_DATA", dict(SAFE))
    assert "PLC's data is still arriving" in " ".join(u.reasons(False))


def test_other_systems_on_this_pc_are_named(app, launcher):
    w, running, *_ = launcher
    u = w.updates
    report_on(w)
    running["procs"] = {(20000, True): ["service_manager", "service_plc"]}
    u.look_around(now=True)
    assert "Other SINDIE services are running on this PC (a simulator on port offset 20000)" in " ".join(u.reasons(True))


def test_the_conditions_are_checked_again_at_the_click(app, launcher):
    w, running, started, clock, open_programs, told = launcher
    u = w.updates
    report_on(w)
    u.check_fn = plan
    applied = []
    u.apply_fn = lambda *a, **k: applied.append(1) or updater.Report(ok=True)
    u.open_update()
    d = u.dialog
    assert wait_for(app, lambda: d.state == "review")
    w.refresh()
    d.edit.setText("UPDATE")
    assert d.btn_go.isEnabled()
    open_programs.add("sindie-viewer-live")             # (opened since the last look)
    d.btn_go.click()
    assert d.state == "review" and not u.busy and applied == []
    assert "data viewer is open" in d.conditions.text() and not d.btn_go.isEnabled()
    d.reject()


def test_no_daily_check_while_the_update_window_is_open(app, launcher):
    w, *_ = launcher
    u = w.updates
    checks = []
    u.back_fn = lambda: plan(kind=updater.BACK)
    u.open_back()
    u.check_fn = lambda: checks.append(1) or plan()
    u.daily()
    assert not u.checking and checks == []
    u.dialog.reject()
    app.processEvents()
    u.daily()
    assert wait_for(app, lambda: not u.checking) and checks == [1]


def test_which_services_are_online_isnt_known_from_a_quiet_manager(launcher):
    w, *_ = launcher
    u = w.updates
    assert u.online() is None
    w.cache.ingest("MANAGER", {"manager.services": dict(ONLINE)})
    assert u.online() == set(ONLINE)


def test_the_launcher_started_again_builds_its_own_simulator_registry(app, launcher, monkeypatch):
    w, *_ = launcher
    envs = []
    monkeypatch.setattr(programs, "spawn", lambda args, log_name, **kw: envs.append(kw.get("env")) or 1)
    monkeypatch.setattr(programs, "_REGISTRY_SET_HERE", [True])
    monkeypatch.setenv("SINDIE_REGISTRY", "/sindie/data_sim/system_tags.json")
    w.updates.restart_launcher()
    assert "SINDIE_REGISTRY" not in envs[0]
    w.updates._restart.stop()


def test_the_simulators_registry_is_built_by_the_new_version(launcher, monkeypatch):
    w, running, started, *_ = launcher
    built = []
    monkeypatch.setattr(programs, "simulation", lambda: True)
    monkeypatch.setattr(programs, "rebuild_simulator_registry", lambda python=None: built.append(python) or "")
    hooks = w.updates.hooks()
    running["on"] = True
    assert hooks.start_services() == "" and len(built) == 1 and started == [1]
    monkeypatch.setattr(programs, "rebuild_simulator_registry", lambda python=None: "exit code 1: bad config")
    assert "simulator's tag registry couldn't be built: exit code 1" in hooks.start_services() and started == [1]


def test_timers_stop_when_the_launcher_closes(app, tmp_path, monkeypatch):
    monkeypatch.setenv("SINDIE_SETTINGS_DIR", str(tmp_path))
    monkeypatch.setattr(updater, "updates_dir", lambda: str(tmp_path / "updates"))
    monkeypatch.setattr(programs, "stop_marker_path", lambda: str(tmp_path / "launcher_state.json"))
    monkeypatch.setattr(programs, "manager_running", lambda: True)
    monkeypatch.setattr(updater, "version", lambda *a, **k: {})
    from src.launcher import window as lw
    w = lw.LauncherWindow(telemetry=False)
    w.tray = None
    assert w.timer.isActive() and all(t.isActive() for t in w.updates.timers) and len(w.updates.timers) == 2
    w._quitting = True
    w.close()
    assert not w.timer.isActive() and not any(t.isActive() for t in w.updates.timers)
    w.deleteLater()
