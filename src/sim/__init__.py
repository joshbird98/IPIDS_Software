"""Fake devices for running the whole SINDIE stack on a laptop.

Set SINDIE_SIMULATE=1 (run_simulation.py does this) and every hardware service
swaps its device driver for one of the classes in this package; everything
above the driver - command handling, interlocks, telemetry, logging, the HMI,
the viewer - is the real code. Data goes to data_sim/ instead of data/.

The fakes are deliberately simple first-order models (setpoint -> readback
with a time constant and a little noise). They exist to exercise the software
(and to make GUI screenshots/demos possible away from the lab), not to model
the physics of the implanter.

Faults can be injected while it runs by listing them (comma or newline separated)
in data_sim/sim_faults.txt - run_simulation.py has a menu for it - or for one
process through the environment variable SINDIE_SIM_FAULTS. Known faults:

    plc_disconnect      PLC unreachable            plc_relay_trip      safety relay drops
    spellman_no_reply   supply silent (timeouts)   spellman_refuse     supply rejects settings
    spellman_arc        einzel output collapses into its current limit (arc)
    magnet_disconnect   magnet PSU unreachable     magnet_sag          magnet delivers 10 % low
    steerer_disconnect  steerer PSU unreachable    smu_disconnect      SMU unreachable
    turbo_disconnect    turbo bridge unreachable   turbo_error         turbo run-up error
    turbo_backing_closed  the valve to the turbo's backing pump shut: its current and temperatures climb
    vacuum_disconnect   gauge bridge unreachable   vacuum_vent_source  VG1 rises to atmosphere
    vacuum_spike_source VG1 at ~1 mbar for 0.8 s every 6 s (a powdered target's bursts)
    vacuum_lossy_bus    the gauge bus loses one read in five, and answers nothing for 4 s every minute
    safety_loop_open    a door, ESO or key switch open in the safety relay's hardwired loop
    air_low             compressed air pressure switch open
"""
import math
import os
import random
import time

from src.core import paths
from src.core.env import getenv
from src.core.paths import simulation_enabled  # noqa: F401  (re-exported for the services)

FAULTS_FILE = os.path.join(paths.DATA_DIR, "sim_faults.txt")
FAULT_DESCRIPTIONS = {
    "plc_disconnect": "PLC unreachable",
    "plc_relay_trip": "safety relay trips (latched: fault reset, then enable it again)",
    "spellman_no_reply": "Spellman supply silent (timeouts)",
    "spellman_refuse": "Spellman supply rejects settings",
    "spellman_arc": "einzel output collapses into its current limit (arc)",
    "magnet_disconnect": "analyser magnet PSU unreachable",
    "magnet_sag": "analyser magnet delivers 10 % low",
    "steerer_disconnect": "X-steerer PSU unreachable",
    "smu_disconnect": "SMU unreachable",
    "turbo_disconnect": "turbo pump bridge unreachable",
    "turbo_error": "turbo pump run-up error",
    "turbo_backing_closed": "valve to the turbo's backing pump shut: its load climbs (guard: source shutdown in ~36 min)",
    "vacuum_disconnect": "gauge controller bridge unreachable",
    "vacuum_vent_source": "source vacuum (VG1) rises to atmosphere",
    "vacuum_spike_source": "source vacuum (VG1) spikes to ~1 mbar for 0.8 s every 6 s (ridden through)",
    "vacuum_lossy_bus": "gauge bus loses 1 read in 5, and answers nothing for 4 s every minute (as the lab's)",
    "safety_loop_open": "a door, an ESO or the key switch open (the safety relay won't close)",
    "air_low": "compressed air pressure low (the gate valve closes, the safety relay drops)",
}
KNOWN_FAULTS = tuple(FAULT_DESCRIPTIONS)
_file_cache = {"checked": 0.0, "mtime": None, "faults": frozenset()}


def _file_faults() -> frozenset:
    now = time.monotonic()
    if now - _file_cache["checked"] < 0.5:
        return _file_cache["faults"]
    _file_cache["checked"] = now
    try:
        mtime = os.path.getmtime(FAULTS_FILE)
    except OSError:
        _file_cache["mtime"], _file_cache["faults"] = None, frozenset()
        return _file_cache["faults"]
    if mtime != _file_cache["mtime"]:
        try:
            with open(FAULTS_FILE, "r") as f:
                text = f.read()
        except OSError:
            text = ""
        _file_cache["mtime"] = mtime
        _file_cache["faults"] = frozenset(x.strip() for x in text.replace(",", "\n").splitlines()
                                          if x.strip() and not x.strip().startswith("#"))
    return _file_cache["faults"]


def injected_faults() -> set:
    env = {f.strip() for f in getenv("SIM_FAULTS").split(",") if f.strip()}
    return env | set(_file_faults())


def set_faults(faults) -> None:
    """Replace the fault list read by every simulated service (used by run_simulation.py and tests)."""
    os.makedirs(os.path.dirname(FAULTS_FILE), exist_ok=True)
    tmp = FAULTS_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(sorted(faults)) + "\n")
    os.replace(tmp, FAULTS_FILE)


def first_order(current: float, target: float, dt: float, tau: float) -> float:
    """Move `current` towards `target` like an RC circuit with time constant tau."""
    if tau <= 0:
        return target
    return target + (current - target) * math.exp(-dt / tau)


def noisy(value: float, rel: float = 0.002, abs_: float = 0.0) -> float:
    return value + random.gauss(0.0, abs(value) * rel + abs_)


class Clock:
    """dt between calls, clamped so a debugger pause doesn't make the models jump."""

    def __init__(self):
        self.last = time.monotonic()

    def dt(self, max_dt: float = 1.0) -> float:
        now = time.monotonic()
        dt = min(max_dt, max(0.0, now - self.last))
        self.last = now
        return dt
