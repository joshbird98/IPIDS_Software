"""Viewer workspace profiles live in config/ (they used to go to src/gui/config/, which only
existed by accident); old ones are copied across once, never overwriting."""
import json

from src.core import paths
from src.gui.data_viewer import viewer_profiles as vp


def test_profiles_live_in_config():
    assert vp.PROFILES_FILE == paths.config_path("workspace_profiles.json")


def test_migration_copies_missing_profiles_only(tmp_path):
    legacy, target = tmp_path / "old.json", tmp_path / "config" / "workspace_profiles.json"
    legacy.write_text(json.dumps({"Beam": {"a": 1}, "Vacuum": {"b": 2}}))
    target.parent.mkdir()
    target.write_text(json.dumps({"Vacuum": {"b": 99}}))
    assert vp.migrate_legacy_profiles(str(legacy), str(target)) == ["Beam"]
    assert json.loads(target.read_text()) == {"Vacuum": {"b": 99}, "Beam": {"a": 1}}
    assert vp.migrate_legacy_profiles(str(legacy), str(target)) == []          # once
    assert vp.migrate_legacy_profiles(str(tmp_path / "none.json"), str(target)) == []


def test_unreadable_files_are_empty_not_fatal(tmp_path):
    bad = tmp_path / "p.json"
    bad.write_text("{not json")
    assert vp.read_profiles(str(bad)) == {}
    bad.write_text("[1, 2]")
    assert vp.read_profiles(str(bad)) == {}
    vp.write_profiles(str(tmp_path / "sub" / "p.json"), {"x": {}})
    assert vp.read_profiles(str(tmp_path / "sub" / "p.json")) == {"x": {}}
