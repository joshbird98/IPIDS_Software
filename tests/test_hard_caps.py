"""Hard caps (src/core/hard_caps.py): the highest values ever allowed, whatever the config files
say - refused by the config editor (tests/test_config_editor.py has the marked fields), kept out
of the registry's setpoint limits, and refused by the services' commands."""
import json

import pytest

from src.core import config_store, hard_caps
from src.core.config_checks import check
from src.utils import build_registry as br

FILES = ("vac_gauges_config.json", "mpd_config.json", "magnet_config.json", "steerer_magnet_config.json",
         "smu_config.json")


def over_caps():
    """Each capped file with values just past its caps."""
    vac = config_store.read("vac_gauges_config.json")
    vac["10"]["relays"]["1"].update(on_val=7e-1, off_val=8e-1)     # VG1: the gate valve's
    vac["10"]["relays"]["3"]["off_val"] = 2.0                      # VG3: the other relays'
    mpd = config_store.read("mpd_config.json")
    mpd["buses"][0]["devices"]["source_einzel"]["max_value"] = 25000.0
    magnet = dict(config_store.read("magnet_config.json"), max_current=96.0, max_voltage=26.0)
    magnet["degauss_steps_amps"] = [100.0, 0.0]
    steerer = dict(config_store.read("steerer_magnet_config.json"), max_current=81.0, max_voltage=41.0)
    smu = dict(config_store.read("smu_config.json"), min_v=-250, max_v=201)
    return {"vac_gauges_config.json": vac, "mpd_config.json": mpd, "magnet_config.json": magnet,
            "steerer_magnet_config.json": steerer, "smu_config.json": smu}


def test_the_files_as_they_are_are_within_the_caps():
    for name in FILES:
        assert hard_caps.violations(name, config_store.read(name)) == [], name


def test_each_cap_is_refused_with_its_reason():
    found = {name: {v.path: v.message for v in hard_caps.violations(name, data)}
             for name, data in over_caps().items()}
    vac = found["vac_gauges_config.json"]
    assert set(vac) == {("10", "relays", "1", "on_val"), ("10", "relays", "1", "off_val"),
                        ("10", "relays", "3", "off_val")}
    assert "1e-05 mbar cap" in vac[("10", "relays", "1", "on_val")] and "gate valve" in vac[("10", "relays", "1", "on_val")]
    assert "1 mbar cap" in vac[("10", "relays", "3", "off_val")]
    assert "22000 V cap" in found["mpd_config.json"][("buses", 0, "devices", "source_einzel", "max_value")]
    assert set(found["magnet_config.json"]) == {("max_current",), ("max_voltage",), ("degauss_steps_amps", 0)}
    assert "95 A cap" in found["magnet_config.json"][("max_current",)]
    assert "40 V cap" in found["steerer_magnet_config.json"][("max_voltage",)]
    assert "below the -200 V cap" in found["smu_config.json"][("min_v",)]
    for name, data in over_caps().items():          # and the editor's checks report them (it refuses on any)
        problems = check(name, data, config_store.read(name))
        assert all(message in problems for message in found[name].values()), name
        assert all(m.startswith("Hard cap:") and "src/core/hard_caps.py" in m for m in found[name].values())


def test_commands_are_capped():
    assert hard_caps.command_cap("ion_beam.source.einzel.sp_requested_voltage").breach(22.5) == "above"
    assert hard_caps.command_cap("ion_beam.beamline.neutral_trap_neg.sp_requested_voltage").breach(22.0) is None
    assert hard_caps.command_cap("ion_beam.beamline.magnet.sp_requested_current").breach(95.5) == "above"
    assert hard_caps.command_cap("ion_beam.beamline.steerer_magnet.sp_requested_current").breach(80.5) == "above"
    assert hard_caps.command_cap("ion_beam.beamline.faraday.smu.sp_requested_voltage").breach(-200.5) == "below"
    assert hard_caps.command_cap("ion_beam.source.extraction.sp_requested_voltage") is None   # the PLC's own


def test_the_registry_keeps_setpoint_limits_within_the_caps(tmp_path):
    for name, data in over_caps().items():
        (tmp_path / name).write_text(json.dumps(data))
    tags = {}
    for build in (br.get_vacuum_tags, br.get_spellman_tags, br.get_magnet_tags, br.get_steerer_magnet_tags,
                  br.get_smu_tags):
        tags.update(build(str(tmp_path)))
    assert tags["ion_beam.facilities.graphix1.relay_1_on"]["max_val"] == pytest.approx(1e-5)
    assert tags["ion_beam.facilities.graphix1.relay_3_off"]["max_val"] == pytest.approx(1.0)
    assert tags["ion_beam.facilities.graphix2.relay_1_on"]["max_val"] == pytest.approx(1.0)
    assert tags["ion_beam.source.einzel.sp_requested_voltage"]["max_val"] == pytest.approx(22.0)
    assert tags["ion_beam.beamline.magnet.sp_requested_current"]["max_val"] == pytest.approx(95.0)
    assert tags["ion_beam.beamline.steerer_magnet.sp_requested_current"]["max_val"] == pytest.approx(80.0)
    smu = tags["ion_beam.beamline.faraday.smu.sp_requested_voltage"]
    assert (smu["min_val"], smu["max_val"]) == (-200.0, 200.0)


class Cmd:
    def __init__(self, tag, value):
        self.tag, self.value, self.result = tag, value, None

    def ok(self, note="", data=None):
        self.result = ("ok", note)

    def reject(self, reason):
        self.result = ("rejected", reason)

    def fail(self, reason):
        self.result = ("failed", reason)


@pytest.mark.parametrize("tag, value, allowed", [
    ("ion_beam.source.einzel.sp_requested_voltage", 25.0, 21.5),
    ("ion_beam.beamline.einzel.sp_requested_voltage", 22.1, 22.0),
    ("ion_beam.beamline.magnet.sp_requested_current", 96.0, 95.0),
    ("ion_beam.beamline.steerer_magnet.sp_requested_current", 85.0, 79.0),
    ("ion_beam.beamline.faraday.smu.sp_requested_voltage", -201.0, -200.0),
])
def test_services_refuse_setpoints_past_a_cap_even_with_a_wider_registry(tag, value, allowed):
    """Every service checks setpoints with DeviceService.within_limits: the cap is checked there
    too, before the registry's limits (a registry built or edited past a cap can't get round it)."""
    from src.core.device_service import DeviceService

    class Rig:
        registry_limits = {tag: {"min_val": -1000.0, "max_val": 1000.0}}
    cmd = Cmd(tag, value)
    assert not DeviceService.within_limits(Rig(), cmd, value)
    assert cmd.result[0] == "rejected" and cmd.result[1].startswith("Hard cap: the setpoint")
    assert DeviceService.within_limits(Rig(), Cmd(tag, allowed), allowed)


def test_nan_infinity_and_text_dont_slip_past():
    cap = hard_caps.relay_cap(10, 1)
    assert cap.breach(float("nan")) == "above" and cap.breach(float("inf")) == "above"
    assert hard_caps.SMU_CAP.breach(float("-inf")) == "below"
    assert hard_caps.clamp(float("inf"), hard_caps.SMU_CAP) is None          # no limit: setpoints refused
    assert hard_caps.relay_cap(10, float("inf")) == hard_caps.relay_cap(10, 1)  # unknown channel: the strictest
    smu = dict(config_store.read("smu_config.json"), max_v=float("nan"))
    assert [v.path for v in hard_caps.violations("smu_config.json", smu)] == [("max_v",)]
    mpd = config_store.read("mpd_config.json")
    mpd["buses"][0]["devices"]["source_einzel"]["min_value"] = -30000.0     # the Spellman cap is both ways
    assert [v.path[-1] for v in hard_caps.violations("mpd_config.json", mpd)] == ["min_value"]


def test_just_past_a_cap_reads_as_past_it():
    assert "22.0000001 kV is above the 22 kV cap" in hard_caps.SPELLMAN_CAP_KV.explain("x", 22.0000001)
    assert "1e-05 mbar cap" in hard_caps.relay_cap(10, 2).explain("x", 1.0000001e-05)
    assert hard_caps.relay_cap(10, 2).breach(1e-5) is None and hard_caps.relay_cap(10, 2).breach(1.0e-05) is None


def test_setpoint_boxes_name_their_cap():
    assert "22 kV" in hard_caps.note("ion_beam.source.einzel.sp_requested_voltage")
    assert "-200 .. 200 V" in hard_caps.note("ion_beam.beamline.faraday.smu.sp_requested_voltage")
    assert hard_caps.note("ion_beam.source.extraction.sp_requested_voltage") == ""


def test_the_optics_panel_keeps_the_cap_in_its_tooltips():
    """The panel re-sets its tooltips on every telemetry update (permit reasons): the cap note must
    survive that."""
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from src.gui.control.widgets.optics_widget import BeamlineOpticsWidget

    class Commands:
        def send_command(self, *a, **k):
            pass

    class Events:
        def log_general(self, *a, **k):
            pass
    w = BeamlineOpticsWidget(Commands(), Events())
    from src.core.spellman_locations import LOCATIONS
    live = {f"{base}.permit": 1.0 for base in ("ion_beam.beamline.magnet", "ion_beam.beamline.steerer_magnet",
                                               *LOCATIONS.values())}
    live["ion_beam.facilities.stat_mag_coolant_ok"] = True
    try:
        for _ in range(2):                                   # every update re-sets the tooltips
            w.update_telemetry(live)
            for box, tag in ((w.sp_mag_current, "ion_beam.beamline.magnet.sp_requested_current"),
                             (w.sp_x_steer, "ion_beam.beamline.steerer_magnet.sp_requested_current")):
                assert box.isEnabled() and hard_caps.note(tag) in box.toolTip(), box.toolTip()
            boxes = [c["sp_box"] for c in w.psu_controls.values()]
            assert boxes and all(b.isEnabled() and "22 kV" in b.toolTip() for b in boxes)
    finally:
        w.deleteLater()
        app.processEvents()
