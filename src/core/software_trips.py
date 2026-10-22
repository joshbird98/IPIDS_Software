"""Software trips with a ride-through: a gauge whose short pressure spikes must not trip the machine.

Some of the source's targets (powdered ones, Josh, October 2026) give VG1 short bursts of pressure
- up to about 1 mbar for a few readings - that are harmless. A gauge controller relay set at the
gate-valve level opens on each one, and the relay in the safety relay's hardwired loop drops the
high voltage. For a gauge listed here instead:

  * its gauge controller relays are only a hardware backstop, set high enough (up to
    hard_caps.BACKSTOP_RELAY_MBAR) that a spike doesn't open them;
  * the vacuum service judges it in software, as its relay was but with an on-delay: an excursion
    starts at a reading above trip_mbar and ends at one at or below reset_mbar (the relay's
    hysteresis). One that lasts ride_through_s (hard_caps: the trip point at most the gate-valve
    cap, the ride-through at most 2.5 s, as approved) reports the gauge above its set point - to
    the PLC, which latches the gauge's fault and takes the gate valve's open permissive away, so
    the gate valve closes and the safety relay opens with it - until the excursion ends;
  * a shorter excursion (a spike) is ridden through: logged as an event, not reported.

In vac_gauges_config.json, system_interlocks.software_trips:

    "software_trips": {"VG1": {"trip_mbar": 8.0e-6, "reset_mbar": 5.5e-6, "ride_through_s": 2.5}}

No Qt, no other SINDIE imports: read by the vacuum service, the config editor's checks, the hard
caps, the registry builder and the HMI.
"""
import math
from dataclasses import dataclass
from typing import Dict, List, Tuple

# The gauges by their names in the HMI: (gauge controller node, channel)
GAUGES = {"VG1": (10, 1), "VG2": (10, 2), "VG3": (10, 3), "VG4": (20, 1), "VG5": (20, 2), "VG6": (20, 3)}
GAUGE_NAMES = {v: k for k, v in GAUGES.items()}


@dataclass(frozen=True)
class SoftwareTrip:
    gauge: str              # "VG1"
    node: int
    channel: int
    trip_mbar: float        # above this: an excursion starts; one that lasts ride_through_s is reported
    reset_mbar: float       # at or below this: the excursion is over (ridden through, or clear again)
    ride_through_s: float

    @property
    def key(self) -> Tuple[int, int]:
        return self.node, self.channel

    def describe(self) -> str:
        return (f"{self.gauge}: above {self.trip_mbar:.1E} mbar for {self.ride_through_s:g} s trips it "
                f"(clear again at {self.reset_mbar:.1E} mbar)")


def _number(v):
    """A finite number as a float, else None (text, true/false, nan, inf - and an integer too big for
    a float, which float() would refuse with an OverflowError)."""
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return None
    try:
        v = float(v)
    except OverflowError:
        return None
    return v if math.isfinite(v) else None


def parse(cfg) -> Tuple[Dict[Tuple[int, int], SoftwareTrip], List[str]]:
    """({(node, channel): SoftwareTrip}, [problems]) from a vac_gauges_config.json's contents. An
    entry with a problem is left out (its gauge is then judged by its relays, at the strict caps)."""
    trips, problems = {}, []
    settings = cfg.get("system_interlocks") if isinstance(cfg, dict) else None
    entries = settings.get("software_trips") if isinstance(settings, dict) else None
    if entries is None:
        return trips, problems
    w = "system_interlocks.software_trips"
    if not isinstance(entries, dict):
        return trips, [f'{w}: must be an object such as {{"VG1": {{"trip_mbar": 8e-06, ...}}}}']
    for gauge, e in entries.items():
        if str(gauge).startswith("_"):
            continue            # (a comment)
        if gauge not in GAUGES:
            problems.append(f"{w}.{gauge}: not a gauge (VG1 to VG6)")
            continue
        if not isinstance(e, dict):
            problems.append(f"{w}.{gauge}: must be an object with trip_mbar, reset_mbar and ride_through_s")
            continue
        trip, reset, ride = (_number(e.get(k)) for k in ("trip_mbar", "reset_mbar", "ride_through_s"))
        bad = [k for k, v in (("trip_mbar", trip), ("reset_mbar", reset), ("ride_through_s", ride))
               if v is None or v <= 0]
        if bad:
            problems.append(f"{w}.{gauge}.{bad[0]}: must be a number above 0")
            continue
        if reset > trip:
            problems.append(f"{w}.{gauge}: reset_mbar ({reset:g}) must be at most trip_mbar ({trip:g})")
            continue
        node, ch = GAUGES[gauge]
        trips[(node, ch)] = SoftwareTrip(gauge, node, ch, float(trip), float(reset), float(ride))
    return trips, problems
