"""Software arc detection for the Spellman HV supplies (used by service_spellman_mpd).

During an arc the supply runs into its current limit and the output voltage collapses to a
small fraction of the setpoint; that collapse is what is detected (see ArcDetector)."""
import logging
from typing import Optional

log = logging.getLogger("service_spellman_mpd.arc")

# Software arc detection; override in config/mpd_config.json under "arc_detection"
# (top level for every supply, or inside a device entry for that supply only).
ARC_DEFAULTS = {
    "collapse_fraction": 0.10,  # output below this fraction of the setpoint = collapsed = arc
    "min_setpoint_kv": 0.5,     # not judged below this setpoint (a low reading is normal there)
    "settle_s": 3.0,            # longest the output may take to come up after an enable / setpoint change
    "overcurrent_ua": 500.0,    # a current above this counts as an arc too
    "confirm_polls": 2,         # polls in a row that must show it (one garbled reading can't trip the PLC)
}


class ArcDetector:
    """Software arc detection for one supply.

    During an arc the supply runs into its current limit and the output voltage
    collapses to a small fraction of the setpoint; that collapse is what is detected.
    The result latches stat_arc_exceeded, which the PLC service turns into a PLC fault.

    - Output settled at its setpoint: collapsed = readback below collapse_fraction x setpoint
      (or current above overcurrent_ua).
    - Output moving after a setpoint change (until it reaches half the new setpoint, at most
      settle_s after it started moving): a normal transition stays between the old and the new
      setpoint, so only a collapse below collapse_fraction x the LOWER of the two counts. So a
      large step up can't trip it while the output ramps, and an arc during setpoint steps
      (the optimiser) is still caught straight away.
    - Output coming up after an enable (or from a setpoint below min_setpoint_kv): there is
      nothing to compare with until it is up, or settle_s has passed - so a supply that never
      comes up (a short at switch-on) is caught after settle_s.
    - Nothing is judged below min_setpoint_kv, and a collapse must be seen on confirm_polls
      polls in a row.
    """

    def __init__(self, settings: Optional[dict] = None):
        self.s = {**ARC_DEFAULTS, **(settings or {})}
        self.restart()

    def restart(self):
        """The output was switched on or off: wait for it to come up again before judging."""
        self._sp = None        # setpoint being watched (kV)
        self._floor = 0.0      # while moving: the lower of the old and new setpoints
        self._since = 0.0      # when the output started moving
        self._settled = False
        self._hits = 0         # consecutive polls showing an arc

    def update(self, enabled: bool, sp_kv: Optional[float], rb_kv: Optional[float],
               rb_ua: Optional[float], now: float) -> Optional[str]:
        """Feed one poll of the supply; returns why it is an arc, else None."""
        if not enabled:
            self.restart()
            return None
        if sp_kv is None or rb_kv is None:
            return None
        sp, rb = abs(sp_kv), abs(rb_kv)
        if self._sp is None:                         # just switched on: the output starts from 0
            self._sp, self._floor, self._since, self._settled = sp, 0.0, now, False
        elif abs(sp - self._sp) > 1e-6:              # setpoint changed
            if self._settled:
                self._floor, self._since = min(self._sp, sp), now
            else:                                    # changed again while still moving: the clock
                self._floor = min(self._floor, sp)   # keeps running, so the wait is capped at settle_s
            self._sp, self._settled = sp, False
        if not self._settled and (rb >= 0.5 * sp or now - self._since >= self.s["settle_s"]):
            self._settled = True

        ref = sp if self._settled else self._floor
        why = None
        if ref >= self.s["min_setpoint_kv"]:
            if rb < self.s["collapse_fraction"] * ref:
                why = (f"output collapsed to {rb:.3f} kV, below {self.s['collapse_fraction']:.0%} "
                       f"of the {ref:.3f} kV setpoint")
            elif self._settled and rb_ua is not None and rb_ua > self.s["overcurrent_ua"]:
                why = f"output current {rb_ua:.1f} uA above {self.s['overcurrent_ua']:g} uA"
        if why is None:
            self._hits = 0
            return None
        self._hits += 1
        return why if self._hits >= max(1, self.s["confirm_polls"]) else None


def arc_settings(config: dict, dev_info: dict) -> dict:
    """ARC_DEFAULTS <- mpd_config 'arc_detection' <- the device's own 'arc_detection'."""
    merged = dict(ARC_DEFAULTS)
    for source in (config.get("arc_detection") or {}, dev_info.get("arc_detection") or {}):
        for key, value in source.items():
            if key.startswith("_"):
                continue
            if key not in ARC_DEFAULTS:
                log.warning("Unknown arc_detection setting %r in mpd_config.json (ignored)", key)
            elif isinstance(value, bool) or not isinstance(value, (int, float)):
                log.warning("arc_detection %s=%r in mpd_config.json is not a number (using %g)",
                            key, value, merged[key])
            else:
                merged[key] = float(value)
    return merged
