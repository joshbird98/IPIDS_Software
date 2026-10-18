"""Recipe files and their steps.

A recipe is a JSON file in config/recipes: a name, an optional fault policy and a list of steps.
The steps are small state machines: execute(telemetry, cmd) is called repeatedly and returns
the step's state (RUNNING, DONE, FAILED, WAITING_FOR_USER, or CALL_SUB_RECIPE). They are run by
the sequencer service (src/sequencer/recipe_job.py), which gives them a snapshot of the live
telemetry and its command port (commands carry the recipe's control lease).
"""
import json
import os
import logging
import time

from src.core.routines import send, ticket_outcome
from src.core.telemetry_cache import fresh_value

log = logging.getLogger("recipe_engine")

STALE_FAIL_S = 10.0     # a step whose tag has had no live value for this long fails


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

class RecipeError(ValueError):
    """A recipe file that can't be run (bad JSON, unknown step type, ...)."""


def create_step(step_data: dict, in_parallel: bool = False):
    if not isinstance(step_data, dict):
        raise RecipeError(f"a step must be a JSON object, not {type(step_data).__name__}")
    stype = step_data.get("type")
    cls = STEP_TYPES.get(stype)
    if cls is None:
        raise RecipeError(f"step {step_data.get('step_id', '?')}: unknown step type {stype!r} "
                          f"(expected one of {', '.join(sorted(STEP_TYPES))})")
    if in_parallel and cls is SubRecipeStep:
        raise RecipeError(f"step {step_data.get('step_id', '?')}: a SUB_RECIPE can't run inside a PARALLEL step")
    return cls(step_data)



class RecipeStep:
    def __init__(self, step_data: dict):
        self.step_id = step_data.get("step_id", 0)
        self.type = step_data.get("type", "UNKNOWN")
        self.comment = step_data.get("comment", "No description")
        self.state = "IDLE"  # IDLE, RUNNING, DONE, FAILED
        self.fail_reason = ""

    def execute(self, telemetry_cache: dict, cmd_thread) -> str:
        raise NotImplementedError()

    def fail(self, reason: str) -> str:
        self.state = "FAILED"
        self.fail_reason = reason
        log.warning("Step %s failed: %s", self.step_id, reason)
        return self.state

    def reset(self):
        """Base reset clears the state flag."""
        self.state = "IDLE"
        self.fail_reason = ""

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
        self.ticket = None

    def reset(self):
        super().reset()
        self.ticket = None

    def execute(self, telemetry_cache: dict, cmd_thread) -> str:
        if self.state == "IDLE":
            if "cmd_fault_reset" in self.target:
                return self.fail("automated fault resets are not allowed in recipes")
            self.state = "RUNNING"
            # Routed by the tag registry (the old name test sent e.g. steerer/SMU/turbo tags to the PLC)
            self.ticket = send(cmd_thread, self.target, self.value)

        if self.state == "RUNNING":
            # Done only once the service has confirmed it executed the command
            outcome = ticket_outcome(self.ticket)
            if outcome is not None:
                ok, reason = outcome
                if ok:
                    self.state = "DONE"
                else:
                    return self.fail(reason)
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
        self.unknown_since = None

    def reset(self):
        """Ensure the timer stopwatch is zeroed out on restart."""
        super().reset()
        self.start_time = 0.0
        self.unknown_since = None

    def execute(self, telemetry_cache: dict, cmd_thread) -> str:
        if self.state == "IDLE":
            self.start_time = time.time()
            self.state = "RUNNING"

        if self.state == "RUNNING":
            # Only a live value can satisfy the condition: a stale last value from a dead
            # service used to be able to complete the wait.
            current_val = fresh_value(telemetry_cache, self.target)
            now = time.time()
            if current_val is None:
                if self.unknown_since is None:
                    self.unknown_since = now
                elif now - self.unknown_since > STALE_FAIL_S:
                    return self.fail(f"no live value for {self.target} for {STALE_FAIL_S:.0f} s")
            else:
                self.unknown_since = None
                if evaluate_condition(current_val, self.condition, self.target_value):
                    self.state = "DONE"
                    return self.state

            if self.timeout_sec > 0 and (now - self.start_time) > self.timeout_sec:
                return self.fail(f"timed out after {self.timeout_sec:g} s waiting for "
                                 f"{self.target} {self.condition} {self.target_value:g} (last: {current_val})")

        return self.state

    def add_pause_offset(self, duration: float):
        if self.state == "RUNNING":
            self.start_time += duration
            if self.unknown_since is not None:
                self.unknown_since += duration

    def get_description(self) -> str:
        short_target = self.target.split('.')[-1] if '.' in self.target else self.target
        timeout_str = f" (Timeout: {self.timeout_sec}s)" if self.timeout_sec > 0 else " (No Timeout)"
        return f"WAIT: Until '{short_target}' {self.condition} {self.target_value}{timeout_str}"

class UserPromptStep(RecipeStep):
    def __init__(self, step_data: dict):
        super().__init__(step_data)
        self.prompt_text = step_data.get("prompt_text", "Please acknowledge to continue.")
        # Conditions that must hold (live) before the operator's OK is accepted
        self.verify_conditions = step_data.get("verify_conditions", [])

    def execute(self, telemetry_cache: dict, cmd_thread) -> str:
        if self.state == "IDLE":
            self.state = "WAITING_FOR_USER"
        return self.state

    def verify(self, telemetry_cache: dict) -> bool:
        """Checks if all physical conditions are met before allowing the OK."""
        return not self.unmet_conditions(telemetry_cache)

    def unmet_conditions(self, telemetry_cache) -> list:
        """The conditions not met now, as text ('<tag> = 0.0, needs == 1'); stale values never verify."""
        unmet = []
        for cond in self.verify_conditions:
            tag = cond.get("target")
            val = fresh_value(telemetry_cache, tag)
            if not evaluate_condition(val, cond.get("condition", "=="), cond.get("value", 1.0)):
                shown = "no live value" if val is None else f"{val}"
                unmet.append(f"{tag} = {shown}, needs {cond.get('condition', '==')} {cond.get('value', 1.0)}")
        return unmet

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
        try:
            with open(self.filepath, 'r') as f:
                data = json.load(f)
        except ValueError as e:
            raise RecipeError(f"{self.filepath} is not valid JSON: {e}") from e
        if not isinstance(data, dict):
            raise RecipeError(f"{self.filepath}: a recipe must be a JSON object")

        self.name = data.get("recipe_name", "Unknown Recipe")
        self.version = data.get("version", "1.0")
        self.parameters = data.get("parameters", {})
        self.fault_policy = data.get("fault_policy", {})
        # Unknown step types used to be skipped silently, so a recipe could run with steps missing
        self.steps = [create_step(step_data) for step_data in data.get("steps", [])]
        # The HMI follows a run by step number and a prompt is told apart from the next by it
        ids = [step.step_id for step in self.steps]
        repeated = sorted({str(i) for i in ids if ids.count(i) > 1})
        if repeated:
            raise RecipeError(f"{os.path.basename(self.filepath)}: step number(s) {', '.join(repeated)} used more "
                              "than once - open the recipe in the Recipe Builder and save it to renumber the steps")

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
                return self.fail(f"unknown routine '{self.routine_name}'")

        if self.routine_instance:
            res = self.routine_instance.tick(telemetry_cache, cmd_thread)

            # Catch the pause request from the routine!
            if res == "WAITING_FOR_USER":
                # Expose the routine's prompt text to the Engine
                self.prompt_text = getattr(self.routine_instance, "prompt_text", "Action Required")
                self.state = "WAITING_FOR_USER"
                return self.state

            if res == "FAILED":
                return self.fail(getattr(self.routine_instance, "fail_reason", "") or f"routine {self.routine_name} failed")
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
            # Pass the acknowledgment down to the actual routine!
            if self.routine_instance and hasattr(self.routine_instance, 'acknowledge'):
                self.routine_instance.acknowledge()
        return True

class ParallelStep(RecipeStep):
    def __init__(self, step_data: dict):
        super().__init__(step_data)
        self.sub_steps = [create_step(s, in_parallel=True) for s in step_data.get("steps", [])]

    def reset(self):
        super().reset()
        for step in self.sub_steps:
            step.reset()

    def execute(self, telemetry_cache: dict, cmd_thread) -> str:
        if self.state == "IDLE":
            self.state = "RUNNING"

        # Tick every unfinished sub-step
        for step in self.sub_steps:
            if step.state not in ("DONE", "FAILED"):
                step.execute(telemetry_cache, cmd_thread)

        failed = [s for s in self.sub_steps if s.state == "FAILED"]
        if failed:
            return self.fail("; ".join(s.fail_reason for s in failed if s.fail_reason) or "a parallel sub-step failed")
        if all(s.state == "DONE" for s in self.sub_steps):
            self.state = "DONE"
            return "DONE"
        # A prompt inside a PARALLEL used to wait forever: the container never reported it
        waiting = [s for s in self.sub_steps if s.state == "WAITING_FOR_USER"]
        if waiting:
            self.prompt_text = " / ".join(getattr(s, "prompt_text", "") or "Action required" for s in waiting)
            self.state = "WAITING_FOR_USER"
            return self.state
        self.state = "RUNNING"
        return "RUNNING"

    def unmet_conditions(self, telemetry_cache) -> list:
        unmet = []
        for step in self.sub_steps:
            if step.state == "WAITING_FOR_USER" and hasattr(step, "unmet_conditions"):
                unmet.extend(step.unmet_conditions(telemetry_cache))
        return unmet

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
        self.ticket = None

    def reset(self):
        super().reset()
        self.internal_state = "CHECK_PRE"
        self.ticket = None

    def execute(self, telemetry_cache: dict, cmd_thread) -> str:
        if self.state == "IDLE":
            if "cmd_fault_reset" in self.target:
                return self.fail("automated fault resets are not allowed in recipes")
            self.state = "RUNNING"

        if self.internal_state == "CHECK_PRE":
            if self.pre_target:
                val = fresh_value(telemetry_cache, self.pre_target)
                if val is None:
                    return self.fail(f"prerequisite {self.pre_target} has no live value")
                if not evaluate_condition(val, self.pre_condition, self.pre_value):
                    return self.fail(f"prerequisite not met: {self.pre_target} = {val} "
                                     f"(needs {self.pre_condition} {self.pre_value})")
            self.internal_state = "FIRE"

        if self.internal_state == "FIRE":
            # Routed by the tag registry. (Was: "plc" if "plc" in the tag name else "spellman",
            # which sent ordinary PLC tags such as ...extraction.cmd_enable to the Spellman service.)
            self.ticket = send(cmd_thread, self.target, self.value)
            self.timer_start = time.time()
            self.internal_state = "WAIT_ACK"

        if self.internal_state == "WAIT_ACK":
            outcome = ticket_outcome(self.ticket)
            if outcome is None:
                return self.state
            ok, reason = outcome
            if not ok:
                return self.fail(reason)
            self.internal_state = "VERIFY"

        if self.internal_state == "VERIFY":
            val = fresh_value(telemetry_cache, self.verify_target)
            if evaluate_condition(val, self.verify_condition, self.verify_value):
                self.state = "DONE"
                return self.state

            if time.time() - self.timer_start > self.timeout_sec:
                return self.fail(f"{self.verify_target} did not reach {self.verify_condition} {self.verify_value} "
                                 f"within {self.timeout_sec:g} s (last live value: {val})")

        return self.state

    def add_pause_offset(self, duration: float):
        if self.internal_state == "VERIFY":
            self.timer_start += duration


STEP_TYPES = {
    "ACTION": ActionStep,
    "WAIT_TIME": WaitTimeStep,
    "WAIT_TELEMETRY": WaitTelemetryStep,
    "USER_PROMPT": UserPromptStep,
    "SUB_RECIPE": SubRecipeStep,
    "ROUTINE_CALL": RoutineCallStep,
    "VERIFIED_ACTION": VerifiedActionStep,
    "PARALLEL": ParallelStep,
}
