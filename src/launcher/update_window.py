"""The launcher's Software box and its update window: the version this PC runs, a check for updates
(by itself once a day; it never updates by itself), and the update - or going back to the version
before it - when the operator chooses and the machine is safe. The work is done by
src/launcher/updater.py; this is the window's side of it.

After an update the launcher starts again (with --after-update), so that it runs the new version
too: the new one takes over the launcher's name (src/launcher/instance.py), then this one closes.
"""
import datetime
import html
import logging
import time

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
                             QSystemTrayIcon, QTextBrowser, QVBoxLayout)

from src.core import paths
from src.gui.style import chip, set_role, set_tone
from src.launcher import programs, updater

log = logging.getLogger("launcher")

LOOK_EVERY_MS = 10 * 60 * 1000      # how often the launcher looks whether a day has passed since the last check
FIRST_LOOK_MS = 60 * 1000           # ... first a minute after it opens
SAFE_WORDS = {updater.UPDATE: "UPDATE", updater.BACK: "BACK"}
START_WAIT_S = 30.0                 # the service manager started: holding its lock within this
RESTART_WAIT_S = 30.0               # the new launcher: holding the launcher's name within this
LOOK_AROUND_S = 3.0                 # the update window open: SINDIE's processes on this PC looked at this often
PROGRAMS = (("hmi", "Operator HMI"), ("viewer", "data viewer"))

WHAT_UPDATE = (
    "When you press Update now, the services stop (the PLC then latches PC_Watchdog_Timeout and keeps the gate "
    "valve shut) and this PC's own settings are kept. The new version is tested: where it reads the PLC, and its "
    "own check. Then the services start again, and every one has to come back. It takes a few minutes. If "
    "anything goes wrong, the version before comes back by itself. Afterwards, press RESET FAULTS in System "
    "Health and open the HMI again.")
WHAT_BACK = (
    "When you press Go back now, the same happens as in an update, to the version this PC ran before the last "
    "update. This PC's own settings stay as they are.")


def when(t: float, now: float = None) -> str:
    """'at 07:41 today', 'on 28 Oct at 07:41'."""
    if not t:
        return "never"
    lt, today = time.localtime(t), time.localtime(time.time() if now is None else now)
    hm = time.strftime("%H:%M", lt)
    return f"at {hm} today" if lt[:3] == today[:3] else f"on {lt.tm_mday} {time.strftime('%b', lt)} at {hm}"


def day(iso: str) -> str:
    """'29 Oct 2026' from git's ISO 8601 date ('' if there is none)."""
    try:
        d = datetime.datetime.fromisoformat((iso or "").strip())
    except ValueError:
        return ""
    return f"{d.day} {d.strftime('%b %Y')}"


def changes_html(commits) -> str:
    """The changelog: each change's date and title, and what its message says."""
    out = []
    for c in commits:
        body = "</p><p>".join(html.escape(" ".join(p.split())) for p in c.body.split("\n\n") if p.strip())
        out.append(f"<p><b>{html.escape(day(c.date))}</b>&nbsp; <b>{html.escape(c.subject)}</b> ({c.short})</p>"
                   + (f"<blockquote><p>{body}</p></blockquote>" if body else ""))
    return "".join(out) or "<p>(none)</p>"


def bullets(lines) -> str:
    return "\n".join(f"• {line}" for line in lines)


class UpdateController(QObject):
    """The Software box of the launcher's window (src/launcher/window.py), the daily check and the
    update window."""
    stepped = pyqtSignal(str)               # a step of the update (from the update's thread)

    def __init__(self, window, auto_check: bool = True, clock=time.monotonic):
        super().__init__(window)
        self.window, self.clock = window, clock
        self.check_fn, self.back_fn, self.apply_fn = updater.check, updater.plan_back, updater.apply
        self.version = {}
        self.plan = updater.last_plan()
        self.checking = self.busy = False
        self.dialog = None
        self.opened = clock()
        self.seen = {"this": None, "other": None}       # when each system's services were last seen running
        self.procs, self._procs_at, self._looking = {}, None, False      # SINDIE's processes on this PC (sindie_processes)
        self._restart = None
        self._restart_until = 0.0
        self.pane = self._build()
        self.stepped.connect(self._stepped)
        self.timers = []
        if auto_check:
            for every, single in ((LOOK_EVERY_MS, False), (FIRST_LOOK_MS, True)):
                timer = QTimer(self)                    # (the controller's own: they go with it)
                timer.setSingleShot(single)
                timer.timeout.connect(self.daily)
                timer.start(every)
                self.timers.append(timer)
        self.update_pane()
        self.window._background(updater.version, self._version_known)

    # --- the box -----------------------------------------------------------------------------------
    def _build(self):
        frame, lay = self.window._pane("")
        row = QHBoxLayout()
        head = QLabel("Software")
        set_role(head, "title")
        row.addWidget(head)
        row.addStretch(1)
        self.chip = chip("NOT CHECKED", "off")
        row.addWidget(self.chip)
        lay.addLayout(row)
        self.version_text = QLabel()
        self.status_text = QLabel()
        for label in (self.version_text, self.status_text):
            label.setWordWrap(True)
            set_role(label, "caption")
            lay.addWidget(label)
        self.version_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        buttons = QHBoxLayout()
        self.btn_check = QPushButton("Check for updates")
        self.btn_check.clicked.connect(lambda: self.check_now())
        self.btn_update = QPushButton("Update...")
        self.btn_update.clicked.connect(self.open_update)
        self.btn_back = QPushButton("Go back...")
        self.btn_back.setToolTip("Back to the version this PC ran before the last update")
        self.btn_back.clicked.connect(self.open_back)
        for b in (self.btn_check, self.btn_update, self.btn_back):
            buttons.addWidget(b)
        buttons.addStretch(1)
        lay.addLayout(buttons)
        return frame

    def _version_known(self, version, error):
        self.version = version or {}
        if self.plan is not None and self.plan.current and self.plan.current != self.version.get("sha"):
            self.plan = None                    # (checked for another version: installed by hand since)
        self.update_pane()

    def update_pane(self):
        v, plan = self.version, self.plan
        if v:
            on = ("on the lab branch" if v.get("branch") == updater.BRANCH else
                  f"on the branch {v['branch']}" if v.get("branch") else "not on a branch")
            self.version_text.setText(f"Version {v['short']} of {day(v['date'])}, {on}.")
        else:
            self.version_text.setText("Version: not known yet.")
        n = len(plan.commits) if plan is not None else 0
        changes = f"{n} change{'s' if n != 1 else ''}"
        checked = when(plan.checked_at) if plan is not None else ""
        if self.busy:
            text, tone, sentence = "UPDATING...", "warn", "Updating: see the update window."
        elif self.checking:
            text, tone, sentence = "CHECKING...", "unknown", "Checking GitHub for updates..."
        elif plan is None:
            text, tone, sentence = "NOT CHECKED", "off", "Not checked for updates yet: the launcher checks once a day."
        elif plan.state == updater.CANT_CHECK:
            text, tone, sentence = "CAN'T CHECK", "unknown", f"Couldn't check for updates {checked}: {plan.error}."
        elif plan.state == updater.UP_TO_DATE:
            text, tone, sentence = "UP TO DATE", "ok", f"Up to date (checked {checked})."
        elif plan.state == updater.READY:
            text, tone, sentence = ("UPDATE AVAILABLE", "info", f"An update is available: {changes} (checked {checked}). "
                                    "Update... shows what is in it. It is applied only when you choose.")
        else:
            text, tone, sentence = ("UPDATE AVAILABLE", "warn", f"An update is available ({changes}), but it can't be "
                                    "applied from here: Update... says why.")
        self.chip.setText(text)
        set_tone(self.chip, tone)
        self.status_text.setText(sentence)
        self.btn_check.setEnabled(not self.busy and not self.checking)
        can_update = not self.busy and plan is not None and plan.available
        self.btn_update.setEnabled(can_update)
        set_tone(self.btn_update, "primary" if can_update and plan.state == updater.READY else None)
        last = updater.last_update()
        self.btn_back.setVisible(last is not None)
        self.btn_back.setEnabled(last is not None and not self.busy and last.get("to") == v.get("sha"))

    # --- checking ----------------------------------------------------------------------------------
    def stop_timers(self):
        """The launcher is closing: nothing more from here."""
        for timer in self.timers + ([self._restart] if self._restart is not None else []):
            timer.stop()

    def daily(self):
        """Once a day: check (never apply) - the box and the tray say if there is an update."""
        if self.busy or self.checking or self.dialog is not None:
            return
        last = updater.load_state().get("last_check") or 0
        if time.time() - float(last) >= updater.CHECK_EVERY_S:
            log.info("Checking for software updates (daily)")
            self.check_now(auto=True)

    def check_now(self, auto: bool = False):
        if self.checking or self.busy:
            return
        self.checking = True
        self.update_pane()
        self.window._background(lambda: self.check_fn(), lambda plan, error: self._checked(plan, error, auto))

    def _checked(self, plan, error, auto: bool = False):
        self.checking = False
        if plan is None:
            plan = updater.Plan(root=paths.PROJECT_ROOT, checked_at=time.time(), error=f"{type(error).__name__}: {error}")
        updater.remember_check(plan)
        self.plan = plan
        log.info("Software updates: %s", plan.summary())
        self.update_pane()
        if self.dialog is not None and self.dialog.kind == updater.UPDATE and self.dialog.state == "checking":
            self.dialog.set_plan(plan)
        if auto and plan.available:
            if updater.load_state().get("told") != plan.target:     # (once for each version)
                updater.change_state(told=plan.target)
                self.notify(f"A software update is available ({len(plan.commits)} changes). The launcher's Software "
                            "box shows what is in it: update when it suits.")

    def notify(self, text: str):
        if self.window.tray is not None:
            self.window.tray.showMessage("SINDIE", text, QSystemTrayIcon.MessageIcon.Information, 10000)

    # --- the update window -------------------------------------------------------------------------
    def open_update(self):
        if self.busy:
            return
        self._open(updater.UPDATE)
        if self.checking:
            return                          # (the check under way fills the window in)
        self.check_now()                    # (what GitHub has now)

    def open_back(self):
        if self.busy:
            return
        self._open(updater.BACK)
        self.window._background(lambda: self.back_fn(), self._back_planned)

    def _back_planned(self, plan, error):
        if plan is None:
            plan = updater.Plan(root=paths.PROJECT_ROOT, kind=updater.BACK, error=f"{type(error).__name__}: {error}")
        if self.dialog is not None and self.dialog.kind == updater.BACK:
            self.dialog.set_plan(plan)

    def _open(self, kind: str):
        if self.dialog is not None:
            self.dialog.close()
        self.dialog = UpdateDialog(self, kind, self.window)
        self.dialog.finished.connect(lambda code, d=self.dialog: self._closed(d))
        self.dialog.open()

    def _closed(self, dialog):
        if self.dialog is dialog:
            self.dialog = None
        if dialog.report is not None and dialog.report.ok:
            self.restart_launcher()
        dialog.deleteLater()

    # --- is the machine safe? ----------------------------------------------------------------------
    def refresh(self, running: bool):
        """Every second, from the window: who has been running, and the update window's conditions."""
        now = self.clock()
        if running:
            self.seen["this"] = now
        other = self.other_running()
        if other:
            self.seen["other"] = now
        if self.dialog is not None and self.dialog.state == "review":
            self.look_around()
            self.dialog.set_reasons(self.reasons(running), running)

    def look_around(self, now: bool = False):
        """SINDIE's processes on this PC (every system's), looked at again: in the background every
        LOOK_AROUND_S while the update window is open, or now (on the window's thread)."""
        if now:
            self._procs_known(programs.sindie_processes(), None)
            return
        if self._looking or (self._procs_at is not None and self.clock() - self._procs_at < LOOK_AROUND_S):
            return
        self._looking = True
        self.window._background(programs.sindie_processes, self._procs_known)

    def _procs_known(self, procs, error):
        self._looking = False
        if error is not None:
            log.warning("SINDIE's processes couldn't be looked at: %s", error)
            procs = {None: ["(processes couldn't be looked at)"]}
        self.procs, self._procs_at = dict(procs or {}), self.clock()

    def other_running(self) -> bool:
        """The other system's services: from the real system's launcher, any other system's (a
        simulator's); from the simulator's, the real system's."""
        mine = programs.this_system()
        if programs.simulation():
            return programs.other_manager_running() or programs.REAL in self.procs
        return programs.other_manager_running() or any(key != mine for key in self.procs)

    def reasons(self, running: bool) -> list:
        now, mine, view = self.clock(), programs.this_system(), self.window.cache.view()
        other = self.other_running()

        def silent(key, up):
            return None if up else now - (self.seen[key] if self.seen[key] is not None else self.opened)
        strays = [] if running else self.procs.get(mine, [])
        heard = not running and self.window.cache.topic_fresh("PLC_DATA")
        others = [programs.system_name(k) for k in self.procs
                  if k != mine and not (programs.simulation() and k == programs.REAL)]
        return updater.machine_reasons(view, running, silent("this", running), self.open_programs(), other,
                                       silent("other", other), programs.simulation(), strays=strays, plc_heard=heard,
                                       others=others)

    def open_programs(self) -> list:
        """The HMIs and data viewers open on this PC (the simulator's too)."""
        from src.launcher import instance
        out = []
        for sim in (False, True):
            for program, title in PROGRAMS:
                if instance.is_running(programs.instance_name(program, sim=sim)):
                    out.append(title + (" (simulator)" if sim else ""))
        return out

    # --- applying ----------------------------------------------------------------------------------
    def start_apply(self, plan) -> bool:
        """Update now pressed: the conditions once more, as they are this moment, then the update in a
        thread of its own. False if it didn't start (the update window shows why)."""
        if self.busy or self.checking:
            return False
        running = programs.manager_running()
        self.look_around(now=True)
        reasons = self.reasons(running)
        if reasons:
            if self.dialog is not None:
                self.dialog.set_reasons(reasons, running)
            return False
        self.busy = True
        self.update_pane()
        self.window.refresh()
        hooks, user = self.hooks(), programs._user()
        log.info("Software %s to %s started by %s", "update" if plan.kind == updater.UPDATE else "going back",
                 plan.target[:7], user)
        registry = updater.live_registry()
        self.window._background(lambda: self.apply_fn(plan, hooks, step=self.stepped.emit, user=user,
                                                      registry_path=registry), self._applied)
        return True

    def _stepped(self, text: str):
        if self.dialog is not None:
            self.dialog.add_step(text)

    def _applied(self, report, error):
        self.busy = False
        if report is None:
            report = updater.Report(message=f"The update stopped: {type(error).__name__}: {error}")
        if self.dialog is not None:
            self.dialog.show_result(report)
        self.plan = None
        if report.ok and report.kind == updater.UPDATE:     # (what the launcher started again shows)
            self.plan = updater.Plan(root=paths.PROJECT_ROOT, checked_at=time.time(), state=updater.UP_TO_DATE,
                                     current=report.now)
            updater.remember_check(self.plan)
        self.update_pane()
        self.window.refresh()
        if not report.ok:
            self.window._background(lambda: updater.version(), self._version_known)

    def hooks(self) -> updater.Hooks:
        def stop(force: bool) -> str:
            programs.record_services_stopped(True)          # (no launcher starts them again meanwhile)
            problem = programs.stop_manager()
            if problem and force:                           # (going back: a new version that doesn't answer)
                log.warning("Update: %s - ending the service manager and its services", problem)
                programs.end_manager_processes()
                end = time.monotonic() + 10.0
                while programs.manager_running() and time.monotonic() < end:
                    time.sleep(0.5)
                problem = "" if not programs.manager_running() else f"{problem}, and it couldn't be ended"
            return problem

        def start():
            if programs.simulation():               # (its registry, built by the version now on disk)
                problem = programs.rebuild_simulator_registry(python)
                if problem:
                    return f"the simulator's tag registry couldn't be built: {problem}"
            programs.record_services_stopped(False)
            try:
                programs.start_manager()
            except OSError as e:
                return f"the service manager couldn't be started: {e}"
            end = time.monotonic() + START_WAIT_S
            while time.monotonic() < end:
                if programs.manager_running():
                    return ""
                time.sleep(0.5)
            return f"the service manager didn't start within {START_WAIT_S:.0f} s"

        # (the machine's conditions were checked once more as Update now was pressed: start_apply)
        python = programs.python_for(gui=False)
        return updater.Hooks(services_running=programs.manager_running, online=self.online, stop_services=stop,
                             start_services=start, healthy=self.wait_healthy,
                             build_registry=lambda out: updater.build_registry_into(out, python=python),
                             self_check=lambda: updater.self_check_of(python=python))

    def online(self):
        """The services the service manager reports ONLINE (None: it hasn't reported just now)."""
        cache = self.window.cache
        states = cache.view().get("manager.services")
        if not cache.topic_fresh("MANAGER") or not isinstance(states, dict):
            return None
        return {n for n, s in states.items() if s == "ONLINE"}

    def health_problem(self, before: set, since: float) -> str:
        """'' once the service manager has reported since `since` (the cache's clock), every service in
        `before` it reports is ONLINE, and the PLC answers; otherwise what is still missing."""
        cache = self.window.cache
        seen = cache.topics
        if seen.get("MANAGER", 0) < since:
            return "the service manager hasn't reported"
        view = cache.view()
        states = view.get("manager.services") or {}
        missing = [n.replace("service_", "") for n in sorted(before) if n not in states]
        if missing:
            return "not run by the service manager now: " + ", ".join(missing)
        waiting = [f"{n.replace('service_', '')} ({states[n]})" for n in sorted(before) if states[n] != "ONLINE"]
        if waiting:
            return "not ONLINE: " + ", ".join(waiting)
        if seen.get("PLC_DATA", 0) < since or not view.get("system.connected"):
            return "the PLC isn't answering"
        return ""

    def wait_healthy(self, timeout: float, before: set) -> str:
        since, end = time.time(), time.monotonic() + timeout
        while True:
            problem = self.health_problem(before, since)
            if not problem or time.monotonic() >= end:
                return problem
            time.sleep(1.0)

    # --- the launcher again, with the new version ---------------------------------------------------
    def restart_launcher(self):
        w = self.window
        args = [programs.python_for(gui=True), programs.script("launcher")] + programs.mode_args()
        args += (["--keep-running"] if w.chk_keep.isChecked() else []) + ["--after-update"]
        guard = getattr(w, "guard", None)
        if guard is not None:
            guard.close()                   # (the new one takes the name)
        try:
            # (without the simulator's registry this one chose: the new launcher builds its own, with the new version)
            programs.spawn(args, "launcher_console.log", env=programs.child_env(own_registry=False))
        except OSError as e:
            self._restart_failed(f"it couldn't be started ({e})")
            return
        log.info("Starting the launcher again, to run the new version")
        self._restart_until = self.clock() + RESTART_WAIT_S
        self._restart = QTimer(self)
        self._restart.timeout.connect(self._restart_poll)
        self._restart.start(500)

    def _restart_poll(self):
        from src.launcher import instance
        if instance.is_running(programs.instance_name("launcher")):
            self._restart.stop()
            log.info("The new launcher is running: this one closes")
            self.window.quit()
        elif self.clock() > self._restart_until:
            self._restart.stop()
            self._restart_failed(f"it didn't start within {RESTART_WAIT_S:.0f} s (SystemLogs/launcher_console.log)")

    def _restart_failed(self, why: str):
        log.error("The launcher couldn't start again: %s", why)
        guard = getattr(self.window, "guard", None)
        if guard is not None:
            guard.claim(notify=False)
        QMessageBox.warning(self.window, "SINDIE", f"The launcher couldn't start again with the new version: {why}. "
                            "This one stays open, but runs the version before: close it (Close the launcher, in the "
                            "tray icon's menu) and open it again from the SINDIE shortcut.")

    def startup(self, after_update: bool = False):
        """When the launcher opens: the outcome of the update it was started again for, or an update
        that never finished."""
        folder = updater.unfinished()
        if folder:
            updater.mark_seen(folder)
            self._message(QMessageBox.Icon.Critical, "An update started and never finished: the launcher stopped half way "
                          "through it, so the software may be half updated. Call Josh before starting the services. "
                          f"What it did and what it kept are in {folder}.")
        elif after_update:
            last = (updater.history() or [{}])[-1]
            self._message(QMessageBox.Icon.Information, "\n\n".join(t for t in (last.get("message") or "Updated.",
                                                                               last.get("advice")) if t))

    def _message(self, icon, text: str):
        box = QMessageBox(icon, "SINDIE", text, QMessageBox.StandardButton.Ok, self.window)
        box.open()
        self._box = box


class UpdateDialog(QDialog):
    """What an update brings (or going back takes away), why it can't start yet, then the update
    itself, step by step, and how it went."""

    def __init__(self, controller: UpdateController, kind: str, parent=None):
        super().__init__(parent)
        self.controller, self.kind = controller, kind
        self.plan, self.report, self.reasons = None, None, ["Checking..."]
        self.state = "checking"             # checking, review, applying, done
        self.word = SAFE_WORDS[kind]
        back = kind == updater.BACK
        self.setWindowTitle("Go back to the version before" if back else "Update SINDIE")
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        self.heading = QLabel("Finding the version before..." if back else "Checking GitHub for updates...")
        set_role(self.heading, "heading")
        self.versions = QLabel()
        set_role(self.versions, "caption")
        self.changes_head = QLabel("What going back takes away" if back else "What is in it")
        set_role(self.changes_head, "title")
        self.changes = QTextBrowser()
        self.changes.setOpenLinks(False)
        self.changes.setMinimumHeight(200)
        self.blockers = QLabel()
        set_role(self.blockers, "note")
        self.blockers.setProperty("level", "fault")
        self.notes = QLabel()
        set_role(self.notes, "caption")
        self.ready_head = QLabel("Before it can start")
        set_role(self.ready_head, "title")
        self.conditions = QLabel()
        set_role(self.conditions, "note")
        self.what = QLabel(WHAT_BACK if back else WHAT_UPDATE)
        set_role(self.what, "caption")
        self.ask = QLabel(f"Type {self.word} to confirm:")
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(self.word)
        self.progress = QLabel()
        self.result = QLabel()
        set_role(self.result, "note")
        for w in (self.heading, self.versions, self.changes_head, self.changes, self.blockers, self.notes,
                  self.ready_head, self.conditions, self.what, self.ask, self.edit, self.progress, self.result):
            if isinstance(w, QLabel):
                w.setWordWrap(True)
            lay.addWidget(w)
        self.buttons = QDialogButtonBox()
        self.btn_later = self.buttons.addButton("Later", QDialogButtonBox.ButtonRole.RejectRole)
        self.btn_go = self.buttons.addButton("Go back now" if back else "Update now",
                                             QDialogButtonBox.ButtonRole.AcceptRole)
        set_tone(self.btn_go, "primary")
        self.btn_close = self.buttons.addButton("Close", QDialogButtonBox.ButtonRole.RejectRole)
        self.btn_restart = self.buttons.addButton("Restart the launcher", QDialogButtonBox.ButtonRole.AcceptRole)
        for b in (self.btn_later, self.btn_close, self.btn_restart):
            b.setAutoDefault(False)
        self.btn_go.setDefault(True)
        self.btn_later.clicked.connect(self.reject)
        self.btn_close.clicked.connect(self.reject)
        self.btn_restart.clicked.connect(self.accept)
        self.btn_go.clicked.connect(self._go)
        self.edit.textChanged.connect(self._update_go)
        lay.addWidget(self.buttons)
        self.setMinimumWidth(620)
        self._show_for_state()

    def _show_for_state(self):
        s, reviewable = self.state, self.plan is not None and self.plan.available
        for w in (self.changes_head, self.changes):
            w.setVisible(reviewable)
        self.blockers.setVisible(reviewable and bool(self.plan.blockers))
        self.notes.setVisible(reviewable and bool(self.plan.notes) and s == "review")
        for w in (self.ready_head, self.conditions, self.what, self.ask, self.edit, self.btn_go):
            w.setVisible(s == "review" and self.plan.state == updater.READY)
        self.btn_later.setVisible(s in ("checking", "review"))
        self.progress.setVisible(s == "applying" or (s == "done" and bool(self.progress.text())))
        self.result.setVisible(s == "done" and self.report is not None)
        self.btn_close.setVisible(s == "done" and not (self.report is not None and self.report.ok))
        self.btn_restart.setVisible(s == "done" and self.report is not None and self.report.ok)
        self._update_go()

    def set_plan(self, plan):
        self.plan = plan
        back = self.kind == updater.BACK
        if plan.state == updater.CANT_CHECK:
            self.heading.setText("Can't go back" if back else "Couldn't check for updates")
            self.versions.setText(plan.error[:1].upper() + plan.error[1:] + ".")
            self.state, self.report = "done", None
        elif plan.state == updater.UP_TO_DATE:
            self.heading.setText("Up to date: there is nothing to update")
            self.versions.setText(f"This PC runs {plan.current[:7]} of {day(plan.current_date)}, what the lab branch has.")
            self.state = "done"
        else:
            n = len(plan.commits)
            self.heading.setText(f"Go back to {plan.target[:7]}?" if back else f"Update SINDIE to {plan.target[:7]}?")
            self.versions.setText(f"From {plan.current[:7]} ({day(plan.current_date)}) to {plan.target[:7]} "
                                  f"({day(plan.target_date)}): {n} change{'s' if n != 1 else ''}."
                                  + ("" if back else f" Its GitHub tests: {plan.tests}."))
            self.changes.setHtml(changes_html(plan.commits))
            self.blockers.setText("It can't be applied from here:\n" + bullets(plan.blockers))
            self.notes.setText(bullets(plan.notes))
            self.conditions.setText("Checking the machine...")
            self.state = "review"
        self._show_for_state()

    def set_reasons(self, reasons: list, running: bool = True):
        if reasons == self.reasons:
            return
        self.reasons = list(reasons)
        if reasons:
            self.conditions.setText("Not yet:\n" + bullets(reasons))
            self.conditions.setProperty("level", "warn")
        else:
            self.conditions.setText(
                "Ready: the PLC reports the safety relay off and the source cold, and nothing is running." if running
                else "Ready: the services have been stopped for over a minute, so the PLC has the safety relay off.")
            self.conditions.setProperty("level", "info")
        self.conditions.style().unpolish(self.conditions)
        self.conditions.style().polish(self.conditions)
        self._update_go()

    def _update_go(self):
        ready = (self.state == "review" and self.plan is not None and self.plan.state == updater.READY
                 and not self.reasons and not self.controller.checking)
        self.btn_go.setEnabled(ready and self.edit.text().strip() == self.word)

    def _go(self):
        if not self.btn_go.isEnabled():
            return
        if not self.controller.start_apply(self.plan):
            return                          # (the conditions, as they are this moment, are shown)
        self.state = "applying"
        self.heading.setText("Going back..." if self.kind == updater.BACK else "Updating...")
        self.progress.setText("")
        self._show_for_state()

    def add_step(self, text: str):
        line = f"{time.strftime('%H:%M:%S')}  {text}"
        self.progress.setText(f"{self.progress.text()}\n{line}" if self.progress.text() else line)

    def show_result(self, report):
        self.report, self.state = report, "done"
        if report.ok:
            self.heading.setText(report.message)
            self.result.setText("\n\n".join(t for t in (report.advice, "The launcher now starts again, to run the new "
                                                        "version too.") if t))
            self.result.setProperty("level", "info")
        else:
            self.heading.setText("The update didn't work" if self.kind == updater.UPDATE else "Going back didn't work")
            self.result.setText("\n\n".join(t for t in (
                f"{report.message[:1].upper()}{report.message[1:]}.",
                "The version before is back." if report.rolled_back else "", report.advice,
                f"What it did is in {report.folder} and SystemLogs/launcher.log." if report.folder else "") if t))
            self.result.setProperty("level", "fault")
        self.result.style().unpolish(self.result)
        self.result.style().polish(self.result)
        self._show_for_state()

    def reject(self):
        if self.state == "applying":
            return                          # (it can't be closed while it runs)
        super().reject()

    def closeEvent(self, event):
        if self.state == "applying":
            event.ignore()
            return
        super().closeEvent(event)
