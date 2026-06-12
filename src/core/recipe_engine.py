import time
import json
import os
from PyQt6.QtCore import QThread, pyqtSignal
from src.core.routines import ROUTINE_REGISTRY

def evaluate_condition(current_val, condition_str, target_val) -> bool:
    """Safely evaluates a string condition against a float value."""
    if current_val is None:
        return False

    current_val = float(current_val)
    target_val = float(target_val)

    if condition_str == "==" and abs(current_val - target_val) < 1e-6:
        return True
    elif condition_str == ">" and current_val > target_val:
        return True
    elif condition_str == "<" and current_val < target_val:
        return True
    elif condition_str == ">=" and current_val >= target_val:
        return True
    elif condition_str == "<=" and current_val <= target_val:
        return True
    return False

def create_step(step_data: dict):
    stype = step_data.get("type")
    if stype == "ACTION": return ActionStep(step_data)
    elif stype == "WAIT_TIME": return WaitTimeStep(step_data)
    elif stype == "WAIT_TELEMETRY": return WaitTelemetryStep(step_data)
    elif stype == "USER_PROMPT": return UserPromptStep(step_data)
    elif stype == "SUB_RECIPE": return SubRecipeStep(step_data)
    elif stype == "ROUTINE_CALL": return RoutineCallStep(step_data)
    elif stype == "VERIFIED_ACTION": return VerifiedActionStep(step_data)
    elif stype == "PARALLEL": return ParallelStep(step_data)
    return RecipeStep(step_data) # Fallback



class RecipeStep:
    def __init__(self, step_data: dict):
        self.step_id = step_data.get("step_id", 0)
        self.type = step_data.get("type", "UNKNOWN")
        self.comment = step_data.get("comment", "No description")
        self.state = "IDLE"  # IDLE, RUNNING, DONE, FAILED

    def execute(self, telemetry_cache: dict, cmd_thread) -> str:
        raise NotImplementedError()

    def reset(self):
        """Base reset clears the state flag."""
        self.state = "IDLE"

    def add_pause_offset(self, duration: float):
        pass

    def get_display_name(self) -> str:
        return f"[{self.step_id}] {self.comment}"

    def get_description(self) -> str:
        return f"{self.type}: Unknown Target"

    def acknowledge(self, telemetry_cache: dict) -> bool:
        """Called by the engine when the UI confirms a prompt. Returns True if successful."""
        return True

class ActionStep(RecipeStep):
    def __init__(self, step_data: dict):
        super().__init__(step_data)
        self.target = step_data.get("target", "")
        self.value = step_data.get("value")

    def execute(self, telemetry_cache: dict, cmd_thread) -> str:
        if self.state == "IDLE":
            if "cmd_fault_reset" in self.target:
                print(f"[SECURITY] Step {self.step_id} aborted: Automated fault resets are strictly prohibited.")
                self.state = "FAILED"
                return self.state

            self.state = "RUNNING"

            if "spellman" in self.target or "einzel" in self.target or "neutral_trap" in self.target:
                subsystem = "spellman"
            elif "magnet" in self.target:
                subsystem = "magnet"
            else:
                subsystem = "plc"

            cmd_thread.send_command(subsystem, self.target, self.value)
            self.state = "DONE"
        return self.state

    def get_description(self) -> str:
        short_target = self.target.split('.')[-1] if '.' in self.target else self.target
        return f"ACTION: Set '{short_target}' to {self.value}"

class WaitTimeStep(RecipeStep):
    def __init__(self, step_data: dict):
        super().__init__(step_data)
        self.duration_sec = float(step_data.get("duration_sec", 0.0))
        self.start_time = 0.0

    def reset(self):
        """Ensure the timer stopwatch is zeroed out on restart."""
        super().reset()
        self.start_time = 0.0

    def execute(self, telemetry_cache: dict, cmd_thread) -> str:
        if self.state == "IDLE":
            self.start_time = time.time()
            self.state = "RUNNING"

        if self.state == "RUNNING":
            if time.time() - self.start_time >= self.duration_sec:
                self.state = "DONE"

        return self.state

    def add_pause_offset(self, duration: float):
        if self.state == "RUNNING":
            self.start_time += duration

    def get_description(self) -> str:
        return f"WAIT: {self.duration_sec}s"

class WaitTelemetryStep(RecipeStep):
    def __init__(self, step_data: dict):
        super().__init__(step_data)
        self.target = step_data.get("target", "")
        self.condition = step_data.get("condition", "==")
        self.target_value = float(step_data.get("value", 0.0))
        self.timeout_sec = float(step_data.get("timeout_sec", 0.0))
        self.start_time = 0.0

    def reset(self):
        """Ensure the timer stopwatch is zeroed out on restart."""
        super().reset()
        self.start_time = 0.0

    def execute(self, telemetry_cache: dict, cmd_thread) -> str:
        if self.state == "IDLE":
            self.start_time = time.time()
            self.state = "RUNNING"

        if self.state == "RUNNING":
            current_val = telemetry_cache.get(self.target)

            # USE THE NEW HELPER
            if evaluate_condition(current_val, self.condition, self.target_value):
                self.state = "DONE"
                return self.state

            if self.timeout_sec > 0 and (time.time() - self.start_time) > self.timeout_sec:
                self.state = "FAILED"

        return self.state

    def add_pause_offset(self, duration: float):
        if self.state == "RUNNING":
            self.start_time += duration

    def get_description(self) -> str:
        short_target = self.target.split('.')[-1] if '.' in self.target else self.target
        timeout_str = f" (Timeout: {self.timeout_sec}s)" if self.timeout_sec > 0 else " (No Timeout)"
        return f"WAIT: Until '{short_target}' {self.condition} {self.target_value}{timeout_str}"

class UserPromptStep(RecipeStep):
    def __init__(self, step_data: dict):
        super().__init__(step_data)
        self.prompt_text = step_data.get("prompt_text", "Please acknowledge to continue.")
        # Now accepts a list of condition dictionaries
        self.verify_conditions = step_data.get("verify_conditions", [])
        self.has_emitted = False

    def reset(self):
        super().reset()
        self.has_emitted = False

    def execute(self, telemetry_cache: dict, cmd_thread) -> str:
        if self.state == "IDLE":
            self.state = "WAITING_FOR_USER"
            self.has_emitted = False
        return self.state

    def verify(self, telemetry_cache: dict) -> bool:
        """Checks if all physical conditions are met before allowing the OK."""
        if not self.verify_conditions:
            return True

        for cond in self.verify_conditions:
            val = telemetry_cache.get(cond.get("target"))
            # Reusing the evaluate_condition helper from earlier
            if not evaluate_condition(val, cond.get("condition", "=="), cond.get("value", 1.0)):
                return False
        return True

    def get_description(self) -> str:
        return f"PROMPT: {self.prompt_text}"

    def acknowledge(self, telemetry_cache: dict) -> bool:
        if self.state == "WAITING_FOR_USER":
            if self.verify(telemetry_cache):
                self.state = "DONE"
                return True
            return False
        return True

class SubRecipeStep(RecipeStep):
    def __init__(self, step_data: dict):
        super().__init__(step_data)
        self.target_file = step_data.get("target_file", "")

    def execute(self, telemetry_cache: dict, cmd_thread) -> str:
        if self.state == "IDLE":
            self.state = "RUNNING"
            return "CALL_SUB_RECIPE"
        return self.state

    def get_description(self) -> str:
        return f"CALL SUB-RECIPE: {self.target_file}"

class Recipe:
    def __init__(self, filepath: str):
        self.filepath = filepath
        self.name = ""
        self.version = ""
        self.parameters = {}
        self.fault_policy = {}
        self.steps = []
        self._load()

    def _load(self):
        with open(self.filepath, 'r') as f:
            data = json.load(f)

        self.name = data.get("recipe_name", "Unknown Recipe")
        self.version = data.get("version", "1.0")
        self.parameters = data.get("parameters", {})
        self.fault_policy = data.get("fault_policy", {})

        for step_data in data.get("steps", []):
            stype = step_data.get("type")
            if stype == "ACTION":
                self.steps.append(ActionStep(step_data))
            elif stype == "WAIT_TIME":
                self.steps.append(WaitTimeStep(step_data))
            elif stype == "WAIT_TELEMETRY":
                self.steps.append(WaitTelemetryStep(step_data))
            elif stype == "USER_PROMPT":
                self.steps.append(UserPromptStep(step_data))
            elif stype == "SUB_RECIPE":
                self.steps.append(SubRecipeStep(step_data))
            elif stype == "ROUTINE_CALL":
                self.steps.append(RoutineCallStep(step_data))
            elif stype == "PARALLEL":
                self.steps.append(ParallelStep(step_data))
            elif stype == "VERIFIED_ACTION":
                self.steps.append(VerifiedActionStep(step_data))

class RecipeWorker(QThread):
    sig_status_update = pyqtSignal(str)
    sig_step_started = pyqtSignal(int, str)
    sig_step_completed = pyqtSignal(int)
    sig_recipe_finished = pyqtSignal(bool, str)
    sig_user_prompt = pyqtSignal(int, str)
    sig_sequence_changed = pyqtSignal(str)

    def __init__(self, cmd_thread, get_telemetry_cb):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.get_telemetry_cb = get_telemetry_cb

        self.root_recipe = None  # The absolute parent
        self.active_recipe = None  # The currently executing recipe
        self.current_step_idx = 0
        self.call_stack = []  # Holds tuples of (parent_recipe, parent_step_idx)

        self.running = False
        self.paused = False
        self.abort_requested = False
        self.pause_start_time = 0.0

    def acknowledge_prompt(self):
        """Called by UI. Returns True if successful, False if conditions failed."""
        if self.active_recipe and self.current_step_idx < len(self.active_recipe.steps):
            step = self.active_recipe.steps[self.current_step_idx]

            # Delegate the logic directly to the step (works for USER_PROMPT, ROUTINE, or PARALLEL!)
            return step.acknowledge(self.get_telemetry_cb())

        return True

    def _get_breadcrumb(self):
        """Builds 'Master Recipe > Sub Recipe' string for the UI"""
        names = [r[0].name for r in self.call_stack]
        names.append(self.active_recipe.name)
        return " > ".join(names)

    def load_recipe(self, filepath: str):
        self.root_recipe = Recipe(filepath)
        self.active_recipe = self.root_recipe
        self.current_step_idx = 0
        self.call_stack.clear()

    def play(self):
        # 1. RESTART INTERCEPT: If the UI forced us to index 0, this is a fresh run.
        if self.current_step_idx == 0 and not self.call_stack:
            self.paused = False # Drop the pause flag so we don't apply an offset!
            self.active_recipe = self.root_recipe
            for step in self.active_recipe.steps:
                step.reset()
            self.sig_sequence_changed.emit(self.active_recipe.name)

        # 2. RESUME MATH: Only applies if we were actually paused mid-sequence
        if self.paused:
            pause_duration = time.time() - self.pause_start_time
            if self.active_recipe and self.current_step_idx < len(self.active_recipe.steps):
                self.active_recipe.steps[self.current_step_idx].add_pause_offset(pause_duration)

        # 3. GO LIVENESS
        self.paused = False
        self.running = True
        self.abort_requested = False
        self.sig_status_update.emit("RUNNING")

        if not self.isRunning():
            self.start()

    def pause(self):
        if not self.paused:
            self.paused = True
            self.pause_start_time = time.time()
            self.sig_status_update.emit("PAUSED")

    def abort(self):
        self.abort_requested = True
        self.sig_status_update.emit("ABORTING...")

    def run(self):
        if not self.active_recipe:
            self.sig_recipe_finished.emit(False, "No recipe loaded.")
            return

        self.sig_status_update.emit("RUNNING")

        while self.running:
            if self.abort_requested:
                self.running = False
                self.call_stack.clear()
                self._execute_safe_abort()
                self.sig_recipe_finished.emit(False, "Recipe aborted by user.")
                return

            if self.paused:
                time.sleep(0.1)
                continue

            # --- STACK POPPING LOGIC ---
            if self.current_step_idx >= len(self.active_recipe.steps):
                if self.call_stack:
                    # We finished a sub-recipe. Pop the parent back into focus.
                    parent_recipe, parent_idx = self.call_stack.pop()
                    self.active_recipe = parent_recipe
                    self.current_step_idx = parent_idx

                    # Mark the SUB_RECIPE step that called us as DONE
                    completed_step = self.active_recipe.steps[self.current_step_idx]
                    completed_step.state = "DONE"
                    self.sig_step_completed.emit(completed_step.step_id)
                    self.current_step_idx += 1

                    # Tell UI to redraw the parent list
                    self.sig_sequence_changed.emit(self._get_breadcrumb())
                    continue
                else:
                    # Root recipe finished
                    break

            telemetry = self.get_telemetry_cb()

            # --- EVALUATE FAULT POLICY ---
            # We must check BOTH the master root recipe and the local sub-recipe policies
            policies_to_check = []
            if isinstance(self.root_recipe.fault_policy, list):
                policies_to_check.extend(self.root_recipe.fault_policy)
            if self.active_recipe != self.root_recipe and isinstance(self.active_recipe.fault_policy, list):
                policies_to_check.extend(self.active_recipe.fault_policy)

            fault_triggered = False
            for fault in policies_to_check:

                # Handling our new nicely formatted PLC Bit Faults
                if fault.get("type") == "BIT_FAULT":
                    tag = fault.get("registry_tag")
                    word_val = telemetry.get(tag)

                    if word_val is not None:
                        try:
                            word_val = int(word_val)
                            bit_str = fault.get("bit_str", "0.0")
                            parts = bit_str.split('.')
                            x = int(parts[0])
                            y = int(parts[1])

                            # Your Siemens DWORD endianness math
                            bit_pos = (3 - x) * 8 + y

                            # If the specific fault bit is HIGH
                            if bool(word_val & (1 << bit_pos)):
                                self.running = False
                                self.call_stack.clear()
                                self._execute_safe_abort()
                                self.sig_recipe_finished.emit(False, f"FAULT ABORT: {fault.get('name')} tripped!")
                                fault_triggered = True
                                break
                        except (ValueError, IndexError):
                            pass

            if fault_triggered:
                return  # Exit the thread completely

            step = self.active_recipe.steps[self.current_step_idx]

            if step.state == "IDLE":
                self.sig_step_started.emit(step.step_id, f"Executing {step.type}")

            result = step.execute(telemetry, self.cmd_thread)

            # --- STACK PUSHING LOGIC ---
            if result == "CALL_SUB_RECIPE":
                # Push current state to stack
                self.call_stack.append((self.active_recipe, self.current_step_idx))

                # Load the new JSON relative to the current one
                base_dir = os.path.dirname(self.active_recipe.filepath)
                sub_path = os.path.join(base_dir, step.target_file)

                self.active_recipe = Recipe(sub_path)
                self.current_step_idx = 0

                # Tell UI to redraw with the sub-recipe list
                self.sig_sequence_changed.emit(self._get_breadcrumb())
                continue

            elif result == "WAITING_FOR_USER":
                if not getattr(step, 'has_emitted', False):
                    self.sig_user_prompt.emit(step.step_id, getattr(step, 'prompt_text', 'Action Required'))
                    step.has_emitted = True

            elif result == "DONE":
                self.sig_step_completed.emit(step.step_id)
                self.current_step_idx += 1

            elif result == "FAILED":
                self.running = False
                self.call_stack.clear()
                self._execute_safe_abort()
                self.sig_recipe_finished.emit(False, f"Step {step.step_id} failed/timed out.")
                return

            time.sleep(0.05)

        self.running = False
        self.sig_recipe_finished.emit(True, "Recipe completed successfully.")

    def _execute_safe_abort(self):
        self.sig_status_update.emit("REVERTING CONTROL MODES...")
        # (Future implementation: ZMQ commands to 0.0V here)


class RoutineCallStep(RecipeStep):
    def __init__(self, step_data: dict):
        super().__init__(step_data)
        self.routine_name = step_data.get("routine_name", "")
        self.parameters = step_data.get("parameters", {})
        self.routine_instance = None
        self.prompt_text = ""

    def reset(self):
        super().reset()
        self.routine_instance = None

    def execute(self, telemetry_cache: dict, cmd_thread) -> str:
        if self.state == "IDLE":
            self.state = "RUNNING"
            # Late binding of the routine to avoid circular imports
            from src.core.routines import ROUTINE_REGISTRY
            routine_class = ROUTINE_REGISTRY.get(self.routine_name)
            if routine_class:
                self.routine_instance = routine_class(self.parameters)
            else:
                print(f"[ENGINE] Unknown routine: {self.routine_name}")
                self.state = "FAILED"
                return self.state

        if self.routine_instance:
            res = self.routine_instance.tick(telemetry_cache, cmd_thread)

            # Catch the pause request from the routine!
            if res == "WAITING_FOR_USER":
                # Expose the routine's prompt text to the Engine
                self.prompt_text = getattr(self.routine_instance, "prompt_text", "Action Required")
                self.state = "WAITING_FOR_USER"
                return self.state

            self.state = res

        return self.state

    def add_pause_offset(self, duration: float):
        if self.routine_instance and hasattr(self.routine_instance, 'add_pause_offset'):
            self.routine_instance.add_pause_offset(duration)

    def get_description(self) -> str:
        return f"ROUTINE: {self.routine_name} {self.parameters}"

    def acknowledge(self, telemetry_cache: dict) -> bool:
        if self.state == "WAITING_FOR_USER":
            self.state = "RUNNING"
            self.has_emitted = False  # Reset emission flag for future prompts
            # Pass the acknowledgment down to the actual routine!
            if self.routine_instance and hasattr(self.routine_instance, 'acknowledge'):
                self.routine_instance.acknowledge()
        return True

class ParallelStep(RecipeStep):
    def __init__(self, step_data: dict):
        super().__init__(step_data)
        self.sub_steps = [self._instantiate_step(s) for s in step_data.get("steps", [])]

    def _instantiate_step(self, step_data):
        # Helper to map types to classes
        mapping = {"ACTION": ActionStep,
                   "ROUTINE_CALL": RoutineCallStep,
                   "WAIT_TIME": WaitTimeStep,
                   "WAIT_TELEMETRY": WaitTelemetryStep,
                   "USER_PROMPT": UserPromptStep,
                   "VERIFIED_ACTION": VerifiedActionStep,
                   "PARALLEL": ParallelStep}
        return mapping[step_data["type"]](step_data)

    def reset(self):
        super().reset()
        for step in self.sub_steps:
            step.reset()

    def execute(self, telemetry_cache: dict, cmd_thread) -> str:
        if self.state == "IDLE":
            self.state = "RUNNING"

        all_done = True
        any_failed = False

        for step in self.sub_steps:
            if step.state in ["DONE", "FAILED"]:
                if step.state == "FAILED": any_failed = True
                continue

            # Tick every non-finished sub-step
            res = step.execute(telemetry_cache, cmd_thread)

            if res != "DONE": all_done = False
            if res == "FAILED": any_failed = True

        if any_failed:
            self.state = "FAILED"
            return "FAILED"
        elif all_done:
            self.state = "DONE"
            return "DONE"

        return "RUNNING"

    def add_pause_offset(self, duration: float):
        for step in self.sub_steps:
            if hasattr(step, 'add_pause_offset'):
                step.add_pause_offset(duration)

    def acknowledge(self, telemetry_cache: dict) -> bool:
        success = True
        # Pass the click down to any sub-steps that are waiting
        for step in self.sub_steps:
            if step.state == "WAITING_FOR_USER":
                if not step.acknowledge(telemetry_cache):
                    success = False

        # Un-pause the parallel container if all sub-steps cleared
        if success and self.state == "WAITING_FOR_USER":
            self.state = "RUNNING"
            self.has_emitted = False
        return success

    def get_display_name(self):
        return f"🔀 PARALLEL ({len(self.sub_steps)} sub-steps)"

    def get_description(self):
        return " | ".join([s.comment for s in self.sub_steps])

class VerifiedActionStep(RecipeStep):
    def __init__(self, step_data: dict):
        super().__init__(step_data)
        self.target = step_data.get("target", "")
        self.value = step_data.get("value", 0.0)

        self.pre_target = step_data.get("pre_target", "")
        self.pre_condition = step_data.get("pre_condition", "==")
        self.pre_value = step_data.get("pre_value", 1.0)

        self.verify_target = step_data.get("verify_target", self.target)
        self.verify_condition = step_data.get("verify_condition", "==")
        self.verify_value = step_data.get("verify_value", self.value)
        self.timeout_sec = float(step_data.get("timeout_sec", 5.0))

        self.timer_start = 0.0
        self.internal_state = "CHECK_PRE"

    def reset(self):
        super().reset()
        self.internal_state = "CHECK_PRE"

    def execute(self, telemetry_cache: dict, cmd_thread) -> str:
        if self.state == "IDLE":
            if "cmd_fault_reset" in self.target:
                print(f"[SECURITY] Step {self.step_id} aborted: Automated fault resets are strictly prohibited.")
                self.state = "FAILED"
                return self.state
            self.state = "RUNNING"

        if self.internal_state == "CHECK_PRE":
            val = telemetry_cache.get(self.pre_target)
            if evaluate_condition(val, self.pre_condition, self.pre_value):
                self.internal_state = "FIRE"
            else:
                print(f"[VERIFIED_ACTION] Prerequisite failed on {self.pre_target}")
                self.state = "FAILED"
                return self.state

        if self.internal_state == "FIRE":
            # Direct routing logic as defined in earlier base steps
            subsystem = "plc" if "plc" in self.target else "spellman"  # Adjust routing logic as needed
            cmd_thread.send_command(subsystem, self.target, self.value)
            self.timer_start = time.time()
            self.internal_state = "VERIFY"

        if self.internal_state == "VERIFY":
            val = telemetry_cache.get(self.verify_target)
            if evaluate_condition(val, self.verify_condition, self.verify_value):
                self.state = "DONE"
                return self.state

            if time.time() - self.timer_start > self.timeout_sec:
                print(f"[VERIFIED_ACTION] Timeout verifying {self.verify_target}")
                self.state = "FAILED"

        return self.state

    def add_pause_offset(self, duration: float):
        if self.internal_state == "VERIFY":
            self.timer_start += duration