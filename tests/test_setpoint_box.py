"""Setpoint boxes send only what the operator typed or stepped (src/gui/control/setpoint_box.py).

Qt reports 'editing finished' on every focus loss; the HMI's boxes used to send their value then,
so at start-up a box holding its default 0 sent it when the first telemetry locked it."""
import pytest
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QFocusEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLineEdit, QVBoxLayout, QWidget


@pytest.fixture
def rig():
    app = QApplication.instance() or QApplication([])
    from src.gui.control.setpoint_box import SetpointBox
    win = QWidget()
    lay = QVBoxLayout(win)
    box = SetpointBox()
    box.setRange(-100.0, 100.0)
    box.setDecimals(1)
    other = QLineEdit()          # somewhere else to click
    lay.addWidget(box)
    lay.addWidget(other)
    sent = []
    box.committed.connect(sent.append)
    win.show()
    win.activateWindow()
    QTest.qWaitForWindowActive(win, 2000)
    yield app, box, other, sent
    win.close()
    win.deleteLater()
    app.processEvents()


def focus(app, widget, reason=Qt.FocusReason.MouseFocusReason):
    widget.setFocus(reason)
    app.processEvents()


def type_in(app, box, text):
    focus(app, box)
    box.lineEdit().selectAll()
    QTest.keyClicks(box.lineEdit(), text)
    app.processEvents()


def test_focus_passing_through_sends_nothing(rig):
    app, box, other, sent = rig
    box.show_setpoint(12.0)
    for _ in range(3):
        focus(app, box)
        focus(app, other)
        focus(app, box, Qt.FocusReason.TabFocusReason)
        QTest.keyClick(box.lineEdit(), Qt.Key.Key_Return)          # Enter on an untouched box too
        focus(app, other, Qt.FocusReason.TabFocusReason)
    assert sent == [] and box.value() == 12.0


def test_typed_value_is_sent_on_enter_once(rig):
    app, box, other, sent = rig
    type_in(app, box, "42.5")
    assert box.editing
    QTest.keyClick(box.lineEdit(), Qt.Key.Key_Return)
    assert sent == [42.5] and not box.editing
    QTest.keyClick(box.lineEdit(), Qt.Key.Key_Return)
    focus(app, other)
    assert sent == [42.5]


def test_typed_value_is_sent_when_focus_moves_on(rig):
    app, box, other, sent = rig
    type_in(app, box, "-7")
    focus(app, other)                                              # clicked elsewhere in the HMI
    assert sent == [-7.0]


def test_stepping_counts_as_an_edit(rig):
    app, box, other, sent = rig
    box.show_setpoint(10.0)
    focus(app, box)
    QTest.keyClick(box, Qt.Key.Key_Up)                             # one single step (1.0)
    assert box.editing and box.value() == 11.0
    focus(app, other)
    assert sent == [11.0]


def test_a_box_locked_while_being_edited_drops_the_edit(rig):
    """The permit went (relay off, a sequence took the supply): nothing is sent, the live value is shown."""
    app, box, other, sent = rig
    box.show_setpoint(3.0)
    type_in(app, box, "55")
    box.setEnabled(False)                                          # Qt moves the focus on: editingFinished
    app.processEvents()
    assert sent == [] and not box.editing and box.value() == 3.0
    box.setEnabled(True)
    box.show_setpoint(4.0)
    assert box.value() == 4.0


def test_a_box_locked_at_start_up_sends_its_default_never(rig):
    """The October 2026 start-up: the focused box still showed its default (no telemetry yet) and the
    first telemetry locked it."""
    app, box, other, sent = rig
    focus(app, box)
    box.setEnabled(False)
    app.processEvents()
    assert sent == []


def test_the_window_losing_focus_keeps_the_edit_for_later(rig):
    app, box, other, sent = rig
    type_in(app, box, "8")
    QApplication.sendEvent(box, QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.ActiveWindowFocusReason))
    assert sent == [] and box.editing                              # another program / a dialog took over
    box.show_setpoint(1.0)
    assert box.value() == 8.0                                      # the operator's value stays shown
    QTest.keyClick(box.lineEdit(), Qt.Key.Key_Return)              # back, and Enter
    assert sent == [8.0]


def test_locked_while_the_window_was_away_drops_the_edit(rig):
    app, box, other, sent = rig
    box.show_setpoint(2.0)
    type_in(app, box, "9")
    QApplication.sendEvent(box, QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.ActiveWindowFocusReason))
    box.setEnabled(False)
    assert not box.editing and box.value() == 2.0
    box.setEnabled(True)
    focus(app, box)
    focus(app, other)
    assert sent == []


def test_escape_drops_the_edit(rig):
    app, box, other, sent = rig
    box.show_setpoint(5.0)
    type_in(app, box, "60")
    QTest.keyClick(box.lineEdit(), Qt.Key.Key_Escape)
    assert not box.editing and box.value() == 5.0
    focus(app, other)
    assert sent == []


def test_follows_the_live_setpoint_until_the_operator_edits(rig):
    """Focused but untouched, it shows the live value (it used to freeze while focused - at start-up
    that was its default 0, one arrow-key step from sending ~0)."""
    app, box, other, sent = rig
    focus(app, box)
    box.show_setpoint(7.0)
    assert box.value() == 7.0
    box.show_setpoint(None)                                        # unknown: keeps the last
    assert box.value() == 7.0
    type_in(app, box, "1")
    box.show_setpoint(8.0)
    assert box.lineEdit().text().startswith("1")                   # not overwritten while typing
    QTest.keyClick(box.lineEdit(), Qt.Key.Key_Return)
    assert sent == [1.0]
    box.show_setpoint(8.0)
    assert box.value() == 8.0


def test_values_set_by_the_program_are_not_edits(rig):
    app, box, other, sent = rig
    box.setValue(4.0)
    focus(app, box)
    focus(app, other)
    assert sent == [] and not box.editing


def test_escape_without_a_live_value_goes_back_to_what_was_shown(rig):
    """Boxes no telemetry feeds (the mass calibration): Escape restores the value shown before the
    edit - and tells valueChanged listeners - instead of leaving the typed text to be taken in later."""
    app, box, other, sent = rig
    box.setValue(13.9)
    seen = []
    box.valueChanged.connect(seen.append)
    type_in(app, box, "50")
    QTest.keyClick(box.lineEdit(), Qt.Key.Key_Escape)
    focus(app, other)
    assert sent == [] and box.value() == pytest.approx(13.9) and box.lineEdit().text().startswith("13.9")
    assert 50.0 not in seen
    focus(app, box)
    QTest.keyClick(box, Qt.Key.Key_Up)                             # a step, then Escape
    QTest.keyClick(box.lineEdit(), Qt.Key.Key_Escape)
    assert box.value() == pytest.approx(13.9) and seen[-1] == pytest.approx(13.9) and sent == []
