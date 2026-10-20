"""A recipe run by the sequencer (was the HMI's RecipeWorker thread).

Before anything runs the whole recipe is checked: it and every sub-recipe it runs are loaded
(src/core/recipe_engine.py load_tree) and checked against the tag registry
(src/core/recipe_check.py). A recipe with errors is refused, the first of them in the refusal,
so a run never stops half way at a step that could never work (a missing sub-recipe, a tag that
doesn't exist, a value outside its limits ...). The start is logged with the recipe's version
and the SHA-256 of each file, so the log says exactly what ran.

Each tick checks the stop conditions - the fault policies of the recipe and of every sub-recipe
running, PLC fault bits (BIT_FAULT) and live conditions (CONDITION) - then evaluates the current
step once (src/core/recipe_engine.py). Sub-recipes are entered and left in the same tick.
Operator prompts wait for a cmd_ack carrying the prompt's number, so an old click can't answer
a newer prompt; the step's conditions are checked when the answer arrives and a refusal says
which are not met.

Pause freezes the step timers. If the operator takes control of something the recipe drives,
the recipe pauses; resuming claims it again and starts the current step afresh (its command
is sent again). Stop leaves every output as the completed steps left it.
"""
import os

from src.core.conditions import holds, number
from src.core.leases import TAKEN
from src.core.recipe_catalog import Catalog
from src.core.recipe_check import check_recipe, errors, summary, warnings
from src.core.recipe_engine import MAX_DEPTH, Recipe, RecipeError, load_tree, resolve_sub_recipe
from src.core.recipe_files import RECIPES_DIR
from src.core.recipe_text import condition_text
from src.core.telemetry_cache import fresh_value
from src.sequencer.jobs import Job, leaf
from src.sequencer.protocol import COMPLETE, FAILED, FINAL, PAUSED, PROMPT, RECIPE, RUNNING, STOPPED, key

FAULT_STALE_S = 3.0     # the recipe stops if its stop conditions can't be checked for this long


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
        # Everything that can refuse the start comes first (RecipeError / OSError)
        self.recipes_dir = os.path.realpath(recipes_dir)
        self.root = Recipe(path)
        self.tree = load_tree(self.root, self.recipes_dir)
        self.catalog = getattr(ctx, "catalog", None) or Catalog.for_registry(ctx.registry)
        problems = check_recipe(self.root.data, self.catalog, self.recipes_dir, path, self.tree)
        if errors(problems):
            raise RecipeError(f"{os.path.basename(path)} can't run: {summary(errors(problems), 3)}")
        self.warnings = warnings(problems)
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
        self.event(f"Recipe '{self.root.name}' version {self.root.version} started ({self.file}, "
                   f"SHA-256 {self.root.sha256[:12]}){self._uses()}"
                   + (f" with {len(self.warnings)} warning(s) from the checks" if self.warnings else ""))
        for path_, recipe in self.tree.items():
            self.log.info("Run %d uses %s version %s, SHA-256 %s", run, os.path.relpath(path_, self.recipes_dir),
                          recipe.version, recipe.sha256)
        for problem in self.warnings:
            self.log.info("Run %d: warning: %s", run, problem.text())

    def _uses(self) -> str:
        if not self.tree:
            return ""
        subs = ", ".join(f"{os.path.relpath(p, self.recipes_dir)} version {r.version} (SHA-256 {r.sha256[:12]})"
                         for p, r in sorted(self.tree.items()))
        return f"; sub-recipes: {subs}"

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
        if self._stop_condition_met(snapshot):
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
        # Checked before the start (load_tree): what fails here is a bug, said as such
        try:
            if len(self.stack) >= MAX_DEPTH:
                raise RecipeError(f"sub-recipes nested more than {MAX_DEPTH} deep")
            path = resolve_sub_recipe(self.active_recipe.filepath, step.target_file, self.recipes_dir)
            loaded = self.tree.get(path)
            if loaded is None:
                raise RecipeError("it wasn't loaded and checked before the start")
            sub = Recipe(path, data=loaded.data)      # fresh steps: a sub-recipe can run more than once
        except RecipeError as e:
            self.finish(FAILED, f"Step {step.step_id} failed: sub-recipe {step.target_file}: {e}")
            return
        sub.sha256 = loaded.sha256
        self.stack.append((self.active_recipe, self.idx))
        self.active_recipe, self.idx = sub, 0
        self.status = f"Entering sub-recipe '{sub.name}'"
        self.event(f"Recipe '{self.root.name}' step {step.step_id}: sub-recipe '{sub.name}' version {sub.version} "
                   f"({os.path.relpath(path, self.recipes_dir)}) started")

    def _stop_conditions(self):
        """(recipe, entry) for the stop conditions in force: the recipe's own and those of every
        sub-recipe running (they used to be ignored below the first level)."""
        out, seen = [], set()
        for recipe in [r for r, _ in self.stack] + [self.active_recipe]:
            if id(recipe) not in seen:
                seen.add(id(recipe))
                out.extend((recipe, entry) for entry in recipe.fault_policy)
        return out

    def _stop_condition_met(self, snapshot) -> bool:
        unknown = []
        for recipe, entry in self._stop_conditions():
            whose = "" if recipe is self.root else f" (a stop condition of '{recipe.name}')"
            if entry["type"] == "BIT_FAULT":
                tag = entry["registry_tag"]
                word = number(fresh_value(snapshot, tag))
                if word is None:
                    unknown.append(tag)
                elif int(word) & (1 << entry["bit"]):
                    self.finish(FAILED, f"Stopped{self._at_step()}: fault {entry['name']} is active{whose}.")
                    return True
            else:
                tag = entry["target"]
                value = fresh_value(snapshot, tag)
                if value is None:
                    unknown.append(tag)
                elif holds(value, entry["condition"], entry["value"], entry["tolerance"]):
                    said = condition_text(tag, entry["condition"], entry["value"], entry["tolerance"], self.catalog)
                    why = f"{entry['message']} ({said})" if entry["message"] else said
                    self.finish(FAILED, f"Stopped{self._at_step()}: {why}{whose}.")
                    return True
        if not unknown:
            self._fault_unknown_since = None
            return False
        now = self.now()
        if self._fault_unknown_since is None:
            self._fault_unknown_since = now
        elif now - self._fault_unknown_since > FAULT_STALE_S:
            self.finish(FAILED, f"Stopped{self._at_step()}: the stop conditions can't be checked - no live value for "
                                f"{', '.join(sorted(set(unknown)))} for {FAULT_STALE_S:.0f} s.")
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
        d.update({key(k, "file"): self.file, key(k, "name"): self.root.name, key(k, "version"): self.root.version,
                  key(k, "breadcrumb"): self.breadcrumb(),
                  key(k, "steps"): [{"id": s.step_id, "name": s.get_display_name(), "desc": s.get_description(),
                                     "type": s.type, "state": s.state} for s in self.active_recipe.steps],
                  key(k, "step"): self.step.step_id if self.step is not None else None,
                  key(k, "prompt"): self.prompt if self.state == PROMPT else None})
        return d
