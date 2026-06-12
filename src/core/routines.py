import time
import numpy as np
from scipy.signal import find_peaks

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

    def tick(self, telemetry_cache: dict, cmd_thread) -> str:
        """Called at 20Hz by the RecipeWorker. Must return RUNNING, DONE, or FAILED."""
        raise NotImplementedError()

    def add_pause_offset(self, duration: float):
        """Allows routines with internal timers to adjust for UI pauses."""
        pass

    def _send_cmd(self, cmd_thread, target: str, value: float):
        """Helper to route commands to the correct microservice."""
        if "spellman" in target or "einzel" in target or "neutral_trap" in target:
            subsystem = "spellman"
        elif "magnet" in target:
            subsystem = "magnet"
        else:
            subsystem = "plc"
        cmd_thread.send_command(subsystem, target, value)


class MassScanRoutine(BaseRoutine):
    """
    This routine sweeps the magnet through a range of values, collecting a mass-scan of beam current.
    It then analyses the mass-scan for peaks and attempts to then set the magnet on to the desired peak.
    Currently untested and probably highly unsuitable.
    """
    def __init__(self, parameters: dict):
        super().__init__(parameters)
        # Parameters extracted from JSON
        self.sp_target = self.parameters.get("sp_target", "ion_beam.beamline.magnet.sp_requested_mass")
        self.rb_target = self.parameters.get("rb_target", "ion_beam.beamline_diagnostics.rb_fc_current")
        self.start_val = float(self.parameters.get("start_val", 0.0))
        self.end_val = float(self.parameters.get("end_val", 100.0))
        self.step_size = float(self.parameters.get("step_size", 1.0))
        self.dwell_sec = float(self.parameters.get("dwell_sec", 0.5))

        # Internal state
        self.current_val = self.start_val
        self.timer_start = 0.0
        self.data_x = []
        self.data_y = []

    def tick(self, telemetry_cache: dict, cmd_thread) -> str:
        if self.state == "INIT":
            self.current_val = self.start_val
            self._send_cmd(cmd_thread, self.sp_target, self.current_val)
            self.timer_start = time.time()
            self.state = "WAIT_SETTLE"
            return "RUNNING"

        elif self.state == "WAIT_SETTLE":
            if time.time() - self.timer_start >= self.dwell_sec:
                self.state = "MEASURE"
            return "RUNNING"

        elif self.state == "MEASURE":
            # Record current data point
            measurement = float(telemetry_cache.get(self.rb_target, 0.0))
            self.data_x.append(self.current_val)
            self.data_y.append(measurement)

            # Check if scan is complete
            if self.current_val >= self.end_val:
                self.state = "ANALYZE"
            else:
                self.current_val += self.step_size
                # Clamp to end_val to prevent overshooting
                if self.current_val > self.end_val:
                    self.current_val = self.end_val

                self._send_cmd(cmd_thread, self.sp_target, self.current_val)
                self.timer_start = time.time()
                self.state = "WAIT_SETTLE"
            return "RUNNING"

        elif self.state == "ANALYZE":
            try:
                y_array = np.array(self.data_y)
                x_array = np.array(self.data_x)

                # Find peaks with minimum prominence to ignore noise
                # Adjust prominence threshold based on your typical FC noise floor
                peaks, properties = find_peaks(y_array, prominence=0.5)

                if len(peaks) > 0:
                    # Select the peak with the highest prominence
                    best_peak_idx = peaks[np.argmax(properties["prominences"])]
                    optimal_mass = x_array[best_peak_idx]

                    print(f"[ROUTINE] Mass Scan complete. Optimal mass found at: {optimal_mass}")
                    self._send_cmd(cmd_thread, self.sp_target, optimal_mass)
                else:
                    print("[ROUTINE] Mass Scan complete. No distinct peaks found. Reverting to start value.")
                    self._send_cmd(cmd_thread, self.sp_target, self.start_val)

                return "DONE"
            except Exception as e:
                print(f"[ROUTINE ERROR] Analysis failed: {e}")
                return "FAILED"

        return "RUNNING"

    def add_pause_offset(self, duration: float):
        if self.state == "WAIT_SETTLE":
            self.timer_start += duration


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
        if self.state == "INIT":
            if evaluate_condition(telemetry_cache.get(self.rb_relay), "==", 1.0):
                return "DONE"
            self.state = "CHECK_GV_STATE"
            return "RUNNING"

        elif self.state == "CHECK_GV_STATE":
            gv_is_open = evaluate_condition(telemetry_cache.get(self.gv_stat_open), "==", 1.0)

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
            permissive = evaluate_condition(telemetry_cache.get(self.gv_open_perm), "==", 1.0)
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
            if evaluate_condition(telemetry_cache.get(self.gv_stat_open), "==", 1.0):
                self.state = "CHECK_PERMISSIVES"
                return "RUNNING"
            if time.time() - self.timer_start > self.gv_timeout_sec:
                self.prompt_text = "Gate Valve failed to open automatically. Please verify vacuum levels manually, open the valve, then click OK."
                self.next_state = "CHECK_GV_STATE"
                self.state = "WAITING_ON_ACK"
                return "WAITING_FOR_USER"
            return "RUNNING"

        elif self.state == "CHECK_PERMISSIVES":
            gv_is_open = evaluate_condition(telemetry_cache.get(self.gv_stat_open), "==", 1.0)

            if not gv_is_open:
                self.prompt_text = "Gate Valve is not fully open. Please remedy this and click OK to re-evaluate."
                self.next_state = "CHECK_GV"
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
            if evaluate_condition(telemetry_cache.get(self.rb_relay), "==", 1.0):
                # Relay is active, start the stabilization countdown instead of finishing
                print("[ROUTINE] Relay active. Stabilizing communications for 15s...")
                self.timer_start = time.time()
                self.state = "WAIT_POST_RELAY_STABILIZATION"
                return "RUNNING"

            if time.time() - self.timer_start > self.timeout_sec:
                print("[ROUTINE] Safety Relay timeout: Physical button not pressed.")
                self._send_cmd(cmd_thread, self.cmd_virtual, 0.0)
                return "FAILED"
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

    def tick(self, telemetry_cache: dict, cmd_thread) -> str:
        # 1. Continuous Safety Checks (Priority Overrides)

        # Arc Detection
        # NOTE: Relying strictly on the PLC arc quench flag here. Calculating dI/dt and dV/dt
        # purely in software over a 20Hz polling loop is highly susceptible to aliasing and network jitter.
        # Please manually verify if a pure software threshold calculation is strictly required over the PLC flag.
        if self.arc_mode_tag and evaluate_condition(telemetry_cache.get(self.arc_mode_tag), "==", 1.0):
            if self.state != "ARC_RECOVERY":
                self.arc_count += 1
                if self.arc_count > self.max_arcs:
                    print(f"[HV_COND] Aborted: Exceeded max arcs ({self.max_arcs}).")
                    return "FAILED"

                self.current_sp = max(0.0, self.current_sp - self.backoff_step)
                self._send_cmd(cmd_thread, self.cmd_tag, self.current_sp)
                self.state = "ARC_RECOVERY"
                self.arc_cooldown_start = time.time()
                return "RUNNING"

        # Pressure Check
        if self.pressure_tag and self.state not in ["PAUSE_PRESSURE", "ARC_RECOVERY"]:
            press = telemetry_cache.get(self.pressure_tag)
            if press is not None and float(press) > self.pressure_max:
                self.state = "PAUSE_PRESSURE"
        #
        # 2. State Machine
        if self.state == "INIT":
            start_val = telemetry_cache.get(self.rb_tag)
            self.current_sp = float(start_val) if start_val is not None else 0.0
            self.state = "RAMP"
            return "RUNNING"

        elif self.state == "ARC_RECOVERY":
            if time.time() - self.arc_cooldown_start > self.arc_cooldown_sec:
                if not evaluate_condition(telemetry_cache.get(self.arc_mode_tag), "==", 1.0):
                    self.timer_start = time.time()
                    self.state = "HOLD"
            return "RUNNING"

        elif self.state == "PAUSE_PRESSURE":
            press = telemetry_cache.get(self.pressure_tag)
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
        if self.state == "INIT":
            # 1. Check Safety Relay if required
            if self.requires_relay:
                if not evaluate_condition(telemetry_cache.get(self.rb_relay), "==", 1.0):
                    print(f"[PSU] Aborted: Safety relay is not active for {self.sp_tag}")
                    return "FAILED"

            # 2. Check if already enabled
            if evaluate_condition(telemetry_cache.get(self.stat_enabled), "==", 1.0):
                self.state = "APPLY_SETPOINT"
            else:
                self._send_cmd(cmd_thread, self.cmd_enable, 1.0)
                self.timer_start = time.time()
                self.state = "WAIT_ENABLE"
            return "RUNNING"

        elif self.state == "WAIT_ENABLE":
            if evaluate_condition(telemetry_cache.get(self.stat_enabled), "==", 1.0):
                self.state = "APPLY_SETPOINT"
                return "RUNNING"

            if time.time() - self.timer_start > 5.0:  # Hardcoded 5s timeout just for the enable bit
                print(f"[PSU] Aborted: Hardware refused to enable {self.cmd_enable}")
                return "FAILED"
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
            current_rb = telemetry_cache.get(self.rb_tag)
            if current_rb is not None:
                # Check if readback is within +/- tolerance of target
                if abs(float(current_rb) - self.target_val) <= self.tolerance:
                    return "DONE"

            if time.time() - self.timer_start > self.timeout_sec:
                print(f"[PSU] Timeout: {self.rb_tag} failed to reach {self.target_val} within {self.timeout_sec}s")
                return "FAILED"
            return "RUNNING"

        return "RUNNING"

ROUTINE_REGISTRY = {
    "mass_scan": MassScanRoutine,
    "safety_relay_init": SafetyRelayRoutine,
    "hv_conditioning": HVConditioningRoutine,
    "psu_control": PSUControlRoutine
}