"""The operator HMI (offscreen) against the simulated stack: fresh-only telemetry,
command feedback, arm-then-confirm, unknown state when a service stops, recipe
failures with reasons, recipe Stop, and readback tolerance colouring."""
import json
import os
import time

import numpy as np
import pytest

from src.core import commands as C
from tests.sim_stack import start_stack, stop_stack

pytestmark = pytest.mark.slow

SHOTS = os.environ.get("SINDIE_TEST_SHOTS")   # set to a folder to save screenshots of each stage


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    s = start_stack(tmp_path_factory.mktemp("hmistack"))
    yield s
    stop_stack(s)


@pytest.fixture(scope="module")
def app():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def pump(app, seconds):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def wait_for(app, predicate, timeout, what):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        try:
            if predicate():
                return
        except (KeyError, TypeError, AttributeError):
            pass
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def shot(window, name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        window.grab().save(os.path.join(SHOTS, f"{name}.png"))


@pytest.fixture(scope="module")
def hmi(stack, app):
    from src.gui.control.control_main import ControlMainWindow
    w = ControlMainWindow()
    w.resize(1700, 1000)
    w.show()
    wait_for(app, lambda: w.master_telemetry_cache.get("system.connected") is True, 20, "PLC telemetry in HMI")
    wait_for(app, lambda: set(w.master_telemetry_cache.get("manager.services", {}).values()) == {"ONLINE"}, 20,
             "service manager state in HMI")
    yield w
    w.close()
    pump(app, 0.5)


def log_rows(w):
    t = w.cmd_log_widget.table
    return [[t.item(r, c).text() for c in range(t.columnCount())] for r in range(t.rowCount())]


def test_start_up_sends_no_commands(stack, app):
    """First in the module: the relay is still off. An HMI that has just opened sends nothing - the
    services hold the machine's state and it only shows it. In October 2026 it did: the einzel box had
    the first focus and its default 0, the first telemetry locked it (relay off), and as the focus
    hopped down the source panel each box sent its 0 (on the real machine, to a running source).
    Now no control has the focus at start, and a box only sends what the operator typed."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication
    from src.gui.control.control_main import ControlMainWindow
    w = ControlMainWindow()
    sent = []
    real = w.cmd_thread.send_command
    w.cmd_thread.send_command = lambda *a, **k: (sent.append(a), real(*a, **k))[1]
    try:
        w.resize(1700, 1000)
        w.show()
        pump(app, 0.5)
        focused = QApplication.focusWidget()
        assert focused is None or focused is w, f"{type(focused).__name__} has the focus at start-up"
        einzel = w.src_widget.psu_controls["Source Einzel"]["sp_box"]
        einzel.setFocus(Qt.FocusReason.OtherFocusReason)          # as Qt used to give it, or a click
        wait_for(app, lambda: w.master_telemetry_cache.get("system.connected") is True, 20, "PLC telemetry")
        assert w.master_telemetry_cache.get("ion_beam.facilities.safety_relay_active") == 0.0
        wait_for(app, lambda: not einzel.isEnabled(), 10, "einzel box locked (relay off)")
        w.vac_widget.btn_gv_close.setFocus(Qt.FocusReason.MouseFocusReason)   # the operator clicks elsewhere
        pump(app, 1.0)
        assert sent == []
    finally:
        w.close()
        pump(app, 0.3)


def test_fresh_view_and_services(hmi, app):
    view = hmi.master_telemetry_cache
    assert view["system.connected"] is True
    assert set(view["manager.services"].values()) == {"ONLINE"}
    assert "PLC_DATA/system.connected" in view            # generic keys kept per service
    wait_for(app, lambda: len(hmi.service_widget.service_rows) >= 10, 5, "service rows")


def test_gate_valve_and_relay_through_the_gui(hmi, app, stack):
    hmi.cmd_thread.send_command("plc", "ion_beam.source.chamber.cmd_open_gv", True)
    wait_for(app, lambda: hmi.master_telemetry_cache.get("ion_beam.source.chamber.stat_gv_open") == 1.0, 15,
             "gate valve open (needs the simulated vacuum permissive)")
    btn = hmi.health_widget.btn_enable_safety
    wait_for(app, btn.isEnabled, 5, "relay button enabled")
    btn.click()
    wait_for(app, lambda: hmi.master_telemetry_cache.get("ion_beam.facilities.safety_relay_active") == 1.0, 10,
             "relay active")
    assert any(r[3] == "ion_beam.system.cmd_enable_safety" and r[5] == "OK" for r in log_rows(hmi))


def test_einzel_needs_two_clicks_to_enable_one_to_disable(hmi, app):
    from src.gui.control.arming import ARMED_TONE
    from src.gui.style import tone
    ctrl = hmi.src_widget.psu_controls["Source Einzel"]
    btn = ctrl["btn_enable"]
    wait_for(app, btn.isEnabled, 15, "einzel enable button available (Spellman sees the relay)")
    n_before = len(log_rows(hmi))
    btn.click()
    pump(app, 0.5)
    assert btn.text().startswith("CONFIRM") and tone(btn) == ARMED_TONE
    assert not btn.isChecked()
    assert len(log_rows(hmi)) == n_before                 # nothing sent yet
    hmi.src_dock.raise_()
    pump(app, 0.2)
    shot(hmi, "1_einzel_armed")
    btn.click()                                           # confirm
    wait_for(app, lambda: hmi.master_telemetry_cache.get("ion_beam.source.einzel.stat_enabled") == 1.0, 10,
             "einzel enabled")
    pump(app, 0.3)
    assert btn.isChecked() and btn.text() == "ENABLE"
    btn.click()                                           # OFF: single click
    wait_for(app, lambda: hmi.master_telemetry_cache.get("ion_beam.source.einzel.stat_enabled") == 0.0, 10,
             "einzel disabled")


def test_einzel_setpoint_typed_in_the_gui_brings_the_output_up(hmi, app):
    """Typed and Enter, as the operator does: the HMI sends the voltage alone. The Spellman service sets
    the supply's current limit if it holds none (it used to be the HMI's 50 uA, sent only when the
    registry had current limits - an older system_tags.json left the einzel at 0 V and latched an arc)."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    ctrl = hmi.src_widget.psu_controls["Source Einzel"]
    box = ctrl["sp_box"]
    wait_for(app, box.isEnabled, 15, "einzel setpoint box available")
    n_before = len(log_rows(hmi))
    box.setFocus(Qt.FocusReason.MouseFocusReason)
    box.lineEdit().selectAll()
    QTest.keyClicks(box.lineEdit(), "4")
    QTest.keyClick(box.lineEdit(), Qt.Key.Key_Return)

    def since():   # the log is newest-first
        rows = log_rows(hmi)
        return rows[:len(rows) - n_before]
    wait_for(app, lambda: any(r[3] == "ion_beam.source.einzel.sp_requested_voltage" and r[5] == "OK"
                              for r in since()), 5, "voltage accepted")
    assert [r[3] for r in since()] == ["ion_beam.source.einzel.sp_requested_voltage"]
    btn = ctrl["btn_enable"]
    btn.click()
    btn.click()                                           # arm + confirm
    wait_for(app, lambda: abs(hmi.master_telemetry_cache.get("ion_beam.source.einzel.rb_voltage", 0) - 4.0) < 0.1,
             10, "einzel output at 4 kV")
    assert hmi.master_telemetry_cache.get("ion_beam.source.einzel.sp_actual_current") == pytest.approx(50.0)
    btn.click()                                           # off
    wait_for(app, lambda: hmi.master_telemetry_cache.get("ion_beam.source.einzel.stat_enabled") == 0.0, 10,
             "einzel disabled")


def test_arming_times_out(hmi, app):
    btn = hmi.optics_widget.btn_mag_en
    wait_for(app, btn.isEnabled, 10, "magnet enable available")
    btn.click()
    assert btn.text().startswith("CONFIRM")
    pump(app, 4.6)
    assert btn.text() == "ENABLE MAGNET"                  # disarmed by itself, nothing sent
    assert hmi.master_telemetry_cache.get("ion_beam.beamline.magnet.stat_enabled") == 0.0


def test_rejection_is_shown_to_the_operator(hmi, app):
    hmi.cmd_thread.send_command("spellman", "ion_beam.source.einzel.sp_requested_voltage", 99.0)
    wait_for(app, lambda: any(r[5] == "REJECTED" and "Hard cap" in r[6] and "22 kV cap" in r[6]
                              for r in log_rows(hmi)), 5, "rejection logged with the cap's reason")
    hmi.cmd_log_dock.raise_()
    pump(app, 0.2)
    shot(hmi, "2_rejected_command")


def test_stopped_service_shows_unknown_not_last_value(hmi, app, stack):
    from src.gui.control.arming import UNKNOWN_TONE
    from src.gui.style import tone
    ctrl = hmi.src_widget.psu_controls["Source Einzel"]
    wait_for(app, lambda: "---" not in ctrl["lbl_rb"].text(), 10, "einzel readback present")
    r = stack.cmd("manager", "manager_cmd", {"command": "stop", "service": "service_spellman_mpd"})
    assert r["status"] == C.OK
    try:
        wait_for(app, lambda: tone(ctrl["btn_enable"]) == UNKNOWN_TONE, 8, "unknown enable state")
        assert "---" in ctrl["lbl_rb"].text()
        assert "ion_beam.source.einzel.rb_voltage" not in hmi.master_telemetry_cache
        assert hmi.telemetry.get("ion_beam.source.einzel.rb_voltage") is not None   # raw value still kept
        hmi.src_dock.raise_()
        pump(app, 0.2)
        shot(hmi, "3_spellman_stopped_unknown")
    finally:
        stack.cmd("manager", "manager_cmd", {"command": "start", "service": "service_spellman_mpd"})
    wait_for(app, lambda: hmi.master_telemetry_cache.get("ion_beam.source.einzel.stat_enabled") is not None, 20,
             "einzel back")


RECIPE_DONE = ("complete", "stopped", "failed")


def _write_recipe(name, steps):
    from src.core.recipe_files import RECIPES_DIR     # the tests' copy of config/recipes
    with open(os.path.join(RECIPES_DIR, name), "w") as f:
        json.dump({"recipe_name": name[:-5], "steps": steps}, f)


def _start_recipe(hmi, app, name, steps):
    """Pick, load and start a recipe in the Recipe panel; it runs in the sequencer service."""
    _write_recipe(name, steps)
    w, view = hmi.recipe_widget, hmi.master_telemetry_cache
    w.select_recipe(name)
    w.btn_load.click()
    wait_for(app, w.btn_start.isEnabled, 15, "recipe START available (sequencer online, idle)")
    before = view.get("sequencer.recipe.run")
    w.btn_start.click()
    wait_for(app, lambda: view.get("sequencer.recipe.run") not in (None, before)
             and view.get("sequencer.recipe.file") == name, 10, "recipe started in the sequencer")


def _recipe_result(hmi, app, timeout=20):
    view = hmi.master_telemetry_cache
    wait_for(app, lambda: view.get("sequencer.recipe.state") in RECIPE_DONE, timeout, "recipe to finish")
    return view["sequencer.recipe.state"] == "complete", view["sequencer.recipe.message"]


def _run_recipe(hmi, app, steps, stop_after=None, name="hmi_test.json"):
    _start_recipe(hmi, app, name, steps)
    if stop_after is not None:
        pump(app, stop_after)
        hmi.recipe_widget.btn_abort.click()
    return _recipe_result(hmi, app)


def test_recipe_step_fails_with_the_services_reason(hmi, app):
    ok, msg = _run_recipe(hmi, app, [
        {"step_id": 1, "type": "ACTION", "target": "ion_beam.source.extraction.sp_requested_voltage", "value": 50.0}])
    assert not ok and "Step 1 failed" in msg and "outside the allowed range" in msg
    assert "Step 1 failed" in hmi.recipe_widget.lbl_status.text()


def test_recipe_waits_only_on_live_values_and_routes_by_registry(hmi, app):
    ok, msg = _run_recipe(hmi, app, [
        # VERIFIED_ACTION on a PLC tag used to be sent to the Spellman service and time out
        {"step_id": 1, "type": "VERIFIED_ACTION", "target": "ion_beam.source.target.sp_requested_voltage",
         "value": 2.0, "pre_target": "ion_beam.facilities.safety_relay_active", "pre_condition": "==",
         "pre_value": 1.0, "verify_target": "ion_beam.source.target.sp_actual_voltage", "verify_condition": ">=",
         "verify_value": 1.99, "timeout_sec": 5},
        {"step_id": 2, "type": "WAIT_TELEMETRY", "target": "ion_beam.source.target.sp_actual_voltage",
         "condition": ">=", "value": 1.99, "timeout_sec": 5}])
    assert ok, msg


def test_recipe_stop_leaves_outputs_alone(hmi, app):
    ok, msg = _run_recipe(hmi, app, [
        {"step_id": 1, "type": "ACTION", "target": "ion_beam.source.target.sp_requested_voltage", "value": 3.0},
        {"step_id": 2, "type": "WAIT_TIME", "duration_sec": 30}], stop_after=1.5)
    assert not ok and "Stopped by operator" in msg
    pump(app, 0.5)
    assert hmi.master_telemetry_cache.get("ion_beam.source.target.sp_actual_voltage") == pytest.approx(3.0)


def test_recipe_prompt_is_answered_in_the_panel(hmi, app):
    """The prompt is a banner in the panel (not a blocking dialog); the sequencer checks the
    step's conditions and the panel shows why an acknowledgement was not accepted."""
    w = hmi.recipe_widget
    _start_recipe(hmi, app, "hmi_prompt.json", [
        {"step_id": 1, "type": "USER_PROMPT", "prompt_text": "Close the gate valve",
         "verify_conditions": [{"target": "ion_beam.source.chamber.stat_gv_closed", "condition": "==", "value": 1.0}]},
        {"step_id": 2, "type": "USER_PROMPT", "prompt_text": "All done?"}])
    wait_for(app, w.prompt_frame.isVisible, 10, "prompt banner")
    assert "Close the gate valve" in w.lbl_prompt.text()
    hmi.recipe_dock.raise_()
    w.btn_ack.click()
    wait_for(app, lambda: "stat_gv_closed" in w.lbl_prompt_feedback.text(), 5, "refusal shown")
    shot(hmi, "5_recipe_prompt_refused")
    w.btn_prompt_stop.click()                     # leave the gate valve open: stop here instead
    ok, msg = _recipe_result(hmi, app)
    assert not ok and "Stopped by operator at step 1" in msg
    wait_for(app, lambda: not w.prompt_frame.isVisible(), 5, "banner gone")

    _start_recipe(hmi, app, "hmi_prompt2.json", [{"step_id": 1, "type": "USER_PROMPT", "prompt_text": "OK?"}])
    wait_for(app, w.prompt_frame.isVisible, 10, "second prompt")
    w.btn_ack.click()
    assert _recipe_result(hmi, app)[0]


def test_operator_takes_control_from_a_recipe(hmi, app, stack):
    """A running recipe holds what it drives: the operator is refused (the Automation bar says by
    what), takes control with two clicks, and the recipe pauses until resumed."""
    view = hmi.master_telemetry_cache
    _start_recipe(hmi, app, "hmi_hold.json", [
        {"step_id": 1, "type": "ACTION", "target": "ion_beam.beamline.steerer_magnet.sp_requested_current",
         "value": 2.0},
        {"step_id": 2, "type": "WAIT_TIME", "duration_sec": 60}])
    bar = hmi.automation_bar
    wait_for(app, lambda: "recipe 'hmi_hold'" in bar.groups, 10, "lease shown in the Automation bar")
    assert bar.groups["recipe 'hmi_hold'"] == ["ion_beam.beamline.steerer_magnet"]
    r = stack.cmd("steerer_magnet", "ion_beam.beamline.steerer_magnet.sp_requested_current", 3.0)
    assert r["status"] == C.REJECTED and "recipe 'hmi_hold'" in r["reason"]
    wait_for(app, lambda: not hmi.optics_widget.sp_x_steer.isEnabled(), 5, "steerer box locked")
    assert "recipe 'hmi_hold'" in hmi.optics_widget.sp_x_steer.toolTip()
    shot(hmi, "6_automation_bar_lease")
    _label, button = bar.rows["recipe 'hmi_hold'"][1:3]
    button.click()
    button.click()                                # arm + confirm
    wait_for(app, lambda: view.get("sequencer.recipe.state") == "paused", 5, "recipe paused")
    assert "operator took control" in view["sequencer.recipe.status"]
    wait_for(app, lambda: not bar.groups, 5, "Automation bar cleared")
    assert stack.cmd("steerer_magnet", "ion_beam.beamline.steerer_magnet.sp_requested_current", 3.0)["status"] == C.OK
    hmi.recipe_widget.btn_abort.click()
    assert not _recipe_result(hmi, app)[0]


def test_tolerance_colours_the_readback(hmi, app, stack):
    import src.sim as sim
    sim.FAULTS_FILE = str(stack.data_dir / "sim_faults.txt")
    assert stack.cmd("magnet", "ion_beam.beamline.magnet.sp_requested_current", 25.0)["status"] == C.OK
    assert stack.cmd("magnet", "ion_beam.beamline.magnet.cmd_enable", True)["status"] == C.OK
    wait_for(app, lambda: abs(hmi.master_telemetry_cache["ion_beam.beamline.magnet.rb_current"] - 25) < 0.2, 15,
             "magnet at 25 A")
    sim.set_faults({"magnet_sag"})
    try:
        wait_for(app, lambda: hmi.optics_widget.lbl_mag_rb.property("tolerance") == "out", 20, "amber readback")
        hmi.optics_dock.raise_()
        pump(app, 0.3)
        shot(hmi, "4_magnet_out_of_tolerance")
    finally:
        sim.set_faults(set())
    wait_for(app, lambda: not hmi.optics_widget.lbl_mag_rb.property("tolerance"), 20, "colour cleared")


def test_calculate_and_set_amps_puts_the_mass_on_the_cup(hmi, app, stack):
    """Optics 'Calculate & set amps' with the Mass Scan panel's calibration lands on the Si-28
    peak (the simulated beam uses the same model: k = 1391.9, I_off = 0.223, E = target + extraction).
    The simulated source only makes beam with > 200 W thermionic, > 4 kV target and > 50 degC cesium."""
    from src.core.mass_calibration import current_for_mass
    for tag, value in (("ion_beam.source.extraction.sp_requested_voltage", 15.0),
                       ("ion_beam.source.target.sp_requested_voltage", 5.0),
                       ("ion_beam.source.extraction.cmd_enable", True),
                       ("ion_beam.source.target.cmd_enable", True),
                       ("ion_beam.source.cesium.sp_requested_temp", 150.0),
                       ("ion_beam.source.cesium.cmd_ctrl_mode", 1),
                       ("ion_beam.source.filament.sp_requested_current", 28.8),
                       ("ion_beam.source.filament.cmd_enable", True),
                       ("ion_beam.source.thermionic.cmd_enable", True)):
        assert stack.cmd("plc", tag, value)["status"] == C.OK, tag
    assert stack.cmd("magnet", "ion_beam.beamline.magnet.cmd_enable", True)["status"] == C.OK
    view = hmi.master_telemetry_cache
    wait_for(app, lambda: abs(view["ion_beam.source.extraction.rb_voltage"] - 15.0) < 0.05
             and abs(view["ion_beam.source.target.rb_voltage"] - 5.0) < 0.05, 15, "20 keV beam")
    cal = hmi.mass_scan_widget.current_calibration()
    assert cal["source"].startswith("default")
    wait_for(app, hmi.optics_widget.btn_calc_mass.isEnabled, 5, "Calculate & set amps available")
    hmi.optics_widget.sp_mass.setValue(28.0)
    hmi.optics_widget.btn_calc_mass.click()
    expected = current_for_mass(28.0, 20000.0, cal["k"], cal["i_off"])
    wait_for(app, lambda: abs(view["ion_beam.beamline.magnet.rb_current"] - expected) < 0.05, 15,
             f"magnet at {expected:.2f} A")
    wait_for(app, lambda: view["ion_beam.source.thermionic.rb_power"] > 210
             and view["ion_beam.source.cesium.rb_temp"] > 52, 60, "source running (thermionic, cesium)")
    wait_for(app, lambda: view["ion_beam.beamline.faraday.smu.rb_current"] > 100e-9, 20, "Si-28 beam on the cup")


def test_mass_scan_runs_in_the_sequencer_and_the_panel_follows_it(hmi, app):
    """After the calculate-and-set test: beam on the cup. A short scan across Si-28 from the
    Mass Scan panel runs in the sequencer (holding the magnet and the SMU), the panel plots it,
    the sequencer saves it, and the SMU is put back to NPLC 1."""
    w, view = hmi.mass_scan_widget, hmi.master_telemetry_cache
    for box, value in ((w.sp_start, 19.5), (w.sp_stop, 20.2), (w.sp_step, 0.1), (w.sp_settle, 0.05),
                       (w.sp_samples, 2), (w.sp_interval, 0.02), (w.sp_nplc, 1.0)):
        box.setValue(value)
    w.chk_loop.setChecked(False)
    w.chk_export.setChecked(True)
    wait_for(app, w.btn_start.isEnabled, 10, "mass scan START available")
    w.btn_start.click()
    wait_for(app, lambda: view.get("sequencer.mass_scan.state") == "running", 10, "scan running")
    wait_for(app, lambda: set(view.get("sequencer.mass_scan.leases") or []) ==
             {"ion_beam.beamline.magnet", "ion_beam.beamline.faraday.smu"}, 10, "magnet and SMU held")
    wait_for(app, lambda: not hmi.optics_widget.sp_mag_current.isEnabled(), 5,
             "the operator's magnet box locked while the scan holds the magnet")
    hmi.mass_scan_dock.raise_()
    wait_for(app, lambda: view.get("sequencer.mass_scan.state") == "complete", 90, "scan complete")
    wait_for(app, lambda: len(w.last_scan_data) == 8, 10, "the whole pass analysed in the panel")
    shot(hmi, "7_mass_scan_from_the_sequencer")
    assert len(w.amps_data) == 8 and max(w.y_data) > 50e-9
    saved = view.get("sequencer.mass_scan.file")
    assert saved and os.path.exists(saved)
    wait_for(app, lambda: view.get("ion_beam.beamline.faraday.smu.rb_nplc") == 1.0, 10, "NPLC restored")
    wait_for(app, lambda: hmi.optics_widget.sp_mag_current.isEnabled(), 10, "magnet back to the operator")


def test_optimiser_sweeps_and_restores(hmi, app, stack):
    w, view = hmi.optimiser_widget, hmi.master_telemetry_cache
    assert stack.cmd("steerer_magnet", "ion_beam.beamline.steerer_magnet.cmd_enable", True)["status"] == C.OK
    before = view["ion_beam.beamline.magnet.sp_actual_current"]
    w.set_axis("X", "ion_beam.beamline.magnet.sp_requested_current")
    w.set_axis("Y", "ion_beam.beamline.steerer_magnet.sp_requested_current")
    for box, value in ((w.sp_x_start, 19.6), (w.sp_x_stop, 20.0), (w.sp_x_steps, 2), (w.sp_y_start, 1.0),
                       (w.sp_y_stop, 2.0), (w.sp_y_steps, 2), (w.sp_dwell, 0.2)):
        box.setValue(value)
    try:
        wait_for(app, w.btn_start.isEnabled, 10, "optimiser START available")
    except AssertionError:
        raise AssertionError(f"optimiser START not available: {w.btn_start.toolTip()}")
    w.btn_start.click()
    wait_for(app, lambda: view.get("sequencer.optimiser.state") == "complete", 30, "sweep complete")
    wait_for(app, lambda: w.heatmap_data is not None and not np.isnan(w.heatmap_data).any(), 5, "map filled")
    wait_for(app, lambda: abs(view["ion_beam.beamline.magnet.sp_actual_current"] - before) < 0.01, 10,
             "magnet restored")


def test_closing_the_gate_valve_trips_the_relay(hmi, app, stack):
    """Last in this module: it drops the relay. The PLC trips the relay on the gate-valve
    readback; the supplies go off and stay off (latched) until the relay is enabled again."""
    view = hmi.master_telemetry_cache
    assert view["ion_beam.facilities.safety_relay_active"] == 1.0
    assert view["ion_beam.source.target.stat_enabled"] == 1.0
    hmi.vac_widget.btn_gv_close.click()
    wait_for(app, lambda: view.get("ion_beam.facilities.safety_relay_active") == 0.0, 10, "relay tripped")
    # ... and the Alarms panel: unacknowledged, critical, sounding, first out
    panel = hmi.alarm_widget
    wait_for(app, lambda: any(a["id"] == "plc.Safety_Relay_Tripped" and a["state"] == "UNACK"
                              for a in panel.alarms), 10, "relay-trip alarm")
    trip = next(a for a in panel.alarms if a["id"] == "plc.Safety_Relay_Tripped")
    assert trip["priority"] == 1 and panel.horn.playing == 1
    hmi.alarm_dock.raise_()
    pump(app, 0.3)
    shot(hmi, "8_relay_trip_alarm")
    panel.btn_ack_all.click()
    wait_for(app, lambda: all(a["state"] != "UNACK" for a in panel.alarms), 5, "alarms acknowledged")
    assert panel.horn.playing is None
    wait_for(app, lambda: view.get("ion_beam.source.target.stat_enabled") == 0.0
             and view.get("ion_beam.source.einzel.stat_enabled") == 0.0, 10, "supplies off")
    assert "Safety_Relay_Tripped" in {f["name"] for f in hmi.fault_engine.get_active_faults(
        "UDT_Fault_Word_0_System", int(view["ion_beam.faults.word_0_system"]))}
    hmi.vac_widget.btn_gv_open.click()
    wait_for(app, lambda: view.get("ion_beam.source.chamber.stat_gv_open") == 1.0, 15, "gate valve open again")
    pump(app, 2)
    assert view["ion_beam.facilities.safety_relay_active"] == 0.0              # not back by itself
