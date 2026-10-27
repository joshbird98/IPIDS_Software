"""Alarm management (src/alarms/): states, delays, acknowledgement, shelving, first-out,
suppression, persistence, and the rules that turn telemetry into alarms."""
import json

import pytest

from src.alarms.model import ACKED, NORMAL, RTN_UNACK, UNACK, AlarmTable
from src.alarms.rules import DEFAULT_CONFIG, AlarmRules, load_config, suppressed
from src.core.telemetry_cache import TelemetryCache


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def table():
    events = []
    clock = Clock()
    t = AlarmTable(first_out_groups=["plc"], clock=clock, on_event=lambda k, a, d: events.append((k, a.id, d)))
    return t, clock, events


# --------------------------------------------------------------------------- states
def test_isa_state_machine(table):
    t, clock, events = table
    t.define("a", "Pump trip", priority=2)
    t.update("a", True)
    assert t.alarms["a"].state == UNACK and t.horn() == 2 and t.counts()["unack"] == 1
    t.update("a", False)
    assert t.alarms["a"].state == RTN_UNACK                 # cleared, still to be acknowledged
    assert t.horn() is None and t.counts()["unack"] == 1    # listed (flashing), no longer sounding
    assert t.ack(["a"], by="hmi") == ["a"] and t.alarms["a"].state == NORMAL
    t.update("a", True)
    t.ack()
    assert t.alarms["a"].state == ACKED and t.horn() is None
    t.update("a", False)
    assert t.alarms["a"].state == NORMAL
    assert [k for k, *_ in events] == ["ACTIVE", "CLEARED", "ACKED", "ACTIVE", "ACKED", "CLEARED"]
    assert t.alarms["a"].count == 2


def test_unknown_condition_changes_nothing(table):
    t, *_ = table
    t.define("a", "x")
    t.update("a", True)
    t.update("a", None)
    assert t.alarms["a"].active and t.alarms["a"].state == UNACK


def test_delays_filter_chatter(table):
    t, clock, _ = table
    t.define("a", "comms", delay_on=5, delay_off=2)
    t.update("a", True)
    clock.t += 4
    t.update("a", True)
    assert not t.alarms["a"].active
    t.update("a", False)                                    # chatter: the delay starts again
    clock.t += 1
    t.update("a", True)
    clock.t += 4.9
    t.update("a", True)
    assert not t.alarms["a"].active
    clock.t += 0.2
    t.update("a", True)
    assert t.alarms["a"].active
    t.update("a", False)
    clock.t += 1
    t.update("a", False)
    assert t.alarms["a"].active                              # off-delay too
    clock.t += 1.1
    t.update("a", False)
    assert not t.alarms["a"].active


def test_shelving_silences_until_it_expires(table):
    t, clock, events = table
    t.define("a", "x", priority=1)
    t.update("a", True)
    t.shelve("a", 60, "known: sensor being replaced", by="hmi")
    assert t.horn() is None and t.counts() == {"unack": 0, "active": 0, "shelved": 1, "suppressed": 0}
    assert t.listed()[-1].id == "a"                          # still listed, at the bottom
    clock.t += 61
    t.expire_shelves()
    assert t.horn() == 1 and ("UNSHELVED", "a", "shelf time over") in events


def test_first_out_marks_the_first_trip_until_reset(table):
    t, *_ = table
    for alarm_id in ("relay", "einzel", "extraction"):
        t.define(alarm_id, alarm_id, group="plc")
    t.define("other", "other", group="")
    t.update("other", True)
    t.update("einzel", True)
    t.update("relay", True)
    assert [a.id for a in t.alarms.values() if a.first_out] == ["einzel"]
    t.reset_first_out()
    t.update("einzel", False)
    t.update("extraction", True)
    assert [a.id for a in t.alarms.values() if a.first_out] == ["extraction"]


def test_suppressed_alarms_are_listed_not_sounded(table):
    t, *_ = table
    t.define("g1", "gauge 1 comms")
    t.update("g1", True)
    t.set_suppressed("g1", True)
    assert t.horn() is None and t.counts()["suppressed"] == 1 and t.listed()[0].id == "g1"


def test_acknowledgements_survive_a_restart(table, tmp_path):
    t, clock, _ = table
    t.define("a", "x")
    t.define("b", "y")
    t.define("c", "z", group="plc")
    for alarm_id in ("a", "b", "c"):
        t.update(alarm_id, True)
    t.ack(["a"])
    t.shelve("b", 600, "maintenance")
    saved = json.loads(json.dumps(t.dump()))
    fresh = AlarmTable(first_out_groups=["plc"], clock=clock)
    fresh.restore(saved)
    fresh.define("a", "x")                                    # defined again by the rules
    fresh.define("b", "y")
    fresh.define("c", "z", group="plc")
    fresh.update("a", True)
    assert fresh.alarms["a"].state == ACKED                   # not annunciated again
    assert fresh.alarms["b"].shelved and fresh.alarms["c"].state == UNACK and fresh.alarms["c"].first_out
    fresh.define("d", "w", group="plc")
    fresh.update("d", True)
    assert not fresh.alarms["d"].first_out                    # the first-out is still c


# --------------------------------------------------------------------------- rules
FAULTS = {"UDT_Fault_Word_0_System": {"registry_tag": "ion_beam.faults.word_0_system", "bits": {
    "0.1": {"name": "Safety_Relay_Tripped", "severity": 1, "description": "Hardware safety loop broken."},
    "0.3": {"name": "Src_Coolant_Flow_Low", "severity": 2, "description": "Source coolant flow below minimum."}}}}


def rules_with(clock, config=None):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG)) if config is None else config
    return AlarmRules(cfg, FAULTS, started=clock() - 60)


def defs(rules, cache, clock):
    return {d.id: d for d in rules.evaluate(cache, clock())}


def test_plc_faults_and_comms_rules():
    clock = Clock()
    cache = TelemetryCache(clock=clock)
    cache.ingest("PLC_DATA", {"faults.Safety_Relay_Tripped": 1.0, "faults.Src_Coolant_Flow_Low": 0.0,
                              "system.connected": 1.0})
    d = defs(rules_with(clock), cache, clock)
    relay = d["plc.Safety_Relay_Tripped"]
    assert relay.condition is True and relay.priority == 1 and relay.group == "plc"   # priority from the override
    assert d["plc.Src_Coolant_Flow_Low"].condition is False and d["plc.Src_Coolant_Flow_Low"].priority == 3
    assert d["comms.plc"].condition is False and d["comms.plc_service"].condition is False
    clock.t += 30                                                                   # the PLC service went quiet
    d = defs(rules_with(clock), cache, clock)
    assert d["comms.plc_service"].condition is True
    assert d["plc.Safety_Relay_Tripped"].condition is None                          # unknown, not cleared


def test_flag_rules_match_devices_and_suppression():
    clock = Clock()
    cache = TelemetryCache(clock=clock)
    cache.ingest("VACUUM_DATA", {"ion_beam.source.vacuum_gauge_1.stat_comms_fail": 1.0,
                                 "ion_beam.source.vacuum_gauge_1.stat_above_sp": 0.0,
                                 "ion_beam.facilities.graphix1.stat_comms_fail": 1.0})
    cache.ingest("MAGNET_DATA", {"ion_beam.beamline.magnet.stat_comms_fail": 0.0})
    d = defs(rules_with(clock), cache, clock)
    gauge = d["comms.source.vacuum_gauge_1"]
    assert gauge.condition is True and gauge.message == "source vacuum gauge 1: communication lost"
    assert suppressed(gauge, {"comms.facilities.graphix1", "comms.source.vacuum_gauge_1"})
    assert not suppressed(d["comms.beamline.magnet"], {"comms.facilities.graphix1"})
    assert d["vacuum.source.vacuum_gauge_1.above_sp"].condition is False


def test_services_and_sequencer_rules():
    clock = Clock()
    cache = TelemetryCache(clock=clock)
    cache.ingest("MANAGER", {"manager.services": {"service_plc": "ONLINE", "service_smu": "CRASHED",
                                                  "service_monitor": "STOPPED", "service_events": "STARTING"}})
    cache.ingest("SEQUENCER_DATA", {"sequencer.recipe.state": "failed", "sequencer.recipe.purpose": "recipe 'Ramp'",
                                    "sequencer.recipe.message": "Step 3 failed: refused",
                                    "sequencer.mass_scan.state": "complete"})
    d = defs(rules_with(clock), cache, clock)
    assert d["service.service_smu"].condition is True and "crashed" in d["service.service_smu"].message
    assert d["service.service_plc"].condition is False and d["service.service_events"].condition is False
    assert d["service.service_monitor"].priority == 4                              # stopped on purpose
    assert d["sequencer.recipe.failed"].condition is True
    assert d["sequencer.recipe.failed"].message == "recipe 'Ramp' failed: Step 3 failed: refused"
    assert d["sequencer.mass_scan.failed"].condition is False


def test_overrides_and_disabling(tmp_path):
    clock = Clock()
    cache = TelemetryCache(clock=clock)
    cache.ingest("PLC_DATA", {"faults.Src_Coolant_Flow_Low": 1.0})
    (tmp_path / "alarm_config.json").write_text(json.dumps({"overrides": {
        "plc.Src_Coolant_Flow_Low": {"priority": 1}, "plc.Safety_Relay_Tripped": {"enabled": False}}}))
    config = load_config(str(tmp_path / "alarm_config.json"))
    assert config["rules"] == DEFAULT_CONFIG["rules"]                               # rules kept, overrides merged
    d = defs(rules_with(clock, config), cache, clock)
    assert d["plc.Src_Coolant_Flow_Low"].priority == 1 and "plc.Safety_Relay_Tripped" not in d
    assert d["plc.Src_Coolant_Flow_Low"].delay_on == 0.0


def test_pattern_overrides_apply_before_exact_ones():
    clock = Clock()
    cache = TelemetryCache(clock=clock)
    faults = {"W": {"bits": {"0.0": {"name": "Magnet_Comms_Fail", "severity": 1, "description": "magnet comms"},
                             "0.1": {"name": "Spellman_Comms_Fail", "severity": 1, "description": "spellman"}}}}
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["overrides"]["plc.Spellman_Comms_Fail"] = {"delay_on": 3}
    rules = AlarmRules(config, faults, started=clock() - 60)
    d = {x.id: x for x in rules.evaluate(cache, clock())}
    assert d["plc.Magnet_Comms_Fail"].delay_on == 10 and d["plc.Spellman_Comms_Fail"].delay_on == 3


def test_shipped_config_parses():
    config = load_config()
    kinds = {r["kind"] for r in config["rules"]}
    assert {"plc_faults", "flag", "stale", "services", "sequencer"} <= kinds


def test_the_source_turbo_guard_alarms_say_what_and_when():
    """One alarm per stage of the turbo's load guard, each with the guard's own text while active (what,
    and when the source will be shut down), the rule's when not; the shipped config has them too."""
    clock = Clock()
    cache = TelemetryCache(clock=clock)
    b = "ion_beam.source.turbo_pump"
    text = "Source turbo still overloaded: the source will be shut down at 10:45 (in 10 min) unless ..."
    cache.ingest("SRC_TURBO_DATA", {f"{b}.stat_load_warning": 0.0, f"{b}.stat_load_critical": 1.0,
                                    f"{b}.stat_load_trip": 0.0, f"{b}.guard_message": text})
    for config in (None, load_config()):
        d = defs(rules_with(clock, config), cache, clock)
        critical, warning, trip = (d[f"turbo.source.load_{x}"] for x in ("critical", "warning", "trip"))
        assert critical.condition is True and critical.priority == 1 and critical.message == text
        assert warning.condition is False and warning.priority == 2 and warning.message.startswith(
            "Source turbo working harder than normal - check")
        assert trip.condition is False and trip.priority == 1 and "RESET FAULTS" in trip.message
