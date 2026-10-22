"""Hard caps: the highest values ever allowed on SINDIE, whatever the config files say.

Set by Josh (October 2026). Nothing gets past them:
  * the config editor refuses a file that goes over one, marks the field and says why
    (src/core/config_checks.py);
  * the tag registry's setpoint limits - what the HMI, recipes and sequencer may send - are kept
    within them (src/utils/build_registry.py);
  * the services check them again at start-up and on every command, so a file or registry
    edited by hand can't get past them either.
Raising one is a deliberate code change here (reviewed, and in git), not an edit in the HMI.
"""
import math
from dataclasses import dataclass
from typing import Optional

from src.core.spellman_locations import LOCATIONS as SPELLMAN_LOCATIONS

GATE_VALVE_GAUGES = ((10, 1), (10, 2))      # (controller node, channel) of VG1 and VG2
GATE_VALVE_RELAY_MBAR = 1e-5    # VG1 / VG2 relay on and off points: they decide when the gate valve may open
OTHER_RELAY_MBAR = 1.0          # the other gauges' relays (not as safety critical)
# A gauge with a software trip (src/core/software_trips.py: rides through short pressure spikes) is
# judged in software at the caps above, for at most RIDE_THROUGH_MAX_S (approved by Josh, 22 October
# 2026); its relays are then only the hardware backstop, and may be set up to BACKSTOP_RELAY_MBAR so
# that a spike doesn't open them
BACKSTOP_RELAY_MBAR = 20.0
RIDE_THROUGH_MAX_S = 2.5
SPELLMAN_MAX_V = 22000.0        # every Spellman MPD supply (source einzel and the others)
MAGNET_PSUS = {                 # config file: (max_current A, max_voltage V)
    "magnet_config.json": (95.0, 25.0),
    "steerer_magnet_config.json": (80.0, 40.0),
}
MAGNET_BASES = {"ion_beam.beamline.magnet": "magnet_config.json",
                "ion_beam.beamline.steerer_magnet": "steerer_magnet_config.json"}
SMU_MAX_ABS_V = 200.0           # Faraday-cup SMU bias, either polarity
SMU_BASE = "ion_beam.beamline.faraday.smu"

WHERE = "src/core/hard_caps.py"


@dataclass(frozen=True)
class Cap:
    low: Optional[float]        # None: no lower cap
    high: Optional[float]       # None: no upper cap
    unit: str
    why: str

    def breach(self, value) -> Optional[str]:
        """'above' / 'below' if the number `value` is past the cap - NaN and infinities count as
        past it - else None (also for anything that isn't a number: see is_number)."""
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        if math.isnan(value):
            return "above" if self.high is not None else "below"
        if self.high is not None and value > self.high:
            return "above"
        if self.low is not None and value < self.low:
            return "below"
        return None

    def limit(self, side: str) -> float:
        return self.high if side == "above" else self.low

    def explain(self, what: str, value) -> str:
        """Why `value` is refused, for the operator."""
        side = self.breach(value)
        limit = self.limit(side)
        shown = f"{value:g}"
        if shown == f"{limit:g}":           # just past it: 22.0000001 mustn't read as "22 ... above the 22 cap"
            shown = repr(float(value))
        return (f"Hard cap: {what} {shown} {self.unit} is {side} the {limit:g} {self.unit} cap - "
                f"{self.why}. Fixed in {WHERE}: raising it is a code change.")

    def note(self) -> str:
        """For a setpoint box's tooltip."""
        bounds = (f"{self.low:g} .. {self.high:g}" if self.low is not None and self.high is not None
                  else f"at most {self.high:g}" if self.high is not None else f"at least {self.low:g}")
        return f"Hard cap: {bounds} {self.unit} - {self.why} ({WHERE})."


@dataclass(frozen=True)
class Violation:
    path: tuple         # where in the config file (as the editor's tree has it)
    message: str


def is_number(v) -> bool:
    """A finite int or float (not a bool, not text) - what the services can use as a limit."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def relay_cap(node, channel, backstop: bool = False) -> Cap:
    """The cap for a gauge controller relay's on / off points (a relay on an unknown channel gets
    the strictest one). backstop: the gauge has a valid software trip (valid_trips), so its relays
    are only the hardware backstop."""
    try:
        gauge = (int(node), int(channel))
    except (TypeError, ValueError, OverflowError):
        gauge, backstop = GATE_VALVE_GAUGES[0], False
    if backstop:
        return Cap(None, BACKSTOP_RELAY_MBAR, "mbar", "the cap for a gauge's relays when a software trip judges "
                                                      "the gauge (they are then only its hardware backstop)")
    if gauge in GATE_VALVE_GAUGES:
        return Cap(None, GATE_VALVE_RELAY_MBAR, "mbar", "VG1's and VG2's relays decide when the gate valve may open")
    return Cap(None, OTHER_RELAY_MBAR, "mbar", "the cap for the other gauges' relays")


def trip_caps(node, channel) -> tuple:
    """(pressure Cap, ride-through Cap) for a gauge's software trip: its trip and reset points are
    held to the cap its relays would have without it."""
    pressure = relay_cap(node, channel)
    return (Cap(None, pressure.high, "mbar", "a software trip is held to the cap of the gauge's relays without one"),
            Cap(None, RIDE_THROUGH_MAX_S, "s", "the longest ride-through approved (Josh, October 2026)"))


def valid_trips(cfg) -> dict:
    """{(node, channel): SoftwareTrip} from a vac_gauges_config.json's contents: the software trips
    that are well formed and within their caps (only these let a gauge's relays be a backstop)."""
    from src.core.software_trips import parse
    trips, _ = parse(cfg)
    out = {}
    for key, t in trips.items():
        pressure, ride = trip_caps(t.node, t.channel)
        if not (pressure.breach(t.trip_mbar) or pressure.breach(t.reset_mbar) or ride.breach(t.ride_through_s)):
            out[key] = t
    return out


SPELLMAN_CAP = Cap(-SPELLMAN_MAX_V, SPELLMAN_MAX_V, "V", "the cap for every Spellman supply")
SPELLMAN_CAP_KV = Cap(-SPELLMAN_MAX_V / 1000.0, SPELLMAN_MAX_V / 1000.0, "kV", "the cap for every Spellman supply")
SMU_CAP = Cap(-SMU_MAX_ABS_V, SMU_MAX_ABS_V, "V", "the SMU bias cap")


def magnet_caps(config_file: str) -> tuple:
    """(current Cap, voltage Cap) for a magnet PSU config file."""
    max_a, max_v = MAGNET_PSUS[config_file]
    name = "magnet" if config_file == "magnet_config.json" else "steerer"
    return (Cap(None, max_a, "A", f"the {name} PSU's current cap"), Cap(None, max_v, "V", f"the {name} PSU's voltage cap"))


def violations(name: str, data) -> list:
    """The values in config file `name` past a hard cap, as [Violation]."""
    out = []

    def check(path, value, cap, what):
        if cap.breach(value):
            out.append(Violation(path, cap.explain(what, value)))

    if not isinstance(data, dict):
        return out
    if name == "vac_gauges_config.json":
        from src.core.software_trips import parse
        backstops = valid_trips(data)
        for key, t in parse(data)[0].items():
            pressure, ride = trip_caps(t.node, t.channel)
            where = ("system_interlocks", "software_trips", t.gauge)
            check(where + ("trip_mbar",), t.trip_mbar, pressure, f"{t.gauge}'s software trip point")
            check(where + ("reset_mbar",), t.reset_mbar, pressure, f"{t.gauge}'s software reset point")
            check(where + ("ride_through_s",), t.ride_through_s, ride, f"{t.gauge}'s ride-through")
        for node, node_data in data.items():
            relays = node_data.get("relays") if isinstance(node_data, dict) and node.isdigit() else None
            for relay, params in (relays.items() if isinstance(relays, dict) else ()):
                if not isinstance(params, dict):
                    continue
                try:
                    backstop = (int(node), int(params.get("channel"))) in backstops
                except (TypeError, ValueError, OverflowError):
                    backstop = False
                cap = relay_cap(node, params.get("channel"), backstop)
                for key in ("on_val", "off_val"):
                    check((node, "relays", relay, key), params.get(key), cap,
                          f"node {node} relay {relay} {key.split('_')[0]} point")
    elif name == "mpd_config.json":
        buses = data.get("buses")
        for i, bus in enumerate(buses if isinstance(buses, list) else ()):
            devices = bus.get("devices") if isinstance(bus, dict) else None
            for dev, cfg in (devices.items() if isinstance(devices, dict) else ()):
                if isinstance(cfg, dict):
                    for key in ("min_value", "max_value"):
                        check(("buses", i, "devices", dev, key), cfg.get(key), SPELLMAN_CAP, f"{dev} {key}")
    elif name in MAGNET_PSUS:
        current, voltage = magnet_caps(name)
        check(("max_current",), data.get("max_current"), current, "max_current")
        check(("max_voltage",), data.get("max_voltage"), voltage, "max_voltage")
        steps = data.get("degauss_steps_amps")
        for i, step in enumerate(steps if isinstance(steps, list) else ()):
            if isinstance(step, (int, float)) and not isinstance(step, bool):
                check(("degauss_steps_amps", i), abs(step), current, f"degauss step {i + 1}")
    elif name == "smu_config.json":
        check(("min_v",), data.get("min_v"), SMU_CAP, "min_v")
        check(("max_v",), data.get("max_v"), SMU_CAP, "max_v")
    return out


def command_cap(tag: str) -> Optional[Cap]:
    """The cap on a setpoint command for `tag` (registry units), if one applies."""
    base, _, leaf = tag.rpartition(".")
    if leaf == "sp_requested_voltage" and base in SPELLMAN_LOCATIONS.values():
        return SPELLMAN_CAP_KV
    if leaf == "sp_requested_current" and base in MAGNET_BASES:
        return magnet_caps(MAGNET_BASES[base])[0]
    if leaf == "sp_requested_voltage" and base == SMU_BASE:
        return SMU_CAP
    return None


def note(tag: str) -> str:
    """Tooltip text for a setpoint box whose command has a hard cap ('' if none)."""
    cap = command_cap(tag)
    return cap.note() if cap is not None else ""


def clamp(value, cap: Cap):
    """`value` brought within `cap` - for the registry's limits. Anything that isn't a finite number
    gives None (no limit: the services then refuse every setpoint for that tag)."""
    if not is_number(value):
        return None
    if cap.high is not None:
        value = min(value, cap.high)
    if cap.low is not None:
        value = max(value, cap.low)
    return value
