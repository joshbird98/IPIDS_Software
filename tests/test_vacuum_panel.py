"""Vacuum System panel: a gauge or the turbo pump without a live reading keeps its last value,
greyed, with a STALE chip and how long ago it was read, and says why; the pump has no stop."""
import pytest

from src.gui.control.last_known import LastKnown, age_text, stale_text

VG1 = "ion_beam.source.vacuum_gauge_1"
VG2 = "ion_beam.beamline.vacuum_gauge_2"
TURBO = "ion_beam.source.turbo_pump"


class FakeCmd:
    def __init__(self):
        self.sent = []

    def send_command(self, target, tag, value, *args, **kwargs):
        self.sent.append((target, tag, value))


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def panel():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from src.gui.control.widgets.vacuum_widget import VacuumControlWidget
    w = VacuumControlWidget(FakeCmd(), fault_engine=None)
    w.clock = Clock()
    w.last = LastKnown(clock=w.clock)
    yield w
    w.deleteLater()
    app.processEvents()


def gauges_live(**pressures):
    data = {"vacuum.connected": True, "VACUUM_DATA/system.connected": 1.0}
    for base, p in pressures.items():
        tag = {"vg1": VG1, "vg2": VG2}[base]
        data.update({f"{tag}.rb_pressure": p, f"{tag}.stat_error_code": 0.0, f"{tag}.stat_relay_active": 1.0})
    return data


def turbo_live(hz, amps=0.8, ready=0.0, turning=1.0):
    return {"pump.connected": True, "SRC_TURBO_DATA/system.connected": 1.0, f"{TURBO}.stat_comms_fail": 0.0,
            f"{TURBO}.rb_speed_hz": hz, f"{TURBO}.rb_current": amps, f"{TURBO}.stat_ready": ready,
            f"{TURBO}.stat_turning": turning, f"{TURBO}.stat_trip": 0.0}


def test_age_text():
    assert [age_text(s) for s in (0, 4.9, 59, 60, 3599, 3600, 3725, 86400 + 7200)] == \
        ["0 s", "4 s", "59 s", "1 min", "59 min", "1 h 00 min", "1 h 02 min", "1 d 2 h"]
    assert stale_text(12) == "STALE 12 s"


def test_a_live_gauge_reads_normally(panel):
    panel.update_telemetry(gauges_live(vg1=2.3e-7))
    g = panel.gauges[1]
    assert g["val"].text() == "2.30e-07 mbar" and not g["val"].property("ink")
    assert (g["status"].text(), g["status"].property("tone")) == ("HEALTHY", "ok")


def test_a_spike_being_ridden_through_and_a_software_trip(panel):
    """VG1 above its software trip point (src/core/software_trips.py): SPIKE while it is ridden through,
    TRIPPED once it has lasted the ride-through - although its backstop relay is still closed."""
    data = gauges_live(vg1=0.9)
    panel.update_telemetry(dict(data, **{f"{VG1}.stat_riding_through": 1.0}))
    g = panel.gauges[1]
    assert (g["status"].text(), g["status"].property("tone")) == ("SPIKE", "warn")
    assert "ride-through" in g["status"].toolTip()
    panel.update_telemetry(dict(data, **{f"{VG1}.stat_riding_through": 0.0, f"{VG1}.stat_above_sp": 1.0}))
    assert (g["status"].text(), g["status"].property("tone")) == ("TRIPPED", "fault")
    assert not g["status"].toolTip()
    # Reported above its set point (a backstop relay open) while a spike is still being ridden through
    panel.update_telemetry(dict(data, **{f"{VG1}.stat_riding_through": 1.0, f"{VG1}.stat_above_sp": 1.0}))
    assert g["status"].text() == "TRIPPED"


def test_a_withdrawn_reading_keeps_its_last_value_marked_stale(panel):
    panel.update_telemetry(gauges_live(vg1=2.3e-7, vg2=4.0e-7))
    panel.clock.t += 12
    data = gauges_live(vg2=4.1e-7)                       # VG1's pressure withdrawn by the service
    data[f"{VG1}.stat_error_code"] = 0.0
    panel.update_telemetry(data)
    g = panel.gauges[1]
    assert g["val"].text() == "2.30e-07 mbar" and g["val"].property("ink") == "muted"
    assert (g["status"].text(), g["status"].property("tone")) == ("STALE 12 s", "unknown")
    assert "no good reading from this gauge" in g["status"].toolTip()
    assert "2.30e-07 mbar is the last reading" in g["val"].toolTip() and "12 s ago" in g["val"].toolTip()
    assert panel.gauges[2]["status"].text() == "HEALTHY"   # the others still live
    panel.clock.t += 60
    panel.update_telemetry(data)
    assert g["status"].text() == "STALE 1 min"             # the age keeps counting


@pytest.mark.parametrize("data, why", [
    ({"vacuum.connected": False}, "the vacuum service isn't sending data"),
    ({"vacuum.connected": True, "VACUUM_DATA/system.connected": 0.0}, "the gauge controllers aren't answering"),
])
def test_every_gauge_stale_when_the_service_or_the_controllers_stop(panel, data, why):
    panel.update_telemetry(gauges_live(vg1=2.3e-7, vg2=4.0e-7))
    panel.clock.t += 3
    panel.update_telemetry(data)
    for i, text in ((1, "2.30e-07 mbar"), (2, "4.00e-07 mbar")):
        g = panel.gauges[i]
        assert g["val"].text() == text and g["val"].property("ink") == "muted"
        assert g["status"].text() == "STALE 3 s" and why in g["status"].toolTip()
    never = panel.gauges[3]                                 # never read: nothing to show
    assert never["val"].text() == "--- mbar"
    assert never["status"].text() == ("OFFLINE" if not data["vacuum.connected"] else "NO READING")


def test_the_controllers_reason_for_no_reading_is_shown(panel):
    panel.update_telemetry(gauges_live(vg1=2.3e-7))
    panel.update_telemetry({"vacuum.connected": True, "VACUUM_DATA/system.connected": 1.0,
                            f"{VG1}.stat_error_code": 5.0, f"{VG1}.stat_not_found": 1.0})
    g = panel.gauges[1]
    assert (g["status"].text(), g["status"].property("tone")) == ("NOT FOUND", "fault")
    assert g["val"].property("ink") == "muted" and "last reading" in g["val"].toolTip()


def test_back_to_live_clears_the_marking(panel):
    panel.update_telemetry(gauges_live(vg1=2.3e-7))
    panel.update_telemetry({"vacuum.connected": False})
    panel.update_telemetry(gauges_live(vg1=2.1e-7))
    g = panel.gauges[1]
    assert g["val"].text() == "2.10e-07 mbar" and not g["val"].property("ink") and not g["val"].toolTip()
    assert g["status"].text() == "HEALTHY" and not g["status"].toolTip()


@pytest.mark.parametrize("data, why", [
    ({"pump.connected": False}, "the turbo service isn't sending data"),
    ({"pump.connected": True, "SRC_TURBO_DATA/system.connected": 1.0, f"{TURBO}.stat_comms_fail": 1.0},
     "the pump isn't answering"),
])
def test_turbo_speed_and_current_stale_when_not_live(panel, data, why):
    panel.update_telemetry(turbo_live(812.0, 1.23))
    panel.clock.t += 7
    panel.update_telemetry(data)
    assert panel.lbl_turbo_speed.text() == "812 Hz" and panel.lbl_turbo_speed.property("ink") == "muted"
    assert panel.lbl_turbo_current.text() == "1.23 A" and panel.lbl_turbo_current.property("ink") == "muted"
    assert (panel.lbl_turbo_status.text(), panel.lbl_turbo_status.property("tone")) == ("STALE 7 s", "unknown")
    assert why in panel.lbl_turbo_status.toolTip()
    assert not panel.btn_turbo_start.isEnabled() and why in panel.btn_turbo_start.toolTip()


def test_turbo_never_read_shows_offline(panel):
    panel.update_telemetry({"pump.connected": False})
    assert panel.lbl_turbo_speed.text() == "--- Hz" and panel.lbl_turbo_status.text() == "OFFLINE"


def test_turbo_slowing_down_is_not_accelerating(panel):
    panel.update_telemetry(turbo_live(500.0))
    panel.update_telemetry(turbo_live(510.0))
    assert panel.lbl_turbo_status.text() == "ACCELERATING"
    panel.update_telemetry(turbo_live(505.0))
    panel.update_telemetry(turbo_live(495.0))
    assert panel.lbl_turbo_status.text() == "SLOWING DOWN" and not panel.btn_turbo_start.isEnabled()


def test_the_turbo_can_be_started_but_has_no_stop(panel):
    from PyQt6.QtWidgets import QPushButton
    panel.update_telemetry(turbo_live(0.0, 0.0, turning=0.0))
    assert panel.lbl_turbo_status.text() == "IDLE" and panel.btn_turbo_start.isEnabled()
    assert "by hand" in panel.btn_turbo_start.toolTip()
    panel.btn_turbo_start.click()
    assert panel.cmd_thread.sent == [("src_turbo", f"{TURBO}.cmd_enable", True)]
    texts = [b.text().upper() for b in panel.findChildren(QPushButton)]
    assert not any("STOP" in t for t in texts), texts


def test_the_gate_valve_group_shows_the_vacuum_hold(panel):
    """While the vacuum service restarts, the PLC service gives the PLC its last judgment for a while
    (tests/test_vacuum_restart.py): the panel says so, with the time left."""
    plc = {"system.connected": True, "ion_beam.source.chamber.stat_gv_open": 1.0,
           "ion_beam.source.chamber.stat_gv_closed": 0.0}
    panel.update_telemetry({**plc, "ion_beam.source.chamber.vac_hold_left_s": 47.6})
    assert panel.lbl_gv_hold.isVisibleTo(panel) and panel.lbl_gv_hold.text() == "VACUUM PERMISSIVE HELD: 48 s LEFT"
    assert "restarting" in panel.lbl_gv_hold.toolTip()
    panel.update_telemetry({**plc, "ion_beam.source.chamber.vac_hold_left_s": 0.0})
    assert not panel.lbl_gv_hold.isVisibleTo(panel)


def test_the_turbos_temperatures_and_load_guard(panel):
    """The bearing and converter temperatures, and under the pump the load guard's stage with the time the
    source will be shut down - nothing while all is well (src/core/turbo_guard.py)."""
    from datetime import datetime
    live = {**turbo_live(1000.0, 1.2), f"{TURBO}.rb_temp_bearing": 44.0, f"{TURBO}.rb_temp_converter": 29.0,
            f"{TURBO}.rb_guard_stage": 0.0, f"{TURBO}.guard_message": "", f"{TURBO}.guard_shutdown_at": None}
    panel.update_telemetry(live)
    assert panel.lbl_turbo_temps.text() == "44 °C, 29 °C" and panel.lbl_turbo_guard.isHidden()
    at = datetime(2026, 10, 26, 10, 45).timestamp()
    text = "Source turbo working harder than normal: the source will be shut down at 10:45 (in 20 min) ..."
    panel.update_telemetry({**live, f"{TURBO}.rb_guard_stage": 1.0, f"{TURBO}.guard_message": text,
                            f"{TURBO}.guard_shutdown_at": at})
    chip = panel.lbl_turbo_guard
    assert not chip.isHidden() and (chip.text(), chip.property("tone")) == ("LOAD HIGH: SOURCE SHUTDOWN AT 10:45", "warn")
    assert chip.toolTip().startswith(text) and "left running" in chip.toolTip()
    panel.update_telemetry({**live, f"{TURBO}.rb_guard_stage": 2.0, f"{TURBO}.guard_shutdown_at": at})
    assert (chip.text(), chip.property("tone")) == ("OVERLOADED: SOURCE SHUTDOWN AT 10:45", "fault")
    panel.update_telemetry({**live, f"{TURBO}.rb_guard_stage": 3.0})
    assert chip.text() == "PROTECTION TRIPPED: SOURCE SHUT DOWN"
    panel.update_telemetry({**live, f"{TURBO}.rb_guard_stage": 1.0})       # running hot: no shutdown due
    assert chip.text() == "LOAD HIGH: CHECK THE BACKING LINE"
    panel.update_telemetry({"pump.connected": False})                      # the turbo service gone: no guard
    assert chip.isHidden() and panel.lbl_turbo_temps.text() == "44 °C, 29 °C"
    assert panel.lbl_turbo_temps.property("ink") == "muted"
