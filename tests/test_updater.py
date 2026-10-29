"""Software updates (src/launcher/updater.py), against real git repositories: a bare one standing in
for GitHub, Josh's (where versions are made and the lab branch is moved), and the lab PC's clone."""
import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core import lab_config, paths
from src.launcher import updater

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

TAGS = {"ion_beam.system.plc_watchdog": {"source": "service_plc_snap7", "db_number": 10, "byte_offset": 196,
                                         "bit_offset": 0, "datatype": "INT", "writable": True},
        "ion_beam.facilities.safety_relay_active": {"source": "service_plc_snap7", "db_number": 10,
                                                    "byte_offset": 2, "bit_offset": 1, "datatype": "BOOL",
                                                    "writable": False},
        "vacuum.vg1": {"source": "service_vacuum", "units": "mbar"}}
SMU = {"max_v": 100, "ramp": {"rate": 1, "step": 2}, "name": "SMU"}
PASS = (lambda repo, sha: updater.PASSED)


def run_git(cwd, *args) -> str:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(args)}: {r.stderr}"
    return r.stdout.strip()


def write(root, files: dict):
    for path, content in files.items():
        p = Path(root) / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content if isinstance(content, str) else json.dumps(content, indent=2) + "\n")


def commit(root, files: dict, message: str) -> str:
    write(root, files)
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", message)
    return run_git(root, "rev-parse", "HEAD")


def configure(repo):
    run_git(repo, "config", "user.name", "Test")
    run_git(repo, "config", "user.email", "test@example.com")
    run_git(repo, "config", "commit.gpgsign", "false")


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """GitHub (bare), Josh's repository, and the lab PC's clone (on 'main', the version the lab
    branch is at)."""
    monkeypatch.setattr(updater, "updates_dir", lambda: str(tmp_path / "updates"))
    origin, dev, pc = tmp_path / "origin.git", tmp_path / "dev", tmp_path / "pc"
    run_git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    run_git(tmp_path, "init", "-q", "-b", "main", str(dev))
    configure(dev)
    first = commit(dev, {"src/app.py": "VERSION = 1\n", "config/smu_config.json": SMU,
                         "config/recipes/r1.json": {"steps": [1]}, "registry_source.json": TAGS,
                         ".gitignore": "config/lab/\n*.log\n", "requirements.txt": "numpy\n",
                         "plc/generated/PLC_PC_Interface.scl": "DB10\n", "plc/build_plc_types.py": "pass\n",
                         "data/example.csv": "a,b\n"}, "First version")
    run_git(dev, "remote", "add", "origin", str(origin))
    run_git(dev, "push", "-q", "origin", "main", "main:lab")
    run_git(tmp_path, "clone", "-q", str(origin), str(pc))
    configure(pc)
    registry = tmp_path / "system_tags.json"
    registry.write_text(json.dumps(TAGS))
    return SimpleNamespace(tmp=tmp_path, origin=origin, dev=dev, pc=pc, git=updater.Git(str(pc)),
                           registry=registry, first=first, updates=tmp_path / "updates")


def release(lab, files: dict, message: str) -> str:
    """Josh makes a version and moves the lab branch to it."""
    sha = commit(lab.dev, files, message)
    run_git(lab.dev, "push", "-q", "origin", "HEAD:lab")
    return sha


def check(lab, tests=PASS, **kw) -> updater.Plan:
    return updater.check(str(lab.pc), git=lab.git, tests=tests, **kw)


class FakeLauncher:
    """The launcher's side of an update: the services, the registry build, the version's own check."""

    def __init__(self, root, running=True):
        self.root, self.running = Path(root), running
        self.calls, self.health = [], ""
        self.stop_problem = ""

    def hooks(self) -> updater.Hooks:
        return updater.Hooks(services_running=lambda: self.running, online=self.online, stop_services=self.stop,
                             start_services=self.start, healthy=self.healthy, build_registry=self.build,
                             self_check=self.self_check)

    def online(self):
        return {"service_plc", "service_logger"} if self.running else set()

    def stop(self):
        self.calls.append("stop")
        if self.stop_problem:
            return self.stop_problem
        self.running = False
        return ""

    def start(self):
        self.calls.append("start")
        self.running = True
        return ""

    def healthy(self, timeout, before):
        self.calls.append(("healthy", tuple(sorted(before))))
        return self.health

    def build(self, out):
        """The registry the version on disk would build: its registry_source.json."""
        self.calls.append("build")
        shutil.copy(self.root / "registry_source.json", out)
        return ""

    def self_check(self):
        self.calls.append("self_check")
        return ["src/app.py doesn't compile"] if "BROKEN" in (self.root / "src" / "app.py").read_text() else []


def apply(lab, plan, launcher=None, **kw) -> updater.Report:
    launcher = launcher or FakeLauncher(lab.pc)
    return updater.apply(plan, launcher.hooks(), git=lab.git, registry_path=str(lab.registry),
                         folder_root=str(lab.updates), user="operator", **kw)


def head(lab) -> str:
    return run_git(lab.pc, "rev-parse", "HEAD")


def branch(lab) -> str:
    return run_git(lab.pc, "rev-parse", "--abbrev-ref", "HEAD")


def text(lab, path) -> str:
    return (lab.pc / path).read_text()


def config_dir(lab) -> str:
    return str(lab.pc / "config")


# --------------------------------------------------------------------------- checking
def test_up_to_date(lab):
    plan = check(lab)
    assert plan.state == updater.UP_TO_DATE and plan.summary() == "Up to date"
    assert plan.current == plan.target == lab.first and plan.branch == "main"


def test_an_update_lists_its_changes_without_the_trailers(lab):
    sha = release(lab, {"src/app.py": "VERSION = 2\n"},
                  "VG1: retry at once\n\nA lost read is made again.\n\nCo-Authored-By: X <x@y>\nClaude-Session: https://z")
    plan = check(lab)
    assert plan.state == updater.READY, plan.blockers
    assert plan.target == sha and plan.base == lab.first and plan.files == {"src/app.py": "M"}
    [c] = plan.commits
    assert (c.subject, c.body, c.short) == ("VG1: retry at once", "A lost read is made again.", sha[:7])
    assert plan.summary() == "An update is available: 1 change"
    assert updater.Plan.from_json(json.loads(json.dumps(plan.to_json()))) == plan


def test_only_a_version_whose_github_tests_passed(lab):
    release(lab, {"src/app.py": "VERSION = 2\n"}, "Two")
    for status, words in ((updater.FAILED, "tests failed"), (updater.RUNNING, "haven't finished"),
                          (updater.UNKNOWN, "couldn't be found out")):
        plan = check(lab, tests=lambda repo, sha, s=status: s)
        assert plan.state == updater.BLOCKED and words in " ".join(plan.blockers)


def test_github_out_of_reach_or_without_a_lab_branch(lab):
    run_git(lab.pc, "remote", "set-url", "origin", str(lab.tmp / "nowhere.git"))
    plan = check(lab)
    assert plan.state == updater.CANT_CHECK and "GitHub couldn't be reached" in plan.error
    run_git(lab.pc, "remote", "set-url", "origin", str(lab.origin))
    run_git(lab.dev, "push", "-q", "origin", ":lab")
    run_git(lab.pc, "update-ref", "-d", "refs/remotes/origin/lab")
    plan = check(lab)
    assert plan.state == updater.CANT_CHECK and "no 'lab' branch on GitHub" in plan.error
    assert check(lab, fetch=False).error.startswith("there is no 'lab' branch from GitHub here")


@pytest.mark.parametrize("files, words", [
    ({"plc/generated/PLC_PC_Interface.scl": "DB10 v2\n"}, "PC-PLC data layout"),
    ({"requirements.txt": "numpy\nscipy\n"}, "other Python packages"),
    ({"config/lab/smu_config.json": {"max_v": 1}}, "config/lab/"),
])
def test_what_is_applied_by_hand(lab, files, words):
    write(lab.dev, files)
    run_git(lab.dev, "add", "-A", "-f")
    run_git(lab.dev, "commit", "-q", "-m", "By hand")
    run_git(lab.dev, "push", "-q", "origin", "HEAD:lab")
    plan = check(lab)
    assert plan.state == updater.BLOCKED and words in " ".join(plan.blockers)


def test_plc_tools_and_fault_texts_are_notes_only(lab):
    release(lab, {"plc/build_plc_types.py": "pass  # v2\n", "config/fault_config.json": {"w0": {}}}, "Tools")
    plan = check(lab)
    assert plan.state == updater.READY
    assert "PLC tools" in " ".join(plan.notes) and "PLC fault list" in " ".join(plan.notes)


def test_this_pcs_own_changes_to_the_software_block(lab):
    release(lab, {"src/app.py": "VERSION = 2\n"}, "Two")
    write(lab.pc, {"src/other.py": "x = 1\n"})
    run_git(lab.pc, "add", "src/other.py")
    plan = check(lab)
    assert plan.state == updater.BLOCKED and "changes of its own to the software: src/other.py" in plan.blockers[0]
    run_git(lab.pc, "commit", "-q", "-m", "PC's own")
    plan = check(lab)
    assert "software changes in its version that the 'lab' branch doesn't have: src/other.py" in plan.blockers[0]


def test_a_recipe_of_its_own_is_kept_unless_the_update_changes_it_too(lab):
    release(lab, {"src/app.py": "VERSION = 2\n"}, "Two")
    write(lab.pc, {"config/recipes/r1.json": {"steps": [1, 2]}})
    plan = check(lab)
    assert plan.state == updater.READY and plan.keep == ["config/recipes/r1.json"]
    release(lab, {"config/recipes/r1.json": {"steps": [9]}}, "Recipe")
    plan = check(lab)
    assert plan.state == updater.BLOCKED and "would be overwritten" in plan.blockers[0]


def test_a_file_in_the_way_of_one_the_update_adds(lab):
    release(lab, {"src/new.py": "x = 1\n"}, "New file")
    write(lab.pc, {"src/new.py": "x = 1\n"})                    # the same: no matter
    assert check(lab).state == updater.READY
    write(lab.pc, {"src/new.py": "x = 2\n"})
    plan = check(lab)
    assert plan.state == updater.BLOCKED and "adds src/new.py" in " ".join(plan.blockers)
    (lab.pc / "src" / "new.py").unlink()
    write(lab.dev, {"notes.log": "theirs\n"})                     # an ignored file, which git would overwrite
    run_git(lab.dev, "add", "-f", "notes.log")
    run_git(lab.dev, "commit", "-q", "-m", "A log")
    run_git(lab.dev, "push", "-q", "origin", "HEAD:lab")
    write(lab.pc, {"notes.log": "ours\n"})
    plan = check(lab)
    assert plan.state == updater.BLOCKED and "adds notes.log" in " ".join(plan.blockers)


def test_config_changes_here_are_adopted_and_broken_ones_block(lab):
    release(lab, {"src/app.py": "VERSION = 2\n"}, "Two")
    write(lab.pc, {"config/smu_config.json": dict(SMU, max_v=150)})
    plan = check(lab)
    assert plan.state == updater.READY and plan.adopt == ["config/smu_config.json"]
    assert "kept, as its lab settings" in " ".join(plan.notes)
    write(lab.pc, {"config/smu_config.json": "{not json"})
    assert "can't be read as a config file" in " ".join(check(lab).blockers)
    (lab.pc / "config" / "smu_config.json").unlink()
    assert "has been deleted on this PC" in " ".join(check(lab).blockers)
    run_git(lab.pc, "checkout", "--", "config/smu_config.json")
    os.makedirs(lab.pc / "config" / "lab")
    (lab.pc / "config" / "lab" / "smu_config.json").write_text("[1]")
    assert "lab settings for smu_config.json can't be read" in " ".join(check(lab).blockers)


def test_a_note_when_the_update_changes_a_value_this_pc_has_its_own_of(lab):
    os.makedirs(lab.pc / "config" / "lab")
    (lab.pc / "config" / "lab" / "smu_config.json").write_text(json.dumps({"ramp": {"rate": 5}}))
    release(lab, {"config/smu_config.json": dict(SMU, ramp={"rate": 3, "step": 2})}, "Rate")
    plan = check(lab)
    assert plan.state == updater.READY
    assert any("smu_config.json where this PC has values of its own (ramp.rate)" in n for n in plan.notes)


def test_a_merge_half_done_blocks(lab):
    release(lab, {"src/app.py": "VERSION = 2\n"}, "Two")
    (lab.pc / ".git" / "MERGE_HEAD").write_text(lab.first + "\n")
    assert "in the middle of a merge" in " ".join(check(lab).blockers)


# --------------------------------------------------------------------------- applying
def test_an_update_from_start_to_finish(lab):
    sha = release(lab, {"src/app.py": "VERSION = 2\n"}, "Two")
    plan = check(lab)
    seen = []
    launcher = FakeLauncher(lab.pc)
    report = apply(lab, plan, launcher, step=seen.append)
    assert report.ok and report.now == sha and report.message == f"Updated to {sha[:7]}."
    assert "RESET FAULTS" in report.advice and seen == report.steps
    assert head(lab) == sha and branch(lab) == "lab" and text(lab, "src/app.py") == "VERSION = 2\n"
    assert run_git(lab.pc, "rev-parse", "--abbrev-ref", "lab@{upstream}") == "origin/lab"
    assert launcher.calls == ["stop", "build", "self_check", "build", "self_check", "start",
                              ("healthy", ("service_logger", "service_plc"))]
    assert json.loads(lab.registry.read_text()) == TAGS
    done = json.loads((Path(report.folder) / "update.json").read_text())
    assert done["from"] == lab.first and done["to"] == sha and "finished" in done and done["by"] == "operator"
    [entry] = updater.history()
    assert entry["ok"] and entry["to"] == sha and updater.last_update() == entry
    assert check(lab).state == updater.UP_TO_DATE
    assert updater.unfinished() is None


def test_with_the_services_stopped_they_are_left_stopped(lab):
    release(lab, {"src/app.py": "VERSION = 2\n"}, "Two")
    launcher = FakeLauncher(lab.pc, running=False)
    report = apply(lab, check(lab), launcher)
    assert report.ok and "left stopped" in report.advice
    assert launcher.calls == ["build", "self_check", "build", "self_check"]


def test_config_edited_here_becomes_lab_settings(lab):
    new = dict(SMU, ramp={"rate": 3, "step": 2})
    release(lab, {"config/smu_config.json": new}, "Rate 3")
    write(lab.pc, {"config/smu_config.json": dict(SMU, max_v=150)})
    report = apply(lab, check(lab))
    assert report.ok, report.message
    assert json.loads(text(lab, "config/smu_config.json")) == new                         # the repository's
    assert lab_config.lab_values("smu_config.json", config_dir(lab)) == {"max_v": 150}     # this PC's
    assert lab_config.read("smu_config.json", config_dir(lab)) == dict(new, max_v=150)
    assert run_git(lab.pc, "status", "--porcelain") == ""
    assert any("smu_config.json: max_v kept" in s for s in report.steps)


def test_this_pcs_own_commits_on_lab_are_kept(lab):
    """The config editor and the recipe builder used to commit on the lab PC."""
    run_git(lab.pc, "checkout", "-q", "-b", "lab", "--track", "origin/lab")
    own = commit(lab.pc, {"config/smu_config.json": dict(SMU, name="Keithley"), "config/recipes/r1.json": {"steps": [7]},
                          "config/recipes/mine.json": {"steps": [3]}}, "Config editor: SMU name")
    sha = release(lab, {"src/app.py": "VERSION = 2\n"}, "Two")
    plan = check(lab)
    assert plan.state == updater.READY, plan.blockers
    assert plan.own_commits == 1 and plan.adopt == ["config/smu_config.json"]
    assert sorted(plan.keep) == ["config/recipes/mine.json", "config/recipes/r1.json"]
    report = apply(lab, plan)
    assert report.ok, report.message
    assert head(lab) == sha and branch(lab) == "lab"
    [backup] = [b for b in run_git(lab.pc, "branch", "--format=%(refname:short)").split() if b.startswith("sindie-pc-")]
    assert run_git(lab.pc, "rev-parse", backup) == own
    assert lab_config.read("smu_config.json", config_dir(lab))["name"] == "Keithley"
    assert json.loads(text(lab, "config/recipes/r1.json")) == {"steps": [7]}
    assert json.loads(text(lab, "config/recipes/mine.json")) == {"steps": [3]}


def test_a_failed_check_of_the_new_version_puts_everything_back(lab):
    release(lab, {"src/app.py": "BROKEN\n", "config/smu_config.json": dict(SMU, ramp={"rate": 3, "step": 2})}, "Bad")
    write(lab.pc, {"config/smu_config.json": dict(SMU, max_v=150), "config/recipes/r1.json": {"steps": [5]},
                   "notes.txt": "mine\n"})
    before = {p: (lab.pc / p).read_bytes() for p in ("config/smu_config.json", "config/recipes/r1.json", "notes.txt")}
    status_before = run_git(lab.pc, "status", "--porcelain")
    launcher = FakeLauncher(lab.pc)
    report = apply(lab, check(lab), launcher)
    assert not report.ok and report.rolled_back
    assert "didn't pass its own check: src/app.py doesn't compile" in report.message
    assert head(lab) == lab.first and branch(lab) == "main"
    assert {p: (lab.pc / p).read_bytes() for p in before} == before
    assert run_git(lab.pc, "status", "--porcelain") == status_before
    assert not (lab.pc / "config" / "lab" / "smu_config.json").exists()             # (moved aside, not deleted)
    assert list(Path(report.folder).glob("set_aside/lab_made_by_the_update/smu_config.json"))
    assert json.loads(lab.registry.read_text()) == TAGS
    assert launcher.calls[-2:] == ["start", ("healthy", ("service_logger", "service_plc"))]
    assert "RESET FAULTS" in report.advice
    [entry] = updater.history()
    assert not entry["ok"] and entry["rolled_back"] and updater.last_update() is None


def untrack_example_data(lab, broken=False):
    """Josh stops tracking the example data in git (git rm --cached): a checkout would delete it."""
    write(lab.dev, {".gitignore": "config/lab/\n*.log\ndata/\n"} if not broken else
          {".gitignore": "config/lab/\n*.log\ndata/\n", "src/app.py": "BROKEN\n"})
    run_git(lab.dev, "rm", "-q", "--cached", "data/example.csv")
    run_git(lab.dev, "commit", "-q", "-am", "Stop tracking the example data")
    run_git(lab.dev, "push", "-q", "origin", "HEAD:lab")


def test_data_the_update_stops_tracking_stays_on_the_pc(lab):
    untrack_example_data(lab)
    plan = check(lab)
    assert plan.state == updater.READY and plan.hold == ["data/example.csv"]
    assert any("stops tracking data/example.csv in git: they stay on this PC" in n for n in plan.notes)
    report = apply(lab, plan)
    assert report.ok, report.message
    assert text(lab, "data/example.csv") == "a,b\n" and run_git(lab.pc, "status", "--porcelain") == ""
    assert run_git(lab.pc, "ls-files", "data") == ""


def test_held_data_is_back_after_a_failed_update(lab):
    untrack_example_data(lab, broken=True)
    report = apply(lab, check(lab))
    assert not report.ok and report.rolled_back
    assert text(lab, "data/example.csv") == "a,b\n" and run_git(lab.pc, "ls-files", "data") == "data/example.csv"
    assert not list(Path(report.folder).glob("held/**/*.csv"))


def test_this_pcs_own_changes_to_tracked_files_survive_an_update_and_a_failed_one(lab):
    """The data compactor deletes and rewrites logged files that git tracks: those changes are
    this PC's, and nothing the update does - or undoes - loses them."""
    commit(lab.dev, {"data/day1.csv": "x\n"}, "More example data")
    run_git(lab.dev, "push", "-q", "origin", "HEAD:lab")
    run_git(lab.pc, "pull", "-q", "origin", "lab")
    (lab.pc / "data" / "example.csv").unlink()
    write(lab.pc, {"data/day1.csv": "x\ny\n", "config/recipes/r1.json": {"steps": [4]}})
    status = run_git(lab.pc, "status", "--porcelain")
    release(lab, {"src/app.py": "BROKEN\n"}, "Bad")
    report = apply(lab, check(lab))
    assert not report.ok and report.rolled_back
    assert run_git(lab.pc, "status", "--porcelain") == status and text(lab, "data/day1.csv") == "x\ny\n"
    stamp = os.path.basename(report.folder)
    assert run_git(lab.pc, "rev-parse", "--verify", "-q", f"refs/sindie/{stamp}")      # (kept, as a stash commit)
    release(lab, {"src/app.py": "VERSION = 3\n"}, "Good")
    report = apply(lab, check(lab))
    assert report.ok, report.message
    assert run_git(lab.pc, "status", "--porcelain") == status and text(lab, "src/app.py") == "VERSION = 3\n"


def test_a_version_that_reads_the_plc_differently_is_not_applied(lab):
    moved = json.loads(json.dumps(TAGS))
    moved["ion_beam.system.plc_watchdog"]["byte_offset"] = 198
    release(lab, {"registry_source.json": moved}, "Moved the watchdog")
    report = apply(lab, check(lab))
    assert not report.ok and report.rolled_back and "read or write the PLC differently" in report.message
    assert "ion_beam.system.plc_watchdog: (10, 196, 0, 'INT', True) -> (10, 198, 0, 'INT', True)" in report.message
    assert head(lab) == lab.first and json.loads(lab.registry.read_text()) == TAGS


def test_the_registry_is_built_on_the_one_in_use(lab):
    """The builder keeps a registry's hand-edited fields (labels, units...): the new registry is
    built on a copy of the one in use, as building it in place does."""
    labelled = json.loads(json.dumps(TAGS))
    labelled["vacuum.vg1"]["default_label"] = "Source gauge"
    lab.registry.write_text(json.dumps(labelled))
    seeds = []

    class Keeping(FakeLauncher):
        def build(self, out):
            seeds.append(json.loads(Path(out).read_text()))
            built = json.loads((self.root / "registry_source.json").read_text())
            for tag, meta in seeds[-1].items():
                if tag in built and "default_label" in meta:
                    built[tag]["default_label"] = meta["default_label"]
            Path(out).write_text(json.dumps(built))
            return ""
    release(lab, {"src/app.py": "VERSION = 2\n"}, "Two")
    report = apply(lab, check(lab), Keeping(lab.pc))
    assert report.ok and seeds == [labelled, labelled]
    assert json.loads(lab.registry.read_text())["vacuum.vg1"]["default_label"] == "Source gauge"


def test_a_version_that_doesnt_come_up_is_taken_back_out(lab):
    release(lab, {"src/app.py": "VERSION = 2\n"}, "Two")
    launcher = FakeLauncher(lab.pc)
    launcher.health = "service_plc isn't ONLINE"
    report = apply(lab, check(lab), launcher)
    assert not report.ok and report.rolled_back and "didn't come up: service_plc isn't ONLINE" in report.message
    assert launcher.calls.count("stop") == 2 and launcher.calls.count("start") == 2
    assert head(lab) == lab.first
    assert "haven't all come back" in report.advice


def test_a_checkout_that_fails_half_way_is_undone(lab):
    release(lab, {"src/app.py": "VERSION = 2\n", "src/more.py": "y = 1\n"}, "Two")
    write(lab.pc, {"config/recipes/r1.json": {"steps": [5]}})
    plan = check(lab)

    class Flaky(updater.Git):
        def __call__(self, *args, **kw):
            if args[:2] == ("checkout", "-B"):
                (lab.pc / "src" / "app.py").write_text("half\n")         # git got part of the way
                raise updater.GitError("git checkout failed: unable to unlink old 'src/app.py': Permission denied")
            return super().__call__(*args, **kw)
    report = updater.apply(plan, FakeLauncher(lab.pc).hooks(), git=Flaky(str(lab.pc)), registry_path=str(lab.registry),
                           folder_root=str(lab.updates))
    assert not report.ok and report.rolled_back and "Permission denied" in report.message
    assert head(lab) == lab.first and branch(lab) == "main" and text(lab, "src/app.py") == "VERSION = 1\n"
    assert json.loads(text(lab, "config/recipes/r1.json")) == {"steps": [5]}


def test_nothing_happens_if_things_moved_since_the_check(lab):
    release(lab, {"src/app.py": "VERSION = 2\n"}, "Two")
    plan = check(lab)
    write(lab.pc, {"src/app.py": "VERSION = 1  # edited\n"})
    launcher = FakeLauncher(lab.pc)
    report = apply(lab, plan, launcher)
    assert not report.ok and "changed since the check" in report.message and launcher.calls == []
    run_git(lab.pc, "checkout", "--", "src/app.py")
    release(lab, {"src/app.py": "VERSION = 3\n"}, "Three")
    run_git(lab.pc, "fetch", "-q", "origin", "+refs/heads/lab:refs/remotes/origin/lab")
    report = apply(lab, plan, launcher)
    assert "changed since the check" in report.message and head(lab) == lab.first


def test_a_blocked_plan_is_never_applied(lab):
    release(lab, {"requirements.txt": "scipy\n"}, "Packages")
    launcher = FakeLauncher(lab.pc)
    report = apply(lab, check(lab), launcher)
    assert not report.ok and launcher.calls == [] and head(lab) == lab.first


def test_going_back_to_the_version_before(lab):
    two = release(lab, {"src/app.py": "VERSION = 2\n"}, "Two")
    assert updater.plan_back(str(lab.pc), git=lab.git).error == "no update has been made from the launcher here"
    assert apply(lab, check(lab)).ok
    back = updater.plan_back(str(lab.pc), git=lab.git)
    assert back.state == updater.READY and back.kind == updater.BACK and back.target == lab.first
    assert [c.subject for c in back.commits] == ["Two"]
    report = apply(lab, back)
    assert report.ok and report.message.startswith(f"Back at {lab.first[:7]}")
    assert head(lab) == lab.first and branch(lab) == "lab" and text(lab, "src/app.py") == "VERSION = 1\n"
    plan = check(lab)
    assert plan.target == two and any("went back from this version" in n for n in plan.notes)
    assert updater.plan_back(str(lab.pc), git=lab.git).state == updater.CANT_CHECK      # (once)


def test_an_update_that_never_finished_is_found(lab):
    folder = lab.updates / "20261029-101500"
    folder.mkdir(parents=True)
    (folder / "update.json").write_text(json.dumps({"started": "20261029-101500"}))
    assert updater.unfinished() == str(folder)
    updater.mark_seen(str(folder))
    assert updater.unfinished() is None


# --------------------------------------------------------------------------- the machine
SAFE = {"system.connected": True, updater.RELAY: 0, updater.SOURCE_COLD: 1, "sequencer.recipe.state": "idle"}


@pytest.mark.parametrize("view, running, silent, words", [
    (SAFE, True, None, None),
    (dict(SAFE, **{updater.RELAY: 1}), True, None, "safety relay is on"),
    (dict(SAFE, **{updater.SOURCE_COLD: 0}), True, None, "source isn't cold"),
    ({"system.connected": False}, True, None, "PLC isn't reporting"),
    ({"system.connected": True}, True, None, "safety relay's state isn't known"),
    (dict(SAFE, **{"sequencer.mass_scan.state": "running"}), True, None, "mass scan is running"),
    ({}, False, 10.0, "Wait 50 s more"),
    ({}, False, None, "stopped for a minute"),
    ({}, False, 61.0, None),
])
def test_when_the_machine_can_be_updated(view, running, silent, words):
    reasons = updater.machine_reasons(view, running, silent, [])
    assert (reasons == []) if words is None else (words in " ".join(reasons))


def test_the_other_system_and_open_programs():
    assert "close the simulator" in updater.machine_reasons(SAFE, True, None, [], other_running=True)[0]
    sim = updater.machine_reasons(SAFE, True, None, [], other_running=True, simulation=True)
    assert "update from the real system's launcher" in sim[0]
    assert updater.machine_reasons(SAFE, True, None, [], other_silent_for=70.0, simulation=True) == []
    assert updater.machine_reasons(SAFE, True, None, ["Operator HMI"]) == ["The Operator HMI is open: close it first."]


def test_plc_differences():
    moved = json.loads(json.dumps(TAGS))
    moved["ion_beam.facilities.safety_relay_active"]["writable"] = True
    moved["new.tag"] = {"source": "service_plc_snap7", "db_number": 10, "byte_offset": 300, "datatype": "REAL"}
    moved["vacuum.vg1"]["units"] = "Pa"                                          # not the PLC's: no matter
    assert updater.plc_differences(TAGS, TAGS) == []
    assert updater.plc_differences(TAGS, moved) == [
        "new.tag: added", "ion_beam.facilities.safety_relay_active: (10, 2, 1, 'BOOL', False) -> (10, 2, 1, 'BOOL', True)"]


# --------------------------------------------------------------------------- GitHub
class _Answer:
    def __init__(self, data):
        self.data = json.dumps(data).encode()

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _runs(*runs):
    return lambda req, timeout: _Answer({"check_runs": [dict(zip(("name", "status", "conclusion"), r)) for r in runs]})


@pytest.mark.parametrize("runs, status", [
    ([("quick", "completed", "success"), ("full", "completed", "success")], updater.PASSED),
    ([("quick", "completed", "success"), ("full", "completed", "failure")], updater.FAILED),
    ([("quick", "completed", "success"), ("full", "in_progress", None)], updater.RUNNING),
    ([("quick", "completed", "success"), ("full", "completed", "cancelled"), ("full", "completed", "success")],
     updater.PASSED),                                   # (the same commit pushed to the lab branch too)
    ([], updater.UNKNOWN),
])
def test_the_github_tests(runs, status):
    assert updater.tests_status("joshbird98/IPIDS_Software", "abc", opener=_runs(*runs)) == status


def test_github_not_answering_is_unknown():
    def down(req, timeout):
        raise OSError("no route to host")
    assert updater.tests_status("a/b", "abc", opener=down) == updater.UNKNOWN
    assert updater.tests_status(None, "abc") == updater.UNKNOWN


def test_github_repository_names():
    assert updater.github_repo("https://github.com/joshbird98/IPIDS_Software.git") == "joshbird98/IPIDS_Software"
    assert updater.github_repo("git@github.com:joshbird98/IPIDS_Software") == "joshbird98/IPIDS_Software"
    assert updater.github_repo("C:/repos/origin.git") is None


# --------------------------------------------------------------------------- a version checks itself
def test_this_version_passes_its_own_check():
    assert updater.self_check() == []


def _fake_version(root, updater_py: str, build_py: str = "") -> Path:
    for pkg in ("src", "src/launcher", "src/utils"):
        (root / pkg).mkdir(parents=True, exist_ok=True)
        (root / pkg / "__init__.py").write_text("")
    (root / "src" / "launcher" / "updater.py").write_text(textwrap.dedent(updater_py))
    if build_py:
        (root / "src" / "utils" / "build_registry.py").write_text(textwrap.dedent(build_py))
    return root


def test_the_new_versions_check_runs_in_a_process_of_its_own(tmp_path):
    good = _fake_version(tmp_path / "good", """
        import sys
        print("a library's banner")
        print('["config: max_v out of range"]')
    """)
    assert updater.self_check_of(str(good)) == ["config: max_v out of range"]
    bad = _fake_version(tmp_path / "bad", "raise SystemExit('cannot start')\n")
    with pytest.raises(updater.UpdateFailed, match="didn't run"):
        updater.self_check_of(str(bad))


def test_the_registry_is_built_by_the_versions_own_code(tmp_path):
    root = _fake_version(tmp_path / "v", "", """
        import argparse, json
        ap = argparse.ArgumentParser()
        ap.add_argument("--out")
        json.dump({"a": 1}, open(ap.parse_args().out, "w"))
    """)
    out = tmp_path / "reg.json"
    assert updater.build_registry_into(str(out), str(root)) == "" and json.loads(out.read_text()) == {"a": 1}
    (root / "src" / "utils" / "build_registry.py").write_text("raise ValueError('bad config')\n")
    assert "bad config" in updater.build_registry_into(str(tmp_path / "x.json"), str(root))


def test_the_updates_folder_is_not_in_git():
    r = subprocess.run(["git", "-C", paths.PROJECT_ROOT, "check-ignore", "-q", "data/updates/state.json"])
    assert r.returncode == 0


def test_the_command_line(lab, capsys, monkeypatch):
    release(lab, {"src/app.py": "VERSION = 2\n"}, "Two")
    real = updater.check
    monkeypatch.setattr(updater, "check", lambda root, fetch=True: real(root, fetch=fetch, tests=PASS))
    assert updater.main(["--root", str(lab.pc)]) == 0
    out = capsys.readouterr().out
    assert "An update is available: 1 change" in out and "Two" in out


def test_applying_by_hand(lab, monkeypatch):
    """From a terminal (and the first time, from a copy of the new version: --root): with the services
    stopped for a minute, and UPDATE typed."""
    from src.launcher import programs
    release(lab, {"src/app.py": "VERSION = 2\n"}, "Two")
    plan = check(lab)
    launcher = FakeLauncher(lab.pc, running=False)
    monkeypatch.setattr(updater, "build_registry_into", lambda path, root=None, python=None: launcher.build(path))
    monkeypatch.setattr(updater, "self_check_of", lambda root=None, python=None: launcher.self_check())
    monkeypatch.setenv("SINDIE_REGISTRY", str(lab.registry))
    taken, t, said = {"on": True}, [0.0], []
    monkeypatch.setattr(programs, "_lock_taken", lambda port: taken["on"])

    def sleep(s):
        t[0] += s
    kw = dict(clock=lambda: t[0], sleep=sleep, out=said.append)
    assert updater.apply_by_hand(plan, str(lab.pc), **kw) == 1 and "stop them first" in said[-1]
    taken["on"] = False
    assert updater.apply_by_hand(plan, str(lab.pc), ask=lambda q: "yes", **kw) == 1
    assert t[0] >= updater.SILENT_FOR_S and said[-1] == "Nothing applied." and head(lab) == lab.first
    assert updater.apply_by_hand(plan, str(lab.pc), ask=lambda q: "UPDATE", **kw) == 0
    assert head(lab) == plan.target and "press Start services" in said[-1]


def test_a_version_from_before_the_updater_is_held_to_a_clean_check(lab):
    """The first update, from a version that has no check of its own: the new one may have no problem."""
    class Old(FakeLauncher):
        def self_check(self):
            if "VERSION = 1" in (self.root / "src" / "app.py").read_text():
                raise updater.UpdateFailed("the version's own check didn't run: No module named src.launcher.updater")
            return super().self_check()
    release(lab, {"src/app.py": "VERSION = 2\n"}, "Two")
    report = apply(lab, check(lab), Old(lab.pc))
    assert report.ok and any("can't check itself" in s for s in report.steps)


def test_only_problems_the_new_version_brings_count(lab):
    class Known(FakeLauncher):
        def self_check(self):
            return ["smu_config.json: max_v out of range"] + super().self_check()
    release(lab, {"src/app.py": "VERSION = 2\n"}, "Two")
    assert apply(lab, check(lab), Known(lab.pc)).ok
