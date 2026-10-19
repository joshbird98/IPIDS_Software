"""Setpoint boxes that send only what the operator entered.

Qt's spin boxes report "editing finished" every time they lose focus: when focus merely passes
through, when the window loses focus, and when the box is locked (disabling a focused widget moves
the focus on). The HMI's setpoint boxes used to send their value then, and a box with focus didn't
follow the live setpoint. Together that sent values nobody typed. At start-up the box that got the
window's first focus still showed its default 0; the first telemetry locked it (safety relay off,
say), and as focus hopped down the panel each box sent its 0. Reproduced in the simulator in
October 2026: einzel, extraction, target, filament, thermionic and cesium setpoints all set to 0
by an HMI that had only just opened.

A SetpointBox sends (`committed`) only after the operator has typed in it or stepped it (arrow
keys, its buttons, Page Up/Down):
- on Enter, or when focus moves on to another control in this window;
- not when only the window loses focus (another program, a dialog): the edit waits;
- not when the box is locked while being edited: the edit is dropped;
- Escape drops the edit.
Until the operator starts editing, the box follows the live setpoint (show_setpoint), even with
focus, so nobody steps or types from a stale number.
"""
import logging

from PyQt6.QtCore import QEvent, Qt, pyqtSignal
from PyQt6.QtWidgets import QDoubleSpinBox

log = logging.getLogger("hmi.setpoint")

# Focus left together with the window (another program, a dialog, a menu): the edit waits
_WINDOW_LEFT = (Qt.FocusReason.ActiveWindowFocusReason, Qt.FocusReason.PopupFocusReason,
                Qt.FocusReason.MenuBarFocusReason)


class SetpointBox(QDoubleSpinBox):
    committed = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setKeyboardTracking(False)
        self._edited = False
        self._live = None
        self._before_edit = None
        self._focus_out_reason = None
        self.lineEdit().textEdited.connect(self._mark_edited)
        self.editingFinished.connect(self._finished)

    @property
    def editing(self) -> bool:
        """The operator has typed or stepped a value that hasn't been sent yet."""
        return self._edited

    def show_setpoint(self, value):
        """The live setpoint (telemetry). Shown unless the operator is editing; None changes nothing."""
        if value is None:
            return
        self._live = float(value)
        if not self._edited:
            self.blockSignals(True)
            try:
                self.setValue(self._live)
            finally:
                self.blockSignals(False)

    def discard_edit(self):
        """Drop the operator's unsent value: back to the live setpoint (or, with none known, to what
        the box showed before the edit)."""
        if not self._edited:
            return
        self._edited = False
        restore = self._live if self._live is not None else self._before_edit
        if restore is not None:
            self.setValue(restore)        # (also clears typed text not yet taken in)

    # ------------------------------------------------------------------ Qt
    def stepBy(self, steps):
        self._mark_edited()
        super().stepBy(steps)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self._edited:
            self.discard_edit()
            event.accept()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event):
        self._focus_out_reason = event.reason()       # read by _finished: Qt emits editingFinished in here
        try:
            super().focusOutEvent(event)
        finally:
            self._focus_out_reason = None

    def changeEvent(self, event):
        super().changeEvent(event)
        # Locked while being edited (also while the window was away, when no focus-out comes)
        if event.type() == QEvent.Type.EnabledChange and not self.isEnabled() and self._edited:
            log.info("%s locked while being edited: the unsent value is dropped",
                     self.objectName() or "Setpoint box")
            self.discard_edit()

    # ------------------------------------------------------------------ internals
    def _mark_edited(self, *_):
        if not self._edited:
            self._edited = True
            self._before_edit = self.value()    # (typed text isn't taken in until Enter / focus-out)

    def _finished(self):
        if not self._edited:
            return                                  # focus passed through, or Enter on an untouched box
        if not self.isEnabled():                    # being locked (permit lost, a sequence took over)
            self.discard_edit()
            return
        if self._focus_out_reason in _WINDOW_LEFT:
            return                                  # back in the window, Enter (or moving on) sends it
        self._edited = False
        self.committed.emit(self.value())
