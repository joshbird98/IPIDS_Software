"""One copy of each program per system: starting a second HMI, data viewer or launcher brings the
first to the front instead (the simulator's and the real system's copies are kept apart).

    guard = SingleInstance("hmi")
    if not guard.claim():           # another copy is running: it has been asked to come forward
        sys.exit(0)
    guard.activated.connect(lambda message: bring_to_front(window))

It uses a local socket (a named pipe on Windows) named for the program, the system and the user.
The running copy answers "ok" to each message, so a copy that is hung can be told from one that
isn't there.
"""
import logging
import os
import time

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtNetwork import QLocalServer, QLocalSocket

from src.launcher.programs import TITLES, instance_name

log = logging.getLogger("launcher")

ANSWERED, SILENT, NONE = "answered", "silent", "none"
REPLY = b"ok"


def _allow_foreground():
    """Windows lets only the program in front bring another to the front: let the one we wake."""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.AllowSetForegroundWindow(-1)         # ASFW_ANY
        except (AttributeError, OSError):
            pass


def ask_running(program: str, message: str = "activate", timeout_ms: int = 500, reply_ms: int = 2000) -> str:
    """Tell a running copy of `program` (this system's) to come forward. ANSWERED; SILENT when a
    copy holds the name but doesn't answer within reply_ms (hung, or busy); NONE when there is none."""
    sock = QLocalSocket()
    sock.connectToServer(instance_name(program))
    if not sock.waitForConnected(timeout_ms):
        return NONE
    _allow_foreground()
    sock.write((message + "\n").encode("utf-8"))
    sock.flush()
    sock.waitForBytesWritten(timeout_ms)
    answered, end = False, time.monotonic() + reply_ms / 1000.0
    while not answered and time.monotonic() < end:
        if not sock.canReadLine() and not sock.waitForReadyRead(max(1, int((end - time.monotonic()) * 1000))):
            break
        while sock.canReadLine():
            answered = answered or bytes(sock.readLine()).strip() == REPLY
    sock.disconnectFromServer()
    return ANSWERED if answered else SILENT


def not_answering(program: str) -> str:
    title = TITLES.get(program, program)
    return (f"The {title} is already open for this system, but isn't answering: it may be busy. Try again in a "
            f"moment. If it stays like this, close it (with Task Manager if need be) and open it again.")


def open_or_raise(program: str) -> str:
    """Bring this system's running copy of a program (the HMI, the data viewer) to the front, or
    start one. '' if done, otherwise what went wrong."""
    from src.launcher import programs
    answer = ask_running(program)
    if answer == ANSWERED:
        return ""
    if answer == SILENT:
        return not_answering(program)
    try:
        programs.open_program(program)
    except OSError as e:
        log.error("Could not start the %s: %s", programs.TITLES.get(program, program), e)
        return f"Couldn't start the {programs.TITLES.get(program, program)}: {e}"
    return ""


def bring_to_front(window):
    if window.isMinimized():
        window.showNormal()
    window.show()
    window.raise_()
    window.activateWindow()


class SingleInstance(QObject):
    activated = pyqtSignal(str)

    def __init__(self, program: str, parent=None):
        super().__init__(parent)
        self.program = program
        self.name = instance_name(program)
        self.server = None
        self.answer = NONE          # what claim() found: another copy ANSWERED, was SILENT, or NONE

    def claim(self, message: str = "activate", notify: bool = True) -> bool:
        """True if this is the only copy (it now listens for later ones); False if another copy
        holds the name: it has been sent `message`. If it didn't answer, the operator is told
        (notify) rather than a second copy started."""
        self.answer = ask_running(self.program, message)
        if self.answer != NONE:
            self._defer(notify)
            return False
        self.server = QLocalServer(self)
        if not self.server.listen(self.name):
            # Taken: another copy starting at this moment, or (Unix) a socket file left by a copy
            # that crashed. Only when nothing answers on it is it removed and taken. (A Windows
            # named pipe can be listened on twice, so two copies started in the same instant may
            # both run there: two windows, nothing worse.)
            self.answer = ask_running(self.program, message)
            if self.answer != NONE:
                self.server = None
                self._defer(notify)
                return False
            QLocalServer.removeServer(self.name)
            if not self.server.listen(self.name):
                log.warning("Single-copy check for %s unavailable: %s", self.program, self.server.errorString())
                return True
        self.server.newConnection.connect(self._incoming)
        return True

    def _defer(self, notify: bool):
        if self.answer == ANSWERED:
            log.info("%s is already running: asked it to come forward", self.program)
            return
        log.warning("%s is already running but didn't answer: not starting a second copy", self.program)
        if notify:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(None, "SINDIE", not_answering(self.program))

    def _incoming(self):
        while self.server is not None and self.server.hasPendingConnections():
            sock = self.server.nextPendingConnection()
            sock.readyRead.connect(lambda s=sock: self._read(s))
            sock.disconnected.connect(sock.deleteLater)
            self._read(sock)                # (what arrived before the connection was taken)

    def _read(self, sock):
        while sock.canReadLine():
            message = bytes(sock.readLine()).decode("utf-8", "replace").strip()
            if message:
                sock.write(REPLY + b"\n")
                sock.flush()
                self.activated.emit(message)

    def close(self):
        if self.server is not None:
            self.server.close()
            self.server = None
