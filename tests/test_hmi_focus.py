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


def test_a_click_on_empty_panel_space_sends_nothing(hmi):
    """October 2026 (found in review before release): with every panel in a scroll area, a click on
    an empty part of a panel moved the keyboard focus to the scroll area - and a typed setpoint not
    yet entered counted as "moved on to another control" and was sent. Empty space must leave the
    focus, and the entry, where they are."""
    from PyQt6.QtCore import QPoint
    app, w, notices = hmi
    sent = []
    w.cmd_thread.send_command = lambda *a, **k: sent.append(a)
    w.src_dock.raise_()
    app.processEvents()
    box = w.src_widget.psu_controls["Extraction"]["sp_box"]
    box.setEnabled(True)
    box.show_setpoint(10.0)
    box.setFocus(Qt.FocusReason.MouseFocusReason)
    box.lineEdit().selectAll()
    QTest.keyClicks(box, "12")
    assert box.editing
    panel = w.src_widget                         # below the supplies: empty panel space
    QTest.mouseClick(panel, Qt.MouseButton.LeftButton, pos=QPoint(panel.width() - 3, panel.height() - 3))
    app.processEvents()
    assert sent == [] and box.editing and box.hasFocus()


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


def test_coming_back_by_clicking_a_box_gives_it_the_keyboard(hmi):
    """October 2026 (Josh, in the simulator): back in the HMI by clicking the filament's setpoint box -
    after the control he had used last was hidden while the HMI was in the background - the keyboard
    was parked on the window: the box showed the text he selected, but "29" and Enter went nowhere,
    with no message. A control the operator has just clicked keeps the keyboard."""
    from PyQt6.QtCore import QPoint
    from PyQt6.QtWidgets import QLineEdit
    app, w, notices = hmi
    sent = []
    w.cmd_thread.send_command = lambda route, tag, value, *a, **k: sent.append((tag, value))
    w.src_dock.raise_()
    app.processEvents()
    used_last = w.health_widget.btn_reset_faults
    used_last.setEnabled(True)
    used_last.setFocus(Qt.FocusReason.MouseFocusReason)
    app.processEvents()
    box = w.src_widget.psu_controls["Filament"]["sp_box"]
    box.setEnabled(True)
    box.show_setpoint(28.01)
    away = QLineEdit()
    try:
        away.show()
        away.activateWindow()
        QTest.qWaitForWindowActive(away, 2000)
        used_last.hide()                         # hidden meanwhile: Qt leaves the focus with it
        edit = box.lineEdit()                    # the click that brings the HMI back, selecting the text
        QTest.mousePress(edit, Qt.MouseButton.LeftButton, pos=QPoint(2, edit.height() // 2))
        QTest.mouseMove(edit, QPoint(edit.width() - 2, edit.height() // 2))
        QTest.mouseRelease(edit, Qt.MouseButton.LeftButton, pos=QPoint(edit.width() - 2, edit.height() // 2))
        w.activateWindow()
        QTest.qWaitForWindowActive(w, 2000)
        app.processEvents()
        assert QApplication.focusWidget() is box
        box.lineEdit().selectAll()
        for key in "29":
            QTest.keyClick(QApplication.focusWidget(), key)
        QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Return)
        assert sent == [("ion_beam.source.filament.sp_requested_current", 29.0)]
    finally:
        used_last.show()
        away.close()
        away.deleteLater()


def test_typing_that_reaches_the_window_says_so(hmi):
    """Digits or Enter with no box holding the keyboard (it was parked on the window, or a button has
    it) went nowhere without a word: now a notice says so (once in a while, not for every key)."""
    app, w, notices = hmi
    w.setFocus(Qt.FocusReason.OtherFocusReason)
    app.processEvents()
    QTest.keyClicks(w, "29")
    QTest.keyClick(w, Qt.Key.Key_Return)
    lost = [n for n in notices if "went into no setpoint box" in n]
    assert len(lost) == 1
    btn = w.src_widget.psu_controls["Extraction"]["btn_enable"]      # a button takes no digits either
    btn.setEnabled(True)
    btn.setFocus(Qt.FocusReason.MouseFocusReason)
    w._lost_keys_shown = 0.0
    QTest.keyClicks(btn, "5")
    assert len([n for n in notices if "went into no setpoint box" in n]) == 2


def test_the_filament_steps_by_10_ma(hmi):
    """Its arrows stepped 1 mA: nine clicks took it from 28.5 to 28.509 A (Oct 2026)."""
    app, w, _ = hmi
    assert w.src_widget.psu_controls["Filament"]["sp_box"].singleStep() == 0.01
