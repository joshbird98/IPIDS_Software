from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel,
    QComboBox, QFrame, QGroupBox
)

from src.gui.reflow import ReflowGrid
from src.gui.style import chip, reserve_width, set_ink, set_role, set_tone


class AdamMonitorWidget(QWidget):
    def __init__(self, cmd_thread):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.main_layout = QVBoxLayout(self)

        self.range_map = ["+/- 150 mV", "+/- 500 mV", "+/- 1 V", "+/- 5 V", "+/- 10 V"]

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
        group = QGroupBox("Beamline current monitors (ADAM-6017)")
        group_layout = QVBoxLayout(group)
        # Four cards across in a wide panel, fewer (down to one) as it narrows
        grid = ReflowGrid(max_columns=4)

        for i, ch in enumerate(self.channels):
            frame = QFrame()
            set_role(frame, "card")
            fl = QVBoxLayout(frame)
            fl.setContentsMargins(8, 6, 8, 8)
            fl.setSpacing(4)

            lbl_title = QLabel(ch["name"])
            set_role(lbl_title, "caption")

            lbl_val = QLabel("--- \u03BCA")
            set_role(lbl_val, "readout-lg")
            reserve_width(lbl_val, "+000.00 \u03BCA")

            cb_range = QComboBox()
            cb_range.addItems(["Auto", "+/- 150 mV", "+/- 500 mV", "+/- 1 V", "+/- 5 V", "+/- 10 V"])
            cb_range.currentIndexChanged.connect(
                lambda idx, tag=ch['tag']: self.cmd_thread.send_command("adam", f"{tag}.cmd_range", idx)
            )

            lbl_status = chip("OFFLINE", "unknown")

            fl.addWidget(lbl_title)
            fl.addWidget(lbl_val)
            fl.addWidget(cb_range)
            fl.addWidget(lbl_status)

            grid.add(frame)
            self.ui_elements.append({
                "val": lbl_val,
                "range": cb_range,
                "status": lbl_status
            })

        group_layout.addWidget(grid)
        self.main_layout.addWidget(group)
        self.main_layout.addStretch()

    def _format_current(self, amps: float) -> str:
        """Dynamically scales base Amps to a readable SI prefix string."""
        abs_amps = abs(amps)
        if abs_amps >= 1e-3:
            return f"{amps * 1e3:+.2f} mA"
        elif abs_amps >= 1e-6:
            return f"{amps * 1e6:+.2f} \u03BCA"
        elif abs_amps >= 1e-9:
            return f"{amps * 1e9:+.2f} nA"
        else:
            return f"{amps * 1e12:+.2f} pA"

    def update_telemetry(self, data: dict):
        sys_connected = bool(data.get("system.connected", False))
        svc_connected = bool(data.get("current_mon.connected", False))
        adam_fail = bool(data.get("system.facilities.adam.stat_comms_fail", True))

        if not sys_connected or not svc_connected or adam_fail:
            for ui in self.ui_elements:
                ui["val"].setText("---")
                set_ink(ui["val"], "faint")
                ui["status"].setText("OFFLINE")
                set_tone(ui["status"], "unknown")
                ui["range"].setEnabled(False)
            return

        for i, ch in enumerate(self.channels):
            tag = ch["tag"]
            ui = self.ui_elements[i]

            rb_current = data.get(f"{tag}.rb_current")
            rb_mode = data.get(f"{tag}.rb_op_mode")
            overrange = bool(data.get(f"{tag}.stat_overrange", False))
            ranging = bool(data.get(f"{tag}.stat_ranging", False))

            # --- MODIFIED SECTION ---
            rb_range_idx = data.get(f"{tag}.rb_range_idx")
            if isinstance(rb_range_idx, (int, float)) and float(rb_range_idx).is_integer() \
                    and 0 <= int(rb_range_idx) < len(self.range_map):
                act_range = self.range_map[int(rb_range_idx)]
            else:
                act_range = "UNKNOWN"
            # ------------------------

            ui["range"].setEnabled(True)

            # Update combo box strictly if it does not have user focus to prevent fighting user input
            if rb_mode is not None and not ui["range"].hasFocus():
                ui["range"].blockSignals(True)
                ui["range"].setCurrentIndex(int(rb_mode))
                ui["range"].blockSignals(False)

            if ranging:
                ui["val"].setText("RANGING...")
                set_ink(ui["val"], "muted")
                ui["status"].setText("RANGING")
                set_tone(ui["status"], "info")
            elif overrange:
                ui["val"].setText("OVERRANGE")
                set_ink(ui["val"], "warn")
                ui["status"].setText(f"CLIP [{act_range}]")
                set_tone(ui["status"], "warn")
            else:
                if rb_current is not None:
                    ui["val"].setText(self._format_current(float(rb_current)))
                set_ink(ui["val"], None)
                ui["status"].setText(f"OK [{act_range}]")
                set_tone(ui["status"], "ok")