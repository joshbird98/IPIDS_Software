import json
import os

from src.core import paths
from src.core.paths import REGISTRY_PATH as REGISTRY_FILE

# Saved plot settings live in config/. (The path used to resolve to src/config/, a folder
# that doesn't normally exist; settings saved there are still read if config/ has none.)
CONFIG_FILE = paths.config_path("plot_settings.json")
_OLD_CONFIG_FILE = os.path.join(paths.PROJECT_ROOT, "src", "config", "plot_settings.json")

def load_registry():
    """Loads the read-only master hardware registry."""
    if not os.path.exists(REGISTRY_FILE):
        print(f"Warning: {REGISTRY_FILE} not found. UI will use fallback defaults.")
        return {}
    try:
        with open(REGISTRY_FILE, 'r') as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        print(f"Error loading registry: {e}")
        return {}

def load_config():
    path = CONFIG_FILE if os.path.exists(CONFIG_FILE) else _OLD_CONFIG_FILE
    if not os.path.exists(path): return {}
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        print(f"Error loading config: {e}")
        return {}

def save_config(config_dict):
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config_dict, f, indent=4)
        print(f"Plot settings saved to: {CONFIG_FILE}")
    except (OSError, TypeError, ValueError) as e:
        print(f"Error saving config: {e}")

def delete_config():
    deleted = False
    for path in (CONFIG_FILE, _OLD_CONFIG_FILE):
        if os.path.exists(path):
            try:
                os.remove(path)
                print(f"Config file deleted: {path}")
                deleted = True
            except OSError as e:
                print(f"Could not delete {path}: {e}")
                return False
    return deleted