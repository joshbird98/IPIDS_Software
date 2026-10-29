"""The config files as the system uses them: the repository's file, with this machine's own
changes - its lab settings - on top. No Qt, and nothing from SINDIE but the paths, so anything
can use it (network_map does, at import).

    data = lab_config.read("vac_gauges_config.json")     # what the services use
    data = lab_config.load(path)                          # the same for a path (a test's own file: as it is)
    lab_config.lab_values("vac_gauges_config.json")      # only this machine's own changes ({} if none)

config/<name>.json is the repository's file: in git, changed only by software updates.
config/lab/<name>.json, if there is one, holds what was changed on this machine (the HMI's
config editor writes it). It isn't in git, so an update never overwrites it, and it never stands in
an update's way. The two are merged key by key:

  * an object in the lab file is merged into the repository's object, key by key;
  * anything else - a number, a string, true/false, a list - replaces the repository's value;
  * {"__lab__": "removed"} removes the repository's key.

So a change an update makes reaches this machine, unless the machine has its own value for that
key: that is kept (and the updater says so). The lab file only ever holds the values that differ
(diff()), so it stays short and says plainly what this machine does differently.

SINDIE_LAB_DIR moves the lab folder (the tests point it at an empty one). Files are read as UTF-8,
or in the Windows code page if they aren't (a file saved from Notepad as "ANSI").
"""
import copy
import json
import os

from src.core import paths
from src.core.env import getenv

LAB_DIRNAME = "lab"
REMOVED = {"__lab__": "removed"}            # in a lab file: the repository's key is taken out
FALLBACK_ENCODING = "cp1252"                # what Notepad's "ANSI" means on the lab PC


class ConfigError(ValueError):
    """A config file can't be read as JSON (the message says which, and where)."""


# ============================================================================ places
def config_dir() -> str:
    """The repository's config folder (read at each call: the tests move it)."""
    return paths.CONFIG_DIR


def lab_dir(base_dir: str = None) -> str:
    """This machine's lab settings: config/lab, or SINDIE_LAB_DIR (only for the real config folder:
    a test's own config folder has its own lab folder)."""
    if base_dir is None or os.path.abspath(base_dir) == os.path.abspath(config_dir()):
        override = getenv("LAB_DIR").strip()
        if override:
            return os.path.abspath(override)
    return os.path.join(base_dir or config_dir(), LAB_DIRNAME)


def lab_path(name: str, base_dir: str = None) -> str:
    return os.path.join(lab_dir(base_dir), name)


def _split(path_or_name: str) -> tuple:
    """(folder, name) for a config file given by name (in the config folder) or by path."""
    if os.path.dirname(path_or_name):
        path = os.path.abspath(path_or_name)
        return os.path.dirname(path), os.path.basename(path)
    return os.path.abspath(config_dir()), path_or_name


def has_lab_layer(path_or_name: str) -> bool:
    """A file in the real config folder (not a test's copy, nor a lab file itself) has lab settings
    on top of it."""
    folder, _ = _split(path_or_name)
    return folder == os.path.abspath(config_dir())


# ============================================================================ reading
def read_text(path: str) -> tuple:
    """(text, encoding): UTF-8 (a byte-order mark is dropped), else the Windows code page (raises
    OSError)."""
    with open(path, "rb") as f:
        raw = f.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    for encoding in ("utf-8", FALLBACK_ENCODING, "latin-1"):     # latin-1 decodes any bytes
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue


def read_path(path: str, label: str = None):
    """The JSON in `path`, as it is (no lab settings). OSError if it can't be read (FileNotFoundError
    if it isn't there), ConfigError if it isn't JSON."""
    label = label or os.path.basename(path)
    text, _ = read_text(path)
    try:
        return json.loads(text)
    except ValueError as e:
        raise ConfigError(f"{label} is not valid JSON: {e}") from e


def lab_values(name: str, base_dir: str = None):
    """This machine's own changes to a config file ({} if it has none). ConfigError if its lab file
    isn't JSON, or isn't an object."""
    path = lab_path(name, base_dir)
    try:
        data = read_path(path, f"config/{LAB_DIRNAME}/{name}")
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"config/{LAB_DIRNAME}/{name} must hold an object ({{...}}), not {type(data).__name__}")
    return data


def read(name: str, base_dir: str = None):
    """A config file as the system uses it: the repository's, with this machine's lab settings on top.
    FileNotFoundError if neither file is there; ConfigError if one of them isn't JSON."""
    folder = base_dir or config_dir()
    try:
        base = read_path(os.path.join(folder, name), name)
    except FileNotFoundError:
        base = None
    lab = lab_values(name, folder)
    if base is None:
        if not lab:
            raise FileNotFoundError(f"no {name} in {folder}")
        return merge({}, lab)
    return merge(base, lab) if lab else base


def load(path_or_name: str):
    """read() for a config file given by path or name: in the real config folder, with its lab
    settings; anywhere else (a test's file), as it is."""
    folder, name = _split(path_or_name)
    if has_lab_layer(path_or_name):
        return read(name)
    return read_path(os.path.join(folder, name), name)


def mtime(path_or_name: str) -> float:
    """When the file as the system uses it last changed: the newer of the repository's file and its
    lab file (0 if neither is there) - for programs that load a file again when it changes."""
    folder, name = _split(path_or_name)
    times = [os.path.join(folder, name)]
    if has_lab_layer(path_or_name):
        times.append(lab_path(name))
    best = 0.0
    for p in times:
        try:
            best = max(best, os.path.getmtime(p))
        except OSError:
            pass
    return best


# ============================================================================ merging
def same_json(a, b) -> bool:
    """Equal with the same JSON types (1 and 1.0, or 1 and true, are different here)."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(same_json(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(same_json(x, y) for x, y in zip(a, b))
    return a == b


def _is_removed(value) -> bool:
    return isinstance(value, dict) and value.get("__lab__") == "removed" and len(value) == 1


def merge(base, lab):
    """The repository's value with the lab's on top (see the module's doc). Neither is changed."""
    if not isinstance(base, dict) or not isinstance(lab, dict):
        return copy.deepcopy(lab)
    out = copy.deepcopy(base)
    for key, value in lab.items():
        if _is_removed(value):
            out.pop(key, None)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def diff(base, data) -> dict:
    """The lab file that turns `base` (the repository's file) into `data`: only what differs.
    Both must be objects."""
    if not isinstance(base, dict) or not isinstance(data, dict):
        raise ConfigError("a config file must hold an object ({...}) for lab settings")
    out = {}
    for key, value in data.items():
        if key not in base:
            out[key] = copy.deepcopy(value)
        elif isinstance(value, dict) and isinstance(base[key], dict):
            inner = diff(base[key], value)
            if inner:
                out[key] = inner
        elif not same_json(base[key], value) or _is_removed(value):
            out[key] = copy.deepcopy(value)
    for key in base:
        if key not in data:
            out[key] = dict(REMOVED)
    return out


def changed_keys(lab, prefix: str = "") -> list:
    """The dotted names of the values a lab file sets (or removes), for saying what it changes:
    ['system_interlocks.safety_loop_relays', ...]."""
    out = []
    for key, value in (lab or {}).items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict) and value and not _is_removed(value):
            out += changed_keys(value, name)
        else:
            out.append(name)
    return out


# ============================================================================ writing
def lab_text(lab: dict) -> str:
    """A lab file's text: indented, ASCII only (other characters as \\u escapes - the programs on the
    Windows lab PC have opened config files in its code page)."""
    return json.dumps(lab, indent=2, ensure_ascii=True) + "\n"


def write_lab(name: str, data, base_dir: str = None) -> dict:
    """Make `data` this machine's version of a config file: its lab file is written with only what
    differs from the repository's file (an empty object if nothing does). Returns what it holds.
    The file is written whole to a temporary file and swapped in."""
    folder = base_dir or config_dir()
    try:
        base = read_path(os.path.join(folder, name), name)
    except FileNotFoundError:
        base = {}
    lab = diff(base, data)
    path = lab_path(name, folder)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="ascii", newline="\n") as f:
        f.write(lab_text(lab))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return lab
