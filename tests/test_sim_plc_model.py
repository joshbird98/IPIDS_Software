"""The simulated PLC's machine model (src/sim/plc.py), run on a fake clock: the behaviour
Josh described for the real source (Oct 2026)."""
import random

import pytest

from src.sim.plc import SimulatedPLC, emission_ma, target_beam_ma

S = "ion_beam.source."
RELAY = "ion_beam.facilities.safety_relay_active"


class FakeClock:
    def __init__(self):
        self.last = 0.0

    def dt(self, max_dt=1.0):
        self.last += 0.1
        return 0.1


@pytest.fixture
def plc():
    random.seed(20261018)       # the model adds measurement noise: the same noise every run
    p = SimulatedPLC()
    p.clock = FakeClock()
    return p


def run(p, seconds, vac_ok=True):
    for _ in range(int(round(seconds / 0.1))):
        p.set("ion_beam.system.plc_watchdog", (p.get("ion_beam.system.plc_watchdog") or 0) + 1)
        p.set(S + "chamber.stat_vac_ok_for_gv", vac_ok)
        p.step()


def energise(p):
    p.set(S + "chamber.cmd_open_gv", 1)
    run(p, 3)
    p.set("ion_beam.system.cmd_enable_safety", 1)
    run(p, 1)
    assert p.get(RELAY) == 1.0


def fault_active(p, name):
    for udt in p.faults.values():
        m = p._meta(udt.get("registry_tag"))
        if m is None:
            continue
        for pos, bit in udt.get("bits", {}).items():
            if bit["name"] == name:
                byte_idx, bit_idx = (int(x) for x in pos.split("."))
                return bool(p.dbs[m["db_number"]][m["byte_offset"] + byte_idx] >> bit_idx & 1)
    raise KeyError(name)


def test_emission_and_sputter_curves():
    assert emission_ma(25.0) == pytest.approx(3.0)
    assert emission_ma(29.0) == pytest.approx(150.0)
    assert emission_ma(10.0) == 0.0
    assert target_beam_ma(199.0, 8.0, 100.0) == 0.0          # each threshold matters
    assert target_beam_ma(250.0, 3.9, 100.0) == 0.0
    assert target_beam_ma(250.0, 8.0, 49.0) == 0.0
    assert target_beam_ma(200.1, 4.01, 50.1) == pytest.approx(0.1, rel=0.02)
    assert target_beam_ma(250.0, 8.0, 100.0) == pytest.approx(1.0)


def test_relay_needs_the_gate_valve_open(plc):
    plc.set("ion_beam.system.cmd_enable_safety", 1)
    run(plc, 1)
    assert plc.get(RELAY) == 0.0
    assert plc.get("ion_beam.system.cmd_enable_safety") == 0.0   # request cleared
    assert fault_active(plc, "Safety_Relay_Refused")


def test_closing_the_gate_valve_trips_the_relay_and_it_stays_off(plc):
    energise(plc)
    for tag, v in (("target.sp_requested_voltage", 6.0), ("target.cmd_enable", 1),
                   ("cesium.sp_requested_temp", 80.0), ("cesium.cmd_ctrl_mode", 1)):
        plc.set(S + tag, v)
    run(plc, 3)
    assert plc.get(S + "target.rb_voltage") == pytest.approx(6.0, abs=0.1)
    plc.set(S + "chamber.cmd_open_gv", 0)
    run(plc, 2)
    assert plc.get(RELAY) == 0.0
    assert fault_active(plc, "Safety_Relay_Tripped")
    assert plc.get(S + "target.stat_enabled") == 0.0 and plc.get(S + "target.cmd_enable") == 0.0
    assert plc.get(S + "target.rb_voltage") < 0.1
    assert plc.get(S + "cesium.stat_enabled") == 0.0 and plc.get(S + "cesium.cmd_ctrl_mode") == 0.0
    plc.set(S + "chamber.cmd_open_gv", 1)                     # valve back open: still off ...
    run(plc, 4)
    assert plc.get(RELAY) == 0.0 and fault_active(plc, "Safety_Relay_Tripped")
    plc.set("ion_beam.system.cmd_fault_reset", 1)              # ... until reset and re-enabled
    run(plc, 0.2)
    assert not fault_active(plc, "Safety_Relay_Tripped")
    plc.set("ion_beam.system.cmd_enable_safety", 1)
    run(plc, 1)
    assert plc.get(RELAY) == 1.0
    assert plc.get(S + "target.stat_enabled") == 0.0          # supplies don't come back by themselves


def test_vacuum_loss_closes_the_valve_and_trips_the_relay(plc):
    energise(plc)
    run(plc, 1, vac_ok=False)
    assert plc.get(S + "chamber.stat_gv_open") == 0.0
    assert plc.get(RELAY) == 0.0


def test_thermionic_2500_v_and_emission_follows_the_filament(plc):
    energise(plc)
    plc.set(S + "thermionic.cmd_enable", 1)
    run(plc, 2)
    assert plc.get(S + "thermionic.rb_voltage") == pytest.approx(2500.0, rel=0.01)
    assert plc.get(S + "thermionic.rb_current") == 0.0        # cold filament: no emission
    plc.set(S + "filament.sp_requested_current", 25.0)
    plc.set(S + "filament.cmd_enable", 1)
    run(plc, 40)
    assert plc.get(S + "thermionic.rb_current") == pytest.approx(3.0, rel=0.1)


def test_auto_emission_drives_the_filament(plc):
    energise(plc)
    for tag, v in (("thermionic.cmd_enable", 1), ("filament.sp_requested_current", 24.0),
                   ("filament.cmd_enable", 1), ("thermionic.sp_requested_current", 80.0)):
        plc.set(S + tag, v)
    run(plc, 30)
    plc.set(S + "thermionic.cmd_auto_emission", 1)
    run(plc, 90)
    assert plc.get(S + "thermionic.stat_auto_emission") == 1.0
    assert plc.get(S + "thermionic.rb_current") == pytest.approx(80.0, rel=0.06)
    fil = plc.get(S + "filament.sp_actual_current")
    assert 28.0 < fil < 28.6                                   # ~28.3 A gives 80 mA
    assert plc.get(S + "filament.sp_requested_current") == 24.0   # the operator's value is ignored ...
    plc.set(S + "thermionic.cmd_auto_emission", 0)
    run(plc, 1)
    assert plc.get(S + "filament.sp_requested_current") == pytest.approx(fil, abs=0.05)  # ... bumpless back


def test_cesium_heats_to_setpoint_and_forced_cooling_goes_to_20(plc):
    energise(plc)
    plc.set(S + "cesium.sp_requested_temp", 60.0)
    plc.set(S + "cesium.cmd_ctrl_mode", 1)
    run(plc, 400)
    assert plc.get(S + "cesium.rb_temp") == pytest.approx(60.0, abs=0.5)
    plc.set(S + "cesium.cmd_ctrl_mode", 0)
    plc.set(S + "cesium.cmd_force_cooling", 1)
    run(plc, 400)
    assert plc.get(S + "cesium.rb_temp") == pytest.approx(20.0, abs=0.5)


def test_bleed_and_sputter_currents(plc):
    energise(plc)
    for tag, v in (("extraction.sp_requested_voltage", 15.0), ("extraction.cmd_enable", 1),
                   ("target.sp_requested_voltage", 8.0), ("target.cmd_enable", 1)):
        plc.set(S + tag, v)
    run(plc, 4)
    assert plc.get(S + "target.rb_current") == pytest.approx(8.0 / 50.0, rel=0.02)       # bleed only
    assert plc.get(S + "extraction.rb_current") == pytest.approx(15.0 / 50.0, rel=0.02)
    # full blast: 250 W thermionic, 100 degC cesium
    plc.set(S + "cesium.estimated_temp", 100.0)
    plc.set(S + "cesium.sp_requested_temp", 100.0)
    plc.set(S + "cesium.cmd_ctrl_mode", 1)
    plc.set(S + "thermionic.sp_requested_current", 100.0)
    for tag in ("thermionic.cmd_enable", "filament.cmd_enable", "thermionic.cmd_auto_emission"):
        plc.set(S + tag, 1)
    plc.set(S + "filament.sp_requested_current", 27.0)
    run(plc, 120)
    beams, extracted = [], []
    for _ in range(50):                  # averaged over 5 s: one reading carries 5 % noise
        run(plc, 0.1)
        beams.append(plc.get(S + "target.rb_current") - 8.0 / 50.0)
        extracted.append(plc.get(S + "extraction.rb_current") - 15.0 / 50.0)
    beam = sum(beams) / len(beams)
    assert beam == pytest.approx(1.0, rel=0.15)
    assert sum(extracted) / len(extracted) == pytest.approx(0.1 * beam, rel=0.25)


def test_source_deck_has_no_readback_without_the_relay(plc):
    energise(plc)
    plc.set(S + "cesium.sp_requested_temp", 60.0)
    plc.set(S + "cesium.cmd_ctrl_mode", 1)
    plc.set(S + "thermionic.cmd_enable", 1)
    run(plc, 60)
    assert plc.get(S + "cesium.rb_temp") > 40.0
    assert plc.get(S + "thermionic.rb_voltage") > 2000.0
    plc.set("ion_beam.system.cmd_enable_safety", 0)            # relay off: deck unpowered
    run(plc, 1)
    assert plc.get(S + "cesium.rb_temp") == 0.0 and plc.get(S + "thermionic.rb_voltage") == 0.0
    est = plc.get(S + "cesium.estimated_temp")
    assert est > 40.0                                          # the PLC's model carries on ...
    run(plc, 60)
    assert plc.get(S + "cesium.estimated_temp") < est          # ... cooling towards ambient


def test_hot_cesium_keeps_target_and_ioniser_on(plc):
    from src.sim.plc import CS_HOT_C
    energise(plc)
    for tag, v in (("target.sp_requested_voltage", 6.0), ("target.cmd_enable", 1),
                   ("thermionic.cmd_enable", 1), ("filament.sp_requested_current", 20.0),
                   ("filament.cmd_enable", 1)):
        plc.set(S + tag, v)
    run(plc, 3)
    plc.set(S + "cesium.estimated_temp", CS_HOT_C + 30.0)     # hot cesium
    run(plc, 0.5)
    assert plc.get("ion_beam.internal.cs_is_hot") == 1.0
    for name in ("target", "thermionic", "filament"):
        plc.set(S + f"{name}.cmd_enable", 0)                   # switch-off requests are held back
    plc.set(S + "target.sp_requested_voltage", 0.1)            # and the target kept >= its minimum
    run(plc, 3)
    for name in ("target", "thermionic", "filament"):
        assert plc.get(S + f"{name}.stat_enabled") == 1.0, name
    assert plc.get(S + "target.rb_voltage") == pytest.approx(
        plc.get("ion_beam.internal.target_min_voltage"), abs=0.05)
    plc.set(S + "cesium.cmd_force_cooling", 1)                 # once it has cooled they follow
    run(plc, 240)
    assert plc.get("ion_beam.internal.cs_is_hot") == 0.0
    for name in ("target", "thermionic", "filament"):
        assert plc.get(S + f"{name}.stat_enabled") == 0.0, name


def test_machine_state_and_the_close_permissive(plc):
    """The interlock bits the Interlocks panel reads (docs/how-it-works/plc.md): Ready only with the
    relay permissive made, Live with the relay closed; the source cold only with no high voltage and
    the filament and the caesium cold."""
    run(plc, 0.5)
    assert plc.get("ion_beam.system.machine_state") == 1.0          # gate valve closed: not ready
    plc.set(S + "chamber.cmd_open_gv", 1)
    run(plc, 3)
    assert plc.get("ion_beam.system.machine_state") == 2.0          # ready
    plc.set("ion_beam.system.cmd_enable_safety", 1)
    run(plc, 1)
    assert plc.get("ion_beam.system.machine_state") == 3.0
    assert plc.get(S + "chamber.stat_gv_close_permissive") == 1.0
    plc.set(S + "filament.sp_requested_current", 5.0)
    plc.set(S + "filament.cmd_enable", 1)
    run(plc, 6)
    assert plc.get("ion_beam.internal.fil_is_hot") == 1.0
    assert plc.get(S + "chamber.stat_gv_close_permissive") == 0.0   # hot: the PLC would hold the relay
    plc.set(S + "filament.cmd_enable", 0)
    run(plc, 8)
    assert plc.get("ion_beam.internal.fil_is_hot") == 0.0
    plc.set(S + "extraction.sp_requested_voltage", 5.0)
    plc.set(S + "extraction.cmd_enable", 1)
    run(plc, 3)
    assert plc.get(S + "chamber.stat_gv_close_permissive") == 0.0   # high voltage
    assert plc.get(S + "cesium.stat_lockout") == 0.0 and plc.get("ion_beam.internal.cs_heating_permissive") == 1.0


def test_ready_is_the_documented_relay_permissive_alone(plc, monkeypatch):
    """Machine state 2 follows the relay permissive plc.md describes, not the simulator's own
    shortcuts (INITIATE SHUTDOWN and the plc_relay_trip fault drop the relay here)."""
    plc.set(S + "chamber.cmd_open_gv", 1)
    run(plc, 3)
    monkeypatch.setenv("SINDIE_SIM_FAULTS", "plc_relay_trip")
    run(plc, 0.3)
    assert plc.get("ion_beam.system.machine_state") == 2.0
    plc.set("ion_beam.system.cmd_source_shutdown", 1)
    run(plc, 0.3)
    assert plc.get("ion_beam.system.machine_state") == 2.0


def test_an_open_safety_loop_refuses_the_relay_after_5_s(plc, monkeypatch):
    """A door, an ESO or the key switch open: the PLC gives its enable, nothing closes, and after
    5 s it latches Safety_Relay_Refused and drops the request. A closed relay opens: tripped."""
    plc.set(S + "chamber.cmd_open_gv", 1)
    run(plc, 3)
    monkeypatch.setenv("SINDIE_SIM_FAULTS", "safety_loop_open")
    plc.set("ion_beam.system.cmd_enable_safety", 1)
    run(plc, 3)
    assert plc.get(RELAY) == 0.0 and plc.get("ion_beam.system.cmd_enable_safety") == 1.0
    assert 2000 < plc.get("ion_beam.system.safety_timer_elapsed_ms") < 5000      # PRESS PANEL START
    assert not fault_active(plc, "Safety_Relay_Refused")
    run(plc, 2.5)
    assert plc.get(RELAY) == 0.0 and plc.get("ion_beam.system.cmd_enable_safety") == 0.0
    assert fault_active(plc, "Safety_Relay_Refused") and plc.get("ion_beam.system.machine_state") == 99.0
    monkeypatch.delenv("SINDIE_SIM_FAULTS")
    plc.set("ion_beam.system.cmd_fault_reset", 1)
    plc.set("ion_beam.system.cmd_enable_safety", 1)
    run(plc, 1)
    assert plc.get(RELAY) == 1.0
    monkeypatch.setenv("SINDIE_SIM_FAULTS", "safety_loop_open")       # an ESO pressed
    run(plc, 0.3)
    assert plc.get(RELAY) == 0.0 and fault_active(plc, "Safety_Relay_Tripped")


def test_low_air_drops_the_relay_and_closes_the_gate_valve(plc, monkeypatch):
    energise(plc)
    monkeypatch.setenv("SINDIE_SIM_FAULTS", "air_low")
    run(plc, 0.5)
    assert plc.get("ion_beam.facilities.stat_air_press_ok") == 0.0
    assert plc.get(RELAY) == 0.0 and plc.get(S + "chamber.stat_gv_open") == 0.0
    assert fault_active(plc, "Air_Pressure_Low")


def test_a_gauge_fault_withholds_the_open_permissive(plc):
    run(plc, 0.5)
    assert plc.get(S + "chamber.stat_gv_open_permissive") == 1.0
    plc.set(S + "vacuum_gauge_1.stat_above_sp", 1)
    run(plc, 0.2)
    assert plc.get(S + "chamber.stat_gv_open_permissive") == 0.0 and fault_active(plc, "VG1_Above_SP_Warn")
