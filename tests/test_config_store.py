"""Config files changed from the HMI (src/core/config_store.py): checked before writing, backed
up, saved as this machine's lab settings (only what differs from the repository's file, which is
left untouched), put back if the registry rebuild fails, and never committed to git."""
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
def test_per_machine_state_is_not_offered_for_editing(tmp_path):
    """The engineering editor lists configuration only: not the viewer's saved state or the theme
    choice (applying an edit would restart the services for a cosmetic file)."""
    for name in ("magnet_config.json", "appearance.json", "workspace_profiles.json", "system_tags.json"):
        (tmp_path / name).write_text("{}")
    assert cs.config_files(str(tmp_path)) == ["magnet_config.json"]


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
    ("vac_gauges_config.json", lambda d: d["10"]["relays"]["1"].update(on_val=12.0), "must be below"),
    ("vac_gauges_config.json", lambda d: d["system_interlocks"]["software_trips"]["VG1"].update(reset_mbar=9e-6),
     "reset_mbar (9e-06) must be at most trip_mbar (8e-06)"),
    ("vac_gauges_config.json", lambda d: d["system_interlocks"]["software_trips"].update(VG7={}), "not a gauge"),
    ("vac_gauges_config.json", lambda d: d["system_interlocks"]["software_trips"]["VG1"].update(ride_through_s=0),
     "ride_through_s: must be a number above 0"),
    ("vac_gauges_config.json", lambda d: d["10"]["relays"]["2"].update(on_val=5.55e-6), "2 significant digits"),
    ("vac_gauges_config.json", lambda d: d["20"]["relays"]["1"].update(channel=7), "has no channel 7"),
    ("vac_gauges_config.json", lambda d: d["20"]["relays"].update({"7": d["20"]["relays"]["1"]}), "numbered 1-6"),
    ("vac_gauges_config.json", lambda d: d["system_interlocks"].update(safety_loop_relays=["10.7"]),
     "not a controller node and relay 1-6"),
    ("vac_gauges_config.json", lambda d: d["system_interlocks"].update(safety_loop_relays=["30.1"]),
     "controller 30 has no relay 1"),
    ("vac_gauges_config.json", lambda d: d["system_interlocks"].update(safety_loop_relays="10.4"), "must be a list"),
    ("network_config.json", lambda d: d["zmq_ports"].update(smu_cmd=5550), "5550 is also plc_pub"),
    ("network_config.json", lambda d: d["plc"].update(ip="192.168.1.300"), "is not an IP address"),
    ("mpd_config.json", lambda d: d["arc_detection"].update(collapse_fraction=1.5), "collapse_fraction"),
    ("mpd_config.json", lambda d: d["buses"][0]["devices"]["source_einzel"].update(max_value=-5.0), "max_value"),
    ("mpd_config.json", lambda d: d["buses"][0]["devices"]["source_einzel"].update(default_current_ua=400.0),
     "default_current_ua (400) must be at most buses[0].devices.source_einzel.max_current_ua (330)"),
    ("mpd_config.json", lambda d: d["buses"][0]["devices"]["source_einzel"].update(default_current_ua=0),
     "default_current_ua: must be above 0"),
    ("logger_config.json", lambda d: d.update(tick_rate_seconds=0), "tick_rate_seconds: must be at least 0.01"),
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


# --------------------------------------------------------------------------- saving (lab settings)
def lab_file(cfg, name):
    return cfg / "lab" / name


def test_save_writes_only_what_differs_as_lab_settings(cfg):
    """A change is this machine's lab settings: config/lab/<name> holds only what differs, the
    repository's file is left exactly as it was (so a software update never meets a local change),
    and the file as the system uses it is the repository's with the lab settings on top."""
    original, new = edited("network_config.json", lambda d: d["smu"].update(port=5026), str(cfg))
    before = (cfg / "network_config.json").read_bytes()
    result = cs.save("network_config.json", new, original, config_dir=str(cfg))
    assert result.saved and result.problems == [] and result.error == ""
    assert (cfg / "network_config.json").read_bytes() == before
    assert result.path == str(lab_file(cfg, "network_config.json"))
    assert json.loads(lab_file(cfg, "network_config.json").read_text()) == {"smu": {"port": 5026}} == result.lab
    assert cs.read("network_config.json", str(cfg)) == new
    assert cs.lab_settings("network_config.json", str(cfg)) == {"smu": {"port": 5026}}
    assert cs.repository_value("network_config.json", str(cfg)) == original
    assert json.load(open(result.backup)) == original                     # the file as it was used
    assert not list(cfg.glob("**/*.tmp"))
    again = cs.save("network_config.json", original, new, config_dir=str(cfg))      # back to the repository's
    assert again.saved and again.lab == {} and json.loads(lab_file(cfg, "network_config.json").read_text()) == {}


def test_floats_survive_exactly(cfg):
    data = {"a": 0.123456789012, "b": 5.5e-06, "c": [], "d": None, "e": {}, "note": "50 µA, 20 °C"}
    (cfg / "x.json").write_text(json.dumps(data))
    result = cs.save("x.json", dict(data, a=0.987654321098, note="5 µA"), data, config_dir=str(cfg))
    assert result.saved and result.lab == {"a": 0.987654321098, "note": "5 µA"}
    assert cs.read("x.json", str(cfg)) == dict(data, a=0.987654321098, note="5 µA")
    lab_file(cfg, "x.json").read_bytes().decode("ascii")      # readable in any Windows code page


def test_problems_mean_nothing_is_written(cfg):
    original, new = edited("magnet_config.json", lambda d: d.update(max_current=0.0), str(cfg))
    result = cs.save("magnet_config.json", new, original, config_dir=str(cfg))
    assert not result.saved and result.problems and result.backup is None
    assert not lab_file(cfg, "magnet_config.json").exists()
    assert cs.backups("magnet_config.json", str(cfg)) == []


def test_failed_registry_rebuild_puts_the_previous_lab_settings_back(cfg):
    original, first = edited("magnet_config.json", lambda d: d.update(max_current=90.0), str(cfg))
    assert cs.save("magnet_config.json", first, original, config_dir=str(cfg)).saved
    kept = lab_file(cfg, "magnet_config.json").read_bytes()
    result = cs.save("magnet_config.json", dict(first, max_current=80.0), first, config_dir=str(cfg),
                     after_write=lambda: "rebuilding system_tags.json failed (exit 1): boom")
    assert not result.saved and "boom" in result.error and "put back" in result.error
    assert lab_file(cfg, "magnet_config.json").read_bytes() == kept
    assert cs.read("magnet_config.json", str(cfg)) == first
    name = "smu_config.json"                        # no lab settings before: none after (an empty file)
    data = cs.read(name, str(cfg))
    assert not cs.save(name, dict(data, max_v=150), data, config_dir=str(cfg), after_write=lambda: "no").saved
    assert cs.read(name, str(cfg)) == data and cs.lab_settings(name, str(cfg)) == {}


def test_backups_are_pruned_and_restorable(cfg, monkeypatch):
    monkeypatch.setattr(cs, "KEEP_BACKUPS", 3)
    clock = iter(range(1_700_000_000, 1_700_000_100, 10))
    name = "smu_config.json"
    for v in (1, 2, 3, 4, 5):
        data = cs.read(name, str(cfg))
        assert cs.save(name, dict(data, max_v=190 + v), data, config_dir=str(cfg),
                       clock=lambda: next(clock)).saved
    kept = cs.backups(name, str(cfg))
    assert len(kept) == 3 and kept == sorted(kept, reverse=True)
    assert json.load(open(kept[0]))["max_v"] == 194                   # newest backup: before the last save
    result = cs.restore(name, kept[-1], config_dir=str(cfg))
    assert result.saved and cs.read(name, str(cfg))["max_v"] == 192
    assert len(cs.backups(name, str(cfg))) == 3


def test_same_second_backups_do_not_overwrite(cfg):
    name = "smu_config.json"
    data = cs.read(name, str(cfg))
    first = cs.save(name, dict(data, max_v=150), data, config_dir=str(cfg), clock=lambda: 1e9)
    second = cs.save(name, dict(data, max_v=160), dict(data, max_v=150), config_dir=str(cfg), clock=lambda: 1e9)
    assert first.backup != second.backup and os.path.exists(first.backup)
    assert cs.backup_time(first.backup).count(":") == 2


def test_unreadable_file_is_a_config_error(cfg):
    (cfg / "smu_config.json").write_text("{\"min_v\": -200,,}")
    with pytest.raises(cs.ConfigError, match="not valid JSON"):
        cs.load("smu_config.json", str(cfg))
    (cfg / "lab").mkdir()
    lab_file(cfg, "magnet_config.json").write_text("[1, 2]")             # a lab file must hold an object
    with pytest.raises(cs.ConfigError, match="config/lab/magnet_config.json"):
        cs.load("magnet_config.json", str(cfg))


def test_a_file_changed_since_it_was_opened_is_not_overwritten(cfg):
    """By a software update (the repository's file), or by another editor (the lab settings)."""
    data = cs.read("smu_config.json", str(cfg))
    (cfg / "smu_config.json").write_text(json.dumps(dict(data, max_v=180)))        # an update's change
    result = cs.save("smu_config.json", dict(data, min_v=-150), data, expected=data, config_dir=str(cfg))
    assert not result.saved and "has changed since it was opened" in result.problems[0]
    assert cs.read("smu_config.json", str(cfg))["max_v"] == 180
    now = cs.read("smu_config.json", str(cfg))
    assert cs.save("smu_config.json", dict(now, min_v=-150), now, config_dir=str(cfg)).saved   # another editor
    result = cs.save("smu_config.json", dict(now, min_v=-100), now, expected=now, config_dir=str(cfg))
    assert not result.saved and cs.read("smu_config.json", str(cfg))["min_v"] == -150


def test_windows_ansi_files_are_read(cfg):
    raw = '{\n  "note": "50 µA at 20 °C",\n  "max_v": 200\n}\n'.encode("cp1252")
    (cfg / "x.json").write_bytes(raw)
    data, _ = cs.load("x.json", str(cfg))
    assert data["note"] == "50 µA at 20 °C"
    assert cs.save("x.json", dict(data, max_v=150), data, config_dir=str(cfg)).saved
    assert (cfg / "x.json").read_bytes() == raw                                     # the repository's: untouched
    assert cs.read("x.json", str(cfg)) == dict(data, max_v=150)


def test_backups_list_newest_first_within_a_second(cfg):
    name = "smu_config.json"
    data = cs.read(name, str(cfg))
    cs.save(name, dict(data, max_v=150), data, config_dir=str(cfg), clock=lambda: 1e9)
    cs.save(name, dict(data, max_v=160), dict(data, max_v=150), config_dir=str(cfg), clock=lambda: 1e9)
    newest, older = cs.backups(name, str(cfg))
    assert newest.endswith("-1.json") and cs.read_path(newest)["max_v"] == 150
    assert cs.read_path(older)["max_v"] == 200


def test_restore_puts_the_backup_back(cfg):
    name = "smu_config.json"
    data = cs.read(name, str(cfg))
    result = cs.save(name, dict(data, max_v=150), data, config_dir=str(cfg))
    assert cs.restore(name, result.backup, config_dir=str(cfg)).saved
    assert cs.read(name, str(cfg)) == data and cs.lab_settings(name, str(cfg)) == {}


def test_a_whole_file_backup_from_before_lab_settings_is_put_back_as_lab_settings(cfg):
    """Backups made before lab settings are whole files; putting one back keeps only what differs."""
    name = "smu_config.json"
    data = cs.read(name, str(cfg))
    (cfg / "backups").mkdir()
    old = cfg / "backups" / "smu_config.20261020-101010.json"
    old.write_text(json.dumps(dict(data, max_v=175), indent=4))
    assert cs.backups(name, str(cfg)) == [str(old)]
    result = cs.restore(name, str(old), config_dir=str(cfg))
    assert result.saved and result.lab == {"max_v": 175} and cs.read(name, str(cfg)) == dict(data, max_v=175)


def test_byte_order_mark_and_crlf(cfg):
    (cfg / "x.json").write_bytes(b'\xef\xbb\xbf{\r\n  "a": 1,\r\n  "b": 2\r\n}\r\n')
    data = cs.read("x.json", str(cfg))                                  # the BOM used to make it unreadable
    assert cs.save("x.json", dict(data, a=5), data, config_dir=str(cfg)).saved
    assert (cfg / "x.json").read_bytes() == b'\xef\xbb\xbf{\r\n  "a": 1,\r\n  "b": 2\r\n}\r\n'
    assert cs.read("x.json", str(cfg)) == {"a": 5, "b": 2}
    full = cs._full_text({"a": 1, "b": [2]}, '{\r\n    "a": 0\r\n}\r\n')       # (recipes) a whole rewrite keeps CRLF
    assert full.count("\r\n") == full.count("\n") and '\r\n    "a": 1' in full


def test_a_type_only_change_counts_as_a_change(cfg):
    data = cs.read("smu_config.json", str(cfg))
    (cfg / "smu_config.json").write_text(json.dumps(dict(data, max_v=200.0)))      # 200 -> 200.0
    result = cs.save("smu_config.json", dict(data, min_v=-150), data, expected=data, config_dir=str(cfg))
    assert not result.saved and "has changed since it was opened" in result.problems[0]
    assert cs.same_json({"a": 1, "b": [True]}, {"b": [True], "a": 1})            # key order doesn't matter
    assert not cs.same_json({"a": 1}, {"a": 1.0}) and not cs.same_json([1], [True])


def test_only_changed_values_are_rewritten_in_a_text():
    """(The recipe files' writing: a recipe keeps its layout, one change is one line in git.)"""
    text = '{\n    "a": 200e-6,\n    "b": {"c": [1, 2]},\n    "d": "x"\n}\n'
    out = cs.patched_text(text, {"a": 0.0002, "b": {"c": [1, 3]}, "d": "x"})
    assert out == '{\n    "a": 200e-6,\n    "b": {"c": [1, 3]},\n    "d": "x"\n}\n'     # as typed, one value
    assert cs.patched_text(text, {"a": 1}) is None                               # another structure


def test_value_spans_cover_every_scalar():
    text = '{"a": [1, 2.5e-3, "x"], "b": {"c": null, "d": true, "e": []}, "f": "q\\"uote"}'
    spans = cs.value_spans(text)
    assert {k: text[a:b] for k, (a, b) in spans.items()} == {
        ("a", 0): "1", ("a", 1): "2.5e-3", ("a", 2): '"x"', ("b", "c"): "null", ("b", "d"): "true",
        ("f",): '"q\\"uote"'}
    for bad in ('{"a": 1,}', '{"a": 1} x', '{"a": tru}', '[1, 2'):
        with pytest.raises(ValueError):
            cs.value_spans(bad)


def test_alarm_shelving_is_bounded():
    _, new = edited("alarm_config.json", lambda d: d.update(shelve_max_minutes=1e6))
    assert any("shelve_max_minutes: must be at most 1440" in p for p in cs.check("alarm_config.json", new))


# --------------------------------------------------------------------------- git
def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def test_saving_never_touches_git(tmp_path):
    """The lab PC's work tree stays as the software updates leave it: no commits, no changed files
    (the editor used to commit each save, which left the lab PC with commits of its own)."""
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    _git(root, "init", "-q")
    (root / "config" / "smu_config.json").write_text(json.dumps({"min_v": -200, "max_v": 200,
                                                                 "compliance_current": 2e-4}, indent=2))
    (root / ".gitignore").write_text("config/lab/\nconfig/backups/\n")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    head = _git(root, "rev-parse", "HEAD")
    cfg = str(root / "config")
    data = cs.read("smu_config.json", cfg)
    assert cs.save("smu_config.json", dict(data, max_v=150), data, reason="new SMU", config_dir=cfg).saved
    assert _git(root, "rev-parse", "HEAD") == head and _git(root, "status", "--porcelain") == ""
    assert cs.read("smu_config.json", cfg)["max_v"] == 150
