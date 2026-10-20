"""Arm-then-confirm for buttons that energise something (HV supplies, filament, magnet...).

Turning an output ON takes two clicks: the first arms the button (it turns
amber and reads 'CONFIRM ...?') and a second click within ARM_WINDOW_S sends
the command; otherwise it quietly disarms. Turning an output OFF is always a
single click - making the machine safer should never take longer.

The widgets refresh their buttons from telemetry 10 times a second; they call
ArmedToggle.show_state() instead of setChecked or a tone directly, so the
armed appearance isn't painted over and an unknown state (service down, supply
not answering) is shown as unknown rather than as the last known one.

The looks are tones (src/gui/style.py) drawn by the theme: "armed" (amber with
a heavy border), "unknown" (dashed outline), "on" (green: the output is ON).
"""
from PyQt6.QtCore import QObject, QTimer

from src.gui.style import set_tone, tone

ARM_WINDOW_S = 4.0
ARMED_TONE = "armed"
UNKNOWN_TONE = "unknown"


class ArmedToggle(QObject):
    def __init__(self, button, send, what: str = "OUTPUT", window_s: float = ARM_WINDOW_S, parent=None):
        """button: a checkable QPushButton. send(bool) dispatches the enable/disable command."""
        super().__init__(parent or button)
        self.button = button
        self.send = send
        self.what = what
        self.window_s = window_s
        self.armed = False
        self._text = button.text()
        self._tone = tone(button)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.disarm)
        button.clicked.connect(self._on_click)

    def _on_click(self, *_):
        wants_on = self.button.isChecked()   # Qt has already toggled the check state
        if not wants_on:
            self.disarm()
            self.send(False)
            return
        if not self.armed:
            # First click: don't switch anything yet, ask for confirmation
            self.button.blockSignals(True)
            self.button.setChecked(False)
            self.button.blockSignals(False)
            self.armed = True
            self._tone = tone(self.button)
            self.button.setText(f"CONFIRM {self.what}?")
            set_tone(self.button, ARMED_TONE)
            self.button.setToolTip(f"Click again within {self.window_s:.0f} s to switch {self.what.lower()} ON.")
            self._timer.start(int(self.window_s * 1000))
            return
        # Second click inside the window: go
        self.disarm()
        self.send(True)

    def disarm(self):
        if self.armed:
            self.armed = False
            self._timer.stop()
            self.button.setText(self._text)
            set_tone(self.button, self._tone)

    def set_idle_text(self, text: str):
        """Change the label shown when not armed (e.g. 'DISARMING...' handled by the caller)."""
        self._text = text
        if not self.armed:
            self.button.setText(text)

    def show_state(self, enabled, unknown_tip: str = None, on_tone: str = "on", off_tone: str = None):
        """Reflect the device state (True / False / None = unknown) without disturbing an armed button.
        Restyles only when the state changes (set_tone does nothing for the tone already shown)."""
        if self.armed:
            if not self.button.isEnabled():
                self.disarm()
            return
        if self.button.isChecked() != bool(enabled):
            self.button.blockSignals(True)
            self.button.setChecked(bool(enabled))
            self.button.blockSignals(False)
        if self.button.text() != self._text:
            self.button.setText(self._text)
        if enabled is None:
            set_tone(self.button, UNKNOWN_TONE)
            if unknown_tip:
                self.button.setToolTip(unknown_tip)
        else:
            set_tone(self.button, on_tone if enabled else off_tone)


class ArmedAction(QObject):
    """Two-click confirmation for a one-shot (non-checkable) button, e.g. DEGAUSS."""

    def __init__(self, button, fire, what: str = "ACTION", window_s: float = ARM_WINDOW_S, parent=None):
        super().__init__(parent or button)
        self.button = button
        self.fire = fire
        self.what = what
        self.window_s = window_s
        self.armed = False
        self._text = button.text()
        self._tone = tone(button)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.disarm)
        button.clicked.connect(self._on_click)

    def _on_click(self, *_):
        if not self.armed:
            self.armed = True
            self._text, self._tone = self.button.text(), tone(self.button)
            self.button.setText(f"CONFIRM {self.what}?")
            set_tone(self.button, ARMED_TONE)
            self._timer.start(int(self.window_s * 1000))
            return
        self.disarm()
        self.fire()

    def disarm(self):
        if self.armed:
            self.armed = False
            self._timer.stop()
            self.button.setText(self._text)
            set_tone(self.button, self._tone)
