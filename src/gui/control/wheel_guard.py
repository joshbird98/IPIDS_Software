"""The mouse wheel never changes a value box or a drop-down in the operator HMI.

Qt's spin boxes and combo boxes change their value under a passing mouse wheel and take focus
while doing it. The HMI's setpoint boxes send their value when editing finishes - on Enter or
when focus leaves the box - so scrolling down a panel over, say, the extraction voltage changed
it by a step, and the next click anywhere else sent that new setpoint to the supply.

With the guard installed, wheel events over any QAbstractSpinBox or QComboBox are dropped:
values are typed (or stepped with the arrow keys / buttons). An open drop-down list still
scrolls, since its list is a separate widget.
"""
from PyQt6.QtCore import QEvent, QObject
from PyQt6.QtWidgets import QAbstractSpinBox, QComboBox

_installed = {}


class WheelGuard(QObject):
    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Wheel and isinstance(obj, (QAbstractSpinBox, QComboBox)):
            event.ignore()
            return True
        return False


def install(app) -> WheelGuard:
    """Install once per QApplication (idempotent)."""
    guard = _installed.get(id(app))
    if guard is None:
        guard = WheelGuard()
        app.installEventFilter(guard)
        _installed[id(app)] = guard
    return guard
