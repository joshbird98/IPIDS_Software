"""The interlock overview's logic (src/core/interlocks.py): every permissive, what holds it, and what
the software can and can't tell about the safety relay's hardwired loop."""
import json
import os

import pytest

from src.core import interlocks as il
from src.core.paths import config_path

ALL_FAULTS = ["Safety_Relay_Refused", "Safety_Relay_Tripped", "Air_Pressure_Low", "Src_Coolant_Flow_Low",
              "Mag_Coolant_Flow_Low", "PC_Watchdog_Timeout", "Remote_IO_Lost", "Cesium_Oven_OverTemp",
              "Cesium_Oven_HeatFail", "Cesium_Oven_CoolFail", "Graphix_1_Comms_Fail", "Graphix_2_Comms_Fail"] + \
             [f"VG{n}_{f}" for n in (1, 2) for f in il.VG_FAULTS]
NOW = 1_800_000_000.0           # (epoch seconds: when the tests look)
VG = {1: "ion_beam.source.vacuum_gauge_1", 2: "ion_beam.beamline.vacuum_gauge_2", 3: "ion_beam.beamline.vacuum_gauge_3"}


def machine(**changes) -> dict:
    """The live view of a machine at rest: everything known, the gate valve shut, the relay open, the
    source cold, no fault latched, every gauge relay on controller 1 closed."""
    d = {"system.connected": True, "vacuum.connected": True, "VACUUM_DATA/system.connected": 1.0,
         il.RELAY: 0.0, il.ENABLE: 0.0, il.TIMER: 0.0, il.TIMER_TOTAL: 5000.0,
         il.AIR: 1.0, il.SRC_COOLANT: 1.0, il.MAG_COOLANT: 1.0, il.GV_OPEN: 0.0, il.GV_CLOSED: 1.0,
         il.GV_OPEN_PERMISSIVE: 1.0, il.GV_CLOSE_PERMISSIVE: 1.0, il.GV_REQUEST: 0.0, il.VAC_OK: 1.0,
         il.VAC_HOLD_LEFT: 0.0, il.KILL: 0.0, il.SEQ_GV: 1.0, il.FORCE_COOLER: 0.0, il.CS_HOT: 0.0, il.FIL_HOT: 0.0,
         il.CS_HEATING_PERMISSIVE: 0.0, il.MACHINE_STATE: 1.0, il.COMMS_LOST: 0.0, il.REMOTE_IO_LOST: 0.0,
         il.AUTO_SHUTDOWN: 0.0, il.REMOTE_STEP: 0.0, il.PC_STEP: 0.0,
         "ion_beam.source.extraction.rb_voltage": 0.0, "ion_beam.source.target.rb_voltage": 0.0,
         "ion_beam.source.cesium.estimated_temp": 22.0, "ion_beam.source.cesium.cmd_ctrl_mode": 0.0,
         "ion_beam.source.cesium.stat_lockout": 1.0, "ion_beam.source.cesium.stat_enabled": 0.0,
         "ion_beam.vacuum.stat_judged": 1.0, "ion_beam.vacuum.stat_relay_sp_mismatch": 0.0}
    d.update({f"faults.{n}": 0.0 for n in ALL_FAULTS})
    for s in ("extraction", "target", "filament", "thermionic"):
        d.update({f"ion_beam.source.{s}.stat_lockout": 1.0, f"ion_beam.source.{s}.ctrl_mode": 0.0,
                  f"ion_beam.source.{s}.stat_enabled": 0.0})
    d.update({il.relay_status_tag(10, r): 1.0 for r in range(1, 7)})
    d.update({il.relay_read_at_tag(10, r): NOW - 3.0 for r in range(1, 7)})      # read 3 s ago
    for n, p in ((1, 2.1e-7), (2, 3.4e-7), (3, 5.2e-7)):
        d.update({f"{VG[n]}.rb_pressure": p, f"{VG[n]}.stat_comms_fail": 0.0, f"{VG[n]}.stat_not_found": 0.0,
                  f"{VG[n]}.stat_mismatch": 0.0})
    d.update(changes)
    return d


def loop_config(wired=None) -> il.LoopConfig:
    with open(config_path("vac_gauges_config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["system_interlocks"]["safety_loop_relays"] = wired or []
    return il.LoopConfig.parse(cfg)


def overview(data, wired=None, **kw) -> il.Overview:
    kw.setdefault("now", NOW)
    return il.evaluate(data, loop_config(wired), **kw)


def cond(group: il.Group, label: str) -> il.Condition:
    return next(c for c in group.conditions if c.label.startswith(label))


def test_no_data_is_unknown_everywhere_and_never_fails():
    ov = il.evaluate({})
    assert not ov.plc_online and ov.machine_name == "PLC OFFLINE" and ov.machine_level == il.LEVEL_UNKNOWN
    for g in ov.groups:
        assert g.status == "UNKNOWN" and g.first is None, g.key
        assert not any(c.state == il.OK for c in g.conditions), g.key
    assert cond(ov.group("relay"), "Gauge controller relay").detail == "no data from the vacuum service"


def test_at_rest_the_gate_valve_holds_the_relay_off():
    ov = overview(machine())
    relay = ov.group("relay")
    assert relay.status == "OFF" and relay.level == il.LEVEL_NORMAL
    assert relay.conditions[relay.first].label == "Gate valve open"
    assert relay.summary == "Held off by the PLC: the gate valve is closed."
    assert cond(relay, "ENABLE SAFETY RELAY").state == il.OFF
    assert not relay.notes and not relay.disagree                   # machine state 1 agrees: not ready
    gv = ov.group("gv_open")
    assert gv.status == "CLOSED" and gv.summary.startswith("Ready to open") and gv.first is None
    assert ov.group("source_cold").status == "COLD"
    supplies = ov.group("supplies")
    assert supplies.status == "HELD" and supplies.summary == "Held off: the safety relay is open."
    assert ov.group("cesium").summary == "Can't heat: the safety relay is open."
    assert ov.group("reset").status == "NONE LATCHED"
    assert ov.machine_name == "SAFE / NOT READY" and "Machine state 1" in ov.machine


def test_ready_and_asked_for():
    ov = overview(machine(**{il.GV_OPEN: 1.0, il.GV_CLOSED: 0.0, il.MACHINE_STATE: 2.0}))
    relay = ov.group("relay")
    assert relay.first is None and relay.summary.startswith("The PLC's conditions hold: press ENABLE")
    ov = overview(machine(**{il.GV_OPEN: 1.0, il.GV_CLOSED: 0.0, il.MACHINE_STATE: 2.0, il.ENABLE: 1.0,
                             il.TIMER: 1800.0}))
    relay = ov.group("relay")
    assert relay.status == "PRESS START" and relay.level == il.LEVEL_ATTENTION
    assert "press START" in relay.summary and "(4 s left)" in relay.summary
    assert cond(relay, "Doors").state == il.HIDDEN and "waiting for START" in cond(relay, "Doors").detail


def refused(**changes) -> dict:
    return machine(**{il.GV_OPEN: 1.0, il.GV_CLOSED: 0.0, il.MACHINE_STATE: 99.0,
                      "faults.Safety_Relay_Refused": 1.0, **changes})


def test_a_refusal_rules_out_what_the_pc_can_see():
    """Josh: the doors, ESOs and key switch are in series and can't be told apart - the PLC only sees
    that it gave its enable for 5 s and nothing closed - but the gauge controller's relay can be
    ruled out from the vacuum service."""
    relay = overview(refused()).group("relay")
    assert relay.status == "REFUSED" and relay.level == il.LEVEL_FAULT
    assert cond(relay, "Doors").state == il.SUSPECT
    assert cond(relay, "Gauge controller relay").state == il.OK
    assert cond(relay, "Gauge controller relay").detail.startswith(
        "all 6 relays on gauge controller 1 closed")
    (text, level), = relay.notes
    assert level == "warn"
    assert text == ("The relay didn't close within 5 s of the PLC's enable (Safety_Relay_Refused). Ruled out: the "
                    "gauge controller relay (vacuum) and the magnet coolant switch (closed now, so not holding the "
                    "loop open). That leaves: a door, an ESO or the key switch is open; or START wasn't pressed "
                    "within the 5 s. The software can't tell which. Check them, then RESET FAULTS and try again.")
    assert relay.summary == "The PLC's conditions hold now: RESET FAULTS, then try again."


def test_a_refusal_with_the_loops_gauge_relay_open():
    data = refused(**{il.relay_status_tag(10, 4): 0.0, f"{VG[1]}.rb_pressure": 9.1e-6})
    relay = overview(data, wired=["10.4"]).group("relay")
    vac = cond(relay, "Gauge controller relay")
    assert vac.state == il.HOLDING
    assert vac.detail == "open: relay 4: VG1 at 9.1E-06 mbar, above the 8.0E-06 at which it opens"
    assert cond(relay, "Doors").state == il.HIDDEN               # not suspected: the vacuum explains it
    assert relay.summary == ("The PLC's conditions hold, but the relay can't close: the gauge controller's relay in "
                             "the loop is open (relay 4: VG1 at 9.1E-06 mbar, above the 8.0E-06 at which it opens).")
    (text, _), = relay.notes
    assert "Now the gauge controller's relay in the loop is open (relay 4: " in text
    assert "that alone holds the loop open. A door, an ESO or the key switch could be open as well" in text
    # Which relay is in the loop not set: an open relay on controller 1 might be it - or the doors...
    relay = overview(data).group("relay")
    vac = cond(relay, "Gauge controller relay")
    assert vac.state == il.SUSPECT and vac.detail == (
        "open: relays 1 and 4: VG1 at 9.1E-06 mbar, above the 8.0E-06 at which they open. Which relay is wired into "
        "the loop isn't set in vac_gauges_config.json: if it is one of these, the loop is open")
    assert cond(relay, "Doors").state == il.SUSPECT
    (text, _), = relay.notes
    assert ("That leaves: a door, an ESO or the key switch is open; or START wasn't pressed within the 5 s; or an "
            "open relay on gauge controller 1, if it is the one wired into the loop (relays 1 and 4: VG1 at "
            "9.1E-06 mbar, above the 8.0E-06 at which they open). The software can't tell which.") in text
    # ... and one that isn't in the loop (set) doesn't matter
    data.update({il.relay_status_tag(10, 4): 1.0, f"{VG[1]}.rb_pressure": 2.1e-7, il.relay_status_tag(10, 3): 0.0})
    assert cond(overview(data, wired=["10.4"]).group("relay"), "Gauge controller relay").state == il.OK


def test_the_gauge_relay_is_judged_on_a_current_reading():
    """Ruled out only on a current reading (the review of 21 October): its state read within
    RELAY_RECENT_S, its gauge answering, and the pressure on the closed side of its set points."""
    def vac(**changes):
        return cond(overview(refused(**changes), wired=["10.4"]).group("relay"), "Gauge controller relay")
    ok = vac()
    assert ok.state == il.OK and ok.detail == "closed: relay 4: VG1 at 2.1E-07 mbar (it opens above 8.0E-06)"
    # In the band between its set points the relay keeps its state: closed as read
    band = vac(**{f"{VG[1]}.rb_pressure": 7.0e-6})
    assert band.state == il.OK and "between its set points" in band.detail
    # ... or open as read
    assert vac(**{f"{VG[1]}.rb_pressure": 7.0e-6, il.relay_status_tag(10, 4): 0.0}).state == il.HOLDING
    for changes, why in [
            ({il.relay_read_at_tag(10, 4): NOW - 40.0}, "relay 4: last read 40 s ago"),
            ({il.relay_status_tag(10, 4): None}, "relay 4: not read recently"),
            ({il.relay_read_at_tag(10, 4): None}, "relay 4: not read recently"),     # (from before a restart)
            ({f"{VG[1]}.stat_comms_fail": 1.0}, "relay 4: read closed 3 s ago, but VG1 isn't answering"),
            ({f"{VG[1]}.stat_comms_fail": None}, "relay 4: read closed 3 s ago, but VG1 isn't answering"),
            ({f"{VG[1]}.rb_pressure": None}, "relay 4: read closed 3 s ago, but VG1 has no live reading"),
            ({f"{VG[1]}.stat_not_found": 1.0}, "relay 4: read closed 3 s ago, but VG1 reports a fault"),
            ({il.VAC_MISMATCH: 1.0}, "its set points aren't confirmed on the controller yet"),
            ({il.VAC_MISMATCH: None}, "its set points aren't confirmed on the controller yet"),
            ({il.relay_status_tag(10, 4): 0.0}, "relay 4: read open 3 s ago, but VG1 at 2.1E-07 mbar is below the "
                                                "5.5E-06 at which it closes (the readings disagree)")]:
        c = vac(**changes)
        assert c.state == il.UNKNOWN and why in c.detail, (changes, c.detail)
    # A pressure above the off point says open, whatever was read before
    high = vac(**{f"{VG[1]}.rb_pressure": 2.0e-5, il.relay_read_at_tag(10, 4): NOW - 40.0})
    assert high.state == il.HOLDING and "above the 8.0E-06 at which it opens" in high.detail
    # Unknown: the doors stay hidden, and the note lists the relay with them
    relay = overview(refused(**{il.relay_status_tag(10, 4): None}), wired=["10.4"]).group("relay")
    assert cond(relay, "Doors").state == il.SUSPECT
    assert ("or the gauge controller relay (vacuum) (not known now: relay 4: not read recently)"
            in relay.notes[0][0])


def test_with_the_relay_closed_an_open_gauge_relay_isnt_in_the_loop():
    data = machine(**{il.RELAY: 1.0, il.ENABLE: 1.0, il.GV_OPEN: 1.0, il.GV_CLOSED: 0.0, il.MACHINE_STATE: 3.0,
                      il.relay_status_tag(10, 3): 0.0, f"{VG[3]}.rb_pressure": 9.0e-6})
    vac = cond(overview(data).group("relay"), "Gauge controller relay")
    assert vac.state == il.OK and vac.detail.startswith("the loop is made, so these open relays aren't in it "
                                                        "(relays 3 and 6: VG3 at 9.0E-06 mbar")
    relay = overview(data, wired=["10.3"]).group("relay")             # set wrongly
    assert cond(relay, "Gauge controller relay").state == il.HOLDING
    assert "check system_interlocks.safety_loop_relays" in relay.disagree
    # Not read lately, or no vacuum data: with the relay closed the loop is made all the same
    data.update({il.relay_status_tag(10, 4): None, "vacuum.connected": True})
    vac = cond(overview(data, wired=["10.4"]).group("relay"), "Gauge controller relay")
    assert vac.state == il.OK and vac.detail == "the loop is made (relay 4: not read recently)"
    data["vacuum.connected"] = False
    vac = cond(overview(data, wired=["10.4"]).group("relay"), "Gauge controller relay")
    assert vac.state == il.OK and vac.detail == "the loop is made (no data from the vacuum service)"


def test_an_old_or_missing_gauge_relay_reading_is_unknown():
    data = refused()
    data.pop(il.relay_status_tag(10, 2))           # published as unknown: not read lately
    relay = overview(data).group("relay")
    vac = cond(relay, "Gauge controller relay")
    assert vac.state == il.UNKNOWN and vac.detail == "relay 2: not read recently"
    data = refused(**{"VACUUM_DATA/system.connected": 0.0})
    assert "aren't answering" in cond(overview(data).group("relay"), "Gauge controller relay").detail


def test_a_trip_whose_cause_the_plc_saw():
    data = machine(**{il.AIR: 0.0, "faults.Air_Pressure_Low": 1.0, "faults.Safety_Relay_Tripped": 1.0,
                      il.MACHINE_STATE: 99.0})
    relay = overview(data).group("relay")
    assert relay.status == "TRIPPED"
    assert relay.conditions[relay.first].label == "Compressed air"
    (text, _), = relay.notes
    assert text.startswith("The relay opened without being switched off (Safety_Relay_Tripped). The PLC withholds "
                           "its enable now as well: the compressed air pressure is low. Ruled out: the gauge "
                           "controller relay (vacuum) and the magnet coolant switch")
    assert "That leaves: a door, an ESO or the key switch opened; or something opened for a moment" in text
    assert text.endswith("Once that is cleared, RESET FAULTS and try again.")
    data[il.AIR] = 1.0                              # recovered, still latched
    relay = overview(data).group("relay")
    assert cond(relay, "Compressed air").detail == "pressure OK (Air_Pressure_Low latched: RESET FAULTS clears it)"


def test_live_and_hot():
    data = machine(**{il.RELAY: 1.0, il.ENABLE: 1.0, il.GV_OPEN: 1.0, il.GV_CLOSED: 0.0, il.MACHINE_STATE: 3.0,
                      il.GV_CLOSE_PERMISSIVE: 0.0, il.CS_HOT: 1.0, "ion_beam.source.cesium.estimated_temp": 85.0,
                      "ion_beam.source.extraction.rb_voltage": 10.0})
    ov = overview(data)
    relay = ov.group("relay")
    assert relay.status == "LIVE" and cond(relay, "Doors").state == il.OK and relay.conditions[-1].state == il.ON
    assert relay.notes == [("The source is hot or at high voltage, so the PLC keeps the relay on (see Source cold).",
                            "info")]
    cold = ov.group("source_cold")
    assert cold.status == "HOT: HELD ON" and cold.level == il.LEVEL_AUTO
    assert cold.summary == ("The PLC holds the relay on and the gate valve open: the extraction is at 10.00 kV. To "
                            "switch off, use INITIATE SHUTDOWN.")
    assert cold.conditions[-1].detail == "no: it holds the relay on and the gate valve open"
    assert cond(cold, "Caesium not hot").detail == "hot, 85 °C estimated" and not cold.disagree
    assert ov.machine_name == "LIVE / HV" and ov.machine_level == il.LEVEL_ATTENTION
    # Hot with the relay open (a trip): nothing to switch off, the PLC keeps asking
    data.update({il.RELAY: 0.0, il.MACHINE_STATE: 99.0, "faults.Safety_Relay_Tripped": 1.0,
                 "ion_beam.source.extraction.rb_voltage": 0.0})
    cold = overview(data).group("source_cold")
    assert cold.status == "HOT" and "INITIATE SHUTDOWN" not in cold.summary
    assert cold.summary.startswith("Not cold yet: the caesium is hot (85 °C estimated). The relay is open")
    assert cold.conditions[-1].detail == "no: it keeps asking for the relay, and for the gate valve to open"


def test_a_latched_gauge_fault_keeps_the_gate_valve_shut():
    data = machine(**{"faults.VG1_Above_SP_Warn": 1.0, il.GV_OPEN_PERMISSIVE: 0.0, il.GV_REQUEST: 1.0,
                      il.MACHINE_STATE: 99.0})
    gv = overview(data).group("gv_open")
    assert gv.level == il.LEVEL_FAULT and gv.conditions[gv.first].label == "VG1 healthy"
    assert gv.summary == ("Can't open: VG1_Above_SP_Warn is latched (RESET FAULTS once the pressure is back below "
                          "the set point), so the PLC withholds its open permissive.")
    assert not gv.disagree


def test_why_the_vacuum_isnt_good():
    gv = overview(machine(**{il.VAC_OK: 0.0, il.GV_OPEN_PERMISSIVE: 0.0, "vacuum.connected": False})).group("gv_open")
    assert gv.summary == ("Can't open: the vacuum isn't good for it (the vacuum service isn't sending data), so the "
                          "PLC withholds its open permissive.")
    data = machine(**{il.VAC_OK: 0.0, il.GV_OPEN_PERMISSIVE: 0.0,
                      "ion_beam.source.vacuum_gauge_1.stat_above_sp": 1.0,
                      "ion_beam.source.vacuum_gauge_1.rb_pressure": 9.5e-6})
    assert cond(overview(data).group("gv_open"), "Vacuum good").detail == \
        "VG1 above its relay's set point (9.5E-06 mbar)"
    data = machine(**{il.VAC_HOLD_LEFT: 42.0})
    assert "restart (42 s left)" in cond(overview(data).group("gv_open"), "Vacuum good").detail


def test_the_plcs_own_verdict_is_checked_against_the_conditions():
    gv = overview(machine(**{il.VAC_OK: 0.0})).group("gv_open")          # the PLC's bit still says given
    assert "may differ from its description" in gv.disagree
    relay = overview(machine(**{il.MACHINE_STATE: 2.0})).group("relay")  # Ready with the gate valve shut
    assert relay.disagree.startswith("The PLC says Ready (machine state 2), but by the conditions listed its enable "
                                     "couldn't be given")
    cs = overview(machine(**{"ion_beam.source.cesium.stat_lockout": 0.0})).group("cesium")
    assert "lockout is clear" in cs.disagree


def test_a_disagreement_shows_only_once_it_lasts():
    t = [0.0]
    flt = il.Disagreements(hold_s=3.0, clock=lambda: t[0])
    data = machine(**{il.VAC_OK: 0.0})
    for t[0] in (0.0, 0.5, 1.0, 2.0, 2.9):
        assert not flt.filter(overview(data)).group("gv_open").disagree
    t[0] = 3.1
    assert flt.filter(overview(data)).group("gv_open").disagree
    t[0] = 3.2
    assert not flt.filter(overview(machine())).group("gv_open").disagree
    t[0] = 10.0                                                     # (not called for a while: starts again)
    assert not flt.filter(overview(data)).group("gv_open").disagree


def test_a_shutdown_sequence():
    data = machine(**{il.RELAY: 1.0, il.GV_OPEN: 1.0, il.GV_CLOSED: 0.0, il.MACHINE_STATE: 3.0, il.PC_STEP: 10.0,
                      il.AUTO_SHUTDOWN: 1.0, il.GV_CLOSE_PERMISSIVE: 0.0, il.CS_HOT: 1.0})
    ov = overview(data)
    assert "Shutdown sequence at HOLD HOT PHASE" in ov.machine
    reset = ov.group("reset")
    assert reset.status == "REFUSED NOW"
    assert reset.summary == "A reset would be refused: a shutdown sequence is at HOLD HOT PHASE."
    supplies = ov.group("supplies")             # (not a gate on the contactors: the PLC drives the supplies)
    assert supplies.status == "AVAILABLE" and supplies.summary == (
        "The PLC lets the contactors close. A shutdown sequence is at HOLD HOT PHASE: the PLC drives the supplies "
        "itself.")
    seq = cond(supplies, "Shutdown sequence")
    assert seq.state == il.INFO and not seq.gates and seq.detail.startswith("at HOLD HOT PHASE")
    killed = overview(machine(**{il.KILL: 1.0, il.PC_STEP: 3.0})).group("relay")
    assert killed.summary == ("Held off by the PLC: the gate valve is closed.")
    assert cond(killed, "No relay kill").detail == "killed at FINAL KILL: RESET FAULTS clears it"


def test_latched_faults_with_their_descriptions():
    data = machine(**{"faults.Safety_Relay_Refused": 1.0, "faults.Unit1_Comms_Fail": 1.0, il.MACHINE_STATE: 99.0})
    reset = overview(data, fault_text={"Unit1_Comms_Fail": "Spellman MPD Unit 1 communication failure."}).group("reset")
    assert reset.status == "2 LATCHED" and reset.summary == "2 faults latched; a reset would be accepted."
    latched = [c for c in reset.conditions if c.section == "Latched now"]
    assert [c.label for c in latched] == ["Safety_Relay_Refused", "Unit1_Comms_Fail"]
    assert latched[1].detail == "Spellman MPD Unit 1 communication failure."


def test_the_services_permits_grouped_by_reason():
    data = {"ion_beam.source.extraction.permit": 0.0, "ion_beam.source.extraction.permit_reason": "safety relay de-energised",
            "ion_beam.source.target.permit": 0.0, "ion_beam.source.target.permit_reason": "safety relay de-energised",
            "ion_beam.beamline.magnet.permit": 0.0, "ion_beam.beamline.magnet.lease_holder": "recipe:1",
            "ion_beam.beamline.magnet.lease_purpose": "recipe 'Scan'", "ion_beam.beamline.faraday.smu.permit": 1.0}
    hmi = il.evaluate(data).group("hmi")
    assert hmi.status == "1 OF 4 FREE" and not hmi.chain
    assert [(c.label, c.state, c.detail) for c in hmi.conditions] == [
        ("Analyser magnet", il.INFO, "held by recipe 'Scan'"),
        ("Extraction, target (cathode)", il.HOLDING, "safety relay de-energised"),
        ("1 other", il.OK, "take commands")]
    assert hmi.summary == "2 refuse commands; 1 held by a sequence."
    hmi = il.evaluate({"ion_beam.beamline.faraday.smu.permit": 1.0}).group("hmi")
    assert hmi.summary == "Every service takes commands." and hmi.conditions[0].label == "All 1"


def test_the_loops_relay_setting():
    cfg = {"10": {"channels": {"1": {"device": "vacuum_gauge_1"}},
                  "relays": {"4": {"channel": 1, "on_val": 5.5e-6, "off_val": 8e-6}, "1": {"channel": 1}}},
           "system_interlocks": {"safety_loop_relays": ["10.4"]}}
    lc = il.LoopConfig.parse(cfg)
    assert lc.relays == [(10, 4)] and lc.candidates() == [(10, 4)] and not lc.problem
    assert lc.relay_info[(10, 4)] == {"ch": 1, "on": 5.5e-6, "off": 8e-6} and lc.relay_info[(10, 1)] == {}
    assert lc.gauges[(10, 1)] == "VG1"
    cfg["system_interlocks"]["safety_loop_relays"] = []
    assert il.LoopConfig.parse(cfg).candidates() == [(10, 1), (10, 4)]       # not set: all of controller 1's
    cfg["system_interlocks"]["safety_loop_relays"] = ["relay four"]
    assert "isn't understood" in il.LoopConfig.parse(cfg).problem
    assert il.parse_relay(" 20.6 ") == (20, 6) and il.parse_relay("10.7") is None
    missing = il.LoopConfig.load(os.path.join(os.path.dirname(__file__), "no_such_file.json"))
    assert "isn't readable" in missing.problem
    assert "isn't readable" in il.evaluate({}, missing).group("relay").notes[0][0]


def test_the_shipped_config_leaves_the_loops_relay_to_be_set():
    lc = il.LoopConfig.load()
    assert lc.relays is None and not lc.problem
    assert lc.candidates() == [(10, r) for r in range(1, 7)]


@pytest.mark.parametrize("value", [None, "1", float("nan"), [1], {"a": 1}])
def test_odd_values_count_as_unknown(value):
    data = machine(**{il.RELAY: value, il.AIR: value, il.MACHINE_STATE: value})
    ov = il.evaluate(data)
    assert ov.group("relay").status == "UNKNOWN" and cond(ov.group("relay"), "Compressed air").state == il.UNKNOWN


def test_a_refusal_with_a_plc_condition_holding_now_still_lists_the_loop():
    """The refusal came from an earlier enable; whatever holds the PLC's enable now came after, so
    the loop is still listed for the next try."""
    relay = overview(refused(**{il.GV_OPEN: 0.0, il.GV_CLOSED: 1.0})).group("relay")
    assert relay.summary == "Held off by the PLC: the gate valve is closed."
    assert cond(relay, "Doors").state == il.HIDDEN
    (text, _), = relay.notes
    assert "The PLC withholds its enable now as well: the gate valve is closed." in text
    assert "That leaves: a door, an ESO or the key switch is open; or START wasn't pressed within the 5 s." in text


def test_the_summaries_lead_with_the_plcs_own_verdict():
    open_gv = {il.GV_OPEN: 1.0, il.GV_CLOSED: 0.0}
    relay = overview(machine(**open_gv)).group("relay")          # state 1, though every condition holds
    assert relay.summary == "The PLC reports not ready (machine state 1), though the conditions listed hold."
    gv = overview(machine(**{il.GV_OPEN_PERMISSIVE: 0.0})).group("gv_open")
    assert gv.summary == "Can't open: the PLC withholds its open permissive."
    # Waiting for START with the loop's gauge relay open: START won't help
    data = machine(**open_gv, **{il.MACHINE_STATE: 2.0, il.ENABLE: 1.0, il.TIMER: 1800.0,
                                 il.relay_status_tag(10, 4): 0.0, f"{VG[1]}.rb_pressure": 9.1e-6})
    relay = overview(data, wired=["10.4"]).group("relay")
    assert relay.summary.startswith("The PLC gives its enable: press START")
    assert "The relay can't close while the gauge controller's relay in the loop is open" in relay.summary
    data[il.TIMER] = 0.0                                        # requested, timer not running
    data.pop(il.relay_status_tag(10, 4))
    data[f"{VG[1]}.rb_pressure"] = 2.1e-7
    relay = overview(data, wired=["10.4"]).group("relay")
    assert relay.summary == "Requested: press START on the High Voltage Systems Control box."


def test_the_remote_io_can_only_be_known_with_the_relay_closed():
    """Remote_IO_Lost is reported only once the relay has been closed 25 s, and clears as it opens."""
    supplies = overview(machine()).group("supplies")
    remote = cond(supplies, "Remote I/O")
    assert remote.state == il.UNKNOWN and "not known with the relay open" in remote.detail
    live = machine(**{il.RELAY: 1.0, il.GV_OPEN: 1.0, il.GV_CLOSED: 0.0, il.MACHINE_STATE: 3.0})
    assert cond(overview(live).group("supplies"), "Remote I/O").detail == "not reported lost"
    live.update({il.REMOTE_IO_LOST: 1.0, "faults.Remote_IO_Lost": 1.0})
    supplies = overview(live).group("supplies")
    assert cond(supplies, "Remote I/O").state == il.HOLDING
    assert supplies.summary == "Held off: the remote I/O on the source deck is lost."


def test_a_reset_at_pc_step_2_warns_that_it_ends_the_cool_down():
    reset = overview(machine(**{il.PC_STEP: 2.0, il.AUTO_SHUTDOWN: 1.0})).group("reset")
    assert reset.first is None
    (text, level), = reset.notes
    assert level == "warn" and text.startswith("A reset now would also end the PC sequence's cool-down "
                                               "(CESIUM COOLDOWN), with the caesium possibly still hot")
    assert not overview(machine()).group("reset").notes


def test_forced_cooling_left_on_after_a_cancelled_shutdown():
    cs = overview(machine(**{il.RELAY: 1.0, il.FORCE_COOLER: 1.0})).group("cesium")
    seq = cond(cs, "No shutdown or forced cooling")
    assert seq.state == il.HOLDING and seq.detail == ("forced cooling left on by a shutdown sequence (RESET FAULTS "
                                                      "clears it)")
    assert cs.summary == "Can't heat: forced cooling is still on from a shutdown sequence (RESET FAULTS clears it)."
    unknown = cond(overview(machine(**{il.RELAY: 1.0, il.FORCE_COOLER: None})).group("cesium"), "No shutdown")
    assert unknown.state == il.UNKNOWN
