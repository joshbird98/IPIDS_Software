"""The mouse wheel never changes a value box or a drop-down in the operator HMI.

Qt's spin boxes and combo boxes change their value under a passing mouse wheel and take focus
while doing it. The HMI's setpoint boxes send a value the operator stepped as well as one typed
(src/gui/control/setpoint_box.py) - on Enter or when focus moves on - so scrolling down a panel
over, say, the extraction voltage changed it by a step, and the next click anywhere else sent that
new setpoint to the supply.

With the guard installed, wheel events over any QAbstractSpinBox or QComboBox are dropped:
values are typed (or stepped with the arrow keys / buttons). An open drop-down list still
scrolls, since its list is a separate widget.

A wheel doesn't give them the focus either: their focus policy is turned from WheelFocus into
StrongFocus when they are first shown. Qt hands out wheel focus before any event filter sees the
wheel, so a wheel passing over one box took the focus from another - finishing (and sending) a
half-typed value there.

As it sees every event anyway, the guard also remembers where on the screen the mouse was last
pressed (last_press_at): the HMI's focus keeping (control_main.py) leaves the keyboard on a control
the operator has just clicked, whatever Qt did with the focus before. (A point, not the widget: a
widget Qt has since deleted can't be asked anything - in the tests, that crashed.)
"""
import time

from PyQt6.QtCore import QEvent, QObject, Qt
from PyQt6.QtWidgets import QAbstractSpinBox, QComboBox, QWidget

_installed = {}


class WheelGuard(QObject):
    def __init__(self):
        super().__init__()
        self.last_press = (0.0, None)               # (monotonic time, where on the screen)

    def eventFilter(self, obj, event):
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress and isinstance(obj, QWidget):
            self.last_press = (time.monotonic(), event.globalPosition().toPoint())
            return False
        if kind == QEvent.Type.Wheel and isinstance(obj, (QAbstractSpinBox, QComboBox)):
            event.ignore()
            return True
        if kind == QEvent.Type.Polish and isinstance(obj, (QAbstractSpinBox, QComboBox)) \
                and obj.focusPolicy() == Qt.FocusPolicy.WheelFocus:
            obj.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        return False


def install(app) -> WheelGuard:
    """Install once per QApplication (idempotent)."""
    guard = _installed.get(id(app))
    if guard is None:
        guard = WheelGuard()
        app.installEventFilter(guard)
        _installed[id(app)] = guard
    return guard


def last_press_at(app, within_s: float = 1.0):
    """Where on the screen the mouse was last pressed (a QPoint), if that was within the last within_s
    seconds; else None (and None without a guard installed)."""
    guard = _installed.get(id(app))
    if guard is None:
        return None
    at, where = guard.last_press
    return where if where is not None and time.monotonic() - at <= within_s else None
