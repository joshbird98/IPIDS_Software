"""Where recipes live: config/recipes/ - in git, next to the other configuration files.

A SUB_RECIPE step is looked up in the folder of the recipe that calls it, so all recipes
share this one folder.

Until Oct 2026 the HMI's recipe panel and builder used src/config/recipes/ (a relative-path
slip; that folder is not in git). migrate_legacy_recipes() copies anything saved there
across once, without overwriting.
"""
import filecmp
import logging
import os
import shutil

from src.core import paths

RECIPES_DIR = paths.config_path("recipes")
LEGACY_RECIPES_DIR = os.path.join(paths.PROJECT_ROOT, "src", "config", "recipes")

log = logging.getLogger("recipes")


def _free_name(recipes_dir: str, stem: str, src: str):
    """'<stem> (from src-config).json', then '... 2', ... - or None if an identical copy is already there."""
    n = 1
    while True:
        label = "from src-config" if n == 1 else f"from src-config {n}"
        dst = os.path.join(recipes_dir, f"{stem} ({label}).json")
        if not os.path.exists(dst):
            return dst
        if filecmp.cmp(src, dst, shallow=False):
            return None
        n += 1


def migrate_legacy_recipes(legacy_dir: str = LEGACY_RECIPES_DIR, recipes_dir: str = RECIPES_DIR) -> list:
    """Copy recipes from the old folder into recipes_dir. Never overwrites: a recipe whose name is
    taken by a different file is copied as '<name> (from src-config).json'. Returns [(src, dst)]."""
    if not os.path.isdir(legacy_dir):
        return []
    os.makedirs(recipes_dir, exist_ok=True)
    copied = []
    for name in sorted(os.listdir(legacy_dir)):
        src = os.path.join(legacy_dir, name)
        if not name.lower().endswith(".json") or not os.path.isfile(src):
            continue
        dst = os.path.join(recipes_dir, name)
        if os.path.exists(dst):
            if filecmp.cmp(src, dst, shallow=False):
                continue
            dst = _free_name(recipes_dir, os.path.splitext(name)[0], src)
            if dst is None:
                continue
        shutil.copy2(src, dst)
        copied.append((src, dst))
        log.warning("Recipe %s copied from the old folder %s to %s - check it, then delete the old folder",
                    name, legacy_dir, dst)
    return copied
