import time
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QGroupBox, QScrollArea, QListWidget, QListWidgetItem)
from PyQt6.QtCore import Qt

from src.core.theme import (
    COLOR_OK,
    COLOR_FAULT,
    COLOR_INACTIVE,
    COLOR_WARNING,
    COLOR_BUTTON_STANDARD
)

# --- System Diagnostics Subsystem Component ---

class DiagnosticsWidget(QWidget):
    def __init__(self, cmd_thread, fault_engine):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.fault_engine = fault_engine
        self.last_manager_beat = 0

        main_layout = QHBoxLayout(self)

        svc_group = QGroupBox("Microservice Infrastructure")
        self.svc_layout = QVBoxLayout()

        self.lbl_manager_status = QLabel("<b>SERVICE MANAGER:</b> WAITING FOR DATA...")
        self.lbl_manager_status.setStyleSheet(COLOR_WARNING)
        self.svc_layout.addWidget(self.lbl_manager_status)

        self.service_rows = {}
        self.svc_layout.addStretch()
        svc_group.setLayout(self.svc_layout)

        fault_group = QGroupBox("Hardware && Software Fault Registry")
        fault_layout = QVBoxLayout()

        fault_layout.addWidget(QLabel("<b>ACTIVE ALARMS:</b>"))
        self.active_alarms_list = QListWidget()
        self.active_alarms_list.setStyleSheet("background-color: #ffebee; color: #b71c1c; font-weight: bold;")
        self.active_alarms_list.setMaximumHeight(150)
        fault_layout.addWidget(self.active_alarms_list)

        fault_layout.addWidget(QLabel("<b>DIAGNOSTIC BIT MAP:</b>"))
        self.fault_labels = {}

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)

        for udt_name, udt_info in self.fault_engine.fault_structures.items():
            clean_name = udt_name.replace("UDT_Fault_Word_", "").replace("_", " ")
            gb = QGroupBox(clean_name)
            gl = QGridLayout()

            row, col = 0, 0
            for bit_str, info in udt_info.get("bits", {}).items():
                lbl = QLabel(info["name"])
                lbl.setStyleSheet(COLOR_OK)
                lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

                desc = info.get("description", "No description provided.")
                lbl.setToolTip(f"<b>{info['name']}</b><br>{desc}<br><i>Status: OK</i>")

                gl.addWidget(lbl, row, col)
                self.fault_labels[info["name"]] = {"label": lbl, "desc": desc}

                col += 1
                if col > 3:
                    col = 0
                    row += 1

            gb.setLayout(gl)
            scroll_layout.addWidget(gb)

        scroll_layout.addStretch()
        scroll.setWidget(scroll_content)
        fault_layout.addWidget(scroll)
        fault_group.setLayout(fault_layout)

        main_layout.addWidget(svc_group, stretch=1)
        main_layout.addWidget(fault_group, stretch=3)

    def _cmd_restart_service(self, svc_name):
        payload = {"command": "restart", "service": svc_name}
        self.cmd_thread.send_command("manager", "manager_cmd", payload)

    def update_telemetry(self, data: dict):
        master_comms_lost = not bool(data.get("system.connected", False)) or bool(
            data.get("ion_beam.system.pc_plc_comms_lost", False))

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
                    self.svc_layout.insertWidget(self.svc_layout.count() - 1, row_widget)
                    self.service_rows[svc_name] = {"label": lbl, "btn": btn_restart}

                lbl = self.service_rows[svc_name]["label"]
                lbl.setText(f"<b>{svc_name}:</b> {state}")
                if state == "ONLINE":
                    lbl.setStyleSheet("color: #4CAF50;")  # Green
                elif state == "STARTING":
                    # Flashes a vibrant Blue/Orange color to clearly indicate initialization
                    lbl.setStyleSheet("color: #2196F3; font-weight: bold;")
                elif state in ["CRASHED", "HANGING"]:
                    lbl.setStyleSheet("color: #F44336; font-weight: bold;")  # Red
                else:
                    lbl.setStyleSheet("color: #757575;")  # Gray

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

        word_mapping = {
            "ion_beam.faults.word_0_system": "UDT_Fault_Word_0_System",
            "ion_beam.faults.word_1_pumps": "UDT_Fault_Word_1_Pumps",
            "ion_beam.faults.word_2_source": "UDT_Fault_Word_2_Source",
            #"ion_beam.faults.word_3_spellman": "UDT_Fault_Word_3_Spellman",
            "ion_beam.faults.word_4_magnet": "UDT_Fault_Word_4_Magnet",
            "ion_beam.faults.word_5_gauges": "UDT_Fault_Word_5_Gauges"
        }

        if master_comms_lost:
            for item in self.fault_labels.values():
                item["label"].setStyleSheet(COLOR_INACTIVE)
                item["label"].setToolTip("Comms Offline")
            return

        self.active_alarms_list.clear()

        for tag, udt_key in word_mapping.items():
            raw_word = data.get(tag)
            if raw_word is not None:
                active_faults = self.fault_engine.get_active_faults(udt_key, int(raw_word))
                active_names = [f["name"] for f in active_faults]

                udt_info = self.fault_engine.fault_structures.get(udt_key, {})
                for bit_str, info in udt_info.get("bits", {}).items():
                    name = info["name"]
                    if name in self.fault_labels:
                        lbl_item = self.fault_labels[name]
                        lbl = lbl_item["label"]
                        desc = lbl_item["desc"]

                        if name in active_names:
                            lbl.setStyleSheet(COLOR_FAULT)
                            lbl.setToolTip(f"<b>{name}</b><br>{desc}<br><i style='color:red;'>Status: FAULT ACTIVE</i>")
                            list_item = QListWidgetItem(f"⚠️ {name.upper()}: {desc}")
                            self.active_alarms_list.addItem(list_item)
                        else:
                            lbl.setStyleSheet(COLOR_OK)
                            lbl.setToolTip(f"<b>{name}</b><br>{desc}<br><i style='color:green;'>Status: OK</i>")
