from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QPushButton,
    QGroupBox, QListWidget
)

from src.gui import theme
from src.gui.reflow import ReflowGrid
from src.gui.style import chip, set_role, set_tone

TURBO_PROTECTION = "ion_beam.system.turbo_protection"   # service_plc: the source turbo protection holds the source


class SystemHealthWidget(QWidget):
    """Centralized widget for Master Safety, Shutdown Sequences, and Active Faults."""

    def __init__(self, cmd_thread, fault_engine):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.fault_engine = fault_engine

        self.main_layout = QVBoxLayout(self)

        self._init_safety_ui()
        self._init_faults_ui()

    def _dispatch_command(self, tag: str, value):
        self.cmd_thread.send_command("plc", tag, value)

    def _reset_faults(self, *args):
        # The PLC latches, plus the Spellman service's software arc latches (which previously
        # could only be cleared by restarting that service: the reset only ever went to the PLC),
        # and the source turbo's load guard once its load is back to normal
        self.cmd_thread.send_command("plc", "ion_beam.system.cmd_fault_reset", True)
        self.cmd_thread.send_command("spellman", "ion_beam.system.cmd_fault_reset", True)
        self.cmd_thread.send_command("src_turbo", "ion_beam.system.cmd_fault_reset", True)

    def _init_safety_ui(self):
        safety_group = QGroupBox("Master safety && sequence control")
        sl = QVBoxLayout(safety_group)

        self.lbl_comms = chip("PLC COMMS: WAITING FOR DATA...", "unknown")
        self.lbl_safety_state = chip("SAFETY RELAY: WAITING FOR DATA...", "unknown")
        self.lbl_shutdown_state = chip("SHUTDOWN SEQ: WAITING FOR DATA...", "unknown")
        for lbl in (self.lbl_comms, self.lbl_safety_state, self.lbl_shutdown_state):
            lbl.setWordWrap(True)           # a long state wraps rather than widening the panel

        self.btn_enable_safety = QPushButton("ENABLE SAFETY RELAY")
        self.btn_enable_safety.setCheckable(True)
        set_tone(self.btn_enable_safety, "primary")
        self._countdown_styled = False
        self.btn_enable_safety.clicked.connect(
            lambda *args, b=self.btn_enable_safety: self._dispatch_command("ion_beam.system.cmd_enable_safety",
                                                                           b.isChecked()))

        self.btn_reset_faults = QPushButton("RESET FAULTS")
        self.btn_reset_faults.clicked.connect(self._reset_faults)

        self.btn_shutdown = QPushButton("INITIATE SHUTDOWN")
        set_tone(self.btn_shutdown, "danger")
        self.btn_shutdown.clicked.connect(
            lambda *args: self._dispatch_command("ion_beam.system.cmd_source_shutdown", True))

        self.btn_cancel_shutdown = QPushButton("ABORT SHUTDOWN")
        set_tone(self.btn_cancel_shutdown, "warn")
        self.btn_cancel_shutdown.clicked.connect(
            lambda *args: self._dispatch_command("ion_beam.system.cmd_cancel_shutdown", True))

        # In a row when the panel is wide; two by two (or one above another) when it is narrow
        states = ReflowGrid(max_columns=3)
        for w in (self.lbl_comms, self.lbl_safety_state, self.lbl_shutdown_state):
            states.add(w)
        buttons = ReflowGrid(max_columns=4)
        for w in (self.btn_enable_safety, self.btn_reset_faults, self.btn_shutdown, self.btn_cancel_shutdown):
            buttons.add(w)
        sl.addWidget(states)
        sl.addWidget(buttons)
        self.main_layout.addWidget(safety_group)

    def _init_faults_ui(self):
        fault_group = QGroupBox("PLC faults")
        fault_layout = QVBoxLayout()
        hint = QLabel("Latched until RESET FAULTS. Acknowledge and shelve them in the Alarms panel.")
        set_role(hint, "caption")
        hint.setWordWrap(True)
        fault_layout.addWidget(hint)
        self.active_faults_list = QListWidget()
        self.active_faults_list.setMinimumHeight(90)
        self._fault_lines = None

        fault_layout.addWidget(self.active_faults_list)
        fault_group.setLayout(fault_layout)
        self.main_layout.addWidget(fault_group, stretch=1)

    def update_telemetry(self, data: dict):
        master_comms_lost = not bool(data.get("system.connected", False)) or bool(
            data.get("ion_beam.system.pc_plc_comms_lost", False))

        relay_active = bool(data.get("ion_beam.facilities.safety_relay_active", False))
        auto_sdown = bool(data.get("ion_beam.system.src_auto_sdown", 0.0))
        active_step = max(int(data.get("ion_beam.system.remote_shutdown_step", 0.0)),
                          int(data.get("ion_beam.system.pc_shutdown_step", 0.0)))

        self._update_comms_and_safety_state(data, master_comms_lost, relay_active)
        self._update_shutdown_labels(active_step, auto_sdown, master_comms_lost)
        self._update_safety_controls(data, relay_active, active_step, master_comms_lost)
        self._show_turbo_protection(data, master_comms_lost, relay_active)
        self._update_fault_list(data, master_comms_lost)

    def _update_comms_and_safety_state(self, data: dict, master_comms_lost: bool, relay_active: bool):
        if master_comms_lost:
            self.lbl_comms.setText("PLC COMMS: OFFLINE")
            set_tone(self.lbl_comms, "fault")
            self.lbl_safety_state.setText("SAFETY: UNKNOWN")
            set_tone(self.lbl_safety_state, "unknown")
            return

        self.lbl_comms.setText("PLC COMMS: OK")
        set_tone(self.lbl_comms, "ok")

        w0_raw = data.get("ion_beam.faults.word_0_system")
        w0_faults = self.fault_engine.get_active_faults("UDT_Fault_Word_0_System", int(w0_raw)) if w0_raw else []

        sr_tripped = any(f["name"] == "Safety_Relay_Tripped" for f in w0_faults)
        sr_refused = any(f["name"] == "Safety_Relay_Refused" for f in w0_faults)
        remote_lost = any(f["name"] == "Remote_IO_Lost" for f in w0_faults) or bool(
            data.get("ion_beam.system.remote_io_lost", 0.0))

        if relay_active:
            self.lbl_safety_state.setText("SAFETY: LIVE")
            set_tone(self.lbl_safety_state, "warn")
        elif sr_tripped or sr_refused:
            self.lbl_safety_state.setText("SAFETY: TRIPPED/REFUSED")
            set_tone(self.lbl_safety_state, "fault")
        elif remote_lost:
            self.lbl_safety_state.setText("SAFETY: NO REMOTE IO")
            set_tone(self.lbl_safety_state, "fault")
        else:
            self.lbl_safety_state.setText("SAFETY: DE-ENERGIZED")
            set_tone(self.lbl_safety_state, "ok")

    def _show_turbo_protection(self, data: dict, master_comms_lost: bool, relay_active: bool):
        """While the source turbo protection holds the source down (service_plc), ABORT SHUTDOWN and
        ENABLE SAFETY RELAY are refused: say so on them, and on the shutdown state."""
        if master_comms_lost or not data.get(TURBO_PROTECTION):
            return
        why = data.get("ion_beam.source.turbo_pump.guard_message") or ""
        self.lbl_shutdown_state.setText(self.lbl_shutdown_state.text() + " - SOURCE TURBO PROTECTION")
        set_tone(self.lbl_shutdown_state, "fault")
        self.lbl_shutdown_state.setToolTip(why)
        self.btn_cancel_shutdown.setEnabled(False)
        self.btn_cancel_shutdown.setToolTip("Disabled: the source turbo protection started this shutdown, and it "
                                            "can't be aborted.\n\n" + why)
        if not relay_active:
            self.btn_enable_safety.setEnabled(False)
            self.btn_enable_safety.setToolTip("Disabled: the source turbo protection has tripped. The source stays off "
                                              "until RESET FAULTS, once the turbo's load is back to normal.\n\n" + why)

    def _update_shutdown_labels(self, active_step: int, auto_sdown: bool, master_comms_lost: bool):
        self.lbl_shutdown_state.setToolTip("")
        if master_comms_lost:
            self.lbl_shutdown_state.setText("SHUTDOWN SEQ: UNKNOWN")
            set_tone(self.lbl_shutdown_state, "unknown")
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
            set_tone(self.lbl_shutdown_state, "warn" if active_step in [1, 2, 10, 11] else "fault")
        else:
            self.lbl_shutdown_state.setText("SHUTDOWN SEQ: INACTIVE")
            set_tone(self.lbl_shutdown_state, "off")

    def _update_safety_controls(self, data: dict, relay_active: bool, active_step: int, master_comms_lost: bool):
        gv_is_open = bool(data.get("ion_beam.source.chamber.stat_gv_open", False))
        source_is_cold = bool(data.get("ion_beam.source.chamber.stat_gv_close_permissive", False))

        # Directly evaluate power supply activity from telemetry tags
        psu_tags = [
            "ion_beam.source.einzel.stat_enabled",
            "ion_beam.source.extraction.stat_enabled",
            "ion_beam.source.target.stat_enabled",
            "ion_beam.source.filament.stat_enabled",
            "ion_beam.source.thermionic.stat_enabled"
        ]
        any_psu_active = any(bool(data.get(tag, False)) for tag in psu_tags)

        self.btn_reset_faults.setEnabled(not master_comms_lost)
        self.btn_reset_faults.setToolTip(
            "Disabled: GUI to PLC communications are offline." if master_comms_lost else "Click to clear latched software and hardware faults.")

        cmd_enable_safety = bool(data.get("ion_beam.system.cmd_enable_safety", False))
        timer_elapsed_ms = float(data.get("ion_beam.system.safety_timer_elapsed_ms", 0.0))
        timer_total_ms = float(data.get("ion_beam.system.safety_timer_total_ms", 5000.0))

        is_timing_out = cmd_enable_safety and (0 < timer_elapsed_ms < timer_total_ms) and not relay_active

        if is_timing_out and timer_total_ms > 0:
            remaining_sec = (timer_total_ms - timer_elapsed_ms) / 1000.0
            total_sec = timer_total_ms / 1000.0
            fill_ratio = max(0.0, min(1.0, remaining_sec / total_sec))

            # The time left to press the panel's START button, as a bar draining across the button
            t = theme.tokens()
            left = theme.rgba(t["warn_fill"], 0.5)
            sweep_style = (f"QPushButton {{ background: qlineargradient(x1:1, y1:0, x2:0, y2:0, "
                           f"stop:0 {left}, stop:{fill_ratio:.3f} {left}, "
                           f"stop:{min(1.0, fill_ratio + 0.001):.3f} {t['raised']}); "
                           f"color: {t['text']}; font-weight: 700; border: 2px solid {t['warn_fill']}; }}")
            self.btn_enable_safety.blockSignals(True)
            self.btn_enable_safety.setChecked(True)
            self.btn_enable_safety.blockSignals(False)

            self.btn_enable_safety.setText(f"PRESS PANEL START ({int(remaining_sec) + 1}s)")
            self.btn_enable_safety.setStyleSheet(sweep_style)
            self._countdown_styled = True
            self.btn_enable_safety.setEnabled(True)
            self.btn_enable_safety.setToolTip(f"Awaiting physical panel button press... ({total_sec}s timeout)")

        else:
            self.btn_enable_safety.blockSignals(True)
            self.btn_enable_safety.setChecked(relay_active)
            self.btn_enable_safety.blockSignals(False)

            if self._countdown_styled:
                self.btn_enable_safety.setStyleSheet("")
                self._countdown_styled = False
            # Energised: amber, as the SAFETY: LIVE label (the HV is on); otherwise the main action
            set_tone(self.btn_enable_safety, "warn" if relay_active else "primary")

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

    def _update_fault_list(self, data: dict, master_comms_lost: bool):
        if master_comms_lost:
            lines, tone = ["Telemetry offline: fault status unknown"], "unknown"
        else:
            # Every fault word in fault_config.json (word_3_spellman used to be missing here)
            lines = []
            for udt_key, udt_info in self.fault_engine.fault_structures.items():
                raw_word = data.get(udt_info.get("registry_tag", ""))
                if raw_word is None:
                    continue
                for fault in self.fault_engine.get_active_faults(udt_key, int(raw_word)):
                    lines.append(f"{fault['name'].upper()}: {fault['description']}")
            tone = "fault" if lines else "off"
            if not lines:
                lines = ["No latched faults"]
        if lines == self._fault_lines:
            return                  # unchanged: leave the list (and its scroll position) alone
        self._fault_lines = lines
        self.active_faults_list.clear()
        self.active_faults_list.addItems(lines)
        set_tone(self.active_faults_list, tone)
