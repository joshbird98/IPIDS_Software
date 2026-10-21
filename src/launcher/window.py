"""The launcher's window and tray icon (main_launcher.py).

The window starts the HMI, the data viewer, the help and the simulator, shows the services' state,
and starts and stops the service manager. Closed, it goes to the tray, where the icon's dot shows
the services' state; the services never depend on it. With "Keep the services running" it starts
the service manager if it isn't running and starts it again if it stops, unless they were stopped
on purpose ("Stop services...", which asks you to type STOP first): that is recorded for this
system, so no launcher starts them again until someone presses Start services.
"""
import logging
import os
import threading
import time

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QFont
from PyQt6.QtWidgets import (QApplication, QCheckBox, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel,
                             QLineEdit, QMenu, QMessageBox, QPushButton, QSystemTrayIcon, QVBoxLayout, QWidget)

from src.core.telemetry_cache import TelemetryCache
from src.gui.settings import gui_settings
from src.gui.style import chip, set_role, set_tone
from src.launcher import icon, programs

log = logging.getLogger("launcher")

MANAGER_TOPIC = "MANAGER"       # (src/core/network_map.py: TOPIC_MANAGER)
MANAGER_SILENT_S = 5.0          # the lock taken but no word from the manager for this long: not answering
SETTLE_S = 30.0                 # ... unless it (or the launcher) started less than this long ago
RESTART_DELAYS = (5.0, 30.0, 120.0)     # keep running: wait this long before starting it again (1st, 2nd, then)
STABLE_S = 300.0                # ... and forget earlier restarts once it has run this long
START_GRACE_S = 30.0            # a service manager just started isn't started again for this long
SAFE_WORD = "STOP"
RELAY = "ion_beam.facilities.safety_relay_active"
SOURCE_COLD = "ion_beam.source.chamber.stat_gv_close_permissive"


class Background(QObject):
    """A slow call (PowerShell, waiting for the service manager to stop) run off the window's
    thread, so the window keeps answering. `done(job, result, error)` is connected to a method of
    the window, so it is delivered on the window's thread; `then` is called there with the outcome."""
    done = pyqtSignal(object, object, object)

    def __init__(self, fn, then, parent=None):
        super().__init__(parent)
        self._fn = fn
        self.then = then
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def _run(self):
        try:
            result, error = self._fn(), None
        except Exception as e:      # (handed to the window, which says what went wrong)
            result, error = None, e
        self.done.emit(self, result, error)


def _services_summary(view: dict) -> tuple:
    """(online, total, waiting text) from the service manager's report."""
    states = view.get("manager.services") or {}
    if not isinstance(states, dict) or not states:
        return 0, 0, ""
    online = sum(1 for s in states.values() if s == "ONLINE")
    rest = ", ".join(f"{n.replace('service_', '').replace('_', ' ')} ({s})" for n, s in sorted(states.items())
                     if s != "ONLINE")
    return online, len(states), rest


class StopDialog(QDialog):
    """Stop every service: the consequences, and STOP typed to confirm."""

    def __init__(self, view: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Stop the SINDIE services")
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        heading = QLabel("Stop every service and the service manager?")
        set_role(heading, "heading")
        lay.addWidget(heading)
        what = self.warning(view)
        if what:
            warn = QLabel(what)
            warn.setWordWrap(True)
            set_role(warn, "note")
            warn.setProperty("level", "fault")
            lay.addWidget(warn)
        text = QLabel(
            "Without the services the PC's watchdog to the PLC stops. After 3 s the PLC latches "
            "PC_Watchdog_Timeout, closes the gate valve (which drops the safety relay) and starts its own "
            "shutdown. Nothing is logged and no alarm sounds until the services are started again.")
        text.setWordWrap(True)
        lay.addWidget(text)
        ask = QLabel(f"Type {SAFE_WORD} to confirm:")
        lay.addWidget(ask)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(SAFE_WORD)
        lay.addWidget(self.edit)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.btn_stop = self.buttons.addButton("Stop services", QDialogButtonBox.ButtonRole.AcceptRole)
        set_tone(self.btn_stop, "danger")
        self.btn_stop.setEnabled(False)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        self.edit.textChanged.connect(lambda t: self.btn_stop.setEnabled(t.strip() == SAFE_WORD))
        self.setMinimumWidth(460)

    @staticmethod
    def warning(view: dict) -> str:
        """What the machine's state means for stopping the services now ('' if nothing to add)."""
        relay, cold = view.get(RELAY), view.get(SOURCE_COLD)
        if relay and cold == 0:
            return ("The safety relay is on and the source is hot or at high voltage. Shut the source down first "
                    "(INITIATE SHUTDOWN, System Health panel).")
        if relay:
            return "The safety relay is on. Switch it off first (System Health panel)."
        if cold == 0:
            return ("The safety relay is off, but the source isn't cold yet (the PLC's model): the PLC will start its "
                    "own shutdown once the watchdog stops.")
        if relay is None:
            return "The PLC isn't reporting, so the safety relay's state isn't known here."
        return ""


class LauncherWindow(QWidget):
    def __init__(self, keep_running: bool = None, telemetry=True, settings=None, clock=time.monotonic,
                 windows: bool = None):
        super().__init__()
        self.windows = os.name == "nt" if windows is None else windows      # (the Windows settings)
        self.setObjectName("LauncherWindow")
        self.setWindowTitle("SINDIE" + (" [SIMULATION]" if programs.simulation() else ""))
        self.setWindowIcon(icon.app_icon())
        self.settings = settings or gui_settings("SurreyIonBeam", "SINDIE_Launcher")
        self.clock = clock
        self.cache = TelemetryCache()
        self._telemetry = None
        self._stopped = programs.services_stopped()     # stopped on purpose: {'at', 'by'} (or None)
        self._stopping = False
        self._jobs = set()                  # Background calls still running
        self._was_running = None
        self._down_since = None
        self._restarts = 0
        self._started_at = None
        self._told_tray = False
        self._status = None
        self._quitting = False
        self._opened_at = clock()
        self._build()
        if keep_running is not None:
            self.chk_keep.blockSignals(True)
            self.chk_keep.setChecked(keep_running)
            self.chk_keep.blockSignals(False)
        self.chk_keep.toggled.connect(self._keep_toggled)
        self.tray = self._make_tray()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        if telemetry:
            self._start_telemetry()
        if self._stopped:
            if self.tray is not None:
                self.tray.showMessage("SINDIE", f"The services were stopped on purpose ({self._stopped_text()}). They "
                                      "stay stopped until Start services.", QSystemTrayIcon.MessageIcon.Information, 8000)
        elif self.chk_keep.isChecked() and not programs.manager_running():
            log.info("Keep running: the service manager isn't running: starting it")
            self._start_manager()
        self.refresh()

    # --- the window --------------------------------------------------------------------------------
    def _pane(self, title: str) -> tuple:
        frame = QFrame()
        set_role(frame, "pane")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(8)
        if title:
            head = QLabel(title)
            set_role(head, "title")
            lay.addWidget(head)
        return frame, lay

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(10)

        top = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(icon.pixmap(48))
        top.addWidget(logo, 0, Qt.AlignmentFlag.AlignTop)
        names = QVBoxLayout()
        names.setSpacing(0)
        title = QLabel("SINDIE")
        font = title.font()
        font.setPointSizeF(font.pointSizeF() * 1.9)
        font.setWeight(QFont.Weight.Bold)
        font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 104)
        title.setFont(font)
        names.addWidget(title)
        sub = QLabel("Surrey Implanter for Negative-ions, Defect Implantation, and Enrichment")
        sub.setWordWrap(True)
        set_role(sub, "caption")
        names.addWidget(sub)
        top.addLayout(names, 1)
        self.mode = chip("SIMULATOR" if programs.simulation() else "LIVE SYSTEM",
                         "seq" if programs.simulation() else "off")
        top.addWidget(self.mode, 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(top)

        frame, lay = self._pane("")
        row = QHBoxLayout()
        head = QLabel("Services")
        set_role(head, "title")
        row.addWidget(head)
        row.addStretch(1)
        self.services_chip = chip("CHECKING...", "unknown")
        row.addWidget(self.services_chip)
        lay.addLayout(row)
        self.services_text = QLabel()
        self.services_text.setWordWrap(True)
        set_role(self.services_text, "caption")
        self.services_text.ensurePolished()
        self.services_text.setMinimumHeight(2 * self.services_text.fontMetrics().lineSpacing() + 2)   # (2 lines)
        lay.addWidget(self.services_text)
        buttons = QHBoxLayout()
        self.btn_start = QPushButton("Start services")
        self.btn_start.clicked.connect(self.start_services)
        self.btn_stop = QPushButton("Stop services...")
        set_tone(self.btn_stop, "danger")
        self.btn_stop.clicked.connect(self.stop_services)
        buttons.addWidget(self.btn_start)
        buttons.addWidget(self.btn_stop)
        buttons.addStretch(1)
        lay.addLayout(buttons)
        self.chk_keep = QCheckBox("Keep the services running (start them again if they stop)")
        self.chk_keep.setChecked(self.settings.value("keep_running", False, type=bool))
        lay.addWidget(self.chk_keep)
        outer.addWidget(frame)

        frame, lay = self._pane("Programs")
        grid = QHBoxLayout()
        self.btn_hmi = QPushButton("Operator HMI")
        set_tone(self.btn_hmi, "primary")
        self.btn_hmi.clicked.connect(lambda: self.open_program("hmi"))
        self.btn_viewer = QPushButton("Data viewer")
        set_tone(self.btn_viewer, "primary")
        self.btn_viewer.clicked.connect(lambda: self.open_program("viewer"))
        self.btn_help = QPushButton("Help")
        self.btn_help.clicked.connect(self.open_help)
        for b in (self.btn_hmi, self.btn_viewer, self.btn_help):
            b.setMinimumHeight(34)
            grid.addWidget(b)
        lay.addLayout(grid)
        hint = QLabel("A program that is already open comes to the front instead of opening twice.")
        hint.setWordWrap(True)
        set_role(hint, "hint")
        lay.addWidget(hint)
        if self.windows and not programs.simulation():         # (it needs a console window of its own)
            self.btn_sim = QPushButton("Start the simulator")
            self.btn_sim.setToolTip("run_simulation.py in a window of its own: simulated devices, its own ports and "
                                    "data_sim/. It never touches the real system.")
            self.btn_sim.clicked.connect(self.open_simulator)
            sim_row = QHBoxLayout()
            sim_row.addWidget(self.btn_sim)
            sim_row.addStretch(1)
            lay.addLayout(sim_row)
        outer.addWidget(frame)

        frame, lay = self._pane("Windows")
        self.chk_startup = QCheckBox("Start with Windows (in the tray, with the services)")
        self.btn_shortcuts = QPushButton("Put shortcuts on the desktop")
        self.btn_shortcuts.clicked.connect(self.make_shortcuts)
        windows = self.windows
        self.chk_startup.toggled.connect(self._startup_toggled)
        for w in (self.chk_startup, self.btn_shortcuts):
            w.setEnabled(windows)
            if not windows:
                w.setToolTip("Windows only")
        if programs.simulation():           # (only the real system starts with Windows)
            self.chk_startup.setEnabled(False)
            self.chk_startup.setToolTip("Only the real system's launcher starts with Windows")
        elif windows:                       # (PowerShell can take seconds: asked in the background)
            self.chk_startup.setEnabled(False)
            self.chk_startup.setToolTip("Checking...")
            self._background(programs.start_with_windows, self._startup_known)
        lay.addWidget(self.chk_startup)
        row = QHBoxLayout()
        row.addWidget(self.btn_shortcuts)
        row.addStretch(1)
        lay.addLayout(row)
        python = QLabel(f"Python: {programs.python_for(gui=True)}")
        python.setWordWrap(True)
        python.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        set_role(python, "hint")
        python.setToolTip("The shortcuts and the programs started here use this Python: the one this launcher "
                          "runs with (PyCharm's project interpreter, if it was started from PyCharm).")
        lay.addWidget(python)
        outer.addWidget(frame)
        outer.addStretch(1)
        self.setMinimumWidth(480)

    def showEvent(self, event):
        if not getattr(self, "_sized", False):
            self._sized = True
            self.resize(max(self.width(), 540), self.height())
            self._fit_height()
        super().showEvent(event)

    def _fit_height(self):
        """Wrapped text needs a height for the window's width, which Qt doesn't give a window by
        itself: grow the window when its text needs more room."""
        lay = self.layout()
        lay.invalidate()
        lay.activate()
        need = lay.totalHeightForWidth(self.width()) if lay.hasHeightForWidth() else lay.totalSizeHint().height()
        if need > self.height():
            self.resize(self.width(), need)

    # --- the tray ----------------------------------------------------------------------------------
    def _make_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None
        tray = QSystemTrayIcon(icon.app_icon(), self)
        menu = QMenu(self)
        menu.addAction("Open SINDIE", self.show_window)
        menu.addAction("Operator HMI", lambda: self.open_program("hmi"))
        menu.addAction("Data viewer", lambda: self.open_program("viewer"))
        menu.addSeparator()
        self.tray_status = QAction("Services: checking...", self)
        self.tray_status.setEnabled(False)
        menu.addAction(self.tray_status)
        self.tray_stop = menu.addAction("Stop services...", self.stop_services)
        menu.addSeparator()
        menu.addAction("Close the launcher (the services keep running)", self.quit)
        tray.setContextMenu(menu)
        tray.activated.connect(self._tray_clicked)
        tray.setToolTip("SINDIE")
        tray.show()
        return tray

    def _tray_clicked(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            if self.isVisible() and not self.isMinimized():
                self.hide()
            else:
                self.show_window()

    def show_window(self):
        from src.launcher.instance import bring_to_front
        bring_to_front(self)

    def closeEvent(self, event):
        # Closed: to the tray. (Not while Windows is logging off or shutting down: a window that
        # refuses to close then would hold the shutdown up.)
        if self.tray is not None and not self._quitting and not QApplication.instance().isSavingSession():
            event.ignore()
            self.hide()
            if not self._told_tray:
                self._told_tray = True
                self.tray.showMessage("SINDIE", "The launcher is still in the tray. The services keep running.",
                                      QSystemTrayIcon.MessageIcon.Information, 4000)
            return
        self._shutdown_threads()
        super().closeEvent(event)

    def _background(self, fn, then):
        job = Background(fn, then, self)
        self._jobs.add(job)
        job.done.connect(self._job_done)
        return job.start()

    def _job_done(self, job, result, error):
        self._jobs.discard(job)
        job.deleteLater()
        job.then(result, error)

    def _startup_known(self, on, error):
        self.chk_startup.blockSignals(True)
        self.chk_startup.setChecked(bool(on) and error is None)
        self.chk_startup.blockSignals(False)
        self.chk_startup.setEnabled(True)
        self.chk_startup.setToolTip("" if error is None else f"Couldn't check: {error}")

    def quit(self):
        self._quitting = True
        self._shutdown_threads()
        if self.tray is not None:
            self.tray.hide()
        self.close()
        QApplication.instance().quit()

    # --- telemetry ---------------------------------------------------------------------------------
    def _start_telemetry(self):
        from src.core.network_map import ZMQ_PORT_MANAGER_PUB, ZMQ_PORT_PLC_PUB
        from src.core.network_threads import ZMQTelemetryThread
        self._telemetry = ZMQTelemetryThread([ZMQ_PORT_MANAGER_PUB, ZMQ_PORT_PLC_PUB])
        self._telemetry.data_received.connect(self.cache.ingest)
        self._telemetry.start()

    def _shutdown_threads(self):
        if self._telemetry is not None:
            self._telemetry.stop()
            self._telemetry = None

    # --- the services ------------------------------------------------------------------------------
    def _stopped_text(self) -> str:
        s = self._stopped or {}
        return " ".join(x for x in (f"at {s['at']}" if s.get("at") else "", f"by {s['by']}" if s.get("by") else "")
                        if x) or "earlier"

    def _starting(self) -> bool:
        return self._started_at is not None and self.clock() - self._started_at < START_GRACE_S

    def status(self, running: bool = None) -> tuple:
        """(chip text, tone, sentence, tray dot) for the services now (`running`: the service
        manager holds its lock)."""
        running = programs.manager_running() if running is None else running
        view = self.cache.view()
        online, total, waiting = _services_summary(view)
        fresh = self.cache.topic_age(MANAGER_TOPIC) <= MANAGER_SILENT_S
        if self._stopping:
            return ("STOPPING...", "warn", "Stopping every service and the service manager...", "warn")
        if not running:
            if self._stopped:
                return ("STOPPED", "off", f"Stopped on purpose ({self._stopped_text()}). They stay stopped until "
                        "Start services is pressed.", "fault")
            if self._starting():
                return ("STARTING", "warn", "Starting the service manager...", "warn")
            return ("NOT RUNNING", "fault", "The service manager isn't running: nothing is talking to the PLC or "
                    "logging." + (" Starting it again shortly." if self.chk_keep.isChecked() else ""), "fault")
        if not fresh or not total:
            recent = self.clock() - max(self._opened_at, self._started_at or self._opened_at) < SETTLE_S
            return ("STARTING" if recent else "NOT ANSWERING", "warn",
                    "The service manager is running but hasn't reported yet." if recent else
                    "The service manager is running but hasn't reported for a while: see SystemLogs/"
                    "service_manager.log.", "warn")
        if online == total:
            return (f"{online} OF {total} ONLINE", "ok", "Every service is running. The service manager runs in the "
                    "background: closing this window or the HMI doesn't stop it.", "ok")
        return (f"{online} OF {total} ONLINE", "warn", f"Waiting for: {waiting}.", "warn")

    def refresh(self):
        self._stopped = programs.services_stopped()          # (another launcher of this system may have)
        running = programs.manager_running()
        if running and self._stopped and not self._stopping:
            # Started since by other means (or a stop that didn't finish): they aren't stopped now
            programs.record_services_stopped(False)
            self._stopped = None
        text, tone, sentence, dot = self.status(running)
        if self.services_chip.text() != text:
            self.services_chip.setText(text)
        set_tone(self.services_chip, tone)
        if self.services_text.text() != sentence:
            self.services_text.setText(sentence)
            QTimer.singleShot(0, self._fit_height)     # (once the layout has taken the new text in)
        can_start = not running and not self._starting() and not self._stopping
        self.btn_start.setEnabled(can_start)
        set_tone(self.btn_start, "primary" if can_start else None)
        self.btn_stop.setEnabled(running and not self._stopping)
        if self.tray is not None and dot != self._status:
            self._status = dot
            self.tray.setIcon(icon.app_icon(dot))
        if self.tray is not None:
            self.tray.setToolTip(f"SINDIE services: {text.lower()}")
            self.tray_status.setText(f"Services: {text.lower()}")
            self.tray_stop.setEnabled(running and not self._stopping)
        self._watch_manager(running)

    def _watch_manager(self, running: bool):
        """Keep running: start the service manager when it isn't running (after a wait that grows
        with each restart), unless the services were stopped on purpose or it was started a moment
        ago; tell the tray when it stops."""
        now = self.clock()
        if self._stopping:
            return
        if running:
            self._down_since = None
            if self._started_at is not None and now - self._started_at > STABLE_S:
                self._restarts = 0
        elif not self._starting():
            if self._was_running and not self._stopped and self.tray is not None:
                self.tray.showMessage("SINDIE", "The SINDIE services have stopped.", QSystemTrayIcon.MessageIcon.Warning,
                                      8000)
            if self._down_since is None:
                self._down_since = now
            wait = RESTART_DELAYS[min(self._restarts, len(RESTART_DELAYS) - 1)]
            if self.chk_keep.isChecked() and not self._stopped and now - self._down_since >= wait:
                self._restarts += 1
                log.warning("Keep running: starting the service manager (it wasn't running)")
                self._start_manager()
        self._was_running = running

    def _start_manager(self, interactive: bool = False) -> bool:
        """Start the service manager. Asked by the operator (interactive), a failure is shown in a
        dialog; from the timer only in the log and the tray, never a dialog that piles up."""
        try:
            programs.start_manager()
        except OSError as e:
            log.error("Could not start the service manager: %s", e)
            if interactive:
                QMessageBox.warning(self, "Start services", f"Couldn't start the service manager: {e}")
            elif self.tray is not None:
                self.tray.showMessage("SINDIE", f"Couldn't start the services: {e}", QSystemTrayIcon.MessageIcon.Critical,
                                      8000)
            return False
        self._started_at = self.clock()
        self._down_since = None
        return True

    def start_services(self):
        programs.record_services_stopped(False)
        self._stopped = None
        if not programs.manager_running() and not self._starting():
            self._start_manager(interactive=True)
        self.refresh()

    def _keep_toggled(self, on: bool):
        self.settings.setValue("keep_running", bool(on))
        if on:              # ticked: start it now if it isn't running (unless stopped on purpose)
            self._restarts = 0
            self._down_since = None
            if not self._stopped and not self._stopping and not self._starting() and not programs.manager_running():
                log.info("Keep running ticked: starting the service manager")
                self._start_manager()
        self.refresh()

    def stop_services(self):
        dlg = StopDialog(self.cache.view(), self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        programs.record_services_stopped(True)
        self._stopped = programs.services_stopped()
        self._stopping = True
        self.refresh()
        self._background(programs.stop_manager, self._stop_finished)

    def _stop_finished(self, problem, error):
        self._stopping = False
        problem = problem or (f"Couldn't ask the service manager to stop: {error}" if error else "")
        if problem:
            answer = QMessageBox.question(
                self, "Stop services", f"{problem}.\n\nEnd the service manager and the services it started now? "
                "(They are ended at once, without tidying up; the next start clears anything left.)")
            if answer == QMessageBox.StandardButton.Yes:
                n = programs.end_manager_processes()
                log.warning("Ended %d SINDIE processes", n)
        self.refresh()

    # --- the programs ------------------------------------------------------------------------------
    def open_program(self, program: str):
        from src.launcher.instance import open_or_raise
        problem = open_or_raise(program)
        if problem:
            QMessageBox.warning(self, programs.TITLES[program], problem)

    def open_help(self):
        from src.gui.help import show_help
        show_help("getting-started/starting-the-software.md")

    def open_simulator(self):
        try:
            programs.open_simulator()
        except (OSError, programs.subprocess.SubprocessError) as e:
            QMessageBox.warning(self, "Simulator", f"Couldn't start the simulator: {e}")

    def make_shortcuts(self):
        try:
            made = programs.make_desktop_shortcuts()
        except (OSError, programs.subprocess.SubprocessError) as e:
            QMessageBox.warning(self, "Shortcuts", f"Couldn't make the shortcuts: {e}")
            return
        QMessageBox.information(self, "Shortcuts", "On the desktop:\n" + "\n".join(made))

    def _startup_toggled(self, on: bool):
        try:
            programs.set_start_with_windows(on)
        except (OSError, programs.subprocess.SubprocessError) as e:
            QMessageBox.warning(self, "Start with Windows", f"Couldn't change it: {e}")
            self.chk_startup.blockSignals(True)
            self.chk_startup.setChecked(not on)
            self.chk_startup.blockSignals(False)
            return
        if on and not self.chk_keep.isChecked():
            self.chk_keep.setChecked(True)          # (started with Windows to keep them running)
