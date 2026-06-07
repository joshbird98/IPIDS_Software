import time
import numpy as np
from scipy.signal import find_peaks


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


# Registry maps JSON string names to class references
ROUTINE_REGISTRY = {
    "mass_scan": MassScanRoutine
}