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
"""
from PyQt6.QtCore import QEvent, QObject, Qt
from PyQt6.QtWidgets import QAbstractSpinBox, QComboBox

_installed = {}


class WheelGuard(QObject):
    def eventFilter(self, obj, event):
        kind = event.type()
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
