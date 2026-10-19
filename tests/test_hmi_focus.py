"""The operator HMI's keyboard focus (src/gui/control/control_main.py), offscreen, no services: no
control has it at start-up, and it comes back to the window - not the next control - when the
focused control is locked or its panel goes away. Entries dropped on the way are reported."""
import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication


@pytest.fixture
def hmi():
    app = QApplication.instance() or QApplication([])
    from src.gui.control.control_main import ControlMainWindow
    w = ControlMainWindow()
    w.watchdog_timer.stop()            # no telemetry here: the panels keep what the test sets
    w.resize(1600, 900)
    w.show()
    w.activateWindow()
    QTest.qWaitForWindowActive(w, 2000)
    app.processEvents()
    notices = []
    w._show_notice = notices.append    # (instead of the pop-up)
    for box in w.findChildren(type(w.src_widget.psu_controls["Target"]["sp_box"])):
        box.notice.disconnect()
        box.notice.connect(lambda m: notices.append(m))
    yield app, w, notices
    w.close()
    w.deleteLater()
    app.processEvents()


def test_no_control_has_the_keyboard_at_start_up(hmi):
    app, w, _ = hmi
    assert QApplication.focusWidget() in (None, w)


def test_a_locked_button_or_box_hands_the_keyboard_to_the_window(hmi):
    """A relay trip locks a whole panel: Qt would pass the focus along the panel, and Space on a
    button it lands on (FORCED COOLING, say) would act."""
    app, w, notices = hmi
    w.src_dock.raise_()
    app.processEvents()
    btn = w.src_widget.psu_controls["Extraction"]["btn_enable"]
    btn.setFocus(Qt.FocusReason.MouseFocusReason)
    app.processEvents()
    assert btn.hasFocus()
    btn.setEnabled(False)
    app.processEvents()
    assert QApplication.focusWidget() is w
    box = w.src_widget.psu_controls["Target"]["sp_box"]
    box.show_setpoint(5.0)
    box.setFocus(Qt.FocusReason.MouseFocusReason)
    box.lineEdit().selectAll()
    QTest.keyClicks(box, "6")
    box.setEnabled(False)
    app.processEvents()
    assert QApplication.focusWidget() is w and not box.editing
    assert notices and notices[-1].startswith("Target setpoint: 6 not sent")


def test_switching_a_dock_tab_away_drops_an_unsent_entry(hmi):
    app, w, notices = hmi
    w.src_dock.raise_()
    app.processEvents()
    box = w.src_widget.psu_controls["Extraction"]["sp_box"]
    box.setEnabled(True)
    box.show_setpoint(10.0)
    box.setFocus(Qt.FocusReason.MouseFocusReason)
    box.lineEdit().selectAll()
    QTest.keyClicks(box, "12")
    assert box.editing
    w.optics_dock.raise_()                       # tabbed with the source panel
    app.processEvents()
    assert not box.editing and box.value() == 10.0
    assert QApplication.focusWidget() is w
    assert "switched away" in notices[-1]


def test_a_control_locked_while_the_hmi_was_in_the_background(hmi):
    """Qt moves the keyboard to the next control without a word then; coming back (alt-tab), it would
    be sitting on a control the operator never chose."""
    from PyQt6.QtWidgets import QLineEdit
    app, w, _ = hmi
    w.src_dock.raise_()
    app.processEvents()
    btn = w.src_widget.psu_controls["Extraction"]["btn_enable"]
    btn.setFocus(Qt.FocusReason.MouseFocusReason)
    app.processEvents()
    away = QLineEdit()
    try:
        away.show()
        away.activateWindow()
        QTest.qWaitForWindowActive(away, 2000)
        btn.setEnabled(False)                    # locked meanwhile
        w.activateWindow()
        QTest.qWaitForWindowActive(w, 2000)
        app.processEvents()
        assert QApplication.focusWidget() in (None, w)
        target = w.src_widget.psu_controls["Target"]["sp_box"]       # and clicking a control still works
        target.setFocus(Qt.FocusReason.MouseFocusReason)
        app.processEvents()
        assert QApplication.focusWidget() is target
    finally:
        away.close()
        away.deleteLater()
