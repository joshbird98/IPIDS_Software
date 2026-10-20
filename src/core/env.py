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


# The simulator's ports are the real ones shifted by this much (run_simulation.py), so it can run
# on the lab PC beside the real system.
SIM_PORT_OFFSET = 10000


def use_simulator(environ=None):
    """`--sim` on the HMI or the data viewer: simulated devices (data in data_sim/) AND the
    simulator's ports. Call it before anything from src is imported (they read these at import).
    Without the port offset, a `--sim` HMI on the lab PC would talk to the real services while
    its title said SIMULATION. An offset already set (a test, run_simulation.py) is kept."""
    env = os.environ if environ is None else environ
    env[PREFIX + "SIMULATE"] = "1"
    if not getenv("PORT_OFFSET", environ=env).strip():
        env[PREFIX + "PORT_OFFSET"] = str(SIM_PORT_OFFSET)
