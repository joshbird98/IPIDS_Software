"""System Health panel: what it shows and allows while the source turbo protection holds the source down
(service_plc's ion_beam.system.turbo_protection, after the turbo's load guard tripped)."""
import pytest


class FakeCmd:
    def __init__(self):
        self.sent = []

    def send_command(self, target, tag, value, *args, **kwargs):
        self.sent.append((target, tag, value))


class FakeFaults:
    fault_structures = {}

    def get_active_faults(self, udt, word):
        return []


@pytest.fixture
def panel():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from src.gui.control.widgets.system_health_widget import SystemHealthWidget
    w = SystemHealthWidget(FakeCmd(), FakeFaults())
    yield w
    w.deleteLater()
    app.processEvents()


def plc(**extra):
    d = {"system.connected": True, "ion_beam.system.pc_plc_comms_lost": 0.0,
         "ion_beam.facilities.safety_relay_active": 0.0, "ion_beam.system.src_auto_sdown": 0.0,
         "ion_beam.system.remote_shutdown_step": 0.0, "ion_beam.system.pc_shutdown_step": 0.0,
         "ion_beam.source.chamber.stat_gv_open": 1.0, "ion_beam.source.chamber.stat_gv_close_permissive": 1.0}
    d.update(extra)
    return d


def test_a_shutdown_the_turbo_protection_started_cant_be_aborted(panel):
    running = dict(**{"ion_beam.system.src_auto_sdown": 1.0, "ion_beam.system.pc_shutdown_step": 10.0,
                      "ion_beam.facilities.safety_relay_active": 1.0})
    panel.update_telemetry(plc(**running))
    assert panel.btn_cancel_shutdown.isEnabled()                       # an operator's shutdown: abortable
    why = "Source turbo protection tripped at 10:45: ..."
    panel.update_telemetry(plc(**running, **{"ion_beam.system.turbo_protection": 1.0,
                                             "ion_beam.source.turbo_pump.guard_message": why}))
    assert not panel.btn_cancel_shutdown.isEnabled()
    assert "can't be aborted" in panel.btn_cancel_shutdown.toolTip() and why in panel.btn_cancel_shutdown.toolTip()
    assert panel.lbl_shutdown_state.text() == "SHUTDOWN SEQ: HOLD HOT PHASE - SOURCE TURBO PROTECTION"
    assert panel.lbl_shutdown_state.property("tone") == "fault" and panel.lbl_shutdown_state.toolTip() == why
    # finished, the relay open: the relay stays off until the trip is reset
    panel.update_telemetry(plc(**{"ion_beam.system.turbo_protection": 1.0}))
    assert not panel.btn_enable_safety.isEnabled() and "RESET FAULTS" in panel.btn_enable_safety.toolTip()
    panel.update_telemetry(plc())
    assert panel.btn_enable_safety.isEnabled() and panel.lbl_shutdown_state.text() == "SHUTDOWN SEQ: INACTIVE"
    assert panel.lbl_shutdown_state.toolTip() == ""


def test_reset_faults_also_goes_to_the_turbo_service(panel):
    panel.update_telemetry(plc())
    panel.btn_reset_faults.click()
    assert [s[0] for s in panel.cmd_thread.sent] == ["plc", "spellman", "src_turbo"]
    assert {s[1] for s in panel.cmd_thread.sent} == {"ion_beam.system.cmd_fault_reset"}
