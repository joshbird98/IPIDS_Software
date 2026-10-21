"""The Interlocks panel (src/gui/control/widgets/interlocks_widget.py), offscreen: it draws what
src/core/interlocks.py works out, folds its cards, follows its links, survives a theme switch, and
finds its place in a layout saved before it existed."""
import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QDockWidget, QFrame, QScrollArea

from src.core import interlocks as il
from src.core.fault_registry import FaultRegistry
from src.core.paths import config_path
from src.gui.style import tone
from tests.test_interlocks import NOW, machine, refused


@pytest.fixture
def panel(tmp_path, monkeypatch):
    monkeypatch.setenv("SINDIE_SETTINGS_DIR", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    from src.gui.control.widgets.interlocks_widget import InterlocksWidget
    w = InterlocksWidget(FaultRegistry(config_path("fault_config.json")), wall_clock=lambda: NOW)
    w.disagreements.hold_s = 0.0
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setWidget(w)
    area.resize(520, 700)
    area.show()
    app.processEvents()
    yield app, w, area
    area.close()
    area.deleteLater()
    app.processEvents()


def rows(card):
    return [r for r in card.chain.rows if r.cond is not None]


def test_it_shows_what_holds_each_permissive(panel):
    app, w, _ = panel
    w.update_telemetry(machine())
    app.processEvents()
    relay = w.cards["relay"]
    assert relay.status.text() == "OFF" and tone(relay.status) == "off"
    assert relay.summary.text() == "Held off by the PLC: the gate valve is closed."
    first = [r for r in rows(relay) if r.tint]
    assert [r.cond.label for r in first] == ["Gate valve open"]
    assert "(see Gate valve)" in first[0].text.text()
    # the rail is solid down to the first condition that doesn't hold, dashed after it
    by_label = {r.cond.label: r for r in rows(relay)}
    assert by_label["Magnet coolant"].rail_below is True and by_label["Gate valve open"].rail_below is False
    assert by_label["Safety relay"].rail_below is None                       # the output ends the chain
    assert w.machine_chip.text() == "SAFE / NOT READY"
    assert "ion_beam.source.chamber.stat_gv_open" in by_label["Gate valve open"].text.toolTip()


def test_a_refusal_says_what_is_left(panel):
    app, w, _ = panel
    w.update_telemetry(refused())
    app.processEvents()
    relay = w.cards["relay"]
    assert relay.status.text() == "REFUSED" and tone(relay.status) == "fault"
    notes = [n for n in relay.notes if not n.isHidden()]
    assert len(notes) == 1 and "START wasn't pressed" in notes[0].text() and notes[0].property("level") == "warn"
    doors = next(r for r in rows(relay) if r.cond.label.startswith("Doors"))
    assert doors.cond.state == il.SUSPECT
    vac = next(r for r in rows(relay) if r.cond.label.startswith("Gauge controller relay"))
    assert vac.cond.state == il.OK and "Ruled out: the gauge controller relay" in notes[0].text()
    w.update_telemetry(machine())              # reset and back to rest: the note goes
    app.processEvents()
    assert all(n.isHidden() for n in relay.notes)


def test_rows_follow_a_changing_list(panel):
    """The faults latched and the services' reasons come and go: the rows are rebuilt to match."""
    app, w, _ = panel
    w.update_telemetry(machine())
    reset = w.cards["reset"]
    n = len(rows(reset))
    data = machine(**{"faults.Safety_Relay_Refused": 1.0, "faults.Air_Pressure_Low": 1.0})
    w.update_telemetry(data)
    app.processEvents()
    assert len(rows(reset)) == n + 2
    assert [r.cond.label for r in rows(reset)[-2:]] == ["Air_Pressure_Low", "Safety_Relay_Refused"]
    assert "Safety relay interlocks not met." in rows(reset)[-1].text.text()      # its fault_config description
    w.update_telemetry(machine())
    assert len(rows(reset)) == n


def test_folding_is_remembered_and_links_unfold(panel, tmp_path):
    app, w, _ = panel
    w.update_telemetry(machine())
    card = w.cards["gv_open"]
    card.toggle()
    assert card.collapsed() and card.btn.text() == "▸"
    from src.gui.control.widgets.interlocks_widget import InterlocksWidget
    again = InterlocksWidget()
    assert again.cards["gv_open"].collapsed() and not again.cards["relay"].collapsed()
    again.deleteLater()
    w._link("group:gv_open")                   # "(see Gate valve)" in the relay's card
    assert not card.collapsed()
    w._link("group:nothing")                   # (an unknown link does nothing)


def test_a_theme_switch_redraws(panel):
    app, w, _ = panel
    from src.gui import theme
    w.update_telemetry(refused())
    before = rows(w.cards["relay"])[0].text.text()
    other = "light" if theme.current_mode() == "dark" else "dark"
    theme.apply_theme(app, other)
    try:
        app.processEvents()
        assert rows(w.cards["relay"])[0].text.text() != before          # the colours in the text changed
    finally:
        theme.apply_theme(app, "dark" if other == "light" else "light")


def test_columns_as_the_width_allows(panel):
    app, w, area = panel
    w.update_telemetry(machine())
    assert w.columns.n == 1
    area.resize(1400, 700)
    app.processEvents()
    assert w.columns.n == 3
    assert w.cards["relay"].parentWidget() is w.columns.columns[0]
    area.resize(820, 700)
    app.processEvents()
    assert w.columns.n == 2


def test_a_layout_saved_before_the_panel_existed(tmp_path, monkeypatch):
    """Restoring it leaves the new panel out of the window; it goes behind System Health instead."""
    monkeypatch.setenv("SINDIE_SETTINGS_DIR", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    from src.gui.control.control_main import ControlMainWindow
    w = ControlMainWindow()
    w.watchdog_timer.stop()
    w.removeDockWidget(w.interlocks_dock)                  # as an older version saved it
    old_state = w.saveState(w.LAYOUT_VERSION)
    w.close()
    w.deleteLater()
    app.processEvents()
    w.settings.setValue("windowState", old_state)
    w.settings.sync()
    w = ControlMainWindow()
    try:
        w.watchdog_timer.stop()
        assert w.dockWidgetArea(w.interlocks_dock) != Qt.DockWidgetArea.NoDockWidgetArea
        assert w.interlocks_dock in w.tabifiedDockWidgets(w.health_dock)
        names = [a.text() for a in w.view_menu.actions()[0].menu().actions()]
        assert "Interlocks" in names
        assert isinstance(w.interlocks_dock, QDockWidget)
    finally:
        w.close()
        w.deleteLater()
        app.processEvents()
