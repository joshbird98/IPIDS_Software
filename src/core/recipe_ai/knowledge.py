"""What the model is told: about SINDIE, its recipes and the request in hand.

    system_prompt(catalog, mode, recipes_dir, current_file)     "draft" or "review"
    draft_task(...), refine_task(...), review_task(...)        the operator's request, with the recipe

Everything that can come from the code does: the tags a recipe may set and every reading (the tag
registry, src/core/recipe_catalog.py - what the checks use), the step fields, the procedures and
their settings (src/core/routines.py), the supplies, the PLC faults (fault_config.json), the
templates (config/recipe_templates) and the saved recipes. So a tag renamed, a range changed or a
procedure setting added reaches the model as it reaches the checks; only the rules and the typical
values below are written by hand, from the help's procedures (docs/procedures/).
"""
import json
import os

from src.core import paths
from src.core.recipe_catalog import (CESIUM_HEATER, FAULT_RESET, SAFETY_ENABLE, SAFETY_RELAY, TURBO_ENABLE, Catalog,
                                     subsystem_name)
from src.core.recipe_check import ERROR, RELAY_SUBSYSTEMS
from src.core.recipe_store import read_recipe, renumber
from src.core.recipe_text import describe_recipe, describe_step
from src.core.routines import ROUTINE_REGISTRY

TEMPLATES_DIR = os.path.join(paths.CONFIG_DIR, "recipe_templates")

# The help pages the model may read (read_help): id -> (file under docs/, title)
HELP_PAGES = {
    "beam-start-up": ("procedures/beam-start-up.md", "Bringing up a beam (the start-up procedure)"),
    "beam-shut-down": ("procedures/beam-shut-down.md", "Shutting down (by hand, or by the PLC)"),
    "tuning-the-beam": ("procedures/tuning-the-beam.md", "Selecting a mass and tuning the beam"),
    "alarms-and-faults": ("procedures/alarms-and-faults.md", "Alarms and faults"),
    "recording-an-experiment": ("procedures/recording-an-experiment.md", "Recording an experiment"),
    "recipes": ("panels/recipes.md", "Recipes: how they run, the steps, procedures and checks"),
    "ion-source": ("panels/ion-source.md", "TESS Ion Source Control panel (the source supplies)"),
    "vacuum": ("panels/vacuum.md", "Vacuum System panel (gauges, gate valve, turbo pumps)"),
    "system-health": ("panels/system-health.md", "System Health panel (safety relay, PLC faults, shutdown)"),
    "beamline-optics": ("panels/beamline-optics.md", "Beamline Optics panel (magnets, steering, slits)"),
    "faraday-cup-smu": ("panels/faraday-cup-smu.md", "Faraday Cup & SMU panel"),
    "mass-scan": ("panels/mass-scan.md", "Mass Scan panel"),
    "safety": ("safety.md", "Safety rules"),
}

ROLE = {
    "draft": "You draft recipes - sequences of steps that the sequencer service runs on the machine - from what the "
             "operator asks for.",
    "review": "You review the operator's recipe - a sequence of steps that the sequencer service runs on the "
              "machine - against SINDIE's procedures, and note what looks wrong or missing.",
}
USE = {
    "draft": """\
- You never act on the machine. The operator sees what you propose as changes to their recipe and keeps or
  leaves out each one; nothing is saved or run until they do, and saving and the sequencer check it again.
- Your proposals go through the same checks as every recipe (check_recipe). An error means the sequencer would
  refuse the recipe, and a proposal with errors is sent back to you. A warning means it would run, but probably
  not as meant: fix it, or say in your summary why it is right here.""",
    "review": """\
- You never act on the machine, and you don't change the recipe: your notes are shown beside its steps, and
  the operator can have any fix you suggest drafted (it is then checked and shown as changes, like any draft).
- The automatic checks (check_recipe: the ones the sequencer makes) have run already; you look for what they
  can't know.""",
}

INTRO = """\
You are the assistant in the recipe builder of SINDIE (the Surrey Implanter for Negative-ions, Defect
Implantation, and Enrichment), an ion implanter at the Surrey Ion Beam Centre. The people you work for are
trained operators and researchers. {role}

How your work is used:
{use}
- Use only the tags, faults, step fields and procedures given here (find_tags and describe_tag give more
  detail). Never invent a tag, a field or a value. If something can't be done with them, say so.
- The same recipes run on the machine and in its simulator: write every one for the machine.

## The machine
- The source: the TESS (Thermionic Emission Sputter Source), a caesium negative-ion source on a high-voltage
  deck. Its supplies: extraction and target (cathode) in kV, the filament in A, the thermionic (the ioniser) in
  mA, the caesium oven in °C, and the source einzel lens in kV. The source gate valve, the source turbo pump and
  the source pressure gauge VG1 are with it.
- The beamline: the analyser magnet (A) selects the mass; the X-steerer magnet (A) and the electrostatic
  steering (V) steer the beam; the Faraday cup, when inserted, stops the beam and measures it (its SMU biases
  it, in V); current monitors on the slits and apertures; gauges VG2 and VG3. The endstation and loadlock have
  gauges VG4 to VG6.
- Three layers of control: this PC software; the PLC, which applies its own interlocks, limits and shutdown
  sequences whatever the PC asks (a command it refuses fails the step); and the hardwired safety relay, without
  which there is no high voltage. The relay is switched on by the operator (key and START on the High Voltage
  Systems Control box) once the PLC allows it.

## How recipes run
- Steps run one at a time, in order. A step that fails or times out ends the recipe at once: no retries.
- A recipe never switches anything off when it ends, fails or is stopped: outputs stay as the steps left them.
- Stop conditions (fault_policy) end the recipe as failed as soon as one holds: a latched PLC fault, or a
  condition on a live reading. A stop condition whose reading has no live value for 3 s stops it too. They only
  stop the recipe; the PLC's interlocks act on the machine anyway.
- An "Ask the operator" step (USER_PROMPT) waits for the operator's OK as long as it takes; its
  verify_conditions refuse the OK until they hold on live values.
- Waits and checks use live values only: a reading with no live value for 10 s fails the step.
- While the safety relay is off the source deck is unpowered and can't be read: the source einzel, filament,
  thermionic and source body readings, and the caesium oven's thermocouple, have no live value. So a stop
  condition on one of them stops a recipe that starts with the relay off: check such a reading with a wait
  once its supply is on (as the start-up template checks the einzel current) instead.
- When the safety relay drops, everything it powers loses power: the source supplies, the caesium oven and
  the source einzel go off, the analyser magnet and X-steerer services switch their outputs off (0 A), and the
  SMU bias goes to 0 V and off. Nothing comes back on by itself. So a recipe that must leave the magnet or the
  cup bias on can't drop the relay (and the PLC's shutdown drops it at its end).
- While a recipe runs it holds the controls it commands (control leases), and the panels can't drive them.
  The safety relay, shutdown, fault reset and gate valve controls can't be leased: the operator keeps them
  even while a recipe runs.
- Closing the source gate valve drops the safety relay (the PLC withdraws its permission). A recipe that ends
  with the relay on leaves the high voltage live (the beacon red): say so in the summary when that is meant.

## Rules
1. Never reset faults ({fault_reset} or any cmd_reset): a person presses RESET FAULTS (an "Ask the operator"
   step may ask them to, where a procedure has them do it).
2. Leave the source turbo pump alone ({turbo}): a recipe never starts or stops it, and never asks anyone to
   stop it. Trained staff run it, by hand, at the pump.
3. Outputs that need the safety relay - {relay_outputs} (powered through it, or switched off by their service
   while it is off; a degauss too) - need it on first. In a recipe that may start with the relay off (a
   start-up), put "Bring the safety relay on" (safety_relay_init) before them. In one meant to run only with
   the source already up (tuning, a scan, a degauss), make sure it is on instead: a short "Wait for a reading"
   of {relay} == 1, or psu_control with requires_relay true (it fails at once without the relay). Never send
   {safety_enable} = 1 yourself to bring it on.
4. A recipe that switches on, or relies on, anything that needs the relay stops if the relay trips: its
   fault_policy includes Safety_Relay_Tripped (the exact entry is below).
5. The source gate valve opens only with the source pressure (VG1) below 5e-7 mbar; safety_relay_init sees to
   it.
6. Switch a supply on before sending its setpoint: the PLC sets the setpoint of its supplies that are off to
   zero (the extraction, target, filament, thermionic, caesium oven and steering), and the SMU refuses a bias
   while it is off. psu_control does both, in order, and waits for the readback.
7. Values within each tag's range (listed below) and SINDIE's typical values (below). A value the operator
   gives is used as given. When drafting, every value that matters which you chose yourself - setpoints, scan
   ranges, steps, time limits - goes in values_to_check (not the defaults you left alone), and so does a value
   the operator gave that is outside the typical values.
8. While the caesium is hot the PLC won't let the target, filament or thermionic be switched off, keeps the
   target at 3 kV or more, and won't let the safety relay drop. To shut down, cool the caesium first (heater
   off, forced cooling on, wait for the PLC's caesium-hot flag to clear) or hand the shutdown to the PLC
   ({shutdown}) - see the shutdown templates.
9. With the Faraday cup out, a beam can reach the sample. Insert it, and check it is in, before anything that
   can put a beam into the beamline (the analyser magnet on, a degauss, a mass scan, tuning), unless the
   operator means the beam to reach the sample.
10. While auto-emission is on the PLC drives the filament itself and refuses filament commands: switch it off
    before setting the filament.
11. Every wait on a reading has a time limit (timeout_sec) that fits what it waits for: a supply's readback 1
    to 2 min (the supplies list and the templates give usual ones), the Faraday cup 15 s, the source vacuum up
    to 1 h, the caesium oven 45 min to heat and 1 h to cool, a degauss 5 min (it takes about 2).
12. Never "==" or "!=" on an analogue reading (a pressure, voltage, current or temperature): use <, >, <=, >=,
    or "within" with a tolerance.
13. Don't put two steps that drive the same thing inside one "Do steps at the same time" step.
14. Use "Set a value and check it" where a later step depends on a command having worked (the Faraday cup in
    before a scan); a plain "Set a value" is enough where nothing does (switching off at the end).

## Typical values (SINDIE's procedures; not limits)
- Source vacuum before the gate valve opens: VG1 below 5e-7 mbar.
- Extraction 15 to 20 kV; target (cathode) 5 to 9 kV. The beam energy is extraction plus target: 15 kV + 5 kV
  is a 20 keV beam of singly charged ions. With no beam, each draws only its bleed current, 0.02 mA per kV.
- Source einzel 8 to 16 kV, its current near zero: above 50 µA its insulators are failing (switch it off and
  report it). Check it with a wait for its current below 50 µA soon after it is on, as the start-up template
  does - the wait fails if it isn't. Its current limit (sp_requested_current) is set by its service (50 µA
  unless set): leave it.
- Thermionic (ioniser): switch it on; it goes to about 2500 V by itself. Its current (100 to 150 mA) comes only
  once the filament is hot and follows the filament, not its setpoint: never wait for its readback.
- Filament: when new, or after the source has been at air, first 15 A to degas (the source pressure spikes,
  then falls back - the operator watches it), then the operating point, 28 to 30 A. The PLC raises it at
  1 A per second at most.
- Caesium oven: forced cooling off, heater on, setpoint 100 to 150 °C; it takes about 15 minutes. The target
  and extraction currents then rise above their bleed currents (aim for about 1 mA on the target).
- Faraday cup: inserted, and biased above 50 V (the SMU at 60 V) for a correct beam current.
- Analyser magnet: 0 to 95 A; mass scans typically 20 to 30 A in 0.05 A steps. The magnet current for a mass
  grows with the square root of mass times beam energy: change the voltages and the magnet must change too.
  For a scan step finer than 0.05 A, make mass_scan's tolerance (how close the magnet must come to each point)
  no bigger than the step. Before a scan or tuning, the procedure has the aperture ladder and the slits set at
  the machine, by hand.
- A degauss steps the magnet through a list of currents (about 2 minutes) and ends at 0 A with the output on.
- High-voltage conditioning (hv_conditioning): steps of 0.5 kV held 60 s each, holding while VG1 is above
  5e-6 mbar (it must hold below the gauge relay's off point, or the gate valve and the relay drop first). It
  starts from the supply's present readback, and has no overall time limit: while the pressure stays high it
  holds, until the operator stops the recipe.
- Arcs. The PLC quenches an arc on the target or the thermionic (output to 0 for 1 s; the flags
  ion_beam.internal.tgt_is_quenching and thm_is_quenching read 1 meanwhile; three close together latch a
  _Sustained_Arc fault and start a shutdown); the extraction's quench can't trigger as the PLC is programmed.
  The source einzel's arcs - its output collapsing below 10 % of its setpoint, or its current above 500 µA,
  on two polls in a row (not judged below 0.5 kV) - latch the Unit1_Arc_Exceeded fault until RESET FAULTS,
  without switching it off: to stop on them, make that fault a stop condition, and leave hv_conditioning's
  arc_mode_tag empty for the einzel (a latched flag would hold the ramp for ever). The Spellman faults Unit1_*
  are the source einzel's; Spellman units 2 to 4 aren't installed.
- Shutdown by hand: caesium heater off and forced cooling on, wait for the caesium to cool (about 20 min), then
  the filament to 0 A, then every supply off, then the safety relay request withdrawn. By the PLC: about
  20 minutes.
"""

STEPS = """\
## Steps
A recipe file: {{"recipe_name": "...", "version": "1.0", "description": "...", "fault_policy": [...],
"steps": [...]}}. Every step has a "type", its own fields, and a "comment": its note, shown under it in the
list - write one for every step you add, in a few plain words. Leave "step_id" out of the steps you add: the
builder numbers the steps 1, 2, 3 ... after every change (so after an insertion or a deletion, check_recipe's
numbers are the new ones). Leave "version" as it is: the builder offers the next one when the recipe is saved.
Values: numbers as numbers; on/off as true/false (or 1/0). Conditions are "==", "!=", ">", "<", ">=", "<=",
or "within" with a "tolerance".

- "Set a value" - {{"type": "ACTION", "target": <tag a recipe may set>, "value": <value>}}
  Sends one command or setpoint; done when the service confirms it was carried out.
- "Set a value and check it" - {{"type": "VERIFIED_ACTION", "target": <tag>, "value": <value>,
  "verify_target": <reading that shows it worked>, "verify_condition": <condition>, "verify_value": <value>,
  "verify_tolerance": <for within>, "timeout_sec": <s, default 5>}}
  Optionally first "pre_target", "pre_condition", "pre_value", "pre_tolerance": a condition that must already
  hold (the step fails at once if it doesn't). Check the status the command changes, not the command itself
  (e.g. insert the cup with ion_beam.beamline.diagnostics.cmd_insert_fc, check
  ion_beam.beamline.diagnostics.stat_fc_inserted == 1).
- "Wait a time" - {{"type": "WAIT_TIME", "duration_sec": <s>}}
- "Wait for a reading" - {{"type": "WAIT_TELEMETRY", "target": <reading>, "condition": <condition>,
  "value": <number>, "tolerance": <for within>, "timeout_sec": <s; 0 means no limit>}}
- "Ask the operator" - {{"type": "USER_PROMPT", "prompt_text": <what to do or check; "Click OK when ..." where they act>,
  "verify_conditions": [{{"target": <reading>, "condition": <condition>, "value": <value>}}, ...]}}
  For what only a person can do or see: checks at the machine, the key and START, watching a degas. Add
  verify_conditions for what can be read.
- "Run another recipe" - {{"type": "SUB_RECIPE", "target_file": <a file in the recipes folder, listed below>}}
- Procedures - {{"type": "ROUTINE_CALL", "routine_name": <name>, "parameters": {{...}}}}: below.
- "Do steps at the same time" - {{"type": "PARALLEL", "steps": [<steps>]}}: done when all of them are. Not for
  "Run another recipe" or another "Do steps at the same time".
"""

FINISH_DRAFT = """\
## How to work
1. Read the request, the recipe the operator has open, what the checks find in it, and the selected step.
2. Look up what you aren't sure of: find_tags and describe_tag for tags, read_help for the procedures,
   read_recipe for a saved recipe or a template.
3. Make the whole recipe as it should be. Edit, don't rewrite: keep every step you weren't asked to change as
   it is - its fields, values and note - and in its place. But change what your change makes wrong: a note, a
   value or the description that no longer fits (a supply's first setpoint that must match a scan's new start,
   say), and say so in the summary. A request about the whole recipe ("make it safe", "check the timings")
   lets you change any step the rules call for: say which.
4. check_recipe it. Fix every error, and every warning that points at a real mistake.
5. Call propose_recipe with the whole recipe, a summary (a few plain sentences: what you did, and anything you
   didn't do and why), your assumptions, and the values the operator should check. That ends your turn.
- "This step", "here" or "it" means the selected step. New steps go where the request says, else after the
  selected step, else at the end - and a step that others need first (the relay, the cup) goes before them.
  In the summary, use the step numbers the operator sees now.
- Add what the request needs to work safely - the relay, the Faraday cup, a stop condition - and say so in the
  summary; nothing beyond that. A new recipe gets a recipe_name and a description.
- A shutdown switches off what the shutdown procedure does, unless the request says to leave something on.
- Prefer the procedures (psu_control, safety_relay_init, hv_conditioning, mass_scan) and the templates'
  patterns to steps of your own.
- If part of the request is something a recipe may not do, leave that part out, do the rest, and say in the
  summary what you left out, why, and who does it instead (RESET FAULTS on System Health; the turbo pump by
  trained staff at the pump). If nothing sensible can be drafted, propose the recipe unchanged and say why.
- If the request is unclear, propose the closest safe recipe and say what you assumed.
- Write as the templates do: British English, short plain sentences, no jargon the operator wouldn't use.
"""

FINISH_REVIEW = """\
## How to review
Review the recipe as an experienced colleague who knows SINDIE's procedures would. The automatic checks'
findings are listed with the recipe: the operator sees them, so don't repeat them. Look for what they can't
know, comparing the recipe with the procedures (read_help) and the templates - a difference that matters is a
note:
- the order and completeness: missing, misplaced or doubled steps;
- values unusual for the purpose (against the typical values), and time limits too short or too long for what
  is waited for;
- prompts that don't say what to do or check, or ask for something they could verify with conditions;
- stop conditions the recipe should have for what it does;
- what the machine is left in when the recipe ends, fails or is stopped, if that would surprise;
- notes that don't match what their step does.
A step done as a template does it is fine: don't note it for being the template's way. Look things up rather
than guess; check_recipe can confirm that a fix you have in mind would pass the checks.
For each finding, one note: the step it is about ("3", "5.2", "stop conditions", or "" for the whole recipe,
with the numbers as the recipe has them now), how much it matters ("concern": probably wrong or unsafe -
including a value outside the typical values with no reason given; "suggestion": would be better;
"question": depends on what only the operator knows, such as whether the filament is new), one or two plain
sentences, and if there is one, a fix as an
instruction a drafter could carry out ("Insert a 15 A degassing step before step 9"). No padding: if the
recipe is good, say so, with no notes. Call submit_review once, at the end. Write in British English, plainly.
"""


# ---------------------------------------------------------------------------------------------- generated parts
def _range(tag) -> str:
    text = tag.range_text()
    return f", {text}" if text else ""


def _kind_of(tag) -> str:
    if tag.is_switch:
        return "on/off"
    if tag.choices:
        return "whole number: " + ", ".join(f"{v:g} = {m}" for v, m in tag.choices)
    if tag.is_whole:
        return "whole number"
    if tag.is_number:
        return f"number{(' in ' + tag.unit) if tag.unit else ''}"
    return tag.datatype or "?"


def settable_section(catalog: Catalog) -> str:
    lines = ["## Tags a recipe may set (all of them)"]
    for t in sorted(catalog.settable(), key=lambda t: t.name):
        if t.name == TURBO_ENABLE:
            continue                                # (rule 2: left alone)
        what = t.label + (" (1 on, 0 off)" if t.name == CESIUM_HEATER else "")
        lines.append(f"- {t.name}: {what}; {_kind_of(t)}{_range(t)}")
    return "\n".join(lines)


def readings_section(catalog: Catalog) -> str:
    """Every reading, by part of the machine: the tag's last part, with its unit."""
    groups = {}
    for t in catalog.readable():
        if t.settable or t.name.rpartition(".")[2].startswith("testing_"):
            continue
        groups.setdefault(t.subsystem, []).append(t)
    lines = ["## Readings: every tag a recipe can wait on or check, by part of the machine",
             "(the full tag is the part's name, a dot, then the reading: e.g. ion_beam.source.extraction.rb_voltage. "
             "Readings without a unit are on/off (1/0) or whole numbers.)"]
    for base in sorted(groups):
        items = []
        for t in sorted(groups[base], key=lambda t: t.leaf):
            items.append(f"{t.leaf} {t.unit}".strip())
        name = subsystem_name(base)
        lines.append(f"- {base}{f' ({name})' if name else ''}: " + ", ".join(items))
    return "\n".join(lines)


def supplies_section(catalog: Catalog) -> str:
    lines = ["## Supplies, for psu_control (and the tags of hv_conditioning)"]
    for s in catalog.supplies:
        rng = f"{s.min_val:g} to {s.max_val:g} {s.unit}" if s.min_val is not None and s.max_val is not None else s.unit
        wait = "true" if s.wait_by_default else "false (its readback doesn't follow its setpoint)"
        lines.append(f"- {s.name}: cmd_enable {s.enable_tag}, stat_enabled {s.enabled_tag}, sp_tag {s.setpoint_tag} "
                     f"({rng}), rb_tag {s.readback_tag}; usually tolerance {s.tolerance:g} {s.unit}, timeout_sec "
                     f"{s.timeout_s:g}, wait_for_readback {wait}")
    return "\n".join(lines)


def _param_line(p) -> str:
    kinds = {"settable_tag": "a tag a recipe may set", "readable_tag": "a reading", "number": "number",
             "integer": "whole number", "seconds": "seconds", "bool": "true/false", "text": "text"}
    bits = [kinds.get(p.kind, p.kind)]
    if p.unit:
        bits.append(p.unit)
    if p.unit_of:
        bits.append(f"in the unit of {p.unit_of}")
    if p.minimum is not None:
        bits.append(f"at least {p.minimum:g}")
    if p.maximum is not None:
        bits.append(f"at most {p.maximum:g}")
    if p.required:
        bits.append("required" + (f" (the builder starts it at {json.dumps(p.default)})" if p.default not in (None, "")
                                  else ""))
    elif p.default not in (None, ""):
        bits.append(f"default {json.dumps(p.default)}")
    text = f"  - {p.key} ({p.label}): {', '.join(bits)}"
    if p.help:
        text += f". {p.help}"
    return text


def procedures_section() -> str:
    lines = ["## Procedures (ROUTINE_CALL steps)"]
    for name, cls in ROUTINE_REGISTRY.items():
        doc = " ".join((cls.__doc__ or "").split())
        lines.append(f"### {name}: \"{cls.TITLE}\"")
        if doc:
            lines.append(doc)
        lines.append("Parameters:")
        lines.extend(_param_line(p) for p in cls.PARAMS if p.key != "gv_stat_closed")
    lines.append("Give psu_control and hv_conditioning a supply's tags from the list below; leave the other tag "
                 "parameters at their defaults unless asked.")
    return "\n".join(lines)


def faults_section(catalog: Catalog) -> str:
    lines = ["## Stop conditions (fault_policy)",
             'A PLC fault: {"type": "BIT_FAULT", "registry_tag": <fault word>, "bit_str": "<byte>.<bit>", "name": <name>, '
             '"action": "ABORT"}',
             'A condition: {"type": "CONDITION", "target": <reading>, "condition": <condition>, "value": <number>, '
             '"message": <what it means, for the operator>} ("tolerance" too, for within)',
             "The one every recipe using the safety relay needs:",
             '{"type": "BIT_FAULT", "registry_tag": "ion_beam.faults.word_0_system", "bit_str": "0.1", '
             '"name": "Safety_Relay_Tripped", "action": "ABORT"}',
             "The PLC faults (fault word: bit name (what it means)):"]
    words = {}
    for f in catalog.faults:
        words.setdefault(f.word_tag, []).append(f)
    for word in sorted(words):
        items = sorted(words[word], key=lambda f: tuple(int(x) for x in f.bit_str.split(".")))
        lines.append(f"- {word}: " + "; ".join(f"{f.bit_str} {f.name} ({f.description.rstrip('.')})" for f in items))
    return "\n".join(lines)


def compact(data: dict) -> str:
    """A recipe as JSON, one step to a line."""
    head = {k: v for k, v in data.items() if k != "steps"}
    text = json.dumps(head, ensure_ascii=False)[:-1]
    steps = data.get("steps") if isinstance(data.get("steps"), list) else []
    if not steps:
        return text + ', "steps": []}'
    body = ",\n  ".join(json.dumps(s, ensure_ascii=False) for s in steps)
    return text + ', "steps": [\n  ' + body + "\n]}"


def templates_section(folder: str = TEMPLATES_DIR) -> str:
    lines = ["## Templates: checked recipes from the procedures (follow their patterns)"]
    try:
        names = sorted(f for f in os.listdir(folder) if f.endswith(".json"))
    except OSError:
        names = []
    for name in names:
        try:
            data, _ = read_recipe(os.path.join(folder, name))
        except (OSError, ValueError):
            continue
        data.pop("template", None)
        lines.append(f"### {name}\n{compact(data)}")
    return "\n".join(lines)


def recipes_section(recipes_dir: str, current_file: str = None, names: dict = None) -> str:
    lines = ["## Saved recipes (for \"Run another recipe\"; read_recipe shows one)"]
    try:
        files = sorted(f for f in os.listdir(recipes_dir) if f.endswith(".json"))
    except OSError:
        files = []
    files = [f for f in files if f != current_file]
    if not files:
        lines.append("(none)")
    for f in files:
        name = (names or {}).get(f)
        if name is None:
            try:
                name = str(read_recipe(os.path.join(recipes_dir, f))[0].get("recipe_name") or "")
            except (OSError, ValueError, AttributeError):
                name = "(can't be read)"
        lines.append(f"- {f}: {name}")
    return "\n".join(lines)


def help_section() -> str:
    lines = ["## Help pages (read_help)"]
    lines.extend(f"- {page}: {title}" for page, (_path, title) in HELP_PAGES.items())
    return "\n".join(lines)


def system_prompt(catalog: Catalog, mode: str = "draft", recipes_dir: str = None, current_file: str = None,
                  names: dict = None, templates_dir: str = TEMPLATES_DIR) -> str:
    relay_outputs = ", ".join(subsystem_name(s) for s in RELAY_SUBSYSTEMS)
    parts = [
        INTRO.format(role=ROLE.get(mode, ROLE["draft"]), use=USE.get(mode, USE["draft"]), fault_reset=FAULT_RESET,
                     turbo=TURBO_ENABLE, relay_outputs=relay_outputs, relay=SAFETY_RELAY, safety_enable=SAFETY_ENABLE,
                     shutdown="ion_beam.system.cmd_source_shutdown"),
        STEPS.format(),
        procedures_section(),
        supplies_section(catalog),
        settable_section(catalog),
        readings_section(catalog),
        faults_section(catalog),
        templates_section(templates_dir),
        recipes_section(recipes_dir or paths.RECIPES_DIR, current_file, names),
        help_section(),
        FINISH_REVIEW if mode == "review" else FINISH_DRAFT,
    ]
    return "\n\n".join(p.strip() for p in parts) + "\n"


# ---------------------------------------------------------------------------------------------- the request
def numbered(data: dict) -> dict:
    """A copy of the recipe with its steps numbered as the builder's list numbers them."""
    out = json.loads(json.dumps(data, default=str))
    if isinstance(out.get("steps"), list):
        renumber(out["steps"])
    return out


def _problems_text(problems) -> str:
    if not problems:
        return "- nothing"
    return "\n".join(f"- {'error' if p.level == ERROR else 'warning'}: {p.text()}" for p in problems)


def _recipe_block(data: dict, catalog, names, filename) -> str:
    where = f"its file is {filename}" if filename else "a new recipe, not saved yet"
    return (f"The recipe ({where}; the steps numbered as the operator sees them):\n{compact(numbered(data))}\n\n"
            f"In words:\n{describe_recipe(data, catalog, names)}")


def _selected_text(data: dict, selected, catalog, names) -> str:
    if not selected:
        return "No step is selected: new steps go at the end unless the request says where."
    step = data
    try:
        for i in selected:
            step = step["steps"][i]
    except (KeyError, IndexError, TypeError):
        return "No step is selected: new steps go at the end unless the request says where."
    number = ".".join(str(i + 1) for i in selected)
    return (f"Selected in the list: step {number} - \"{describe_step(step, catalog, names)}\". \"This step\" or "
            "\"here\" means it.")


def draft_task(instruction: str, data: dict, problems, selected, filename, catalog, names=None, earlier=None,
               unticked=()) -> str:
    text = (f"The operator asks: \"{instruction.strip()}\"\n\n{_recipe_block(data, catalog, names, filename)}\n\n"
            f"What the checks find in it now:\n{_problems_text(problems)}\n\n"
            f"{_selected_text(data, selected, catalog, names)}\n\n")
    if earlier is not None:
        text += ("An earlier draft of yours is shown to the operator as changes to that recipe (they haven't kept "
                 f"it); their request is a change to it:\n{compact(numbered(earlier))}\n")
        if unticked:
            text += ("They had unticked these of its changes (so may not want them):\n"
                     + "\n".join(f"- {t}" for t in unticked) + "\n")
        text += "\n"
    return text + ("Draft it: look up what you need, check your recipe, then call propose_recipe with the whole "
                   "recipe.")


def refine_task(instruction: str, unticked: list) -> str:
    text = f"The operator has looked at your proposal and asks: \"{instruction.strip()}\"\n"
    if unticked:
        text += ("They had unticked these of your changes (so may not want them):\n"
                 + "\n".join(f"- {t}" for t in unticked) + "\n")
    return text + ("Their recipe is as it was; change your proposal as asked, check it, and call propose_recipe "
                   "again with the whole recipe.")


def review_task(data: dict, problems, filename, catalog, names=None, focus: str = "") -> str:
    ask = f"\nThe operator asks you to look especially at: \"{focus.strip()}\"\n" if focus.strip() else ""
    return (f"Review this recipe.{ask}\n{_recipe_block(data, catalog, names, filename)}\n\n"
            f"The automatic checks already report (the operator sees these - don't repeat them):\n"
            f"{_problems_text(problems)}\n\nCall submit_review when you are done.")
