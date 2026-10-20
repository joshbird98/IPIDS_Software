"""Single place that decides where data, logs and config live.

Normally everything is under the project folder (data/, SystemLogs/, config/),
exactly as before. A few environment variables change that, mainly so the
simulator and the automated tests never touch real machine data:

    SINDIE_SIMULATE=1    fake-device mode: data goes to data_sim/ instead of data/
    SINDIE_DATA_DIR=...  explicit data folder (overrides both of the above)
    SINDIE_LOG_DIR=...   explicit folder for the per-process log files
    SINDIE_REGISTRY=...  tag registry to use instead of config/system_tags.json
                         (the tests build a fresh one from the config files)
    SINDIE_RECIPES_DIR=... recipe folder instead of config/recipes (the tests use a copy)
    SINDIE_SETTINGS_DIR=... saved window layouts in .ini files there, not the user's settings
                         (src/gui/settings.py; the tests use it)
(The machine's old name also works: IPIDS_SIMULATE etc., see src/core/env.py.)

The values are read once at import, so set the variables before starting a
process (the service manager passes its own environment to every service).
"""
import os

from src.core.env import flag, getenv

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CONFIG_DIR = os.path.join(PROJECT_ROOT, "config")
DOCS_DIR = os.path.join(PROJECT_ROOT, "docs")          # the help pages (Markdown), shown by src/gui/help


def simulation_enabled() -> bool:
    return flag("SIMULATE")


def _data_dir() -> str:
    override = getenv("DATA_DIR").strip()
    if override:
        return os.path.abspath(override)
    return os.path.join(PROJECT_ROOT, "data_sim" if simulation_enabled() else "data")


DATA_DIR = _data_dir()
PARQUET_DIR = os.path.join(DATA_DIR, "parquet_logs")   # 15-min chunks written by the logger
DAILY_DIR = os.path.join(DATA_DIR, "daily_logs")        # compacted day files + rollups
EVENTS_DB = os.path.join(DATA_DIR, "events.db")
EXPORTS_DIR = os.path.join(DATA_DIR, "exports")

_log_override = getenv("LOG_DIR").strip()
if _log_override:
    LOG_DIR = os.path.abspath(_log_override)
elif simulation_enabled() or getenv("DATA_DIR").strip():
    LOG_DIR = os.path.join(DATA_DIR, "SystemLogs")
else:
    LOG_DIR = os.path.join(PROJECT_ROOT, "SystemLogs")


_registry_override = getenv("REGISTRY").strip()
# The tag registry built by src/utils/build_registry.py from the config files
REGISTRY_PATH = os.path.abspath(_registry_override) if _registry_override else os.path.join(CONFIG_DIR,
                                                                                             "system_tags.json")


def recipes_dir(environ=None) -> str:
    override = getenv("RECIPES_DIR", environ=environ).strip()
    return os.path.abspath(override) if override else os.path.join(CONFIG_DIR, "recipes")


RECIPES_DIR = recipes_dir()


def config_path(name: str) -> str:
    if name == "system_tags.json":
        return REGISTRY_PATH
    return os.path.join(CONFIG_DIR, name)
