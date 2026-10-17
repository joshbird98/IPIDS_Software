"""Per-process logging: one rotating file per service in SystemLogs/, plus the console.

Before this, services reported problems with print() (lost when the window
closed) or swallowed them with bare `except: pass`. Every process now calls
setup_logging("service_xxx") once at start-up and uses ordinary
logging.getLogger(...) calls. Uncaught exceptions, including ones in background
threads, are written to the log with a full traceback before the process dies,
so a crash that made the service manager restart something can be explained
afterwards.

Files: SystemLogs/<name>.log, rotated at 5 MB, 5 old copies kept (30 MB max
per process).
"""
import logging
import logging.handlers
import os
import sys
import threading
import time

from src.core import paths

FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"

_configured_name = None


class _NotAudit(logging.Filter):
    """Keeps the high-volume command audit trail (logger names ending in .audit)
    out of the console; it still goes to the log file and to events.db."""

    def filter(self, record):
        return not record.name.endswith(".audit")


def setup_logging(name: str, level: int = logging.INFO, console: bool = True,
                  log_dir: str = None, max_bytes: int = 5 * 1024 * 1024, backups: int = 5) -> logging.Logger:
    """Configure the root logger for this process (idempotent) and return a logger called `name`."""
    global _configured_name
    if _configured_name is not None:
        return logging.getLogger(name)

    log_dir = log_dir or paths.LOG_DIR
    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter(FORMAT, DATEFMT)

    try:
        os.makedirs(log_dir, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            os.path.join(log_dir, f"{name}.log"), maxBytes=max_bytes, backupCount=backups,
            encoding="utf-8", delay=True)
        fh.setFormatter(formatter)
        root.addHandler(fh)
    except OSError as e:  # read-only disk etc. - still log to the console
        sys.stderr.write(f"[log_setup] cannot open log file in {log_dir}: {e}\n")

    if console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(formatter)
        ch.addFilter(_NotAudit())
        root.addHandler(ch)

    # Third-party libraries are chatty at INFO (pymodbus logs every reconnect)
    for noisy in ("pymodbus", "snap7", "matplotlib", "PIL", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    log = logging.getLogger(name)

    def _excepthook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        # In a service this ends the process; in the Qt GUIs (PyQt calls this hook for exceptions
        # raised in slots) the application carries on - either way the traceback is kept.
        log.critical("Unhandled exception", exc_info=(exc_type, exc, tb))

    def _thread_excepthook(args):
        if args.exc_type is SystemExit:
            return
        log.critical("Uncaught exception in thread %s", getattr(args.thread, "name", "?"),
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    _configured_name = name
    return log


class Throttle:
    """Rate-limits a repeating log message (e.g. 'connection failed' every 100 ms).

        if (n := throttle.ready("connect")) is not None:
            log.warning("PLC connect failed: %s%s", err, Throttle.suffix(n))

    ready() returns None while the message is being suppressed, otherwise the
    number of occurrences swallowed since it last returned non-None.
    """

    def __init__(self, interval_s: float = 60.0, clock=time.monotonic):
        self.interval_s = interval_s
        self._clock = clock
        self._last = {}
        self._suppressed = {}

    def ready(self, key="default"):
        now = self._clock()
        last = self._last.get(key)
        if last is None or now - last >= self.interval_s:
            self._last[key] = now
            return self._suppressed.pop(key, 0)
        self._suppressed[key] = self._suppressed.get(key, 0) + 1
        return None

    def reset(self, key="default"):
        """Call when the condition clears so the next occurrence is logged straight away."""
        self._last.pop(key, None)
        self._suppressed.pop(key, None)

    @staticmethod
    def suffix(n: int) -> str:
        return f" (+{n} similar suppressed)" if n else ""
