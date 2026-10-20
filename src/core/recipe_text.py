"""Recipes in plain English: one sentence per step, for the recipe builder (its outline and the
"Read as text" tab), the Recipe Execution panel's step list and the sequencer's status line.

    describe_step({"type": "ACTION", "target": "...extraction.sp_requested_voltage", "value": 15})
        -> "Set the extraction voltage setpoint to 15 kV"
    describe_recipe(data)       the whole recipe as numbered text, with what stops it
    estimate(data)              (seconds the timed parts add up to, whether waits come on top)

Names come from the tag registry (src/core/recipe_catalog.py); a tag it doesn't know is shown
as its name. Nothing here raises for a half-made step: the builder describes steps as they are
typed.
"""
import logging
import math

from src.core.conditions import CONDITIONS, number
from src.core.recipe_catalog import (CESIUM_HEATER, SAFETY_ENABLE, TURBO_ENABLE, Catalog, shared,
                                     subsystem_name)

log = logging.getLogger("recipe_text")

CUP_CMD = "ion_beam.beamline.diagnostics.cmd_insert_fc"
GV_CMD = "ion_beam.source.chamber.cmd_open_gv"
AUTO_EMISSION = "ion_beam.source.thermionic.cmd_auto_emission"
SMU_ENABLE = "ion_beam.beamline.faraday.smu.cmd_enable"

# Commands said as what they do
ACTIONS = {
    (CUP_CMD, 1.0): "Insert the Faraday cup",
    (CUP_CMD, 0.0): "Retract the Faraday cup",
    (GV_CMD, 1.0): "Open the source gate valve",
    (GV_CMD, 0.0): "Close the source gate valve",
    (SAFETY_ENABLE, 1.0): "Ask for the safety relay (the operator then presses START)",
    (SAFETY_ENABLE, 0.0): "Withdraw the safety relay request",
    ("ion_beam.system.cmd_source_shutdown", 1.0): "Ask the PLC to shut the source down (its shutdown sequence)",
    ("ion_beam.system.cmd_source_shutdown", 0.0): "Withdraw the source shutdown request",
    ("ion_beam.system.cmd_cancel_shutdown", 1.0): "Cancel the PLC's source shutdown",
    (CESIUM_HEATER, 1.0): "Switch the caesium oven heater on",
    (CESIUM_HEATER, 0.0): "Switch the caesium oven heater off",
    ("ion_beam.source.cesium.cmd_force_cooling", 1.0): "Switch the caesium oven's forced cooling on",
    ("ion_beam.source.cesium.cmd_force_cooling", 0.0): "Switch the caesium oven's forced cooling off",
    (AUTO_EMISSION, 1.0): "Switch auto-emission on (the PLC then drives the filament)",
    (AUTO_EMISSION, 0.0): "Switch auto-emission off",
    (TURBO_ENABLE, 1.0): "Start the source turbo pump",
    (SMU_ENABLE, 1.0): "Switch the Faraday cup bias on (ENABLE BIAS)",
    (SMU_ENABLE, 0.0): "Switch the Faraday cup bias off",
}
# Status flags said as statements: leaf -> (subject, when 1, when 0)
STATES = {
    "stat_enabled": ("{sub} output", "on", "off"),
    "stat_fc_inserted": ("{sub}", "in", "not in"),
    "stat_fc_retracted": ("{sub}", "out", "not out"),
    "stat_gv_open": ("{sub} gate valve", "open", "not open"),
    "stat_gv_closed": ("{sub} gate valve", "closed", "not closed"),
    "stat_gv_open_permissive": ("{sub} gate valve", "allowed to open", "not allowed to open"),
    "stat_gv_close_permissive": ("{sub} gate valve", "allowed to close", "not allowed to close"),
    "stat_vac_ok_for_gv": ("{sub} vacuum", "good enough for the gate valve (PLC)",
                           "not good enough for the gate valve (PLC)"),
    "gv_permissive_ready": ("vacuum", "good enough for the gate valve", "not good enough for the gate valve"),
    "safety_relay_active": ("safety relay", "on", "off"),
    "stat_ready": ("{sub}", "at speed", "not at speed"),
    "stat_turning": ("{sub}", "turning", "not turning"),
    "stat_auto_emission": ("{sub} auto-emission", "on", "off"),
    "stat_cooling_active": ("{sub} cooling air", "on", "off"),
    "stat_degaussing": ("{sub}", "degaussing", "not degaussing"),
    "stat_air_press_ok": ("compressed air", "OK", "not OK"),
    "stat_src_coolant_ok": ("source coolant flow", "OK", "not OK"),
    "stat_mag_coolant_ok": ("magnet coolant flow", "OK", "not OK"),
    "stat_lockout": ("{sub}", "locked out", "not locked out"),
    "cs_is_hot": ("caesium", "hot (the PLC's flag)", "no longer hot (the PLC's flag)"),
    "fil_is_hot": ("filament", "hot (the PLC's flag)", "no longer hot (the PLC's flag)"),
    "src_auto_sdown": ("PLC's source shutdown", "running", "not running"),
    "pc_plc_comms_lost": ("PLC's link to the PC", "lost (watchdog)", "working"),
}


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _catalog(catalog) -> Catalog:
    return catalog if catalog is not None else shared()


def fmt(value, unit: str = "") -> str:
    """15 -> '15', 5e-7 -> '5e-07', 1.26e-7 -> '1.26e-07' ... with the unit if there is one (six
    significant figures: the text is what authors check a recipe by)."""
    x = number(value)
    if x is None:
        return "?" if value in (None, "") else repr(value)
    s = f"{x:.6g}"
    return f"{s} {unit}" if unit else s


def duration_text(seconds) -> str:
    """'0.5 s', '45 s', '2 min 30 s', '1 h 05 min'."""
    s = number(seconds)
    if s is None:
        return "?"
    if s < 59.5:
        return f"{s:.3g} s"
    minutes, sec = divmod(int(round(s)), 60)
    if minutes < 60:
        return f"{minutes} min" + (f" {sec} s" if sec else "")
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h" + (f" {minutes:02d} min" if minutes else "")


def _and(items) -> str:
    items = list(items)
    if not items:
        return ""
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:] if text[:2] != text[:2].upper() else text


def _the(label: str, tag: str) -> str:
    """'the extraction voltage' - or the bare tag name if nothing better is known."""
    return label if label == tag else f"the {label}"


def statement(tag: str, truth: bool, catalog=None) -> str:
    """'the Faraday cup is in', 'the safety relay is off' ... for a flag reading 1 (truth) or 0."""
    catalog = _catalog(catalog)
    base, _, leaf = tag.rpartition(".")
    if leaf in STATES:
        subject, yes, no = STATES[leaf]
        subject = subject.format(sub=subsystem_name(base)).strip()
        return f"the {subject} is {yes if truth else no}"
    label = catalog.label(tag)
    if label == tag:
        return f"{tag} is {1 if truth else 0}"
    return f"the {label} flag is {'set' if truth else 'clear'}"


def condition_text(tag, condition, value, tolerance=None, catalog=None) -> str:
    """'the source pressure (VG1) is below 5.0e-07 mbar', 'the Faraday cup is in' ..."""
    catalog = _catalog(catalog)
    if not isinstance(tag, str) or not tag:
        return "(no tag chosen)"
    t = catalog.tag(tag)
    v = number(value)
    switch = (t is not None and t.is_switch) or tag.rpartition(".")[2] in STATES
    if switch and condition in ("==", "!=") and v in (0.0, 1.0):
        return statement(tag, (v == 1.0) == (condition == "=="), catalog)
    label = _the(catalog.label(tag), tag)
    unit = catalog.unit(tag)
    if condition == "within":
        return f"{label} is within ±{fmt(tolerance, unit)} of {fmt(value, unit)}"
    meaning = t.choice(v) if t is not None and v is not None else ""
    return f"{label} {CONDITIONS.get(condition, condition)} {fmt(value, unit)}" + (f" ({meaning})" if meaning else "")


def action_text(tag, value, catalog=None) -> str:
    """'Set the extraction voltage setpoint to 15 kV', 'Insert the Faraday cup' ..."""
    catalog = _catalog(catalog)
    if not isinstance(tag, str) or not tag:
        return "Set (no tag chosen)"
    v = number(value)
    if (tag, v) in ACTIONS:
        return ACTIONS[(tag, v)]
    base, _, leaf = tag.rpartition(".")
    if leaf == "cmd_degauss" and v == 1.0:
        return f"Degauss the {subsystem_name(base)}"
    if leaf == "cmd_enable" and v in (0.0, 1.0):
        return f"Switch the {subsystem_name(base)} {'on' if v else 'off'}"
    t = catalog.tag(tag)
    if ((t is not None and t.is_switch) or isinstance(value, bool)) and v in (0.0, 1.0):
        return f"Switch {_the(catalog.label(tag), tag)} {'on' if v else 'off'}"
    meaning = t.choice(v) if t is not None and v is not None else ""
    return (f"Set {_the(catalog.label(tag), tag)} to {fmt(value, catalog.unit(tag))}"
            + (f" ({meaning})" if meaning else ""))


# ---------------------------------------------------------------------------- the steps
def _action(s, catalog, names):
    return action_text(s.get("target"), s.get("value"), catalog)


def _verified_action(s, catalog, names):
    act = action_text(s.get("target"), s.get("value"), catalog)
    value = s.get("verify_value")
    value = s.get("value") if value in (None, "") else value
    check = condition_text(s.get("verify_target") or s.get("target"), s.get("verify_condition", "=="), value,
                           s.get("verify_tolerance"), catalog)
    text = f"{act}, then check that {check} (within {duration_text(s.get('timeout_sec', 5.0))})"
    if s.get("pre_target"):
        pre = condition_text(s.get("pre_target"), s.get("pre_condition", "=="), s.get("pre_value", 1.0),
                             s.get("pre_tolerance"), catalog)
        text = f"Check that {pre}; then {_lower_first(text)}"
    return text


def _wait_time(s, catalog, names):
    return f"Wait {duration_text(s.get('duration_sec', 0.0))}"


def _wait_telemetry(s, catalog, names):
    cond = condition_text(s.get("target"), s.get("condition", "=="), s.get("value", 0.0), s.get("tolerance"),
                          catalog)
    limit = number(s.get("timeout_sec", 0.0))
    return f"Wait until {cond}" + (f" (fails after {duration_text(limit)})" if limit else " (no time limit)")


def _user_prompt(s, catalog, names):
    text = str(s.get("prompt_text") or "").strip()
    out = f"Ask the operator: “{text}”" if text else "Ask the operator to click OK"
    conds = [condition_text(c.get("target"), c.get("condition", "=="), c.get("value", 1.0), c.get("tolerance"),
                            catalog) for c in s.get("verify_conditions") or [] if isinstance(c, dict)]
    return out + (f" (OK only once {_and(conds)})" if conds else "")


def _sub_recipe(s, catalog, names):
    target = str(s.get("target_file") or "").strip()
    if not target:
        return "Run another recipe (none chosen)"
    name = names.get(target)
    return f"Run the recipe “{name}” ({target})" if name else f"Run the recipe {target}"


def _parallel(s, catalog, names):
    n = len(s.get("steps") or [])
    return f"Do {n} step{'s' if n != 1 else ''} at the same time"


def _routine_call(s, catalog, names):
    name = s.get("routine_name")
    params = s.get("parameters") or {}
    describe = _ROUTINES.get(name)
    if describe is None:
        return f"Run the routine {name!r}"
    return describe(params, catalog)


def _mass_scan(p, catalog):
    start, stop, step = number(p.get("start_a")), number(p.get("stop_a")), number(p.get("step_a"))
    text = f"Mass scan: the magnet from {fmt(start, 'A')} to {fmt(stop, 'A')} in {fmt(step, 'A')} steps"
    if None not in (start, stop, step) and step > 0:
        text += f" ({int(round(abs(stop - start) / step)) + 1} points)"
    if number(p.get("goto_peak", True)):
        text += ", then set it on the biggest peak"
    if number(p.get("export", False)):
        text += "; saved as CSV"
    return text


def _safety_relay_init(p, catalog):
    details = []
    vmax = number(p.get("vacuum_max", 5e-7))
    if vmax:
        details.append(f"the gate valve opened once the source pressure is below {fmt(vmax, 'mbar')}")
    details.append(f"START pressed within {duration_text(p.get('timeout_sec', 6.0))}")
    settle = number(p.get("stabilization_sec", 15.0))
    if settle:
        details.append(f"then {duration_text(settle)} to settle")
    return f"Bring the safety relay on, with the operator ({'; '.join(details)})"


def _hv_conditioning(p, catalog):
    cmd = p.get("cmd_tag") or ""
    unit = catalog.unit(cmd)
    what = subsystem_name(cmd.rpartition(".")[0]) if cmd else "(no supply chosen)"
    text = (f"Condition the {what}: ramp it up to {fmt(p.get('max_val'), unit)} in {fmt(p.get('step_size', 0.5), unit)} "
            f"steps, {duration_text(p.get('hold_time_sec', 60.0))} each")
    gauge = p.get("pressure_tag", "ion_beam.source.vacuum_gauge_1.rb_pressure")
    if gauge:
        text += (f", holding while {_the(catalog.label(gauge), gauge)} is above "
                 f"{fmt(p.get('pressure_max', 5e-6), 'mbar')}")
    return text


def _psu_control(p, catalog):
    enable = p.get("cmd_enable") or ""
    what = subsystem_name(enable.rpartition(".")[0]) if enable else "(no supply chosen)"
    text = f"Switch the {what} on"
    sp = p.get("sp_tag")
    if sp:
        unit = catalog.unit(sp)
        text += f" and set it to {fmt(p.get('target_val', 0.0), unit)}"
        if number(p.get("wait_for_readback", True)) and p.get("rb_tag"):
            text += (f", then wait up to {duration_text(p.get('timeout_sec', 10.0))} for the readback to be within "
                     f"±{fmt(p.get('tolerance', 0.5), unit)}")
    return text


_ROUTINES = {"mass_scan": _mass_scan, "safety_relay_init": _safety_relay_init,
             "hv_conditioning": _hv_conditioning, "psu_control": _psu_control}
_DESCRIBE = {"ACTION": _action, "VERIFIED_ACTION": _verified_action, "WAIT_TIME": _wait_time,
             "WAIT_TELEMETRY": _wait_telemetry, "USER_PROMPT": _user_prompt, "SUB_RECIPE": _sub_recipe,
             "PARALLEL": _parallel, "ROUTINE_CALL": _routine_call}


def describe_step(step, catalog=None, recipe_names: dict = None) -> str:
    """One sentence for a step, as it is in the file. recipe_names: {file: recipe name} for the
    sub-recipes (their names are shown with them)."""
    if not isinstance(step, dict):
        return "(not a step)"
    kind = step.get("type")
    describe = _DESCRIBE.get(kind)
    if describe is None:
        return f"{kind} step (a step type the sequencer doesn't know)"
    try:
        return describe(step, _catalog(catalog), recipe_names or {})
    except (TypeError, ValueError, AttributeError, KeyError) as e:     # a half-made step
        log.debug("No text for %r: %s", step, e)
        return f"{kind} step"


def describe_policy_entry(entry: dict, catalog=None) -> str:
    """'the Safety Relay Tripped fault is latched', 'the safety relay is off' ..."""
    catalog = _catalog(catalog)
    if entry.get("type") == "BIT_FAULT" or ("registry_tag" in entry and "target" not in entry):
        name = str(entry.get("name") or f"{entry.get('registry_tag')} bit {entry.get('bit_str')}")
        fault = catalog.fault(name=name)
        text = f"the {name.replace('_', ' ')} fault is latched"
        return text + (f" ({fault.description.rstrip('.')})" if fault is not None and fault.description else "")
    text = condition_text(entry.get("target"), entry.get("condition", "=="), entry.get("value"),
                          entry.get("tolerance"), catalog)
    message = str(entry.get("message") or "").strip()
    return text + (f": “{message}”" if message else "")


def estimate(data: dict, load=None, depth: int = 0) -> tuple:
    """(seconds, open_ended): what the timed parts of a recipe add up to, and whether parts of
    it have no fixed length (waits for a condition or the operator, ramps that start from the
    readback). load(target_file) -> sub-recipe data, or None if not known."""
    seconds, open_ended = 0.0, False
    for step in _list(_dict(data).get("steps")):
        s, o = _step_estimate(step, load, depth)
        seconds += s
        open_ended = open_ended or o
    return seconds, open_ended


def _step_estimate(step, load, depth):
    if not isinstance(step, dict):
        return 0.0, False
    kind = step.get("type")
    if kind == "WAIT_TIME":
        return max(0.0, number(step.get("duration_sec", 0.0)) or 0.0), False
    if kind == "ACTION":
        return 0.0, False
    if kind in ("WAIT_TELEMETRY", "USER_PROMPT", "VERIFIED_ACTION"):
        return 0.0, True
    if kind == "PARALLEL":
        parts = [_step_estimate(s, load, depth) for s in _list(step.get("steps"))]
        return max((p[0] for p in parts), default=0.0), any(p[1] for p in parts)
    if kind == "SUB_RECIPE":
        sub = load(step.get("target_file")) if load is not None and depth < 8 else None
        return estimate(sub, load, depth + 1) if isinstance(sub, dict) else (0.0, True)
    if kind == "ROUTINE_CALL":
        p = _dict(step.get("parameters"))
        name = step.get("routine_name")
        if name == "mass_scan":
            start, stop, size = number(p.get("start_a")), number(p.get("stop_a")), number(p.get("step_a"))
            if None in (start, stop, size) or size <= 0:
                return 0.0, True
            points = int(round(abs(stop - start) / size)) + 1
            samples = number(p.get("samples", 5)) or 5
            reading = max(number(p.get("sample_interval", 0.02)) or 0.0, (number(p.get("nplc", 1.0)) or 1.0) / 50.0)
            per_point = 0.3 + (number(p.get("settle_time", 0.1)) or 0.0) + samples * reading
            return points * per_point + 0.5, False
        if name == "hv_conditioning":
            top, size = number(p.get("max_val")), number(p.get("step_size", 0.5))
            hold = number(p.get("hold_time_sec", 60.0)) or 0.0
            if top is None or not size or size <= 0:
                return 0.0, True
            return math.ceil(abs(top) / size) * hold, True        # from 0: it starts from the readback
        if name == "safety_relay_init":
            return number(p.get("stabilization_sec", 15.0)) or 0.0, True
        return 0.0, True
    return 0.0, False


def describe_recipe(data: dict, catalog=None, recipe_names: dict = None, load=None) -> str:
    """The whole recipe as text: name, version, what stops it, the numbered steps (with their
    notes) and how long the timed parts take."""
    catalog = _catalog(catalog)
    if not isinstance(data, dict):
        return "(not a recipe)"
    lines = [f"{data.get('recipe_name') or '(no name)'} - version {data.get('version', '1.0')}"]
    if str(data.get("description") or "").strip():
        lines.append(str(data["description"]).strip())
    lines.append("")
    policy = _list(data.get("fault_policy"))
    if policy:
        lines.append("It stops if:")
        lines.extend(f"  - {describe_policy_entry(e, catalog)}" for e in policy if isinstance(e, dict))
    else:
        lines.append("It has no stop conditions: it runs to the end unless a step fails or it is stopped.")
    lines.append("")
    steps = _list(data.get("steps"))

    def add(step, number_text, indent):
        lines.append(f"{indent}{number_text} {describe_step(step, catalog, recipe_names)}")
        note = str(step.get("comment") or "").strip() if isinstance(step, dict) else ""
        if note and note != "No description":
            lines.append(f"{indent}{' ' * (len(number_text) + 1)}Note: {note}")
        if isinstance(step, dict) and step.get("type") == "PARALLEL":
            for j, sub in enumerate(_list(step.get("steps")), 1):
                add(sub, f"{number_text[:-1]}.{j}.", indent + "    ")

    for i, step in enumerate(steps, 1):
        add(step, f"{i}.", "")
    if not steps:
        lines.append("(no steps)")
    seconds, open_ended = estimate(data, load)
    lines.append("")
    if seconds and open_ended:
        lines.append(f"The timed parts take about {duration_text(seconds)}; waits for conditions and for the "
                     "operator come on top.")
    elif seconds:
        lines.append(f"It takes about {duration_text(seconds)}.")
    elif open_ended:
        lines.append("How long it takes depends on the waits for conditions and for the operator.")
    return "\n".join(lines)
