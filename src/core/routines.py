"""Recipe routines: the multi-stage steps a ROUTINE_CALL step runs.

    mass_scan           the Mass Scan panel's scan as a step, then the magnet set on the peak
    safety_relay_init   the safety relay brought on with the operator (vacuum, gate valve, START)
    hv_conditioning     a supply ramped up slowly, holding on pressure and backing off on arcs
    psu_control         a supply switched on and set, its readback checked

Each routine declares its parameters (PARAMS: name, kind, default, unit, limits, help). The
engine checks a recipe's parameters against them when it loads (validate), the recipe checks
(src/core/recipe_check.py) add the registry's tags and limits, and the recipe builder makes its
forms from them. Times are in seconds, values in the unit of the tag they go to.
"""
import logging
import time
from dataclasses import dataclass
from typing import Optional

from src.core.commands import describe, route_for_tag
from src.core.conditions import holds, number
from src.core.telemetry_cache import fresh_value

log = logging.getLogger("routines")

STALE_HOLD_FAIL_S = 30.0   # a routine waiting on a value with no live data fails after this long
RELAY = "ion_beam.facilities.safety_relay_active"
VG1_PRESSURE = "ion_beam.source.vacuum_gauge_1.rb_pressure"
MAGNET_ENABLED = "ion_beam.beamline.magnet.stat_enabled"
CUP_INSERTED = "ion_beam.beamline.diagnostics.stat_fc_inserted"


class RoutineError(ValueError):
    """Parameters a routine can't run with."""


def send(cmd_thread, target, value, timeout=None):
    """Route and send one automation command; returns its ticket (None with an old-style cmd_thread)."""
    if timeout is None:
        return cmd_thread.send_command(route_for_tag(target), target, value, origin="recipe")
    return cmd_thread.send_command(route_for_tag(target), target, value, origin="recipe", timeout=timeout)


def ticket_outcome(ticket):
    """None while waiting; (True, '') or (False, reason) once the service has answered."""
    if ticket is None or not hasattr(ticket, "done"):
        return True, ""
    if not ticket.done():
        return None
    if ticket.ok:
        return True, ""
    return False, describe(ticket.result)


def evaluate_condition(current_val, condition_str, target_val, tolerance=None) -> bool:
    """(Kept for older callers: src/core/conditions.py holds().)"""
    return holds(current_val, condition_str, target_val, tolerance)


@dataclass(frozen=True)
class Param:
    """One parameter of a routine.

    kind: settable_tag (a command or setpoint tag), readable_tag, number, integer, seconds, bool, text
    unit_of: the parameter whose tag gives this one's unit and limits (e.g. psu_control target_val -> sp_tag)."""
    key: str
    label: str
    kind: str
    default: object = None
    required: bool = False
    unit: str = ""
    unit_of: str = ""
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    help: str = ""
    advanced: bool = False          # the builder shows it under "More settings"
    aliases: tuple = ()             # older names the routine also reads

    def read(self, params: dict):
        """The value given for this parameter (or under an older name), or the default."""
        for name in (self.key,) + self.aliases:
            if name in params:
                return params[name]
        return self.default


def _check(cls, params: dict) -> list:
    """Errors in a routine's parameters, from its PARAMS (types, required, fixed ranges)."""
    errors = []
    for p in cls.PARAMS:
        present = any(name in params for name in (p.key,) + p.aliases)
        value = p.read(params)
        if not present:
            if p.required:
                errors.append(f"{p.key} ({p.label}) is needed")
            continue
        if p.kind.endswith("_tag"):
            if value in (None, ""):
                if p.required:
                    errors.append(f"no tag for {p.key} ({p.label})")
            elif not isinstance(value, str):
                errors.append(f"{p.key} must be a tag name, not {value!r}")
            continue
        if p.kind == "text":
            if value is not None and not isinstance(value, str):
                errors.append(f"{p.key} must be text")
            continue
        x = number(value)
        if x is None:
            errors.append(f"{p.key} must be a number, not {value!r}")
            continue
        if p.kind == "bool":
            if x not in (0.0, 1.0):
                errors.append(f"{p.key} must be true or false (1 or 0), not {value!r}")
            continue
        if p.kind == "integer" and x != int(x):
            errors.append(f"{p.key} must be a whole number, not {value!r}")
        if p.minimum is not None and x < p.minimum:
            errors.append(f"{p.key} can't be below {p.minimum:g} (it is {x:g})")
        if p.maximum is not None and x > p.maximum:
            errors.append(f"{p.key} can't be above {p.maximum:g} (it is {x:g})")
    return errors


def known_keys(cls) -> set:
    keys = set()
    for p in cls.PARAMS:
        keys.add(p.key)
        keys.update(p.aliases)
    return keys


class BaseRoutine:
    PARAMS = ()
    TITLE = ""
    ACCEPTS_UNKNOWN = False         # mass_scan passes its settings on, and refuses unknown ones there

    def __init__(self, parameters: dict):
        self.parameters = parameters
        self.state = "INIT"
        self.fail_reason = ""
        self._tickets = []

    @classmethod
    def validate(cls, params: dict) -> list:
        """Errors that stop a recipe from loading ([] if none)."""
        return _check(cls, params)

    def p(self, key):
        for param in self.PARAMS:
            if param.key == key:
                return param.read(self.parameters)
        return self.parameters.get(key)

    def num(self, key) -> float:
        x = number(self.p(key))
        if x is None:
            raise RoutineError(f"{key} must be a number")
        return x

    def tick(self, telemetry_cache: dict, cmd_thread) -> str:
        """Called every recipe tick. Must return RUNNING, DONE, FAILED or WAITING_FOR_USER."""
        raise NotImplementedError()

    def add_pause_offset(self, duration: float):
        """Allows routines with internal timers to adjust for UI pauses."""
        pass

    def _send_cmd(self, cmd_thread, target: str, value: float):
        """Route by the tag registry and remember the ticket so a refusal fails the routine."""
        ticket = send(cmd_thread, target, value)
        if ticket is not None and hasattr(ticket, "done"):
            self._tickets.append(ticket)
        return ticket

    def _command_failure(self):
        """Reason of the first refused/failed command since the last check, else None."""
        for t in list(self._tickets):
            outcome = ticket_outcome(t)
            if outcome is None:
                continue
            self._tickets.remove(t)
            if not outcome[0]:
                return outcome[1]
        return None

    def _fail(self, reason: str) -> str:
        self.fail_reason = reason
        log.warning("%s failed: %s", type(self).__name__, reason)
        return "FAILED"

    @staticmethod
    def _get(telemetry, tag):
        """Live value only - None if the tag's service is silent or the value is unknown."""
        return fresh_value(telemetry, tag) if tag else None


# ============================================================================ mass_scan
class MassScanRoutine(BaseRoutine):
    """The precision mass scan (src/sequencer/mass_scan.py - the same code as the HMI's Mass Scan
    panel) as a recipe step. It needs the analyser magnet on and the Faraday cup in, and fails at
    once if they aren't. With goto_peak (the default) the magnet is then set on the most prominent
    peak of the scan; with export, each scan is saved as a CSV file, as on the panel.

    Parameters: start_a, stop_a, step_a (magnet amps) and optionally tolerance, settle_time,
    samples, sample_interval, nplc, export, export_path, goto_peak. (loop isn't allowed: a recipe
    step has to end.)"""

    TITLE = "Mass scan, then go to the biggest peak"
    ACCEPTS_UNKNOWN = False
    PARAMS = (
        Param("start_a", "Start", "number", 20.0, True, unit="A", minimum=0.0,
              help="The magnet current the scan starts at."),
        Param("stop_a", "Stop", "number", 30.0, True, unit="A", minimum=0.0,
              help="The magnet current it ends at."),
        Param("step_a", "Step", "number", 0.05, True, unit="A", minimum=1e-6,
              help="The change in magnet current from one point to the next."),
        Param("samples", "Samples per point", "integer", 5, unit="", minimum=1, maximum=1000,
              help="Beam current readings averaged at each point."),
        Param("nplc", "SMU integration (NPLC)", "number", 1.0, minimum=0.001, maximum=100.0,
              help="Longer is quieter and slower: 1 is about 20 ms per reading."),
        Param("goto_peak", "Go to the biggest peak", "bool", True,
              help="Afterwards, set the magnet on the most prominent peak of the scan (if there is one)."),
        Param("settle_time", "Settle time", "seconds", 0.1, minimum=0.0, advanced=True,
              help="Wait at each point after the magnet has settled, before reading."),
        Param("tolerance", "Magnet tolerance", "number", 0.05, unit="A", minimum=1e-6, advanced=True,
              help="How close the magnet readback must come to each point's setpoint (5 s at most)."),
        Param("sample_interval", "Sample interval", "seconds", 0.02, minimum=0.0, advanced=True,
              help="Time between readings (at least one integration)."),
        Param("export", "Save the scan as CSV", "bool", False, advanced=True,
              help="Write the scan to a CSV file, as the Mass Scan panel does."),
        Param("export_path", "CSV file name (start)", "text", "", advanced=True,
              help="Path and start of the file name; the date and time are added. Empty: "
                   "data/exports/Mass_Scans/MassScan."),
    )

    @classmethod
    def validate(cls, params: dict) -> list:
        errors = _check(cls, params)
        if "loop" in params and number(params.get("loop")):
            errors.append("loop isn't allowed in a recipe: the step has to end (use the Mass Scan panel to "
                          "scan again and again)")
        unknown = sorted(set(params) - known_keys(cls) - {"loop"})
        if unknown:
            errors.append(f"unknown setting(s) {', '.join(unknown)}")
        if not errors:
            try:
                cls._config(params, (float("-inf"), float("inf")))   # the registry limits: recipe_check
            except ValueError as e:
                errors.append(str(e))
        return errors

    @staticmethod
    def _config(params, limits):
        from src.sequencer.mass_scan import ScanConfig
        settings = {k: v for k, v in params.items() if k not in ("goto_peak",)}
        settings["loop"] = False
        settings.setdefault("nplc", 1.0)
        return ScanConfig.from_value(settings, limits)

    def __init__(self, parameters: dict):
        super().__init__(parameters)
        from src.sequencer.mass_scan import MassScan
        self.goto_peak = bool(number(parameters.get("goto_peak", True)))
        self.config_error = None
        self.scan = None
        self.files = []
        try:
            self.cfg = self._config(parameters, _magnet_limits())
            self.scan = MassScan(self.cfg, io=self, on_pass_done=self._pass_done)
        except (ValueError, TypeError) as e:
            self.config_error = f"mass scan settings: {e}"
        self._telemetry = {}
        self._cmd = None
        self._gen = None
        self._paused = 0.0
        self.peak_a = None
        self._checked = False

    # io for MassScan
    stopping = False

    def now(self) -> float:
        return time.time() - self._paused        # pauses don't count against the scan's timeouts

    def send(self, tag, value, timeout=None):
        return send(self._cmd, tag, value, timeout=timeout)

    def fresh_float(self, tag):
        value = fresh_value(self._telemetry, tag)
        try:
            return None if value is None else float(value)
        except (TypeError, ValueError):
            return None

    def add_pause_offset(self, duration: float):
        self._paused += duration

    def _pass_done(self, pass_no, points):
        if not self.cfg.export or not points:
            return
        from src.sequencer.mass_scan import export_csv
        try:
            self.files.append(export_csv(points, self.cfg.export_path))
            log.info("Mass scan routine: saved %s", self.files[-1])
        except OSError as e:
            log.error("Mass scan routine: scan not saved: %s", e)

    def tick(self, telemetry_cache: dict, cmd_thread) -> str:
        from src.sequencer.jobs import JobFailed
        if self.config_error:
            return self._fail(self.config_error)
        self._telemetry, self._cmd = telemetry_cache, cmd_thread
        if not self._checked:
            # Scanning with the magnet off or the cup out measures noise for minutes, then goes to a
            # 'peak' in it
            if not holds(self._get(telemetry_cache, MAGNET_ENABLED), "==", 1.0):
                return self._fail("the analyser magnet is off (or its state isn't known): switch it on first")
            if not holds(self._get(telemetry_cache, CUP_INSERTED), "==", 1.0):
                return self._fail("the Faraday cup isn't in (or its position isn't known): insert it first")
            self._checked = True
        if self._gen is None:
            self._gen = self._run()
        try:
            next(self._gen)
            return "RUNNING"
        except StopIteration:
            return "DONE"
        except JobFailed as e:
            return self._fail(str(e))

    def _run(self):
        from src.sequencer.jobs import JobFailed
        from src.sequencer.mass_scan import SP_TAG, strongest_peak
        error = None
        try:
            yield from self.scan.run()
        except JobFailed as e:
            error = e
        yield from self.scan.restore()           # SMU back to fast readings either way
        if error is not None:
            raise error
        if not self.goto_peak:
            return
        self.peak_a = strongest_peak(self.scan.points)
        if self.peak_a is None:
            log.info("Mass scan routine: no distinct peak - magnet left at the end of the scan")
            return
        log.info("Mass scan routine: setting the magnet on the peak at %.3f A", self.peak_a)
        ticket = send(self._cmd, SP_TAG, self.peak_a)
        while not ticket.done():
            yield
        if not ticket.ok:
            raise JobFailed(f"could not set the magnet on the peak ({self.peak_a:.3f} A): {ticket.reason}")


_registry_limits = {}


def _magnet_limits():
    """(min, max) of the magnet current setpoint from the tag registry (read once)."""
    if "magnet" not in _registry_limits:
        import json
        from src.core import paths
        from src.sequencer.mass_scan import SP_TAG
        try:
            with open(paths.REGISTRY_PATH, "r") as f:
                meta = json.load(f).get(SP_TAG, {})
            _registry_limits["magnet"] = (meta.get("min_val"), meta.get("max_val"))
        except (OSError, ValueError) as e:
            log.error("Tag registry not readable for the mass scan limits: %s", e)
            return None
    return _registry_limits["magnet"]


# ============================================================================ safety_relay_init
class SafetyRelayRoutine(BaseRoutine):
    """
    Brings the safety relay on, with the operator:
    1. done at once if the relay is already on;
    2. if the gate valve isn't open: waits (with a prompt) for the source vacuum to be below
       vacuum_max, then opens the valve, if the PLC allows it (or asks the operator to);
    3. asks the operator to acknowledge and press START, sends the relay enable and waits
       timeout_sec for the relay;
    4. waits stabilization_sec before it is done.
    """
    TITLE = "Bring the safety relay on, with the operator"
    PARAMS = (
        Param("timeout_sec", "Time to press START", "seconds", 6.0, minimum=1.0, maximum=60.0,
              help="After the operator acknowledges, how long the relay has to come on. The PLC itself gives "
                   "up after 5 s."),
        Param("gv_timeout_sec", "Time for the gate valve to open", "seconds", 15.0, minimum=1.0, maximum=300.0),
        Param("stabilization_sec", "Wait after the relay comes on", "seconds", 15.0, minimum=0.0, maximum=600.0),
        Param("vacuum_max", "Source pressure needed first", "number", 5e-7, unit="mbar", minimum=0.0,
              help="The gate valve is opened only with the source pressure below this (the start-up "
                   "procedure: 5e-7 mbar). 0: not checked."),
        Param("vacuum_tag", "Source pressure gauge", "readable_tag", VG1_PRESSURE, advanced=True),
        Param("cmd_gv_open", "Gate valve open command", "settable_tag", "ion_beam.source.chamber.cmd_open_gv",
              advanced=True, aliases=("gv_cmd_open",)),
        Param("gv_stat_open", "Gate valve open status", "readable_tag", "ion_beam.source.chamber.stat_gv_open",
              advanced=True),
        Param("gv_open_perm", "Gate valve open permissive", "readable_tag",
              "ion_beam.source.chamber.stat_gv_open_permissive", advanced=True),
        Param("cmd_virtual", "Safety relay enable command", "settable_tag", "ion_beam.system.cmd_enable_safety",
              advanced=True),
        Param("rb_relay", "Safety relay status", "readable_tag", RELAY, advanced=True),
        Param("gv_stat_closed", "(not used)", "readable_tag", "", advanced=True),
    )

    def __init__(self, parameters: dict):
        super().__init__(parameters)
        self.gv_cmd_open = self.p("cmd_gv_open")
        self.gv_stat_open = self.p("gv_stat_open")
        self.gv_open_perm = self.p("gv_open_perm")
        self.cmd_virtual = self.p("cmd_virtual")
        self.rb_relay = self.p("rb_relay")
        self.vacuum_tag = self.p("vacuum_tag")
        self.vacuum_max = self.num("vacuum_max")
        self.timeout_sec = self.num("timeout_sec")
        self.gv_timeout_sec = self.num("gv_timeout_sec")
        self.stabilization_sec = self.num("stabilization_sec")

        self.timer_start = 0.0
        self.state = "INIT"
        self.next_state = ""
        self.prompt_text = ""

    def add_pause_offset(self, duration: float):
        self.timer_start += duration

    def _ask(self, text: str, then: str) -> str:
        self.prompt_text = text
        self.next_state = then
        self.state = "WAITING_ON_ACK"
        return "WAITING_FOR_USER"

    def tick(self, telemetry_cache: dict, cmd_thread) -> str:
        bad = self._command_failure()
        if bad:
            return self._fail(bad)

        if self.state == "INIT":
            if holds(self._get(telemetry_cache, self.rb_relay), "==", 1.0):
                return "DONE"
            self.state = "CHECK_GV_STATE"
            return "RUNNING"

        elif self.state == "CHECK_GV_STATE":
            if holds(self._get(telemetry_cache, self.gv_stat_open), "==", 1.0):
                self.state = "CHECK_PERMISSIVES"
                return "RUNNING"
            if self.vacuum_max > 0 and self.vacuum_tag:
                pressure = self._get(telemetry_cache, self.vacuum_tag)
                if not holds(pressure, "<", self.vacuum_max):
                    shown = "not known" if number(pressure) is None else f"{number(pressure):.2e} mbar"
                    return self._ask(f"The source pressure is {shown}: the gate valve should only open below "
                                     f"{self.vacuum_max:.1e} mbar. Wait for it, then click OK.", "CHECK_GV_STATE")
            # GV is closed or travelling. Ask user if we should open it or if they need to manually fix.
            return self._ask("Safety Relay requires Gate Valve to be OPEN. Click OK to attempt an automatic Open, "
                             "or manually open it now.", "ATTEMPT_GV_OPEN")

        elif self.state == "ATTEMPT_GV_OPEN":
            if not holds(self._get(telemetry_cache, self.gv_open_perm), "==", 1.0):
                return self._ask("Gate Valve cannot be opened: Permissive is FALSE. Resolve vacuum/gauge fault, then "
                                 "click OK to retry.", "CHECK_GV_STATE")

            self._send_cmd(cmd_thread, self.gv_cmd_open, 1.0)
            self.timer_start = time.time()
            self.state = "WAIT_GV"
            return "RUNNING"

        elif self.state == "WAIT_GV":
            if holds(self._get(telemetry_cache, self.gv_stat_open), "==", 1.0):
                self.state = "CHECK_PERMISSIVES"
                return "RUNNING"
            if time.time() - self.timer_start > self.gv_timeout_sec:
                return self._ask("Gate Valve failed to open automatically. Please verify vacuum levels manually, "
                                 "open the valve, then click OK.", "CHECK_GV_STATE")
            return "RUNNING"

        elif self.state == "CHECK_PERMISSIVES":
            if not holds(self._get(telemetry_cache, self.gv_stat_open), "==", 1.0):
                return self._ask("Gate Valve is not fully open. Please remedy this and click OK to re-evaluate.",
                                 "CHECK_GV_STATE")
            return self._ask("Safety Permissives Met. Turn the key to ENABLE, click OK, then immediately press START "
                             "on the High Voltage Systems Control box.", "START_PHYSICAL_TIMER")

        # --- THE HOLDING PATTERN ---
        elif self.state == "WAITING_ON_ACK":
            # The routine sits here doing nothing while the GUI waits for the human
            return "WAITING_FOR_USER"

        elif self.state == "START_PHYSICAL_TIMER":
            self._send_cmd(cmd_thread, self.cmd_virtual, 1.0)
            self.timer_start = time.time()
            self.state = "WAIT_PHYSICAL"
            return "RUNNING"

        elif self.state == "WAIT_PHYSICAL":
            if holds(self._get(telemetry_cache, self.rb_relay), "==", 1.0):
                # Relay is active, start the stabilization countdown instead of finishing
                log.info("Relay active. Stabilising communications for %.0f s...", self.stabilization_sec)
                self.timer_start = time.time()
                self.state = "WAIT_POST_RELAY_STABILIZATION"
                return "RUNNING"

            if time.time() - self.timer_start > self.timeout_sec:
                self._send_cmd(cmd_thread, self.cmd_virtual, 0.0)
                return self._fail("safety relay did not energise: physical PANEL START not pressed in time")
            return "RUNNING"

        elif self.state == "WAIT_POST_RELAY_STABILIZATION":
            if time.time() - self.timer_start > self.stabilization_sec:
                return "DONE"
            return "RUNNING"

        return "RUNNING"

    def acknowledge(self):
        """Called by the Step when the GUI confirms the user clicked OK."""
        if self.state == "WAITING_ON_ACK":
            # Release the holding pattern and move to whatever state was queued!
            self.state = self.next_state


# ============================================================================ hv_conditioning
class HVConditioningRoutine(BaseRoutine):
    """
    Ramps a supply that is already on from its present readback up to max_val, in steps of
    step_size held hold_time_sec each. While the pressure is above pressure_max it holds, until
    it is below pressure_recover; when the arc flag reads 1 it steps back by backoff_step and waits
    arc_cooldown_sec, and fails after max_arcs arcs. It fails if the supply is off (enabled_tag),
    if its readback doesn't follow the setpoint, or if a watched value has no live value for 30 s.
    """
    TITLE = "Ramp a supply up slowly (conditioning)"
    PARAMS = (
        Param("cmd_tag", "Setpoint", "settable_tag", "", True, help="The setpoint the ramp sends."),
        Param("rb_tag", "Readback", "readable_tag", "", True, help="Where the ramp starts, and what must follow it."),
        Param("enabled_tag", "On status", "readable_tag", "",
              help="The supply's 'output on' status: the ramp fails if the supply is off."),
        Param("max_val", "Up to", "number", None, True, unit_of="cmd_tag",
              help="Where the ramp ends, in the setpoint's unit."),
        Param("step_size", "Step", "number", 0.5, unit_of="cmd_tag", minimum=1e-9),
        Param("hold_time_sec", "Hold each step", "seconds", 60.0, minimum=0.0),
        Param("pressure_tag", "Pressure gauge", "readable_tag", VG1_PRESSURE,
              help="The ramp holds while this pressure is above the hold point. Empty: no pressure watch."),
        Param("pressure_max", "Hold above", "number", 5e-6, unit="mbar", minimum=0.0,
              help="Keep it below the gauge relay's off point, or the interlock drops the source first."),
        Param("pressure_recover", "Carry on below", "number", 2e-6, unit="mbar", minimum=0.0),
        Param("arc_mode_tag", "Arc flag", "readable_tag", "", advanced=True,
              help="A flag that reads 1 while the supply arcs. Empty: arcs aren't watched."),
        Param("backoff_step", "Back off by", "number", 1.0, unit_of="cmd_tag", minimum=0.0, advanced=True),
        Param("max_arcs", "Most arcs", "integer", 5, minimum=0, advanced=True),
        Param("arc_cooldown_sec", "Wait after an arc", "seconds", 5.0, minimum=0.0, advanced=True),
        Param("follow_tolerance", "Readback must follow within", "number", None, unit_of="cmd_tag", minimum=0.0,
              advanced=True, help="At the end of each hold the readback must be this close to the setpoint "
                                  "(empty: one step)."),
    )

    @classmethod
    def validate(cls, params: dict) -> list:
        errors = _check(cls, params)
        rec, mx = number(params.get("pressure_recover", 2e-6)), number(params.get("pressure_max", 5e-6))
        if rec is not None and mx is not None and rec > mx:
            errors.append(f"pressure_recover ({rec:.1e}) must not be above pressure_max ({mx:.1e})")
        return errors

    def __init__(self, parameters: dict):
        super().__init__(parameters)
        self.cmd_tag = self.p("cmd_tag")
        self.rb_tag = self.p("rb_tag")
        self.enabled_tag = self.p("enabled_tag") or ""
        self.max_val = self.num("max_val")
        self.step_size = self.num("step_size")
        self.hold_time_sec = self.num("hold_time_sec")

        # Pressure constraints
        self.pressure_tag = self.p("pressure_tag") or ""
        self.pressure_max = self.num("pressure_max")
        self.pressure_recover = self.num("pressure_recover")

        # Arc constraints
        self.arc_mode_tag = self.p("arc_mode_tag") or ""
        self.backoff_step = self.num("backoff_step")
        self.max_arcs = int(self.num("max_arcs"))
        self.arc_cooldown_sec = self.num("arc_cooldown_sec")
        follow = number(self.p("follow_tolerance"))
        self.follow_tolerance = follow if follow is not None else self.step_size

        self.current_sp = 0.0
        self.timer_start = 0.0
        self.state = "INIT"
        self.arc_count = 0
        self.arc_cooldown_start = 0.0
        self.unknown_since = None

    def add_pause_offset(self, duration: float):
        self.timer_start += duration
        self.arc_cooldown_start += duration
        if self.unknown_since is not None:
            self.unknown_since += duration

    def tick(self, telemetry_cache: dict, cmd_thread) -> str:
        bad = self._command_failure()
        if bad:
            return self._fail(bad)

        # 0. Never ramp blind: if the pressure, arc or on/off signal has no live value (its service
        #    down), hold the setpoint where it is, and give up if it stays unknown.
        watched = [t for t in (self.pressure_tag, self.arc_mode_tag, self.enabled_tag) if t]
        missing = [t for t in watched if self._get(telemetry_cache, t) is None]
        if missing:
            if self.unknown_since is None:
                self.unknown_since = time.time()
                log.warning("HV conditioning holding at %.3f: no live value for %s", self.current_sp, missing)
            elif time.time() - self.unknown_since > STALE_HOLD_FAIL_S:
                return self._fail(f"no live value for {', '.join(missing)} for {STALE_HOLD_FAIL_S:.0f} s")
            if self.state == "HOLD":
                self.timer_start = time.time()   # the hold only counts while we can see the pressure
            return "RUNNING"
        self.unknown_since = None

        if self.enabled_tag and not holds(self._get(telemetry_cache, self.enabled_tag), "==", 1.0):
            return self._fail(f"the supply is off ({self.enabled_tag} is 0): switch it on before conditioning it")

        # 1. Continuous Safety Checks (Priority Overrides)

        # Arc detection, from the arc flag (a PLC quench or a Spellman arc flag): a software dI/dt
        # at 20 Hz would alias on network jitter.
        if self.arc_mode_tag and holds(self._get(telemetry_cache, self.arc_mode_tag), "==", 1.0):
            if self.state != "ARC_RECOVERY":
                self.arc_count += 1
                if self.arc_count > self.max_arcs:
                    return self._fail(f"exceeded the maximum of {self.max_arcs} arcs")

                self.current_sp = max(0.0, self.current_sp - self.backoff_step)
                self._send_cmd(cmd_thread, self.cmd_tag, self.current_sp)
                self.state = "ARC_RECOVERY"
                self.arc_cooldown_start = time.time()
                return "RUNNING"

        # Pressure Check
        if self.pressure_tag and self.state not in ["PAUSE_PRESSURE", "ARC_RECOVERY"]:
            press = number(self._get(telemetry_cache, self.pressure_tag))
            if press is not None and press > self.pressure_max:
                self.state = "PAUSE_PRESSURE"
        #
        # 2. State Machine
        if self.state == "INIT":
            start_val = number(self._get(telemetry_cache, self.rb_tag))
            if start_val is None:
                return self._fail(f"no live readback {self.rb_tag} to start the ramp from")
            self.current_sp = start_val
            self.state = "RAMP"
            return "RUNNING"

        elif self.state == "ARC_RECOVERY":
            if time.time() - self.arc_cooldown_start > self.arc_cooldown_sec:
                if not holds(self._get(telemetry_cache, self.arc_mode_tag), "==", 1.0):
                    self.timer_start = time.time()
                    self.state = "HOLD"
            return "RUNNING"

        elif self.state == "PAUSE_PRESSURE":
            press = number(self._get(telemetry_cache, self.pressure_tag))
            if press is not None and press < self.pressure_recover:
                self.timer_start = time.time()  # Reset hold timer to ensure thermal stability
                self.state = "HOLD"
            return "RUNNING"

        elif self.state == "RAMP":
            if self.current_sp >= self.max_val:
                return "DONE"

            self.current_sp = min(self.max_val, self.current_sp + self.step_size)
            self._send_cmd(cmd_thread, self.cmd_tag, self.current_sp)
            self.timer_start = time.time()
            self.state = "HOLD"
            return "RUNNING"

        elif self.state == "HOLD":
            if time.time() - self.timer_start > self.hold_time_sec:
                rb = number(self._get(telemetry_cache, self.rb_tag))
                if rb is None or abs(rb - self.current_sp) > self.follow_tolerance:
                    return self._fail(f"the readback ({rb}) didn't follow the setpoint ({self.current_sp:g}) within "
                                      f"{self.follow_tolerance:g}: is the supply on?")
                self.state = "RAMP"
            return "RUNNING"

        return "RUNNING"


# ============================================================================ psu_control
class PSUControlRoutine(BaseRoutine):
    """
    Switches a supply on (unless it is on already) and sends its setpoint. With wait_for_readback,
    waits until the readback is within tolerance of it; without, waits only for the service to
    accept the setpoint. Fails if the safety relay is off (requires_relay), if the supply doesn't
    report on within 5 s, if the setpoint is refused, or if the readback isn't there in time.
    """
    TITLE = "Switch on a supply and set it"
    PARAMS = (
        Param("cmd_enable", "On/off command", "settable_tag", "", True),
        Param("stat_enabled", "On status", "readable_tag", "", True),
        Param("sp_tag", "Setpoint", "settable_tag", ""),
        Param("rb_tag", "Readback", "readable_tag", ""),
        Param("target_val", "Set it to", "number", 0.0, unit_of="sp_tag"),
        Param("wait_for_readback", "Wait for the readback", "bool", True,
              help="Off for the thermionic current, which follows the filament rather than its setpoint."),
        Param("tolerance", "Readback within", "number", 0.5, unit_of="sp_tag", minimum=0.0),
        Param("timeout_sec", "For up to", "seconds", 10.0, minimum=0.0),
        Param("requires_relay", "Needs the safety relay", "bool", True,
              help="Fail at once if the safety relay is off."),
        Param("rb_relay", "Safety relay status", "readable_tag", RELAY, advanced=True),
    )

    def __init__(self, parameters: dict):
        super().__init__(parameters)
        self.cmd_enable = self.p("cmd_enable")
        self.stat_enabled = self.p("stat_enabled")
        self.sp_tag = self.p("sp_tag") or ""
        self.rb_tag = self.p("rb_tag") or ""
        self.target_val = self.num("target_val")
        self.tolerance = self.num("tolerance")
        self.timeout_sec = self.num("timeout_sec")
        self.wait_for_readback = bool(self.num("wait_for_readback"))
        self.requires_relay = bool(self.num("requires_relay"))
        self.rb_relay = self.p("rb_relay")

        self.timer_start = 0.0
        self.state = "INIT"
        self._sp_ticket = None

    def add_pause_offset(self, duration: float):
        self.timer_start += duration

    def tick(self, telemetry_cache: dict, cmd_thread) -> str:
        bad = self._command_failure()
        if bad:
            return self._fail(bad)

        if self.state == "INIT":
            # 1. Check Safety Relay if required
            if self.requires_relay:
                if not holds(self._get(telemetry_cache, self.rb_relay), "==", 1.0):
                    return self._fail(f"safety relay is not active (needed for {self.sp_tag or self.cmd_enable})")

            # 2. Check if already enabled
            if holds(self._get(telemetry_cache, self.stat_enabled), "==", 1.0):
                self.state = "APPLY_SETPOINT"
            else:
                self._send_cmd(cmd_thread, self.cmd_enable, 1.0)
                self.timer_start = time.time()
                self.state = "WAIT_ENABLE"
            return "RUNNING"

        elif self.state == "WAIT_ENABLE":
            if holds(self._get(telemetry_cache, self.stat_enabled), "==", 1.0):
                self.state = "APPLY_SETPOINT"
                return "RUNNING"

            if time.time() - self.timer_start > 5.0:  # Hardcoded 5s timeout just for the enable bit
                return self._fail(f"{self.stat_enabled} did not report enabled within 5 s of {self.cmd_enable}")
            return "RUNNING"

        elif self.state == "APPLY_SETPOINT":
            if not self.sp_tag:
                return "DONE"
            self._sp_ticket = self._send_cmd(cmd_thread, self.sp_tag, self.target_val)
            self.timer_start = time.time()
            self.state = "WAIT_READBACK" if self.wait_for_readback and self.rb_tag else "WAIT_SETPOINT_ANSWER"
            return "RUNNING"

        elif self.state == "WAIT_SETPOINT_ANSWER":
            # Without a readback to wait for, done once the service has accepted the setpoint (a refusal
            # fails the step above): it used to be done before the answer came, so a refusal went unseen
            outcome = ticket_outcome(self._sp_ticket)
            if outcome is None:
                return "RUNNING"
            return "DONE"

        elif self.state == "WAIT_READBACK":
            current_rb = number(self._get(telemetry_cache, self.rb_tag))
            if current_rb is not None and abs(current_rb - self.target_val) <= self.tolerance:
                return "DONE"

            if time.time() - self.timer_start > self.timeout_sec:
                return self._fail(f"{self.rb_tag} did not reach {self.target_val} +/- {self.tolerance} within "
                                  f"{self.timeout_sec:g} s (last live value: {current_rb})")
            return "RUNNING"

        return "RUNNING"


ROUTINE_REGISTRY = {
    "mass_scan": MassScanRoutine,
    "safety_relay_init": SafetyRelayRoutine,
    "hv_conditioning": HVConditioningRoutine,
    "psu_control": PSUControlRoutine
}


def routine_defaults(name: str) -> dict:
    """The parameters a new step of this routine starts with (every one the routine reads, so the
    file says what runs)."""
    cls = ROUTINE_REGISTRY[name]
    return {p.key: p.default for p in cls.PARAMS
            if p.default is not None and p.key != "gv_stat_closed" and not (p.kind == "text" and p.default == "")}
