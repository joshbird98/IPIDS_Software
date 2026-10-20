"""Small pieces of the data viewer: the add-marker dialog and the plot axes."""
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
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.offset = 0.0
        self.hide_text = False  # NEW: Flag to control text rendering

    def set_offset(self, offset):
        self.offset = offset

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
                    fmt = '%Y-%m-%d'
                elif spacing >= 3600:
                    fmt = '%b %d\n%H:%M'
                elif spacing >= 60:
                    fmt = '%H:%M'
                elif spacing >= 1:
                    fmt = '%H:%M:%S'
                else:
                    fmt = '%H:%M:%S.%f'
                strings.append(dt.strftime(fmt))
            except (OverflowError, OSError, ValueError):  # timestamp outside the platform's range
                strings.append("")
        return strings

# --- Main Application ---
