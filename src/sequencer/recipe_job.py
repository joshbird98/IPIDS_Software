"""A recipe run by the sequencer (was the HMI's RecipeWorker thread).

Each tick evaluates the current step once (src/core/recipe_engine.py), after checking the
recipe's fault policy. Sub-recipes are entered and left in the same tick. Operator prompts wait
for a cmd_ack carrying the prompt's number, so an old click can't answer a newer prompt; the
step's conditions are checked when the answer arrives and a refusal says which are not met.

Pause freezes the step timers. If the operator takes control of something the recipe drives,
the recipe pauses; resuming claims it again and starts the current step afresh (its command
is sent again). Stop leaves every output as the completed steps left it.
"""
import os

from src.core.leases import TAKEN
from src.core.recipe_engine import Recipe, RecipeError
from src.core.recipe_files import RECIPES_DIR
from src.core.telemetry_cache import fresh_value
from src.sequencer.jobs import Job, leaf
from src.sequencer.protocol import COMPLETE, FAILED, FINAL, PAUSED, PROMPT, RECIPE, RUNNING, STOPPED, key

FAULT_STALE_S = 3.0     # the recipe stops if its fault policy can't be checked for this long
MAX_DEPTH = 8           # sub-recipes within sub-recipes (a recipe that calls itself ends here)


def resolve_recipe(name: str, recipes_dir: str = RECIPES_DIR) -> str:
    """Path of a recipe file given by name, refusing anything outside the recipes folder."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("no recipe file given")
    root = os.path.realpath(recipes_dir)
    path = os.path.realpath(os.path.join(root, name))
    if os.path.commonpath([root, path]) != root:
        raise ValueError(f"{name} is not in the recipes folder")
    if not os.path.isfile(path):
        raise ValueError(f"no recipe {name} in {recipes_dir}")
    return path


class RecipeJob(Job):
    KIND = RECIPE

    def __init__(self, ctx, run, path: str, recipes_dir: str = RECIPES_DIR):
        self.recipes_dir = os.path.realpath(recipes_dir)
        self.root = Recipe(path)          # RecipeError / OSError: the start is refused
        super().__init__(ctx, run, f"recipe '{self.root.name}'")
        self.file = os.path.relpath(os.path.realpath(path), self.recipes_dir)
        self.active_recipe = self.root
        self.idx = 0
        self.stack = []                   # (parent recipe, index of its SUB_RECIPE step)
        self.prompt = None                # {"seq", "step", "text"} while waiting for the operator
        self._prompt_seq = 0
        self._paused_wall = None
        self._reset_on_resume = False
        self._fault_unknown_since = None
        for step in self.root.steps:
            step.reset()
        self.status = "running"
        self.event(f"Recipe '{self.root.name}' started ({self.file})")

    # ------------------------------------------------------------------ helpers
    @property
    def step(self):
        steps = self.active_recipe.steps
        return steps[self.idx] if self.idx < len(steps) else None

    def breadcrumb(self) -> str:
        return " > ".join([r.name for r, _ in self.stack] + [self.active_recipe.name])

    def _at_step(self) -> str:
        return f" at step {self.step.step_id}" if self.step is not None else ""

    # ------------------------------------------------------------------ the loop
    def tick(self):
        if self.state == PAUSED:
            return
        if not self._pop_finished_sub_recipes():
            return
        snapshot = self.ctx.telemetry.snapshot()
        if self._fault_policy_stops(snapshot):
            return
        step = self.step
        try:
            result = step.execute(snapshot, self.port)
        except Exception as e:   # a bug in a step or routine fails this recipe, not the sequencer
            self.log.exception("Step %s raised", step.step_id)
            result = step.fail(f"internal error in the {step.type} step: {type(e).__name__}: {e}")

        if result == "CALL_SUB_RECIPE":
            self._enter_sub_recipe(step)
        elif result == "WAITING_FOR_USER":
            if self.prompt is None or self.prompt["step"] != step.step_id:
                self._prompt_seq += 1
                self.prompt = {"seq": self._prompt_seq, "step": step.step_id,
                               "text": getattr(step, "prompt_text", "") or "Action required"}
                self.event(f"Recipe '{self.root.name}' step {step.step_id} waiting for the operator: "
                           f"{self.prompt['text']}")
            self.state = PROMPT
            self.status = f"Step {step.step_id}: waiting for the operator"
        elif result == "DONE":
            self.prompt = None
            self.state = RUNNING
            self.idx += 1
            self.status = f"Step {step.step_id} done"
        elif result == "FAILED":
            self.finish(FAILED, f"Step {step.step_id} failed: {step.fail_reason or 'failed/timed out'}")
        else:
            self.prompt = None
            self.state = RUNNING
            self.status = f"Step {step.step_id}: {step.get_description()}"

    def _pop_finished_sub_recipes(self) -> bool:
        """Leave finished sub-recipes; finish the job at the end of the root. False if finished."""
        while self.idx >= len(self.active_recipe.steps):
            if not self.stack:
                self.finish(COMPLETE, "Recipe completed successfully.")
                return False
            self.active_recipe, self.idx = self.stack.pop()
            self.active_recipe.steps[self.idx].state = "DONE"
            self.idx += 1
        return True

    def _enter_sub_recipe(self, step):
        if len(self.stack) >= MAX_DEPTH:
            self.finish(FAILED, f"Step {step.step_id} failed: sub-recipes nested more than {MAX_DEPTH} deep "
                                f"(does a recipe call itself?)")
            return
        base = os.path.dirname(self.active_recipe.filepath)
        path = os.path.realpath(os.path.join(base, step.target_file))
        if os.path.commonpath([self.recipes_dir, path]) != self.recipes_dir:
            self.finish(FAILED, f"Step {step.step_id} failed: sub-recipe {step.target_file} is not in the recipes folder")
            return
        try:
            sub = Recipe(path)
        except (OSError, RecipeError) as e:
            self.finish(FAILED, f"Step {step.step_id} failed: sub-recipe {step.target_file}: {e}")
            return
        self.stack.append((self.active_recipe, self.idx))
        self.active_recipe, self.idx = sub, 0
        self.status = f"Entering sub-recipe '{sub.name}'"

    def _fault_policy_stops(self, snapshot) -> bool:
        policies = []
        for recipe in ([self.root] if self.active_recipe is self.root else [self.root, self.active_recipe]):
            if isinstance(recipe.fault_policy, list):
                policies.extend(recipe.fault_policy)
        unknown = []
        for fault in policies:
            if fault.get("type") != "BIT_FAULT":
                continue
            tag = fault.get("registry_tag")
            word = fresh_value(snapshot, tag)
            if word is None:
                unknown.append(str(tag))
                continue
            try:
                x, y = (int(p) for p in fault.get("bit_str", "0.0").split("."))
                bit = (3 - x) * 8 + y          # Siemens DWORD byte order
                active = bool(int(word) & (1 << bit))
            except (TypeError, ValueError) as e:
                self.log.error("Bad BIT_FAULT entry %r in the fault policy: %s", fault, e)
                continue
            if active:
                self.finish(FAILED, f"Stopped: fault {fault.get('name')} is active (fault policy).")
                return True
        if not unknown:
            self._fault_unknown_since = None
            return False
        now = self.now()
        if self._fault_unknown_since is None:
            self._fault_unknown_since = now
        elif now - self._fault_unknown_since > FAULT_STALE_S:
            self.finish(FAILED, f"Stopped: fault status unknown for {FAULT_STALE_S:.0f} s "
                                f"(no live PLC data for {', '.join(sorted(set(unknown)))}).")
            return True
        return False

    # ------------------------------------------------------------------ leases
    def lease_lost(self, subsystem, reason):
        if self.state in FINAL:
            return
        if reason == TAKEN:
            self._pause(f"Paused: the operator took control of the {leaf(subsystem)}. Resume to carry on "
                        f"(the current step starts again), or stop.", reset_step=True)
        else:
            self.finish(FAILED, f"Stopped{self._at_step()}: could not keep control of the {leaf(subsystem)}: {reason}")

    # ------------------------------------------------------------------ commands
    def _pause(self, why: str, reset_step: bool = False):
        if self.state != PAUSED:
            self._paused_wall = self.ctx.wall()
            self.state = PAUSED
        self._reset_on_resume = self._reset_on_resume or reset_step
        self.status = why
        self.event(f"Recipe '{self.root.name}': {why}", severity="WARNING")

    def cmd_pause(self, value=None):
        if self.state in FINAL:
            return False, "the recipe is not running", None
        if self.state == PAUSED:
            return True, "already paused", None
        self._pause(f"Paused by operator{self._at_step()}")
        return True, "paused", None

    def cmd_resume(self, value=None):
        if self.state != PAUSED:
            return False, "the recipe is not paused", None
        step = self.step
        if self._reset_on_resume:
            if step is not None:
                step.reset()
            self.prompt = None
            self._reset_on_resume = False
        elif step is not None:
            step.add_pause_offset(self.ctx.wall() - self._paused_wall)
        self.keeper.forgive()
        self._fault_unknown_since = None
        self.state = RUNNING
        self.status = f"Resumed{self._at_step()}"
        self.event(f"Recipe '{self.root.name}' resumed")
        return True, "resumed", None

    def cmd_stop(self, value=None):
        if self.state in FINAL:
            return True, "not running", None
        self.finish(STOPPED, f"Stopped by operator{self._at_step()} - outputs left as they were.")
        return True, "stopped", None

    def abort(self, reason: str):
        if self.state not in FINAL:
            self.finish(STOPPED, f"{reason}{self._at_step()} - outputs left as they were.")

    def cmd_ack(self, value=None):
        seq = value.get("seq") if isinstance(value, dict) else value
        if self.state == PAUSED:
            return False, "the recipe is paused - resume it first", None
        if self.state != PROMPT or self.prompt is None:
            return False, "no prompt is waiting for an answer", None
        try:
            if seq is not None and int(seq) != self.prompt["seq"]:
                return False, "that prompt has already been answered", None
        except (TypeError, ValueError):
            return False, f"bad prompt number {seq!r}", None
        step = self.step
        snapshot = self.ctx.telemetry.snapshot()
        if step.acknowledge(snapshot):
            self.event(f"Recipe '{self.root.name}' step {step.step_id}: operator acknowledged")
            self.prompt = None
            self.state = RUNNING
            self.status = f"Step {step.step_id}: acknowledged"
            return True, "acknowledged", None
        unmet = step.unmet_conditions(snapshot) if hasattr(step, "unmet_conditions") else []
        return False, "not yet: " + ("; ".join(unmet) if unmet else "the step's conditions are not met"), None

    # ------------------------------------------------------------------ telemetry
    def telemetry(self) -> dict:
        d = super().telemetry()
        k = self.KIND
        d.update({key(k, "file"): self.file, key(k, "name"): self.root.name, key(k, "breadcrumb"): self.breadcrumb(),
                  key(k, "steps"): [{"id": s.step_id, "name": s.get_display_name(), "desc": s.get_description(),
                                     "type": s.type, "state": s.state} for s in self.active_recipe.steps],
                  key(k, "step"): self.step.step_id if self.step is not None else None,
                  key(k, "prompt"): self.prompt if self.state == PROMPT else None})
        return d
