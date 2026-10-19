"""Config files changed from the HMI (src/core/config_store.py): checked before writing, backed
up, written whole with their types and indentation kept, rolled back if the registry rebuild
fails, and committed to git."""
import json
import os
import shutil
import subprocess

import pytest

from src.core import config_store as cs
from src.core import paths


@pytest.fixture
def cfg(tmp_path):
    """A copy of the shipped config folder."""
    d = tmp_path / "config"
    d.mkdir()
    for name in cs.config_files(paths.CONFIG_DIR):
        shutil.copy(os.path.join(paths.CONFIG_DIR, name), d / name)
    return d


def edited(name, change, config_dir=paths.CONFIG_DIR):
    data = cs.read(name, config_dir)
    new = json.loads(json.dumps(data))
    change(new)
    return data, new


# --------------------------------------------------------------------------- checks
def test_shipped_configs_have_no_problems():
    names = cs.config_files()
    assert "magnet_config.json" in names and "system_tags.json" not in names
    for name in names:
        data, problems = cs.load(name)
        assert problems == [], (name, problems)


@pytest.mark.parametrize("name, change, words", [
    ("magnet_config.json", lambda d: d.update(max_current=-1.0), "max_current: must be above 0"),
    ("magnet_config.json", lambda d: d["degauss_steps_amps"].__setitem__(0, 120.0), "beyond max_current"),
    ("magnet_config.json", lambda d: d.update(open_circuit_threshold=0.1), "must be below open_circuit_threshold"),
    ("smu_config.json", lambda d: d.update(min_v=300), "min_v (300) must be below max_v"),
    ("plc_config.json", lambda d: d["source_target"].update(min_v=12.0), "source_target.min_v"),
    ("vac_gauges_config.json", lambda d: d["10"]["relays"]["1"].update(on_val=9e-6), "must be below"),
    ("vac_gauges_config.json", lambda d: d["10"]["relays"]["2"].update(on_val=5.55e-6), "2 significant digits"),
    ("vac_gauges_config.json", lambda d: d["20"]["relays"]["1"].update(channel=7), "has no channel 7"),
    ("vac_gauges_config.json", lambda d: d["20"]["relays"].update({"7": d["20"]["relays"]["1"]}), "numbered 1-6"),
    ("network_config.json", lambda d: d["zmq_ports"].update(smu_cmd=5550), "5550 is also plc_pub"),
    ("network_config.json", lambda d: d["plc"].update(ip="192.168.1.300"), "is not an IP address"),
    ("mpd_config.json", lambda d: d["arc_detection"].update(collapse_fraction=1.5), "collapse_fraction"),
    ("mpd_config.json", lambda d: d["buses"][0]["devices"]["source_einzel"].update(max_value=-5.0), "max_value"),
    ("mpd_config.json", lambda d: d["buses"][0]["devices"]["source_einzel"].update(default_current_ua=400.0),
     "default_current_ua (400) must be at most buses[0].devices.source_einzel.max_current_ua (330)"),
    ("mpd_config.json", lambda d: d["buses"][0]["devices"]["source_einzel"].update(default_current_ua=0),
     "default_current_ua: must be above 0"),
    ("logger_config.json", lambda d: d.update(tick_rate_seconds=0), "tick_rate_seconds: must be at least 0.01"),
    ("theme_config.json", lambda d: d.update(theme_mode="purple"), "theme_mode"),
    ("theme_config.json", lambda d: d["dark_palette"].update(accent="blue"), "must be a colour"),
    ("fault_config.json", lambda d: next(iter(d.values()))["bits"].update({"4.0": {"name": "X"}}), "byte 0-3"),
    ("alarm_config.json", lambda d: d["overrides"].update({"x": {"priority": 7}}), "priority: must be at most 4"),
    ("alarm_config.json", lambda d: d.setdefault("rules", []).append({"kind": "telepathy"}), "telepathy"),
])
def test_bad_values_are_named(name, change, words):
    _, new = edited(name, change)
    problems = cs.check(name, new)                 # the file's own rules (structure: next test)
    assert any(words in p for p in problems), problems


def test_structure_must_not_change():
    original, new = edited("magnet_config.json", lambda d: d.update(max_current="lots", spare=1))
    del new["max_power"]
    problems = cs.check("magnet_config.json", new, original)
    assert "max_current: must be a number" in problems
    assert "(file): missing max_power" in problems and "(file): unexpected spare" in problems
    original, new = edited("smu_config.json", lambda d: d.update(min_v=-150.5))     # was a whole number:
    assert cs.check("smu_config.json", new, original) == []                         # a fraction is fine...
    original, new = edited("network_config.json", lambda d: d["plc"].update(rack=0.5))
    assert cs.check("network_config.json", new, original) == ["plc.rack: must be a whole number (is 0.5)"]  # ...here not


def test_device_names_are_fixed():
    """The Spellman service routes by device name: a renamed supply would lose its telemetry and
    commands and the PLC would see it as healthy for ever. So names can't change."""
    original = cs.read("mpd_config.json")
    renamed = json.loads(json.dumps(original))
    devices = renamed["buses"][0]["devices"]
    devices["einzel_1"] = devices.pop("source_einzel")
    problems = cs.check("mpd_config.json", renamed, original)
    assert "buses[0].devices: missing source_einzel" in problems
    assert any("unknown supply" in p and "source_einzel" in p for p in cs.check("mpd_config.json", renamed))


@pytest.mark.parametrize("name, change, words", [
    ("vac_gauges_config.json", lambda d: d["10"]["relays"]["1"].update(on_val=5e4, off_val=6e4), "at most 1000"),
    ("vac_gauges_config.json", lambda d: d["system_interlocks"].update(rapid_rise_thresh_mb_s=1e9), "at most 1000"),
    ("magnet_config.json", lambda d: d.update(max_current=950.0), "above max_voltage"),     # 95.0 mistyped
    ("mpd_config.json", lambda d: d["arc_detection"].update(confirm_polls=1000000), "at most 10"),
    ("logger_config.json", lambda d: d.update(tick_rate_seconds=0.0005), "at least 0.01"),
    ("logger_config.json", lambda d: d.update(flush_interval_seconds=900000.0), "at most 3600"),
    ("logger_config.json", lambda d: d.update(tick_rate_seconds=0.01, flush_interval_seconds=3600.0), "100,000"),
    ("alarm_config.json", lambda d: d["rules"][1].update(priority=7), "rules[1].priority: must be at most 4"),
])
def test_physical_and_sanity_bounds(name, change, words):
    _, new = edited(name, change)
    problems = cs.check(name, new)
    assert any(words in p for p in problems), problems


def test_large_changes_need_a_second_look():
    old = {"max_current": 95.0, "steps": [50.0, 0.0], "min": 0, "bias": -10.0, "same": 1.0, "priority": 1,
           "relay": 5e-6}
    new = {"max_current": 950.0, "steps": [52.0, 5.0], "min": 2, "bias": 10.0, "same": 1.0, "priority": 4,
           "relay": 5e-5}
    assert cs.large_changes(old, new) == ["max_current: 95 -> 950 (x10)", "bias: -10 -> 10 (sign change)",
                                          "relay: 5e-06 -> 5e-05 (x10)"]
    # not flagged: to/from zero (no ratio), small whole numbers (priorities, counts)
    assert cs.large_changes({"a": 9.0}, {"a": 2.0}) == ["a: 9 -> 2 (/4.5)"]
    assert cs.large_changes({"a": 9.0}, {"a": 4.0}) == []
    assert cs.large_changes({"n": 20}, {"n": 200}) == ["n: 20 -> 200 (x10)"]


def test_diff_lists_changed_values():
    old = {"a": 1.0, "b": {"c": [1, 2]}, "d": "x"}
    new = {"a": 1.5, "b": {"c": [1, 3]}, "d": "x"}
    assert cs.diff(old, new) == ["a: 1.0 -> 1.5", "b.c[1]: 2 -> 3"]
    assert cs.diff(old, old) == []


# --------------------------------------------------------------------------- saving
def test_save_backs_up_and_keeps_types_and_indentation(cfg):
    original, new = edited("network_config.json", lambda d: d["smu"].update(port=5026), str(cfg))
    before = (cfg / "network_config.json").read_text()
    result = cs.save("network_config.json", new, original, config_dir=str(cfg), commit=False)
    assert result.saved and result.problems == [] and result.error == ""
    after = (cfg / "network_config.json").read_text()
    assert after.splitlines()[1].startswith("    \"")                      # 4-space file stays 4-space
    changed = [(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
    assert changed == [('        "port": 5025', '        "port": 5026')]   # one value, one line
    assert open(result.backup).read() == before
    assert not list(cfg.glob("*.tmp"))


def test_floats_survive_exactly(cfg):
    data = {"a": 0.123456789012, "b": 5.5e-06, "c": [], "d": None, "e": {}, "note": "50 µA, 20 °C"}
    (cfg / "x.json").write_text(json.dumps(data))
    result = cs.save("x.json", dict(data, a=0.987654321098), data, config_dir=str(cfg), commit=False)
    assert result.saved
    assert json.loads((cfg / "x.json").read_text()) == dict(data, a=0.987654321098)
    (cfg / "x.json").read_bytes().decode("ascii")             # readable in any Windows code page


def test_problems_mean_nothing_is_written(cfg):
    original, new = edited("magnet_config.json", lambda d: d.update(max_current=0.0), str(cfg))
    before = (cfg / "magnet_config.json").read_text()
    result = cs.save("magnet_config.json", new, original, config_dir=str(cfg), commit=False)
    assert not result.saved and result.problems and result.backup is None
    assert (cfg / "magnet_config.json").read_text() == before
    assert cs.backups("magnet_config.json", str(cfg)) == []


def test_failed_registry_rebuild_puts_the_old_file_back(cfg):
    original, new = edited("magnet_config.json", lambda d: d.update(max_current=90.0), str(cfg))
    before = (cfg / "magnet_config.json").read_text()
    result = cs.save("magnet_config.json", new, original, config_dir=str(cfg), commit=False,
                     after_write=lambda: "rebuilding system_tags.json failed (exit 1): boom")
    assert not result.saved and "boom" in result.error and "put back" in result.error
    assert (cfg / "magnet_config.json").read_text() == before


def test_backups_are_pruned_and_restorable(cfg, monkeypatch):
    monkeypatch.setattr(cs, "KEEP_BACKUPS", 3)
    clock = iter(range(1_700_000_000, 1_700_000_100, 10))
    name = "smu_config.json"
    for v in (1, 2, 3, 4, 5):
        data = cs.read(name, str(cfg))
        assert cs.save(name, dict(data, max_v=190 + v), data, config_dir=str(cfg), commit=False,
                       clock=lambda: next(clock)).saved
    kept = cs.backups(name, str(cfg))
    assert len(kept) == 3 and kept == sorted(kept, reverse=True)
    assert json.load(open(kept[0]))["max_v"] == 194                   # newest backup: before the last save
    result = cs.restore(name, kept[-1], config_dir=str(cfg), commit=False)
    assert result.saved and cs.read(name, str(cfg))["max_v"] == 192
    assert len(cs.backups(name, str(cfg))) == 3


def test_same_second_backups_do_not_overwrite(cfg):
    name = "smu_config.json"
    data = cs.read(name, str(cfg))
    first = cs.save(name, dict(data, max_v=150), data, config_dir=str(cfg), commit=False, clock=lambda: 1e9)
    second = cs.save(name, dict(data, max_v=160), dict(data, max_v=150), config_dir=str(cfg), commit=False,
                     clock=lambda: 1e9)
    assert first.backup != second.backup and os.path.exists(first.backup)
    assert cs.backup_time(first.backup).count(":") == 2


def test_unreadable_file_is_a_config_error(cfg):
    (cfg / "smu_config.json").write_text("{\"min_v\": -200,,}")
    with pytest.raises(cs.ConfigError, match="not valid JSON"):
        cs.load("smu_config.json", str(cfg))


def test_only_changed_values_are_rewritten(cfg):
    """The file keeps its layout and the rest of its text: one change is one line in git
    (writing the whole file used to turn fault_config.json's 141 lines into 562)."""
    text = (cfg / "fault_config.json").read_text()
    data, new = json.loads(text), json.loads(text)
    udt = next(iter(new))
    new[udt]["bits"][next(iter(new[udt]["bits"]))]["description"] = "changed"
    assert cs.save("fault_config.json", new, data, config_dir=str(cfg), commit=False).saved
    after = (cfg / "fault_config.json").read_text()
    assert len(after.splitlines()) == len(text.splitlines())
    assert len([1 for a, b in zip(text.splitlines(), after.splitlines()) if a != b]) == 1
    d = cs.read("smu_config.json", str(cfg))
    assert cs.save("smu_config.json", dict(d, max_v=150), d, config_dir=str(cfg), commit=False).saved
    assert "200e-6" in (cfg / "smu_config.json").read_text()            # untouched values stay as typed


def test_value_spans_cover_every_scalar():
    text = '{"a": [1, 2.5e-3, "x"], "b": {"c": null, "d": true, "e": []}, "f": "q\\"uote"}'
    spans = cs.value_spans(text)
    assert {k: text[a:b] for k, (a, b) in spans.items()} == {
        ("a", 0): "1", ("a", 1): "2.5e-3", ("a", 2): '"x"', ("b", "c"): "null", ("b", "d"): "true",
        ("f",): '"q\\"uote"'}
    for bad in ('{"a": 1,}', '{"a": 1} x', '{"a": tru}', '[1, 2'):
        with pytest.raises(ValueError):
            cs.value_spans(bad)


def test_a_file_changed_on_disk_is_not_overwritten(cfg):
    data = cs.read("smu_config.json", str(cfg))
    (cfg / "smu_config.json").write_text(json.dumps(dict(data, max_v=180)))        # someone else's change
    result = cs.save("smu_config.json", dict(data, min_v=-150), data, expected=data, config_dir=str(cfg),
                     commit=False)
    assert not result.saved and "changed on disk" in result.problems[0]
    assert cs.read("smu_config.json", str(cfg))["max_v"] == 180


def test_windows_ansi_files_are_read_and_kept(cfg):
    raw = '{\n  "note": "50 µA at 20 °C",\n  "max_v": 200\n}\n'.encode("cp1252")
    (cfg / "x.json").write_bytes(raw)
    data, _ = cs.load("x.json", str(cfg))
    assert data["note"] == "50 µA at 20 °C"
    assert cs.save("x.json", dict(data, max_v=150), data, config_dir=str(cfg), commit=False).saved
    assert (cfg / "x.json").read_bytes() == raw.replace(b"200", b"150")    # same encoding, one value changed


def test_backups_list_newest_first_within_a_second(cfg):
    name = "smu_config.json"
    data = cs.read(name, str(cfg))
    cs.save(name, dict(data, max_v=150), data, config_dir=str(cfg), commit=False, clock=lambda: 1e9)
    cs.save(name, dict(data, max_v=160), dict(data, max_v=150), config_dir=str(cfg), commit=False, clock=lambda: 1e9)
    newest, older = cs.backups(name, str(cfg))
    assert newest.endswith("-1.json") and cs.read_path(newest)["max_v"] == 150
    assert cs.read_path(older)["max_v"] == 200


def test_restore_puts_the_backup_back_byte_for_byte(cfg):
    name = "smu_config.json"
    before = (cfg / name).read_bytes()
    data = cs.read(name, str(cfg))
    result = cs.save(name, dict(data, max_v=150), data, config_dir=str(cfg), commit=False)
    assert cs.restore(name, result.backup, config_dir=str(cfg), commit=False).saved
    assert (cfg / name).read_bytes() == before


def test_restore_is_byte_for_byte_across_encodings(cfg):
    """A cp1252 backup put back over a UTF-8 file (or the other way round) comes back exactly."""
    ansi = '{\n  "note": "50 \u00b5A",\n  "max_v": 200\n}\n'.encode("cp1252")
    (cfg / "x.json").write_bytes(ansi)
    data = cs.read("x.json", str(cfg))
    r = cs.save("x.json", dict(data, max_v=150), data, config_dir=str(cfg), commit=False)
    (cfg / "x.json").write_bytes('{"note": "\u03a9", "max_v": 150}'.encode("utf-8"))   # now UTF-8, with an Omega
    assert cs.restore("x.json", r.backup, config_dir=str(cfg), commit=False).saved
    assert (cfg / "x.json").read_bytes() == ansi


def test_byte_order_mark_and_crlf(cfg):
    (cfg / "x.json").write_bytes(b'\xef\xbb\xbf{\r\n  "a": 1,\r\n  "b": 2\r\n}\r\n')
    data = cs.read("x.json", str(cfg))                                  # the BOM used to make it unreadable
    assert cs.save("x.json", dict(data, a=5), data, config_dir=str(cfg), commit=False).saved
    assert (cfg / "x.json").read_bytes() == b'{\r\n  "a": 5,\r\n  "b": 2\r\n}\r\n'   # no BOM, CRLF kept
    full = cs._full_text({"a": 1, "b": [2]}, '{\r\n    "a": 0\r\n}\r\n')       # a whole rewrite keeps CRLF too
    assert full.count("\r\n") == full.count("\n") and '\r\n    "a": 1' in full


def test_a_type_only_change_on_disk_counts_as_a_change(cfg):
    data = cs.read("smu_config.json", str(cfg))
    (cfg / "smu_config.json").write_text(json.dumps(dict(data, max_v=200.0)))      # 200 -> 200.0
    result = cs.save("smu_config.json", dict(data, min_v=-150), data, expected=data, config_dir=str(cfg),
                     commit=False)
    assert not result.saved and "changed on disk" in result.problems[0]
    assert cs.same_json({"a": 1, "b": [True]}, {"b": [True], "a": 1})            # key order doesn't matter
    assert not cs.same_json({"a": 1}, {"a": 1.0}) and not cs.same_json([1], [True])


def test_alarm_shelving_is_bounded():
    _, new = edited("alarm_config.json", lambda d: d.update(shelve_max_minutes=1e6))
    assert any("shelve_max_minutes: must be at most 1440" in p for p in cs.check("alarm_config.json", new))


# --------------------------------------------------------------------------- git
def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    for var in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"):
        monkeypatch.setenv(var, os.devnull)                  # no identity configured, like a fresh PC
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    _git(root, "init", "-q")
    (root / "config" / "smu_config.json").write_text(json.dumps({"min_v": -200, "max_v": 200,
                                                                 "compliance_current": 2e-4}, indent=2))
    (root / "other.txt").write_text("x")
    (root / ".gitignore").write_text("config/local.json\n")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    return root


def test_saves_are_committed_alone(repo):
    cfg = str(repo / "config")
    (repo / "other.txt").write_text("staged, not part of the config change")
    _git(repo, "add", "other.txt")
    data = cs.read("smu_config.json", cfg)
    result = cs.save("smu_config.json", dict(data, max_v=150), data, reason="new SMU", config_dir=cfg)
    assert result.saved and result.committed, result.git
    log = _git(repo, "log", "-1", "--format=%an|%s", "--name-only")
    assert log.startswith("SINDIE HMI|Config editor: smu_config.json - new SMU")
    assert "other.txt" not in log
    assert "other.txt" in _git(repo, "diff", "--cached", "--name-only")       # still staged, untouched


def test_git_problems_are_reported_not_raised(repo, tmp_path):
    ok, why = cs.git_commit(str(tmp_path / "loose.json"), "x")
    assert not ok and "not in a git work tree" in why
    (repo / "config" / "local.json").write_text("{}")
    ok, why = cs.git_commit(str(repo / "config" / "local.json"), "x")
    assert not ok and why == "file is ignored by git"
    ok, why = cs.git_commit(str(repo / "config" / "smu_config.json"), "x")
    assert not ok and why == "no change to commit"

    def no_git(*a, **k):
        raise FileNotFoundError("git")
    ok, why = cs.git_commit(str(repo / "config" / "smu_config.json"), "x", run=no_git)
    assert not ok and why.startswith("git not available")
