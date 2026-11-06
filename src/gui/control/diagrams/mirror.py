"""A faceplate's controls are mirrors of the panels' own: a MirrorButton shows what its panel's button
shows - its text, colour, tooltip, whether it can be pressed - and pressing it presses that button;
a MirrorSetpoint is a setpoint box that follows the panel's box and sends what is entered through it.

So a diagram has no rules of its own about what can be done: the gate valve opens from its faceplate
only when the Vacuum System panel's OPEN could be pressed, switching a supply on takes the same two
clicks (arming.py), a value typed is sent and checked as from the panel (setpoint_box.py), and the
command goes the same way. sync() is called on every refresh (10 times a second) and after a press.

A mirror follows its source's hidden or shown only once it is in its card: a widget shown before it
has a parent is a window of its own, which flashes up on the screen (on Windows, with its title bar).
"""
from PyQt6.QtWidgets import QLabel, QPushButton

from src.gui.control.setpoint_box import SetpointBox
from src.gui.style import set_prop, set_role, set_tone

_COPIED = ("tone", "ink", "tolerance")


def _copy_props(source, target):
    for name in _COPIED:
        set_prop(target, name, source.property(name))


class MirrorLabel(QLabel):
    def __init__(self, source: QLabel, role: str = None):
        super().__init__()
        self.source = source
        self.setAlignment(source.alignment())
        self.setWordWrap(source.wordWrap())
        set_role(self, role if role is not None else source.property("role"))
        self.sync()

    def sync(self):
        s = self.source
        if self.text() != s.text():
            self.setText(s.text())
        _copy_props(s, self)
        if self.toolTip() != s.toolTip():
            self.setToolTip(s.toolTip())
        if self.parentWidget() is not None and self.isHidden() != s.isHidden():
            self.setHidden(s.isHidden())


class MirrorButton(QPushButton):
    def __init__(self, source: QPushButton):
        super().__init__(source.text())
        self.source = source
        self.setCheckable(source.isCheckable())
        self.clicked.connect(self._press)
        self.sync()

    def _press(self, *_):
        if self.source.isEnabled():
            self.source.click()             # the panel's own button: its rules, its confirmation
        self.sync()

    def sync(self):
        s = self.source
        if self.text() != s.text():
            self.setText(s.text())
        if self.isCheckable() and self.isChecked() != s.isChecked():
            self.blockSignals(True)
            self.setChecked(s.isChecked())
            self.blockSignals(False)
        if self.isEnabled() != s.isEnabled():
            self.setEnabled(s.isEnabled())
        set_tone(self, s.property("tone"))
        if self.styleSheet() != s.styleSheet():         # (the safety relay's countdown draws itself)
            self.setStyleSheet(s.styleSheet())
        if self.toolTip() != s.toolTip():
            self.setToolTip(s.toolTip())
        if self.parentWidget() is not None and self.isHidden() != s.isHidden():
            self.setHidden(s.isHidden())


class MirrorSetpoint(SetpointBox):
    def __init__(self, source: SetpointBox):
        super().__init__(live=source._follows_live)
        self.source = source
        self.setObjectName(source.objectName())         # (named in its notices)
        self.setDecimals(source.decimals())
        self.setRange(source.minimum(), source.maximum())
        self.setSingleStep(source.singleStep())
        self.setSuffix(source.suffix())
        self.setPrefix(source.prefix())
        self.setProperty("cap_note", source.property("cap_note"))
        self.why_not_taken = source.why_not_taken
        self.committed.connect(self._commit)
        self.sync()

    def _commit(self, value: float):
        """Sent as the panel sends it. A value that is only entered (a slit width, for the log) shows in
        the panel's box too."""
        s = self.source
        if not s._follows_live and not s.editing:
            s.blockSignals(True)
            s.setValue(value)
            s.blockSignals(False)
        s.committed.emit(value)

    def sync(self):
        s = self.source
        if (self.minimum(), self.maximum()) != (s.minimum(), s.maximum()) and not self.editing:
            self.setRange(s.minimum(), s.maximum())
        if self.isEnabled() != s.isEnabled():
            self.setEnabled(s.isEnabled())
        if self.toolTip() != s.toolTip():
            self.setToolTip(s.toolTip())
        if self._follows_live:
            self.show_setpoint(s.live)
        elif not self.editing and self.value() != s.value():
            self.blockSignals(True)
            self.setValue(s.value())
            self.blockSignals(False)
