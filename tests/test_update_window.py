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
    running = {"on": True, "other": False}
    started = []
    monkeypatch.setattr(programs, "manager_running", lambda: running["on"])
    monkeypatch.setattr(programs, "other_manager_running", lambda: running["other"])
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
    w, running, started, clock, open_programs, told = launcher
    u = w.updates
    u.check_fn = plan
    gate, steps = threading.Event(), []

    def fake_apply(p, hooks, step, user):
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
    gate.set()
    assert wait_for(app, lambda: d.state == "done")
    assert not u.busy and steps == ["b" * 40] and d.heading.text() == "Updated to bbbbbbb."
    assert d.btn_restart.isVisibleTo(d) and "starts again" in d.result.text()
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
    u.apply_fn = lambda p, hooks, step, user: updater.Report(
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
    assert "stopped for a minute" in " ".join(u.reasons(False, False))
    clock.t += 61
    assert u.reasons(False, False) == []
    running["other"] = True                                 # (the simulator's services)
    assert "close the simulator" in u.reasons(False, True)[0]


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
    assert hooks.stop_services() == "" and stops == [1] and programs.services_stopped()
    running["on"] = True
    assert hooks.start_services() == "" and started == [1] and programs.services_stopped() is None


def test_healthy_means_every_service_back_and_the_plc_answering(launcher):
    w, *_ = launcher
    u = w.updates
    since = time.time() - 1
    assert u.health_problem({"service_plc"}, since) == "the service manager hasn't reported"
    w.cache.ingest("MANAGER", {"manager.services": {"service_plc": "BACKOFF", "service_logger": "ONLINE"}})
    assert u.health_problem({"service_plc", "service_gone"}, since) == "not ONLINE: plc (BACKOFF)"
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
    monkeypatch.setattr(programs, "spawn", lambda args, log_name: spawned.append(args) or 4242)
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

    def fail(args, log_name):
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
