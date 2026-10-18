"""Recipes saved in the old (untracked) src/config/recipes folder are copied into config/recipes
once, never overwriting anything."""
import os

from src.core.recipe_files import RECIPES_DIR, migrate_legacy_recipes
from src.core import paths


def test_recipes_live_in_config():
    assert RECIPES_DIR == paths.RECIPES_DIR             # the tests' copy (SINDIE_RECIPES_DIR)
    assert paths.recipes_dir({}) == paths.config_path("recipes")
    assert paths.recipes_dir({"SINDIE_RECIPES_DIR": "/x/r"}) == os.path.abspath("/x/r")


def test_migration_copies_without_overwriting(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "mine.json").write_text('{"recipe_name": "mine"}')
    (old / "same.json").write_text('{"recipe_name": "same"}')
    (new / "same.json").write_text('{"recipe_name": "same"}')
    (old / "edited.json").write_text('{"recipe_name": "edited", "v": 2}')
    (new / "edited.json").write_text('{"recipe_name": "edited", "v": 1}')
    (old / "notes.txt").write_text("not a recipe")

    copied = migrate_legacy_recipes(str(old), str(new))
    names = sorted(p.name for p in new.iterdir())
    assert names == ["edited (from src-config).json", "edited.json", "mine.json", "same.json"]
    assert (new / "edited.json").read_text() == '{"recipe_name": "edited", "v": 1}'          # untouched
    assert (new / "edited (from src-config).json").read_text() == '{"recipe_name": "edited", "v": 2}'
    assert len(copied) == 2

    assert migrate_legacy_recipes(str(old), str(new)) == []        # running it again changes nothing
    (old / "edited.json").write_text('{"recipe_name": "edited", "v": 3}')
    migrate_legacy_recipes(str(old), str(new))
    assert (new / "edited (from src-config 2).json").exists()


def test_no_old_folder_is_fine(tmp_path):
    assert migrate_legacy_recipes(str(tmp_path / "missing"), str(tmp_path / "new")) == []
