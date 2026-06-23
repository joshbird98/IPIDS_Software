import time
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QGroupBox,
    QScrollArea, QFrame
)
from src.core.theme import (
    COLOR_WARNING,
    COLOR_BUTTON_STANDARD
)

class ServicesWidget(QWidget):
    def __init__(self, cmd_thread):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.last_manager_beat = 0

        # --- Layout Refactor: QScrollArea ---
        self.outer_layout = QVBoxLayout(self)
        self.outer_layout.setContentsMargins(0, 0, 0, 0)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)

        self.content_widget = QWidget()
        self.content_layout = QVBoxLayout(self.content_widget)

        svc_group = QGroupBox("Microservice Infrastructure")
        self.svc_layout = QVBoxLayout()

        self.lbl_manager_status = QLabel("<b>SERVICE MANAGER:</b> WAITING FOR DATA...")
        self.lbl_manager_status.setStyleSheet(COLOR_WARNING)
        self.svc_layout.addWidget(self.lbl_manager_status)

        self.service_rows = {}
        self.svc_layout.addStretch()
        svc_group.setLayout(self.svc_layout)

        self.content_layout.addWidget(svc_group)
        self.content_layout.addStretch()

        self.scroll_area.setWidget(self.content_widget)
        self.outer_layout.addWidget(self.scroll_area)

    def _cmd_restart_service(self, svc_name):
        payload = {"command": "restart", "service": svc_name}
        self.cmd_thread.send_command("manager", "manager_cmd", payload)

    def update_telemetry(self, data: dict):
        services = data.get("manager.services")
        now = time.time()

        if services:
            self.last_manager_beat = now
            self.lbl_manager_status.setText("<b>SERVICE MANAGER:</b> ONLINE")
            self.lbl_manager_status.setStyleSheet("color: #4CAF50; font-size: 11pt;")

            for svc_name, state in services.items():
                if svc_name not in self.service_rows:
                    row_widget = QWidget()
                    row_layout = QHBoxLayout(row_widget)
                    row_layout.setContentsMargins(0, 0, 0, 0)

                    lbl = QLabel(f"<b>{svc_name}:</b> {state}")
                    btn_restart = QPushButton("Restart")
                    btn_restart.setStyleSheet(COLOR_BUTTON_STANDARD)
                    btn_restart.clicked.connect(lambda checked, s=svc_name: self._cmd_restart_service(s))

                    row_layout.addWidget(lbl)
                    row_layout.addWidget(btn_restart)
                    # Insert right above the bottom stretch
                    self.svc_layout.insertWidget(self.svc_layout.count() - 1, row_widget)
                    self.service_rows[svc_name] = {"label": lbl, "btn": btn_restart}

                lbl = self.service_rows[svc_name]["label"]
                lbl.setText(f"<b>{svc_name}:</b> {state}")
                if state == "ONLINE":
                    lbl.setStyleSheet("color: #4CAF50;")
                elif state == "STARTING":
                    lbl.setStyleSheet("color: #2196F3; font-weight: bold;")
                elif state in ["CRASHED", "HANGING"]:
                    lbl.setStyleSheet("color: #F44336; font-weight: bold;")
                else:
                    lbl.setStyleSheet("color: #757575;")

        if now - self.last_manager_beat > 2.5:
            self.lbl_manager_status.setText("<b>SERVICE MANAGER:</b> OFFLINE")
            self.lbl_manager_status.setStyleSheet(
                "background-color: #F44336; color: white; font-size: 11pt; padding: 4px;")
            for row in self.service_rows.values():
                row["label"].setStyleSheet("color: #757575;")
                row["label"].setText(row["label"].text().split(":")[0] + ": UNKNOWN")
                row["btn"].setEnabled(False)
        else:
            for row in self.service_rows.values():
                row["btn"].setEnabled(True)