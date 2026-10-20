"""Recipe files and their steps.

A recipe is a JSON file in config/recipes: a name, a version, an optional fault policy (what stops
it) and a list of steps. The steps are small state machines: execute(telemetry, cmd) is called
repeatedly and returns the step's state (RUNNING, DONE, FAILED, WAITING_FOR_USER, or
CALL_SUB_RECIPE). They are run by the sequencer service (src/sequencer/recipe_job.py), which
gives them a snapshot of the live telemetry and its command port (commands carry the recipe's
control lease).

Everything that can be told from the file is checked when it loads, and a recipe that fails
raises RecipeError naming the step: a value that isn't a number, an unknown condition, step type
or routine, a routine parameter it can't use, a fault policy entry it can't evaluate. (A bad
value used to raise a bare ValueError when a sub-recipe was entered, which ended the sequencer.)
What needs the tag registry - tags, limits, the order of steps - is checked by
src/core/recipe_check.py, before a run starts and in the recipe builder.
"""
import hashlib
import json
import logging
import os
import time

from src.core.conditions import holds, number, problem as condition_problem
from src.core.routines import ROUTINE_REGISTRY, RoutineError, send, ticket_outcome
from src.core.telemetry_cache import fresh_value

log = logging.getLogger("recipe_engine")

STALE_FAIL_S = 10.0     # a step whose tag has had no live value for this long fails
NO_RESET_LEAVES = ("cmd_fault_reset", "cmd_reset")     # recipes never reset faults (the PLC's or a pump's)
MAX_DEPTH = 8           # sub-recipes within sub-recipes


def evaluate_condition(current_val, condition_str, target_val, tolerance=None) -> bool:
    """(Kept for older callers: src/core/conditions.py holds() is the one implementation.)"""
    return holds(current_val, condition_str, target_val, tolerance)


class RecipeError(ValueError):
    """A recipe that can't be run (bad JSON, unknown step type, a value that isn't a number ...)."""


def _where(step_data) -> str:
    sid = step_data.get("step_id", "?") if isinstance(step_data, dict) else "?"
    return f"step {sid}"


def _num(step_data: dict, key: str, default=None, minimum=None):
    """A number field of a step; RecipeError naming the step and the field if it isn't one."""
    raw = step_data.get(key, default)
    value = number(raw)
    if value is None:
        raise RecipeError(f"{_where(step_data)}: {key} must be a number, not {raw!r}")
    if minimum is not None and value < minimum:
        raise RecipeError(f"{_where(step_data)}: {key} can't be below {minimum:g} (it is {value:g})")
    return value


def _tag(step_data: dict, key: str, required: bool = True) -> str:
    raw = step_data.get(key, "")
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raise RecipeError(f"{_where(step_data)}: {key} must be a tag name, not {raw!r}")
    if required and not raw.strip():
        raise RecipeError(f"{_where(step_data)}: no {key} given")
    return raw.strip()


def _condition(step_data: dict, where: str, condition, value, tolerance):
    why = condition_problem(condition, value, tolerance)
    if why:
        raise RecipeError(f"{_where(step_data)}: {where}: {why}")


def _no_reset(step_data: dict, target: str):
    if target.rpartition(".")[2] in NO_RESET_LEAVES:
        raise RecipeError(f"{_where(step_data)}: recipes may not reset faults ({target}): press RESET FAULTS, "
                          "or RESET & START for the turbo pump, yourself")


def create_step(step_data: dict, in_parallel: bool = False):
    if not isinstance(step_data, dict):
        raise RecipeError(f"a step must be a JSON object, not {type(step_data).__name__}")
    stype = step_data.get("type")
    cls = STEP_TYPES.get(stype)
    if cls is None:
        raise RecipeError(f"{_where(step_data)}: unknown step type {stype!r} "
                          f"(expected one of {', '.join(sorted(STEP_TYPES))})")
    if in_parallel and cls is SubRecipeStep:
        raise RecipeError(f"{_where(step_data)}: a SUB_RECIPE can't run inside a PARALLEL step")
    try:
        return cls(step_data)
    except RecipeError:
        raise
    except (TypeError, ValueError, AttributeError) as e:      # whatever else a bad file holds
        raise RecipeError(f"{_where(step_data)} ({stype}): {e}") from e


class RecipeStep:
    def __init__(self, step_data: dict):
        self.data = step_data               # as in the file (the plain-language text is made from it)
        self.step_id = step_data.get("step_id", 0)
        self.type = step_data.get("type", "UNKNOWN")
        comment = step_data.get("comment", "")
        self.comment = str(comment) if comment not in (None, "") else "No description"
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
        """What the step does, in words (src/core/recipe_text.py; worked out once)."""
        if getattr(self, "_description", None) is None:
            try:
                from src.core.recipe_text import describe_step
                self._description = describe_step(self.data)
            except Exception:   # the text is a convenience: never let it stop a run
                log.exception("No description for step %s", self.step_id)
                self._description = f"{self.type} step"
        return self._description

    def acknowledge(self, telemetry_cache: dict) -> bool:
        """Called by the engine when the UI confirms a prompt. Returns True if successful."""
        return True


class ActionStep(RecipeStep):
    def __init__(self, step_data: dict):
        super().__init__(step_data)
        self.target = _tag(step_data, "target")
        _no_reset(step_data, self.target)
        if step_data.get("value") is None:
            raise RecipeError(f"{_where(step_data)}: no value to send to {self.target}")
        self.value = step_data.get("value")
        self.ticket = None

    def reset(self):
        super().reset()
        self.ticket = None

    def execute(self, telemetry_cache: dict, cmd_thread) -> str:
        if self.state == "IDLE":
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


class WaitTimeStep(RecipeStep):
    def __init__(self, step_data: dict):
        super().__init__(step_data)
        self.duration_sec = _num(step_data, "duration_sec", 0.0, minimum=0.0)
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


class WaitTelemetryStep(RecipeStep):
    def __init__(self, step_data: dict):
        super().__init__(step_data)
        self.target = _tag(step_data, "target")
        self.condition = step_data.get("condition", "==")
        _condition(step_data, "condition", self.condition, step_data.get("value", 0.0), step_data.get("tolerance"))
        self.target_value = _num(step_data, "value", 0.0)
        self.tolerance = number(step_data.get("tolerance"))
        self.timeout_sec = _num(step_data, "timeout_sec", 0.0, minimum=0.0)
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
                if holds(current_val, self.condition, self.target_value, self.tolerance):
                    self.state = "DONE"
                    return self.state

            if self.timeout_sec > 0 and (now - self.start_time) > self.timeout_sec:
                within = f" ± {self.tolerance:g}" if self.condition == "within" else ""
                return self.fail(f"timed out after {self.timeout_sec:g} s waiting for "
                                 f"{self.target} {self.condition} {self.target_value:g}{within} (last: {current_val})")

        return self.state

    def add_pause_offset(self, duration: float):
        if self.state == "RUNNING":
            self.start_time += duration
            if self.unknown_since is not None:
                self.unknown_since += duration


def _check_conditions(step_data: dict, conditions) -> list:
    """A prompt's verify conditions, checked (RecipeError) and with their numbers as numbers."""
    if conditions is None:
        return []
    if not isinstance(conditions, list):
        raise RecipeError(f"{_where(step_data)}: verify_conditions must be a list")
    out = []
    for i, cond in enumerate(conditions, 1):
        if not isinstance(cond, dict):
            raise RecipeError(f"{_where(step_data)}: verify condition {i} must be an object")
        tag = cond.get("target")
        if not isinstance(tag, str) or not tag.strip():
            raise RecipeError(f"{_where(step_data)}: verify condition {i} has no tag")
        op, value, tol = cond.get("condition", "=="), cond.get("value", 1.0), cond.get("tolerance")
        _condition(step_data, f"verify condition {i}", op, value, tol)
        out.append({"target": tag.strip(), "condition": op, "value": number(value), "tolerance": number(tol)})
    return out


class UserPromptStep(RecipeStep):
    def __init__(self, step_data: dict):
        super().__init__(step_data)
        self.prompt_text = str(step_data.get("prompt_text") or "Please acknowledge to continue.")
        # Conditions that must hold (live) before the operator's OK is accepted
        self.verify_conditions = _check_conditions(step_data, step_data.get("verify_conditions", []))

    def execute(self, telemetry_cache: dict, cmd_thread) -> str:
        if self.state == "IDLE":
            self.state = "WAITING_FOR_USER"
        return self.state

    def verify(self, telemetry_cache: dict) -> bool:
        """Checks if all physical conditions are met before allowing the OK."""
        return not self.unmet_conditions(telemetry_cache)

    def unmet_conditions(self, telemetry_cache) -> list:
        """The conditions not met now, as text ('waiting until the Faraday cup is in (<tag> = 0.0)');
        stale values never verify."""
        from src.core.recipe_text import condition_text
        unmet = []
        for cond in self.verify_conditions:
            tag = cond["target"]
            val = fresh_value(telemetry_cache, tag)
            if not holds(val, cond["condition"], cond["value"], cond["tolerance"]):
                shown = "no live value" if val is None else f"{val}"
                said = condition_text(tag, cond["condition"], cond["value"], cond["tolerance"])
                unmet.append(f"waiting until {said} ({tag} = {shown})")
        return unmet

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
        target = step_data.get("target_file")
        if not isinstance(target, str) or not target.strip():
            raise RecipeError(f"{_where(step_data)}: no recipe file to run")
        self.target_file = target.strip()

    def execute(self, telemetry_cache: dict, cmd_thread) -> str:
        if self.state == "IDLE":
            self.state = "RUNNING"
            return "CALL_SUB_RECIPE"
        return self.state


def parse_fault_policy(policy, where: str = "") -> list:
    """A recipe's fault policy (what stops it), checked: a list of
        {"type": "BIT_FAULT", "registry_tag", "bit_str", "name", "bit"}     a PLC fault bit latched
        {"type": "CONDITION", "target", "condition", "value", "tolerance", "message"}
                                                                            a live condition holding
    Entries without a type, as older files hold them, are read by their keys. Anything the
    sequencer couldn't evaluate is a RecipeError: it used to be skipped silently, so a recipe ran
    with no protection while its file said otherwise."""
    prefix = f"{where}: " if where else ""
    if policy is None or policy == {} or policy == []:
        return []
    if not isinstance(policy, list):
        raise RecipeError(f"{prefix}fault_policy must be a list, not {type(policy).__name__}")
    out = []
    for i, entry in enumerate(policy, 1):
        if not isinstance(entry, dict):
            raise RecipeError(f"{prefix}fault policy entry {i} must be an object")
        kind = entry.get("type")
        if kind is None:
            kind = "BIT_FAULT" if "registry_tag" in entry or "bit_str" in entry else \
                "CONDITION" if "target" in entry else None
        if kind == "BIT_FAULT":
            tag, bit_str = entry.get("registry_tag"), entry.get("bit_str")
            if not isinstance(tag, str) or not tag.strip():
                raise RecipeError(f"{prefix}fault policy entry {i} has no fault word (registry_tag)")
            try:
                byte, bit = (int(p) for p in str(bit_str).split("."))
                if not (0 <= byte <= 3 and 0 <= bit <= 7):
                    raise ValueError
            except (TypeError, ValueError):
                raise RecipeError(f"{prefix}fault policy entry {i}: bad bit {bit_str!r} "
                                  "(byte.bit, byte 0 to 3, bit 0 to 7)") from None
            out.append({"type": "BIT_FAULT", "registry_tag": tag.strip(), "bit_str": f"{byte}.{bit}",
                        "name": str(entry.get("name") or f"{tag} bit {byte}.{bit}"), "bit": (3 - byte) * 8 + bit})
        elif kind == "CONDITION":
            tag = entry.get("target")
            if not isinstance(tag, str) or not tag.strip():
                raise RecipeError(f"{prefix}fault policy entry {i} has no tag (target)")
            op, value, tol = entry.get("condition", "=="), entry.get("value"), entry.get("tolerance")
            why = condition_problem(op, value, tol)
            if why:
                raise RecipeError(f"{prefix}fault policy entry {i}: {why}")
            out.append({"type": "CONDITION", "target": tag.strip(), "condition": op, "value": number(value),
                        "tolerance": number(tol), "message": str(entry.get("message") or "")})
        else:
            raise RecipeError(f"{prefix}fault policy entry {i}: unknown type {entry.get('type')!r} "
                              "(BIT_FAULT or CONDITION)")
    return out


class Recipe:
    """A recipe file, checked and ready to run (Recipe.from_data for one not saved yet)."""

    def __init__(self, filepath: str, data: dict = None):
        self.filepath = filepath
        self.name = ""
        self.version = ""
        self.parameters = {}
        self.fault_policy = []
        self.steps = []
        self.sha256 = ""
        self.data = None
        if data is None:
            data = self._read()
        self._load(data)

    @classmethod
    def from_data(cls, data: dict, filepath: str = "<unsaved>") -> "Recipe":
        return cls(filepath, data=data)

    def _read(self) -> dict:
        try:
            with open(self.filepath, "rb") as f:
                raw = f.read()
        except OSError:
            raise
        self.sha256 = hashlib.sha256(raw).hexdigest()
        try:
            return json.loads(raw.decode("utf-8-sig"))
        except UnicodeDecodeError:
            try:
                return json.loads(raw.decode("cp1252"))
            except (UnicodeDecodeError, ValueError) as e:
                raise RecipeError(f"{os.path.basename(self.filepath)} is not valid JSON: {e}") from e
        except ValueError as e:
            raise RecipeError(f"{os.path.basename(self.filepath)} is not valid JSON: {e}") from e

    def _load(self, data):
        where = os.path.basename(self.filepath)
        if not isinstance(data, dict):
            raise RecipeError(f"{where}: a recipe must be a JSON object")
        self.data = data
        self.name = str(data.get("recipe_name") or "Unknown Recipe")
        self.version = str(data.get("version", "1.0"))
        self.parameters = data.get("parameters", {})
        self.fault_policy = parse_fault_policy(data.get("fault_policy"), where)
        steps = data.get("steps", [])
        if not isinstance(steps, list):
            raise RecipeError(f"{where}: steps must be a list")
        # Unknown step types used to be skipped silently, so a recipe could run with steps missing
        try:
            self.steps = [create_step(step_data) for step_data in steps]
        except RecipeError as e:
            raise RecipeError(f"{where}: {e}") from None
        # The HMI follows a run by step number and a prompt is told apart from the next by it
        ids = [step.step_id for step in self.steps]
        repeated = sorted({str(i) for i in ids if ids.count(i) > 1})
        if repeated:
            raise RecipeError(f"{where}: step number(s) {', '.join(repeated)} used more than once - open the "
                              "recipe in the recipe builder and save it to renumber the steps")


def resolve_sub_recipe(caller_path: str, target: str, recipes_dir: str) -> str:
    """The file a SUB_RECIPE step runs: its target_file in the folder of the recipe that calls it
    (the recipes folder for a recipe not saved yet), which must be inside the recipes folder.
    RecipeError if it isn't, or doesn't exist."""
    root = os.path.realpath(recipes_dir)
    saved = bool(caller_path) and not str(caller_path).startswith("<")
    base = os.path.dirname(os.path.realpath(caller_path)) if saved else root
    path = os.path.realpath(os.path.join(base, str(target)))
    if os.path.commonpath([root, path]) != root:
        raise RecipeError(f"{target} isn't in the recipes folder")
    if not os.path.isfile(path):
        raise RecipeError(f"there is no recipe {target} in the recipes folder")
    return path


def load_tree(root: "Recipe", recipes_dir: str) -> dict:
    """Every sub-recipe `root` runs, however deep, loaded and checked before anything runs:
    {path: Recipe}. RecipeError naming the step if one is missing, can't be loaded, calls a
    recipe that called it (it would never end), or is nested more than MAX_DEPTH deep. (These
    used to fail when the step was reached, half way through a run.)"""
    tree = {}
    root_path = os.path.realpath(root.filepath) if not str(root.filepath).startswith("<") else str(root.filepath)

    def visit(recipe, chain):
        for step in recipe.steps:
            if not isinstance(step, SubRecipeStep):
                continue            # (a PARALLEL step can't hold one)
            where = f"{os.path.basename(recipe.filepath)} step {step.step_id}"
            try:
                path = resolve_sub_recipe(recipe.filepath, step.target_file, recipes_dir)
            except RecipeError as e:
                raise RecipeError(f"{where}: {e}") from None
            if path in chain:
                circle = " > ".join(os.path.basename(p) for p in chain + (path,))
                raise RecipeError(f"{where}: the recipes call each other in a circle ({circle}), so it would "
                                  "never end")
            if len(chain) > MAX_DEPTH:
                raise RecipeError(f"{where}: sub-recipes nested more than {MAX_DEPTH} deep")
            if path not in tree:
                try:
                    tree[path] = Recipe(path)
                except OSError as e:
                    raise RecipeError(f"{where}: {step.target_file} can't be read: {e}") from None
                except RecipeError as e:          # (its message starts with the file's name)
                    raise RecipeError(f"{where}: {e}") from None
            visit(tree[path], chain + (path,))

    visit(root, (root_path,))
    return tree


class RoutineCallStep(RecipeStep):
    def __init__(self, step_data: dict):
        super().__init__(step_data)
        self.routine_name = step_data.get("routine_name", "")
        routine_class = ROUTINE_REGISTRY.get(self.routine_name)
        if routine_class is None:
            raise RecipeError(f"{_where(step_data)}: unknown routine {self.routine_name!r} "
                              f"(one of {', '.join(sorted(ROUTINE_REGISTRY))})")
        parameters = step_data.get("parameters", {})
        if parameters is None:
            parameters = {}
        if not isinstance(parameters, dict):
            raise RecipeError(f"{_where(step_data)}: the routine's parameters must be an object")
        errors = routine_class.validate(parameters)
        if errors:
            raise RecipeError(f"{_where(step_data)} ({self.routine_name}): {'; '.join(errors)}")
        self.parameters = parameters
        self.routine_instance = None
        self.prompt_text = ""

    def reset(self):
        super().reset()
        self.routine_instance = None

    def execute(self, telemetry_cache: dict, cmd_thread) -> str:
        if self.state == "IDLE":
            self.state = "RUNNING"
            try:
                self.routine_instance = ROUTINE_REGISTRY[self.routine_name](self.parameters)
            except (RoutineError, TypeError, ValueError) as e:
                return self.fail(f"routine {self.routine_name}: {e}")

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
        children = step_data.get("steps", [])
        if not isinstance(children, list):
            raise RecipeError(f"{_where(step_data)}: a PARALLEL step's steps must be a list")
        self.sub_steps = [create_step(s, in_parallel=True) for s in children]

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
        return f"[{self.step_id}] {self.comment}" if self.comment != "No description" else \
            f"[{self.step_id}] {len(self.sub_steps)} steps at the same time"


class VerifiedActionStep(RecipeStep):
    """Check a condition (optional), send a command, then wait for a condition to hold."""

    def __init__(self, step_data: dict):
        super().__init__(step_data)
        self.target = _tag(step_data, "target")
        _no_reset(step_data, self.target)
        if step_data.get("value") is None:
            raise RecipeError(f"{_where(step_data)}: no value to send to {self.target}")
        self.value = step_data.get("value")

        self.pre_target = _tag(step_data, "pre_target", required=False)
        self.pre_condition = step_data.get("pre_condition", "==")
        self.pre_value = step_data.get("pre_value", 1.0)
        self.pre_tolerance = number(step_data.get("pre_tolerance"))
        if self.pre_target:
            _condition(step_data, "pre-condition", self.pre_condition, self.pre_value, self.pre_tolerance)
            self.pre_value = number(self.pre_value)

        # An empty verify tag or value means the default (the command's): the builder used to write
        # "" for the tag, which made every new step time out
        self.verify_target = _tag(step_data, "verify_target", required=False) or self.target
        self.verify_condition = step_data.get("verify_condition", "==")
        verify_value = step_data.get("verify_value")
        self.verify_value = self.value if verify_value in (None, "") else verify_value
        self.verify_tolerance = number(step_data.get("verify_tolerance"))
        _condition(step_data, "check", self.verify_condition, self.verify_value, self.verify_tolerance)
        self.verify_value = number(self.verify_value)
        self.timeout_sec = _num(step_data, "timeout_sec", 5.0, minimum=0.0)

        self.timer_start = 0.0
        self.internal_state = "CHECK_PRE"
        self.ticket = None

    def reset(self):
        super().reset()
        self.internal_state = "CHECK_PRE"
        self.ticket = None

    def execute(self, telemetry_cache: dict, cmd_thread) -> str:
        if self.state == "IDLE":
            self.state = "RUNNING"

        if self.internal_state == "CHECK_PRE":
            if self.pre_target:
                val = fresh_value(telemetry_cache, self.pre_target)
                if val is None:
                    return self.fail(f"prerequisite {self.pre_target} has no live value")
                if not holds(val, self.pre_condition, self.pre_value, self.pre_tolerance):
                    return self.fail(f"prerequisite not met: {self.pre_target} = {val} "
                                     f"(needs {self.pre_condition} {self.pre_value:g})")
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
            if holds(val, self.verify_condition, self.verify_value, self.verify_tolerance):
                self.state = "DONE"
                return self.state

            if time.time() - self.timer_start > self.timeout_sec:
                within = f" ± {self.verify_tolerance:g}" if self.verify_condition == "within" else ""
                return self.fail(f"{self.verify_target} did not reach {self.verify_condition} "
                                 f"{self.verify_value:g}{within} within {self.timeout_sec:g} s "
                                 f"(last live value: {val})")

        return self.state

    def add_pause_offset(self, duration: float):
        if self.internal_state in ("VERIFY", "WAIT_ACK"):
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
