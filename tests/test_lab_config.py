"""Lab settings (src/core/lab_config.py): config/lab/<name>.json, not in git, holds only what this
machine changes; the system reads the repository's file with it on top. Software updates change
the repository's files and never meet a local change."""
import json
import os
import re
import shutil

import pytest

from src.core import config_store, lab_config, paths

ROOT = paths.PROJECT_ROOT


# --------------------------------------------------------------------------- merging
def test_merge_is_key_by_key_and_lists_replace():
    base = {"a": 1, "b": {"c": 2, "d": [1, 2]}, "e": "x", "f": {"g": 1}}
    lab = {"b": {"c": 3, "d": [9]}, "e": lab_config.REMOVED, "h": True}
    assert lab_config.merge(base, lab) == {"a": 1, "b": {"c": 3, "d": [9]}, "f": {"g": 1}, "h": True}
    assert base == {"a": 1, "b": {"c": 2, "d": [1, 2]}, "e": "x", "f": {"g": 1}}         # neither is changed
    assert lab_config.merge({"a": {"b": 1}}, {"a": 5}) == {"a": 5}                      # a value over an object
    assert lab_config.merge({"a": 5}, {"a": {"b": 1}}) == {"a": {"b": 1}}


def test_diff_keeps_only_what_differs_and_gives_the_data_back():
    base = {"a": 1, "b": {"c": 2, "d": [1, 2], "e": None}, "f": 1.0, "g": "x"}
    data = {"a": 1, "b": {"c": 3, "d": [1, 2], "e": None}, "f": 1, "h": {"i": []}}
    lab = lab_config.diff(base, data)
    assert lab == {"b": {"c": 3}, "f": 1, "h": {"i": []}, "g": lab_config.REMOVED}      # 1.0 -> 1 is a change
    assert config_store.same_json(lab_config.merge(base, lab), data)
    assert lab_config.diff(base, base) == {}
    assert lab_config.changed_keys(lab) == ["b.c", "f", "h.i", "g"]
    with pytest.raises(lab_config.ConfigError):
        lab_config.diff([1], {"a": 1})


@pytest.mark.parametrize("name", config_store.config_files())
def test_every_shipped_file_round_trips_through_lab_settings(name, tmp_path):
    """Whatever is changed in a shipped config file, the lab file gives exactly that back."""
    base = lab_config.read_path(os.path.join(paths.CONFIG_DIR, name))
    if not isinstance(base, dict):
        pytest.skip("not an object")

    def tweak(value):
        if isinstance(value, dict):
            return {k: tweak(v) for k, v in list(value.items())[:-1]}         # the last key removed at each level
        if isinstance(value, bool):
            return not value
        if isinstance(value, (int, float)):
            return value * 2 + 1
        if isinstance(value, str):
            return value + "!"
        return value
    data = tweak(base)
    (tmp_path / name).write_text(json.dumps(base))
    lab_config.write_lab(name, data, str(tmp_path))
    assert config_store.same_json(lab_config.read(name, str(tmp_path)), data)
    assert (tmp_path / name).read_text() == json.dumps(base)                   # the repository's: untouched


# --------------------------------------------------------------------------- reading
@pytest.fixture
def lab(tmp_path, monkeypatch):
    """This machine's lab folder (SINDIE_LAB_DIR) in a temporary directory."""
    folder = tmp_path / "lab"
    folder.mkdir()
    monkeypatch.setenv("SINDIE_LAB_DIR", str(folder))
    return folder


def test_read_puts_the_lab_settings_on_top(lab):
    base = lab_config.read("smu_config.json")
    assert lab_config.lab_values("smu_config.json") == {}
    (lab / "smu_config.json").write_text(json.dumps({"max_v": 150}))
    assert lab_config.read("smu_config.json") == dict(base, max_v=150)
    path = paths.config_path("smu_config.json")
    assert lab_config.load(path) == dict(base, max_v=150)                       # by path, in the config folder
    assert lab_config.has_lab_layer(path) and lab_config.has_lab_layer("smu_config.json")
    assert lab_config.mtime(path) == max(os.path.getmtime(path), os.path.getmtime(lab / "smu_config.json"))


def test_a_file_elsewhere_is_read_as_it_is(lab, tmp_path):
    (lab / "x.json").write_text(json.dumps({"a": 2}))
    own = tmp_path / "x.json"
    own.write_text(json.dumps({"a": 1}))
    assert lab_config.load(str(own)) == {"a": 1} and not lab_config.has_lab_layer(str(own))


def test_a_lab_only_file_and_missing_files(lab):
    with pytest.raises(FileNotFoundError):
        lab_config.read("nothing_here.json")
    (lab / "only_here.json").write_text(json.dumps({"a": 1}))
    assert lab_config.read("only_here.json") == {"a": 1}
    assert lab_config.mtime("nothing_else.json") == 0.0


def test_a_lab_file_is_ascii_and_only_holds_differences(lab):
    base = lab_config.read("smu_config.json")
    written = lab_config.write_lab("smu_config.json", dict(base, note="50 µA"))
    assert written == {"note": "50 µA"}
    raw = (lab / "smu_config.json").read_bytes()
    raw.decode("ascii")
    assert json.loads(raw) == {"note": "50 µA"} and lab_config.read("smu_config.json")["note"] == "50 µA"


# --------------------------------------------------------------------------- everything reads them
def test_the_services_and_the_hmi_read_the_lab_settings(lab):
    """Spot checks of the readers: each sees a value set only in the lab settings."""
    from src.alarms import rules
    from src.core import fault_registry, interlocks, tolerance
    from src.core.device_service import DeviceService
    from src.core.export_data import logger_tick_seconds
    from src.core.recipe_ai.settings import Settings
    from src.core.recipe_catalog import Catalog
    from src.utils import build_registry

    (lab / "smu_config.json").write_text(json.dumps({"max_v": 123}))
    (lab / "alarm_config.json").write_text(json.dumps({"shelve_max_minutes": 77}))
    faults = lab_config.read("fault_config.json")
    word = next(iter(faults))
    bit = next(iter(faults[word]["bits"]))
    (lab / "fault_config.json").write_text(json.dumps({word: {"bits": {bit: {"description": "lab text"}}}}))
    (lab / "readback_tolerance.json").write_text(json.dumps({"rules": []}))
    (lab / "vac_gauges_config.json").write_text(json.dumps({"system_interlocks": {"safety_loop_relays": ["10.4"]}}))
    (lab / "logger_config.json").write_text(json.dumps({"tick_rate_seconds": 0.1}))
    (lab / "recipe_ai.json").write_text(json.dumps({"enabled": False}))

    class Fake:
        class events:
            @staticmethod
            def log_general(*a, **k):
                raise AssertionError(a)
    assert DeviceService.load_config(Fake(), "smu_config.json", {})["max_v"] == 123
    assert rules.load_config()["shelve_max_minutes"] == 77
    assert rules.load_fault_config()[word]["bits"][bit]["description"] == "lab text"
    assert fault_registry.FaultRegistry(paths.config_path("fault_config.json")).fault_structures[word]["bits"][bit][
        "description"] == "lab text"
    assert tolerance.load_rules(paths.config_path("readback_tolerance.json")) == []
    assert interlocks.LoopConfig.load().relays == [(10, 4)]
    assert logger_tick_seconds(paths.PROJECT_ROOT) == 0.1
    assert Settings.load().enabled is False
    assert Catalog.load().faults and any(f.description == "lab text" for f in Catalog.load().faults)
    smu = build_registry.get_smu_tags(paths.CONFIG_DIR)
    assert any(meta.get("max_val") == 123 for meta in smu.values())


EDITABLE = sorted(set(config_store.config_files()) - {"system_tags.json"})
# Where code names an editable config file's path. Each one must go through lab_config (read,
# load, mtime) - a plain open() would miss this machine's lab settings. A new one fails here
# until it has been checked and added.
PATH_PATTERNS = (re.compile(r"config_(?:path|file)\(\s*[\"'](?P<name>[\w.]+\.json)[\"']"),
                 re.compile(r"[\"']config[\"']\s*,\s*[\"'](?P<name>[\w.]+\.json)[\"']"),
                 re.compile(r"[\"'][^\"'\n]*config/(?P<name>[\w.]+\.json)[\"']"),
                 re.compile(r"join\(\s*config_dir\s*,\s*[\"'](?P<name>[\w.]+\.json)[\"']"))
CHECKED = {
    ("src/alarms/rules.py", "alarm_config.json"), ("src/alarms/rules.py", "fault_config.json"),
    ("src/services/service_monitor.py", "readback_tolerance.json"),
    ("src/services/service_plc.py", "fault_config.json"),
    ("src/services/service_logger.py", "logger_config.json"),
    ("src/core/export_data.py", "logger_config.json"),
    ("src/core/event_helper.py", "fault_config.json"),
    ("src/core/interlocks.py", "vac_gauges_config.json"),
    ("src/core/recipe_catalog.py", "fault_config.json"),
    ("src/core/network_map.py", "network_config.json"),
    ("src/gui/control/control_main.py", "fault_config.json"),
    ("src/utils/build_registry.py", "vac_gauges_config.json"), ("src/utils/build_registry.py", "mpd_config.json"),
    ("src/utils/build_registry.py", "magnet_config.json"), ("src/utils/build_registry.py", "steerer_magnet_config.json"),
    ("src/utils/build_registry.py", "smu_config.json"), ("src/utils/build_registry.py", "plc_config.json"),
    # (paths as git names them, not opened: the updater's and the help pictures' canned update)
    ("src/launcher/updater.py", "fault_config.json"), ("src/launcher/updater.py", "network_config.json"),
    ("src/utils/make_help_screenshots.py", "smu_config.json"),
}


def test_no_config_file_is_opened_past_its_lab_settings():
    found = set()
    for folder, _, files in os.walk(os.path.join(ROOT, "src")):
        for f in files:
            if not f.endswith(".py"):
                continue
            path = os.path.join(folder, f)
            rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
            if rel in ("src/core/lab_config.py", "src/core/config_store.py", "src/core/paths.py"):
                continue
            text = open(path, encoding="utf-8").read()
            for pattern in PATH_PATTERNS:
                for m in pattern.finditer(text):
                    if m["name"] in EDITABLE:
                        found.add((rel, m["name"]))
    assert found - CHECKED == set(), "read these through src/core/lab_config.py, then add them to CHECKED"
    assert CHECKED - found == set(), "no longer named there: take them out of CHECKED"


def test_the_lab_folder_is_not_in_git():
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    import subprocess
    r = subprocess.run(["git", "-C", ROOT, "check-ignore", "-q", "config/lab/x.json"], capture_output=True)
    assert r.returncode == 0
