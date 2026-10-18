"""The operator HMI (offscreen) against the simulated stack: fresh-only telemetry,
command feedback, arm-then-confirm, unknown state when a service stops, recipe
failures with reasons, recipe Stop, and readback tolerance colouring."""
import json
import os
import time

import pytest

from src.core import commands as C
from tests.sim_stack import start_stack, stop_stack

pytestmark = pytest.mark.slow

SHOTS = os.environ.get("IPIDS_TEST_SHOTS")   # set to a folder to save screenshots of each stage


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
    from src.gui.control.arming import ARMED_STYLE
    ctrl = hmi.src_widget.psu_controls["Source Einzel"]
    btn = ctrl["btn_enable"]
    wait_for(app, btn.isEnabled, 15, "einzel enable button available (Spellman sees the relay)")
    n_before = len(log_rows(hmi))
    btn.click()
    pump(app, 0.5)
    assert btn.text().startswith("CONFIRM") and btn.styleSheet() == ARMED_STYLE
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


def test_einzel_setpoint_from_the_gui_brings_its_current_limit(hmi, app):
    """The HMI sends a 50 uA current limit with each einzel voltage (it was always refused before
    the limits were in mpd_config.json); without it the supply's output can't come up."""
    ctrl = hmi.src_widget.psu_controls["Source Einzel"]
    wait_for(app, ctrl["sp_box"].isEnabled, 15, "einzel setpoint box available")
    n_before = len(log_rows(hmi))
    ctrl["sp_box"].setValue(4.0)
    ctrl["sp_box"].editingFinished.emit()
    wanted = {"ion_beam.source.einzel.sp_requested_voltage", "ion_beam.source.einzel.sp_requested_current"}
    def accepted_since():   # the log is newest-first
        rows = log_rows(hmi)
        return {r[3] for r in rows[:len(rows) - n_before] if r[5] == "OK"}
    wait_for(app, lambda: wanted <= accepted_since(), 5, "voltage and current limit accepted")
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
    wait_for(app, lambda: any(r[5] == "REJECTED" and "range" in r[6] for r in log_rows(hmi)), 5, "rejection logged")
    hmi.cmd_log_dock.raise_()
    pump(app, 0.2)
    shot(hmi, "2_rejected_command")


def test_stopped_service_shows_unknown_not_last_value(hmi, app, stack):
    from src.gui.control.arming import UNKNOWN_STYLE
    ctrl = hmi.src_widget.psu_controls["Source Einzel"]
    wait_for(app, lambda: "---" not in ctrl["lbl_rb"].text(), 10, "einzel readback present")
    r = stack.cmd("manager", "manager_cmd", {"command": "stop", "service": "service_spellman_mpd"})
    assert r["status"] == C.OK
    try:
        wait_for(app, lambda: ctrl["btn_enable"].styleSheet() == UNKNOWN_STYLE, 8, "unknown enable state")
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


def _run_recipe(hmi, app, tmp_path, steps, stop_after=None):
    path = tmp_path / "recipe.json"
    path.write_text(json.dumps({"recipe_name": "test", "steps": steps}))
    finished = []
    hmi.recipe_worker.sig_recipe_finished.connect(lambda ok, msg: finished.append((ok, msg)))
    hmi.recipe_worker.load_recipe(str(path))
    hmi.recipe_worker.play()
    if stop_after is not None:
        pump(app, stop_after)
        hmi.recipe_worker.stop()
    wait_for(app, lambda: finished, 20, "recipe to finish")
    hmi.recipe_worker.sig_recipe_finished.disconnect()
    hmi.recipe_widget._wire_signals()
    hmi.recipe_worker.wait(3000)
    return finished[0]


def test_recipe_step_fails_with_the_services_reason(hmi, app, tmp_path):
    ok, msg = _run_recipe(hmi, app, tmp_path, [
        {"step_id": 1, "type": "ACTION", "target": "ion_beam.source.extraction.sp_requested_voltage", "value": 50.0}])
    assert not ok and "Step 1 failed" in msg and "outside the allowed range" in msg


def test_recipe_waits_only_on_live_values_and_routes_by_registry(hmi, app, tmp_path):
    ok, msg = _run_recipe(hmi, app, tmp_path, [
        # VERIFIED_ACTION on a PLC tag used to be sent to the Spellman service and time out
        {"step_id": 1, "type": "VERIFIED_ACTION", "target": "ion_beam.source.target.sp_requested_voltage",
         "value": 2.0, "pre_target": "ion_beam.facilities.safety_relay_active", "pre_condition": "==",
         "pre_value": 1.0, "verify_target": "ion_beam.source.target.sp_actual_voltage", "verify_condition": ">=",
         "verify_value": 1.99, "timeout_sec": 5},
        {"step_id": 2, "type": "WAIT_TELEMETRY", "target": "ion_beam.source.target.sp_actual_voltage",
         "condition": ">=", "value": 1.99, "timeout_sec": 5}])
    assert ok, msg


def test_recipe_stop_leaves_outputs_alone(hmi, app, tmp_path):
    ok, msg = _run_recipe(hmi, app, tmp_path, [
        {"step_id": 1, "type": "ACTION", "target": "ion_beam.source.target.sp_requested_voltage", "value": 3.0},
        {"step_id": 2, "type": "WAIT_TIME", "duration_sec": 30}], stop_after=1.5)
    assert not ok and "Stopped by operator" in msg
    pump(app, 0.5)
    assert hmi.master_telemetry_cache.get("ion_beam.source.target.sp_actual_voltage") == pytest.approx(3.0)


def test_tolerance_colours_the_readback(hmi, app, stack):
    import src.sim as sim
    from src.gui.control.widgets.source_widget import TOLERANCE_STYLE
    sim.FAULTS_FILE = str(stack.data_dir / "sim_faults.txt")
    assert stack.cmd("magnet", "ion_beam.beamline.magnet.sp_requested_current", 25.0)["status"] == C.OK
    assert stack.cmd("magnet", "ion_beam.beamline.magnet.cmd_enable", True)["status"] == C.OK
    wait_for(app, lambda: abs(hmi.master_telemetry_cache["ion_beam.beamline.magnet.rb_current"] - 25) < 0.2, 15,
             "magnet at 25 A")
    sim.set_faults({"magnet_sag"})
    try:
        wait_for(app, lambda: hmi.optics_widget.lbl_mag_rb.styleSheet() == TOLERANCE_STYLE, 20, "amber readback")
        hmi.optics_dock.raise_()
        pump(app, 0.3)
        shot(hmi, "4_magnet_out_of_tolerance")
    finally:
        sim.set_faults(set())
    wait_for(app, lambda: hmi.optics_widget.lbl_mag_rb.styleSheet() == "", 20, "colour cleared")


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


def test_closing_the_gate_valve_trips_the_relay(hmi, app, stack):
    """Last in this module: it drops the relay. The PLC trips the relay on the gate-valve
    readback; the supplies go off and stay off (latched) until the relay is enabled again."""
    view = hmi.master_telemetry_cache
    assert view["ion_beam.facilities.safety_relay_active"] == 1.0
    assert view["ion_beam.source.target.stat_enabled"] == 1.0
    hmi.vac_widget.btn_gv_close.click()
    wait_for(app, lambda: view.get("ion_beam.facilities.safety_relay_active") == 0.0, 10, "relay tripped")
    wait_for(app, lambda: view.get("ion_beam.source.target.stat_enabled") == 0.0
             and view.get("ion_beam.source.einzel.stat_enabled") == 0.0, 10, "supplies off")
    assert "Safety_Relay_Tripped" in {f["name"] for f in hmi.fault_engine.get_active_faults(
        "UDT_Fault_Word_0_System", int(view["ion_beam.faults.word_0_system"]))}
    hmi.vac_widget.btn_gv_open.click()
    wait_for(app, lambda: view.get("ion_beam.source.chamber.stat_gv_open") == 1.0, 15, "gate valve open again")
    pump(app, 2)
    assert view["ion_beam.facilities.safety_relay_active"] == 0.0              # not back by itself
