import json
import os

from src.core.tolerance import FLAG_PREFIX, RuleFileWatcher, ToleranceMonitor, ToleranceRule, load_rules
from src.core import paths

SP, RB, EN = "ion_beam.source.einzel.sp_actual_voltage", "ion_beam.source.einzel.rb_voltage", \
    "ion_beam.source.einzel.stat_enabled"


def rule(**kw):
    spec = {"name": "einzel", "setpoint": SP, "readback": RB, "abs_tol": 0.1, "rel_tol": 0.02, "delay_s": 5.0,
            "only_when": EN}
    spec.update(kw)
    return ToleranceRule(spec)


def test_alarm_only_after_delay_and_clears_with_events(fake_events):
    r = rule()
    mon = ToleranceMonitor([r], events=fake_events)
    vals = {SP: 10.0, RB: 9.0, EN: 1.0}
    get = vals.get
    assert mon.evaluate(get, now=0.0)[FLAG_PREFIX + RB] == 0.0
    assert mon.evaluate(get, now=4.9)[FLAG_PREFIX + RB] == 0.0      # still inside the delay
    assert mon.evaluate(get, now=5.1)[FLAG_PREFIX + RB] == 1.0
    mon.evaluate(get, now=6.0)
    warnings = fake_events.of_type("TOLERANCE")
    assert len(warnings) == 1 and warnings[0]["severity"] == "WARNING"   # one event, not one per poll
    vals[RB] = 9.95                                                      # within max(0.1, 2% of 10)
    assert mon.evaluate(get, now=7.0)[FLAG_PREFIX + RB] == 0.0
    assert [e["severity"] for e in fake_events.of_type("TOLERANCE")] == ["WARNING", "INFO"]


def test_short_excursions_never_alarm(fake_events):
    mon = ToleranceMonitor([rule()], events=fake_events)
    vals = {SP: 10.0, RB: 8.0, EN: 1.0}
    for t in range(0, 40, 1):                 # out for 3 s, back for 1 s, repeatedly (e.g. ramping)
        vals[RB] = 10.0 if t % 4 == 3 else 8.0
        mon.evaluate(vals.get, now=float(t))
    assert fake_events.of_type("TOLERANCE") == []


def test_disabled_output_and_unknown_data_are_ignored(fake_events):
    mon = ToleranceMonitor([rule()], events=fake_events)
    vals = {SP: 10.0, RB: 0.0, EN: 0.0}       # output off: readback 0 is expected
    for t in range(20):
        assert mon.evaluate(vals.get, now=float(t))[FLAG_PREFIX + RB] == 0.0
    vals = {SP: 10.0, RB: None, EN: 1.0}      # readback unknown (service down / stale)
    for t in range(20, 40):
        assert mon.evaluate(vals.get, now=float(t))[FLAG_PREFIX + RB] == 0.0
    assert fake_events.of_type("TOLERANCE") == []


def test_relative_tolerance_and_min_setpoint():
    r = rule(abs_tol=0.0, rel_tol=0.05, min_setpoint=1.0, only_when=None, delay_s=0.0)
    mon = ToleranceMonitor([r])
    assert mon.evaluate({SP: 20.0, RB: 19.1}.get, now=0)[FLAG_PREFIX + RB] == 0.0   # within 5 %
    assert mon.evaluate({SP: 20.0, RB: 18.9}.get, now=1)[FLAG_PREFIX + RB] == 1.0
    assert mon.evaluate({SP: 0.5, RB: 0.0}.get, now=2)[FLAG_PREFIX + RB] == 0.0      # below min_setpoint


def test_shipped_rule_file_loads_and_references_real_tags():
    rules = load_rules(paths.config_path("readback_tolerance.json"))
    assert len(rules) >= 8
    reg_path = paths.config_path("system_tags.json")
    if os.path.exists(reg_path):
        registry = json.load(open(reg_path))
        for r in rules:
            for tag in (r.setpoint, r.readback, r.only_when):
                if tag:
                    assert tag in registry, f"{r.name}: {tag} is not in system_tags.json"


def test_watcher_reloads_edits_and_keeps_state(tmp_path, fake_events):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps({"rules": [{"setpoint": SP, "readback": RB, "abs_tol": 0.1, "delay_s": 0}]}))
    mon = ToleranceMonitor([], events=fake_events)
    w = RuleFileWatcher(str(path), mon, events=fake_events, check_every=0)
    assert w.poll(now=0) and len(mon.rules) == 1
    mon.evaluate({SP: 10.0, RB: 5.0}.get, now=1)
    assert mon.rules[0].alarmed
    path.write_text(json.dumps({"rules": [{"setpoint": SP, "readback": RB, "abs_tol": 0.2, "delay_s": 0}]}))
    os.utime(path, (2000, 2000))
    assert w.poll(now=2)
    assert mon.rules[0].abs_tol == 0.2 and mon.rules[0].alarmed   # state carried over
    path.write_text("{ broken json")
    os.utime(path, (3000, 3000))
    assert not w.poll(now=3) and mon.rules[0].abs_tol == 0.2      # bad edit keeps the old rules
