"""Start a program and leave it running on its own:

    python -m src.launcher.detach <log file> <program> [arguments...]

starts <program> detached (no console window on Windows, its own process group / session, its output
appended to <log file>) and exits at once. The program is then nobody's child: PyCharm, which ends
a run's whole process tree when its Stop button is pressed (and Windows tools that end a "tree"),
can't take the service manager down with the launcher that started it - stopping the services
makes the PLC shut the source down, so that must only happen when someone means it.
"""
import os
import subprocess
import sys

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NEW_CONSOLE = 0x00000010
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def start(args: list, log_path: str = None, console: bool = False, cwd: str = None, env: dict = None):
    """Popen `args` detached; its output appended to log_path (or nowhere). console (Windows): in a
    console window of its own instead, reading its keyboard and writing to it."""
    console = console and os.name == "nt"
    out = open(log_path, "a", buffering=1, encoding="utf-8", errors="replace") if log_path and not console \
        else subprocess.DEVNULL
    kw = dict(cwd=cwd, env=env, close_fds=True)
    if not console:
        kw.update(stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
    # (With a console of its own it is given no handles at all, so Windows gives it the new console's.
    # Passing any one would hand it this helper's others - its output a pipe to the launcher, which
    # would then wait for the program to end.)
    try:
        if os.name == "nt":
            flags = (CREATE_NEW_CONSOLE if console else DETACHED_PROCESS) | CREATE_NEW_PROCESS_GROUP
            try:
                return subprocess.Popen(args, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **kw)
            except OSError:         # (a job that doesn't allow breaking away: stay in it)
                return subprocess.Popen(args, creationflags=flags, **kw)
        return subprocess.Popen(args, start_new_session=True, **kw)
    finally:
        if out is not subprocess.DEVNULL:
            out.close()             # (the child has its own handle)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    console = argv[:1] == ["--console"]
    if console:
        argv = argv[1:]
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    log_path, args = argv[0] or None, argv[1:]
    proc = start(args, log_path, console=console)
    print(proc.pid)                 # (for whoever started this; nobody waits for the program itself)
    return 0


if __name__ == "__main__":
    sys.exit(main())
