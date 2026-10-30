"""Software updates for the lab PC: the lab branch on GitHub, applied when an operator chooses, only
with the machine safe, and undone if the new version doesn't come up. No Qt here (the launcher's
Software box and update window are src/launcher/update_window.py).

    plan = check()                        # what an update would bring (fetches the lab branch; changes nothing else)
    why_not = machine_reasons(...)        # why the machine can't be updated now ([] = it can)
    report = apply(plan, hooks, step)     # the update; on any failure, the version before again
    plan = plan_back()                    # going back to the version before the last update (then apply)

Where updates come from: the branch "lab" on GitHub (origin/lab). Josh moves it on when a version is
ready for the lab (docs/engineering/releasing.md); nothing else reaches the lab PC. An update is only
ever a move forward along it, to a version whose GitHub tests passed.

What blocks an update (check() lists it; it can't be applied from the launcher):
  * a change to the PC-PLC data layout (plc/generated/), or to where the PC finds the PLC
    (network_config.json's plc): the PLC program has to match it, in TIA Portal, before the PC reads
    the PLC differently - or the PLC refuses the PC's reads. Applied by hand, after the PLC;
  * new Python packages (requirements.txt): pip can't replace a package the running programs hold
    open. Applied by hand;
  * the GitHub tests failing on the version (or not finished, or not known);
  * this PC's own changes in the way: a change to the running software itself (src/, plc/, config/,
    main_launcher.py, run_simulation.py, requirements.txt, .gitignore: not on the lab branch), a file of
    the PC's - a recipe, data, a script in testing/ - that the update changes too, a file the update adds
    that is here already and isn't the same. An edit to anything that isn't the running software (the
    scripts in testing/, notebooks, docs, tests) is kept as it is, and blocks only when the update
    changes that same file.

This PC's own config changes (the config editor used to commit them, and a file can be edited by
hand) are kept: they become its lab settings (config/lab/, src/core/lab_config.py), and the
repository's files go back to the repository's versions. Its recipes and data stay as they are (one
the update stops tracking in git stays on the PC, untracked). Its own commits stay where they were: a
branch it was on is never moved, and the branch "lab", if it had commits of its own, is kept as
sindie-pc-<time> first.

apply(), in order (everything undone if a step fails):
   1. the machine checked once more, and the services stopped (if they run), so nothing reads half
      an update;
   2. a copy of what the update may change (data/updates/<time>/): this PC's own files it touches,
      git's index, the tag registry, the lab settings;
   3. this version: its tag registry, built by its own code - the one in use must match it, where
      the PLC's tags are - and its own check (--self-check);
   4. this PC's own config changes become lab settings;
   5. the new version: every file it changes written (git checkout <version> -- <those files>), the
      branch "lab" at it and checked out, and each of those files checked: a file a program holds
      open isn't written, and the update stops;
   6. the new version's tag registry, built by the new code: if any PLC tag's address, data type or
      write access differs from this version's, the PC would talk to the PLC differently - not applied;
   7. the new version's own check: everything compiles, every service and program imports, every
      config file (with the lab settings) passes its checks - nothing worse than this version;
   8. the services started again (if they ran), and every service that was ONLINE before ONLINE
      again, with the PLC answering, within two minutes.
If a step fails, the version before is put back - git (the branch, the files the update changed, the
index), this PC's own files, lab settings, registry - each part on its own, and the services are
started again as they were. If any part can't be put back, the services are left stopped and no
launcher starts them by itself (held_up()) until someone has looked. Going back (plan_back) is the
same, the other way.

One update at a time on this PC (the real system's launcher, the simulator's and the command line
share the software): an OS lock on data/updates/update.lock while it runs. Everything an update does
goes into data/updates/<time>/ (update.json: what it did; the copies it kept) and
SystemLogs/launcher.log; data/updates/state.json keeps the last check and the history. Nothing is
ever deleted: a file that has to make way is moved into data/updates/<time>/set_aside/.
"""
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Callable, List, Optional

from src.core import lab_config, paths
from src.core.env import getenv

log = logging.getLogger("launcher.updater")

REMOTE, BRANCH = "origin", "lab"
FETCH_TIMEOUT_S = 90.0
GIT_TIMEOUT_S = 60.0
CHECKOUT_TIMEOUT_S = 300.0
TESTS_TIMEOUT_S = 10.0
CHILD_TIMEOUT_S = 300.0             # building the registry, a version's own check
CHECK_EVERY_S = 24 * 3600.0         # the launcher checks by itself once a day (it never applies by itself)
SILENT_FOR_S = 60.0                 # no service manager this long: the PLC has lost the watchdog and dropped the relay
HEALTHY_WITHIN_S = 120.0            # after the start: the services ONLINE and the PLC answering within this
GIT_BUSY_WAIT_S = 10.0              # another git (PyCharm's, say) holding the index: waited for this long
BACKUP_PREFIX = "sindie-pc-"        # a branch keeping this PC's own commits on "lab" before "lab" is moved

PLC_INTERFACE = ("plc/generated/",)         # the PC-PLC data layout: the PLC program must match it first
PLC_TOOLS = ("plc/",)                       # anything else under plc/: a note
PACKAGES = ("requirements.txt",)            # new Python packages for the programs: by hand
KEEP = ("config/recipes/", "data/")         # this PC's own files: kept as they are
SOFTWARE = ("src/", "plc/", "config/")      # the running software (config/: settings and recipes are told apart first) ...
SOFTWARE_FILES = ("main_launcher.py", "run_simulation.py", "requirements.txt", ".gitignore")   # ... and these
LAB_FOLDER = "config/lab/"                  # this PC's lab settings: never in git
FAULT_BITS = "config/fault_config.json"     # the PLC program sets the bits: a note
NETWORK_CONFIG = "config/network_config.json"   # its "plc": where the PC finds the PLC
NOT_SETTINGS = {"config/system_tags.json", "config/appearance.json", "config/workspace_profiles.json",
                "config/plot_settings.json"}
TRAILERS = re.compile(r"^(Co-Authored-By|Claude-Session|Signed-off-by):", re.IGNORECASE)

READY, BLOCKED, UP_TO_DATE, CANT_CHECK = "ready", "blocked", "up to date", "can't check"
UPDATE, BACK = "update", "back"
PASSED, FAILED, RUNNING, UNKNOWN = "passed", "failed", "running", "unknown"
SEQUENCER = "service_sequencer"
SEQUENCER_SLOTS = ("recipe", "mass_scan", "optimiser")
SEQUENCER_ACTIVE = ("running", "paused", "prompt", "stopping")      # (src/sequencer/protocol.py: ACTIVE)
RELAY = "ion_beam.facilities.safety_relay_active"
SOURCE_COLD = "ion_beam.source.chamber.stat_gv_close_permissive"
BEING_UPDATED = "the software is being updated"


class GitError(RuntimeError):
    pass


class UpdateFailed(RuntimeError):
    pass


# ============================================================================ git
def find_git() -> Optional[str]:
    """git on the PATH, or Git for Windows where it installs itself."""
    exe = shutil.which("git")
    if exe:
        return exe
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramW6432", ""),
                     os.environ.get("ProgramFiles(x86)", ""),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
            exe = os.path.join(base, "Git", "cmd", "git.exe") if base else ""
            if exe and os.path.isfile(exe):
                return exe
    return None


def _no_window() -> dict:
    return {"creationflags": 0x08000000} if os.name == "nt" else {}         # CREATE_NO_WINDOW


def _git_errors(output: bytes, code: int) -> str:
    """What git said went wrong: its error and fatal lines (with the files an error lists), else its
    last line."""
    lines = [ln.rstrip() for ln in (output or b"").decode("utf-8", "replace").splitlines() if ln.strip()]
    found = []
    for line in lines:
        if line.startswith(("error:", "fatal:")):
            found.append(line)
        elif found and line[:1] in ("\t", " "):
            found.append(line.strip())
    shown = found or lines[-1:] or [f"exit code {code}"]
    return "; ".join(shown[:10]) + (f" (and {len(shown) - 10} more)" if len(shown) > 10 else "")


class Git:
    """git in the SINDIE folder. It never asks for anything (GitHub is read as the public repository
    it is: no password, no sign-in window), and opens no console window. Paths are taken literally."""

    def __init__(self, root: str, exe: str = None, run=subprocess.run):
        self.root, self.exe, self.run = root, exe or find_git(), run
        self._dir = None

    def __call__(self, *args, timeout: float = GIT_TIMEOUT_S, check: bool = True, raw: bool = False,
                 paths: Optional[List[str]] = None):
        """git <args>. paths: files for a command that takes them (checkout, rm), given in a file (any
        number of them, with any characters)."""
        if not self.exe:
            raise GitError("git isn't installed (or isn't on the PATH): install Git for Windows")
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", GIT_OPTIONAL_LOCKS="0",
                   GIT_LITERAL_PATHSPECS="1", LC_ALL="C", LANGUAGE="C",
                   GIT_AUTHOR_NAME="SINDIE updater", GIT_COMMITTER_NAME="SINDIE updater",
                   GIT_AUTHOR_EMAIL="sindie-updater@localhost", GIT_COMMITTER_EMAIL="sindie-updater@localhost")
        listed = None
        if paths is not None:
            fd, listed = tempfile.mkstemp(prefix="sindie-git-paths-")
            with os.fdopen(fd, "wb") as f:
                f.write(b"\0".join(p.encode("utf-8") for p in paths))
            args = (*args, f"--pathspec-from-file={listed}", "--pathspec-file-nul")
        log.debug("git %s", " ".join(args))
        started = time.time()
        try:
            r = self.run([self.exe, "-C", self.root, *args], capture_output=True, timeout=timeout, env=env,
                         stdin=subprocess.DEVNULL, **_no_window())
        except subprocess.TimeoutExpired:
            kept = self.set_aside_lock(newer_than=started)      # (git was ended: its lock would stop every git after it)
            raise GitError(f"git {args[0]} took longer than {timeout:.0f} s"
                           + (f" (the lock it left is kept as {kept})" if kept else "")) from None
        except OSError as e:
            raise GitError(f"git couldn't be run: {e}") from None
        finally:
            if listed:
                try:
                    os.remove(listed)               # (the list of paths, written above)
                except OSError:
                    pass
        if check and r.returncode:
            raise GitError(f"git {args[0]} failed: {_git_errors(r.stderr or r.stdout, r.returncode)}")
        return r.stdout if raw else r.stdout.decode("utf-8", "replace")

    def ok(self, *args) -> bool:
        try:
            self(*args)
            return True
        except GitError:
            return False

    def rev(self, ref: str) -> str:
        """The commit `ref` names ('' if none)."""
        return self("rev-parse", "--verify", "--quiet", ref + "^{commit}", check=False).strip()

    def show(self, rev: str, path: str) -> Optional[bytes]:
        """A file as it is in commit `rev` (None if it isn't there)."""
        try:
            return self("show", f"{rev}:{path}", raw=True)
        except GitError:
            return None

    def count(self, *args) -> int:
        out = self("rev-list", "--count", *args).strip()
        return int(out) if out.isdigit() else 0

    def git_dir(self) -> str:
        if self._dir is None:
            found = self("rev-parse", "--git-dir", check=False).strip()
            self._dir = found if not found or os.path.isabs(found) else os.path.join(self.root, found)
        return self._dir

    def index_lock(self) -> str:
        folder = self.git_dir()
        return os.path.join(folder, "index.lock") if folder else ""

    def set_aside_lock(self, newer_than: float = None) -> str:
        """git's index.lock left by a git that was ended (it stops every git command after it):
        renamed index.lock.stale-<time>, never deleted. newer_than: only one made since then. Returns
        its new name ('' if there was none)."""
        lock = self.index_lock()
        try:
            if not lock or not os.path.exists(lock) or (newer_than is not None
                                                        and os.path.getmtime(lock) < newer_than - 1.0):
                return ""
            kept = f"{lock}.stale-{time.strftime('%Y%m%d-%H%M%S')}"
            os.replace(lock, kept)
        except OSError:
            return ""
        log.warning("Update: git's index.lock, left by a git that was ended, set aside as %s", kept)
        return kept


def _z(text: str) -> list:
    return [p for p in text.split("\0") if p]


def local_changes(git: Git) -> dict:
    """{path: status} of the work tree against HEAD: 'M' changed, 'A' added, 'D' deleted, '?' not
    tracked (ignored files are left out)."""
    out, items = {}, _z(git("status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames"))
    i = 0
    while i < len(items):
        xy, path = items[i][:2], items[i][3:]
        if "R" in xy or "C" in xy:                  # (a rename all the same: the next item is where it came from)
            i += 1
            if "R" in xy:
                out[items[i]] = "D"
            xy = "A "
        out[path] = "?" if xy == "??" else "D" if "D" in xy else "A" if "A" in xy else "M"
        i += 1
    return out


def changed_between(git: Git, a: str, b: str) -> dict:
    """{path: 'A'|'M'|'D'} from commit a to commit b."""
    items = _z(git("diff", "--name-status", "-z", "--no-renames", a, b))
    return {path: status[0] for status, path in zip(items[0::2], items[1::2])}


def _differing(git: Git, commit: str, paths) -> List[str]:
    """Which of `paths` (tracked ones) aren't in the work tree as they are in `commit`."""
    wanted = set(paths)
    if not wanted:
        return []
    return sorted(wanted & set(_z(git("diff", "--name-only", "-z", "--no-renames", commit))))


def _in_the_middle(git: Git) -> str:
    """'a merge' (or a rebase...) if git is in the middle of one here, else ''."""
    folder = git.git_dir()
    if not folder:
        return ""
    for name, what in (("MERGE_HEAD", "a merge"), ("rebase-merge", "a rebase"), ("rebase-apply", "a rebase"),
                       ("CHERRY_PICK_HEAD", "a cherry-pick"), ("REVERT_HEAD", "a revert")):
        if os.path.exists(os.path.join(folder, name)):
            return what
    if git("ls-files", "-u", "-z", check=False).strip("\0"):
        return "resolving a conflict"
    return ""


def _wait_for_git(git: Git, wait: float = GIT_BUSY_WAIT_S, set_aside: bool = False) -> str:
    """Another git working here (PyCharm's, say) holds index.lock: wait for it. Still held after
    `wait`: '' if set_aside (going back: it is set aside), else why the update can't go on."""
    lock, end = git.index_lock(), time.monotonic() + wait
    while lock and os.path.exists(lock) and time.monotonic() < end:
        time.sleep(0.25)
    if not lock or not os.path.exists(lock):
        return ""
    if set_aside:
        git.set_aside_lock()
        return ""
    return ("git is busy in the SINDIE folder (.git/index.lock): try again in a minute; if it stays, a git that "
            "was ended left it - ask Josh")


# ============================================================================ files
def _read(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _save(path: str, raw: bytes):
    """Written whole to a temporary file of this thread's own and swapped in."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}-{threading.get_ident()}.update-tmp"
    with open(tmp, "wb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _move_file(src: str, dst: str):
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    shutil.move(src, dst)                   # (a rename; copied, then the original taken away, across drives)


def _aside(root: str, path: str, folder: str, why: str) -> str:
    """A file moved out of the way into the update's folder (never deleted). Returns where it went."""
    dst = os.path.join(folder, "set_aside", why, path)
    n = 1
    while os.path.lexists(dst):
        n += 1
        dst = os.path.join(folder, "set_aside", f"{why}-{n}", path)
    _move_file(os.path.join(root, path), dst)
    log.info("Update: %s moved aside to %s", path, dst)
    return dst


def _json(raw: Optional[bytes]):
    """JSON from a file's bytes, as the programs read it (UTF-8, or the Windows code page); None if
    it isn't JSON."""
    if raw is None:
        return None
    raw = raw[3:] if raw.startswith(b"\xef\xbb\xbf") else raw
    for encoding in ("utf-8", lab_config.FALLBACK_ENCODING, "latin-1"):
        try:
            return json.loads(raw.decode(encoding))
        except UnicodeDecodeError:
            continue
        except ValueError:
            return None
    return None


def _same_text(a: Optional[bytes], b: Optional[bytes]) -> bool:
    """The same file, line endings aside (Git for Windows writes CRLF)."""
    return a is not None and b is not None and a.replace(b"\r\n", b"\n") == b.replace(b"\r\n", b"\n")


def _config_dir(root: str) -> str:
    return os.path.join(root, "config")


def _is_software(path: str) -> bool:
    """Part of the running software: a change to it on the PC needs Josh. Anything else - the scripts
    in testing/, tests, docs, notebooks - is the PC's to change: kept as it is, unless the update
    changes it too (nothing in src/ reads them; the help window only shows docs/)."""
    return path.startswith(SOFTWARE) or path in SOFTWARE_FILES


def _is_setting(path: str) -> bool:
    """A config file of the repository's that can carry lab settings: config/<name>.json (not the
    registry or the per-machine files, nor anything in a sub-folder)."""
    return (path.startswith("config/") and path.endswith(".json") and path.count("/") == 1
            and path not in NOT_SETTINGS)


def updates_dir() -> str:
    """data/updates in the SINDIE folder (the simulator's launcher uses the same: the software is
    shared). SINDIE_DATA_DIR moves it (the tests)."""
    override = getenv("DATA_DIR").strip()
    return os.path.join(override or os.path.join(paths.PROJECT_ROOT, "data"), "updates")


def live_registry() -> str:
    """The real system's tag registry, which an update rebuilds - from the simulator's launcher too
    (the simulator builds its own, data_sim/system_tags.json, at each start): SINDIE_REGISTRY if the
    real system's programs have it, else config/system_tags.json."""
    from_env = getenv("REGISTRY").strip()
    if from_env and not paths.simulation_enabled():
        return from_env
    return os.path.join(paths.PROJECT_ROOT, "config", "system_tags.json")


# ============================================================================ one update at a time
def _lock_file(f):
    if os.name == "nt":
        import msvcrt
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(f):
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


class FileLock:
    """An OS lock on a file in data/updates: held by one process (and one holder in it) at a time,
    and let go of by itself if the process ends."""

    def __init__(self, name: str):
        self.path, self.f = os.path.join(updates_dir(), name), None

    def acquire(self, wait: float = 0.0) -> bool:
        end = time.monotonic() + wait
        while True:
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                f = open(self.path, "a+b")
            except OSError as e:
                log.warning("Couldn't open %s: %s", self.path, e)
                return False
            try:
                _lock_file(f)
            except OSError:
                f.close()
                if time.monotonic() >= end:
                    return False
                time.sleep(0.05)
                continue
            self.f = f
            return True

    def release(self):
        if self.f is not None:
            _unlock_file(self.f)
            self.f.close()
            self.f = None

    def held_elsewhere(self) -> bool:
        """Another holder has it (another launcher's update, the command line's, or this process's
        own update in its thread)."""
        if self.f is not None:
            return False
        if self.acquire():
            self.release()
            return False
        return True


def update_lock() -> FileLock:
    return FileLock("update.lock")


# ============================================================================ what an update brings
@dataclass
class Commit:
    sha: str
    short: str
    date: str               # ISO 8601, the committer's
    author: str
    subject: str
    body: str = ""


@dataclass
class Plan:
    root: str
    checked_at: float = 0.0
    state: str = CANT_CHECK
    error: str = ""                                     # why it couldn't be checked
    kind: str = UPDATE                                  # UPDATE: along the lab branch; BACK: to the version before
    branch: str = ""                                    # the branch checked out here ("" detached)
    current: str = ""                                   # this PC's version (HEAD)
    current_date: str = ""
    target: str = ""                                    # the version it would go to
    target_date: str = ""
    base: str = ""                                      # what this PC's own changes are counted from
    commits: List[Commit] = field(default_factory=list)  # what it brings (BACK: what it takes away), newest first
    files: dict = field(default_factory=dict)          # {path: A/M/D}, base to target
    moving: dict = field(default_factory=dict)         # {path: A/M/D}, this PC's version to target: what is written
    blockers: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    adopt: List[str] = field(default_factory=list)      # this PC's config changes, to become lab settings
    keep: List[str] = field(default_factory=list)       # this PC's own recipes and data, kept as they are
    hold: List[str] = field(default_factory=list)       # its recipes and data the update stops tracking, or adds too: they stay
    aside: List[str] = field(default_factory=list)      # files here the update adds, the same: set aside for the update's
    own_commits: int = 0                                # this PC's commits that the lab branch doesn't have
    tests: str = UNKNOWN                                # the GitHub tests on the target

    @property
    def available(self) -> bool:
        return self.state in (READY, BLOCKED)

    def summary(self) -> str:
        if self.state == CANT_CHECK:
            return f"Couldn't check for updates: {self.error}"
        if self.state == UP_TO_DATE:
            return "Up to date"
        if self.kind == BACK:
            return f"Going back to {self.target[:7]}" + (" can't be done from here" if self.state == BLOCKED else "")
        n = len(self.commits)
        return f"An update is available: {n} change{'s' if n != 1 else ''}" + (
            " (it can't be applied from here)" if self.state == BLOCKED else "")

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "Plan":
        known = set(cls.__dataclass_fields__)
        d = {k: v for k, v in dict(d).items() if k in known}
        d["commits"] = [Commit(**c) for c in d.get("commits", []) if isinstance(c, dict)]
        return cls(**d)


def _commits(git: Git, revs: str) -> List[Commit]:
    fmt = "%H%x1f%h%x1f%cI%x1f%an%x1f%s%x1f%b%x1e"
    out = []
    for record in git("log", f"--format={fmt}", revs).split("\x1e"):
        parts = record.strip("\n").split("\x1f")
        if len(parts) >= 5:
            body = "\n".join(ln for ln in (parts[5] if len(parts) > 5 else "").splitlines() if not TRAILERS.match(ln))
            out.append(Commit(*parts[:5], body=body.strip()))
    return out


def github_repo(url: str) -> Optional[str]:
    """'owner/repo' of a GitHub remote's URL (https or ssh), else None."""
    m = re.search(r"github\.com[:/]+([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", url.strip())
    return f"{m[1]}/{m[2]}" if m else None


def tests_status(repo: Optional[str], sha: str, opener=urllib.request.urlopen) -> str:
    """The GitHub tests of a version: PASSED (each workflow passed in a run of it on this commit),
    FAILED (one failed, or was cancelled, in every run of it), RUNNING, or UNKNOWN (none run yet, or
    GitHub didn't answer). A workflow's own conclusion counts: a job skipped because the one before
    it failed makes the workflow fail."""
    if not repo or not sha:
        return UNKNOWN
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/actions/runs?head_sha={sha}&per_page=100",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "SINDIE-updater"})
    try:
        with opener(req, timeout=TESTS_TIMEOUT_S) as r:
            answer = json.loads(r.read().decode("utf-8"))
    except (OSError, ValueError, AttributeError) as e:          # (urllib.error.URLError is an OSError)
        log.info("GitHub tests of %s not known: %s", sha[:7], e)
        return UNKNOWN
    runs = answer.get("workflow_runs") if isinstance(answer, dict) else None
    workflows = {}
    for run in runs if isinstance(runs, list) else []:
        if isinstance(run, dict):
            workflows.setdefault(str(run.get("name") or run.get("workflow_id")), []).append(run)
    if not workflows:
        return UNKNOWN
    outcome = PASSED
    for tries in workflows.values():
        # The same commit can be tested more than once (pushed to another branch too, or run again): one pass is a pass
        if any(t.get("status") == "completed" and t.get("conclusion") == "success" for t in tries):
            continue
        if any(t.get("status") != "completed" for t in tries):
            outcome = FAILED if outcome == FAILED else RUNNING
        else:
            outcome = FAILED
    return outcome


def _cached_tests(repo: Optional[str], sha: str) -> str:
    """tests_status(), remembered once it has PASSED (GitHub answers 60 questions an hour per
    address). A failure is asked again: a run made again can pass."""
    known = load_state().get("tests")
    if isinstance(known, dict) and known.get(sha) == PASSED:
        return PASSED
    status = tests_status(repo, sha)
    if status == PASSED:
        def remember(state):
            done = state.get("tests") if isinstance(state.get("tests"), dict) else {}
            done[sha] = PASSED
            state["tests"] = dict(list(done.items())[-50:])
        _change(remember)
    return status


def version(root: str = None, git: Git = None) -> dict:
    """This PC's version: {'sha', 'short', 'date', 'subject', 'branch'} ({} if git can't say)."""
    git = git or Git(root or paths.PROJECT_ROOT)
    try:
        sha, date, subject = (git("log", "-1", "--format=%H%x1f%cI%x1f%s", "HEAD").strip().split("\x1f") + ["", ""])[:3]
        branch = git("symbolic-ref", "--short", "-q", "HEAD", check=False).strip()
    except GitError:
        return {}
    return {"sha": sha, "short": sha[:7], "date": date, "subject": subject, "branch": branch} if sha else {}


def check(root: str = None, git: Git = None, fetch: bool = True, tests=_cached_tests, clock=time.time) -> Plan:
    """What an update would bring, and what would stand in its way. Fetches the lab branch from
    GitHub (fetch=False: what was fetched last); changes nothing else."""
    root = root or paths.PROJECT_ROOT
    plan = Plan(root=root, checked_at=clock())
    git = git or Git(root)
    try:
        if git("rev-parse", "--is-inside-work-tree", check=False).strip() != "true":
            raise GitError(f"the SINDIE folder ({root}) isn't a git checkout")
        if fetch:
            try:
                git("fetch", "--quiet", "--no-tags", REMOTE, f"+refs/heads/{BRANCH}:refs/remotes/{REMOTE}/{BRANCH}",
                    timeout=FETCH_TIMEOUT_S)
            except GitError as e:
                if "couldn't find remote ref" in str(e):
                    raise GitError(f"there is no '{BRANCH}' branch on GitHub yet") from None
                raise GitError(f"GitHub couldn't be reached ({e})") from None
        _versions(plan, git, f"refs/remotes/{REMOTE}/{BRANCH}")
        plan.base = git("merge-base", "HEAD", plan.target, check=False).strip()
        if not plan.base:
            raise GitError(f"this PC's version and the '{BRANCH}' branch have no history in common")
    except GitError as e:
        plan.error = str(e)
        return plan
    plan.own_commits = git.count(f"{plan.target}..HEAD")
    if git.count(f"HEAD..{plan.target}") == 0:
        plan.state = UP_TO_DATE
        if plan.own_commits:
            plan.notes.append(f"This PC's version has {plan.own_commits} commit(s) that the '{BRANCH}' branch "
                              "doesn't have.")
        return plan
    plan.commits = _commits(git, f"{plan.base}..{plan.target}")
    plan.files = changed_between(git, plan.base, plan.target)
    _judge(plan, git)
    gone_back = load_state().get("went_back_from") or {}
    if isinstance(gone_back, dict) and gone_back.get("sha") == plan.target:
        plan.notes.append(f"This PC went back from this version on {gone_back.get('when', 'an earlier day')}: ask "
                          "Josh before updating to it again.")
    url = git("remote", "get-url", REMOTE, check=False).strip()
    plan.tests = tests(github_repo(url), plan.target) if tests else UNKNOWN
    if plan.tests == FAILED:
        plan.blockers.append("The GitHub tests failed on this version: it isn't for the lab. Tell Josh.")
    elif plan.tests == RUNNING:
        plan.blockers.append("The GitHub tests on this version haven't finished yet: check again in a quarter "
                             "of an hour.")
    elif plan.tests != PASSED:
        plan.blockers.append("Whether the GitHub tests passed on this version couldn't be found out (GitHub didn't "
                             "answer, or they haven't run): check again later.")
    plan.state = BLOCKED if plan.blockers else READY
    return plan


def _versions(plan: Plan, git: Git, target_ref: str):
    plan.current = git.rev("HEAD")
    if not plan.current:
        raise GitError("this PC's version isn't known (git has no HEAD)")
    plan.current_date = git("log", "-1", "--format=%cI", plan.current).strip()
    plan.branch = git("symbolic-ref", "--short", "-q", "HEAD", check=False).strip()
    plan.target = git.rev(target_ref)
    if not plan.target:
        raise GitError(f"there is no '{BRANCH}' branch from GitHub here: check for updates first")
    plan.target_date = git("log", "-1", "--format=%cI", plan.target).strip()


def _judge(plan: Plan, git: Git):
    """Blockers and notes: the PLC, packages, and this PC's own changes against the update's."""
    files, root = plan.files, plan.root
    cfg = _config_dir(root)
    middle = _in_the_middle(git)
    if middle:
        plan.blockers.append(f"git is in the middle of {middle} in the SINDIE folder: ask Josh.")
    plc = sorted(p for p in files if p.startswith(PLC_INTERFACE))
    if plc:
        plan.blockers.append("It changes the PC-PLC data layout (" + ", ".join(plc) + "): the PLC program has to "
                             "match it first, in TIA Portal, or the PLC refuses the PC's reads. Josh applies such a "
                             "version by hand, after the PLC.")
    _plc_address(plan, git)
    tools = sorted(p for p in files if p.startswith(PLC_TOOLS) and p not in plc)
    if tools:
        plan.notes.append("It changes the PLC tools (" + ", ".join(tools) + "), not what the PC reads from the PLC.")
    if FAULT_BITS in files:
        plan.notes.append("It changes the PLC fault list (fault_config.json): its names and texts - the PLC program "
                          "sets the bits.")
    if any(p in files for p in PACKAGES):
        plan.blockers.append("It needs other Python packages (requirements.txt): they can't be installed while "
                             "SINDIE's programs run. Josh applies such a version by hand.")
    if any(p.startswith(LAB_FOLDER) for p in files):
        plan.blockers.append("It has files in config/lab/, where this PC keeps its own settings (that folder is "
                             "never in git): tell Josh.")

    # What the move writes (from this PC's version, its own commits included), and files of this PC's
    # in the way of ones it adds (not tracked here, or ignored)
    moving = plan.moving = changed_between(git, plan.current, plan.target)
    arriving = sorted(p for p, s in moving.items() if s == "A" and os.path.lexists(os.path.join(root, p)))

    # This PC's own changes: in its own commits, and not committed
    local = local_changes(git)
    committed = changed_between(git, plan.base, "HEAD") if plan.own_commits else {}
    own = dict(committed)
    for path, status in local.items():
        if status != "?" and path not in arriving:
            own.setdefault(path, status)
    software_here, software_committed, coming_back = [], [], []
    for path, status in sorted(own.items()):
        here = os.path.join(root, path)
        if _is_setting(path):
            _judge_setting(plan, path, status)
        elif path.startswith(KEEP) or not _is_software(path):
            if path in files and files[path] != "D":
                if status != "D" and _same_text(_read(here), git.show(plan.target, path)):
                    plan.keep.append(path)          # (the update's version is this PC's: nothing changes)
                elif status == "D" and not path.startswith(KEEP):
                    plan.keep.append(path)          # (a script deleted here: the update's comes, nothing is lost)
                    coming_back.append(path)
                elif status == "D":
                    plan.blockers.append(f"{path} has been deleted on this PC, and the update changes it: put it back "
                                         f"(git checkout -- {path}, in the SINDIE folder) or ask Josh, then check "
                                         "again.")
                else:
                    plan.blockers.append(f"{path} has been changed on this PC, and the update changes it too: it would "
                                         "be overwritten. Save your version under another name (or ask Josh), then "
                                         "check again.")
            elif path not in files:
                plan.keep.append(path)
                if status == "D" and path in moving:
                    coming_back.append(path)
            # (the update stops tracking it: if it is here, it is held below; a deletion here needs nothing)
        else:
            (software_committed if path in committed else software_here).append(path)
    if coming_back:
        plan.notes.append(f"{_files(coming_back)} deleted on this PC come back, as the repository has them.")
    # This PC's recipes and data that the update stops tracking, its other files of its own (scripts,
    # notes) it changed that the update takes away, and its files the update adds too: they stay as they are
    gone = [p for p, s in moving.items() if s == "D" and os.path.lexists(os.path.join(root, p))
            and (p.startswith(KEEP) or (not _is_software(p) and p in own))]
    theirs_too = [p for p in arriving if p.startswith(KEEP) or not _is_software(p)]
    plan.hold = sorted(set(gone) | set(theirs_too))
    if gone:
        plan.notes.append(f"The update stops tracking {_files(sorted(gone))} in git: "
                          + ("it stays on this PC, as it is." if len(gone) == 1 else "they stay on this PC, as they are."))
    differing = [p for p in theirs_too if not _same_text(_read(os.path.join(root, p)), git.show(plan.target, p))]
    if differing:
        plan.notes.append(f"The update brings {_files(differing)}, which this PC has a version of its own of: this "
                          "PC's is kept.")
    for found, where in ((software_here, "changes of its own to the software"),
                         (software_committed, f"software changes in its version that the '{BRANCH}' branch doesn't "
                                              "have")):
        if found:
            shown = ", ".join(found[:6]) + (f" and {len(found) - 6} more" if len(found) > 6 else "")
            plan.blockers.append(f"This PC has {where}: {shown}. Ask Josh before updating.")

    # Other files here that the update adds: the same file is set aside for it; another one is in the way
    for path in arriving:
        if path in theirs_too:
            continue
        here = os.path.join(root, path)
        if os.path.isfile(here) and not os.path.islink(here) and _same_text(_read(here), git.show(plan.target, path)):
            plan.aside.append(path)
        else:
            plan.blockers.append(f"The update adds {path}, but this PC has a file of that name already: move it "
                                 "somewhere else (or ask Josh), then check again.")
    # This PC's lab settings
    for name in _lab_files(cfg):
        try:
            lab_config.lab_values(name, cfg)
        except lab_config.ConfigError as e:
            plan.blockers.append(f"This PC's lab settings for {name} can't be read ({e}): put them right in the config "
                                 "editor (History...) or ask Josh.")
        if files.get(f"config/{name}") == "D" and f"config/{name}" not in plan.adopt:
            plan.blockers.append(f"The update removes config/{name}, but this PC has lab settings for it "
                                 f"(config/lab/{name}): ask Josh.")
    if plan.adopt:
        plan.notes.append("This PC's own changes to " + ", ".join(os.path.basename(p) for p in plan.adopt)
                          + " are kept, as its lab settings (config/lab/).")
    if plan.own_commits and plan.kind == UPDATE:
        plan.notes.append(f"This PC's version has {plan.own_commits} commit(s) of its own: they are kept "
                          f"({'on a branch of their own' if plan.branch == BRANCH else f'on {plan.branch!r}'}).")
    _both_changed(plan, git)


def _plc_address(plan: Plan, git: Git):
    """A blocker if the move changes where the PC finds the PLC (network_config.json's plc: its
    address, rack, slot, data blocks)."""
    before, after = _json(git.show(plan.current, NETWORK_CONFIG)), _json(git.show(plan.target, NETWORK_CONFIG))
    a = before.get("plc") if isinstance(before, dict) else None
    b = after.get("plc") if isinstance(after, dict) else None
    if a == b:
        return
    a, b = a if isinstance(a, dict) else {}, b if isinstance(b, dict) else {}
    keys = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k)) or ["plc"]
    plan.blockers.append(f"It changes where the PC finds the PLC (network_config.json: plc {', '.join(keys)}): Josh "
                         "applies such a version by hand.")


def _files(paths: list) -> str:
    """'config/recipes/a.json', or '12 files (in data/daily_logs, data/parquet_logs)'."""
    if len(paths) == 1:
        return paths[0]
    folders = sorted({os.path.dirname(p) for p in paths})
    return f"{len(paths)} files (in {', '.join(folders[:4])}{' ...' if len(folders) > 4 else ''})"


def _judge_setting(plan: Plan, path: str, status: str):
    name, cfg = os.path.basename(path), _config_dir(plan.root)
    if status == "D":
        plan.blockers.append(f"{path} has been deleted on this PC: put it back (git checkout -- {path}, in the "
                             "SINDIE folder), then check again.")
        return
    if plan.files.get(path) == "D":
        plan.blockers.append(f"The update removes {path}, which has been changed on this PC: ask Josh.")
        return
    if not isinstance(_json(_read(os.path.join(plan.root, path))), dict):
        plan.blockers.append(f"{path} has been changed on this PC and can't be read as a config file: put it back "
                             f"(git checkout -- {path}, in the SINDIE folder), or ask Josh.")
        return
    try:
        lab_config.lab_values(name, cfg)
    except lab_config.ConfigError:
        return                                      # (a blocker of its own: _judge)
    plan.adopt.append(path)


def _lab_files(cfg: str) -> list:
    try:
        return sorted(n for n in os.listdir(lab_config.lab_dir(cfg)) if n.endswith(".json"))
    except OSError:
        return []


def _both_changed(plan: Plan, git: Git):
    """A note for each config file the update changes where this PC has values of its own."""
    cfg = _config_dir(plan.root)
    for path in sorted(p for p, s in plan.files.items() if s == "M" and _is_setting(p)):
        name = os.path.basename(path)
        try:
            ours = set(lab_config.changed_keys(lab_config.lab_values(name, cfg)))
        except lab_config.ConfigError:
            continue
        before = _json(git.show(plan.base, path))
        if path in plan.adopt:
            here = _json(_read(os.path.join(plan.root, path)))
            if isinstance(before, dict) and isinstance(here, dict):
                ours |= set(lab_config.changed_keys(lab_config.diff(before, here)))
        after = _json(git.show(plan.target, path))
        if ours and isinstance(before, dict) and isinstance(after, dict):
            theirs = set(lab_config.changed_keys(lab_config.diff(before, after)))
            both = sorted(k for k in ours if any(k == t or k.startswith(t + ".") or t.startswith(k + ".")
                                                 for t in theirs))
            if both:
                what = "The update changes" if plan.kind == UPDATE else "Going back changes"
                plan.notes.append(f"{what} {name} where this PC has values of its own ({', '.join(both)}): this PC's "
                                  "values are kept.")


def plan_back(root: str = None, git: Git = None, clock=time.time) -> Plan:
    """Going back to the version before the last update (made here, and still what this PC runs)."""
    root = root or paths.PROJECT_ROOT
    plan = Plan(root=root, checked_at=clock(), kind=BACK)
    git = git or Git(root)
    last = last_update()
    try:
        if not last:
            raise GitError("no update has been made from the launcher here")
        _versions(plan, git, last["from"])
        if plan.current != last.get("to"):
            raise GitError(f"this PC's version isn't the one the last update made ({last.get('to', '')[:7]})")
    except GitError as e:
        plan.error = str(e)
        return plan
    plan.base = plan.current
    plan.commits = _commits(git, f"{plan.target}..{plan.current}")
    plan.files = changed_between(git, plan.current, plan.target)
    _judge(plan, git)
    plan.tests = PASSED                             # (it ran here before)
    plan.state = BLOCKED if plan.blockers else READY
    return plan


# ============================================================================ is the machine safe?
def machine_reasons(view: dict, running: bool, silent_for: Optional[float], programs_open: List[str],
                    other_running: bool = False, other_silent_for: Optional[float] = None,
                    simulation: bool = False, strays: List[str] = (), plc_heard: bool = False,
                    others: List[str] = ()) -> List[str]:
    """Why the software can't be updated now ([] = it can).

    view: this system's fresh telemetry (the launcher's). running: this system's service manager
    runs; silent_for: how long it hasn't (None: not known). With the services running, the PLC's own
    data must say the safety relay is off and the source cold (the gate valve's close permissive:
    extraction and target off, filament and caesium cold), and the service manager and the
    sequencer must say no recipe, scan or optimiser runs. With them stopped for SILENT_FOR_S, the PLC
    has lost the PC's watchdog and has switched the relay off itself (it closes the gate valve, which
    opens the relay) - unless a service still runs without the manager: strays (its processes), or
    plc_heard (its PLC data still arriving). The software is shared by the systems on this PC:
    other_running is the other system's services running (from the real system's launcher: a
    simulator's; from the simulator's: the real system's, whose silence other_silent_for must also
    have lasted SILENT_FOR_S), others names the other systems running. The HMI and the data viewer
    must be closed: they run the version before."""
    out = []
    if running:
        if not view.get("system.connected"):
            out.append("The PLC isn't reporting, so the safety relay's state isn't known.")
        else:
            relay, cold = view.get(RELAY), view.get(SOURCE_COLD)
            if relay is None:
                out.append("The safety relay's state isn't known.")
            elif relay:
                out.append("The safety relay is on: switch it off first (System Health panel).")
            if cold is None:
                out.append("Whether the source is cold isn't known.")
            elif not cold:
                out.append("The source isn't cold yet (high voltage on, or the filament or caesium hot): shut it "
                           "down and let it cool first.")
        states = view.get("manager.services")
        if not isinstance(states, dict) or not states:
            out.append("The service manager isn't reporting, so what is running isn't known.")
        elif states.get(SEQUENCER) == "ONLINE" and not any(f"sequencer.{s}.state" in view for s in SEQUENCER_SLOTS):
            out.append("Whether a recipe, scan or optimiser is running isn't known: the sequencer isn't reporting.")
        for slot in SEQUENCER_SLOTS:
            if view.get(f"sequencer.{slot}.state") in SEQUENCER_ACTIVE:
                out.append(f"A {slot.replace('_', ' ')} is running: let it finish, or stop it, first.")
    elif strays:
        out.append(f"SINDIE services are running without the service manager ({', '.join(strays)}), so the PLC may "
                   "still have the safety relay on: press Start services (the service manager ends them as it starts), "
                   "then Stop services... and wait a minute. Or call Josh.")
    elif plc_heard:
        out.append("The PLC's data is still arriving though the service manager isn't running: a SINDIE service runs "
                   "without it, so the PLC may still have the safety relay on. Call Josh.")
    elif silent_for is None or silent_for < SILENT_FOR_S:
        out.append(f"Wait {SILENT_FOR_S - (silent_for or 0.0):.0f} s more: the services have to have been stopped for a "
                   "minute, so that the PLC has the safety relay off.")
    if simulation:
        if other_running:
            out.append("The real system's services are running: update from the real system's launcher.")
        elif other_silent_for is None or other_silent_for < SILENT_FOR_S:
            out.append(f"Wait {SILENT_FOR_S - (other_silent_for or 0.0):.0f} s more: the real system's services have to "
                       "have been stopped for a minute.")
        if others:
            out.append(f"Other SINDIE services are running on this PC ({', '.join(others)}): close them first - they "
                       "share the software.")
    elif other_running or others:
        out.append(f"Other SINDIE services are running on this PC ({', '.join(others) or 'the simulator'}): close them "
                   "first - they share the software.")
    for name in programs_open:
        out.append(f"The {name} is open: close it first.")
    return out


# ============================================================================ applying
@dataclass
class Hooks:
    """What apply() asks of the launcher (fakes in the tests). Those returning str return '' if it
    worked, else what went wrong."""
    services_running: Callable[[], bool]
    online: Callable[[], Optional[set]]                 # the services ONLINE now (None: not known)
    stop_services: Callable[[bool], str]               # (True, going back: end them if the manager doesn't answer)
    start_services: Callable[[], str]
    healthy: Callable[[float, set], str]                # wait (up to the time given) for these ONLINE + the PLC
    build_registry: Callable[[str], str]                # the tag registry of the version on disk, into this file
    self_check: Callable[[], List[str]]                 # the version on disk's own check; raises UpdateFailed if it can't run
    safe_now: Callable[[], List[str]] = lambda: []      # the machine's conditions once more, as it starts ([] = go)


@dataclass
class Report:
    kind: str = UPDATE
    ok: bool = False
    rolled_back: bool = False                           # (after a failure) the version before is back
    message: str = ""
    advice: str = ""                                    # what the operator does next
    steps: List[str] = field(default_factory=list)
    folder: str = ""
    previous: str = ""
    now: str = ""


def plc_interface(registry: dict) -> dict:
    """{tag: (DB, byte, bit, type, writable)} of the PLC's tags: what the PC reads and writes where."""
    return {tag: (m.get("db_number"), m.get("byte_offset"), m.get("bit_offset"), m.get("datatype"),
                  bool(m.get("writable", True)))
            for tag, m in registry.items() if isinstance(m, dict) and m.get("source") == "service_plc_snap7"}


def plc_differences(old: dict, new: dict) -> List[str]:
    a, b = plc_interface(old), plc_interface(new)
    out = [f"{t}: added" for t in sorted(set(b) - set(a))] + [f"{t}: removed" for t in sorted(set(a) - set(b))]
    return out + [f"{t}: {a[t]} -> {b[t]}" for t in sorted(set(a) & set(b)) if a[t] != b[t]]


def _listed(items: List[str], most: int = 4) -> str:
    return "; ".join(items[:most]) + (f"; and {len(items) - most} more" if len(items) > most else "")


class _Undo:
    """What the update has done so far, to put back."""

    def __init__(self, git: Git, plan: Plan, folder: str, registry_path: str):
        self.git, self.plan, self.folder, self.registry_path = git, plan, folder, registry_path
        self.services_were_running = False
        self.online_before = set()
        self.services_stopped = False
        self.services_started = False
        self.copies = {}                # path -> this PC's bytes before (None: it wasn't there): its config changes, its own files the move writes
        self.index_tree = ""            # git's index before, as a tree (git write-tree)
        self.held = []                  # this PC's files held out of git's way (in <folder>/held) now
        self.aside = {}                 # path -> where: files the same as the update's, set aside for them
        self.lab_backup = {}            # lab file name -> its bytes before (None: there wasn't one)
        self.registry = None            # the registry's bytes before
        self.refs_taken = False         # what follows was read (the branches are about to change)
        self.lab_before = ""            # where the branch "lab" was ('' none)
        self.lab_upstream = ("", "")    # its branch.lab.remote and .merge before
        self.backup = ""                # the branch made to keep this PC's own commits on "lab"
        self.move_started = False


def _seed(path: str, registry: Optional[bytes]):
    """A registry is built on the one in use: the builder keeps its hand-edited fields (labels,
    units, scaling: src/utils/build_registry.py), as it does building in place."""
    if registry is not None:
        _save(path, registry)


def _new_folder(root: str, stamp: str) -> str:
    folder, n = os.path.join(root, stamp), 1
    while os.path.exists(folder):
        n += 1
        folder = os.path.join(root, f"{stamp}-{n}")
    os.makedirs(folder)
    return folder


def apply(plan: Plan, hooks: Hooks, step: Callable[[str], None] = lambda s: None, git: Git = None,
          user: str = "", clock=time.time, registry_path: str = None, folder_root: str = None) -> Report:
    """Apply a plan from check() or plan_back() (see the module's doc). On a failure the version
    before is put back; the report says what happened."""
    report = Report(kind=plan.kind, previous=plan.current)
    if plan.state != READY:
        report.message = "This can't be applied from here." if plan.state == BLOCKED else "There is nothing to apply."
        return report
    git = git or Git(plan.root)
    lock = update_lock()
    if not lock.acquire(wait=2.0):
        report.message = ("another update is under way on this PC (from another launcher, or the command line): wait "
                          "for it to finish")
        return report
    try:
        why = _not_now(plan, hooks, git)
        if why:
            report.message = why
            log.info("Update to %s not started: %s", plan.target[:7], why)
            return report
        return _apply(plan, hooks, step, git, user, clock, registry_path, folder_root, report)
    finally:
        lock.release()


def _not_now(plan: Plan, hooks: Hooks, git: Git) -> str:
    """Why the update can't start after all ('' it can): something has moved since the check, the
    machine isn't safe now, or another git is busy here. Nothing has been done."""
    try:
        _still_as_checked(plan, git)
        not_now = list(hooks.safe_now() or [])
        if not_now:
            return "not now: " + " ".join(not_now)
        return _wait_for_git(git)
    except (UpdateFailed, GitError) as e:
        return str(e)


def _apply(plan, hooks, step, git, user, clock, registry_path, folder_root, report) -> Report:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(clock()))
    folder = _new_folder(folder_root or updates_dir(), stamp)
    report.folder = folder
    undo = _Undo(git, plan, folder, registry_path or live_registry())
    going = "the new version" if plan.kind == UPDATE else "the version before"

    def did(text):
        report.steps.append(text)
        log.info("Update: %s", text)
        _record(folder, {"steps": report.steps}, merge=True)
        step(text)

    _record(folder, {"kind": plan.kind, "from": plan.current, "branch": plan.branch, "to": plan.target, "by": user,
                     "started": stamp, "adopt": plan.adopt, "keep": plan.keep, "hold": plan.hold, "aside": plan.aside,
                     "commits": len(plan.commits)})
    try:
        # 1. The services (the machine was checked once more: _not_now)
        undo.services_were_running = hooks.services_running()
        if undo.services_were_running:
            online = hooks.online()
            if online is None:
                raise UpdateFailed("which services are ONLINE isn't known (the service manager hasn't reported just "
                                   "now): try again in a moment")
            undo.online_before = set(online)
            did("Stopping the services")
            problem = hooks.stop_services(False)
            if problem:
                raise UpdateFailed(f"the services didn't stop: {problem}")
            undo.services_stopped = True
        # 2. Copies of what this update may change
        _snapshot(undo)
        # 3. This version: its registry (the one in use must match it), its own check
        did("Checking this version (its tag registry, its own check)")
        before = os.path.join(folder, "registry_before.json")
        _seed(before, undo.registry)
        problem = _build_registry(undo, hooks, before, did)
        if problem:
            if undo.registry is None:
                raise UpdateFailed(f"this version's tag registry can't be built ({problem}), so the PLC tags can't be "
                                   "compared")
            _save(before, undo.registry)
            did(f"(this version's registry couldn't be built - {problem} - the one in use is compared instead)")
        in_use, built = _json(undo.registry), _json(_read(before))
        if not isinstance(built, dict):
            raise UpdateFailed("this version's tag registry can't be read")
        stale = plc_differences(in_use, built) if isinstance(in_use, dict) else []
        if stale:
            raise UpdateFailed(f"the tag registry in use doesn't match what this version builds, where the PLC's tags are "
                               f"({_listed(stale)}): Josh rebuilds it by hand first (python src/utils/build_registry.py), "
                               "checking it against the PLC")
        try:
            problems_before = set(hooks.self_check())
        except UpdateFailed as e:                   # (a version from before the updater: it has no check)
            log.info("Update: this version can't check itself: %s", e)
            problems_before = set()
            did("(this version can't check itself: the new one has to pass its check without a single problem)")
        # 4. This PC's own config changes become lab settings
        _keep_own_settings(undo, did)
        # 5. The new version
        did(f"Going to {going} ({plan.target[:7]})")
        _move(undo, did, f"{BACKUP_PREFIX}{os.path.basename(folder)}")
        # 6. Its registry, and the PLC's tags in it
        did("Building its tag registry")
        after = os.path.join(folder, "registry_after.json")
        _seed(after, undo.registry)
        problem = _build_registry(undo, hooks, after, did)
        if problem:
            raise UpdateFailed(f"its tag registry couldn't be built: {problem}")
        new_registry = _json(_read(after))
        if not isinstance(new_registry, dict):
            raise UpdateFailed("its tag registry can't be read")
        differences = plc_differences(built, new_registry)
        if differences:
            raise UpdateFailed(f"it would read or write the PLC differently ({_listed(differences)}): the PLC program "
                               "has to match it first")
        did("The PLC's tags are where they were")
        _save(undo.registry_path, _read(after))
        # 7. Its own check
        did("Checking it (everything compiles and imports, the config files pass)")
        try:
            new = [p for p in hooks.self_check() if p not in problems_before]
        except UpdateFailed as e:
            if plan.kind != BACK:
                raise
            log.info("Update: the version before can't check itself: %s", e)
            did("(the version before can't check itself - it is from before the updater - but it ran on this PC "
                "before)")
            new = []
        if new:
            raise UpdateFailed("it didn't pass its own check: " + "; ".join(new[:3])
                               + (f"; and {len(new) - 3} more" if len(new) > 3 else ""))
        # 8. The services again
        if undo.services_were_running:
            did("Starting the services")
            undo.services_started = True
            problem = hooks.start_services()
            if problem:
                raise UpdateFailed(f"the services didn't start: {problem}")
            did("Waiting for every service and the PLC")
            problem = hooks.healthy(HEALTHY_WITHIN_S, undo.online_before)
            if problem:
                raise UpdateFailed(f"it didn't come up: {problem}")
    except BaseException as e:      # (anything at all, Ctrl+C too: the version before must come back)
        what = "Update" if plan.kind == UPDATE else "Going back"
        if isinstance(e, (UpdateFailed, GitError)):
            report.message = str(e)
            log.error("%s to %s failed: %s", what, plan.target[:7], e)
        elif isinstance(e, Exception):
            report.message = f"{type(e).__name__}: {e}"
            log.exception("%s to %s failed", what, plan.target[:7])
        else:
            report.message = f"it was interrupted ({type(e).__name__})"
            log.error("%s to %s interrupted: %s", what, plan.target[:7], type(e).__name__)
        _roll_back(undo, hooks, did, report)
        _record(folder, {"failed": report.message, "rolled_back": report.rolled_back}, merge=True)
        _history({"at": clock(), "kind": plan.kind, "by": user, "from": plan.current, "to": plan.target, "ok": False,
                  "message": report.message, "advice": report.advice, "rolled_back": report.rolled_back,
                  "folder": folder})
        if not isinstance(e, Exception):
            raise
        return report
    report.ok, report.now = True, plan.target
    report.message = (f"Updated to {plan.target[:7]}." if plan.kind == UPDATE else
                      f"Back at {plan.target[:7]}, the version before the update.")
    report.advice = ("Press RESET FAULTS in System Health: stopping the services latched PC_Watchdog_Timeout in "
                     "the PLC." if undo.services_were_running else
                     "The services were stopped before, and are left stopped: start them when you're ready, and "
                     "check that every one comes ONLINE.")
    did(report.message)
    _record(folder, {"finished": time.strftime("%Y%m%d-%H%M%S", time.localtime(clock()))}, merge=True)
    _history({"at": clock(), "kind": plan.kind, "by": user, "from": plan.current, "to": plan.target, "ok": True,
              "message": report.message, "advice": report.advice, "folder": folder})
    if plan.kind == BACK:
        change_state(went_back_from={"sha": plan.current, "when": time.strftime("%d %b %Y", time.localtime(clock()))})
    return report


def _still_as_checked(plan: Plan, git: Git):
    """Nothing has moved since the check: not this PC's version, not the target, not its own files."""
    target_ref = f"refs/remotes/{REMOTE}/{BRANCH}" if plan.kind == UPDATE else plan.target
    branch = git("symbolic-ref", "--short", "-q", "HEAD", check=False).strip()
    if git.rev("HEAD") != plan.current or git.rev(target_ref) != plan.target or branch != plan.branch:
        raise UpdateFailed("this PC's version, or the lab branch, has changed since the check: check again")
    again = Plan(root=plan.root, kind=plan.kind, branch=plan.branch, current=plan.current, base=plan.base,
                 target=plan.target, files=plan.files, own_commits=plan.own_commits)
    _judge(again, git)
    if again.blockers or ((again.adopt, again.keep, again.hold, again.aside, again.moving)
                          != (plan.adopt, plan.keep, plan.hold, plan.aside, plan.moving)):
        raise UpdateFailed("this PC's own files have changed since the check: check again")


def _snapshot(undo: _Undo):
    """What the update may change, kept: git's index, copies of this PC's changed config files and of
    its own files the move writes, the lab settings it may write, the registry."""
    plan, git = undo.plan, undo.git
    try:
        undo.index_tree = git("write-tree").strip()
    except GitError as e:
        raise UpdateFailed(f"git's index can't be kept ({e}): ask Josh") from None
    for path in sorted(set(plan.adopt) | {p for p in plan.keep if p in plan.moving}):
        raw = _read(os.path.join(plan.root, path))
        undo.copies[path] = raw
        if raw is not None:
            _save(os.path.join(undo.folder, "worktree", path), raw)
    for path in plan.adopt:
        name = os.path.basename(path)
        raw = _read(lab_config.lab_path(name, _config_dir(plan.root)))
        undo.lab_backup[name] = raw
        if raw is not None:
            _save(os.path.join(undo.folder, "lab", name), raw)
    undo.registry = _read(undo.registry_path)
    if undo.registry is not None:
        _save(os.path.join(undo.folder, "system_tags.json"), undo.registry)
    _record(undo.folder, {"index_tree": undo.index_tree, "copies": sorted(undo.copies),
                          "lab_copies": sorted(undo.lab_backup)}, merge=True)


def _build_registry(undo: _Undo, hooks: Hooks, out: str, did) -> str:
    """A version's registry, into `out`. The registry in use stays as it was (a version from before
    17 October 2026 ignores --out and builds it in place: it is put back)."""
    problem = hooks.build_registry(out)
    if _read(undo.registry_path) != undo.registry:
        _put(undo, os.path.dirname(undo.registry_path), os.path.basename(undo.registry_path), undo.registry,
             "registry_made_by_the_update")
        did("(its registry builder wrote the registry in use: that is put back)")
    return problem


def _keep_own_settings(undo: _Undo, did):
    """Each config file changed on this PC: what the programs used (the file, with the lab settings it
    had on top) becomes its lab settings, counted from the repository's version (at `base`)."""
    plan = undo.plan
    if not plan.adopt:
        return
    did("Keeping this PC's own config changes as lab settings")
    cfg = _config_dir(plan.root)
    for path in plan.adopt:
        name = os.path.basename(path)
        used = lab_config.merge(_json(undo.copies.get(path)), lab_config.lab_values(name, cfg))
        base = _json(undo.git.show(plan.base, path))
        base = {} if base is None else base             # (a file only this PC's commits have)
        if not isinstance(base, dict) or not isinstance(used, dict):
            raise UpdateFailed(f"{path} isn't a JSON object here or in the repository")
        lab = lab_config.diff(base, used)
        if not lab_config.same_json(lab_config.merge(base, lab), used):
            raise UpdateFailed(f"{name}'s lab settings wouldn't give back what this PC used")
        _save(lab_config.lab_path(name, cfg), lab_config.lab_text(lab).encode("ascii"))
        did(f"{name}: " + (", ".join(lab_config.changed_keys(lab)) or "nothing different") + " kept")


def _point_lab(undo: _Undo, did, backup: str):
    """The branch 'lab' at the target, checked out (HEAD), following origin/lab. Its own commits, if
    it had any, kept on the branch `backup` first."""
    plan, git = undo.plan, undo.git
    lab_ref = f"refs/heads/{BRANCH}"
    undo.lab_before = git.rev(lab_ref)
    undo.lab_upstream = (git("config", "--get", f"branch.{BRANCH}.remote", check=False).strip(),
                         git("config", "--get", f"branch.{BRANCH}.merge", check=False).strip())
    undo.refs_taken = True
    if undo.lab_before and git.count(undo.lab_before, "--not", plan.target, "--remotes"):
        kept = [b for b in git("branch", "--format=%(refname:short)", "--contains", undo.lab_before).split()
                if b != BRANCH]
        if kept:                                        # (on a branch already: one an earlier update made, say)
            did(f"This PC's own commits on '{BRANCH}' are on the branch {kept[0]}")
        else:
            git("branch", backup, undo.lab_before)
            undo.backup = backup
            did(f"This PC's own commits on '{BRANCH}' are kept on the branch {backup}")
    message = f"SINDIE {'update' if plan.kind == UPDATE else 'going back'} to {plan.target[:7]}"
    git("update-ref", "-m", message, lab_ref, plan.target)
    git("symbolic-ref", "-m", message, "HEAD", lab_ref)
    git("config", f"branch.{BRANCH}.remote", REMOTE)
    git("config", f"branch.{BRANCH}.merge", f"refs/heads/{BRANCH}")


def _move(undo: _Undo, did, backup: str):
    """To the target: every file it changes written (and the files it deletes moved aside), the
    branch 'lab' at it, checked out - and each of those files checked. This PC's own commits stay on
    their branch (or, if they were on 'lab', on a branch named `backup`); its own recipes and data
    are as they were."""
    plan, git, root = undo.plan, undo.git, undo.plan.root
    moving = plan.moving
    local = local_changes(git)
    mine = set(plan.adopt) | set(plan.keep) | set(plan.hold) | set(plan.aside)
    unexpected = sorted(p for p, s in moving.items() if local.get(p) and p not in mine
                        and not (s == "D" and local[p] == "D"))          # (deleted here and in the update: no matter)
    if unexpected:
        raise UpdateFailed(f"this PC has changes to {_files(unexpected)} that the update would overwrite: check again")
    write = sorted({p for p, s in moving.items() if s != "D"} | set(plan.adopt))   # (adopted: back to the repository's)
    untrack = sorted(p for p, s in moving.items() if s == "D")
    undo.move_started = True                            # (from here, going back puts the version before's files back)
    for path in plan.hold:                              # (out of git's way: kept as they are)
        _hold(undo, path)
    for path in plan.aside:                             # (the same as the update's: it takes their place)
        undo.aside[path] = _aside(root, path, undo.folder, "in_the_way")
    _point_lab(undo, did, backup)
    said = []                                           # (git's own words, if it stopped: the files say how far it got)
    if write:
        try:
            git("checkout", plan.target, timeout=CHECKOUT_TIMEOUT_S, paths=write)
        except GitError as e:
            said.append(str(e))
    if untrack:
        try:
            git("rm", "-q", "--cached", "--ignore-unmatch", timeout=CHECKOUT_TIMEOUT_S, paths=untrack)
        except GitError as e:
            said.append(str(e))
        for path in untrack:
            if path not in plan.hold and os.path.lexists(os.path.join(root, path)):
                _aside(root, path, undo.folder, "removed_by_the_update")
    if git.rev("HEAD") != plan.target:
        raise UpdateFailed("git didn't move to the new version")
    wrong = _differing(git, plan.target, [p for p in write if p not in plan.keep and p not in plan.hold])
    if wrong:
        raise UpdateFailed(f"{_files(wrong)} couldn't be written ({', '.join(wrong[:3])}): is a program - Excel, an "
                           "antivirus - holding them open?" + (f" ({'; '.join(said)})" if said else ""))
    if said:
        raise UpdateFailed("; ".join(said))
    if undo.held:
        did(f"{_files(undo.held)}: kept on this PC as {'it is' if len(undo.held) == 1 else 'they are'}")
    _return_held(undo)
    for path in plan.keep:
        if path in moving and undo.copies.get(path) is not None and _read(os.path.join(root, path)) != undo.copies[path]:
            _save(os.path.join(root, path), undo.copies[path])
            did(f"{path}: this PC's version kept")


def _hold(undo: _Undo, path: str):
    here, held = os.path.join(undo.plan.root, path), os.path.join(undo.folder, "held", path)
    if os.path.lexists(here) and path not in undo.held:
        _move_file(here, held)
        undo.held.append(path)


def _return_held(undo: _Undo):
    """This PC's files held out of git's way, back where they were."""
    for path in list(undo.held):
        here = os.path.join(undo.plan.root, path)
        if os.path.lexists(here):                       # (git put the repository's there)
            _aside(undo.plan.root, path, undo.folder, "in_the_way_of_held")
        _move_file(os.path.join(undo.folder, "held", path), here)
        undo.held.remove(path)


def _put(undo: _Undo, root: str, path: str, raw: Optional[bytes], why: str):
    """A file as it was: these bytes, or (None) not there - what is there set aside."""
    here = os.path.join(root, path)
    if raw is None:
        if os.path.lexists(here):
            _aside(root, path, undo.folder, why)
    elif _read(here) != raw:
        _save(here, raw)


def _restore_refs(undo: _Undo):
    """HEAD, the branch 'lab' and its upstream as they were; the backup branch made for this PC's
    own commits taken away again (they are on 'lab' once more)."""
    plan, git = undo.plan, undo.git
    if not undo.refs_taken:
        return                                          # (they weren't changed)
    lab_ref, message = f"refs/heads/{BRANCH}", "SINDIE update undone"
    if plan.branch == BRANCH:
        git("update-ref", "-m", message, lab_ref, plan.current)
    if plan.branch:
        git("symbolic-ref", "-m", message, "HEAD", f"refs/heads/{plan.branch}")
    else:
        git("update-ref", "--no-deref", "-m", message, "HEAD", plan.current)
    if plan.branch != BRANCH:
        if undo.lab_before:
            git("update-ref", "-m", message, lab_ref, undo.lab_before)
        elif git.rev(lab_ref):
            git("update-ref", "-m", message, "-d", lab_ref)        # (made by the update)
    for key, value in zip(("remote", "merge"), undo.lab_upstream):
        if value:
            git("config", f"branch.{BRANCH}.{key}", value)
        else:
            git("config", "--unset", f"branch.{BRANCH}.{key}", check=False)
    if undo.backup and git.rev(lab_ref) == undo.lab_before:
        git("branch", "-D", undo.backup)
        undo.backup = ""


def _return_aside(undo: _Undo):
    for path, where in list(undo.aside.items()):
        here = os.path.join(undo.plan.root, path)
        if os.path.lexists(here):
            _aside(undo.plan.root, path, undo.folder, "in_the_way_of_returned")
        _move_file(where, here)
        del undo.aside[path]


def _restore(undo: _Undo) -> List[str]:
    """The version before, as it was: git (the branch, the files the update changed, the index), this
    PC's own files, lab settings, registry - each part on its own, so that one that can't be put back
    doesn't stop the others. git is judged by what it leaves (it can fail on one file and have put
    back the rest), not by what it says. Returns what isn't as it was ([] all of it)."""
    plan, git, root = undo.plan, undo.git, undo.plan.root
    problems, said = [], []

    def step(what: str, fn, judged_after: bool = False):
        try:
            fn()
        except (GitError, UpdateFailed, OSError) as e:
            log.error("Update: going back: %s: %s", what, e)
            (said if judged_after else problems).append(f"{what}: {e}")

    if undo.move_started:
        _wait_for_git(git, set_aside=True)
        touched = dict(plan.moving)
        for path in plan.adopt:
            touched.setdefault(path, "M")
        now = git.rev("HEAD")
        if now and now not in (plan.current, plan.target):    # (git stopped half way)
            for path, status in changed_between(git, plan.current, now).items():
                touched.setdefault(path, status)
        there = sorted(p for p, s in touched.items() if s != "A")          # in the version before
        new = sorted(p for p, s in touched.items() if s == "A")            # only in the new one
        for path in plan.hold:                          # (held again: the version before's files are written now)
            step(f"keeping {path} out of git's way", lambda p=path: _hold(undo, p))
        step("the branch", lambda: _restore_refs(undo), judged_after=True)
        if there:
            step("the files of the version before",
                 lambda: git("checkout", plan.current, timeout=CHECKOUT_TIMEOUT_S, paths=there), judged_after=True)
        for path in new:
            if path not in plan.hold and os.path.lexists(os.path.join(root, path)):
                step(f"taking {path} away", lambda p=path: _aside(root, p, undo.folder, "from_the_new_version"),
                     judged_after=True)
        if undo.index_tree:
            step("git's index", lambda: git("read-tree", "-m", undo.index_tree, timeout=CHECKOUT_TIMEOUT_S),
                 judged_after=True)
        step("git's index", lambda: git("update-index", "-q", "--refresh", check=False, timeout=CHECKOUT_TIMEOUT_S),
             judged_after=True)
        for path, raw in sorted(undo.copies.items()):
            step(path, lambda p=path, r=raw: _put(undo, root, p, r, "not_there_before"))
        step("this PC's files held out of git's way", lambda: _return_held(undo))
        step("files set aside for the update's", lambda: _return_aside(undo))
        # As it is now
        try:
            branch = git("symbolic-ref", "--short", "-q", "HEAD", check=False).strip()
            if git.rev("HEAD") != plan.current or branch != plan.branch:
                problems.append(f"git isn't back at {plan.current[:7]}" + (f" on {plan.branch}" if plan.branch else ""))
            if undo.refs_taken and git.rev(f"refs/heads/{BRANCH}") != undo.lab_before:
                problems.append(f"the branch '{BRANCH}' isn't back at {undo.lab_before[:7] or '(none)'}")
            if undo.index_tree and git("write-tree", check=False).strip() != undo.index_tree:
                problems.append("git's index isn't as it was")
            mine = set(plan.adopt) | set(plan.keep) | set(plan.hold) | set(plan.aside)
            wrong = _differing(git, plan.current, [p for p in there if p not in mine])
            still = [p for p in new if p not in mine and os.path.lexists(os.path.join(root, p))]
            if wrong or still:
                problems.append(f"{_files(wrong + still)} aren't as they were ({', '.join((wrong + still)[:3])}): is a "
                                "program holding them open?")
        except GitError as e:
            problems.append(f"git can't say how things are: {e}")
        if problems:
            problems += said                            # (and what git said as it went)
    cfg = _config_dir(root)
    for name, raw in undo.lab_backup.items():
        step(f"the lab settings {name}",
             lambda n=name, r=raw: _put(undo, lab_config.lab_dir(cfg), n, r, "lab_made_by_the_update"))
    step("the tag registry", lambda: _put(undo, os.path.dirname(undo.registry_path),
                                          os.path.basename(undo.registry_path), undo.registry,
                                          "registry_made_by_the_update"))
    return problems


def _roll_back(undo: _Undo, hooks: Hooks, did, report: Report):
    """The version before, as it was; then the services as they were. Anything that can't be done is
    in the report, and the services are then left stopped (and no launcher starts them by itself:
    held_up())."""
    did("Putting the version before back")
    if undo.services_started:
        try:
            problem = hooks.stop_services(True)
        except Exception as e:      # (anything: said, and nothing more is done)
            problem = f"{type(e).__name__}: {e}"
        if problem:
            report.message += (f". The services didn't stop to go back ({problem}): call Josh. What was kept is in "
                               f"{undo.folder}")
            return
    try:
        problems = _restore(undo)
    except Exception as e:          # (anything: said)
        log.exception("Update: going back failed")
        problems = [f"{type(e).__name__}: {e}"]
    if problems:
        report.message += (f". AND the version before couldn't be put back completely ({_listed(problems, 3)}): the "
                           f"services are left stopped - call Josh. What was kept is in {undo.folder}")
        return
    report.rolled_back = True
    did("The version before is back")
    if undo.services_stopped:
        did("Starting the services again")
        try:
            problem = hooks.start_services() or hooks.healthy(HEALTHY_WITHIN_S, undo.online_before)
        except Exception as e:      # (anything: said)
            problem = f"{type(e).__name__}: {e}"
        if problem:
            report.advice = f"The services haven't all come back ({problem}): see the Services box, and call Josh."
            return
    report.advice = ("Press RESET FAULTS in System Health: stopping the services latched PC_Watchdog_Timeout in the "
                     "PLC." if undo.services_stopped else "")


# ============================================================================ state and history
_STATE_LOCK = threading.RLock()     # (the launcher's window and its checks and updates, in threads of their own)


def state_path() -> str:
    return os.path.join(updates_dir(), "state.json")


def load_state() -> dict:
    try:
        state = json.loads(_read(state_path()) or b"{}")
    except ValueError:
        return {}
    return state if isinstance(state, dict) else {}


def save_state(state: dict):
    with _STATE_LOCK:
        try:
            _save(state_path(), (json.dumps(state, indent=2) + "\n").encode("utf-8"))
        except OSError as e:
            log.error("Couldn't save the updater's state (%s): %s", state_path(), e)


def _change(change: Callable[[dict], None]):
    """The state read, changed and written whole: one thread, and one process (the other system's
    launcher, the command line), at a time."""
    with _STATE_LOCK:
        lock = FileLock("state.lock")
        locked = lock.acquire(wait=10.0)
        if not locked:
            log.warning("The updater's state is held by another program: changed without its lock")
        try:
            state = load_state()
            change(state)
            save_state(state)
        finally:
            lock.release()


def change_state(**values):
    """Set some of the state's values."""
    _change(lambda state: state.update(values))


def remember_check(plan: Plan):
    change_state(last_check=plan.checked_at, plan=plan.to_json())


def last_plan() -> Optional[Plan]:
    try:
        return Plan.from_json(load_state()["plan"])
    except (KeyError, TypeError, ValueError):
        return None


def _history(entry: dict):
    def add(state):
        state["history"] = (state.get("history") or [])[-49:] + [entry]
    _change(add)


def history() -> list:
    return list(load_state().get("history") or [])


def last_update() -> Optional[dict]:
    """The last update that worked (not a going back), if it is the last thing done here."""
    done = [h for h in history() if h.get("ok")]
    return done[-1] if done and done[-1].get("kind", UPDATE) == UPDATE else None


def _record(folder: str, data: dict, merge: bool = False):
    path = os.path.join(folder, "update.json")
    old = {}
    if merge:
        try:
            old = json.loads(_read(path) or b"{}")
        except ValueError:
            old = {}
    try:
        _save(path, (json.dumps(dict(old, **data), indent=2) + "\n").encode("utf-8"))
    except OSError as e:
        log.error("Couldn't write %s: %s", path, e)


def _last_folder() -> tuple:
    """(the last update's folder, what its update.json says) - (None, {}) if there is none."""
    try:
        folders = sorted(f for f in os.listdir(updates_dir()) if f[:1].isdigit())
    except OSError:
        return None, {}
    if not folders:
        return None, {}
    folder = os.path.join(updates_dir(), folders[-1])
    try:
        done = json.loads(_read(os.path.join(folder, "update.json")) or b"{}")
    except ValueError:
        done = {}
    return folder, done if isinstance(done, dict) else {}


def unfinished() -> Optional[str]:
    """The folder of an update that started and never finished (the launcher stopped half way), if
    the last one is and nobody has been told yet (mark_seen)."""
    if update_lock().held_elsewhere():
        return None                                     # (it is running now)
    folder, done = _last_folder()
    if folder is None:
        return None
    return None if ("finished" in done or "failed" in done or "seen" in done) else folder


def mark_seen(folder: str):
    _record(folder, {"seen": True}, merge=True)


def held_up() -> str:
    """Why no launcher may start the services by itself now ('' if nothing): the software being
    updated, an update that never finished (the launcher stopped half way through it), or one whose
    going back didn't finish. Start services, pressed by someone who has been told why, can go ahead
    (release_hold)."""
    if update_lock().held_elsewhere():
        return BEING_UPDATED
    folder, done = _last_folder()
    if folder is None or done.get("released"):
        return ""
    if "finished" not in done and "failed" not in done:
        return (f"an update never finished (the launcher stopped half way through it), so the software may be half "
                f"updated (what it did is in {folder})")
    if "failed" in done and not done.get("rolled_back"):
        return (f"an update failed, and the version before couldn't be put back completely (what it did is in "
                f"{folder})")
    return ""


def release_hold(user: str = ""):
    """Someone has been told why the services were held, and starts them all the same."""
    folder, _done = _last_folder()
    if folder is not None:
        _record(folder, {"released": {"by": user, "at": time.strftime("%Y-%m-%d %H:%M")}}, merge=True)
        log.warning("Update: the hold on starting the services released by %s (%s)", user or "someone", folder)


def note_on_last(data: dict):
    """Something that happened to the update under way, in its update.json (the PC shutting down)."""
    folder, _done = _last_folder()
    if folder is not None:
        _record(folder, data, merge=True)


# ============================================================================ a version on disk, in a process of its own
def _python_run(args: list, root: str, python: str = None, timeout: float = CHILD_TIMEOUT_S):
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8")
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return subprocess.run([python or sys.executable, *args], cwd=root, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
                          **_no_window())


def build_registry_into(out: str, root: str = None, python: str = None) -> str:
    """The tag registry of the version on disk (its own build_registry), into `out`: '' or what went
    wrong."""
    root = root or paths.PROJECT_ROOT
    try:
        r = _python_run([os.path.join(root, "src", "utils", "build_registry.py"), "--out", out], root, python)
    except (OSError, subprocess.SubprocessError) as e:
        return f"it couldn't be run: {e}"
    if r.returncode or not os.path.isfile(out):
        tail = (r.stderr or r.stdout or "").strip()[-600:]
        return f"exit code {r.returncode}: {tail}"
    return ""


def self_check_of(root: str = None, python: str = None) -> List[str]:
    """The version on disk checks itself (python -m src.launcher.updater --self-check), in a process
    of its own: its problems ([] none). UpdateFailed if it couldn't run at all."""
    root = root or paths.PROJECT_ROOT
    try:
        r = _python_run(["-m", "src.launcher.updater", "--self-check"], root, python)
    except (OSError, subprocess.SubprocessError) as e:
        raise UpdateFailed(f"the version's own check couldn't be run: {e}") from None
    lines = [ln for ln in (r.stdout or "").splitlines() if ln.strip()]
    try:
        problems = json.loads(lines[-1]) if lines else None
    except ValueError:
        problems = None
    if not isinstance(problems, list):
        tail = (r.stderr or r.stdout or "").strip()[-600:]
        raise UpdateFailed(f"the version's own check didn't run (exit code {r.returncode}): {tail}")
    return [str(p) for p in problems]


def self_check(root: str = None) -> List[str]:
    """What is wrong with the version on disk ([] nothing): every Python file compiles, every service
    and program imports, every config file (with this machine's lab settings) passes its checks."""
    import importlib
    root = root or paths.PROJECT_ROOT
    problems = []
    scripts = [os.path.join(root, f) for f in ("main_launcher.py", "run_simulation.py")]
    for folder, dirs, files in os.walk(os.path.join(root, "src")):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        scripts += [os.path.join(folder, f) for f in files if f.endswith(".py")]
    for path in scripts:
        try:
            with open(path, "rb") as f:
                compile(f.read(), path, "exec")
        except FileNotFoundError:
            continue
        except (SyntaxError, ValueError, OSError) as e:
            problems.append(f"{os.path.relpath(path, root)} doesn't compile: {e}")
    modules = ["src.services.service_manager", "src.utils.build_registry", "src.hmi", "src.launcher.window",
               "src.launcher.updater", "src.gui.data_viewer.viewer_main", "run_simulation"]
    try:
        from src.services.service_manager import SERVICES_CONFIG
        modules += sorted("src.services." + os.path.splitext(os.path.basename(c["script"]))[0]
                          for c in SERVICES_CONFIG.values())
    except Exception as e:      # (whatever an import raises: a problem to report)
        problems.append(f"the service manager's list of services can't be read: {type(e).__name__}: {e}")
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception as e:      # (whatever an import raises: a problem to report)
            problems.append(f"{name} doesn't import: {type(e).__name__}: {e}")
    try:
        from src.core import config_store
        names = config_store.config_files()
    except Exception as e:      # (as above)
        problems.append(f"the config files can't be listed: {type(e).__name__}: {e}")
        names = []
    for name in names:
        try:
            _, found = config_store.load(name)
        except (config_store.ConfigError, OSError) as e:
            problems.append(f"{name}: {e}")
            continue
        problems += [f"{name}: {p}" for p in found]
    return problems


def sindie_running() -> List[str]:
    """Every SINDIE service manager and service running on this PC, of any system (the real one, the
    simulator, a copy with ports of its own): ['<system>: <names>'] ([] none)."""
    from src.launcher import programs
    return [f"{programs.system_name(key)}: {', '.join(names)}" for key, names in programs.sindie_processes().items()]


def apply_by_hand(plan: Plan, root: str, wait: float = SILENT_FOR_S, ask=input, clock=time.monotonic,
                  sleep=time.sleep, out=print, running=sindie_running) -> int:
    """--apply: the update from a terminal, with the services stopped (from the launcher) and the
    launcher closed. It waits SILENT_FOR_S with no SINDIE service running on this PC (the PLC then
    has the safety relay off), asks for UPDATE, then applies (the services are left stopped)."""
    from src.launcher.programs import _user
    found = running()
    if found:
        out("SINDIE services are running (" + "; ".join(found) + "): stop them first (the launcher's Stop "
            "services..., type STOP; and close the simulator), close the launcher, then run this again.")
        return 1
    out(f"Waiting {wait:.0f} s with the services stopped (by then the PLC has the safety relay off)...")
    end = clock() + wait
    while clock() < end:
        if running():
            out("The services have been started again: nothing applied.")
            return 1
        sleep(1.0)
    if ask("Close the HMI, the data viewer and the launcher, then type UPDATE to apply it: ").strip() != "UPDATE":
        out("Nothing applied.")
        return 1

    def safe_now() -> List[str]:
        return [f"SINDIE services are running again ({'; '.join(found)})"] if (found := running()) else []
    hooks = Hooks(services_running=lambda: False, online=set, stop_services=lambda force: "", start_services=lambda: "",
                  healthy=lambda timeout, before: "", build_registry=lambda path: build_registry_into(path, root),
                  self_check=lambda: self_check_of(root), safe_now=safe_now)
    registry = getenv("REGISTRY").strip() or os.path.join(root, "config", "system_tags.json")
    report = apply(plan, hooks, step=lambda s: out(f"  {s}"), user=_user(), registry_path=registry)
    if not report.ok:
        out(report.message)
    if report.rolled_back:
        out("The version before is back.")
    if report.ok:
        out("Now open the launcher (the SINDIE shortcut), press Start services, check that every service comes "
            "ONLINE, and press RESET FAULTS in System Health.")
    elif report.advice:
        out(report.advice)
    return 0 if report.ok else 1


def main(argv=None) -> int:
    """python -m src.launcher.updater [--root FOLDER] [--no-fetch | --apply] | --self-check"""
    import argparse
    ap = argparse.ArgumentParser(description="SINDIE software updates: what the launcher's Software box does, from "
                                             "a terminal (docs/engineering/releasing.md).")
    ap.add_argument("--root", help="the SINDIE folder to update (default: this one). From a copy of the new version, "
                                   "it updates a folder whose version has no updater yet")
    ap.add_argument("--no-fetch", action="store_true", help="check against what was last fetched from GitHub")
    ap.add_argument("--apply", action="store_true",
                    help="apply the update: with the services stopped from the launcher, and the launcher closed")
    ap.add_argument("--self-check", action="store_true",
                    help="check this version (an update runs it): its problems, as JSON, on the last line")
    args = ap.parse_args(argv)
    if args.self_check:
        logging.disable(logging.CRITICAL)
        problems = self_check()
        sys.stdout.write(json.dumps(problems) + "\n")
        return 0
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    root = os.path.abspath(args.root) if args.root else paths.PROJECT_ROOT
    if args.root:
        os.environ["SINDIE_DATA_DIR"] = os.path.join(root, "data")         # (its data/updates, not this copy's)
    plan = check(root, fetch=not args.no_fetch)
    print(plan.summary())
    for c in plan.commits:
        print(f"  {c.date[:10]} {c.short} {c.subject}")
    for b in plan.blockers:
        print(f"BLOCKED: {b}")
    for n in plan.notes:
        print(f"note: {n}")
    if not args.apply:
        return 0
    if plan.state != READY:
        print("Nothing applied.")
        return 1
    return apply_by_hand(plan, root)


if __name__ == "__main__":
    sys.exit(main())
