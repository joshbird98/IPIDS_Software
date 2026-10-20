"""The recipe checks (src/core/recipe_check.py) and the plain-English text (src/core/recipe_text.py):
what the builder, the Recipe Execution panel's LOAD and the sequencer's start say about a recipe."""
import json
import os

import pytest

from src.core import paths
from src.core.recipe_catalog import Catalog
from src.core.recipe_check import ERROR, WARNING, check_file, check_recipe, errors
from src.core.recipe_text import describe_policy_entry, describe_recipe, describe_step, duration_text, estimate

EXT = "ion_beam.source.extraction"
MAG = "ion_beam.beamline.magnet"
RELAY = "ion_beam.facilities.safety_relay_active"
VG1 = "ion_beam.source.vacuum_gauge_1.rb_pressure"
RELAY_TRIP = {"type": "BIT_FAULT", "registry_tag": "ion_beam.faults.word_0_system", "bit_str": "0.1",
              "name": "Safety_Relay_Tripped", "action": "ABORT"}


@pytest.fixture(scope="module")
def catalog():
    return Catalog.load()


def recipe(*steps, policy=None, name="t"):
    data = {"recipe_name": name, "version": "1.0", "steps": [dict(s, step_id=i) for i, s in enumerate(steps, 1)]}
    if policy is not None:
        data["fault_policy"] = policy
    return data


def act(target, value):
    return {"type": "ACTION", "target": target, "value": value}


def wait_for(target, condition, value, timeout=30):
    return {"type": "WAIT_TELEMETRY", "target": target, "condition": condition, "value": value, "timeout_sec": timeout}


RELAY_ON = {"type": "ROUTINE_CALL", "routine_name": "safety_relay_init", "parameters": {}}


def messages(problems, level=None):
    return [p.text() for p in problems if level is None or p.level == level]


def test_every_shipped_recipe_has_no_errors(catalog):
    folder = os.path.join(paths.CONFIG_DIR, "recipes")
    for name in sorted(os.listdir(folder)):
        if name.endswith(".json"):
            assert not errors(check_file(os.path.join(folder, name), catalog, folder)), name


@pytest.mark.parametrize("step, why", [
    (act(f"{EXT}.sp_nonsense", 1), "isn't a tag the machine knows"),
    (act(f"{EXT}.rb_voltage", 1), "can't be set by a recipe: a reading or a status, not a command"),
    (act("ion_beam.source.einzel.sp_requested_voltage", 25.0),
     "Hard cap: the source einzel voltage setpoint 25 kV is above the 22 kV cap"),
    (act(f"{EXT}.sp_requested_voltage", 21.0), "21 kV is outside the extraction voltage setpoint's range, 0 to 20 kV"),
    (act(f"{EXT}.sp_requested_voltage", "15"), "needs a number, not '15'"),
    (act(f"{EXT}.cmd_enable", 2), "is on or off (true or false), not 2"),
    (act("ion_beam.source.turbo_pump.cmd_enable", False), "never stopped from the software"),
    (act("ion_beam.system.cmd_fault_reset", True), "recipes may not reset faults"),
    (act("ion_beam.source.cesium.cmd_ctrl_mode", 2), "caesium oven heater is on or off"),
    (act("ion_beam.source.extraction.cmd_ctrl_mode", 0), "control modes aren't set by recipes"),
    ({"type": "WAIT_TIME", "duration_sec": -1}, "duration_sec can't be below 0"),
    ({"type": "WAIT_TELEMETRY", "target": VG1, "condition": "<<", "value": 1}, "unknown condition '<<'"),
    ({"type": "SUB_RECIPE", "target_file": "nowhere.json"}, "there is no recipe nowhere.json"),
    ({"type": "ROUTINE_CALL", "routine_name": "mass_scan", "parameters": {"start_a": 20, "stop_a": 120, "step_a": 1}},
     "stop_a 120 A is above the 95 A cap"),
    ({"type": "ROUTINE_CALL", "routine_name": "psu_control",
      "parameters": {"cmd_enable": f"{EXT}.cmd_enable", "stat_enabled": f"{EXT}.stat_enabled",
                     "sp_tag": f"{EXT}.sp_requested_voltage", "target_val": 30}},
     "the value to set: 30 kV is outside the extraction voltage setpoint's range, 0 to 20 kV"),
    ({"type": "PARALLEL", "steps": [{"type": "SUB_RECIPE", "target_file": "x.json", "step_id": 9}]},
     "a SUB_RECIPE step can't run inside a PARALLEL step"),
])
def test_errors(catalog, step, why):
    problems = check_recipe(recipe(step), catalog)
    assert any(why in m for m in messages(problems, ERROR)), messages(problems)


def test_problems_say_where_they_are(catalog):
    data = recipe(act(f"{EXT}.sp_nonsense", 1),
                  {"type": "PARALLEL", "steps": [{"type": "WAIT_TIME", "duration_sec": 1, "step_id": 21},
                                                 dict(act(f"{EXT}.sp_bad", 1), step_id=22)]},
                  policy=[RELAY_TRIP, {"type": "CONDITION", "target": VG1, "condition": "==", "value": 1e-6}])
    found = {(p.path, p.where) for p in check_recipe(data, catalog)}
    assert ((0,), "step 1") in found and ((1, 1), "step 2.2") in found
    assert (("policy", 1), "stop condition 2") in found          # an analogue value compared with ==


def test_without_the_registry_only_the_engine_checks_are_made():
    data = recipe(act("anything.at.all", 1), {"type": "WAIT_TIME", "duration_sec": "soon"})
    assert messages(check_recipe(data, Catalog({}, {}))) == ["step 2: duration_sec must be a number, not 'soon'"]


def test_a_good_start_up_has_no_problems(catalog):
    data = recipe(
        wait_for(VG1, "<", 5e-7, timeout=3600), RELAY_ON,
        {"type": "ROUTINE_CALL", "routine_name": "psu_control",
         "parameters": {"cmd_enable": f"{EXT}.cmd_enable", "stat_enabled": f"{EXT}.stat_enabled",
                        "sp_tag": f"{EXT}.sp_requested_voltage", "rb_tag": f"{EXT}.rb_voltage", "target_val": 15.0,
                        "tolerance": 0.2, "timeout_sec": 60}},
        policy=[RELAY_TRIP])
    assert messages(check_recipe(data, catalog)) == []


@pytest.mark.parametrize("steps, policy, why", [
    ([RELAY_ON, act(f"{EXT}.sp_requested_voltage", 15.0), act(f"{EXT}.cmd_enable", True)], [RELAY_TRIP],
     "step 2: this setpoint is sent before a later step switches the extraction on: the PLC zeroes"),
    ([act(f"{EXT}.cmd_enable", True)], [RELAY_TRIP],
     "step 1: this switches on the extraction, which needs the safety relay, but no step before it makes sure"),
    ([RELAY_ON, act(f"{EXT}.cmd_enable", True)], [],
     "stop conditions: the recipe switches on outputs that need the safety relay, but doesn't stop if the relay "
     "trips: add Safety_Relay_Tripped"),
    ([act("ion_beam.source.chamber.cmd_open_gv", True)], [],
     "step 1: the gate valve is opened with no wait for the source vacuum"),
    ([act("ion_beam.beamline.diagnostics.cmd_insert_fc", False), act(f"{MAG}.cmd_degauss", True)], [],
     "step 2: the degauss sweeps the magnet with the Faraday cup out"),
    ([{"type": "ROUTINE_CALL", "routine_name": "mass_scan", "parameters": {"start_a": 20, "stop_a": 30,
                                                                             "step_a": 0.1}}], [],
     "step 1: the scan needs the analyser magnet on, the Faraday cup in and the cup bias on (above 50 V)"),
    ([wait_for(f"{EXT}.rb_voltage", "==", 15.0)], [], "an analogue reading (the extraction voltage) is rarely"),
    ([wait_for(VG1, "<", 1e-6, timeout=0)], [], "step 1: no time limit"),
    ([act("ion_beam.system.cmd_enable_safety", True)], [], "use 'Bring the safety relay on'"),
    ([RELAY_ON, act("ion_beam.source.thermionic.cmd_auto_emission", True),
      act("ion_beam.source.filament.sp_requested_current", 10.0)], [RELAY_TRIP], "step 3: auto-emission is on"),
    ([{"type": "ROUTINE_CALL", "routine_name": "psu_control", "parameters": {
        "cmd_enable": "ion_beam.source.thermionic.cmd_enable", "stat_enabled": "ion_beam.source.thermionic.stat_enabled",
        "sp_tag": "ion_beam.source.thermionic.sp_requested_current",
        "rb_tag": "ion_beam.source.thermionic.rb_current"}}], [RELAY_TRIP],
     "the thermionic (ioniser) current follows the filament"),
    ([{"type": "ROUTINE_CALL", "routine_name": "hv_conditioning", "parameters": {
        "cmd_tag": f"{EXT}.sp_requested_voltage", "rb_tag": f"{EXT}.rb_voltage", "enabled_tag": f"{EXT}.stat_enabled",
        "max_val": 15, "pressure_max": 9e-6, "pressure_recover": 2e-6}}], [RELAY_TRIP],
     "the gauge's relay drops at 8.0e-06 mbar"),
    ([{"type": "VERIFIED_ACTION", "target": f"{EXT}.sp_requested_voltage", "value": 5.0}], [],
     "it checks the command itself"),
    ([{"type": "PARALLEL", "steps": [dict(act(f"{MAG}.sp_requested_current", 5), step_id=11),
                                     dict(act(f"{MAG}.cmd_enable", True), step_id=12)]}], [],
     "step 1.2: another step at the same time also drives the analyser magnet"),
])
def test_warnings(catalog, steps, policy, why):
    problems = check_recipe(recipe(*steps, policy=policy), catalog)
    assert not errors(problems), messages(problems)
    assert any(why in m for m in messages(problems, WARNING)), messages(problems)


def test_a_sub_recipes_steps_count_for_the_order_and_its_errors_for_the_caller(catalog, tmp_path):
    (tmp_path / "relay.json").write_text(json.dumps(recipe(RELAY_ON, name="relay")))
    (tmp_path / "bad.json").write_text(json.dumps(recipe(act(f"{EXT}.sp_requested_voltage", 50), name="bad")))
    data = recipe({"type": "SUB_RECIPE", "target_file": "relay.json"}, act(f"{EXT}.cmd_enable", True),
                  policy=[RELAY_TRIP])
    assert messages(check_recipe(data, catalog, str(tmp_path))) == []         # the relay is seen to in relay.json
    data = recipe({"type": "SUB_RECIPE", "target_file": "bad.json"})
    assert messages(check_recipe(data, catalog, str(tmp_path)), ERROR) == [
        "step 1: in bad.json: step 1: 50 kV is outside the extraction voltage setpoint's range, 0 to 20 kV"]


def test_recipes_calling_each_other_are_an_error(catalog, tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(recipe({"type": "SUB_RECIPE", "target_file": "b.json"}, name="a")))
    (tmp_path / "b.json").write_text(json.dumps(recipe({"type": "SUB_RECIPE", "target_file": "a.json"}, name="b")))
    problems = check_file(str(tmp_path / "a.json"), catalog, str(tmp_path))
    assert messages(problems, ERROR) == ["step 1: in b.json: step 1: the recipes call each other in a circle "
                                         "(a.json > b.json > a.json), so it would never end"]


# ---------------------------------------------------------------------------- the text
@pytest.mark.parametrize("step, text", [
    (act(f"{EXT}.sp_requested_voltage", 15), "Set the extraction voltage setpoint to 15 kV"),
    (act(f"{EXT}.cmd_enable", True), "Switch the extraction on"),
    (act("ion_beam.beamline.diagnostics.cmd_insert_fc", True), "Insert the Faraday cup"),
    (act(f"{MAG}.cmd_degauss", True), "Degauss the analyser magnet"),
    (act("ion_beam.beamline.image_slits.left.cmd_range", 2),
     "Set the image slits left current monitor range to 2 (500mV)"),
    ({"type": "WAIT_TIME", "duration_sec": 150}, "Wait 2 min 30 s"),
    (wait_for(VG1, "<", 5e-7, timeout=0), "Wait until the source pressure (VG1) is below 5e-07 mbar (no time limit)"),
    (wait_for(RELAY, "==", 1, timeout=10), "Wait until the safety relay is on (fails after 10 s)"),
    (wait_for(f"{EXT}.rb_voltage", "within", 15) | {"tolerance": 0.2},
     "Wait until the extraction voltage is within ±0.2 kV of 15 kV (fails after 30 s)"),
    ({"type": "USER_PROMPT", "prompt_text": "Close the door",
      "verify_conditions": [{"target": "ion_beam.source.chamber.stat_gv_open", "condition": "==", "value": 1}]},
     "Ask the operator: “Close the door” (OK only once the source gate valve is open)"),
    ({"type": "VERIFIED_ACTION", "target": "ion_beam.beamline.diagnostics.cmd_insert_fc", "value": True,
      "verify_target": "ion_beam.beamline.diagnostics.stat_fc_inserted", "timeout_sec": 10},
     "Insert the Faraday cup, then check that the Faraday cup is in (within 10 s)"),
    ({"type": "SUB_RECIPE", "target_file": "sub.json"}, "Run the recipe sub.json"),
    ({"type": "PARALLEL", "steps": [{}, {}]}, "Do 2 steps at the same time"),
    ({"type": "ROUTINE_CALL", "routine_name": "mass_scan", "parameters": {"start_a": 20, "stop_a": 30, "step_a": 0.05}},
     "Mass scan: the magnet from 20 A to 30 A in 0.05 A steps (201 points), then set it on the biggest peak"),
    ({"type": "ROUTINE_CALL", "routine_name": "psu_control",
      "parameters": {"cmd_enable": f"{EXT}.cmd_enable", "sp_tag": f"{EXT}.sp_requested_voltage", "target_val": 15,
                     "rb_tag": f"{EXT}.rb_voltage", "tolerance": 0.2, "timeout_sec": 60}},
     "Switch the extraction on and set it to 15 kV, then wait up to 1 min for the readback to be within ±0.2 kV"),
    ({"type": "ROUTINE_CALL", "routine_name": "safety_relay_init", "parameters": {}},
     "Bring the safety relay on, with the operator (the gate valve opened once the source pressure is below "
     "5e-07 mbar; START pressed within 6 s; then 15 s to settle)"),
    ({"type": "TELEPORT"}, "TELEPORT step (a step type the sequencer doesn't know)"),
    ({"type": "ACTION"}, "Set (no tag chosen)"),
])
def test_step_text(catalog, step, text):
    assert describe_step(step, catalog) == text


def test_policy_text(catalog):
    assert describe_policy_entry(RELAY_TRIP, catalog) == ("the Safety Relay Tripped fault is latched (Hardware safety "
                                                          "loop broken. HV disabled)")
    assert describe_policy_entry({"target": RELAY, "condition": "==", "value": 0, "message": "Relay lost"},
                                 catalog) == "the safety relay is off: “Relay lost”"


def test_recipe_text_and_time(catalog):
    data = recipe({"type": "WAIT_TIME", "duration_sec": 90, "comment": "Let it settle"},
                  {"type": "PARALLEL", "steps": [{"type": "WAIT_TIME", "duration_sec": 30, "step_id": 21},
                                                 dict(wait_for(VG1, "<", 1e-6), step_id=22)]},
                  policy=[RELAY_TRIP])
    text = describe_recipe(data, catalog)
    assert text.splitlines() == [
        "t - version 1.0", "", "It stops if:",
        "  - the Safety Relay Tripped fault is latched (Hardware safety loop broken. HV disabled)", "",
        "1. Wait 1 min 30 s", "   Note: Let it settle", "2. Do 2 steps at the same time",
        "    2.1. Wait 30 s", "    2.2. Wait until the source pressure (VG1) is below 1e-06 mbar (fails after 30 s)",
        "", "The timed parts take about 2 min; waits for conditions and for the operator come on top."]
    assert estimate(recipe({"type": "WAIT_TIME", "duration_sec": 5})) == (5.0, False)
    assert duration_text(3900) == "1 h 05 min" and duration_text(0.5) == "0.5 s"


# ============================================================================ found in review
def test_malformed_steps_are_errors_not_exceptions(catalog, tmp_path):
    """The order checks and the text walked steps the engine had refused, and raised on them."""
    from src.core.recipe_text import describe_recipe, estimate
    bad = recipe({"type": "ROUTINE_CALL", "routine_name": "psu_control", "parameters": ["x"]},
                 {"type": "USER_PROMPT", "prompt_text": "x", "verify_conditions": 5},
                 {"type": "PARALLEL", "steps": 5})
    found = messages(check_recipe(bad, catalog), ERROR)
    assert len(found) == 3, found
    describe_recipe(bad, catalog)
    estimate(bad)
    (tmp_path / "bad.json").write_text(json.dumps(bad))
    caller = recipe({"type": "SUB_RECIPE", "target_file": "bad.json"})
    assert any(m.startswith("step 1: in bad.json:") for m in messages(check_recipe(caller, catalog, str(tmp_path))))


def test_a_step_type_or_stop_condition_that_isnt_text_or_an_object_is_an_error_not_an_exception(catalog):
    """A hand-edited file, or a draft: the checks, the text and the builder must say so, not raise."""
    from src.core.recipe_text import describe_policy_entry, describe_recipe
    bad = recipe({"type": ["WAIT_TIME"]}, {"type": "PARALLEL", "steps": [{"type": {"a": 1}}]},
                 {"type": "ROUTINE_CALL", "routine_name": ["psu_control"], "parameters": {}})
    bad["fault_policy"] = ["Safety_Relay_Tripped"]
    found = messages(check_recipe(bad, catalog), ERROR)
    assert any("unknown step type ['WAIT_TIME']" in m for m in found), found
    assert any(m.startswith("step 2.1: unknown step type") for m in found), found
    assert any("unknown routine ['psu_control']" in m for m in found), found
    assert any(m.startswith("stop conditions:") for m in found), found
    describe_recipe(bad, catalog)
    assert "not a stop condition" in describe_policy_entry("Safety_Relay_Tripped", catalog)


def test_a_sub_recipe_in_the_windows_code_page_is_read_as_the_sequencer_reads_it(catalog, tmp_path):
    (tmp_path / "ansi.json").write_bytes(json.dumps(recipe(
        {"type": "WAIT_TIME", "duration_sec": 1, "comment": "warm to 40 °C"}), ensure_ascii=False).encode("cp1252"))
    caller = recipe({"type": "SUB_RECIPE", "target_file": "ansi.json"})
    assert messages(check_recipe(caller, catalog, str(tmp_path))) == []


def test_the_turbo_start_is_no_routines_tag_and_its_reset_isnt_offered(catalog):
    psu = {"type": "ROUTINE_CALL", "routine_name": "psu_control", "parameters": {
        "cmd_enable": f"{EXT}.cmd_enable", "stat_enabled": f"{EXT}.stat_enabled",
        "sp_tag": "ion_beam.source.turbo_pump.cmd_enable", "target_val": 0}}
    assert any("turbo pump's start isn't a routine's to drive" in m for m in messages(check_recipe(recipe(psu),
                                                                                                  catalog), ERROR))
    assert "ion_beam.source.turbo_pump.cmd_reset" not in {t.name for t in catalog.settable()}


def test_messages_name_steps_by_their_own_numbers(catalog):
    data = {"recipe_name": "r", "steps": [{"step_id": 10, "type": "WAIT_TIME", "duration_sec": 1},
                                          {"step_id": 20, "type": "ACTION", "target": "nope.cmd_x", "value": 1}]}
    assert messages(check_recipe(data, catalog), ERROR) == [
        "step 20: nope.cmd_x isn't a tag the machine knows (it isn't in the tag registry)"]


def test_small_numbers_keep_their_digits():
    from src.core.recipe_text import fmt
    from src.gui.control.dialogs.recipe_builder.fields import show_number
    assert fmt(1.26e-7, "mbar") == "1.26e-07 mbar" and fmt(5e-7) == "5e-07" and fmt(15.0, "kV") == "15 kV"
    assert show_number(1.234e-7) == "1.234e-07" and show_number(20.0) == "20" and show_number(0.05) == "0.05"
