import json
import logging
import os

from src.core import paths
from src.core.paths import REGISTRY_PATH as REGISTRY_FILE

# Saved plot settings live in config/. (The path used to resolve to src/config/, a folder
# that doesn't normally exist; settings saved there are still read if config/ has none.)
CONFIG_FILE = paths.config_path("plot_settings.json")
_OLD_CONFIG_FILE = os.path.join(paths.PROJECT_ROOT, "src", "config", "plot_settings.json")

log = logging.getLogger("viewer.config")

def load_registry():
    """Loads the read-only master hardware registry."""
    if not os.path.exists(REGISTRY_FILE):
        log.warning("%s not found - the viewer shows raw tag names and no limits", REGISTRY_FILE)
        return {}
    try:
        with open(REGISTRY_FILE, 'r') as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        log.error("Registry %s not readable: %s", REGISTRY_FILE, e)
        return {}

def load_config():
    path = CONFIG_FILE if os.path.exists(CONFIG_FILE) else _OLD_CONFIG_FILE
    if not os.path.exists(path): return {}
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        log.error("Plot settings %s not readable (defaults used): %s", path, e)
        return {}

def save_config(config_dict):
    tmp = CONFIG_FILE + ".tmp"
    try:
        with open(tmp, 'w') as f:
            json.dump(config_dict, f, indent=4)
        os.replace(tmp, CONFIG_FILE)
        log.info("Plot settings saved to %s", CONFIG_FILE)
    except (OSError, TypeError, ValueError) as e:
        log.error("Plot settings not saved: %s", e)

def delete_config():
    deleted = False
    for path in (CONFIG_FILE, _OLD_CONFIG_FILE):
        if os.path.exists(path):
            try:
                os.remove(path)
                log.info("Plot settings %s deleted", path)
                deleted = True
            except OSError as e:
                log.error("Could not delete %s: %s", path, e)
                return False
    return deleted