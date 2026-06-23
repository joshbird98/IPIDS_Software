import time
import math
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QPushButton, QGroupBox, QDoubleSpinBox, QComboBox, QStackedWidget
)
from PyQt6.QtCore import Qt, QRectF, QPointF
from PyQt6.QtGui import QPainter, QPen, QColor, QFont

from src.core.theme import (
    COLOR_OK, COLOR_FAULT, COLOR_INACTIVE, COLOR_WARNING, COLOR_BUTTON_STANDARD
)


class SpeedoWidget(QWidget):
    """Custom QPainter widget to draw an analog gauge with polarity sign and color."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(250, 150)
        self.raw_val = 0.0
        self.max_val = 1e-6
        self.setToolTip("Analog visualization of the live beam current. Color and sign indicate polarity.")

    def update_gauge(self, value, scale_max):
        self.raw_val = value
        self.max_val = scale_max
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        width = self.width()
        height = self.height()
        center_x = width / 2
        center_y = height - 20
        radius = min(width / 2, height) - 30

        # --- 1. Draw Background Arc ---
        rect = QRectF(center_x - radius, center_y - radius, radius * 2, radius * 2)
        pen_bg = QPen(QColor("#333333"), 8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        painter.setPen(pen_bg)
        painter.drawArc(rect, 180 * 16, -180 * 16)  # Start at left (180 deg), span full half-circle

        # --- 2. Calculate Standard 0-Max Ratio using Absolute Value ---
        abs_val = abs(self.raw_val)
        clamped_val = min(abs_val, self.max_val)
        ratio = clamped_val / self.max_val if self.max_val > 0 else 0
        span_angle = int(-180 * ratio * 16)

        # --- 3. Determine Color by Polarity & Saturation ---
        if ratio > 0.95:
            arc_color = QColor("#FF9800")  # Warning: Pegged at limits
        elif self.raw_val < 0:
            arc_color = QColor("#2196F3")  # Blue for Negative Current
        else:
            arc_color = QColor("#4CAF50")  # Green for Positive Current

        # Draw Active Value Arc
        pen_fg = QPen(arc_color, 8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        painter.setPen(pen_fg)
        painter.drawArc(rect, 180 * 16, span_angle)

        # --- 4. Draw Polarity Sign Overlay ---
        sign_str = "-" if self.raw_val < 0 else "+"
        painter.setPen(arc_color)

        # Create a massive bold font for the sign
        font = QFont("Monospace", 48, QFont.Weight.Bold)
        painter.setFont(font)

        # Center the text directly above the needle pivot
        text_rect = QRectF(center_x - 30, center_y - 70, 60, 60)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, sign_str)

        # --- 5. Calculate and Draw Needle ---
        angle_rad = math.radians(180 - (180 * ratio))
        needle_length = radius - 10
        end_x = center_x + needle_length * math.cos(angle_rad)
        end_y = center_y - needle_length * math.sin(angle_rad)

        painter.setPen(QPen(QColor("#F44336"), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(QPointF(center_x, center_y), QPointF(end_x, end_y))

        # --- 6. Draw Base Pivot ---
        painter.setBrush(QColor("#E0E0E0"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(center_x, center_y), 8, 8)

class FaradaySMUWidget(QWidget):
    def __init__(self, cmd_thread):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.main_layout = QVBoxLayout(self)

        self._init_actuator_ui()
        self._init_bias_ui()
        self._init_measurement_ui()

        self.main_layout.addStretch()

    def _dispatch_smu(self, tag: str, value):
        self.cmd_thread.send_command("smu", tag, value)

    def _dispatch_plc(self, tag: str, value):
        self.cmd_thread.send_command("plc", tag, value)

    def _init_actuator_ui(self):
        group = QGroupBox("1. Faraday Cup Pneumatics")
        layout = QVBoxLayout()

        self.btn_fc = QPushButton("FARADAY CUP: UNKNOWN")
        self.btn_fc.setCheckable(True)
        self.btn_fc.setStyleSheet(COLOR_BUTTON_STANDARD)
        self.btn_fc.setToolTip("Click to physically insert or retract the Faraday Cup into the beam path.")
        self.btn_fc.clicked.connect(
            lambda *args, b=self.btn_fc: self._dispatch_plc(
                "ion_beam.beamline.diagnostics.cmd_insert_fc", b.isChecked()
            )
        )
        layout.addWidget(self.btn_fc)
        group.setLayout(layout)
        self.main_layout.addWidget(group)

    def _init_bias_ui(self):
        group = QGroupBox("2. SMU Bias Voltage")
        layout = QGridLayout()

        self.lbl_bias_status = QLabel("STATUS: OFFLINE")
        self.lbl_bias_status.setStyleSheet(COLOR_INACTIVE)
        self.lbl_bias_status.setToolTip("Indicates whether the Source-Measure Unit is actively applying bias voltage.")
        layout.addWidget(self.lbl_bias_status, 0, 0, 1, 2)

        lbl_sp = QLabel("<b>Bias Setpoint:</b>")
        lbl_sp.setToolTip(
            "Target bias voltage. Positive bias suppresses secondary electron emission; negative bias repels ambient electrons.")
        layout.addWidget(lbl_sp, 1, 0)

        self.sp_bias = QDoubleSpinBox()
        self.sp_bias.setRange(-200.0, 200.0)
        self.sp_bias.setSuffix(" V")
        self.sp_bias.setDecimals(1)
        self.sp_bias.setKeyboardTracking(False)
        self.sp_bias.setToolTip("Enter the target bias voltage (-200V to +200V). Requires Master Safety Relay.")
        self.sp_bias.editingFinished.connect(
            lambda: self._dispatch_smu("ion_beam.beamline.faraday.smu.sp_requested_voltage", self.sp_bias.value())
        )
        layout.addWidget(self.sp_bias, 1, 1)

        self.lbl_bias_rb = QLabel("RB: --- V")
        self.lbl_bias_rb.setMinimumWidth(80)
        self.lbl_bias_rb.setToolTip("Live readback of the actual voltage currently being applied by the SMU.")
        layout.addWidget(self.lbl_bias_rb, 1, 2)

        self.btn_enable_bias = QPushButton("ENABLE BIAS")
        self.btn_enable_bias.setCheckable(True)
        self.btn_enable_bias.setStyleSheet(COLOR_BUTTON_STANDARD)
        self.btn_enable_bias.setToolTip(
            "Click to enable or disable the SMU bias voltage output. Requires Master Safety Relay.")
        self.btn_enable_bias.clicked.connect(
            lambda *args, b=self.btn_enable_bias: self._dispatch_smu("ion_beam.beamline.faraday.smu.cmd_enable",
                                                                     b.isChecked())
        )
        layout.addWidget(self.btn_enable_bias, 1, 3)

        group.setLayout(layout)
        self.main_layout.addWidget(group)

    def _init_measurement_ui(self):
        group = QGroupBox("3. Beam Current Measurement")
        layout = QVBoxLayout()

        ctrl_row = QHBoxLayout()
        lbl_mode = QLabel("<b>View Mode:</b>")
        lbl_mode.setToolTip("Switch between a high-precision digital readout and an analog speedometer gauge.")
        ctrl_row.addWidget(lbl_mode)

        self.cb_view_mode = QComboBox()
        self.cb_view_mode.addItems(["Digital Readout", "Analog Gauge"])
        self.cb_view_mode.setToolTip("Select the display format for the beam current.")
        self.cb_view_mode.currentIndexChanged.connect(self._toggle_view_mode)
        ctrl_row.addWidget(self.cb_view_mode)

        lbl_scale = QLabel("<b>Gauge Scale:</b>")
        lbl_scale.setToolTip("Select the maximum scale for the analog gauge to optimize visual resolution.")
        ctrl_row.addWidget(lbl_scale)

        self.cb_gauge_scale = QComboBox()
        self.scale_map = {"10nA": 10e-9, "100 nA": 100e-9, "1 µA": 1e-6, "10 µA": 10e-6, "100 µA": 100e-6, "1 mA": 1e-3}
        self.cb_gauge_scale.addItems(list(self.scale_map.keys()))
        self.cb_gauge_scale.setCurrentText("100 µA")
        self.cb_gauge_scale.setToolTip("Adjusts the maximum value shown on the analog dial.")
        ctrl_row.addWidget(self.cb_gauge_scale)
        ctrl_row.addStretch()
        layout.addLayout(ctrl_row)

        self.display_stack = QStackedWidget()

        # Page 0: Digital Display
        self.page_digital = QWidget()
        dig_layout = QVBoxLayout(self.page_digital)
        self.lbl_digital_current = QLabel("--- µA")
        self.lbl_digital_current.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # SCOPED TO QLabel TO PREVENT TOOLTIP CASCADING
        self.lbl_digital_current.setStyleSheet(
            "QLabel { font-family: monospace; font-size: 32pt; font-weight: bold; color: #4CAF50; }")
        self.lbl_digital_current.setToolTip(
            "Live beam current measured by the Source-Measure Unit. Valid only when Safety Relay is active.")
        dig_layout.addWidget(self.lbl_digital_current)
        self.display_stack.addWidget(self.page_digital)

        # Page 1: Analog Gauge
        self.page_analog = QWidget()
        ana_layout = QVBoxLayout(self.page_analog)
        self.speedo = SpeedoWidget()
        self.lbl_speedo_text = QLabel("--- µA")
        self.lbl_speedo_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # SCOPED TO QLabel TO PREVENT TOOLTIP CASCADING
        self.lbl_speedo_text.setStyleSheet("QLabel { font-family: monospace; font-size: 14pt; font-weight: bold; }")
        self.lbl_speedo_text.setToolTip(
            "Live beam current measured by the Source-Measure Unit. Valid only when Safety Relay is active.")
        ana_layout.addWidget(self.speedo)
        ana_layout.addWidget(self.lbl_speedo_text)
        self.display_stack.addWidget(self.page_analog)

        layout.addWidget(self.display_stack)
        group.setLayout(layout)
        self.main_layout.addWidget(group)

    def _toggle_view_mode(self, index):
        self.display_stack.setCurrentIndex(index)
        self.cb_gauge_scale.setVisible(index == 1)

    def _format_current(self, amps: float) -> str:
        abs_amps = abs(amps)
        if abs_amps >= 1e-3:
            return f"{amps * 1e3:.2f} mA"
        elif abs_amps >= 1e-6:
            return f"{amps * 1e6:.2f} µA"
        elif abs_amps >= 1e-9:
            return f"{amps * 1e9:.2f} nA"
        else:
            return f"{amps * 1e12:.2f} pA"

    def update_telemetry(self, data: dict):
        master_comms_lost = not bool(data.get("system.connected", False)) or bool(
            data.get("ion_beam.system.pc_plc_comms_lost", False))
        relay_active = bool(data.get("ion_beam.facilities.safety_relay_active", False))

        # --- Arbitration Flags ---
        smu_ctrl_mode = float(data.get("ion_beam.beamline.faraday.smu.rb_ctrl_mode", 0.0))
        smu_auto_locked = (smu_ctrl_mode > 0.0)

        fc_ctrl_mode = float(data.get("ion_beam.beamline.diagnostics.rb_ctrl_mode", 0.0))
        fc_auto_locked = (fc_ctrl_mode > 0.0)

        # --- 1. Actuator Update ---
        fc_in = data.get("ion_beam.beamline.diagnostics.stat_fc_inserted")
        fc_out = data.get("ion_beam.beamline.diagnostics.stat_fc_retracted")
        fc_lock = bool(data.get("ion_beam.beamline.diagnostics.stat_lockout", False))
        air_ok = bool(data.get("ion_beam.facilities.stat_air_press_ok", True))

        self.btn_fc.blockSignals(True)
        fc_state_string = "UNKNOWN"

        if fc_in is not None and fc_out is not None:
            if bool(fc_in) and not bool(fc_out):
                fc_state_string = "INSERTED"
                self.btn_fc.setChecked(True)
                self.btn_fc.setText("FARADAY CUP: INSERTED")
                self.btn_fc.setStyleSheet(
                    """background-color: #FF9800; color: black; font-weight: bold; padding: 6px; border-radius: 4px;""")
            elif not bool(fc_in) and bool(fc_out):
                fc_state_string = "RETRACTED"
                self.btn_fc.setChecked(False)
                self.btn_fc.setText("FARADAY CUP: RETRACTED")
                self.btn_fc.setStyleSheet(COLOR_BUTTON_STANDARD)
            elif not bool(fc_in) and not bool(fc_out):
                fc_state_string = "UNPOWERED / TRAVELLING"
                self.btn_fc.setText("FARADAY CUP: TRAVELLING")
                self.btn_fc.setStyleSheet(COLOR_WARNING)
            else:
                fc_state_string = "SENSOR ERROR"
                self.btn_fc.setText("FARADAY CUP: LOGIC ERROR")
                self.btn_fc.setStyleSheet(COLOR_FAULT)

        if master_comms_lost:
            self.btn_fc.setEnabled(False)
            self.btn_fc.setToolTip("Disabled: PLC telemetry stream is dead.")
        elif not air_ok:
            self.btn_fc.setEnabled(False)
            self.btn_fc.setToolTip("Disabled: Facility air pressure is lost. Actuator cannot be moved.")
        elif fc_lock:
            self.btn_fc.setEnabled(False)
            self.btn_fc.setToolTip(f"Disabled: Faraday Cup is locked {fc_state_string} by an active PLC sequence.")
        elif fc_auto_locked:
            self.btn_fc.setEnabled(False)
            self.btn_fc.setToolTip(f"Disabled: Locked by automated sequencer.")
        else:
            self.btn_fc.setEnabled(True)
            self.btn_fc.setToolTip("Click to physically insert or retract the Faraday Cup into the beam path.")

        self.btn_fc.blockSignals(False)

        # --- 2. SMU Telemetry ---
        smu_comms_fail = bool(data.get("ion_beam.beamline.faraday.smu.stat_comms_fail", True))

        # SCENARIO A: Total Comms Failure
        if smu_comms_fail or master_comms_lost:
            self.lbl_bias_status.setText("STATUS: SMU OFFLINE")
            self.lbl_bias_status.setStyleSheet(COLOR_FAULT)
            self.lbl_bias_rb.setText("RB: --- V")

            self.lbl_digital_current.setText("NO MEASUREMENT")
            self.lbl_digital_current.setStyleSheet(
                "QLabel { font-family: monospace; font-size: 18pt; font-weight: bold; color: #F44336; }")
            self.lbl_speedo_text.setText("NO MEASUREMENT")
            self.speedo.update_gauge(0.0, 1e-6)

            offline_msg = "Disabled: SMU Microservice or PLC telemetry is offline."
            self.lbl_digital_current.setToolTip(offline_msg)
            self.lbl_speedo_text.setToolTip(offline_msg)
            self.speedo.setToolTip(offline_msg)

            self.btn_enable_bias.setEnabled(False)
            self.btn_enable_bias.setToolTip(offline_msg)
            self.sp_bias.setEnabled(False)
            self.sp_bias.setToolTip(offline_msg)
            return

        # SCENARIO B: Safety Relay is De-energized
        if not relay_active:
            self.lbl_bias_status.setText("STATUS: SAFETY LOCK")
            self.lbl_bias_status.setStyleSheet(COLOR_INACTIVE)
            self.lbl_bias_rb.setText("RB: 0.0 V")

            self.lbl_digital_current.setText("NO MEASUREMENT")
            self.lbl_digital_current.setStyleSheet(
                "QLabel { font-family: monospace; font-size: 18pt; font-weight: bold; color: #FF9800; }")
            self.lbl_speedo_text.setText("NO MEASUREMENT")
            self.speedo.update_gauge(0.0, self.scale_map.get(self.cb_gauge_scale.currentText(), 1e-6))

            lockout_msg = "Measurement invalid: Master Safety Relay is OPEN."
            self.lbl_digital_current.setToolTip(lockout_msg)
            self.lbl_speedo_text.setToolTip(lockout_msg)
            self.speedo.setToolTip(lockout_msg)

            self.btn_enable_bias.setEnabled(False)
            self.btn_enable_bias.setChecked(False)
            self.btn_enable_bias.setStyleSheet(COLOR_BUTTON_STANDARD)
            self.btn_enable_bias.setToolTip("Disabled: Bias requires Safety Relay to be closed.")

            self.sp_bias.setEnabled(False)
            self.sp_bias.setToolTip("Disabled: Bias requires Safety Relay to be closed.")
            return

        # SCENARIO C: Online and Safe (Evaluate Arbitration Lockout)
        if smu_auto_locked:
            self.btn_enable_bias.setEnabled(False)
            self.btn_enable_bias.setToolTip("Disabled: Locked by automated sequencer.")
            self.sp_bias.setEnabled(False)
            self.sp_bias.setToolTip("Disabled: Locked by automated sequencer.")
        else:
            self.btn_enable_bias.setEnabled(True)
            self.btn_enable_bias.setToolTip("Click to enable or disable the SMU bias voltage output.")
            self.sp_bias.setEnabled(True)
            self.sp_bias.setToolTip("Enter the target bias voltage (-200V to +200V).")

        live_msg = "Live beam current measured by the Source-Measure Unit."
        self.lbl_digital_current.setToolTip(live_msg)
        self.lbl_speedo_text.setToolTip(live_msg)
        self.speedo.setToolTip(live_msg)

        self.lbl_digital_current.setStyleSheet(
            "QLabel { font-family: monospace; font-size: 32pt; font-weight: bold; color: #4CAF50; }")

        smu_en = bool(data.get("ion_beam.beamline.faraday.smu.stat_enabled", False))
        if smu_en:
            self.lbl_bias_status.setText("STATUS: BIAS LIVE")
            self.lbl_bias_status.setStyleSheet(COLOR_WARNING)

            self.btn_enable_bias.blockSignals(True)
            self.btn_enable_bias.setChecked(True)
            self.btn_enable_bias.setStyleSheet(COLOR_OK)
            self.btn_enable_bias.blockSignals(False)
        else:
            self.lbl_bias_status.setText("STATUS: BIAS DISABLED")
            self.lbl_bias_status.setStyleSheet(COLOR_OK)

            self.btn_enable_bias.blockSignals(True)
            self.btn_enable_bias.setChecked(False)
            self.btn_enable_bias.setStyleSheet(COLOR_BUTTON_STANDARD)
            self.btn_enable_bias.blockSignals(False)

        v_rb = data.get("ion_beam.beamline.faraday.smu.rb_voltage")
        i_rb = data.get("ion_beam.beamline.faraday.smu.rb_current")
        v_sp = data.get("ion_beam.beamline.faraday.smu.sp_actual_voltage")

        if v_rb is not None: self.lbl_bias_rb.setText(f"RB: {v_rb:.1f} V")
        if v_sp is not None and not self.sp_bias.hasFocus():
            self.sp_bias.blockSignals(True)
            self.sp_bias.setValue(float(v_sp))
            self.sp_bias.blockSignals(False)

        if i_rb is not None:
            formatted_i = self._format_current(i_rb)
            self.lbl_digital_current.setText(formatted_i)
            self.lbl_speedo_text.setText(formatted_i)

            scale_max = self.scale_map.get(self.cb_gauge_scale.currentText(), 1e-6)
            self.speedo.update_gauge(i_rb, scale_max)