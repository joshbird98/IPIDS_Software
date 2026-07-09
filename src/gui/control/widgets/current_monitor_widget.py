from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QGridLayout, QLabel,
    QComboBox, QFrame, QGroupBox
)
from PyQt6.QtCore import Qt

from src.core.theme import (
    COLOR_OK,
    COLOR_FAULT,
    COLOR_INACTIVE,
    COLOR_WARNING
)

class AdamMonitorWidget(QWidget):
    def __init__(self, cmd_thread):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.main_layout = QVBoxLayout(self)

        self.channels = [
            {"name": "Object Slits (Left)", "tag": "ion_beam.beamline.object_slits.left"},
            {"name": "Object Slits (Right)", "tag": "ion_beam.beamline.object_slits.right"},
            {"name": "Straight Through", "tag": "ion_beam.beamline.straight_through"},
            {"name": "Image Slits (Left)", "tag": "ion_beam.beamline.image_slits.left"},
            {"name": "Image Slits (Right)", "tag": "ion_beam.beamline.image_slits.right"},
            {"name": "Faraday Front Aperture", "tag": "ion_beam.beamline.faraday.front_aperture"},
            {"name": "ADAM Aux Channel 6", "tag": "ion_beam.system.facilities.adam.ch6"},
            {"name": "ADAM Aux Channel 7", "tag": "ion_beam.system.facilities.adam.ch7"}
        ]

        self.ui_elements = []
        self._init_ui()

    def _init_ui(self):
        group = QGroupBox("Beamline Current Monitors (ADAM-6017)")
        grid = QGridLayout()

        for i, ch in enumerate(self.channels):
            frame = QFrame()
            frame.setFrameStyle(QFrame.Shape.StyledPanel | QFrame.Shadow.Raised)
            fl = QVBoxLayout(frame)

            lbl_title = QLabel(f"<b>{ch['name']}</b>")
            lbl_title.setAlignment(Qt.AlignmentFlag.AlignCenter)

            lbl_val = QLabel("--- \u03BCA")
            lbl_val.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl_val.setStyleSheet("font-size: 16pt; font-family: monospace; font-weight: bold;")

            cb_range = QComboBox()
            cb_range.addItems(["Auto", "+/- 150 mV", "+/- 500 mV", "+/- 1 V", "+/- 5 V", "+/- 10 V"])
            cb_range.currentIndexChanged.connect(
                lambda idx, tag=ch['tag']: self.cmd_thread.send_command("adam", f"{tag}.cmd_range", idx)
            )

            lbl_status = QLabel("OFFLINE")
            lbl_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl_status.setStyleSheet(COLOR_INACTIVE)

            fl.addWidget(lbl_title)
            fl.addWidget(lbl_val)
            fl.addWidget(cb_range)
            fl.addWidget(lbl_status)

            # Arrange in a 2x4 grid
            grid.addWidget(frame, i // 4, i % 4)
            self.ui_elements.append({
                "val": lbl_val,
                "range": cb_range,
                "status": lbl_status
            })

        group.setLayout(grid)
        self.main_layout.addWidget(group)
        self.main_layout.addStretch()

    def update_telemetry(self, data: dict):
        sys_connected = bool(data.get("system.connected", False))
        svc_connected = bool(data.get("current_mon.connected", False))
        adam_fail = bool(data.get("system.facilities.adam.stat_comms_fail", True))

        if not sys_connected or not svc_connected or adam_fail:
            for ui in self.ui_elements:
                ui["val"].setText("---")
                ui["status"].setText("OFFLINE")
                ui["status"].setStyleSheet(COLOR_INACTIVE)
                ui["range"].setEnabled(False)
            return

        for i, ch in enumerate(self.channels):
            tag = ch["tag"]
            ui = self.ui_elements[i]

            rb_current = data.get(f"{tag}.rb_current")
            rb_mode = data.get(f"{tag}.rb_op_mode")
            act_range = data.get(f"{tag}.range", "UNKNOWN")
            overrange = bool(data.get(f"{tag}.stat_overrange", False))
            ranging = bool(data.get(f"{tag}.stat_ranging", False))

            ui["range"].setEnabled(True)

            # Update combo box strictly if it does not have user focus to prevent fighting user input
            if rb_mode is not None and not ui["range"].hasFocus():
                ui["range"].blockSignals(True)
                ui["range"].setCurrentIndex(int(rb_mode))
                ui["range"].blockSignals(False)

            if ranging:
                ui["val"].setText("RANGING...")
                ui["status"].setText("RANGING")
                ui["status"].setStyleSheet(COLOR_WARNING)
            elif overrange:
                ui["val"].setText("OVERRANGE")
                ui["status"].setText(f"CLIP [{act_range}]")
                ui["status"].setStyleSheet(COLOR_FAULT)
            else:
                if rb_current is not None:
                    ui["val"].setText(f"{float(rb_current):+.3f} \u03BCA")
                ui["status"].setText(f"OK [{act_range}]")
                ui["status"].setStyleSheet(COLOR_OK)