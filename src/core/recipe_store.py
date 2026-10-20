"""Saving recipes from the recipe builder: checked, never over another file, backed up, written
whole and committed to git, as the config editor saves configuration (src/core/config_store.py).

    result = save_recipe(data, recipes_dir, filename="ramp.json", expected=text_when_opened)

- The file: a recipe opened from a file is saved to that file, whatever its name is changed to
  (it used to go to <new name>.json, a second copy, with the old file left as it was). A new
  recipe gets a file named after it (letters, digits, '-' and '_'), and never one that exists
  already: a second 'Ramp' is ramp_2.json, not an overwrite of the first one.
- expected: the file's text when it was opened. If the file has changed since (saved from another
  builder, edited by hand, a git pull), nothing is written, rather than undo that change unseen.
- The recipe must have no errors (src/core/recipe_check.py, the same checks the sequencer makes
  before it starts a recipe); warnings are the author's call.
- The file as it was is copied to config/backups/recipes/<name>.<time>.json first (not in git),
  the new one is written to a temporary file and swapped in, then committed to git (best effort).
"""
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

from src.core import config_store, paths
from src.core.recipe_check import check_recipe, errors, summary

log = logging.getLogger("recipe_store")

KEEP_BACKUPS = 50



def renumber(steps: list):
    """Steps numbered 1..n in order, and a PARALLEL step's own steps <its number>01, 02 ... (The
    sequencer refuses a recipe with a number used twice.)"""
    for i, step in enumerate(s for s in steps if isinstance(s, dict)):
        step["step_id"] = i + 1
        if step.get("type") == "PARALLEL" and isinstance(step.get("steps"), list):
            for j, sub in enumerate(s for s in step["steps"] if isinstance(s, dict)):
                sub["step_id"] = (i + 1) * 100 + j + 1


@dataclass
class SaveResult:
    saved: bool = False
    path: str = ""
    backup: Optional[str] = None
    refused: str = ""           # why nothing was written
    committed: bool = False
    git: str = ""               # the commit, or why there is none

    @property
    def filename(self) -> str:
        return os.path.basename(self.path)


def recipe_filename(name: str) -> str:
    """The file a recipe called `name` is saved as: letters, digits, '-' and '_' only, so a name
    like 'Ramp 1/2' can't point outside the recipes folder."""
    stem = re.sub(r"[^a-z0-9_-]+", "_", str(name).strip().lower()).strip("_")
    if len(stem) > 48:                    # a long name makes a short file name: up to a whole word
        stem = stem[:48].rsplit("_", 1)[0] if "_" in stem[:48] else stem[:48]
    return (stem.strip("_-") or "recipe") + ".json"


def free_filename(recipes_dir: str, name: str) -> str:
    """A file name for a new recipe called `name` that no file has yet: ramp.json, ramp_2.json ..."""
    first = recipe_filename(name)
    stem, n = first[:-5], 1
    candidate = first
    while os.path.exists(os.path.join(recipes_dir, candidate)):
        n += 1
        candidate = f"{stem}_{n}.json"
    return candidate


def read_recipe(path: str) -> tuple:
    """(data, text) of a recipe file. Raises OSError, or ValueError if it isn't a JSON object."""
    text, _ = config_store.read_text(path)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("not a recipe (a recipe is a JSON object with a list of steps)")
    return data, text


def backup_dir(recipes_dir: str) -> str:
    """config/backups/recipes, beside the other config backups (git ignores config/backups)."""
    return os.path.join(os.path.dirname(os.path.realpath(recipes_dir)), "backups", "recipes")


def backups(filename: str, recipes_dir: str) -> list:
    """Backups of a recipe file, newest first."""
    folder = backup_dir(recipes_dir)
    stem = filename[:-5] if filename.endswith(".json") else filename
    try:
        found = [f for f in os.listdir(folder) if re.fullmatch(re.escape(stem) + r"\.\d{8}-\d{6}(-\d+)?\.json", f)]
    except OSError:
        return []
    return [os.path.join(folder, f) for f in sorted(found, key=config_store._backup_order, reverse=True)]


def _backup(path: str, recipes_dir: str, clock) -> Optional[str]:
    if not os.path.exists(path):
        return None
    folder = backup_dir(recipes_dir)
    os.makedirs(folder, exist_ok=True)
    name = os.path.basename(path)
    base = f"{name[:-5]}.{time.strftime('%Y%m%d-%H%M%S', time.localtime(clock()))}"
    dst, n = os.path.join(folder, base + ".json"), 1
    while os.path.exists(dst):
        dst, n = os.path.join(folder, f"{base}-{n}.json"), n + 1
    with open(path, "rb") as src, open(dst, "wb") as out:
        out.write(src.read())
    for old in backups(name, recipes_dir)[KEEP_BACKUPS:]:
        try:
            os.remove(old)
        except OSError as e:
            log.warning("Could not prune old recipe backup %s: %s", old, e)
    return dst


def text_of(data: dict, old_text: str = "") -> str:
    """The file's text: only the changed values rewritten if the structure is the same (one changed
    number is one changed line in git), else the whole recipe, indented as before (two spaces for a
    new file). What is written is ASCII: the lab PC opens files in its own code page."""
    if old_text:
        patched = config_store.patched_text(old_text, data)
        if patched is not None:
            return patched
    return config_store._full_text(data, old_text)


def save_recipe(data: dict, recipes_dir: str = None, filename: str = None, expected: str = None,
                reason: str = "", catalog=None, commit: bool = True, run=subprocess.run,
                clock=time.time) -> SaveResult:
    """Save a recipe (see the module doc). filename: its file if it was opened from one (None: a
    new recipe, named after it). expected: that file's text when it was opened."""
    recipes_dir = os.path.realpath(recipes_dir or paths.RECIPES_DIR)
    result = SaveResult()
    if filename is None:
        filename = free_filename(recipes_dir, data.get("recipe_name", ""))
    elif os.path.basename(filename) != filename or not filename.endswith(".json"):
        result.refused = f"{filename!r} isn't a recipe file name"
        return result
    path = os.path.join(recipes_dir, filename)
    result.path = path
    problems = errors(check_recipe(data, catalog, recipes_dir, path))
    if problems:
        result.refused = f"it has errors - {summary(problems)}"
        return result
    old_text, encoding = "", "utf-8"
    if os.path.exists(path):
        try:
            old_text, encoding = config_store.read_text(path)
        except OSError as e:
            result.refused = f"{filename} can't be read: {e}"
            return result
        if expected is None:
            result.refused = f"there is a recipe {filename} already - it would be overwritten"
            return result
        if old_text.replace("\r\n", "\n") != expected.replace("\r\n", "\n"):
            result.refused = (f"{filename} has changed since it was opened here (saved from another recipe builder, "
                              "edited by hand, or a git pull): open it again and make the change again, so that "
                              "change isn't undone")
            return result
    elif expected is not None:
        result.refused = f"{filename} has been deleted or renamed since it was opened here"
        return result
    try:
        raw = text_of(data, old_text).encode(encoding)
        os.makedirs(recipes_dir, exist_ok=True)
        result.backup = _backup(path, recipes_dir, clock)
        config_store._write_atomic(path, raw)
    except (OSError, UnicodeEncodeError, TypeError, ValueError) as e:
        result.refused = f"{filename} could not be written: {e}"
        return result
    result.saved = True
    if commit:
        why = f" - {reason.strip()}" if reason.strip() else ""
        result.committed, result.git = config_store.git_commit(path, f"Recipe builder: {filename}{why}", run=run)
    else:
        result.git = "git commit switched off"
    log.info("Recipe saved: %s (version %s)%s; backup %s; git: %s", path, data.get("version"),
             f" - {reason}" if reason else "", result.backup, result.git)
    return result
