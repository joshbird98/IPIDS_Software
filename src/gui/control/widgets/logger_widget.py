import time
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton, QGroupBox, QListWidget, QListWidgetItem, QComboBox, QLineEdit
)
from PyQt6.QtGui import QFont
from PyQt6.QtCore import QSize

from src.gui import theme
from src.gui.style import set_role, set_tone, swatch

# --- Experiment Logging Subsystem Component ---

class ExperimentLoggerWidget(QWidget):
    def __init__(self, event_helper):
        super().__init__()
        self.event_helper = event_helper
        self.last_manager_beat = 0

        layout = QVBoxLayout(self)

        # 1. Phase Change Group
        phase_group = QGroupBox("Experiment phase")
        phase_layout = QHBoxLayout()

        self.le_phase = QLineEdit()
        self.le_phase.setPlaceholderText("e.g., Silicon_Implant_Run_04")
        self.le_phase.returnPressed.connect(self._set_phase)

        self.btn_phase = QPushButton("SET NEW PHASE")
        set_tone(self.btn_phase, "primary")
        self.btn_phase.clicked.connect(self._set_phase)

        phase_layout.addWidget(self.le_phase)
        phase_layout.addWidget(self.btn_phase)
        phase_group.setLayout(phase_layout)
        layout.addWidget(phase_group)

        # 2. User Marker Group
        marker_group = QGroupBox("Add a log marker or observation")
        marker_layout = QGridLayout()

        self.le_marker = QLineEdit()
        self.le_marker.setPlaceholderText("Type observation or comment here...")
        self.le_marker.returnPressed.connect(self._add_marker)

        self.cb_color = QComboBox()
        self.colors = {
            "White": "#FFFFFF",
            "Cyan": "#00E5FF",
            "Yellow": "#FFEA00",
            "Magenta": "#FF4081",
            "Purple": "#9C27B0",
            "Pink": "#E91E63"
        }

        # (The colour is kept with the marker in events.db; the viewer draws it, adjusted if needed
        # to stand out on the plot background of its theme)
        for name, hex_code in self.colors.items():
            self.cb_color.addItem(swatch(hex_code), name, hex_code)

        self.cb_color.setIconSize(QSize(14, 14))

        self.btn_marker = QPushButton("ADD MARKER")
        self.btn_marker.clicked.connect(self._add_marker)

        lbl_colour = QLabel("Marker colour")
        set_role(lbl_colour, "caption")
        marker_layout.addWidget(lbl_colour, 0, 0)
        marker_layout.addWidget(self.cb_color, 0, 1)
        marker_layout.addWidget(self.le_marker, 1, 0, 1, 2)
        marker_layout.addWidget(self.btn_marker, 2, 0, 1, 2)
        marker_group.setLayout(marker_layout)
        layout.addWidget(marker_group)

        # 3. Session History
        self.list_history = QListWidget()
        self.list_history.setFont(theme.mono_font())
        self.list_history.setIconSize(QSize(12, 12))
        self.list_history.setMinimumHeight(80)
        lbl_history = QLabel("This session's phases and markers")
        set_role(lbl_history, "caption")
        layout.addWidget(lbl_history)
        layout.addWidget(self.list_history, stretch=1)

    def update_telemetry(self, data: dict):
        # Update our local manager heartbeat
        if "manager.services" in data:
            self.last_manager_beat = time.time()

        manager_dead = (time.time() - self.last_manager_beat) > 2.5
        events_state = data.get("manager.services", {}).get("service_events", "OFFLINE")

        # Evaluate Lockout condition
        is_locked = manager_dead or (events_state != "ONLINE")

        if manager_dead:
            lock_reason = "Service Manager is offline."
        else:
            lock_reason = f"service_events is {events_state}."

        controls = [self.le_phase, self.btn_phase, self.le_marker, self.cb_color, self.btn_marker]

        for ctrl in controls:
            ctrl.setEnabled(not is_locked)
            ctrl.setToolTip(f"Disabled: {lock_reason}" if is_locked else "")

    def _set_phase(self):
        phase = self.le_phase.text().strip()
        if phase:
            self.event_helper.log_phase(phase)
            list_item = QListWidgetItem(swatch(theme.color("ev_phase")),
                                        f"[{time.strftime('%H:%M:%S')}] PHASE: {phase.upper()}")
            font = QFont(self.list_history.font())
            font.setBold(True)
            list_item.setFont(font)
            self.list_history.insertItem(0, list_item)
            self.le_phase.clear()

    def _add_marker(self):
        text = self.le_marker.text().strip()
        if text:
            hex_color = self.cb_color.currentData()
            self.event_helper.log_user_marker(time.time(), text, hex_color)
            list_item = QListWidgetItem(swatch(hex_color), f"[{time.strftime('%H:%M:%S')}] MARKER: {text}")
            self.list_history.insertItem(0, list_item)
            self.le_marker.clear()

