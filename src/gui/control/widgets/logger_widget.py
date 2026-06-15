import time
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton, QGroupBox, QListWidget, QListWidgetItem, QComboBox, QLineEdit
)
from PyQt6.QtGui import QColor, QPixmap, QIcon
from PyQt6.QtCore import QSize

# --- Experiment Logging Subsystem Component ---

class ExperimentLoggerWidget(QWidget):
    def __init__(self, event_helper):
        super().__init__()
        self.event_helper = event_helper
        self.last_manager_beat = 0

        layout = QVBoxLayout(self)

        # 1. Phase Change Group
        phase_group = QGroupBox("Experiment Phase Control")
        phase_layout = QHBoxLayout()

        self.le_phase = QLineEdit()
        self.le_phase.setPlaceholderText("e.g., Silicon_Implant_Run_04")
        self.le_phase.returnPressed.connect(self._set_phase)

        self.btn_phase = QPushButton("SET NEW PHASE")
        self.btn_phase.setStyleSheet("""
            QPushButton { background-color: #4CAF50; color: white; font-weight: bold; padding: 6px; border-radius: 4px; }
            QPushButton:disabled { background-color: #757575; color: #B0B0B0; }
        """)
        self.btn_phase.clicked.connect(self._set_phase)

        phase_layout.addWidget(self.le_phase)
        phase_layout.addWidget(self.btn_phase)
        phase_group.setLayout(phase_layout)
        layout.addWidget(phase_group)

        # 2. User Marker Group
        marker_group = QGroupBox("Add Log Marker / Observation")
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

        for name, hex_code in self.colors.items():
            pixmap = QPixmap(16, 16)
            pixmap.fill(QColor(hex_code))
            icon = QIcon(pixmap)
            self.cb_color.addItem(icon, name, hex_code)

        self.cb_color.setIconSize(QSize(16, 16))

        self.btn_marker = QPushButton("ADD MARKER")
        self.btn_marker.setStyleSheet("""
            QPushButton { background-color: #E0E0E0; color: black; font-weight: bold; padding: 6px; border-radius: 4px; }
            QPushButton:disabled { background-color: #757575; color: #B0B0B0; }
        """)
        self.btn_marker.clicked.connect(self._add_marker)

        marker_layout.addWidget(QLabel("<b>Marker Color:</b>"), 0, 0)
        marker_layout.addWidget(self.cb_color, 0, 1)
        marker_layout.addWidget(self.le_marker, 1, 0, 1, 2)
        marker_layout.addWidget(self.btn_marker, 2, 0, 1, 2)
        marker_group.setLayout(marker_layout)
        layout.addWidget(marker_group)

        # 3. Session History
        self.list_history = QListWidget()
        self.list_history.setStyleSheet("background-color: #FAFAFA; font-family: monospace; font-size: 10pt;")
        layout.addWidget(QLabel("<b>Recent Session Logs:</b>"))
        layout.addWidget(self.list_history)

        layout.addStretch()

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
            list_item = QListWidgetItem(f"[{time.strftime('%H:%M:%S')}] PHASE: {phase.upper()}")
            list_item.setBackground(QColor("#4CAF50"))
            list_item.setForeground(QColor("white"))
            self.list_history.insertItem(0, list_item)
            self.le_phase.clear()

    def _add_marker(self):
        text = self.le_marker.text().strip()
        if text:
            hex_color = self.cb_color.currentData()
            self.event_helper.log_user_marker(time.time(), text, hex_color)
            list_item = QListWidgetItem(f"[{time.strftime('%H:%M:%S')}] MARKER: {text}")
            list_item.setBackground(QColor(hex_color))
            if QColor(hex_color).lightness() < 128:
                list_item.setForeground(QColor("white"))
            else:
                list_item.setForeground(QColor("black"))
            self.list_history.insertItem(0, list_item)
            self.le_marker.clear()

