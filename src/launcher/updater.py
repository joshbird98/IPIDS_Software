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
  * a change to the PC-PLC data layout (plc/generated/): the PLC program has to match it, in TIA
    Portal, before the PC reads the PLC differently - or the PLC refuses the PC's reads. Applied by
    hand, after the PLC;
  * new Python packages (requirements.txt): pip can't replace a package the running programs hold
    open. Applied by hand;
  * the GitHub tests failing on the version (or not finished, or not known);
  * this PC's own changes in the way: a change to the software itself (not on the lab branch), a
    recipe or data file the update would overwrite, a file the update adds that is here already.

This PC's own config changes (the config editor used to commit them, and a file can be edited by
hand) are kept: they become its lab settings (config/lab/, src/core/lab_config.py), and the
repository's files go back to the repository's versions. A recipe or data file of its own stays as
it is. Its own commits stay where they were: a branch it was on is never moved, and the branch "lab",
if it had commits of its own, is kept as sindie-pc-<time> first.

apply(), in order (everything undone if a step fails):
   1. stop the services (if they run), so nothing reads half an update;
   2. keep a copy of this PC's own files, the tag registry, the lab settings (data/updates/<time>/);
   3. this version: its tag registry, built by its own code, and its own check (--self-check);
   4. this PC's own config changes become lab settings;
   5. the new version: the branch "lab" at it, checked out (git refuses to overwrite a local change,
      and check() has made sure there is none in the way);
   6. the new version's tag registry, built by the new code: if any PLC tag's address, type or write
      access differs from this version's, the PC would talk to the PLC differently - not applied;
   7. the new version's own check: everything compiles, every service and program imports, every
      config file (with the lab settings) passes its checks - nothing worse than this version;
   8. the services started again (if they ran), and every service that was ONLINE before ONLINE
      again, with the PLC answering, within two minutes.
If a step fails, the version before is put back - git, this PC's own files, lab settings, registry -
and the services are started again as they were. Going back (plan_back) is the same, the other way.

Everything an update does goes into data/updates/<time>/ (update.json: what it did; the copies it
kept) and SystemLogs/launcher.log; data/updates/state.json keeps the last check and the history.
Nothing is ever deleted: a file that has to make way is moved into data/updates/<time>/set_aside/.
"""
import json
import logging
import os
import re
import shutil
import subprocess
import sys
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
BACKUP_PREFIX = "sindie-pc-"        # a branch keeping this PC's own commits on "lab" before "lab" is moved

PLC_INTERFACE = ("plc/generated/",)         # the PC-PLC data layout: the PLC program must match it first
PLC_TOOLS = ("plc/",)                       # anything else under plc/: a note
PACKAGES = ("requirements.txt",)            # new Python packages for the programs: by hand
KEEP = ("config/recipes/", "data/")         # this PC's own files: kept as they are
LAB_FOLDER = "config/lab/"                  # this PC's lab settings: never in git
FAULT_BITS = "config/fault_config.json"     # the PLC program sets the bits: a note
NOT_SETTINGS = {"config/system_tags.json", "config/appearance.json", "config/workspace_profiles.json",
                "config/plot_settings.json"}
TRAILERS = re.compile(r"^(Co-Authored-By|Claude-Session|Signed-off-by):", re.IGNORECASE)

READY, BLOCKED, UP_TO_DATE, CANT_CHECK = "ready", "blocked", "up to date", "can't check"
UPDATE, BACK = "update", "back"
PASSED, FAILED, RUNNING, UNKNOWN = "passed", "failed", "running", "unknown"
SEQUENCER_SLOTS = ("recipe", "mass_scan", "optimiser")
SEQUENCER_ACTIVE = ("running", "paused", "prompt", "stopping")      # (src/sequencer/protocol.py: ACTIVE)
RELAY = "ion_beam.facilities.safety_relay_active"
SOURCE_COLD = "ion_beam.source.chamber.stat_gv_close_permissive"


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


class Git:
    """git in the SINDIE folder. It never asks for anything (GitHub is read as the public repository
    it is: no password, no sign-in window), and opens no console window."""

    def __init__(self, root: str, exe: str = None, run=subprocess.run):
        self.root, self.exe, self.run = root, exe or find_git(), run

    def __call__(self, *args, timeout: float = GIT_TIMEOUT_S, check: bool = True, raw: bool = False):
        if not self.exe:
            raise GitError("git isn't installed (or isn't on the PATH): install Git for Windows")
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", GIT_OPTIONAL_LOCKS="0",
                   LC_ALL="C", LANGUAGE="C", GIT_AUTHOR_NAME="SINDIE updater", GIT_COMMITTER_NAME="SINDIE updater",
                   GIT_AUTHOR_EMAIL="sindie-updater@localhost", GIT_COMMITTER_EMAIL="sindie-updater@localhost")
        log.debug("git %s", " ".join(args))
        try:
            r = self.run([self.exe, "-C", self.root, *args], capture_output=True, timeout=timeout, env=env,
                         stdin=subprocess.DEVNULL, **_no_window())
        except subprocess.TimeoutExpired:
            raise GitError(f"git {args[0]} took longer than {timeout:.0f} s") from None
        except OSError as e:
            raise GitError(f"git couldn't be run: {e}") from None
        if check and r.returncode:
            err = (r.stderr or r.stdout or b"").decode("utf-8", "replace").strip()
            lines = [ln for ln in err.splitlines() if ln.strip()]
            raise GitError(f"git {args[0]} failed: {lines[-1] if lines else f'exit code {r.returncode}'}")
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


def _in_the_middle(git: Git) -> str:
    """'a merge' (or a rebase...) if git is in the middle of one here, else ''."""
    folder = git("rev-parse", "--git-dir", check=False).strip()
    if not folder:
        return ""
    folder = folder if os.path.isabs(folder) else os.path.join(git.root, folder)
    for name, what in (("MERGE_HEAD", "a merge"), ("rebase-merge", "a rebase"), ("rebase-apply", "a rebase"),
                       ("CHERRY_PICK_HEAD", "a cherry-pick"), ("REVERT_HEAD", "a revert")):
        if os.path.exists(os.path.join(folder, name)):
            return what
    return ""


# ============================================================================ files
def _read(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _save(path: str, raw: bytes):
    """Written whole to a temporary file and swapped in."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".update-tmp"
    with open(tmp, "wb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _aside(root: str, path: str, folder: str, why: str) -> str:
    """A file moved out of the way into the update's folder (never deleted). Returns where it went."""
    dst = os.path.join(folder, "set_aside", why, path)
    n = 1
    while os.path.lexists(dst):
        n += 1
        dst = os.path.join(folder, "set_aside", f"{why}-{n}", path)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.replace(os.path.join(root, path), dst)
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
    blockers: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    adopt: List[str] = field(default_factory=list)      # this PC's config changes, to become lab settings
    keep: List[str] = field(default_factory=list)       # this PC's own recipes and data, kept as they are
    hold: List[str] = field(default_factory=list)       # its recipes and data the update stops tracking: they stay
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
    """The GitHub tests of a version: PASSED (every test job passed, in some run on it), FAILED,
    RUNNING, or UNKNOWN (none run, or GitHub didn't answer)."""
    if not repo or not sha:
        return UNKNOWN
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/commits/{sha}/check-runs?per_page=100",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "SINDIE-updater"})
    try:
        with opener(req, timeout=TESTS_TIMEOUT_S) as r:
            runs = json.loads(r.read().decode("utf-8")).get("check_runs", [])
    except (OSError, ValueError, AttributeError) as e:          # (urllib.error.URLError is an OSError)
        log.info("GitHub tests of %s not known: %s", sha[:7], e)
        return UNKNOWN
    jobs = {}
    for run in runs if isinstance(runs, list) else []:
        jobs.setdefault(str(run.get("name")), []).append(run)
    if not jobs:
        return UNKNOWN
    passed = ("success", "neutral", "skipped")
    outcome = PASSED
    for tries in jobs.values():
        # The same commit can be tested more than once (pushed to another branch too): one pass is a pass
        if any(t.get("status") == "completed" and t.get("conclusion") in passed for t in tries):
            continue
        if any(t.get("status") != "completed" for t in tries):
            outcome = FAILED if outcome == FAILED else RUNNING
        else:
            outcome = FAILED
    return outcome


def _cached_tests(repo: Optional[str], sha: str) -> str:
    """tests_status(), remembered once it is final (GitHub answers 60 questions an hour per address)."""
    state = load_state()
    known = state.get("tests") if isinstance(state.get("tests"), dict) else {}
    if known.get(sha) in (PASSED, FAILED):
        return known[sha]
    status = tests_status(repo, sha)
    if status in (PASSED, FAILED):
        known[sha] = status
        state["tests"] = dict(list(known.items())[-50:])
        save_state(state)
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

    # This PC's own changes: in its own commits, and not committed
    local = local_changes(git)
    committed = changed_between(git, plan.base, "HEAD") if plan.own_commits else {}
    own = dict(committed)
    for path, status in local.items():
        if status != "?":
            own.setdefault(path, status)
    software_here, software_committed, coming_back = [], [], []
    for path, status in sorted(own.items()):
        if _is_setting(path):
            _judge_setting(plan, path, status)
        elif path.startswith(KEEP):
            if path in files:
                plan.blockers.append(f"{path} has been changed on this PC, and the update changes it too: it would be "
                                     "overwritten. Save your version under another name (or ask Josh), then check "
                                     "again.")
            else:
                plan.keep.append(path)
                if status == "D" and path in committed:
                    coming_back.append(path)
        else:
            (software_committed if path in committed else software_here).append(path)
    if coming_back:
        plan.notes.append(f"{_files(coming_back)} deleted in this PC's own commits come back, as the repository has "
                          "them.")
    # This PC's recipes and data that the update stops tracking: they stay where they are
    plan.hold = sorted(p for p, s in files.items() if s == "D" and p.startswith(KEEP) and p not in own
                       and os.path.lexists(os.path.join(root, p)))
    if plan.hold:
        plan.notes.append(f"The update stops tracking {_files(plan.hold)} in git: they stay on this PC, as they are.")
    for found, where in ((software_here, "changes of its own to the software"),
                         (software_committed, f"software changes in its version that the '{BRANCH}' branch doesn't "
                                              "have")):
        if found:
            shown = ", ".join(found[:6]) + (f" and {len(found) - 6} more" if len(found) > 6 else "")
            plan.blockers.append(f"This PC has {where}: {shown}. Ask Josh before updating.")

    # Files the update adds that are here already (not tracked, or ignored: git would refuse, or overwrite)
    for path in sorted(p for p, s in files.items() if s == "A" and p not in own):
        here = os.path.join(root, path)
        if os.path.lexists(here) and not _same_text(_read(here) if os.path.isfile(here) else None,
                                                    git.show(plan.target, path)):
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
                    simulation: bool = False) -> List[str]:
    """Why the software can't be updated now ([] = it can).

    view: this system's fresh telemetry (the launcher's). running: this system's service manager
    runs; silent_for: how long it hasn't (None: not known). With the services running, the PLC's own
    data must say the safety relay is off and the source cold (the gate valve's close permissive:
    extraction and target off, filament and caesium cold), and no recipe, scan or optimiser may run.
    With them stopped for SILENT_FOR_S, the PLC has lost the PC's watchdog and has switched the relay
    off itself (it closes the gate valve, which opens the relay). other_*: the other system on this PC
    (the simulator, or from the simulator's launcher the real one): the software is shared. The HMI
    and the data viewer must be closed: they run the version before."""
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
        for slot in SEQUENCER_SLOTS:
            if view.get(f"sequencer.{slot}.state") in SEQUENCER_ACTIVE:
                out.append(f"A {slot.replace('_', ' ')} is running: let it finish, or stop it, first.")
    elif silent_for is None or silent_for < SILENT_FOR_S:
        out.append(f"Wait {SILENT_FOR_S - (silent_for or 0.0):.0f} s more: the services have to have been stopped for a "
                   "minute, so that the PLC has the safety relay off.")
    if simulation:
        if other_running:
            out.append("The real system's services are running: update from the real system's launcher.")
        elif other_silent_for is None or other_silent_for < SILENT_FOR_S:
            out.append(f"Wait {SILENT_FOR_S - (other_silent_for or 0.0):.0f} s more: the real system's services have to "
                       "have been stopped for a minute.")
    elif other_running:
        out.append("The simulator's services are running: close the simulator first (it shares the software).")
    for name in programs_open:
        out.append(f"The {name} is open: close it first.")
    return out


# ============================================================================ applying
@dataclass
class Hooks:
    """What apply() asks of the launcher (fakes in the tests). Those returning str return '' if it
    worked, else what went wrong."""
    services_running: Callable[[], bool]
    online: Callable[[], set]                           # the services ONLINE now
    stop_services: Callable[[], str]
    start_services: Callable[[], str]
    healthy: Callable[[float, set], str]                # wait (up to the time given) for these ONLINE + the PLC
    build_registry: Callable[[str], str]                # the tag registry of the version on disk, into this file
    self_check: Callable[[], List[str]]                 # the version on disk's own check; raises UpdateFailed if it can't run


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


class _Undo:
    """What the update has done so far, to put back."""

    def __init__(self, git: Git, plan: Plan, folder: str, registry_path: str):
        self.git, self.plan, self.folder, self.registry_path = git, plan, folder, registry_path
        self.services_were_running = False
        self.online_before = set()
        self.services_stopped = False
        self.services_started = False
        self.copies = {}                # path -> its bytes before (its config changes; its own files the move changes)
        self.stash = ""                 # this PC's own changes to tracked files before: a git stash commit
        self.held = []                  # its files the update stops tracking, held out of git's way meanwhile
        self.lab_backup = {}            # lab file name -> its bytes before (None: there wasn't one)
        self.registry = None            # the registry's bytes before
        self.registry_written = False
        self.lab_before = None          # where the branch "lab" was ('' none)
        self.move_started = False


def _seed(path: str, registry: Optional[bytes]):
    """A registry is built on the one in use: the builder keeps its hand-edited fields (labels,
    units, scaling, data types: src/utils/build_registry.py), as it does building in place."""
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
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(clock()))
    folder = _new_folder(folder_root or updates_dir(), stamp)
    report.folder = folder
    undo = _Undo(git, plan, folder, registry_path or paths.REGISTRY_PATH)
    going = "the new version" if plan.kind == UPDATE else "the version before"

    def did(text):
        report.steps.append(text)
        log.info("Update: %s", text)
        _record(folder, {"steps": report.steps}, merge=True)
        step(text)

    _record(folder, {"kind": plan.kind, "from": plan.current, "branch": plan.branch, "to": plan.target, "by": user,
                     "started": stamp, "adopt": plan.adopt, "keep": plan.keep, "commits": len(plan.commits)})
    try:
        _still_as_checked(plan, git)
        # 1. The services
        undo.services_were_running = hooks.services_running()
        if undo.services_were_running:
            undo.online_before = set(hooks.online())
            did("Stopping the services")
            problem = hooks.stop_services()
            if problem:
                raise UpdateFailed(f"the services didn't stop: {problem}")
            undo.services_stopped = True
        # 2. Copies of what this update may change
        _snapshot(undo, stamp)
        # 3. This version: its registry, its own check
        did("Checking this version (its tag registry, its own check)")
        before = os.path.join(folder, "registry_before.json")
        _seed(before, undo.registry)
        problem = hooks.build_registry(before)
        if problem:
            if undo.registry is None:
                raise UpdateFailed(f"this version's tag registry can't be built ({problem}), so the PLC tags can't be "
                                   "compared")
            _save(before, undo.registry)
            did(f"(this version's registry couldn't be built - {problem} - the one in use is compared instead)")
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
        _move(undo, did, f"{BACKUP_PREFIX}{stamp}")
        # 6. Its registry, and the PLC's tags in it
        did("Building its tag registry")
        after = os.path.join(folder, "registry_after.json")
        _seed(after, undo.registry)
        problem = hooks.build_registry(after)
        if problem:
            raise UpdateFailed(f"its tag registry couldn't be built: {problem}")
        try:
            differences = plc_differences(json.loads(_read(before) or b"{}"), json.loads(_read(after) or b"{}"))
        except ValueError as e:
            raise UpdateFailed(f"the tag registries can't be compared: {e}") from None
        if differences:
            raise UpdateFailed("it would read or write the PLC differently (" + "; ".join(differences[:4])
                               + (f"; and {len(differences) - 4} more" if len(differences) > 4 else "")
                               + "): the PLC program has to match it first")
        did("The PLC's tags are where they were")
        _save(undo.registry_path, _read(after))
        undo.registry_written = True
        # 7. Its own check
        did("Checking it (everything compiles and imports, the config files pass)")
        new = [p for p in hooks.self_check() if p not in problems_before]
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
    except Exception as e:      # (anything at all: the version before must come back)
        if isinstance(e, (UpdateFailed, GitError)):
            report.message = str(e)
            log.error("Update to %s failed: %s", plan.target[:7], e)
        else:
            report.message = f"{type(e).__name__}: {e}"
            log.exception("Update to %s failed", plan.target[:7])
        _roll_back(undo, hooks, did, report)
        _record(folder, {"failed": report.message, "rolled_back": report.rolled_back}, merge=True)
        _history({"at": clock(), "kind": plan.kind, "by": user, "from": plan.current, "to": plan.target, "ok": False,
                  "message": report.message, "advice": report.advice, "rolled_back": report.rolled_back,
                  "folder": folder})
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
        state = load_state()
        state["went_back_from"] = {"sha": plan.current, "when": time.strftime("%d %b %Y", time.localtime(clock()))}
        save_state(state)
    return report


def _still_as_checked(plan: Plan, git: Git):
    """Nothing has moved since the check: not this PC's version, not the target, not its own files."""
    target_ref = f"refs/remotes/{REMOTE}/{BRANCH}" if plan.kind == UPDATE else plan.target
    if git.rev("HEAD") != plan.current or git.rev(target_ref) != plan.target:
        raise UpdateFailed("this PC's version, or the lab branch, has changed since the check: check again")
    again = Plan(root=plan.root, kind=plan.kind, branch=plan.branch, current=plan.current, base=plan.base,
                 target=plan.target, files=plan.files, own_commits=plan.own_commits)
    _judge(again, git)
    if again.blockers or (again.adopt, again.keep, again.hold) != (plan.adopt, plan.keep, plan.hold):
        raise UpdateFailed("this PC's own files have changed since the check: check again")


def _snapshot(undo: _Undo, stamp: str):
    """What the update may change, kept: this PC's own changes to tracked files (as a git stash
    commit, kept as refs/sindie/<time>: going back applies it again), copies of its changed config
    files and of its own files the move changes, the lab settings it may write, the registry."""
    plan, git = undo.plan, undo.git
    undo.stash = git("stash", "create", f"SINDIE update {stamp}: this PC's own changes before it").strip()
    if undo.stash:
        git("update-ref", f"refs/sindie/{stamp}", undo.stash)
    moving = changed_between(git, "HEAD", plan.target)
    for path in sorted(set(plan.adopt) | {p for p in plan.keep if p in moving}):
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
    _record(undo.folder, {"stash": undo.stash, "copies": sorted(undo.copies), "lab_copies": sorted(undo.lab_backup)},
            merge=True)


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


def _move(undo: _Undo, did, backup: str):
    """To the target: the branch 'lab' at it, checked out. This PC's own commits stay on their branch
    (or, if they were on 'lab', on a branch named `backup`); its own recipes and data are written back
    as they were."""
    plan, git = undo.plan, undo.git
    moving = changed_between(git, "HEAD", plan.target)
    local = local_changes(git)
    undo.move_started = True                            # (from here, going back checks the version before out again)
    for path in plan.hold:                              # (out of git's way: it would delete them)
        _hold(undo, path)
    for path in plan.adopt:
        if local.get(path, "?") != "?":
            git("checkout", "HEAD", "--", path)         # (its values are lab settings now)
    undo.lab_before = git.rev(f"refs/heads/{BRANCH}")
    if undo.lab_before and git.count(undo.lab_before, "--not", plan.target, "--remotes"):
        git("branch", backup, undo.lab_before)
        did(f"This PC's own commits on '{BRANCH}' are kept on the branch {backup}")
    git("checkout", "-B", BRANCH, plan.target, timeout=CHECKOUT_TIMEOUT_S)
    git("branch", f"--set-upstream-to={REMOTE}/{BRANCH}", BRANCH, check=False)
    if git.rev("HEAD") != plan.target:
        raise UpdateFailed("git didn't move to the new version")
    if undo.held:
        did(f"{_files(undo.held)}: kept on this PC, no longer tracked")
    _return_held(undo)
    for path in plan.keep:
        raw, here = undo.copies.get(path), os.path.join(plan.root, path)
        if path in moving and raw is not None and _read(here) != raw:
            _save(here, raw)
            did(f"{path}: this PC's version kept")


def _hold(undo: _Undo, path: str):
    here, held = os.path.join(undo.plan.root, path), os.path.join(undo.folder, "held", path)
    if os.path.lexists(here):
        os.makedirs(os.path.dirname(held), exist_ok=True)
        os.replace(here, held)
        undo.held.append(path)


def _return_held(undo: _Undo):
    """This PC's files held out of git's way, back where they were."""
    for path in list(undo.held):
        here = os.path.join(undo.plan.root, path)
        if os.path.lexists(here):                       # (git put the repository's there: it is the same file)
            _aside(undo.plan.root, path, undo.folder, "in_the_way_of_held")
        os.makedirs(os.path.dirname(here) or ".", exist_ok=True)
        os.replace(os.path.join(undo.folder, "held", path), here)
        undo.held.remove(path)


def _roll_back(undo: _Undo, hooks: Hooks, did, report: Report):
    """The version before, as it was: git, this PC's own files, lab settings, registry; then the
    services as they were."""
    plan, git = undo.plan, undo.git
    did("Putting the version before back")
    if undo.services_started:
        problem = hooks.stop_services()
        if problem:
            report.message += (f". The services didn't stop to go back ({problem}): call Josh. What was kept is in "
                               f"{undo.folder}")
            return
    try:
        _return_held(undo)
        if undo.move_started:
            if plan.branch == BRANCH:
                git("checkout", "-f", "-B", BRANCH, plan.current, timeout=CHECKOUT_TIMEOUT_S)
            elif plan.branch:
                git("checkout", "-f", plan.branch, timeout=CHECKOUT_TIMEOUT_S)
                if undo.lab_before:
                    git("branch", "-f", BRANCH, undo.lab_before)
            else:
                git("checkout", "-f", "--detach", plan.current, timeout=CHECKOUT_TIMEOUT_S)
            if git.rev("HEAD") != plan.current:
                raise GitError(f"git isn't back at {plan.current[:7]}")
            if undo.stash:                              # this PC's own changes, as they were
                git("stash", "apply", undo.stash, timeout=CHECKOUT_TIMEOUT_S)
        cfg = _config_dir(plan.root)
        for name, raw in undo.lab_backup.items():
            lab_file = lab_config.lab_path(name, cfg)
            if raw is not None:
                _save(lab_file, raw)
            elif os.path.exists(lab_file):
                _aside(os.path.dirname(lab_file), name, undo.folder, "lab_made_by_the_update")
        if undo.registry_written:
            if undo.registry is not None:
                _save(undo.registry_path, undo.registry)
            elif os.path.exists(undo.registry_path):
                _aside(os.path.dirname(undo.registry_path), os.path.basename(undo.registry_path), undo.folder,
                       "registry_made_by_the_update")
    except Exception as e:      # (anything: say so, and leave the services stopped)
        log.exception("Update: going back failed")
        report.message += (f". AND the version before couldn't be put back completely ({e}): the services are left "
                           f"stopped - call Josh. What was kept is in {undo.folder}")
        return
    report.rolled_back = True
    did("The version before is back")
    if undo.services_stopped:
        did("Starting the services again")
        problem = hooks.start_services() or hooks.healthy(HEALTHY_WITHIN_S, undo.online_before)
        if problem:
            report.advice = f"The services haven't all come back ({problem}): see the Services box, and call Josh."
            return
    report.advice = ("Press RESET FAULTS in System Health: stopping the services latched PC_Watchdog_Timeout in the "
                     "PLC." if undo.services_stopped else "")


# ============================================================================ state and history
def state_path() -> str:
    return os.path.join(updates_dir(), "state.json")


def load_state() -> dict:
    try:
        state = json.loads(_read(state_path()) or b"{}")
    except ValueError:
        return {}
    return state if isinstance(state, dict) else {}


def save_state(state: dict):
    try:
        _save(state_path(), (json.dumps(state, indent=2) + "\n").encode("utf-8"))
    except OSError as e:
        log.error("Couldn't save the updater's state (%s): %s", state_path(), e)


def remember_check(plan: Plan):
    state = load_state()
    state["last_check"] = plan.checked_at
    state["plan"] = plan.to_json()
    save_state(state)


def last_plan() -> Optional[Plan]:
    try:
        return Plan.from_json(load_state()["plan"])
    except (KeyError, TypeError, ValueError):
        return None


def _history(entry: dict):
    state = load_state()
    state["history"] = (state.get("history") or [])[-49:] + [entry]
    save_state(state)


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


def unfinished() -> Optional[str]:
    """The folder of an update that started and never finished (the launcher stopped half way), if
    the last one is."""
    try:
        folders = sorted(f for f in os.listdir(updates_dir()) if f[:1].isdigit())
    except OSError:
        return None
    if not folders:
        return None
    folder = os.path.join(updates_dir(), folders[-1])
    try:
        done = json.loads(_read(os.path.join(folder, "update.json")) or b"{}")
    except ValueError:
        return folder
    return None if ("finished" in done or "failed" in done or "seen" in done) else folder


def mark_seen(folder: str):
    _record(folder, {"seen": True}, merge=True)


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


def apply_by_hand(plan: Plan, root: str, wait: float = SILENT_FOR_S, ask=input, clock=time.monotonic,
                  sleep=time.sleep, out=print) -> int:
    """--apply: the update from a terminal, with the services stopped (from the launcher) and the
    launcher closed. It waits SILENT_FOR_S with no service manager running (the PLC then has the
    safety relay off), asks for UPDATE, then applies (the services are left stopped)."""
    from src.core.env import SIM_PORT_OFFSET
    from src.core.network_map import manager_lock_port
    from src.launcher.programs import _lock_taken, _user
    ports = (manager_lock_port(0), manager_lock_port(SIM_PORT_OFFSET))

    def running() -> bool:
        return any(_lock_taken(p) for p in ports)
    if running():
        out("The services are running: stop them first (the launcher's Stop services..., type STOP), close the "
            "launcher, then run this again.")
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
    hooks = Hooks(services_running=lambda: False, online=set, stop_services=lambda: "", start_services=lambda: "",
                  healthy=lambda timeout, before: "", build_registry=lambda path: build_registry_into(path, root),
                  self_check=lambda: self_check_of(root))
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
