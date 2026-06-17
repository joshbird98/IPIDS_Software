import time
from collections import deque
import pyqtgraph as pg
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton, QGroupBox, QDoubleSpinBox)
from PyQt6.QtCore import Qt

from src.core.theme import (
    COLOR_OK,
    COLOR_FAULT,
    COLOR_INACTIVE,
    COLOR_WARNING,
    COLOR_BUTTON_STANDARD
)

# --- Ion Source Subsystem Component ---

class IonSourceWidget(QWidget):
    def __init__(self, cmd_thread, fault_engine):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.fault_engine = fault_engine

        self.history_len = 600
        self.time_data = deque(maxlen=self.history_len)
        self.tgt_cur_data = deque(maxlen=self.history_len)
        self.thm_cur_data = deque(maxlen=self.history_len)
        self.start_time = time.time()

        self.main_layout = QVBoxLayout(self)

        self._init_alarms_ui()
        self._init_safety_ui()
        self._init_plot_ui()
        self._init_psu_ui()

    def _dispatch_command(self, tag: str, value):
        target = "spellman" if "einzel" in tag else "plc"
        self.cmd_thread.send_command(target, tag, value)

    def _update_dual_axis(self):
        plot_view = self.plot_widget.getPlotItem().getViewBox()
        self.view_right.setGeometry(plot_view.sceneBoundingRect())
        self.view_right.linkedViewChanged(plot_view, self.view_right.XAxis)

    def _init_alarms_ui(self):
        self.lbl_alarm_banner = QLabel("")
        self.lbl_alarm_banner.setStyleSheet(
            "background-color: #F44336; color: yellow; font-size: 11pt; font-weight: bold; padding: 6px;")
        self.lbl_alarm_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_alarm_banner.hide()
        self.main_layout.addWidget(self.lbl_alarm_banner)

    def _init_safety_ui(self):
        safety_group = QGroupBox("Master Safety && Sequence Control")
        sl = QGridLayout()

        self.lbl_comms = QLabel("PLC COMMS: WAITING FOR DATA...")
        self.lbl_comms.setStyleSheet(COLOR_WARNING)
        self.lbl_safety_state = QLabel("SAFETY RELAY: WAITING FOR DATA...")
        self.lbl_safety_state.setStyleSheet(COLOR_WARNING)
        self.lbl_shutdown_state = QLabel("SHUTDOWN SEQ: WAITING FOR DATA...")
        self.lbl_shutdown_state.setStyleSheet(COLOR_WARNING)

        self.btn_enable_safety = QPushButton("ENABLE SAFETY RELAY")
        self.btn_enable_safety.setCheckable(True)
        self.btn_enable_safety.setStyleSheet("""
            QPushButton { background-color: #2196F3; color: white; font-weight: bold; border-radius: 4px; padding: 6px; }
            QPushButton:checked { background-color: #F44336; color: white; }
            QPushButton:disabled { background-color: #757575; color: #B0B0B0; font-weight: bold; }
        """)
        self.btn_enable_safety.clicked.connect(
            lambda *args, b=self.btn_enable_safety: self._dispatch_command("ion_beam.system.cmd_enable_safety",
                                                                           b.isChecked()))

        self.btn_reset_faults = QPushButton("RESET FAULTS")
        self.btn_reset_faults.setStyleSheet(COLOR_BUTTON_STANDARD)
        self.btn_reset_faults.clicked.connect(
            lambda *args: self._dispatch_command("ion_beam.system.cmd_fault_reset", True))

        self.btn_shutdown = QPushButton("INITIATE SHUTDOWN")
        self.btn_shutdown.setStyleSheet("""
            QPushButton { background-color: #F44336; color: white; font-weight: bold; border-radius: 4px; padding: 6px; }
            QPushButton:disabled { background-color: #757575; color: #E0E0E0; font-weight: bold; }
        """)
        self.btn_shutdown.clicked.connect(
            lambda *args: self._dispatch_command("ion_beam.system.cmd_source_shutdown", True))

        self.btn_cancel_shutdown = QPushButton("ABORT SHUTDOWN")
        self.btn_cancel_shutdown.setStyleSheet("""
            QPushButton { background-color: #FF9800; color: black; font-weight: bold; border-radius: 4px; padding: 6px; }
            QPushButton:disabled { background-color: #757575; color: #E0E0E0; font-weight: bold; }
        """)
        self.btn_cancel_shutdown.clicked.connect(
            lambda *args: self._dispatch_command("ion_beam.system.cmd_cancel_shutdown", True))

        sl.addWidget(self.lbl_comms, 0, 0)
        sl.addWidget(self.lbl_safety_state, 0, 1)
        sl.addWidget(self.lbl_shutdown_state, 0, 2)
        sl.addWidget(self.btn_enable_safety, 1, 0)
        sl.addWidget(self.btn_reset_faults, 1, 1)
        sl.addWidget(self.btn_shutdown, 1, 2)
        sl.addWidget(self.btn_cancel_shutdown, 1, 3)
        safety_group.setLayout(sl)
        self.main_layout.addWidget(safety_group)

    def _init_plot_ui(self):
        plot_group = QGroupBox("Emission Stability")
        pl_layout = QVBoxLayout()
        pg.setConfigOption('background', 'w')
        pg.setConfigOption('foreground', 'k')

        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setLabel('bottom', 'Time', units='s')
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)

        plot_item = self.plot_widget.getPlotItem()
        plot_item.setLabel('left', 'Target Current', units='mA', color='r')
        plot_item.getAxis('left').setPen('r')
        self.tgt_curve = plot_item.plot(pen=pg.mkPen(color='r', width=2), name="Target")

        self.view_right = pg.ViewBox()
        plot_item.scene().addItem(self.view_right)
        self.axis_right = pg.AxisItem('right')
        plot_item.layout.addItem(self.axis_right, 2, 3)
        self.axis_right.linkToView(self.view_right)
        self.axis_right.setLabel('Thermionic Current', units='mA', color='b')
        self.axis_right.setPen('b')
        self.view_right.setXLink(plot_item)

        self.thm_curve = pg.PlotDataItem(pen=pg.mkPen(color='b', width=2), name="Thermionic")
        self.view_right.addItem(self.thm_curve)

        plot_item.getViewBox().sigResized.connect(self._update_dual_axis)
        pl_layout.addWidget(self.plot_widget)
        plot_group.setLayout(pl_layout)
        self.main_layout.addWidget(plot_group)

    def _dispatch_spellman_setpoints(self, base_tag: str, voltage: float):
        """Dispatches voltage and autonomously sets a safe 50uA current limit to unclamp the CC loop."""
        self._dispatch_command(f"{base_tag}.sp_requested_voltage", voltage)
        self._dispatch_command(f"{base_tag}.sp_requested_current", 50.0)

    def _init_psu_ui(self):
        psu_group = QGroupBox("Source Power Supplies")
        self.psu_layout = QGridLayout()
        self.psu_controls = {}

        # Updated to the new ISA-95 path: ion_beam.source.einzel
        self._build_psu_row(0, "Source Einzel", "ion_beam.source.einzel", "kV", "µA", 0.0, 30.0,
                            is_voltage=True, decimals=3)

        self._build_psu_row(1, "Extraction", "ion_beam.source.extraction", "kV", "mA", 0.0, 20.0, is_voltage=True, decimals=3)
        self._build_psu_row(2, "Target", "ion_beam.source.target", "kV", "mA", 0.0, 10.0, is_voltage=True, decimals=3)
        self._build_psu_row(3, "Filament", "ion_beam.source.filament", "A", "V", 0.0, 38.0, is_voltage=False, decimals=3)
        self._build_psu_row(4, "Thermionic", "ion_beam.source.thermionic", "mA", "V", 0.0, 1000.0, is_voltage=False, decimals=2)
        self._build_cs_row(5)

        psu_group.setLayout(self.psu_layout)
        self.main_layout.addWidget(psu_group)
        self.main_layout.addStretch()

    def _build_psu_row(self, row, name, base_tag, pri_unit, sec_unit, min_v, max_v, is_voltage, decimals):
        lbl_name = QLabel(f"<b>{name}</b>")

        if name in ["Extraction", "Target", "Filament", "Thermionic"]:
            lbl_rb = QLabel(f"RB: --- {pri_unit} | --- {sec_unit} | --- W")
            lbl_rb.setMinimumWidth(250)
        else:
            lbl_rb = QLabel(f"RB: --- {pri_unit} | --- {sec_unit}")
            lbl_rb.setMinimumWidth(180)

        lbl_badge = QLabel("")
        lbl_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_badge.setFixedWidth(100)
        lbl_badge.hide()

        btn_enable = QPushButton("ENABLE")
        btn_enable.setCheckable(True)
        btn_enable.setStyleSheet(COLOR_BUTTON_STANDARD)
        btn_enable.clicked.connect(
            lambda *args, b=btn_enable, t=base_tag: self._dispatch_command(f"{t}.cmd_enable", b.isChecked()))

        sp_box = QDoubleSpinBox()
        sp_box.setRange(min_v, max_v)
        sp_box.setSuffix(f" {pri_unit}")
        sp_box.setDecimals(decimals)

        if decimals >= 3:
            sp_box.setSingleStep(0.001)
        elif decimals == 2:
            sp_box.setSingleStep(0.01)
        else:
            sp_box.setSingleStep(0.1)

        sp_box.setKeyboardTracking(False)

        # Updated to check for "einzel"
        if "einzel" in base_tag:
            sp_box.editingFinished.connect(
                lambda t=base_tag, b=sp_box: self._dispatch_spellman_setpoints(t, b.value()))
        else:
            sp_box.editingFinished.connect(
                lambda t=base_tag, b=sp_box, iv=is_voltage: self._dispatch_command(
                    f"{t}.sp_requested_{'voltage' if iv else 'current'}", b.value()))

        self.psu_layout.addWidget(lbl_name, row, 0)
        self.psu_layout.addWidget(lbl_rb, row, 1)
        self.psu_layout.addWidget(sp_box, row, 2)
        self.psu_layout.addWidget(lbl_badge, row, 3)
        self.psu_layout.addWidget(btn_enable, row, 4)

        self.psu_controls[name] = {
            "lbl_rb": lbl_rb, "sp_box": sp_box, "btn_enable": btn_enable,
            "lbl_badge": lbl_badge, "base_tag": base_tag,
            "pri_unit": pri_unit, "sec_unit": sec_unit, "is_voltage": is_voltage,
            "decimals": decimals
        }

        if name == "Thermionic":
            btn_auto = QPushButton("AUTO EMISSION")
            btn_auto.setCheckable(True)
            btn_auto.setStyleSheet(COLOR_BUTTON_STANDARD)
            btn_auto.clicked.connect(
                lambda *args, b=btn_auto, t=base_tag: self._dispatch_command(f"{t}.cmd_auto_emission", b.isChecked()))
            self.psu_layout.addWidget(btn_auto, row, 5)
            self.psu_controls[name]["btn_auto"] = btn_auto

    def _build_cs_row(self, row):
        lbl_name = QLabel("<b>Cesium Oven</b>")
        lbl_rb = QLabel("RB: --- °C")
        lbl_rb.setMinimumWidth(180)

        lbl_badge = QLabel("")
        lbl_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_badge.setFixedWidth(100)
        lbl_badge.hide()

        btn_enable = QPushButton("HEATER ON")
        btn_enable.setCheckable(True)
        btn_enable.setStyleSheet(COLOR_BUTTON_STANDARD)
        btn_enable.clicked.connect(
            lambda *args, b=btn_enable: self._dispatch_command("ion_beam.source.cesium.cmd_ctrl_mode",
                                                               1 if b.isChecked() else 0))

        btn_cool = QPushButton("FORCED COOLING")
        btn_cool.setCheckable(True)
        btn_cool.setStyleSheet(COLOR_BUTTON_STANDARD)
        btn_cool.clicked.connect(
            lambda *args, b=btn_cool: self._dispatch_command("ion_beam.source.cesium.cmd_force_cooling", b.isChecked()))

        sp_box = QDoubleSpinBox()
        sp_box.setRange(0.0, 200.0)
        sp_box.setSuffix(" °C")
        sp_box.editingFinished.connect(
            lambda: self._dispatch_command("ion_beam.source.cesium.sp_requested_temp", sp_box.value()))

        self.psu_layout.addWidget(lbl_name, row, 0)
        self.psu_layout.addWidget(lbl_rb, row, 1)
        self.psu_layout.addWidget(sp_box, row, 2)
        self.psu_layout.addWidget(lbl_badge, row, 3)
        self.psu_layout.addWidget(btn_enable, row, 4)
        self.psu_layout.addWidget(btn_cool, row, 5)

        self.psu_controls["Cesium"] = {
            "lbl_rb": lbl_rb, "sp_box": sp_box, "btn_enable": btn_enable,
            "btn_cool": btn_cool, "lbl_badge": lbl_badge,
            "base_tag": "ion_beam.source.cesium", "is_voltage": False
        }

    def update_telemetry(self, data: dict):
        try:
            master_comms_lost = self._update_comms_status(data)
            relay_active, remote_lost = self._update_alarms_and_safety_state(data, master_comms_lost)

            auto_sdown = bool(data.get("ion_beam.system.src_auto_sdown", 0.0))
            active_step = max(int(data.get("ion_beam.system.remote_shutdown_step", 0.0)),
                              int(data.get("ion_beam.system.pc_shutdown_step", 0.0)))

            spellman_state = data.get("manager.services", {}).get("service_spellman_mpd", "OFFLINE")
            spellman_comms_lost = (spellman_state != "ONLINE")

            self._update_safety_controls(data, relay_active, active_step, master_comms_lost)
            self._update_shutdown_labels(active_step, auto_sdown, master_comms_lost)
            self._update_plot(data)
            self._update_power_supplies(data, relay_active, remote_lost, auto_sdown, active_step, master_comms_lost,
                                        spellman_comms_lost)

        except Exception as e:
            print(f"[UI Parser Error] Ion Source Runtime exception: {e}")

    def _update_comms_status(self, data: dict) -> bool:
        sys_connected = bool(data.get("system.connected", False))
        pc_plc_lost = bool(data.get("ion_beam.system.pc_plc_comms_lost", False))
        master_comms_lost = not sys_connected or pc_plc_lost

        if master_comms_lost:
            self.lbl_comms.setText("PLC COMMS: OFFLINE")
            self.lbl_comms.setStyleSheet(COLOR_FAULT)
        else:
            self.lbl_comms.setText("PLC COMMS: OK")
            self.lbl_comms.setStyleSheet(COLOR_OK)
        return master_comms_lost

    def _update_alarms_and_safety_state(self, data: dict, master_comms_lost: bool) -> tuple[bool, bool]:
        active_alarms = []
        first_out = int(data.get("ion_beam.settings.first_out_code", 0))
        if first_out == 10:
            active_alarms.append("SAFETY TRIP: PROBABLE VACUUM SPIKE")
        elif first_out == 11:
            active_alarms.append("SAFETY TRIP: PHYSICAL INTERLOCK OR POWER SAG")

        w2_raw = data.get("ion_beam.faults.word_2_source")
        if w2_raw:
            w2_faults = self.fault_engine.get_active_faults("UDT_Fault_Word_2_Source", int(w2_raw))
            for f in w2_faults:
                if f["severity"] == "CRITICAL": active_alarms.append(f["name"].upper().replace("_", " "))

        # --- OFFLINE / STALE ALARM BANNER OVERRIDE ---
        if master_comms_lost:
            self.lbl_alarm_banner.setText("⚠️ TELEMETRY OFFLINE: FAULT STATUS UNKNOWN ⚠️")
            self.lbl_alarm_banner.setStyleSheet(
                "background-color: #9E9E9E; color: black; font-size: 11pt; font-weight: bold; padding: 6px;")
            self.lbl_alarm_banner.show()
        elif active_alarms:
            self.lbl_alarm_banner.setText(" | ".join(active_alarms))
            self.lbl_alarm_banner.setStyleSheet(
                "background-color: #F44336; color: yellow; font-size: 11pt; font-weight: bold; padding: 6px;")
            self.lbl_alarm_banner.show()
        else:
            self.lbl_alarm_banner.hide()

        relay_active = bool(data.get("ion_beam.facilities.safety_relay_active", False))
        w0_raw = data.get("ion_beam.faults.word_0_system")
        w0_faults = self.fault_engine.get_active_faults("UDT_Fault_Word_0_System", int(w0_raw)) if w0_raw else []

        sr_tripped = any(f["name"] == "Safety_Relay_Tripped" for f in w0_faults)
        sr_refused = any(f["name"] == "Safety_Relay_Refused" for f in w0_faults)
        remote_lost = any(f["name"] == "Remote_IO_Lost" for f in w0_faults) or bool(
            data.get("ion_beam.system.remote_io_lost", 0.0))

        if master_comms_lost:
            self.lbl_safety_state.setText("SAFETY: UNKNOWN")
            self.lbl_safety_state.setStyleSheet(COLOR_INACTIVE)
        elif relay_active:
            self.lbl_safety_state.setText("SAFETY: LIVE")
            self.lbl_safety_state.setStyleSheet(COLOR_WARNING)
        elif sr_tripped or sr_refused:
            self.lbl_safety_state.setText("SAFETY: TRIPPED/REFUSED")
            self.lbl_safety_state.setStyleSheet(COLOR_FAULT)
        elif remote_lost:
            self.lbl_safety_state.setText("SAFETY: NO REMOTE IO")
            self.lbl_safety_state.setStyleSheet(COLOR_FAULT)
        else:
            self.lbl_safety_state.setText("SAFETY: DE-ENERGIZED")
            self.lbl_safety_state.setStyleSheet(COLOR_OK)

        return relay_active, remote_lost

    def _update_safety_controls(self, data: dict, relay_active: bool, active_step: int, master_comms_lost: bool):
        gv_is_open = bool(data.get("ion_beam.source.chamber.stat_gv_open", False))
        source_is_cold = bool(data.get("ion_beam.source.chamber.stat_gv_close_permissive", False))
        any_psu_active = any(
            bool(data.get(f"{ctrl['base_tag']}.stat_enabled", False)) for ctrl in self.psu_controls.values())

        self.btn_reset_faults.setEnabled(not master_comms_lost)
        self.btn_reset_faults.setToolTip(
            "Disabled: GUI to PLC communications are offline." if master_comms_lost else "Click to clear latched software and hardware faults.")

        # Extract the software command state from telemetry
        cmd_enable_safety = bool(data.get("ion_beam.system.cmd_enable_safety", False))
        timer_elapsed_ms = float(data.get("ion_beam.system.safety_timer_elapsed_ms", 0.0))
        timer_total_ms = float(data.get("ion_beam.system.safety_timer_total_ms", 5000.0))

        # Enforce that the software command must be True to show the countdown
        is_timing_out = cmd_enable_safety and (0 < timer_elapsed_ms < timer_total_ms) and not relay_active

        if is_timing_out and timer_total_ms > 0:
            remaining_sec = (timer_total_ms - timer_elapsed_ms) / 1000.0
            total_sec = timer_total_ms / 1000.0
            fill_ratio = max(0.0, min(1.0, remaining_sec / total_sec))

            sweep_style = f"""
                QPushButton {{
                    background: qlineargradient(x1:1, y1:0, x2:0, y2:0, 
                                                stop:0 #F44336, 
                                                stop:{fill_ratio:.3f} #F44336, 
                                                stop:{min(1.0, fill_ratio + 0.001):.3f} #2196F3);
                    color: white; font-weight: bold; border-radius: 4px; padding: 6px;
                }}
            """
            self.btn_enable_safety.blockSignals(True)
            self.btn_enable_safety.setChecked(True)
            self.btn_enable_safety.blockSignals(False)

            self.btn_enable_safety.setText(f"PRESS PANEL START ({int(remaining_sec) + 1}s)")
            self.btn_enable_safety.setStyleSheet(sweep_style)
            self.btn_enable_safety.setEnabled(True)
            self.btn_enable_safety.setToolTip(f"Awaiting physical panel button press... ({total_sec}s timeout)")

        else:
            self.btn_enable_safety.blockSignals(True)
            self.btn_enable_safety.setChecked(relay_active)
            self.btn_enable_safety.blockSignals(False)

            self.btn_enable_safety.setStyleSheet("""
                QPushButton { background-color: #2196F3; color: white; font-weight: bold; border-radius: 4px; padding: 6px; }
                QPushButton:checked { background-color: #F44336; color: white; }
                QPushButton:disabled { background-color: #757575; color: #B0B0B0; font-weight: bold; }
            """)

            if master_comms_lost:
                self.btn_enable_safety.setEnabled(False)
                self.btn_enable_safety.setToolTip("Disabled: GUI to PLC communications are offline.")
            elif not relay_active:
                if not gv_is_open:
                    self.btn_enable_safety.setEnabled(False)
                    self.btn_enable_safety.setText("ENABLE SAFETY RELAY")
                    self.btn_enable_safety.setToolTip(
                        "Disabled: The Gate Valve must be fully OPEN before the safety circuit can be energized.")
                else:
                    self.btn_enable_safety.setEnabled(True)
                    self.btn_enable_safety.setText("ENABLE SAFETY RELAY")
                    self.btn_enable_safety.setToolTip("Click to energize the Master Safety Relay.")
            else:
                if not source_is_cold:
                    self.btn_enable_safety.setEnabled(False)
                    self.btn_enable_safety.setText("RELAY ENERGIZED (LOCKED)")
                    self.btn_enable_safety.setToolTip(
                        "Disabled: You cannot drop the safety relay while the source is hot or active. Run a shutdown first.")
                else:
                    self.btn_enable_safety.setEnabled(True)
                    self.btn_enable_safety.setText("DISABLE SAFETY RELAY")
                    self.btn_enable_safety.setToolTip("Click to manually de-energize the Master Safety Relay.")

        # Permit shutdown if PSUs are active OR the source thermal mass is hot
        can_shutdown = (active_step == 0) and relay_active and (any_psu_active or not source_is_cold)
        self.btn_shutdown.setEnabled(can_shutdown and not master_comms_lost)

        if master_comms_lost:
            self.btn_shutdown.setToolTip("Disabled: GUI to PLC communications are offline.")
        elif active_step > 0:
            self.btn_shutdown.setToolTip("Disabled: Shutdown sequence already in progress.")
        elif not relay_active:
            self.btn_shutdown.setToolTip("Disabled: Safety Relay is dead. Source is already powered off.")
        elif not any_psu_active and source_is_cold:
            self.btn_shutdown.setToolTip("Disabled: All power supplies are off and the source is already cold.")
        else:
            self.btn_shutdown.setToolTip("Click to initiate a controlled, safe shutdown of the Ion Source.")

        p_step = int(data.get("ion_beam.system.pc_shutdown_step", 0.0))
        self.btn_cancel_shutdown.setEnabled(p_step == 10 and not master_comms_lost)
        if master_comms_lost:
            self.btn_cancel_shutdown.setToolTip("Disabled: GUI to PLC communications are offline.")
        elif p_step == 10:
            self.btn_cancel_shutdown.setToolTip("Click to abort the shutdown and resume normal operations.")
        else:
            self.btn_cancel_shutdown.setToolTip(
                "Disabled: Abort is only permitted during the initial 'Hold Hot' phase.")

    def _update_shutdown_labels(self, active_step: int, auto_sdown: bool, master_comms_lost: bool):
        if master_comms_lost:
            self.lbl_shutdown_state.setText("SHUTDOWN SEQ: UNKNOWN")
            self.lbl_shutdown_state.setStyleSheet(COLOR_INACTIVE)
        elif auto_sdown or active_step > 0:
            if active_step == 1:
                self.lbl_shutdown_state.setText("SHUTDOWN SEQ: GRACE PERIOD (75% EXTR)")
            elif active_step == 2:
                self.lbl_shutdown_state.setText("SHUTDOWN SEQ: CESIUM COOLDOWN")
            elif active_step == 3:
                self.lbl_shutdown_state.setText("SHUTDOWN SEQ: FINAL KILL")
            elif active_step == 10:
                self.lbl_shutdown_state.setText("SHUTDOWN SEQ: HOLD HOT PHASE")
            elif active_step == 11:
                self.lbl_shutdown_state.setText("SHUTDOWN SEQ: FILAMENT RAMP DOWN")
            elif active_step == 99:
                self.lbl_shutdown_state.setText("SHUTDOWN SEQ: LATCHED SAFE (RESET REQ)")
            self.lbl_shutdown_state.setStyleSheet(COLOR_WARNING if active_step in [1, 2, 10, 11] else COLOR_FAULT)
        else:
            self.lbl_shutdown_state.setText("SHUTDOWN SEQ: INACTIVE")
            self.lbl_shutdown_state.setStyleSheet(COLOR_OK)

    def _update_plot(self, data: dict):
        t_now = time.time() - self.start_time
        self.time_data.append(t_now)
        self.tgt_cur_data.append(data.get("ion_beam.source.target.rb_current", 0.0))
        self.thm_cur_data.append(data.get("ion_beam.source.thermionic.rb_current", 0.0))

        self.tgt_curve.setData(list(self.time_data), list(self.tgt_cur_data))
        self.thm_curve.setData(list(self.time_data), list(self.thm_cur_data))

    def _update_power_supplies(self, data: dict, relay_active: bool, remote_lost: bool, auto_sdown: bool,
                               active_step: int, master_comms_lost: bool, spellman_comms_lost: bool):
        for name, ctrl in self.psu_controls.items():
            base_tag = ctrl["base_tag"]
            pri_unit = ctrl.get("pri_unit", "")
            sec_unit = ctrl.get("sec_unit", "")
            sp_box = ctrl["sp_box"]
            lbl_badge = ctrl["lbl_badge"]
            is_spellman = "einzel" in base_tag

            comp_lock_reason = ""
            if is_spellman and spellman_comms_lost:
                comp_lock_reason = "Spellman microservice is offline."
            elif master_comms_lost:
                comp_lock_reason = "Safety Relay state is unknown (PLC offline)."
            elif not relay_active:
                comp_lock_reason = "Safety Relay is De-Energized."
            elif remote_lost:
                comp_lock_reason = "Remote I/O communications lost."
            elif auto_sdown or active_step > 0:
                comp_lock_reason = "Automated shutdown sequence in progress."

            comp_lockout = bool(comp_lock_reason)

            # A. Readbacks (PV) with Dynamic Power Append
            is_stale = master_comms_lost or (is_spellman and spellman_comms_lost)
            stale_str = " [?]" if is_stale else ""

            if name == "Cesium":
                rb_val = data.get(f"{base_tag}.rb_temp")
                est_val = data.get(f"{base_tag}.estimated_temp")
                duty_cycle = data.get(f"{base_tag}.out_cv_heating")

                if duty_cycle is not None:
                    # If hardware is dead, fall back to the PLC's thermal estimate
                    if not relay_active or remote_lost:
                        temp_str = f"~{est_val:.1f}" if est_val is not None else "---"
                    else:
                        temp_str = f"{rb_val:.1f}" if rb_val is not None else "---"

                    ctrl["lbl_rb"].setText(f"RB: {temp_str} °C (H: {duty_cycle:.0f}%){stale_str}")
            else:
                rb_v = data.get(f"{base_tag}.rb_voltage")
                rb_i = data.get(f"{base_tag}.rb_current")
                rb_p = data.get(f"{base_tag}.rb_power")

                # Match primary readback to the SpinBox precision
                pri_decimals = ctrl.get("decimals", 2)

                # Cap the secondary µA readout at 1 DP to match the hardware serial limits
                sec_decimals = 1 if sec_unit == "µA" else 2

                # Append wattage conditionally if present
                p_str = f" | {rb_p:.1f} W" if rb_p is not None else ""

                if ctrl["is_voltage"]:
                    v_str = f"{rb_v:.{pri_decimals}f}" if rb_v is not None else "---"
                    i_str = f"{rb_i:.{sec_decimals}f}" if rb_i is not None else "---"
                    ctrl["lbl_rb"].setText(f"RB: {v_str} {pri_unit} | {i_str} {sec_unit}{p_str}{stale_str}")
                else:
                    i_str = f"{rb_i:.{pri_decimals}f}" if rb_i is not None else "---"
                    v_str = f"{rb_v:.{sec_decimals}f}" if rb_v is not None else "---"
                    ctrl["lbl_rb"].setText(f"RB: {i_str} {pri_unit} | {v_str} {sec_unit}{p_str}{stale_str}")

            # B. Active Setpoint Synchronization
            sp_tag = "sp_actual_temp" if name == "Cesium" else (
                "sp_actual_voltage" if ctrl["is_voltage"] else "sp_actual_current")
            active_sp = data.get(f"{base_tag}.{sp_tag}")
            if active_sp is not None and not sp_box.hasFocus():
                sp_box.blockSignals(True)
                sp_box.setValue(float(active_sp))
                sp_box.blockSignals(False)

            # C. Hardware State & Component Logic Sync
            ctrl_mode = data.get(f"{base_tag}.ctrl_mode", 0)
            hw_lockout = bool(data.get(f"{base_tag}.stat_lockout", 0.0))

            if name == "Cesium":
                stat_en = data.get(f"{base_tag}.stat_enabled")
                if stat_en is not None:
                    ctrl["btn_enable"].setChecked(bool(stat_en))
                    ctrl["btn_enable"].setStyleSheet(COLOR_OK if stat_en else COLOR_BUTTON_STANDARD)

                ctrl["btn_enable"].setEnabled(not comp_lockout)
                ctrl["btn_enable"].setToolTip(
                    f"Disabled: {comp_lock_reason}" if comp_lockout else "Click to toggle Cesium Heater PID control.")

                stat_cool = bool(data.get(f"{base_tag}.stat_cooling_active", False))
                stat_wait = bool(data.get(f"{base_tag}.stat_cooling_wait", False))
                ctrl["btn_cool"].setEnabled(not master_comms_lost)
                ctrl["btn_cool"].setToolTip(
                    "Disabled: GUI to PLC communications are offline." if master_comms_lost else "Click to manually force the cooling air solenoid open.")

                if stat_cool:
                    ctrl["btn_cool"].setChecked(True)
                    ctrl["btn_cool"].setText("COOLING ACTIVE")
                    ctrl["btn_cool"].setStyleSheet("background-color: #03A9F4; color: white;")
                elif stat_wait:
                    ctrl["btn_cool"].setChecked(True)
                    ctrl["btn_cool"].setText("COMPRESSOR RESTING")
                    ctrl["btn_cool"].setStyleSheet("background-color: #FF9800; color: black; font-weight: bold;")
                else:
                    ctrl["btn_cool"].setChecked(False)
                    ctrl["btn_cool"].setText("FORCED COOLING")
                    ctrl["btn_cool"].setStyleSheet(COLOR_BUTTON_STANDARD)

            else:
                if "btn_enable" in ctrl:
                    stat_en = data.get(f"{base_tag}.stat_enabled")
                    stat_disarming = bool(data.get(f"{base_tag}.stat_disarming", False))

                    if name == "Filament" and stat_disarming:
                        ctrl["btn_enable"].blockSignals(True)
                        ctrl["btn_enable"].setChecked(True)
                        ctrl["btn_enable"].blockSignals(False)

                        ctrl["btn_enable"].setText("DISARMING...")
                        ctrl["btn_enable"].setStyleSheet(
                            "background-color: #FF9800; color: black; font-weight: bold; border-radius: 4px;")
                        ctrl["btn_enable"].setEnabled(False)
                        ctrl["btn_enable"].setToolTip(
                            "Contactor remains closed while safe step-down ramp drops current to 0A.")
                    else:
                        if name == "Filament":
                            ctrl["btn_enable"].setText("ENABLE")

                        if stat_en is not None:
                            ctrl["btn_enable"].setChecked(bool(stat_en))
                            ctrl["btn_enable"].setStyleSheet(COLOR_OK if stat_en else COLOR_BUTTON_STANDARD)

                        ctrl["btn_enable"].setEnabled(not comp_lockout)
                        ctrl["btn_enable"].setToolTip(
                            f"Disabled: {comp_lock_reason}" if comp_lockout else f"Click to toggle {name} power output.")

                if name == "Thermionic" and "btn_auto" in ctrl:
                    auto_em_stat = data.get(f"{base_tag}.stat_auto_emission")

                    if auto_em_stat is not None:
                        is_auto = bool(auto_em_stat)

                        # Block signals to prevent the UI update from accidentally triggering a ZMQ command
                        ctrl["btn_auto"].blockSignals(True)
                        ctrl["btn_auto"].setChecked(is_auto)

                        if is_auto:
                            ctrl["btn_auto"].setText("AUTO EMISSION: ON")
                            ctrl["btn_auto"].setStyleSheet(COLOR_OK)
                            tt = f"Disabled: {comp_lock_reason}" if comp_lockout else "Auto-Emission ACTIVE. Click to revert to Manual Filament Control."
                        else:
                            ctrl["btn_auto"].setText("AUTO EMISSION")
                            ctrl["btn_auto"].setStyleSheet(COLOR_BUTTON_STANDARD)
                            tt = f"Disabled: {comp_lock_reason}" if comp_lockout else "Click to engage closed-loop Auto-Emission control (Cascades Filament output)."

                        ctrl["btn_auto"].setToolTip(tt)
                        ctrl["btn_auto"].blockSignals(False)

                    ctrl["btn_auto"].setEnabled(not comp_lockout)
                    if comp_lockout and auto_em_stat is None:
                        ctrl["btn_auto"].setToolTip(f"Disabled: {comp_lock_reason}")

            # D. Control Authority Badges & Spinbox Lockouts
            if hw_lockout:
                sp_box.setEnabled(False)
                sp_box.setToolTip("Disabled: Hardware interlock or fault is active.")
                lbl_badge.setText("🔒 LOCKED")
                lbl_badge.setStyleSheet(COLOR_FAULT)
                lbl_badge.show()
            elif ctrl_mode == 1:
                if name == "Cesium":
                    sp_box.setEnabled(not comp_lockout)
                    sp_box.setToolTip(f"Disabled: {comp_lock_reason}" if comp_lockout else "")
                    lbl_badge.setText("🔥 HEATING")
                    lbl_badge.setStyleSheet("background-color: #FF5722; color: white; border-radius: 3px;")
                    lbl_badge.show()
                else:
                    sp_box.setEnabled(False)
                    sp_box.setToolTip("Disabled: Control yielded to PLC automated sequence.")
                    lbl_badge.setText("⚙️ PLC AUTO")
                    lbl_badge.setStyleSheet("background-color: #2196F3; color: white; border-radius: 3px;")
                    lbl_badge.show()
            elif ctrl_mode == 2:
                sp_box.setEnabled(False)
                sp_box.setToolTip("Disabled: Control yielded to active Recipe.")
                lbl_badge.setText("🤖 RECIPE")
                lbl_badge.setStyleSheet("background-color: #9C27B0; color: white; border-radius: 3px;")
                lbl_badge.show()
            elif ctrl_mode == 3:
                sp_box.setEnabled(False)
                sp_box.setToolTip("Disabled: Target is dynamically cascaded from a master PID loop.")
                lbl_badge.setText("🔗 CASCADED")
                lbl_badge.setStyleSheet("background-color: #4CAF50; color: white; border-radius: 3px;")
                lbl_badge.show()
            elif ctrl_mode == 4:
                sp_box.setEnabled(False)
                sp_box.setToolTip("Disabled: Output temporarily held by Arc Quench algorithm.")
                lbl_badge.setText("⚡ ARC QUENCH")
                lbl_badge.setStyleSheet(COLOR_WARNING)
                lbl_badge.show()
            else:
                sp_box.setEnabled(not comp_lockout)
                sp_box.setToolTip(f"Disabled: {comp_lock_reason}" if comp_lockout else "")
                lbl_badge.hide()
