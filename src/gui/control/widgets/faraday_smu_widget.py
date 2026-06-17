import time
import math
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QPushButton, QGroupBox, QDoubleSpinBox, QComboBox, QStackedWidget
)
from PyQt6.QtCore import Qt, QRectF, QPointF
from PyQt6.QtGui import QPainter, QPen, QColor, QPolygonF, QFont

from src.core.theme import (
    COLOR_OK, COLOR_FAULT, COLOR_INACTIVE, COLOR_WARNING, COLOR_BUTTON_STANDARD
)


class SpeedoWidget(QWidget):
    """Custom QPainter widget to draw an analog gauge for beam current."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(250, 150)
        self.current_val = 0.0
        self.max_val = 1e-6  # Default 1uA scale

    def update_gauge(self, value, scale_max):
        self.current_val = abs(value)
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

        # Draw background arc
        rect = QRectF(center_x - radius, center_y - radius, radius * 2, radius * 2)
        pen_bg = QPen(QColor("#333333"), 8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        painter.setPen(pen_bg)
        painter.drawArc(rect, 180 * 16, -180 * 16)  # Start at 180 deg, span -180 deg

        # Draw active value arc
        clamped_val = min(self.current_val, self.max_val)
        ratio = clamped_val / self.max_val if self.max_val > 0 else 0
        span_angle = int(-180 * ratio * 16)

        # Color transitions to warning orange if nearing scale max
        arc_color = QColor("#4CAF50") if ratio < 0.85 else QColor("#FF9800")
        pen_fg = QPen(arc_color, 8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        painter.setPen(pen_fg)
        painter.drawArc(rect, 180 * 16, span_angle)

        # Draw Needle
        angle_rad = math.radians(180 - (180 * ratio))
        needle_length = radius - 10
        end_x = center_x + needle_length * math.cos(angle_rad)
        end_y = center_y - needle_length * math.sin(angle_rad)

        painter.setPen(QPen(QColor("#F44336"), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(QPointF(center_x, center_y), QPointF(end_x, end_y))

        # Draw base pivot
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
        self.btn_fc.clicked.connect(
            lambda *args, b=self.btn_fc: self._dispatch_plc(
                "ion_beam.beamline_diagnostics.cmd_insert_fc", b.isChecked()
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
        layout.addWidget(self.lbl_bias_status, 0, 0, 1, 2)

        layout.addWidget(QLabel("<b>Bias Setpoint:</b>"), 1, 0)
        self.sp_bias = QDoubleSpinBox()
        self.sp_bias.setRange(-200.0, 200.0)
        self.sp_bias.setSuffix(" V")
        self.sp_bias.setDecimals(1)
        self.sp_bias.setKeyboardTracking(False)
        self.sp_bias.editingFinished.connect(
            lambda: self._dispatch_smu("ion_beam.beamline.faraday.smu.sp_requested_voltage", self.sp_bias.value())
        )
        layout.addWidget(self.sp_bias, 1, 1)

        self.lbl_bias_rb = QLabel("RB: --- V")
        self.lbl_bias_rb.setMinimumWidth(80)
        layout.addWidget(self.lbl_bias_rb, 1, 2)

        self.btn_enable_bias = QPushButton("ENABLE BIAS")
        self.btn_enable_bias.setCheckable(True)
        self.btn_enable_bias.setStyleSheet(COLOR_BUTTON_STANDARD)
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

        # View Toggle Row
        ctrl_row = QHBoxLayout()
        ctrl_row.addWidget(QLabel("<b>View Mode:</b>"))
        self.cb_view_mode = QComboBox()
        self.cb_view_mode.addItems(["Digital Readout", "Analog Gauge"])
        self.cb_view_mode.currentIndexChanged.connect(self._toggle_view_mode)
        ctrl_row.addWidget(self.cb_view_mode)

        ctrl_row.addWidget(QLabel("<b>Gauge Scale:</b>"))
        self.cb_gauge_scale = QComboBox()
        # Mapping nice labels to actual float Ampere max limits
        self.scale_map = {"100 nA": 100e-9, "1 µA": 1e-6, "10 µA": 10e-6, "100 µA": 100e-6, "1 mA": 1e-3}
        self.cb_gauge_scale.addItems(list(self.scale_map.keys()))
        self.cb_gauge_scale.setCurrentText("100 µA")
        ctrl_row.addWidget(self.cb_gauge_scale)
        ctrl_row.addStretch()
        layout.addLayout(ctrl_row)

        # Stacked Widget for Displays
        self.display_stack = QStackedWidget()

        # Page 0: Digital Display
        self.page_digital = QWidget()
        dig_layout = QVBoxLayout(self.page_digital)
        self.lbl_digital_current = QLabel("--- µA")
        self.lbl_digital_current.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_digital_current.setStyleSheet(
            "font-family: monospace; font-size: 32pt; font-weight: bold; color: #4CAF50;")
        dig_layout.addWidget(self.lbl_digital_current)
        self.display_stack.addWidget(self.page_digital)

        # Page 1: Analog Gauge
        self.page_analog = QWidget()
        ana_layout = QVBoxLayout(self.page_analog)
        self.speedo = SpeedoWidget()
        self.lbl_speedo_text = QLabel("--- µA")
        self.lbl_speedo_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_speedo_text.setStyleSheet("font-family: monospace; font-size: 14pt; font-weight: bold;")
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

        # --- 1. Actuator Update ---
        fc_in = data.get("ion_beam.beamline_diagnostics.stat_fc_inserted")
        fc_out = data.get("ion_beam.beamline_diagnostics.stat_fc_retracted")
        fc_lock = bool(data.get("ion_beam.beamline_diagnostics.stat_lockout", False))
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
            self.btn_fc.setToolTip("Disabled: PLC offline.")
        elif not air_ok:
            self.btn_fc.setEnabled(False)
            self.btn_fc.setToolTip("Disabled: Facility air pressure lost.")
        elif fc_lock:
            self.btn_fc.setEnabled(False)
            self.btn_fc.setToolTip(f"Disabled: Faraday Cup is locked {fc_state_string} by an active PLC sequence.")
        else:
            self.btn_fc.setEnabled(True)
            self.btn_fc.setToolTip("Click to toggle the Faraday Cup position in the beamline.")

        self.btn_fc.blockSignals(False)

        # --- 2. SMU Telemetry ---
        smu_comms_fail = bool(data.get("ion_beam.beamline.faraday.smu.stat_comms_fail", True))

        if smu_comms_fail:
            self.lbl_bias_status.setText("STATUS: SMU OFFLINE")
            self.lbl_bias_status.setStyleSheet(COLOR_FAULT)
            self.lbl_bias_rb.setText("RB: --- V")
            self.lbl_digital_current.setText("OFFLINE")
            self.lbl_digital_current.setStyleSheet("color: #F44336;")
            self.lbl_speedo_text.setText("OFFLINE")
            self.speedo.update_gauge(0.0, 1e-6)

            self.btn_enable_bias.setEnabled(False)
            self.sp_bias.setEnabled(False)
            return

        self.btn_enable_bias.setEnabled(True)
        self.sp_bias.setEnabled(True)
        self.lbl_digital_current.setStyleSheet("color: #4CAF50;")  # Restore green text

        smu_en = bool(data.get("ion_beam.beamline.faraday.smu.stat_enabled", False))
        if smu_en:
            self.lbl_bias_status.setText("STATUS: BIAS LIVE")
            self.lbl_bias_status.setStyleSheet(COLOR_WARNING)
            self.btn_enable_bias.setChecked(True)
            self.btn_enable_bias.setStyleSheet(COLOR_OK)
        else:
            self.lbl_bias_status.setText("STATUS: BIAS DISABLED")
            self.lbl_bias_status.setStyleSheet(COLOR_OK)
            self.btn_enable_bias.setChecked(False)
            self.btn_enable_bias.setStyleSheet(COLOR_BUTTON_STANDARD)

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