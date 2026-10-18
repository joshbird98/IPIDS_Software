"""Environment variables of the SINDIE software (SINDIE_SIMULATE, SINDIE_PORT_OFFSET, ...).

The machine used to be called IPIDS: the old IPIDS_* names are still read when the
SINDIE_* one isn't set, so existing shortcuts and scripts keep working.
"""
import os

PREFIX, LEGACY_PREFIX = "SINDIE_", "IPIDS_"


def getenv(name: str, default: str = "", environ=None) -> str:
    """name without prefix, e.g. getenv("SIMULATE"). environ: another process's environment."""
    env = os.environ if environ is None else environ
    value = env.get(PREFIX + name)
    if value is None:
        value = env.get(LEGACY_PREFIX + name)
    return default if value is None else value


def flag(name: str, environ=None) -> bool:
    return getenv(name, environ=environ).strip().lower() in {"1", "true", "yes", "on"}
