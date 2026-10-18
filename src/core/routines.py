import logging
import time

from src.core.commands import describe, route_for_tag
from src.core.telemetry_cache import fresh_value

log = logging.getLogger("routines")

STALE_HOLD_FAIL_S = 30.0   # a routine waiting on a value with no live data fails after this long


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


def evaluate_condition(current_val, condition_str, target_val) -> bool:
    if current_val is None: return False
    try:
        c = float(current_val)
        t = float(target_val)
        if condition_str == "==" and abs(c - t) < 1e-6: return True
        elif condition_str == ">" and c > t: return True
        elif condition_str == "<" and c < t: return True
        elif condition_str == ">=" and c >= t: return True
        elif condition_str == "<=" and c <= t: return True
    except (ValueError, TypeError):
        pass
    return False

class BaseRoutine:
    def __init__(self, parameters: dict):
        self.parameters = parameters
        self.state = "INIT"
        self.fail_reason = ""
        self._tickets = []

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
        return fresh_value(telemetry, tag)


class MassScanRoutine(BaseRoutine):
    """The precision mass scan (src/sequencer/mass_scan.py - the same code as the HMI's Mass Scan
    panel) as a recipe step. With goto_peak (the default) the magnet is then set on the most
    prominent peak of the scan.

    Parameters: start_a, stop_a, step_a (magnet amps) and optionally tolerance, settle_time,
    samples, sample_interval, nplc, export, export_path, goto_peak."""

    def __init__(self, parameters: dict):
        super().__init__(parameters)
        from src.sequencer.mass_scan import MassScan, ScanConfig
        settings = {k: v for k, v in parameters.items() if k != "goto_peak"}
        settings.setdefault("nplc", 1.0)
        self.goto_peak = bool(parameters.get("goto_peak", True))
        self.config_error = None
        self.scan = None
        try:
            self.scan = MassScan(ScanConfig.from_value(settings, _magnet_limits()), io=self)
        except ValueError as e:
            self.config_error = f"mass scan settings: {e}"
        self._telemetry = {}
        self._cmd = None
        self._gen = None
        self._paused = 0.0
        self.peak_a = None

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

    def tick(self, telemetry_cache: dict, cmd_thread) -> str:
        from src.sequencer.jobs import JobFailed
        if self.config_error:
            return self._fail(self.config_error)
        self._telemetry, self._cmd = telemetry_cache, cmd_thread
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


class SafetyRelayRoutine(BaseRoutine):
    """
    Initializes the facility safety system.
    1. Checks if relay is active.
    2. Opens gate valve (automatic open or manual request).
    3. Prompts operator for physical panel start.
    4. Waits 15s for comms to stabilize before completing.
    """
    def __init__(self, parameters: dict):
        super().__init__(parameters)
        # Gate Valve Tags
        self.gv_cmd_open = self.parameters.get("cmd_gv_open", "ion_beam.source.chamber.cmd_open_gv")
        self.gv_stat_open = self.parameters.get("gv_stat_open", "ion_beam.source.chamber.stat_gv_open")
        self.gv_stat_closed = self.parameters.get("gv_stat_closed", "ion_beam.source.chamber.stat_gv_closed")
        self.gv_open_perm = self.parameters.get("gv_open_perm", "ion_beam.source.chamber.stat_gv_open_permissive")

        self.cmd_virtual = self.parameters.get("cmd_virtual", "ion_beam.system.cmd_enable_safety")
        self.rb_relay = self.parameters.get("rb_relay", "ion_beam.facilities.safety_relay_active")

        self.timeout_sec = float(self.parameters.get("timeout_sec", 6.0))
        self.gv_timeout_sec = float(self.parameters.get("gv_timeout_sec", 15.0))
        self.stabilization_sec = float(self.parameters.get("stabilization_sec", 15.0))

        self.timer_start = 0.0
        self.state = "INIT"
        self.next_state = ""
        self.prompt_text = ""

    def tick(self, telemetry_cache: dict, cmd_thread) -> str:
        bad = self._command_failure()
        if bad:
            return self._fail(bad)

        if self.state == "INIT":
            if evaluate_condition(self._get(telemetry_cache, self.rb_relay), "==", 1.0):
                return "DONE"
            self.state = "CHECK_GV_STATE"
            return "RUNNING"

        elif self.state == "CHECK_GV_STATE":
            gv_is_open = evaluate_condition(self._get(telemetry_cache, self.gv_stat_open), "==", 1.0)

            if gv_is_open:
                self.state = "CHECK_PERMISSIVES"
            else:
                # GV is closed or travelling. Ask user if we should open it or if they need to manually fix.
                self.prompt_text = "Safety Relay requires Gate Valve to be OPEN. Click OK to attempt an automatic Open, or manually open it now."
                self.next_state = "ATTEMPT_GV_OPEN"
                self.state = "WAITING_ON_ACK"
                return "WAITING_FOR_USER"
            return "RUNNING"

        elif self.state == "ATTEMPT_GV_OPEN":
            permissive = evaluate_condition(self._get(telemetry_cache, self.gv_open_perm), "==", 1.0)
            if not permissive:
                self.prompt_text = "Gate Valve cannot be opened: Permissive is FALSE. Resolve vacuum/gauge fault, then click OK to retry."
                self.next_state = "CHECK_GV_STATE"
                self.state = "WAITING_ON_ACK"
                return "WAITING_FOR_USER"

            self._send_cmd(cmd_thread, self.gv_cmd_open, 1.0)
            self.timer_start = time.time()
            self.state = "WAIT_GV"
            return "RUNNING"

        elif self.state == "WAIT_GV":
            if evaluate_condition(self._get(telemetry_cache, self.gv_stat_open), "==", 1.0):
                self.state = "CHECK_PERMISSIVES"
                return "RUNNING"
            if time.time() - self.timer_start > self.gv_timeout_sec:
                self.prompt_text = "Gate Valve failed to open automatically. Please verify vacuum levels manually, open the valve, then click OK."
                self.next_state = "CHECK_GV_STATE"
                self.state = "WAITING_ON_ACK"
                return "WAITING_FOR_USER"
            return "RUNNING"

        elif self.state == "CHECK_PERMISSIVES":
            gv_is_open = evaluate_condition(self._get(telemetry_cache, self.gv_stat_open), "==", 1.0)

            if not gv_is_open:
                self.prompt_text = "Gate Valve is not fully open. Please remedy this and click OK to re-evaluate."
                self.next_state = "CHECK_GV_STATE"
            else:
                self.prompt_text = "Safety Permissives Met. Click OK, then immediately press the physical PANEL START button."
                self.next_state = "START_PHYSICAL_TIMER"

            self.state = "WAITING_ON_ACK"
            return "WAITING_FOR_USER"

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
            if evaluate_condition(self._get(telemetry_cache, self.rb_relay), "==", 1.0):
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

class HVConditioningRoutine(BaseRoutine):
    """
    Safely ramps up a power supply setpoint over a slow ramp time (must be already enabled).
    During the ramp, if a pressure spike or arc occurs, the setpoint is held steady until it recovers.
    """
    def __init__(self, parameters: dict):
        super().__init__(parameters)
        self.cmd_tag = self.parameters.get("cmd_tag", "")
        self.rb_tag = self.parameters.get("rb_tag", "")
        self.max_val = float(self.parameters.get("max_val", 30.0))
        self.step_size = float(self.parameters.get("step_size", 1.0))
        self.hold_time_sec = float(self.parameters.get("hold_time_sec", 60.0))

        # Pressure constraints
        self.pressure_tag = self.parameters.get("pressure_tag", "")
        self.pressure_max = float(self.parameters.get("pressure_max", 5e-5))
        self.pressure_recover = float(self.parameters.get("pressure_recover", 1e-5))

        # Arc constraints
        self.arc_mode_tag = self.parameters.get("arc_mode_tag", "")
        self.backoff_step = float(self.parameters.get("backoff_step", 2.0))
        self.max_arcs = int(self.parameters.get("max_arcs", 5))
        self.arc_cooldown_sec = float(self.parameters.get("arc_cooldown_sec", 5.0))

        self.current_sp = 0.0
        self.timer_start = 0.0
        self.state = "INIT"
        self.arc_count = 0
        self.arc_cooldown_start = 0.0
        self.unknown_since = None

    def tick(self, telemetry_cache: dict, cmd_thread) -> str:
        bad = self._command_failure()
        if bad:
            return self._fail(bad)

        # 0. Never ramp blind: if the pressure or arc signal has no live value (vacuum or PLC
        #    service down), hold the setpoint where it is, and give up if it stays unknown.
        watched = [t for t in (self.pressure_tag, self.arc_mode_tag) if t]
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

        # 1. Continuous Safety Checks (Priority Overrides)

        # Arc Detection
        # NOTE: Relying strictly on the PLC arc quench flag here. Calculating dI/dt and dV/dt
        # purely in software over a 20Hz polling loop is highly susceptible to aliasing and network jitter.
        # Please manually verify if a pure software threshold calculation is strictly required over the PLC flag.
        if self.arc_mode_tag and evaluate_condition(self._get(telemetry_cache, self.arc_mode_tag), "==", 1.0):
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
            press = self._get(telemetry_cache, self.pressure_tag)
            if press is not None and float(press) > self.pressure_max:
                self.state = "PAUSE_PRESSURE"
        #
        # 2. State Machine
        if self.state == "INIT":
            start_val = self._get(telemetry_cache, self.rb_tag)
            if start_val is None:
                return self._fail(f"no live readback {self.rb_tag} to start the ramp from")
            self.current_sp = float(start_val)
            self.state = "RAMP"
            return "RUNNING"

        elif self.state == "ARC_RECOVERY":
            if time.time() - self.arc_cooldown_start > self.arc_cooldown_sec:
                if not evaluate_condition(self._get(telemetry_cache, self.arc_mode_tag), "==", 1.0):
                    self.timer_start = time.time()
                    self.state = "HOLD"
            return "RUNNING"

        elif self.state == "PAUSE_PRESSURE":
            press = self._get(telemetry_cache, self.pressure_tag)
            if press is not None and float(press) < self.pressure_recover:
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
                self.state = "RAMP"
            return "RUNNING"

        return "RUNNING"


class PSUControlRoutine(BaseRoutine):
    """
    Safely boots a power supply.
    Checks safety relay permissives, enables output,
    applies setpoint.
    If wait_for_readback=1.0, blocks execution until rb_tag is within tolerance of target.
    If wait_for_readback=0.0, completes immediately after sending the setpoint (useful for Thermionic supplies).
    """
    def __init__(self, parameters: dict):
        super().__init__(parameters)
        self.cmd_enable = self.parameters.get("cmd_enable", "")
        self.stat_enabled = self.parameters.get("stat_enabled", "")
        self.sp_tag = self.parameters.get("sp_tag", "")
        self.rb_tag = self.parameters.get("rb_tag", "")
        self.target_val = float(self.parameters.get("target_val", 0.0))
        self.tolerance = float(self.parameters.get("tolerance", 0.5))
        self.timeout_sec = float(self.parameters.get("timeout_sec", 10.0))

        self.wait_for_readback = bool(float(self.parameters.get("wait_for_readback", 1.0)))

        # Security: Does this PSU require the safety relay to be active?
        self.requires_relay = bool(float(self.parameters.get("requires_relay", 1.0)))
        self.rb_relay = self.parameters.get("rb_relay", "ion_beam.facilities.safety_relay_active")

        self.timer_start = 0.0
        self.state = "INIT"

    def tick(self, telemetry_cache: dict, cmd_thread) -> str:
        bad = self._command_failure()
        if bad:
            return self._fail(bad)

        if self.state == "INIT":
            # 1. Check Safety Relay if required
            if self.requires_relay:
                if not evaluate_condition(self._get(telemetry_cache, self.rb_relay), "==", 1.0):
                    return self._fail(f"safety relay is not active (needed for {self.sp_tag or self.cmd_enable})")

            # 2. Check if already enabled
            if evaluate_condition(self._get(telemetry_cache, self.stat_enabled), "==", 1.0):
                self.state = "APPLY_SETPOINT"
            else:
                self._send_cmd(cmd_thread, self.cmd_enable, 1.0)
                self.timer_start = time.time()
                self.state = "WAIT_ENABLE"
            return "RUNNING"

        elif self.state == "WAIT_ENABLE":
            if evaluate_condition(self._get(telemetry_cache, self.stat_enabled), "==", 1.0):
                self.state = "APPLY_SETPOINT"
                return "RUNNING"

            if time.time() - self.timer_start > 5.0:  # Hardcoded 5s timeout just for the enable bit
                return self._fail(f"{self.stat_enabled} did not report enabled within 5 s of {self.cmd_enable}")
            return "RUNNING"

        elif self.state == "APPLY_SETPOINT":
            # Only send a setpoint if a tag was actually provided
            if self.sp_tag:
                self._send_cmd(cmd_thread, self.sp_tag, self.target_val)

            # Decide whether to wait or move on immediately
            if self.wait_for_readback and self.rb_tag:
                self.timer_start = time.time()
                self.state = "WAIT_READBACK"
                return "RUNNING"
            else:
                return "DONE"

        elif self.state == "WAIT_READBACK":
            current_rb = self._get(telemetry_cache, self.rb_tag)
            if current_rb is not None:
                # Check if readback is within +/- tolerance of target
                if abs(float(current_rb) - self.target_val) <= self.tolerance:
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