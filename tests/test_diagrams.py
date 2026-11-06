"""The machine diagrams (src/gui/control/diagrams): drawn from the panels, in both themes, with
nothing of their own. A faceplate's controls are its panel's controls, with its panel's rules: the
gate valve opens only when the Vacuum System panel's OPEN could be pressed, the source turbo can only
be started, switching a supply on takes two clicks, and a value typed goes out on the panel's tag."""
import pytest
from PyQt6.QtCore import QSize
from PyQt6.QtWidgets import QApplication, QPushButton

from src.gui.style import set_ink, set_tone

# Each diagram's READABLE size, smaller (as parts of it: the drawing shrinks to fit), and larger
SIZES = ("readable", 0.7, 0.45, QSize(1280, 820), QSize(1700, 1000))


class FakeCmd:
    def __init__(self):
        self.sent = []

    def send_command(self, target, tag, value, *args, **kwargs):
        self.sent.append((target, tag, value))

    def tags(self):
        return [tag for _target, tag, _value in self.sent]


class FakeEvents:
    def log_user_marker(self, *args, **kwargs):
        pass


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def cmd():
    return FakeCmd()


@pytest.fixture
def panels(app, cmd):
    from src.core.fault_registry import FaultRegistry
    from src.core.paths import config_path
    from src.gui.control.widgets.current_monitor_widget import AdamMonitorWidget
    from src.gui.control.widgets.faraday_smu_widget import FaradaySMUWidget
    from src.gui.control.widgets.interlocks_widget import InterlocksWidget
    from src.gui.control.widgets.optics_widget import BeamlineOpticsWidget
    from src.gui.control.widgets.source_widget import IonSourceWidget
    from src.gui.control.widgets.system_health_widget import SystemHealthWidget
    from src.gui.control.widgets.vacuum_widget import VacuumControlWidget
    faults = FaultRegistry(config_path("fault_config.json"))
    out = {"vacuum": VacuumControlWidget(cmd, faults), "source": IonSourceWidget(cmd, faults),
           "optics": BeamlineOpticsWidget(cmd, FakeEvents()), "faraday": FaradaySMUWidget(cmd),
           "currents": AdamMonitorWidget(cmd), "health": SystemHealthWidget(cmd, faults),
           "interlocks": InterlocksWidget(faults)}
    out["optics"].mass_calibration = lambda: {"k": 395.0, "i_off": 0.12, "source": "test"}
    yield out
    for w in out.values():
        w.deleteLater()
    app.processEvents()


def live(on: bool = True) -> dict:
    """The machine running (on) or off, as the services would report it."""
    f = 1.0 if on else 0.0
    d = {"system.connected": True, "vacuum.connected": True, "pump.connected": True, "current_mon.connected": True,
         "VACUUM_DATA/system.connected": 1.0, "SRC_TURBO_DATA/system.connected": 1.0,
         "system.facilities.adam.stat_comms_fail": 0.0, "ion_beam.faults.word_0_system": 0,
         "ion_beam.facilities.safety_relay_active": f, "ion_beam.system.machine_state": 3.0 if on else 2.0,
         "ion_beam.system.cmd_enable_safety": f, "ion_beam.internal.seq_cmd_kill_relay": 0.0,
         "ion_beam.system.pc_plc_comms_lost": 0.0, "ion_beam.system.remote_shutdown_step": 0.0,
         "ion_beam.system.pc_shutdown_step": 0.0, "faults.Cesium_Oven_OverTemp": 0.0,
         "faults.Air_Pressure_Low": 0.0, "faults.Src_Coolant_Flow_Low": 0.0, "faults.Mag_Coolant_Flow_Low": 0.0,
         "faults.Safety_Relay_Refused": 0.0, "faults.Safety_Relay_Tripped": 0.0,
         "ion_beam.facilities.stat_air_press_ok": 1.0, "ion_beam.facilities.stat_src_coolant_ok": 1.0,
         "ion_beam.facilities.stat_mag_coolant_ok": 1.0,
         "manager.services": {"service_spellman_mpd": "ONLINE", "service_events": "ONLINE"},
         "ion_beam.source.turbo_pump.stat_comms_fail": 0.0, "ion_beam.source.turbo_pump.rb_speed_hz": 1000.0,
         "ion_beam.source.turbo_pump.rb_current": 0.8, "ion_beam.source.turbo_pump.stat_ready": 1.0,
         "ion_beam.source.turbo_pump.stat_turning": 1.0, "ion_beam.source.turbo_pump.stat_trip": 0.0,
         "ion_beam.source.chamber.stat_gv_open": f, "ion_beam.source.chamber.stat_gv_closed": 1.0 - f,
         "ion_beam.source.chamber.stat_gv_open_permissive": 1.0,
         "ion_beam.source.chamber.stat_gv_close_permissive": 1.0, "ion_beam.source.chamber.rb_body_temp": 18.4,
         "ion_beam.loadlock.turbo_pump.stat_comms_fail": 0.0, "ion_beam.loadlock.turbo_pump.stat_error": 0.0,
         "ion_beam.loadlock.turbo_pump.stat_trip": 0.0, "ion_beam.loadlock.turbo_pump.stat_warning": 0.0,
         "ion_beam.beamline.diagnostics.stat_fc_inserted": 0.0, "ion_beam.beamline.diagnostics.stat_fc_retracted": 1.0,
         "ion_beam.beamline.faraday.smu.stat_comms_fail": 0.0, "ion_beam.beamline.faraday.smu.rb_current": 2.3e-6,
         "ion_beam.beamline.faraday.smu.stat_enabled": 1.0,
         "ion_beam.beamline.magnet.rb_current": 45.2, "ion_beam.beamline.magnet.rb_voltage": 2.1,
         "ion_beam.beamline.magnet.stat_enabled": f, "ion_beam.beamline.magnet.sp_actual_current": 45.2,
         "ion_beam.beamline.steering.stat_enabled": f, "ion_beam.beamline.steering.sp_actual_y_volts": 12.0,
         "ion_beam.beamline.steerer_magnet.stat_enabled": f, "ion_beam.beamline.steerer_magnet.rb_current": 1.2,
         "ion_beam.beamline.steerer_magnet.rb_voltage": 0.4}
    for name, (v1, v2) in {"einzel": (20.2, 3.1), "extraction": (20.0, 1.2), "target": (9.0, 2.0),
                           "filament": (27.4, 6.2), "thermionic": (412.0, 205.0)}.items():
        b = f"ion_beam.source.{name}"
        first, second = ("current", "voltage") if name in ("filament", "thermionic") else ("voltage", "current")
        d.update({f"{b}.rb_{first}": v1, f"{b}.rb_{second}": v2, f"{b}.sp_actual_{first}": v1, f"{b}.stat_enabled": f})
    d.update({"ion_beam.source.cesium.rb_temp": 128.0, "ion_beam.source.cesium.out_cv_heating": 34.0,
              "ion_beam.source.cesium.stat_enabled": f, "ion_beam.source.cesium.sp_actual_temp": 130.0})
    for name in ("einzel", "neutral_trap_pos", "neutral_trap_neg"):
        b = f"ion_beam.beamline.{name}"
        d.update({f"{b}.rb_voltage": 1.85, f"{b}.rb_current": 0.4, f"{b}.sp_actual_voltage": 1.85,
                  f"{b}.stat_enabled": f if name == "einzel" else 0.0})
    for tag, amps in {"object_slits.left": 3.2e-6, "object_slits.right": 2.9e-6, "straight_through": 4.1e-9,
                      "image_slits.left": 2.4e-7, "image_slits.right": 1.9e-7, "faraday.front_aperture": 6.4e-8}.items():
        b = f"ion_beam.beamline.{tag}"
        d.update({f"{b}.rb_current": amps, f"{b}.rb_op_mode": 0, f"{b}.rb_range_idx": 2})
    where = {1: "source", 2: "beamline", 3: "beamline", 4: "endstation", 5: "loadlock", 6: "endstation"}
    for n, location in where.items():
        b = f"ion_beam.{location}.vacuum_gauge_{n}"
        d.update({f"{b}.rb_pressure": 3e-7, f"{b}.stat_error_code": 0, f"{b}.stat_relay_active": 1.0,
                  f"{b}.stat_above_sp": 0.0})
    return d


def busy(panels):
    """The longest text each label a diagram copies can show."""
    for ui in panels["currents"].ui_elements:
        ui["val"].setText("OVERRANGE")
        set_ink(ui["val"], "warn")
    for n, g in panels["vacuum"].gauges.items():
        g["status"].setText("STALE 1 h 05 min")
        set_tone(g["status"], "unknown")
    panels["vacuum"].lbl_gv_status.setText("SENSOR CONFLICT")
    set_tone(panels["vacuum"].lbl_gv_status, "fault")
    panels["vacuum"].lbl_turbo_guard.setText("PROTECTION TRIPPED: SOURCE SHUT DOWN")
    panels["vacuum"].lbl_turbo_guard.show()
    panels["faraday"].lbl_digital_current.setText("NO MEASUREMENT")
    for ctrl in panels["source"].psu_controls.values():
        ctrl["lbl_badge"].setText("ARC QUENCH")
        set_tone(ctrl["lbl_badge"], "warn")
        ctrl["lbl_badge"].show()
        ctrl["lbl_rb"].setText("0000.000 kV [?]")
    for ctrl in panels["optics"].psu_controls.values():
        ctrl["lbl_rb"].setText("RB: 00.00 kV | 000.00 µA")


def make(kind: str, panels, shown=None):
    from src.gui.control.diagrams.beamline import BeamPathDiagram
    from src.gui.control.diagrams.safety import SafetyChainDiagram
    from src.gui.control.diagrams.source import TessSourceDiagram
    from src.gui.control.diagrams.vacuum import VacuumDiagram
    cls = {"vacuum": VacuumDiagram, "beam": BeamPathDiagram, "safety": SafetyChainDiagram,
           "tess": TessSourceDiagram}[kind]
    d = cls(panels, (lambda name: shown.append(name)) if shown is not None else None)
    d.resize(1280, 820)
    return d


def feed(panels, diagram, data):
    for w in panels.values():
        w.update_telemetry(data)
    diagram.update_telemetry(data)


@pytest.mark.parametrize("kind", ["vacuum", "beam", "safety", "tess"])
@pytest.mark.parametrize("mode", ["dark", "light"])
def test_each_diagram_draws_with_no_data_and_with_the_machine_running(app, panels, kind, mode):
    from src.gui import theme
    from src.gui.control.diagrams.canvas import layout_problems
    theme.apply_theme(app, mode)
    try:
        d = make(kind, panels)
        for data in ({}, live(False), live(True)):
            feed(panels, d, data)
            layout_problems(d.canvas, QSize(1280, 820))         # (draws it, as on the screen)
            assert d.canvas.legend_box.width() > 0
        d.deleteLater()
    finally:
        theme.apply_theme(app, theme.DEFAULT_MODE)


@pytest.mark.parametrize("kind", ["vacuum", "beam", "safety", "tess"])
@pytest.mark.parametrize("texts", ["normal", "longest"])
def test_no_label_overlaps_another_a_part_a_pipe_or_the_edge(app, panels, kind, texts):
    """At the readable size and smaller (the drawing shrinks to fit), a usual size and a large one,
    with the machine running and with every label at the longest it can be."""
    from src.gui.control.diagrams.canvas import layout_problems
    d = make(kind, panels)
    feed(panels, d, live(True))
    if texts == "longest":
        busy(panels)
        d.update_telemetry(live(True))
    for size in SIZES:
        size = d.READABLE if size == "readable" else d.READABLE * size if isinstance(size, float) else size
        assert layout_problems(d.canvas, size) == [], (kind, size)
    d.deleteLater()


@pytest.mark.parametrize("kind", ["vacuum", "beam", "safety", "tess"])
def test_a_small_panel_shrinks_the_drawing_to_fit_rather_than_cropping_it(app, panels, kind):
    """Josh's laptop (6 Nov): a diagram cut off in a small panel. Below its readable size the whole
    drawing shrinks, so every part is in view; the panel scrolls only below canvas.SMALLEST."""
    from PyQt6.QtCore import QRectF
    from src.gui.control.diagrams.canvas import SMALLEST
    d = make(kind, panels)
    feed(panels, d, live(True))
    assert d.minimumSizeHint().width() <= SMALLEST.width() and d.minimumSizeHint().height() <= SMALLEST.height()
    for size in (d.READABLE, d.READABLE * 0.5, QSize(d.READABLE.width(), SMALLEST.height()), SMALLEST):
        d.canvas.resize(size)
        seen = QRectF(d.canvas.rect())
        assert d.canvas.zoom() == pytest.approx(1.0) if size == d.READABLE else d.canvas.zoom() < 1
        for part in d.parts():
            assert seen.contains(d.canvas.box_on_screen(part.key)), (kind, size, part.key)
    d.deleteLater()


def test_the_layout_check_finds_what_it_should(app, panels):
    """A label moved on to a pipe, and two on top of each other, are found."""
    from src.gui.control.diagrams import canvas
    from src.gui.control.diagrams.vacuum import VacuumDiagram
    d = make("vacuum", panels)
    feed(panels, d, live(True))
    original = VacuumDiagram._labels

    def crowded(self, v):
        original(self, v)
        v.text(215, 500, "on the column", "name")             # (the source column runs down x = 215)
        v.text(900, 700, "one", "name")
        v.text(905, 700, "two", "name")
    VacuumDiagram._labels = crowded
    try:
        problems = canvas.layout_problems(d.canvas, QSize(1280, 820))
    finally:
        VacuumDiagram._labels = original
    assert "'on the column' crosses a line" in problems
    assert "'one' overlaps 'two'" in problems
    d.deleteLater()


def plate_buttons(plate):
    return [b for b in plate.findChildren(QPushButton) if hasattr(b, "source")]


def test_gv2_opens_from_its_faceplate_only_as_from_its_panel(app, panels, cmd):
    vac = panels["vacuum"]
    d = make("vacuum", panels)
    data = live(False)
    data["ion_beam.source.chamber.stat_gv_open_permissive"] = 0.0
    feed(panels, d, data)
    d.open_faceplate("GV2")
    plate = d.faceplate
    open_btn = next(b for b in plate_buttons(plate) if b.source is vac.btn_gv_open)
    assert not vac.btn_gv_open.isEnabled() and not open_btn.isEnabled()      # no permissive: not here either
    open_btn.click()
    assert "ion_beam.source.chamber.cmd_open_gv" not in cmd.tags()
    data["ion_beam.source.chamber.stat_gv_open_permissive"] = 1.0
    feed(panels, d, data)
    assert vac.btn_gv_open.isEnabled() and open_btn.isEnabled()
    open_btn.click()
    sent = [(tag, value) for _t, tag, value in cmd.sent if tag.endswith("cmd_open_gv")]
    assert sent and sent[-1][1] in (True, 1, 1.0)
    d.deleteLater()


def test_the_source_turbo_can_only_be_started(app, panels, cmd):
    vac = panels["vacuum"]
    d = make("vacuum", panels)
    feed(panels, d, live(True))
    d.open_faceplate("turbo_source")
    buttons = plate_buttons(d.faceplate)
    assert [b.source for b in buttons] == [vac.btn_turbo_start]
    assert not any("stop" in b.text().lower() for b in d.faceplate.findChildren(QPushButton))
    d.deleteLater()


def test_switching_a_supply_on_takes_two_clicks_and_the_panels_tag(app, panels, cmd):
    src = panels["source"]
    d = make("tess", panels)
    data = live(True)
    data.update({"ion_beam.source.extraction.stat_enabled": 0.0, "ion_beam.source.extraction.permit": 1.0})
    feed(panels, d, data)
    d.open_faceplate("extraction")
    btn = next(b for b in plate_buttons(d.faceplate) if b.source is src.psu_controls["Extraction"]["btn_enable"])
    assert btn.isEnabled() and src.psu_controls["Extraction"]["btn_enable"].isEnabled()
    btn.click()
    assert "ion_beam.source.extraction.cmd_enable" not in cmd.tags()
    assert btn.text().startswith("CONFIRM")                   # as the panel's own button shows
    btn.click()
    assert ("ion_beam.source.extraction.cmd_enable", True) in [(t, v) for _x, t, v in cmd.sent]
    data["ion_beam.source.extraction.permit"] = 0.0
    feed(panels, d, data)
    assert not btn.isEnabled()                                # locked on the panel: locked here too
    d.deleteLater()


def test_a_value_typed_in_a_faceplate_goes_out_on_the_panels_tag(app, panels, cmd):
    from src.gui.control.diagrams.mirror import MirrorSetpoint
    opt = panels["optics"]
    d = make("beam", panels)
    feed(panels, d, live(True))
    d.open_faceplate("magnet")
    box = next(b for b in d.faceplate.findChildren(MirrorSetpoint) if b.source is opt.sp_mag_current)
    box.committed.emit(46.5)
    assert ("ion_beam.beamline.magnet.sp_requested_current", 46.5) in [(t, v) for _x, t, v in cmd.sent]
    d.deleteLater()


def test_a_slit_width_entered_in_a_faceplate_shows_in_the_panel_too(app, panels, cmd):
    from src.gui.control.diagrams.mirror import MirrorSetpoint
    opt = panels["optics"]
    d = make("beam", panels)
    feed(panels, d, live(True))
    d.open_faceplate("object_slits")
    left = next(b for b in d.faceplate.findChildren(MirrorSetpoint) if b.source is opt.sp_obj_l)
    left.committed.emit(3.5)
    assert opt.sp_obj_l.value() == pytest.approx(3.5)
    opt.sp_obj_r.setValue(2.25)
    d.update_telemetry(live(True))
    right = next(b for b in d.faceplate.findChildren(MirrorSetpoint) if b.source is opt.sp_obj_r)
    assert right.value() == pytest.approx(2.25)
    d.deleteLater()


def test_the_cup_moves_from_its_faceplate(app, panels, cmd):
    smu = panels["faraday"]
    d = make("beam", panels)
    feed(panels, d, live(True))
    d.open_faceplate("fc")
    btn = next(b for b in plate_buttons(d.faceplate) if b.source is smu.btn_fc)
    assert btn.isEnabled() == smu.btn_fc.isEnabled()
    if smu.btn_fc.isEnabled():
        btn.click()
        assert "ion_beam.beamline.diagnostics.cmd_insert_fc" in cmd.tags()
    d.deleteLater()


def test_the_beams_path(app, panels):
    d = make("beam", panels)
    data = live(True)
    feed(panels, d, data)
    assert d.route() == ["column", "column-on", "bend", "station"]
    data.update({"ion_beam.beamline.diagnostics.stat_fc_inserted": 1.0,
                 "ion_beam.beamline.diagnostics.stat_fc_retracted": 0.0})
    feed(panels, d, data)
    assert d.route() == ["column", "column-on", "bend-to-cup"]
    data["ion_beam.beamline.magnet.stat_enabled"] = 0.0
    feed(panels, d, data)
    assert d.route() == ["column", "column-on", "straight"]
    data.update({"ion_beam.source.chamber.stat_gv_open": 0.0, "ion_beam.source.chamber.stat_gv_closed": 1.0})
    feed(panels, d, data)
    assert d.route() == ["column"]
    feed(panels, d, live(False))
    assert d.route() == []
    d.deleteLater()


def test_the_safety_chain_draws_the_unseen_switches_made_only_with_the_relay_closed(app, panels):
    from src.core import interlocks as il
    d = make("safety", panels)
    feed(panels, d, live(True))
    assert d.relay_closed() is True and d.enable_given() is True
    hidden = d._hidden()
    assert hidden.state == il.OK
    data = live(False)
    data.update({"ion_beam.system.cmd_enable_safety": 1.0, "ion_beam.facilities.stat_src_coolant_ok": 0.0})
    feed(panels, d, data)
    assert d.relay_closed() is False and d.enable_given() is False
    coolant = d.cond("Source coolant flow")
    assert coolant.state == il.HOLDING and d.tone_of(coolant) == "warn"      # asked for, and held off
    assert d.tone_of(d._hidden()) is None                                     # unseen: dotted
    d.deleteLater()


def test_the_relays_faceplate_has_system_healths_buttons(app, panels, cmd):
    health = panels["health"]
    d = make("safety", panels)
    feed(panels, d, live(False))
    d.open_faceplate("relay")
    sources = [b.source for b in plate_buttons(d.faceplate)]
    assert sources == [health.btn_enable_safety, health.btn_reset_faults, health.btn_shutdown,
                       health.btn_cancel_shutdown]
    enable = plate_buttons(d.faceplate)[0]
    assert enable.text() == health.btn_enable_safety.text() and enable.isEnabled() == health.btn_enable_safety.isEnabled()
    d.deleteLater()


def test_the_loadlock_turbo_isnt_drawn_running_when_it_only_reports_no_fault(app, panels):
    from src.gui.control.diagrams.vacuum import VacuumDiagram
    data = live(True)
    assert VacuumDiagram._loadlock_state(data) == ("off", "NO FAULT")
    data["ion_beam.loadlock.turbo_pump.stat_trip"] = 1.0
    assert VacuumDiagram._loadlock_state(data) == ("fault", "TRIPPED")
    assert VacuumDiagram._loadlock_state({}) == ("unknown", "NO DATA")


def test_clicking_parts_opens_and_closes_faceplates_and_folds_the_key(app, panels):
    from PyQt6.QtCore import QPointF
    from PyQt6.QtGui import QMouseEvent
    from PyQt6.QtCore import QEvent, Qt
    d = make("vacuum", panels)
    d.show()
    feed(panels, d, live(True))
    app.processEvents()
    canvas = d.canvas

    def click(point):
        for kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
            canvas.event(QMouseEvent(kind, QPointF(point), QPointF(canvas.mapToGlobal(point.toPoint())),
                                     Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                                     Qt.KeyboardModifier.NoModifier))
        app.processEvents()

    click(canvas.box_on_screen("VG3").center())
    assert d.faceplate is not None and d.faceplate.key == "VG3" and canvas.selected == "VG3"
    seen = d.visibleRegion().boundingRect()
    assert (seen if not seen.isEmpty() else d.rect()).contains(d.faceplate.geometry())     # inside the panel
    click(canvas.box_on_screen("turbo_source").center())
    assert d.faceplate.key == "turbo_source"
    click(QPointF(canvas.width() * 0.5, 4))                                   # empty space closes it
    assert d.faceplate is None and canvas.selected == ""
    assert canvas.legend_open
    click(canvas.legend_box.center())
    assert not canvas.legend_open
    d.deleteLater()


@pytest.mark.parametrize("kind", ["vacuum", "beam", "safety", "tess"])
def test_opening_a_faceplate_flashes_up_no_window_and_shows_it_in_its_place(app, panels, kind):
    """Josh saw a small empty window flicker before a faceplate appeared (6 Nov): its controls were
    shown before they were in the card, and a widget shown with no parent is a window of its own (on
    Windows, with its title bar). And the card is placed before it is shown, never seen elsewhere."""
    from PyQt6.QtCore import QEvent, QObject
    from PyQt6.QtWidgets import QWidget
    from src.gui.control.diagrams.faceplate import Faceplate

    class Spy(QObject):
        def __init__(self):
            super().__init__()
            self.windows, self.shown_at = [], {}

        def eventFilter(self, obj, event):
            if event.type() == QEvent.Type.Show and isinstance(obj, QWidget):
                if obj.isWindow():
                    self.windows.append(type(obj).__name__)
                if isinstance(obj, Faceplate):
                    self.shown_at[obj.key] = obj.geometry()
            return False

    d = make(kind, panels)
    d.show()
    feed(panels, d, live(True))
    app.processEvents()
    spy = Spy()
    app.installEventFilter(spy)
    try:
        keys = [part.key for part in d.parts() if part.clickable]
        assert keys
        for key in keys:
            d.open_faceplate(key)
            app.processEvents()
            if d.faceplate is not None:
                assert spy.shown_at.get(key) == d.faceplate.geometry(), key
    finally:
        app.removeEventFilter(spy)
    assert spy.windows == []
    assert len(spy.shown_at) >= len(keys) // 2          # (most parts have a faceplate)
    d.open_faceplate("")
    d.close()
    d.deleteLater()


def test_a_refresh_repaints_only_what_changed(app, panels):
    """The panels refresh ten times a second: the same telemetry repaints nothing, a reading that
    changed only its own text (the machine drawn once, kept), and a valve that moved all of it."""
    from PyQt6.QtCore import QEvent, QObject

    class Paints(QObject):
        def __init__(self):
            super().__init__()
            self.regions = []

        def eventFilter(self, obj, event):
            if event.type() == QEvent.Type.Paint:
                self.regions.append(event.region().boundingRect())
            return False

    d = make("vacuum", panels)
    d.show()
    data = live(True)
    feed(panels, d, data)
    app.processEvents()
    paints = Paints()
    d.canvas.installEventFilter(paints)
    whole = d.canvas.rect()

    feed(panels, d, data)
    app.processEvents()
    assert paints.regions == []                         # nothing new: nothing repainted

    layer = d.canvas._layer
    data = dict(data, **{"ion_beam.beamline.vacuum_gauge_3.rb_pressure": 5.2e-8})
    feed(panels, d, data)
    app.processEvents()
    assert paints.regions and all(r.width() * r.height() < whole.width() * whole.height() / 20 for r in paints.regions)
    assert d.canvas._layer is layer                     # the machine wasn't drawn again

    paints.regions.clear()
    data = dict(data, **{"ion_beam.source.chamber.stat_gv_open": 0.0, "ion_beam.source.chamber.stat_gv_closed": 1.0})
    feed(panels, d, data)
    app.processEvents()
    assert whole in paints.regions and d.canvas._layer is not layer
    d.canvas.removeEventFilter(paints)
    d.close()
    d.deleteLater()


def test_a_faceplate_taller_than_its_panel_scrolls_inside_it(app, panels):
    """The PLC's card is taller than a laptop's Safety Chain panel: it stays inside the panel, its
    title and its link to the panel showing, and its contents scroll between them."""
    d = make("safety", panels)
    d.show()
    feed(panels, d, live(True))
    for size, scrolls in ((QSize(1280, 820), False), (QSize(700, 330), True)):
        d.resize(size)
        app.processEvents()
        d.open_faceplate("plc")
        app.processEvents()
        plate = d.faceplate
        assert d.rect().contains(plate.geometry()), size
        assert (plate.scroll.verticalScrollBar().maximum() > 0) == scrolls, size
        links = [b for b in plate.findChildren(QPushButton) if b.property("role") == "link"]
        assert links and all(plate.rect().contains(b.geometry()) for b in links)
        d.open_faceplate("")
    d.close()
    d.deleteLater()


def test_a_faceplates_link_brings_its_panel(app, panels):
    shown = []
    d = make("beam", panels, shown)
    feed(panels, d, live(True))
    d.open_faceplate("steerers")
    links = [b for b in d.faceplate.findChildren(QPushButton) if b.property("role") == "link"]
    assert links
    links[0].click()
    assert shown == ["optics"]
    d.deleteLater()


# --- In the HMI ----------------------------------------------------------------------------------
@pytest.fixture
def hmi(app):
    from src.gui import theme
    theme.apply_theme(app, theme.DEFAULT_MODE)
    from src.gui.control.control_main import ControlMainWindow
    w = ControlMainWindow()
    w.watchdog_timer.stop()
    w.show()
    app.processEvents()
    yield w
    w.close()
    w.deleteLater()
    app.processEvents()


def test_each_diagram_is_a_tab_beside_its_panel(hmi):
    pairs = {hmi.vac_diagram_dock: hmi.vac_dock, hmi.beam_diagram_dock: hmi.optics_dock,
             hmi.safety_diagram_dock: hmi.interlocks_dock, hmi.source_diagram_dock: hmi.src_dock}
    hmi._arrange_default()
    for diagram_dock, twin in pairs.items():
        assert diagram_dock in hmi.tabifiedDockWidgets(twin), diagram_dock.objectName()
    assert hmi.on_screen(hmi.vac_dock) and not hmi.on_screen(hmi.vac_diagram_dock)  # the panels stay in front


def test_a_diagram_in_view_keeps_the_panels_it_draws_from_up_to_date(hmi, app, monkeypatch):
    hmi._arrange_default()
    app.processEvents()
    hmi.beam_diagram_dock.raise_()
    app.processEvents()
    assert hmi.on_screen(hmi.beam_diagram_dock) and not hmi.on_screen(hmi.optics_dock)
    refreshed = []
    for w in (hmi.optics_widget, hmi.smu_widget, hmi.beam_diagram, hmi.mass_scan_widget):
        monkeypatch.setattr(w, "update_telemetry", lambda data, w=w: refreshed.append(w))
    monkeypatch.setattr(hmi.telemetry, "view", lambda: live(True))
    hmi._ui_update_loop()
    assert hmi.optics_widget in refreshed and hmi.smu_widget in refreshed       # their docks hidden behind it
    assert refreshed.index(hmi.optics_widget) < refreshed.index(hmi.beam_diagram)
    assert hmi.mass_scan_widget in refreshed                                    # (always refreshed anyway)


def test_show_panel_brings_the_panel_forward(hmi, app):
    hmi._arrange_default()
    app.processEvents()
    hmi.beam_diagram_dock.raise_()
    app.processEvents()
    hmi.show_panel("optics")
    app.processEvents()
    assert hmi.on_screen(hmi.optics_dock)
    hmi.interlocks_dock.close()
    hmi.show_panel("interlocks")
    app.processEvents()
    assert hmi.on_screen(hmi.interlocks_dock)


def test_a_saved_layout_from_before_the_diagrams_puts_them_beside_their_panels(hmi, app):
    hmi._arrange_default()
    app.processEvents()
    for dock in (hmi.vac_diagram_dock, hmi.beam_diagram_dock, hmi.safety_diagram_dock, hmi.source_diagram_dock):
        hmi.removeDockWidget(dock)
    hmi._place_new_panels()
    app.processEvents()
    assert hmi.vac_diagram_dock in hmi.tabifiedDockWidgets(hmi.vac_dock)
    assert hmi.source_diagram_dock in hmi.tabifiedDockWidgets(hmi.src_dock)
    assert hmi.safety_diagram_dock in hmi.tabifiedDockWidgets(hmi.interlocks_dock)
    assert hmi.on_screen(hmi.vac_dock) and not hmi.on_screen(hmi.vac_diagram_dock)
