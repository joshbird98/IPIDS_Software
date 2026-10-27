"""Setpoint boxes send only what the operator typed or stepped (src/gui/control/setpoint_box.py).

Qt reports 'editing finished' on every focus loss; the HMI's boxes used to send their value then,
so at start-up a box holding its default 0 sent it when the first telemetry locked it. These tests
drive the boxes with real key and focus events (offscreen Qt)."""
import pytest
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QFocusEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QDockWidget, QLineEdit, QMainWindow, QVBoxLayout, QWidget


def make_box(live=True, lo=-100.0, hi=100.0):
    from src.gui.control.setpoint_box import SetpointBox
    box = SetpointBox(live=live)
    box.setRange(lo, hi)
    box.setDecimals(1)
    return box


class Rig:
    """A window with two setpoint boxes (as in a panel) and a line edit to click into."""

    def __init__(self, app, live=True):
        self.app = app
        self.win = QWidget()
        lay = QVBoxLayout(self.win)
        self.box, self.box2 = make_box(live), make_box(live)
        self.box.setObjectName("Extraction setpoint")
        self.box2.setObjectName("Target setpoint")
        self.other = QLineEdit()
        for w in (self.box, self.box2, self.other):
            lay.addWidget(w)
        self.sent, self.sent2, self.notices = [], [], []
        self.box.committed.connect(self.sent.append)
        self.box2.committed.connect(self.sent2.append)
        self.box.notice.connect(self.notices.append)
        self.win.show()
        self.win.activateWindow()
        QTest.qWaitForWindowActive(self.win, 2000)

    def focus(self, widget, reason=Qt.FocusReason.MouseFocusReason):
        widget.setFocus(reason)
        self.app.processEvents()

    def type_in(self, box, text):
        """Click in, select all, type (keys go to the box, as they do to the focus widget)."""
        self.focus(box)
        box.lineEdit().selectAll()
        QTest.keyClicks(box, text)
        self.app.processEvents()

    def key(self, key, widget=None):
        QTest.keyClick(widget or QApplication.focusWidget() or self.win, key)
        self.app.processEvents()

    def close(self):
        self.win.close()
        self.win.deleteLater()
        self.app.processEvents()


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def rig(app):
    r = Rig(app)
    yield r
    r.close()


def test_focus_passing_through_sends_nothing(rig):
    rig.box.show_setpoint(12.0)
    for _ in range(3):
        rig.focus(rig.box)
        rig.focus(rig.other)
        rig.focus(rig.box, Qt.FocusReason.TabFocusReason)
        rig.key(Qt.Key.Key_Return)                                 # Enter on an untouched box too
        rig.focus(rig.other, Qt.FocusReason.TabFocusReason)
    assert rig.sent == [] and rig.box.value() == 12.0


def test_typed_value_is_sent_on_enter_once(rig):
    rig.box.show_setpoint(1.0)
    rig.type_in(rig.box, "42.5")
    assert rig.box.editing
    rig.key(Qt.Key.Key_Return)
    assert rig.sent == [42.5] and not rig.box.editing
    rig.key(Qt.Key.Key_Return)
    rig.focus(rig.other)
    assert rig.sent == [42.5]


def test_typed_value_is_sent_when_focus_moves_on(rig):
    rig.box.show_setpoint(3.0)
    rig.type_in(rig.box, "-7")
    rig.focus(rig.other)                                           # clicked elsewhere in the HMI
    assert rig.sent == [-7.0]


def test_stepping_counts_as_an_edit(rig):
    rig.box.show_setpoint(10.0)
    rig.focus(rig.box)
    rig.key(Qt.Key.Key_Up)                                         # one single step (1.0)
    assert rig.box.editing and rig.box.value() == 11.0
    rig.focus(rig.other)
    assert rig.sent == [11.0]


def test_a_box_locked_while_being_edited_drops_the_edit_and_says_so(rig):
    """The permit went (relay off, a sequence took the supply): nothing is sent, the live value is
    shown again, and the operator is told."""
    rig.box.show_setpoint(3.0)
    rig.type_in(rig.box, "55")
    rig.box.setEnabled(False)
    assert rig.sent == [] and not rig.box.editing and rig.box.value() == 3.0
    assert len(rig.notices) == 1 and rig.notices[0].startswith("Extraction setpoint: 55 not sent")
    assert "locked while you were typing" in rig.notices[0]
    rig.box.setEnabled(True)
    rig.box.show_setpoint(4.0)
    assert rig.box.value() == 4.0


def test_a_lock_leaves_the_keyboard_with_the_window_not_the_next_supply(rig):
    """Qt passes the focus of a locked widget to the next control, its text selected: the rest of
    the operator's typing ('.5' and Enter) then set another supply (review, Oct 2026)."""
    rig.box.show_setpoint(10.0)
    rig.box2.show_setpoint(6.0)
    rig.type_in(rig.box, "12")
    rig.box.setEnabled(False)                                      # a lease taken, an arc quench, ...
    assert QApplication.focusWidget() in (None, rig.win)
    QTest.keyClicks(QApplication.focusWidget() or rig.win, ".5")
    rig.key(Qt.Key.Key_Return)
    assert rig.sent == [] and rig.sent2 == [] and rig.box2.value() == 6.0 and len(rig.notices) == 1


def test_a_box_locked_at_start_up_sends_its_default_never(rig):
    """The October 2026 start-up: the focused box still showed its default (no telemetry yet) and the
    first telemetry locked it."""
    rig.focus(rig.box)
    rig.box.setEnabled(False)
    assert rig.sent == [] and rig.sent2 == [] and rig.notices == []
    assert QApplication.focusWidget() in (None, rig.win)


def test_text_that_is_not_a_value_is_not_sent(rig):
    """Qt falls back to the old value for empty text or a lone '-', and still reports 'finished': that
    re-sent the old setpoint (or a box's default) as if typed."""
    rig.box.show_setpoint(10.0)
    rig.focus(rig.box)
    rig.box.lineEdit().selectAll()
    rig.key(Qt.Key.Key_Delete)
    rig.key(Qt.Key.Key_Return)                                     # Enter: the edit stays, to be corrected
    assert rig.sent == [] and rig.box.editing and "is not a value" in rig.notices[-1]
    rig.focus(rig.other)                                           # moving on drops it
    assert rig.sent == [] and not rig.box.editing and rig.box.value() == 10.0
    assert "not sent" in rig.notices[-1]
    rig.type_in(rig.box, "-")
    rig.focus(rig.other)
    assert rig.sent == [] and "'-' is not a value" in rig.notices[-1]


def test_unknown_or_out_of_range_live_setpoints_show_empty_and_cant_be_stepped(rig):
    """A step from a number that isn't the real setpoint sends a wrong one: from the default 0 before
    any telemetry, the pre-trip value after the service lost it, or a value Qt clamped to the box."""
    box = rig.box
    assert box.lineEdit().displayText() == ""                     # shown before any telemetry: empty
    rig.focus(box)
    for key in (Qt.Key.Key_Up, Qt.Key.Key_PageDown):
        rig.key(key)
    assert not box.editing
    box.show_setpoint(10.0)
    assert box.lineEdit().displayText() == "10.0"
    box.show_setpoint(None)                                        # the service went quiet
    assert box.lineEdit().displayText() == ""
    rig.key(Qt.Key.Key_Up)
    box.show_setpoint(250.0)                                       # held past the box's range
    assert box.lineEdit().displayText() == ""
    rig.key(Qt.Key.Key_Up)
    rig.focus(rig.other)
    assert rig.sent == []
    rig.type_in(box, "20")                                         # a whole value can still be typed
    rig.key(Qt.Key.Key_Return)
    assert rig.sent == [20.0]


def test_text_put_straight_into_an_empty_box_is_what_is_sent(rig):
    """With the setpoint unknown the box is emptied (Qt's 'cleared' state), and Qt then ignores text
    that didn't come through the box's own key handling - pasted from the line edit's context menu,
    say - keeping its old number (0 here). The value sent comes from the text."""
    box = rig.box
    assert box.lineEdit().displayText() == ""
    rig.focus(box)
    QTest.keyClicks(box.lineEdit(), "4")                           # not through the box: as a paste is
    rig.key(Qt.Key.Key_Return)
    assert rig.sent == [4.0] and box.value() == 4.0


def test_steps_dont_reach_a_locked_box(rig):
    """The context menu's Step up acts after the menu closes - the box may be locked by then."""
    rig.box.show_setpoint(6.0)
    rig.box.setEnabled(False)
    rig.box.stepBy(1)
    assert not rig.box.editing and rig.box.value() == 6.0
    rig.box.setEnabled(True)
    rig.box.show_setpoint(7.0)
    rig.focus(rig.box)
    rig.focus(rig.other)
    assert rig.sent == []


def test_an_edit_made_before_the_setpoint_changed_is_not_sent(rig):
    """Someone else set it meanwhile (often while the HMI was in the background): the operator's
    number was chosen against the old one."""
    rig.box.show_setpoint(10.0)
    rig.type_in(rig.box, "18")
    QApplication.sendEvent(rig.box, QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.ActiveWindowFocusReason))
    rig.box.show_setpoint(5.0)                                     # set elsewhere while away
    assert rig.box.editing and rig.box.lineEdit().displayText() == "18.0"
    rig.focus(rig.other)                                           # the click that brings them back
    assert rig.sent == [] and "changed to 5 while you were typing" in rig.notices[-1]
    assert rig.box.value() == 5.0


def test_the_operators_own_previous_entry_arriving_is_not_someone_elses_change(rig):
    """10 Enter, then straight on to 11 before the telemetry shows the 10: that is not a change by
    someone else, so the 11 goes too."""
    rig.box.show_setpoint(5.0)
    rig.type_in(rig.box, "10")
    rig.key(Qt.Key.Key_Return)
    rig.box.lineEdit().selectAll()
    QTest.keyClicks(rig.box, "11")
    rig.box.show_setpoint(10.0)                                    # the first one lands
    rig.key(Qt.Key.Key_Return)
    assert rig.sent == [10.0, 11.0] and rig.notices == []


def test_the_window_losing_focus_keeps_the_edit_for_later(app):
    """Another window really takes over (not a synthetic event): the edit waits; back, Enter sends it."""
    r = Rig(app)
    away = QLineEdit()
    try:
        r.box.show_setpoint(1.0)
        r.type_in(r.box, "8")
        away.show()
        away.activateWindow()
        QTest.qWaitForWindowActive(away, 2000)
        app.processEvents()
        assert r.sent == [] and r.box.editing
        r.win.activateWindow()
        QTest.qWaitForWindowActive(r.win, 2000)
        app.processEvents()
        assert QApplication.focusWidget() is r.box
        r.key(Qt.Key.Key_Return)
        assert r.sent == [8.0]
    finally:
        away.close()
        away.deleteLater()
        r.close()


def test_locked_while_the_window_was_away_drops_the_edit(app):
    r = Rig(app)
    away = QLineEdit()
    try:
        r.box.show_setpoint(2.0)
        r.type_in(r.box, "9")
        away.show()
        away.activateWindow()
        QTest.qWaitForWindowActive(away, 2000)
        r.box.setEnabled(False)
        assert not r.box.editing and r.box.value() == 2.0 and "locked" in r.notices[-1]
        r.box.setEnabled(True)
        r.win.activateWindow()
        QTest.qWaitForWindowActive(r.win, 2000)
        app.processEvents()
        assert QApplication.focusWidget() in (None, r.win)         # not back in the box (or the next one)
        r.focus(r.other)
        assert r.sent == []
    finally:
        away.close()
        away.deleteLater()
        r.close()


def test_a_switched_away_dock_tab_takes_no_keys(app):
    """Qt moves a switched-away dock tab off-screen, its focus kept: PageUp, digits and Enter went into
    a box nobody could see, and the next click sent it (review, Oct 2026)."""
    from src.gui.control.setpoint_box import SetpointBox
    mw = QMainWindow()
    mw.setCentralWidget(QLineEdit())
    docks, boxes = [], []
    for name in ("Source", "Optics"):
        dock, inner = QDockWidget(name, mw), QWidget()
        box = SetpointBox()
        box.setObjectName(f"{name} setpoint")
        box.setRange(0, 100)
        QVBoxLayout(inner).addWidget(box)
        dock.setWidget(inner)
        mw.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        docks.append(dock)
        boxes.append(box)
    mw.tabifyDockWidget(docks[0], docks[1])
    docks[0].raise_()
    sent, notices = [], []
    for box in boxes:
        box.committed.connect(sent.append)
        box.notice.connect(notices.append)
    mw.show()
    mw.activateWindow()
    QTest.qWaitForWindowActive(mw, 2000)
    try:
        boxes[0].show_setpoint(15.0)
        boxes[1].show_setpoint(31.0)
        boxes[0].setFocus(Qt.FocusReason.MouseFocusReason)
        boxes[0].lineEdit().selectAll()
        QTest.keyClicks(boxes[0], "16")                            # typed, no Enter
        assert boxes[0].editing
        docks[1].raise_()                                          # the Optics tab
        app.processEvents()
        assert QApplication.focusWidget() is boxes[0]              # (Qt keeps it there, off-screen)
        QTest.keyClick(boxes[0], Qt.Key.Key_PageUp)
        assert not boxes[0].editing and QApplication.focusWidget() is mw
        assert "switched away" in notices[-1]
        boxes[1].setFocus(Qt.FocusReason.MouseFocusReason)
        app.processEvents()
        assert sent == [] and boxes[0].value() == 15.0
    finally:
        mw.close()
        mw.deleteLater()
        app.processEvents()


def test_escape_drops_the_edit(rig):
    rig.box.show_setpoint(5.0)
    rig.type_in(rig.box, "60")
    rig.key(Qt.Key.Key_Escape)
    assert not rig.box.editing and rig.box.value() == 5.0
    rig.focus(rig.other)
    assert rig.sent == [] and rig.notices == []


def test_follows_the_live_setpoint_until_the_operator_edits(rig):
    """Focused but untouched, it shows the live value (it used to freeze while focused - at start-up
    that was its default 0, one arrow-key step from sending ~0)."""
    rig.focus(rig.box)
    rig.box.show_setpoint(7.0)
    assert rig.box.value() == 7.0
    rig.type_in(rig.box, "1")
    rig.box.show_setpoint(7.0)
    assert rig.box.lineEdit().displayText().startswith("1")       # not overwritten while typing
    rig.key(Qt.Key.Key_Return)
    assert rig.sent == [1.0]
    rig.box.show_setpoint(8.0)
    assert rig.box.value() == 8.0


def test_values_set_by_the_program_are_not_edits(rig):
    rig.box.show_setpoint(1.0)
    rig.box.setValue(4.0)
    rig.focus(rig.box)
    rig.focus(rig.other)
    assert rig.sent == [] and not rig.box.editing


def test_a_box_without_telemetry_goes_back_to_what_it_showed(app):
    """Boxes only the operator fills (the mass calibration, manual slits): Escape restores the value
    shown before the edit - and tells valueChanged listeners - and steps work from it."""
    r = Rig(app, live=False)
    try:
        box = r.box
        box.setValue(13.9)
        seen = []
        box.valueChanged.connect(seen.append)
        r.type_in(box, "50")
        r.key(Qt.Key.Key_Escape)
        r.focus(r.other)
        assert r.sent == [] and box.value() == pytest.approx(13.9)
        assert box.lineEdit().displayText().startswith("13.9") and 50.0 not in seen
        r.focus(box)
        r.key(Qt.Key.Key_Up)                                       # a step, then Escape
        r.key(Qt.Key.Key_Escape)
        assert box.value() == pytest.approx(13.9) and seen[-1] == pytest.approx(13.9) and r.sent == []
        r.key(Qt.Key.Key_Up)
        r.key(Qt.Key.Key_Return)
        assert r.sent == [pytest.approx(14.9)]
    finally:
        r.close()


# --------------------------------------------------------------------------- not sent, not taken
def test_an_entry_not_sent_yet_looks_it(rig):
    """(Josh, 27 October 2026: a filament setpoint typed after a recipe "seemed to be ignored": a number
    typed and left unsent mustn't look like the setpoint in use.)"""
    rig.box.show_setpoint(28.0)
    assert not rig.box.property("pending")
    rig.type_in(rig.box, "29")
    assert rig.box.property("pending") == "yes"
    rig.key(Qt.Key.Key_Return)
    assert not rig.box.property("pending") and rig.sent == [29.0]
    rig.type_in(rig.box, "30")
    rig.key(Qt.Key.Key_Escape)
    assert not rig.box.property("pending")
    rig.type_in(rig.box, "31")
    rig.box.setEnabled(False)                                      # locked: dropped, and says so
    assert not rig.box.property("pending")


def test_a_value_sent_but_not_taken_is_reported(rig):
    """Sent, and TAKE_S later the setpoint in use is still the one from before: the operator is told,
    with the panel's guess at why. Taken, or on its way (a ramp), or locked meanwhile: nothing said."""
    from src.gui.control import setpoint_box as sb
    box = rig.box
    box.why_not_taken = lambda: "the filament supply is off, and the PLC zeroes the setpoint of a supply that is off"
    box.show_setpoint(28.0)
    rig.type_in(box, "29")
    rig.key(Qt.Key.Key_Return)
    assert rig.sent == [29.0]
    _value, before, at = box._sent
    box.show_setpoint(28.0)                                        # not yet: still waited for
    box._check_taken(now=at + sb.TAKE_S - 0.5)
    assert rig.notices == []
    box._check_taken(now=at + sb.TAKE_S + 0.1)
    assert len(rig.notices) == 1
    assert rig.notices[0].startswith("Extraction setpoint: 29 was sent, but 28 is still the setpoint in use")
    assert "The filament supply is off" in rig.notices[0]
    box._check_taken(now=at + 10)
    assert len(rig.notices) == 1                                   # once
    # taken
    rig.type_in(box, "30")
    rig.key(Qt.Key.Key_Return)
    box.show_setpoint(30.0)
    assert box._sent is None
    # on its way: a ramp
    rig.type_in(box, "40")
    rig.key(Qt.Key.Key_Return)
    box.show_setpoint(31.0)
    assert box._sent is None
    # locked meanwhile: its tooltip says why
    box.show_setpoint(40.0)
    rig.type_in(box, "50")
    rig.key(Qt.Key.Key_Return)
    at = box._sent[2]
    box.setEnabled(False)
    box._check_taken(now=at + 10)
    assert len(rig.notices) == 1
