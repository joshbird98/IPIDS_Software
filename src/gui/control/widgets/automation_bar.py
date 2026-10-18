"""The Automation bar (top of the HMI): which sequence holds which subsystems, and a Take control
button for each. Taking control frees the subsystems at once; the sequence then pauses (a
recipe - Resume claims them again) or stops (a scan, the optimiser)."""
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QToolBar, QWidget

from src.gui.control.arming import ArmedAction
from src.gui.control.permits import leases, take_control

IDLE_TEXT = "Automation: nothing holds any controls"
HELD_STYLE = "QLabel { color: #CE93D8; font-weight: bold; padding: 0 6px; }"
TAKE_STYLE = "background-color: #7B1FA2; color: white; font-weight: bold; border-radius: 4px; padding: 3px 8px;"


def describe(subsystems) -> str:
    return ", ".join(sorted(s.split(".")[-1].replace("_", " ") for s in subsystems))


class AutomationBar(QToolBar):
    def __init__(self, cmd_thread, parent=None):
        super().__init__("Automation", parent)
        self.setObjectName("AutomationBar")
        self.setMovable(False)
        self.cmd_thread = cmd_thread
        self.lbl_idle = QLabel(IDLE_TEXT)
        self.lbl_idle.setStyleSheet("QLabel { color: #9E9E9E; padding: 0 6px; }")
        self.idle_action = self.addWidget(self.lbl_idle)     # a toolbar shows/hides the action, not the widget
        self.groups = {}                  # purpose -> [subsystems]
        self.rows = {}                    # purpose -> (action, label, button, arm)
        self._shown = None

    def update_telemetry(self, data: dict):
        groups = {}
        for sub, purpose in leases(data).items():
            groups.setdefault(purpose, []).append(sub)
        key = tuple(sorted((p, tuple(sorted(s))) for p, s in groups.items()))
        if key == self._shown:
            return
        self._shown = key
        self.groups = groups
        for purpose in list(self.rows):
            if purpose not in groups:
                self.removeAction(self.rows.pop(purpose)[0])
        for purpose, subs in groups.items():
            if purpose not in self.rows:
                self._add_row(purpose)
            self.rows[purpose][1].setText(f"🤖 {purpose} holds: {describe(subs)}")
        self.idle_action.setVisible(not groups)

    def _add_row(self, purpose):
        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(0, 0, 8, 0)
        label = QLabel()
        label.setStyleSheet(HELD_STYLE)
        button = QPushButton("TAKE CONTROL")
        button.setStyleSheet(TAKE_STYLE)
        button.setToolTip(f"Take these controls back from {purpose} (click twice). It pauses (a recipe) or stops.")
        arm = ArmedAction(button, lambda p=purpose: self.take(p), what="TAKE CONTROL")
        layout.addWidget(label)
        layout.addWidget(button)
        self.rows[purpose] = (self.addWidget(box), label, button, arm)

    def take(self, purpose: str):
        for sub in self.groups.get(purpose, []):
            take_control(self.cmd_thread, sub)
