"""Setpoint boxes that send only what the operator entered.

Qt's spin boxes report "editing finished" every time they lose focus: when focus merely passes
through, when the window loses focus, and when the box is locked (disabling a focused widget moves
the focus on). The HMI's setpoint boxes used to send their value then, and a box with focus didn't
follow the live setpoint. Together that sent values nobody typed. At start-up the box that got the
window's first focus still showed its default 0; the first telemetry locked it (safety relay off,
say), and as focus hopped down the panel each box sent its 0. Reproduced in the simulator in
October 2026: einzel, extraction, target, filament, thermionic and cesium setpoints all set to 0
by an HMI that had only just opened.

A SetpointBox sends (`committed`) only a value the operator typed or stepped (arrow keys, its
buttons, Page Up/Down), on Enter or when the focus moves on to another control in the window.
It sends nothing, and tells the operator why (`notice`), when:
- the box is locked while being edited - and the keyboard goes to the window, not to the next
  control (where the operator's next keys would have set another supply);
- its panel is closed or switched away first (a switched-away dock tab keeps its widgets, and
  their focus, off-screen: keys typed then are not taken either);
- the text isn't a value (empty, a lone '-'): Qt would quietly fall back to the old value;
- the live setpoint changed while the operator was typing (someone else set it).
When only the window loses focus (another program, a dialog), the edit waits. Escape drops it.
A passing mouse wheel doesn't take the focus (StrongFocus; see also wheel_guard.py).

Until the operator starts editing, the box follows the live setpoint (show_setpoint), focused or
not. While that isn't known - no telemetry yet, the service silent, or a value outside the box's
range, which Qt would clamp - the box is empty and can't be stepped: a step from a number that
isn't the real setpoint would send a wrong one. Typing a whole value still works.

An entry not yet sent looks it (the "pending" property: a dashed amber outline), so a number typed
and left - the window left before Enter, say - can't be taken for the setpoint in use. And a value
sent is checked: if TAKE_S later the setpoint in use is still the one from before, not moving
towards it, the operator is told it wasn't taken, with the panel's guess at why (why_not_taken) -
the service may have accepted it and the device not applied it (the PLC zeroes the setpoint of a
supply that is off; auto-emission sets the filament's). A setpoint that moves - a ramp - is left to
get there.
"""
import logging
import math
import time

from PyQt6.QtCore import QEvent, Qt, pyqtSignal
from PyQt6.QtGui import QValidator
from PyQt6.QtWidgets import QAbstractSpinBox, QDoubleSpinBox

log = logging.getLogger("hmi.setpoint")

# Focus left together with the window (another program, a dialog, a menu): the edit waits
_WINDOW_LEFT = (Qt.FocusReason.ActiveWindowFocusReason, Qt.FocusReason.PopupFocusReason,
                Qt.FocusReason.MenuBarFocusReason)
_ENTER = (Qt.Key.Key_Return, Qt.Key.Key_Enter)
_NO_STEPS = QAbstractSpinBox.StepEnabledFlag.StepNone
LOCKED = "the box was locked while you were typing (its tooltip says why)"
TAKE_S = 3.0            # a value sent and not in use this long after, nor on its way: not taken


def _number(value):
    """A finite number, else None (unknown)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


class SetpointBox(QDoubleSpinBox):
    committed = pyqtSignal(float)
    notice = pyqtSignal(str)        # an operator's entry was not sent, and why (the HMI shows it)

    def __init__(self, parent=None, live: bool = True):
        """live: the box shows a setpoint from telemetry (show_setpoint). False for values only the
        operator enters (manual slit widths, the mass calibration)."""
        super().__init__(parent)
        self.setKeyboardTracking(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)     # not WheelFocus: see the module docstring
        self._follows_live = live
        self._live = None             # live setpoint; None: not known
        self._live_at_edit = None     # ... when the operator started editing
        self._before_edit = None      # the value shown then
        self._last_sent = None        # the operator's last value sent from here
        self._edited = False
        self._finishing = None        # (focus-out reason or None for Enter, typed, text ok, on screen, text)
        self._sent = None             # (value, the live setpoint before, when) until it is in use
        self.why_not_taken = None     # () -> str: the panel's guess at why a value sent wasn't taken
        self.lineEdit().textEdited.connect(self._mark_edited)
        self.editingFinished.connect(self._finished)

    @property
    def editing(self) -> bool:
        """The operator has typed or stepped a value that hasn't been sent yet."""
        return self._edited

    def show_setpoint(self, value):
        """The live setpoint from telemetry (None, or anything not a number: not known)."""
        self._live = _number(value)
        self._check_taken()
        if not self._edited:
            self._show_live()

    def drop_edit(self, why: str):
        """Drop the operator's unsent value and tell them why (notice)."""
        if self._edited:
            self._drop(why)

    def discard_edit(self):
        """Drop the operator's unsent value: back to the live setpoint (for a box without one, to
        what it showed before the edit)."""
        if not self._edited:
            return
        self._edited = False
        self._show_pending(False)
        if self._follows_live:
            self._show_live()
        elif self._before_edit is not None:
            self.setValue(self._before_edit)        # (also clears typed text not yet taken in)

    def setEnabled(self, enabled: bool):
        if not enabled and self.isEnabled():
            # Locked: an unsent value is dropped, and if the box holds the keyboard it goes to the
            # window - Qt would pass it to the next control, its text selected, and the operator's
            # next keys would set another supply
            if self._edited:
                self._drop(LOCKED)
            if self.window().focusWidget() is self:
                self.window().setFocus(Qt.FocusReason.OtherFocusReason)
        super().setEnabled(enabled)

    def setDisabled(self, disabled: bool):
        self.setEnabled(not disabled)

    # ------------------------------------------------------------------ Qt
    def stepEnabled(self):
        if not self.isEnabled() or not self._on_screen():
            return _NO_STEPS
        if self._follows_live and not self._edited and not self._live_shown():
            return _NO_STEPS                        # nothing known to step from
        return super().stepEnabled()

    def stepBy(self, steps):
        # (also reached from the context menu's Step up/down after the menu closes - the box may
        # have been locked meanwhile)
        flags = self.stepEnabled()
        wanted = (QAbstractSpinBox.StepEnabledFlag.StepUpEnabled if steps > 0
                  else QAbstractSpinBox.StepEnabledFlag.StepDownEnabled)
        if not flags & wanted:
            return
        self._mark_edited()
        super().stepBy(steps)

    def keyPressEvent(self, event):
        if not self._on_screen():
            # Its panel was switched away: keys typed now are meant for something on screen
            if self._edited:
                self._drop("its panel was switched away before you pressed Enter")
            self.window().setFocus(Qt.FocusReason.OtherFocusReason)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape and self._edited:
            self.discard_edit()
            event.accept()
            return
        if event.key() in _ENTER and self._edited and not self._text_ok():
            self.notice.emit(f"{self._name()}: '{self._typed()}' is not a value - nothing sent. Type a "
                             "value and press Enter, or Escape to go back to the setpoint.")
            event.accept()
            return
        self._finishing = (None, self._typed(), True, True, self.lineEdit().displayText())
        try:
            super().keyPressEvent(event)
        finally:
            self._finishing = None

    def focusOutEvent(self, event):
        # Read by _finished: Qt emits editingFinished in here, after falling back to the old value
        # for text that isn't one
        self._finishing = (event.reason(), self._typed(), self._text_ok(), self._on_screen(),
                           self.lineEdit().displayText())
        try:
            super().focusOutEvent(event)
        finally:
            self._finishing = None

    def changeEvent(self, event):
        super().changeEvent(event)
        # Locked other than through setEnabled (a parent locked), or while the window was away
        if event.type() == QEvent.Type.EnabledChange and not self.isEnabled() and self._edited:
            self._drop(LOCKED)

    def showEvent(self, event):
        super().showEvent(event)
        if self._follows_live and not self._edited and not self._live_shown():
            self._show_unknown()

    # ------------------------------------------------------------------ internals
    def _name(self) -> str:
        return self.objectName() or "Setpoint"

    def _typed(self) -> str:
        text = self.lineEdit().displayText()
        if self.suffix() and text.endswith(self.suffix()):
            text = text[:-len(self.suffix())]
        return text[len(self.prefix()):].strip() if text.startswith(self.prefix()) else text.strip()

    def _text_ok(self) -> bool:
        text = self.lineEdit().displayText()
        return self.validate(text, len(text))[0] == QValidator.State.Acceptable

    def _on_screen(self) -> bool:
        return self.isVisible() and not self.visibleRegion().isEmpty()

    def _live_shown(self) -> bool:
        return self._live is not None and self.minimum() <= self._live <= self.maximum()

    def _show_live(self):
        if self._live_shown():
            self.blockSignals(True)
            try:
                self.setValue(self._live)
            finally:
                self.blockSignals(False)
        elif self._follows_live:
            self._show_unknown()

    def _show_unknown(self):
        if self.lineEdit().displayText() != self.prefix() + self.suffix():
            self.blockSignals(True)
            try:
                self.clear()
            finally:
                self.blockSignals(False)

    def _mark_edited(self, *_):
        if not self._edited and self.isEnabled():
            self._edited = True
            self._before_edit = self.value()        # (typed text isn't taken in until Enter / focus-out)
            self._live_at_edit = self._live
            self._show_pending(True)

    def _show_pending(self, pending: bool):
        """An entry not sent yet looks it (the theme draws a dashed amber outline)."""
        value = "yes" if pending else ""
        if (self.property("pending") or "") != value:
            self.setProperty("pending", value)
            self.style().unpolish(self)
            self.style().polish(self)
            self.update()

    def _tol(self, value: float) -> float:
        """Values closer than this are the same setpoint, as the box shows it."""
        return max(abs(value) * 1e-6, 0.5 * 10 ** -self.decimals())

    def _live_moved(self) -> bool:
        """The live setpoint changed while the operator was editing (it was known when they began) -
        to something other than their own previous entry, arriving after they had started again."""
        was, now = self._live_at_edit, self._live
        if was is None or now is None:
            return False
        tol = self._tol(was)
        if abs(now - was) <= tol:
            return False
        return self._last_sent is None or abs(now - self._last_sent) > tol

    def _check_taken(self, now: float = None):
        """The value last sent: in use, on its way (the setpoint moving) or still waited for; or, TAKE_S
        after it was sent with the setpoint in use still the one from before, not taken - the operator
        is told, with the panel's guess at why. Not judged when the setpoint before wasn't known, or the
        box has been locked meanwhile (its tooltip says why)."""
        if self._sent is None:
            return
        value, before, at = self._sent
        live = self._live
        if live is None or before is None:
            if before is None:
                self._sent = None
            return
        if abs(live - value) <= self._tol(value) or abs(live - before) > self._tol(before):
            self._sent = None                       # in use; or moving - a ramp, or someone else's
            return
        now = time.monotonic() if now is None else now
        if now - at < TAKE_S:
            return
        self._sent = None
        if not self.isEnabled():
            return
        why = ""
        if callable(self.why_not_taken):
            try:
                why = self.why_not_taken() or ""
            except Exception:                       # (a guess at a reason must never break the box)
                log.exception("why_not_taken failed")
        message = (f"{self._name()}: {value:g} was sent, but {live:g} is still the setpoint in use "
                   f"{TAKE_S:g} s later - it wasn't taken. "
                   + (why[:1].upper() + why[1:] if why else "Check the Command Log and the panel's badges")
                   + ".")
        log.warning(message)
        self.notice.emit(message)

    def _drop(self, why: str, typed: str = None):
        typed = self._typed() if typed is None else typed
        self.discard_edit()
        message = f"{self._name()}: {typed or 'your entry'} not sent - {why}."
        log.info(message)
        self.notice.emit(message)

    def _finished(self):
        if not self._edited:
            return                                  # focus passed through, or Enter on an untouched box
        reason, typed, text_ok, on_screen, text = self._finishing or (
            None, self._typed(), self._text_ok(), True, self.lineEdit().displayText())
        if reason in _WINDOW_LEFT:
            return                                  # back in the window, Enter (or moving on) sends it
        if not self.isEnabled():
            self._drop(LOCKED, typed)
        elif not on_screen:
            self._drop("its panel was closed or switched away before you pressed Enter", typed)
        elif not text_ok:
            self._drop(f"'{typed}' is not a value", typed)
        elif self._follows_live and self._live_moved():
            self._drop(f"the setpoint changed to {self._live:g} while you were typing - enter it again "
                       "if you still want it", typed)
        else:
            # The value sent is the one typed, read from the text: Qt doesn't always take it in (a
            # box emptied while its setpoint was unknown ignores text that didn't come through its
            # own key handling - pasted from the context menu, say - and keeps its old number)
            value = self.valueFromText(text)
            self._edited = False
            self._show_pending(False)
            self._last_sent = value
            if value != self.value():
                self.setValue(value)
            self._sent = (value, self._live, time.monotonic()) if self._follows_live else None
            self.committed.emit(value)
