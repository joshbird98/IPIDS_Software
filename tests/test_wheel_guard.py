"""The mouse wheel never changes a value box or a drop-down in the HMI (src/gui/control/wheel_guard.py):
the setpoint boxes send their value when focus leaves them, so a scroll over one used to change a
setpoint that the next click elsewhere sent to the supply."""
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QWheelEvent


def wheel(widget, notches=1):
    ev = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, 120 * notches),
                     Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    from PyQt6.QtWidgets import QApplication
    QApplication.sendEvent(widget, ev)


def test_wheel_does_not_change_boxes_or_dropdowns():
    from PyQt6.QtWidgets import QApplication, QComboBox, QDoubleSpinBox, QSpinBox
    from src.gui.control import wheel_guard
    app = QApplication.instance() or QApplication([])
    box, ibox, combo = QDoubleSpinBox(), QSpinBox(), QComboBox()
    box.setRange(0, 20)
    box.setValue(10.0)
    ibox.setValue(5)
    combo.addItems(["1 nA", "10 nA", "100 nA"])
    assert wheel_guard.install(app) is wheel_guard.install(app)          # once per application
    for w in (box, ibox, combo):
        wheel(w, 3)
    assert box.value() == 10.0 and ibox.value() == 5 and combo.currentIndex() == 0
    assert not box.hasFocus()                                            # nor does it take focus
