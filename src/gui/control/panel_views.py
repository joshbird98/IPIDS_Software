"""A panel shown two ways: as its list of readouts and controls, or as a diagram of that part of the
machine (a mimic diagram: src/gui/control/diagrams), with a List | Diagram switch at its top right.

One dock, and one entry in View > Panels, for both. The HMI remembers each panel's view (its
settings, views/<dock name>) and saves it with a named layout (View > Layouts). The view switched
away keeps its state, as a tab behind another does: an open faceplate, a scroll position.
"""
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QButtonGroup, QHBoxLayout, QPushButton, QStackedWidget, QVBoxLayout, QWidget

from src.gui.style import set_role

LIST, DIAGRAM = "list", "diagram"
VIEWS = (LIST, DIAGRAM)
DEFAULT_VIEW = LIST

_BUTTONS = ((LIST, "List", "left", "Every reading and control of this panel, listed"),
            (DIAGRAM, "Diagram", "right", "This part of the machine drawn as it is laid out, live: "
                                          "click a part for its readings and controls"))


class PanelViews(QWidget):
    """The list and the diagram of one panel, each in its own scroll area (`list_page`,
    `diagram_page`), one at a time, under the switch."""
    switched = pyqtSignal(str)          # the view now shown (LIST or DIAGRAM)

    def __init__(self, list_page: QWidget, diagram_page: QWidget, parent=None):
        super().__init__(parent)
        self.setObjectName("PanelViews")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._pages = {LIST: list_page, DIAGRAM: diagram_page}

        bar = QWidget()
        bar.setObjectName("PanelViewBar")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row = QHBoxLayout(bar)
        row.setContentsMargins(8, 4, 8, 2)
        row.setSpacing(0)
        row.addStretch(1)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self.buttons = {}
        for view, text, side, tip in _BUTTONS:
            button = QPushButton(text)
            button.setCheckable(True)
            # A click on the switch must leave the keyboard where it is: a value being typed in a
            # setpoint box counts as entered when its box loses the focus (control_main._panel)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setAutoDefault(False)
            button.setToolTip(tip)
            set_role(button, "segment")
            button.setProperty("side", side)
            button.clicked.connect(lambda _checked=False, v=view: self.show_view(v))
            self._group.addButton(button)
            row.addWidget(button)
            self.buttons[view] = button
        self.bar = bar

        self.stack = QStackedWidget()
        for view in VIEWS:
            self.stack.addWidget(self._pages[view])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(bar)
        layout.addWidget(self.stack, 1)
        self.stack.setCurrentWidget(self._pages[DEFAULT_VIEW])
        self.buttons[DEFAULT_VIEW].setChecked(True)

    def view(self) -> str:
        """The view shown: LIST or DIAGRAM."""
        return DIAGRAM if self.stack.currentWidget() is self._pages[DIAGRAM] else LIST

    def page(self, view: str = None) -> QWidget:
        """The scroll area of `view` (of the view shown, if None)."""
        return self._pages[view or self.view()]

    def show_view(self, view: str) -> bool:
        """Show `view`. False, and nothing changes, if it is not one of VIEWS or is shown already."""
        if view not in self._pages:
            return False
        self.buttons[view].setChecked(True)
        if view == self.view():
            return False
        self.stack.setCurrentWidget(self._pages[view])
        self.switched.emit(view)
        return True
