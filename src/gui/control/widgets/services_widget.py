"""Services panel: each microservice's state as the service manager reports it, with Stop/Start
and Restart. A row is only redrawn when what it shows changes (the panel used to re-render every
row ten times a second)."""
import time

from PyQt6.QtWidgets import QGridLayout, QGroupBox, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from src.gui.style import chip, set_role, set_tone

# Manager state -> chip tone (grey is normal: a running service is a quiet green, problems stand out)
STATE_TONES = {
    "ONLINE": "ok",
    "STARTING": "info",
    "CRASHED": "fault",
    "HANGING": "fault",
    "BACKOFF": "warn",
    "STOPPED": "off",
}
MANAGER_TIMEOUT_S = 2.5


class ServicesWidget(QWidget):
    def __init__(self, cmd_thread):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.last_manager_beat = 0
        self._manager_online = None

        self.outer_layout = QVBoxLayout(self)

        svc_group = QGroupBox("Microservices")
        group_layout = QVBoxLayout(svc_group)

        self.lbl_manager_status = chip("SERVICE MANAGER: WAITING FOR DATA...", "unknown")
        group_layout.addWidget(self.lbl_manager_status)

        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(8)
        self.grid.setVerticalSpacing(4)
        self.grid.setColumnStretch(2, 1)
        group_layout.addLayout(self.grid)

        self.service_rows = {}
        self.outer_layout.addWidget(svc_group)
        self.outer_layout.addStretch()

    def _cmd_service(self, svc_name, action):
        payload = {"command": action, "service": svc_name}
        self.cmd_thread.send_command("manager", "manager_cmd", payload)

    def _cmd_restart_service(self, svc_name):
        self._cmd_service(svc_name, "restart")

    def _add_row(self, svc_name: str):
        r = len(self.service_rows)
        name = QLabel(svc_name)
        set_role(name, "readout-sm")
        state = chip("", "unknown")
        state.setMinimumWidth(state.fontMetrics().horizontalAdvance("STARTING") + 24)
        detail = QLabel("")
        set_role(detail, "caption")
        detail.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)   # may be cut, never widens the panel
        btn_stop = QPushButton("Stop")
        btn_stop.setToolTip("Stop this service and keep it stopped (no automatic restarts).")
        btn_stop.clicked.connect(lambda checked, s=svc_name, b=btn_stop: self._cmd_service(
            s, "start" if b.text() == "Start" else "stop"))
        btn_restart = QPushButton("Restart")
        btn_restart.setToolTip("Restart now (also clears the restart back-off).")
        btn_restart.clicked.connect(lambda checked, s=svc_name: self._cmd_restart_service(s))
        for col, w in enumerate((name, state, detail, btn_stop, btn_restart)):
            self.grid.addWidget(w, r, col)
        self.service_rows[svc_name] = {"name": name, "label": state, "detail": detail, "btn": btn_restart,
                                       "btn_stop": btn_stop, "shown": None}

    @staticmethod
    def _show(row, state: str, detail: str, tip: str):
        shown = (state, detail, tip)
        if row["shown"] == shown:
            return                          # nothing changed: leave the row alone
        row["shown"] = shown
        row["label"].setText(state)
        set_tone(row["label"], STATE_TONES.get(state, "unknown"))
        row["detail"].setText(detail)
        row["detail"].setToolTip(detail if not tip else f"{detail}\n{tip}")
        row["name"].setToolTip(tip)
        row["btn_stop"].setText("Start" if state == "STOPPED" else "Stop")

    def update_telemetry(self, data: dict):
        services = data.get("manager.services")
        info = data.get("manager.restart_info") or {}
        now = time.time()

        if services:
            self.last_manager_beat = now
            for svc_name, state in services.items():
                if svc_name not in self.service_rows:
                    self._add_row(svc_name)
                details = info.get(svc_name, {})
                detail, tip = "", ""
                if state == "BACKOFF":
                    detail = (f"restart in {details.get('retry_in', 0):.0f} s, "
                              f"failure {details.get('failures', '?')}")
                    tip = details.get("reason", "")
                elif details.get("failures"):
                    detail = f"{details['failures']} recent restart(s)"
                    tip = f"Last problem: {details.get('reason') or 'see the service log'}"
                self._show(self.service_rows[svc_name], state, detail, tip)

        online = now - self.last_manager_beat <= MANAGER_TIMEOUT_S
        if online != self._manager_online:
            self._manager_online = online
            self.lbl_manager_status.setText(f"SERVICE MANAGER: {'ONLINE' if online else 'OFFLINE'}")
            set_tone(self.lbl_manager_status, "ok" if online else "fault")
            for row in self.service_rows.values():
                row["btn"].setEnabled(online)
                row["btn_stop"].setEnabled(online)
        if not online:
            for row in self.service_rows.values():
                self._show(row, "UNKNOWN", "", "")
