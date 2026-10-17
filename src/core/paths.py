"""Single place that decides where data, logs and config live.

Normally everything is under the project folder (data/, SystemLogs/, config/),
exactly as before. Two environment variables change that, mainly so the
simulator and the automated tests never touch real machine data:

    IPIDS_SIMULATE=1     fake-device mode: data goes to data_sim/ instead of data/
    IPIDS_DATA_DIR=...   explicit data folder (overrides both of the above)
    IPIDS_LOG_DIR=...    explicit folder for the per-process log files

The values are read once at import, so set the variables before starting a
process (the service manager passes its own environment to every service).
"""
import os

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CONFIG_DIR = os.path.join(PROJECT_ROOT, "config")


def simulation_enabled() -> bool:
    return os.environ.get("IPIDS_SIMULATE", "").strip().lower() in {"1", "true", "yes", "on"}


def _data_dir() -> str:
    override = os.environ.get("IPIDS_DATA_DIR", "").strip()
    if override:
        return os.path.abspath(override)
    return os.path.join(PROJECT_ROOT, "data_sim" if simulation_enabled() else "data")


DATA_DIR = _data_dir()
PARQUET_DIR = os.path.join(DATA_DIR, "parquet_logs")   # 15-min chunks written by the logger
DAILY_DIR = os.path.join(DATA_DIR, "daily_logs")        # compacted day files + rollups
EVENTS_DB = os.path.join(DATA_DIR, "events.db")
EXPORTS_DIR = os.path.join(DATA_DIR, "exports")

_log_override = os.environ.get("IPIDS_LOG_DIR", "").strip()
if _log_override:
    LOG_DIR = os.path.abspath(_log_override)
elif simulation_enabled() or os.environ.get("IPIDS_DATA_DIR", "").strip():
    LOG_DIR = os.path.join(DATA_DIR, "SystemLogs")
else:
    LOG_DIR = os.path.join(PROJECT_ROOT, "SystemLogs")


def config_path(name: str) -> str:
    return os.path.join(CONFIG_DIR, name)
