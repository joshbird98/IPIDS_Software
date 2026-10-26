"""Small pieces of the data viewer: the add-marker dialog and the plot axes."""
import math
from datetime import datetime

import pyqtgraph as pg
from PyQt6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLineEdit
from PyQt6.QtCore import QSize

from src.gui.style import swatch


class MarkerDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add event marker")
        self.setMinimumWidth(300)
        layout = QFormLayout(self)

        self.text_input = QLineEdit()
        self.color_input = QComboBox()

        # System events have the theme's colours (faults red, cleared blue, watchdog orange, phase
        # green: theme.py ev_*). The marker keeps the colour chosen here; the plot draws it darker
        # or lighter if needed to stand out on its background.
        self.colors = {
            "Cyan": "#00E5FF",
            "Yellow": "#FFEA00",
            "Magenta": "#FF4081",
            "White": "#FFFFFF",
            "Purple": "#9C27B0",
            "Pink": "#E91E63"
        }

        for name, hex_code in self.colors.items():
            self.color_input.addItem(swatch(hex_code), name)
        self.color_input.setIconSize(QSize(14, 14))

        layout.addRow("Note:", self.text_input)
        layout.addRow("Colour:", self.color_input)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def get_data(self):
        color_name = self.color_input.currentText()
        return self.text_input.text(), self.colors[color_name]

# --- Custom Axis Items ---

class LogAxisItem(pg.AxisItem):
    def tickStrings(self, values, scale, spacing):
        strings = []
        for v in values:
            try:
                real_val = 10 ** float(v)
                strings.append(f"{real_val:.1e}")
            except (OverflowError, ValueError):  # tick far outside the representable range
                strings.append("")
        return strings


class TimeAxisItem(pg.AxisItem):
    """Clock time along the bottom: x is seconds from `offset` (the viewer's start, unix time)."""
    # (major, minor) tick steps in seconds: whole clock times
    CLOCK_STEPS = ((0.01, 0.002), (0.02, 0.005), (0.05, 0.01), (0.1, 0.02), (0.2, 0.05), (0.5, 0.1),
                   (1, 0.2), (2, 0.5), (5, 1), (10, 2), (15, 5), (30, 5), (60, 10), (120, 30), (300, 60),
                   (600, 120), (900, 300), (1800, 300), (3600, 600), (7200, 1800), (10800, 3600),
                   (21600, 3600), (43200, 7200), (86400, 21600), (172800, 43200), (604800, 86400))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.offset = 0.0
        self.hide_text = False  # NEW: Flag to control text rendering

    def set_offset(self, offset):
        self.offset = offset

    def tickSpacing(self, minVal, maxVal, size):
        """Ticks on whole clock times (whole minutes, quarter hours, midnight...), about as many as
        pyqtgraph would draw. Its own are decimal seconds from the viewer's start - 50 s or 100 s
        apart, at no particular second - which the minute labels made 08:48, 08:50, 08:51, 08:53."""
        levels = super().tickSpacing(minVal, maxVal, size)
        if not levels or self._tickSpacing is not None:
            return levels
        want = levels[0][0]
        major, minor = min(self.CLOCK_STEPS, key=lambda s: abs(math.log(s[0] / want)))
        try:   # local clock time: hours and days start on the local hour and midnight
            utc = datetime.fromtimestamp(self.offset + (minVal + maxVal) / 2).astimezone().utcoffset()
            shift = self.offset + (utc.total_seconds() if utc else 0.0)
        except (OverflowError, OSError, ValueError):
            shift = self.offset
        out = [(major, -shift % major)]
        if self.style['maxTickLevel'] >= 1:
            out.append((minor, -shift % minor))
        return out

    def tickStrings(self, values, scale, spacing):
        # NEW: If this is an upper lane, return blank strings.
        # The grid still draws, but the text is invisible.
        if self.hide_text:
            return [""] * len(values)

        strings = []
        for v in values:
            try:
                dt = datetime.fromtimestamp(v + self.offset)
                if spacing >= 86400:
                    text = dt.strftime('%Y-%m-%d')
                elif spacing >= 3600:
                    text = dt.strftime('%b %d\n%H:%M')
                elif spacing >= 60:
                    text = dt.strftime('%H:%M')
                elif spacing >= 1:
                    text = dt.strftime('%H:%M:%S')
                else:   # tenths, or hundredths of a second
                    text = dt.strftime('%H:%M:%S.%f')[:-5 if spacing >= 0.1 else -4]
                strings.append(text)
            except (OverflowError, OSError, ValueError):  # timestamp outside the platform's range
                strings.append("")
        return strings

# --- Main Application ---
