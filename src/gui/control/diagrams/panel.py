"""What the four diagram panels have in common: the drawing (canvas.py) filling the panel, refreshed
with the telemetry, and the faceplate of the part last clicked (faceplate.py).

A diagram is drawn from the panels it pictures (it is given them: `panels`), so it shows what they
show and its controls are theirs. The HMI refreshes those panels while a diagram of them is in view,
even when their own docks are hidden (control_main.ControlMainWindow._ui_update_loop).
"""
import logging

from PyQt6.QtCore import QPointF, QSize, QSizeF, pyqtSignal
from PyQt6.QtWidgets import QVBoxLayout, QWidget

from src.gui import theme
from src.gui.control.diagrams.canvas import DiagramCanvas
from src.gui.control.diagrams.mirror import MirrorSetpoint

log = logging.getLogger("hmi.diagrams")


class DiagramPanel(QWidget):
    notice = pyqtSignal(str)        # a faceplate's setpoint entry not sent, and why (the HMI shows it)
    ORIGIN = QPointF(0, 0)          # the drawing's top left corner, in its own units
    SIZE = QSizeF(1000, 700)        # ... and its size
    DESIGN_SCALE = 0.55             # screen px per unit the labels were placed for
    MINIMUM = QSize(640, 460)

    def __init__(self, panels: dict, show_panel=None):
        """panels: the HMI's panels this diagram pictures, by name. show_panel(name): bring that
        panel's dock to the front (the faceplates' link to it)."""
        super().__init__()
        self.panels = panels
        self.show_panel = show_panel or (lambda name: None)
        self.data = {}
        self.drawing_size = self.SIZE
        self.drawing_origin = self.ORIGIN
        self.faceplate = None
        self.canvas = DiagramCanvas(self, self.DESIGN_SCALE, self.MINIMUM, self)
        self.canvas.clicked.connect(self.open_faceplate)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.canvas)
        theme.theme_changed().connect(self._restyle)

    # --- for subclasses -------------------------------------------------------------------------
    def parts(self) -> list:
        return []

    def paint(self, view):
        pass

    def read(self, data: dict):
        """Work out what to draw from the telemetry (and the panels, refreshed just before)."""

    def faceplate_for(self, key: str):
        return None

    @property
    def watched(self) -> list:
        """The panels to keep refreshed while this diagram is in view."""
        return list(self.panels.values())

    # --- the HMI ------------------------------------------------------------------------------
    def update_telemetry(self, data: dict):
        self.data = data
        try:
            self.read(data)
        except Exception:       # one bad value must not freeze the panel
            log.exception("%s: reading the telemetry failed", type(self).__name__)
        self.canvas.update()
        if self.faceplate is not None:
            try:
                self.faceplate.sync()
            except RuntimeError:            # (deleted under us)
                self.faceplate = None

    def _restyle(self, *_):
        try:
            self.canvas.update()
        except RuntimeError:
            pass

    # --- faceplates ---------------------------------------------------------------------------
    def open_faceplate(self, key: str):
        old = self.faceplate
        if old is not None:
            self.faceplate = None
            old.closed.disconnect(self._faceplate_closed)
            old.close_plate()
        self.canvas.selected = ""
        if key:
            plate = self.faceplate_for(key)
            if plate is not None:
                for box in plate.findChildren(MirrorSetpoint):
                    box.notice.connect(self.notice.emit)
                plate.closed.connect(self._faceplate_closed)
                self.faceplate = plate
                self.canvas.selected = key
                plate.sync()
                plate.show()
                self._place_faceplate()
        self.canvas.update()

    def _faceplate_closed(self):
        self.faceplate = None
        self.canvas.selected = ""
        self.canvas.update()

    def _place_faceplate(self):
        plate = self.faceplate
        if plate is None:
            return
        box = self.canvas.box_on_screen(plate.key)
        anchor = box.translated(self.canvas.pos().x(), self.canvas.pos().y())
        seen = self.visibleRegion().boundingRect()      # (the panel scrolls when its dock is small)
        plate.place(anchor, seen if not seen.isEmpty() else self.rect())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_faceplate()
