"""Checking a recipe before it runs: in the recipe builder as it is edited, when the Recipe
Execution panel loads it, and in the sequencer before it starts it (a recipe with errors is
refused, so nothing is done before a step that could never work is reached).

    problems = check_recipe(data, catalog, recipes_dir, path)   # data: the recipe as in its file
    errors(problems)        # the recipe can't run
    warnings(problems)      # it can, but look at these

Errors: what the engine refuses when it loads (src/core/recipe_engine.py: values that aren't
numbers, unknown conditions, routines or step types, fault policies it can't evaluate), and what
would fail when the step is reached: a tag the machine doesn't know or a recipe can't set, a value
outside the tag's limits or a hard cap, a sub-recipe that is missing, has errors or calls back into
a recipe that called it, a fault reset, a stop of the source turbo pump.

Warnings: what runs but is probably not meant, mostly from the order of the steps - a setpoint
sent before its supply is switched on (the PLC zeroes it), an output that needs the safety relay
switched on with no step that makes sure of the relay, a wait with no time limit, an analogue
reading compared with '==', a mass scan before the magnet is on, a degauss with the Faraday cup
out, a source recipe that doesn't stop if the safety relay trips ...

The tag checks need the tag registry (Catalog): with an empty one (a test world, a registry that
couldn't be read) they are left out and only the engine's own checks are made.
"""
import json
import os
from dataclasses import dataclass
from typing import List, Optional

from src.core import hard_caps, lab_config, paths
from src.core.conditions import number
from src.core.config_store import read_text
from src.core.recipe_catalog import FAULT_RESET, SAFETY_ENABLE, SAFETY_RELAY, TURBO_ENABLE, Catalog, shared
from src.core.recipe_engine import (MAX_DEPTH, STEP_TYPES, RecipeError, create_step, parse_fault_policy,
                                    resolve_sub_recipe)
from src.core.routines import CUP_INSERTED, MAGNET_ENABLED, ROUTINE_REGISTRY, known_keys

ERROR, WARNING = "error", "warning"
PARALLEL_TYPES = ("ACTION", "VERIFIED_ACTION", "WAIT_TIME", "WAIT_TELEMETRY", "USER_PROMPT", "ROUTINE_CALL",
                  "PARALLEL")
# Outputs powered through the safety relay (or held off by their service while it is off)
RELAY_SUBSYSTEMS = ("ion_beam.source.extraction", "ion_beam.source.target", "ion_beam.source.filament",
                    "ion_beam.source.thermionic", "ion_beam.source.cesium", "ion_beam.source.einzel",
                    "ion_beam.beamline.steering", "ion_beam.beamline.magnet", "ion_beam.beamline.steerer_magnet",
                    "ion_beam.beamline.faraday.smu")
# The PLC zeroes the request of a supply that is off; the SMU refuses a bias before ENABLE BIAS
ZEROED_WHEN_OFF = ("ion_beam.source.extraction", "ion_beam.source.target", "ion_beam.source.filament",
                   "ion_beam.source.thermionic", "ion_beam.source.cesium", "ion_beam.beamline.steering",
                   "ion_beam.beamline.faraday.smu")
CESIUM = "ion_beam.source.cesium"
FILAMENT = "ion_beam.source.filament"
MAGNET = "ion_beam.beamline.magnet"
SMU = "ion_beam.beamline.faraday.smu"
CUP_CMD = "ion_beam.beamline.diagnostics.cmd_insert_fc"
GV_CMD = "ion_beam.source.chamber.cmd_open_gv"
AUTO_EMISSION = "ion_beam.source.thermionic.cmd_auto_emission"
VG1 = "ion_beam.source.vacuum_gauge_1.rb_pressure"
VG2 = "ion_beam.beamline.vacuum_gauge_2.rb_pressure"
SAFETY_TRIP_FAULT = "Safety_Relay_Tripped"
TURBO_STOP = ("the source turbo pump is never stopped from the software: trained staff stop it by hand, at the "
              "pump")
NO_RESET = "recipes may not reset faults: press RESET FAULTS (or RESET & START for the turbo pump) yourself"
VALUE_PARAMS = {"target_val": "the value to set", "max_val": "the top of the ramp"}   # routines' values for a tag


@dataclass(frozen=True)
class Problem:
    level: str              # ERROR or WARNING
    path: tuple             # (): the recipe; ("policy", i): stop condition i; (i,) step i; (i, j) its sub-step j
    message: str
    where: str = ""         # "step 3", "step 3.1", "stop condition 2", "" for the recipe as a whole

    def text(self) -> str:
        return f"{self.where}: {self.message}" if self.where else self.message


class _Checker:
    def __init__(self, catalog: Optional[Catalog], recipes_dir: str, stack: tuple, cache: dict, tree: dict):
        self.catalog = catalog if catalog is not None else shared()
        self.known = bool(self.catalog.registry)
        self.recipes_dir = os.path.realpath(recipes_dir or paths.RECIPES_DIR)
        self.stack = stack                  # the files from the root to this one ('<unsaved>' for a new recipe)
        self.cache = cache                  # sub-recipe path -> (data or None, its problems), checked once
        self.tree = tree or {}              # path -> Recipe already loaded (the sequencer's)
        self.steps = []                     # the recipe's steps (for the step numbers in the messages)
        self.problems: List[Problem] = []

    @property
    def file(self) -> str:
        return self.stack[-1]

    # ------------------------------------------------------------------ helpers
    def add(self, level, path, message):
        self.problems.append(Problem(level, tuple(path), message, self.where(tuple(path))))

    def where(self, path) -> str:
        """'step 3', 'step 20.1' ... by the step's own number in the file, as the Recipe Execution
        panel lists it (the builder numbers steps 1..n, so there it is the position too)."""
        if not path or path[0] == "policy":
            return _where(path)
        head = path[0]
        step = self.steps[head] if isinstance(head, int) and 0 <= head < len(self.steps) else None
        sid = step.get("step_id") if isinstance(step, dict) else None
        own = isinstance(sid, (int, str)) and not isinstance(sid, bool) and str(sid).strip() != ""
        return "step " + (str(sid).strip() if own else str(head + 1)) + "".join(f".{i + 1}" for i in path[1:])

    def label(self, tag) -> str:
        return self.catalog.label(tag) if self.known else str(tag)

    def thing(self, subsystem) -> str:
        """'extraction', 'analyser magnet' ... (the label of its output, less 'output')."""
        label = self.label(subsystem + ".cmd_enable")
        return label[:-len(" output")] if label.endswith(" output") else subsystem.rpartition(".")[2]

    def settable(self, path, tag, value=None, what="", check_value=True):
        """A tag a step sends a value to: known, settable, and the value right for it."""
        if not isinstance(tag, str) or not tag:
            self.add(ERROR, path, f"no tag {what or 'to set'}")
            return
        if tag.rpartition(".")[2] in ("cmd_fault_reset", "cmd_reset") or tag == FAULT_RESET:
            self.add(ERROR, path, NO_RESET)
            return
        if tag == TURBO_ENABLE and value is not None and number(value) == 0.0:
            self.add(ERROR, path, TURBO_STOP)
            return
        if not self.known:
            return
        t = self.catalog.tag(tag)
        if t is None:
            self.add(ERROR, path, f"{tag} isn't a tag the machine knows (it isn't in the tag registry)")
            return
        if not t.settable:
            self.add(ERROR, path, f"the {t.label} ({tag}) can't be set by a recipe: {t.not_settable}")
            return
        if check_value and value is not None:
            self.value_for(path, t, value)

    def value_for(self, path, t, value, what=""):
        """The value right for the tag t. what: which value, for a routine's ('the value to set')."""
        name = f"{what}: " if what else ""
        if t.is_switch:
            if isinstance(value, str) or number(value) not in (0.0, 1.0):
                self.add(ERROR, path, f"{name}the {t.label} is on or off (true or false), not {value!r}")
            return
        if not t.is_number:
            return
        x = number(value)
        if x is None or isinstance(value, (bool, str)):
            self.add(ERROR, path, f"{name}the {t.label} needs a number, not {value!r}")
            return
        cap = hard_caps.command_cap(t.name)
        if cap is not None and cap.breach(x):
            self.add(ERROR, path, f"{name}{cap.explain(f'the {t.label}', x)}")
            return
        if t.min_val is not None and t.max_val is not None and not t.min_val <= x <= t.max_val:
            unit = f" {t.unit}" if t.unit else ""
            self.add(ERROR, path, f"{name}{x:g}{unit} is outside the {t.label}'s range, {t.range_text()}")
            return
        if t.is_whole and x != int(x):
            self.add(ERROR, path, f"{name}the {t.label} is a whole number, not {x:g}")
            return
        if t.choices and not t.choice(x):
            codes = ", ".join(f"{v:g} = {m}" for v, m in t.choices)
            self.add(WARNING, path, f"{name}{x:g} isn't one of the {t.label}'s values ({codes})")

    def readable(self, path, tag, what=""):
        if not isinstance(tag, str) or not tag:
            self.add(ERROR, path, f"no tag {what or 'to read'}")
            return None
        if not self.known:
            return None
        t = self.catalog.tag(tag)
        if t is None:
            self.add(WARNING, path, f"{tag} isn't in the tag registry: if nothing reports it, the step fails after "
                                    "10 s without a live value")
        return t

    def comparison(self, path, tag, condition, value, what=""):
        t = self.readable(path, tag, what)
        if t is None:
            return
        x = number(value)
        if t.is_switch and condition in ("==", "!=") and x not in (0.0, 1.0):
            self.add(WARNING, path, f"the {t.label} is 1 or 0: it never equals {value!r}")
        elif t.is_number and condition in ("==", "!=") and t.datatype in ("REAL", "LREAL"):
            self.add(WARNING, path, f"an analogue reading (the {t.label}) is rarely exactly {value!r}: use "
                                    "'is within ±', 'is at least' or 'is at most'")

    def sub_recipe(self, caller_file, target):
        """(path, data) of the sub-recipe a SUB_RECIPE step in `caller_file` runs, loaded and checked
        once (data None if it can't be read). RecipeError if there is no such recipe."""
        full = resolve_sub_recipe(caller_file, target, self.recipes_dir)
        return full, self._load(full)[0]

    def _load(self, full):
        if full not in self.cache:
            self.cache[full] = (None, [])           # being checked (a circle is caught by the stack)
            try:
                if full in self.tree:
                    data = self.tree[full].data
                else:
                    data = json.loads(read_text(full)[0])
            except (OSError, ValueError) as e:
                self.cache[full] = (None, [Problem(ERROR, (), f"can't be read: {e}")])
            else:
                inner = _Checker(self.catalog, self.recipes_dir, self.stack + (full,), self.cache, self.tree)
                inner.recipe(data)
                self.cache[full] = (data, inner.problems)
        return self.cache[full]

    # ------------------------------------------------------------------ the recipe
    def recipe(self, data: dict):
        if not isinstance(data, dict):
            self.add(ERROR, (), "a recipe must be a JSON object")
            return
        if not str(data.get("recipe_name") or "").strip():
            self.add(ERROR, (), "the recipe has no name")
        try:
            policy = parse_fault_policy(data.get("fault_policy"))
        except RecipeError as e:
            self.add(ERROR, ("policy",), str(e))
            policy = []
        steps = data.get("steps", [])
        if not isinstance(steps, list):
            self.add(ERROR, (), "steps must be a list")
            return
        self.steps = steps
        if not steps:
            self.add(WARNING, (), "the recipe has no steps")
        ids = [s.get("step_id") for s in steps if isinstance(s, dict)]
        repeated = sorted({str(i) for i in ids if ids.count(i) > 1})
        if repeated:
            self.add(ERROR, (), f"step number(s) {', '.join(repeated)} used more than once (saving it in the "
                                "recipe builder renumbers the steps)")
        for i, step in enumerate(steps):
            self.step((i,), step, in_parallel=False)
        self.policy(policy)
        self.order(steps, policy)

    def policy(self, policy):
        if not self.known:
            return
        for i, entry in enumerate(policy):
            path = ("policy", i)
            if entry["type"] == "BIT_FAULT":
                if self.catalog.faults and self.catalog.fault(word_tag=entry["registry_tag"],
                                                              bit_str=entry["bit_str"]) is None:
                    self.add(WARNING, path, f"{entry['name']} ({entry['registry_tag']} bit {entry['bit_str']}) "
                                            "isn't a fault in fault_config.json")
            else:
                self.comparison(path, entry["target"], entry["condition"], entry["value"])

    def step(self, path, step, in_parallel):
        # The engine's own checks first: what it refuses when the recipe loads
        if isinstance(step, dict) and step.get("type") == "PARALLEL" and isinstance(step.get("steps"), list):
            try:
                create_step(dict(step, steps=[]), in_parallel=in_parallel)
            except RecipeError as e:
                self.add(ERROR, path, _plain(e))
                return
            if not step["steps"]:
                self.add(WARNING, path, "nothing to do at the same time: add steps to it")
            drives = {}                                   # subsystem -> the first sub-step that drives it
            for j, sub in enumerate(step["steps"]):
                sub_path = path + (j,)
                kind = sub.get("type") if isinstance(sub, dict) else None
                if isinstance(kind, str) and kind in STEP_TYPES and kind not in PARALLEL_TYPES:
                    self.add(ERROR, sub_path, f"a {sub.get('type')} step can't run inside a PARALLEL step")
                    continue
                self.step(sub_path, sub, in_parallel=True)
                for tag in _targets(sub):
                    subsystem = tag.rpartition(".")[0]
                    if drives.setdefault(subsystem, j) != j:
                        self.add(WARNING, sub_path, f"another step at the same time also drives the "
                                                    f"{self.thing(subsystem)}: which value wins depends on timing")
            return
        try:
            create_step(step, in_parallel=in_parallel)
        except RecipeError as e:
            self.add(ERROR, path, _plain(e))
            return
        check = getattr(self, f"step_{step.get('type', '').lower()}", None)
        if check is not None:
            check(path, step)

    def step_action(self, path, s):
        self.settable(path, s.get("target"), s.get("value"))
        if s.get("target") == SAFETY_ENABLE and number(s.get("value")) == 1.0:
            self.add(WARNING, path, "this only asks for the safety relay: use 'Bring the safety relay on', which "
                                    "checks the vacuum and the gate valve and works with the operator's START")

    def step_verified_action(self, path, s):
        self.step_action(path, s)
        if s.get("pre_target"):
            self.comparison(path, s.get("pre_target"), s.get("pre_condition", "=="), s.get("pre_value", 1.0),
                            "for the condition first")
        verify = s.get("verify_target") or s.get("target")
        value = s.get("verify_value")
        value = s.get("value") if value in (None, "") else value
        self.comparison(path, verify, s.get("verify_condition", "=="), value, "to check")
        leaf = verify.rpartition(".")[2] if isinstance(verify, str) else ""
        if leaf.startswith(("cmd_", "sp_requested_")):
            self.add(WARNING, path, "it checks the command itself, which only shows it was received: check the "
                                    "status or reading it changes instead")
        if number(s.get("timeout_sec", 5.0)) == 0:
            self.add(WARNING, path, "a time limit of 0 s fails unless the check holds straight away")

    def step_wait_telemetry(self, path, s):
        self.comparison(path, s.get("target"), s.get("condition", "=="), s.get("value", 0.0), "to wait on")
        if not number(s.get("timeout_sec", 0.0)):
            self.add(WARNING, path, "no time limit: it waits until the condition holds, however long that takes "
                                    "(it fails only if the reading stops for 10 s)")

    def step_user_prompt(self, path, s):
        if not str(s.get("prompt_text") or "").strip():
            self.add(WARNING, path, "the operator is shown no message")
        for i, cond in enumerate(s.get("verify_conditions") or [], 1):
            self.comparison(path, cond.get("target"), cond.get("condition", "=="), cond.get("value", 1.0),
                            f"for condition {i}")

    def step_sub_recipe(self, path, s):
        target = s.get("target_file", "")
        try:
            full = resolve_sub_recipe(self.file, target, self.recipes_dir)
        except RecipeError as e:
            self.add(ERROR, path, str(e))
            return
        if full in self.stack:
            circle = " > ".join(os.path.basename(p) for p in self.stack + (full,))
            self.add(ERROR, path, f"the recipes call each other in a circle ({circle}), so it would never end")
            return
        if len(self.stack) > MAX_DEPTH:
            self.add(ERROR, path, f"sub-recipes nested more than {MAX_DEPTH} deep")
            return
        _, problems = self._load(full)
        name = os.path.relpath(full, self.recipes_dir)
        for p in problems:
            if p.level == ERROR:
                self.add(ERROR, path, f"in {name}: {p.text()}")

    def step_routine_call(self, path, s):
        name = s.get("routine_name")
        cls = ROUTINE_REGISTRY[name]
        params = s.get("parameters") or {}
        if not cls.ACCEPTS_UNKNOWN:
            unknown = sorted(set(params) - known_keys(cls) - {"loop"})
            if unknown and name != "mass_scan":              # (mass_scan refuses them: an error already)
                self.add(WARNING, path, f"{', '.join(unknown)}: not read by this routine (misspelt?)")
        by_key = {p.key: p for p in cls.PARAMS}
        for p in cls.PARAMS:
            value = p.read(params)
            if p.kind == "settable_tag" and value == TURBO_ENABLE:
                # (a routine sends its tags other values than 1: a supply's setpoint, a ramp's back-off, the
                # relay routine's 0 when START isn't pressed - any of them would be a stop)
                self.add(ERROR, path, f"{p.label.lower()}: the source turbo pump's start isn't a routine's to drive - "
                                      f"{TURBO_STOP}")
            elif p.kind == "settable_tag" and value:
                self.settable(path, value, None, f"for {p.label.lower()}")
            elif p.kind == "readable_tag" and value:
                self.readable(path, value, f"for {p.label.lower()}")
            elif p.unit_of and value is not None and self.known and p.key in VALUE_PARAMS:
                t = self.catalog.tag(by_key[p.unit_of].read(params))
                if t is not None and t.is_number:
                    self.value_for(path, t, value, VALUE_PARAMS[p.key])
        check = getattr(self, f"routine_{name}", None)
        if check is not None:
            check(path, params)

    def routine_mass_scan(self, path, params):
        if not self.known:
            return
        from src.sequencer.mass_scan import SP_TAG
        t = self.catalog.tag(SP_TAG)
        if t is None or t.min_val is None or t.max_val is None:
            return
        try:
            ROUTINE_REGISTRY["mass_scan"]._config(params, (t.min_val, t.max_val))
        except ValueError as e:
            self.add(ERROR, path, str(e))

    def routine_psu_control(self, path, params):
        enable = str(params.get("cmd_enable") or "")
        sub = enable.rpartition(".")[0]
        others = [params.get(k) for k in ("stat_enabled", "sp_tag", "rb_tag") if params.get(k)]
        if sub and any(isinstance(t, str) and t.rpartition(".")[0] != sub for t in others):
            self.add(WARNING, path, "its tags belong to different supplies: check the on/off command, the status, "
                                    "the setpoint and the readback")
        supply = self.catalog.supply(enable) if self.known else None
        if supply is not None and not supply.wait_by_default and number(params.get("wait_for_readback", 1.0)):
            self.add(WARNING, path, f"the {supply.name.lower()} current follows the filament, not its setpoint: "
                                    "don't wait for its readback here (it times out while the filament warms)")
        if sub in RELAY_SUBSYSTEMS and number(params.get("requires_relay", 1.0)) == 0:
            self.add(WARNING, path, "this supply needs the safety relay: tick 'Needs the safety relay', so the "
                                    "step fails at once without it")

    def routine_hv_conditioning(self, path, params):
        if not params.get("enabled_tag"):
            self.add(WARNING, path, "no 'on status' tag: the ramp can't tell if the supply is off")
        if "pressure_tag" in params and not params.get("pressure_tag"):
            self.add(WARNING, path, "no pressure gauge: the ramp won't hold for the pressure")
            return
        gauge = params.get("pressure_tag") or VG1
        off = gauge_relay_off(gauge)
        mx = number(params.get("pressure_max", 5e-6))
        if off is not None and mx is not None and mx >= off:
            self.add(WARNING, path, f"it holds only above {mx:.1e} mbar, but the gate valve's permissive goes at "
                                    f"{off:.1e} mbar on that gauge and the gate valve closes: hold below that")

    # ------------------------------------------------------------------ the order of the steps
    def _flatten(self, steps, base, file, report, depth=0):
        """[(path, step, report)] in the order they run: a PARALLEL's steps at its place, a
        sub-recipe's steps (not reported: they are its own) after its SUB_RECIPE step."""
        out = []
        for i, s in enumerate(steps):
            if not isinstance(s, dict):
                continue
            path = base + (i,) if report else base
            kind = s.get("type")
            if kind == "PARALLEL" and isinstance(s.get("steps"), list):
                out.extend(((path + (j,)) if report else path, c, report)
                           for j, c in enumerate(s["steps"]) if isinstance(c, dict))
                continue
            out.append((path, s, report))
            if kind == "SUB_RECIPE" and depth < MAX_DEPTH:
                try:
                    full, data = self.sub_recipe(file, s.get("target_file", ""))
                except RecipeError:
                    continue
                if isinstance(data, dict) and isinstance(data.get("steps"), list) and full not in self.stack:
                    out.extend(self._flatten(data["steps"], path, full, False, depth + 1))
        return out

    def order(self, steps, policy):
        flat = self._flatten(steps, (), self.file, True)
        first_on = {}                        # subsystem -> index into flat of its first switch-on
        for k, (_, s, _) in enumerate(flat):
            for sub in _switched_on(s):
                first_on.setdefault(sub, k)
        relay_sure = vacuum_waited = auto_emission = cup_out = magnet_on = cup_in = smu_on = False
        relay_outputs_on = False
        for k, (path, s, report) in enumerate(flat):
            kind, target = s.get("type"), s.get("target")
            value = number(s.get("value"))
            routine = s.get("routine_name") if kind == "ROUTINE_CALL" else None
            params = _dict(s.get("parameters")) if routine else {}
            conds = _conditions_of(s)
            if routine == "safety_relay_init" or _holds_one(conds, SAFETY_RELAY):
                relay_sure = True
            if (routine == "safety_relay_init" and number(params.get("vacuum_max", 5e-7))) or any(
                    t == VG1 and c in ("<", "<=") for t, c, v in conds):
                vacuum_waited = True
            if _holds_one(conds, MAGNET_ENABLED):
                magnet_on = True
            if _holds_one(conds, CUP_INSERTED):
                cup_in, cup_out = True, False
            if kind in ("ACTION", "VERIFIED_ACTION") and isinstance(target, str):
                sub, leaf = target.rpartition(".")[::2]
                # A setpoint before the same recipe switches the supply on: the PLC zeroes it
                if report and leaf.startswith("sp_requested_") and sub in ZEROED_WHEN_OFF and first_on.get(sub, -1) > k:
                    self.add(WARNING, path, f"this setpoint is sent before a later step switches the {self.thing(sub)} "
                                            "on: the PLC zeroes the setpoint of a supply that is off (and the SMU "
                                            "refuses a bias) - switch it on first")
                if report and target == GV_CMD and value == 1.0 and not vacuum_waited:
                    self.add(WARNING, path, "the gate valve is opened with no wait for the source vacuum before it "
                                            "(the start-up procedure waits for VG1 below 5e-7 mbar)")
                if target == CUP_CMD:
                    cup_out, cup_in = value == 0.0, value == 1.0
                if report and leaf == "cmd_degauss" and value == 1.0 and cup_out:
                    self.add(WARNING, path, "the degauss sweeps the magnet with the Faraday cup out (an earlier step "
                                            "retracts it): with a beam, every mass reaches the sample")
                if target == AUTO_EMISSION:
                    auto_emission = value == 1.0
                if report and sub == FILAMENT and auto_emission and leaf in ("sp_requested_current", "cmd_enable"):
                    self.add(WARNING, path, "auto-emission is on (an earlier step): the PLC drives the filament "
                                            "itself and refuses filament commands")
            ons = _switched_on(s)
            magnet_on = magnet_on or MAGNET in ons
            smu_on = smu_on or SMU in ons
            relay_outputs = sorted(x for x in ons if x in RELAY_SUBSYSTEMS)
            if relay_outputs or routine == "hv_conditioning":
                relay_outputs_on = True
            guarded = routine == "psu_control" and number(params.get("requires_relay", 1.0))
            if report and relay_outputs and not relay_sure and not guarded:
                names = ", ".join(self.thing(x) for x in relay_outputs)
                self.add(WARNING, path, f"this switches on the {names}, which needs the safety relay, but no step "
                                        "before it makes sure the relay is on ('Bring the safety relay on', or a wait "
                                        "for the relay)")
            if report and routine == "mass_scan":
                missing = [what for what, ok in (("the analyser magnet on", magnet_on), ("the Faraday cup in", cup_in),
                                                 ("the cup bias on (above 50 V)", smu_on)) if not ok]
                if missing:
                    self.add(WARNING, path, f"the scan needs {_and(missing)}, and no step before it sees to that: "
                                            "it fails at once without the magnet or the cup, and measures nothing "
                                            "useful without the bias")
        if relay_outputs_on and not any(
                (e["type"] == "BIT_FAULT" and e["name"] == SAFETY_TRIP_FAULT)
                or (e["type"] == "CONDITION" and e["target"] == SAFETY_RELAY) for e in policy):
            self.add(WARNING, ("policy",), "the recipe switches on outputs that need the safety relay, but doesn't stop "
                                           f"if the relay trips: add {SAFETY_TRIP_FAULT} to its stop conditions")


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _where(path) -> str:
    if not path:
        return ""
    if path[0] == "policy":
        return f"stop condition {path[1] + 1}" if len(path) > 1 else "stop conditions"
    return "step " + ".".join(str(i + 1) for i in path)


def _and(items) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _plain(e: Exception) -> str:
    """The engine's message without its own 'step N: ' prefix (the problem says where)."""
    text = str(e)
    for _ in range(2):
        head, sep, rest = text.partition(": ")
        if sep and head.startswith("step "):
            text = rest
    return text


def _targets(step) -> list:
    """The tags a step sends values to."""
    if not isinstance(step, dict):
        return []
    if step.get("type") in ("ACTION", "VERIFIED_ACTION") and isinstance(step.get("target"), str):
        return [step["target"]]
    if step.get("type") == "ROUTINE_CALL":
        p = _dict(step.get("parameters"))
        return [t for t in (p.get("cmd_enable"), p.get("sp_tag"), p.get("cmd_tag")) if isinstance(t, str) and t]
    return []


def _switched_on(step) -> set:
    """Subsystems a step switches on (an output enable, the caesium heater, a psu_control)."""
    out = set()
    if not isinstance(step, dict):
        return out
    kind = step.get("type")
    if kind in ("ACTION", "VERIFIED_ACTION") and isinstance(step.get("target"), str):
        sub, leaf = step["target"].rpartition(".")[::2]
        v = number(step.get("value"))
        if leaf == "cmd_enable" and v == 1.0:
            out.add(sub)
        if sub == CESIUM and leaf == "cmd_ctrl_mode" and v == 1.0:
            out.add(sub)
        if leaf == "cmd_degauss" and v == 1.0:
            out.add(sub)            # (the magnet service switches the output on for it, and ends with it on)
    if kind == "ROUTINE_CALL" and step.get("routine_name") == "psu_control":
        tag = _dict(step.get("parameters")).get("cmd_enable")
        if isinstance(tag, str):
            out.add(tag.rpartition(".")[0])
    return out


def _conditions_of(step) -> list:
    """(tag, condition, value) a step waits on or checks."""
    kind = step.get("type")
    if kind == "WAIT_TELEMETRY":
        return [(step.get("target"), step.get("condition", "=="), number(step.get("value")))]
    if kind == "USER_PROMPT":
        return [(c.get("target"), c.get("condition", "=="), number(c.get("value", 1.0)))
                for c in _list(step.get("verify_conditions")) if isinstance(c, dict)]
    if kind == "VERIFIED_ACTION":
        out = []
        if step.get("pre_target"):
            out.append((step.get("pre_target"), step.get("pre_condition", "=="), number(step.get("pre_value", 1.0))))
        verify_value = step.get("verify_value")
        verify_value = step.get("value") if verify_value in (None, "") else verify_value
        out.append((step.get("verify_target") or step.get("target"), step.get("verify_condition", "=="),
                    number(verify_value)))
        return out
    return []


def _holds_one(conds, tag) -> bool:
    """True if one of the conditions makes sure `tag` reads 1."""
    return any(t == tag and ((c in ("==", ">=") and v == 1.0) or (c == ">" and v is not None and 0.0 <= v < 1.0)
                             or (c == "!=" and v == 0.0)) for t, c, v in conds)


_relay_off = {}


def gauge_relay_off(tag) -> Optional[float]:
    """Where the gate valve's permissive goes for VG1 or VG2 (None if unknown): the gauge's software
    trip point if it has one (src/core/software_trips.py), else the lowest off point of its relays in
    vac_gauges_config.json."""
    if not _relay_off:
        try:
            cfg = lab_config.read("vac_gauges_config.json")         # (with this machine's lab settings)
            trips = hard_caps.valid_trips(cfg)
            for node, channel, gauge in ((*hard_caps.GATE_VALVE_GAUGES[0], VG1), (*hard_caps.GATE_VALVE_GAUGES[1], VG2)):
                if (node, channel) in trips:
                    _relay_off[gauge] = trips[(node, channel)].trip_mbar
                    continue
                relays = (cfg.get(str(node), {}) or {}).get("relays") or {}
                for relay in relays.values():
                    off = number(relay.get("off_val")) if isinstance(relay, dict) else None
                    if off is not None and int(number(relay.get("channel")) or 0) == channel:
                        _relay_off[gauge] = min(off, _relay_off.get(gauge, off))
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        _relay_off.setdefault("", None)          # read once, even if nothing was found
    return _relay_off.get(tag)


def check_recipe(data: dict, catalog: Catalog = None, recipes_dir: str = None, path: str = None,
                 tree: dict = None) -> List[Problem]:
    """Every problem with a recipe (see the module doc). catalog: the machine's tags (None: this
    process's, from the tag registry; an empty Catalog leaves the tag checks out). `path`: its file,
    if it has one (where its sub-recipes are looked up, and a sub-recipe calling it back is a circle).
    `tree`: sub-recipes loaded already ({path: Recipe}, src/core/recipe_engine.py load_tree)."""
    stack = (os.path.realpath(path),) if path else ("<unsaved>",)
    checker = _Checker(catalog, recipes_dir, stack, {}, tree)
    checker.recipe(data)
    return checker.problems


def check_file(path: str, catalog: Catalog = None, recipes_dir: str = None) -> List[Problem]:
    try:
        data = json.loads(read_text(path)[0])
    except (OSError, ValueError) as e:
        return [Problem(ERROR, (), f"{os.path.basename(path)} can't be read: {e}")]
    return check_recipe(data, catalog, recipes_dir, path)


def errors(problems) -> list:
    return [p for p in problems if p.level == ERROR]


def warnings(problems) -> list:
    return [p for p in problems if p.level == WARNING]


def summary(problems, limit: int = 4) -> str:
    """'step 2: ...; step 5: ... (and 3 more)' for a status line or a refusal."""
    shown = "; ".join(p.text() for p in problems[:limit])
    more = len(problems) - limit
    return shown + (f" (and {more} more)" if more > 0 else "")
