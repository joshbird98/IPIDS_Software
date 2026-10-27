"""Source turbo pump (Leybold TURBOVAC, USS protocol over a Waveshare bridge).

On connect the pump's vent/relay strategy parameters are checked and, if the pump is stopped,
corrected and saved to its flash (about 30 s, heartbeating meanwhile). Then the frequency and
status word are read every cycle and the slower values round-robin, one per cycle.

The software can start the pump and reset its faults, but never stop it: trained staff stop it
by hand, at the pump (a stop command is refused). A start or reset is not sent to a pump already
running without a fault, as the reset word carries a stop.

Its load guard (src/core/turbo_guard.py, set in config/turbo_guard.json) warns of an overload -
the backing valve closed, as on 26 October 2026 - hours before the pump fails, and if the load
doesn't fall back trips: stat_load_trip, on which service_plc shuts the source down. Its state is
kept in data/turbo_guard_state.json, so a trip (and an overload's clock) survives a restart.
"""
import json
import os
import time

from src.core import paths, turbo_guard as tg
from src.core.device_service import DeviceService
from src.core.log_setup import Throttle, setup_logging
from src.core.os_helper import harden_windows_process
from src.core.paths import simulation_enabled
from src.core.network_map import (ZMQ_PORT_SRC_TURBO_PUB, ZMQ_PORT_SRC_TURBO_CMD, TOPIC_SRC_TURBO_DATA,
                                  SRC_TURBO_WAVESHARE_IP, SRC_TURBO_WAVESHARE_PORT)
from src.drivers.turbo_uss import AK_READ, AK_WRITE_16, AK_WRITE_FIELD_16, UssLink

BASE_TAG = "ion_beam.source.turbo_pump"
CW_STOP, CW_RUN, CW_RESET = 0x0400, 0x0401, 0x0480
STOP_REFUSED = ("the source turbo pump is never stopped from the software: trained staff stop it by hand, "
                "at the pump")

# Parameter numbers (PNU)
PNU_HW_VER = 1
PNU_SERIAL = 2
PNU_ACT_FREQ = 3        # Hz
PNU_ACT_VOLT = 4        # 0.1 V (the intermediate circuit voltage: 242 is 24.2 V)
PNU_ACT_CURR = 5        # 0.1 A
PNU_SAVE = 8
PNU_CONV_TEMP = 11      # degC
PNU_MAX_FREQ = 18       # Hz (for the % figure)
PNU_RELAY_X1 = 29       # indexed
PNU_BEARING_TEMP = 125  # degC
PNU_X201_FUNC = 134     # non-indexed for the 350i
PNU_OP_HOURS = 184      # 0.01 h
PNU_ERR_CODE = 171      # latest error
PNU_WARN_BITS = 227     # warning bitfield
PNU_VENT_ON_FREQ = 247
PNU_VENT_OFF_FREQ = 248
PNU_USS_WATCHDOG = 182  # comms timeout
# The argon/vent strategy the pump must hold: (pnu, value, write task)
STRATEGY = ((PNU_X201_FUNC, 19, AK_WRITE_16), (PNU_RELAY_X1, 4, AK_WRITE_FIELD_16),
            (PNU_VENT_ON_FREQ, 500, AK_WRITE_16), (PNU_VENT_OFF_FREQ, 5, AK_WRITE_16),
            (PNU_USS_WATCHDOG, 0, AK_WRITE_16))
NACK = (7, 8)
# Status word (ZSW) bits, from Leybold's "Serial Interfaces for TURBOVAC i/iX"
ZSW_READY, ZSW_ERROR, ZSW_TEMP_WARNING, ZSW_TURNING = 1 << 0, 1 << 3, 1 << 7, 1 << 11
ZSW_OVERLOAD_WARNING, ZSW_WARNING = 1 << 13, 1 << 14
GUARD_STATE = "turbo_guard_state.json"
GUARD_SAVE_S = 60.0     # an overload's clock is saved this often (a trip at once)

TURBO_ERROR_DICT = {
    0: "No Error",
    1: "Overspeed warning",
    2: "Pass through time / Bearing temperature too high",
    4: "Short circuit in motor coil or converter",
    5: "Converter temperature error",
    6: "Run-up time error",
    7: "Motor temperature error",
    61: "Bearing temperature warning",
    83: "Motor undertemperature warning",
    84: "Motor temperature warning",
    85: "Converter overtemperature warning",
    86: "Pump temperature 6 warning",
    87: "Pump temperature 6 failure",
    94: "Pump temperature 4 warning",
    95: "Pump temperature 4 failure",
    96: "Pump temperature 5 warning",
    97: "Pump temperature 5 failure",
    101: "Overload warning (speed dropped)",
    103: "Supply voltage warning",
    106: "Overload Failure (speed below minimum)",
    111: "Motor undertemperature error",
    116: "Permanent overload error",
    117: "Motor current error",
    143: "Overspeed failure",
    213: "Supply voltage error (overvoltage)",
    221: "Checksum error 1",
    225: "Bearing run-in active",
    227: "Frequency converter collective error",
    228: "Frequency converter collective error",
    229: "Frequency converter collective error",
    230: "Frequency converter collective error",
    231: "Supply voltage error (overvoltage)",
    232: "Supply voltage error (undervoltage)",
    233: "Supply voltage error (overvoltage)",
    234: "Supply voltage error (undervoltage)",
    235: "Frequency converter collective error",
    236: "Startup failure (mechanical block/high gas load)",
    237: "Frequency converter collective error",
    238: "Frequency converter collective error",
    239: "Frequency converter collective error",
    240: "Checksum error 2",
    241: "Supply voltage is not 24V",
    242: "Supply voltage is not 48V",
    252: "Hardware plausibility error (Converter/Front-end mismatch)"
}

TURBO_WARNING_DICT = {
    0: "Pump temperature 1 too high",
    1: "Pump temperature 2 too high",
    2: "Pump temperature 3 too high",
    3: "Ambient temperature too low",
    6: "Overspeed warning",
    7: "Pump temperature 4 too high",
    11: "Overload warning",
    12: "Pump temperature 5 too high",
    13: "Pump temperature 6 too high",
    14: "Power supply voltage warning"
}


def _make_link():
    if simulation_enabled():
        from src.sim.turbo import FakeUssLink
        return FakeUssLink(SRC_TURBO_WAVESHARE_IP, SRC_TURBO_WAVESHARE_PORT)
    return UssLink(SRC_TURBO_WAVESHARE_IP, SRC_TURBO_WAVESHARE_PORT)


class TurbovacMicroservice(DeviceService):
    NAME = "service_source_turbo"
    DEVICE_LABEL = "source turbo controller"
    PUB_ENDPOINT, CMD_ENDPOINT, TOPIC = ZMQ_PORT_SRC_TURBO_PUB, ZMQ_PORT_SRC_TURBO_CMD, TOPIC_SRC_TURBO_DATA
    POLL_INTERVAL = 0.05
    RECONNECT_DELAYS = (1.0, 2.0, 5.0, 10.0)
    SUBSYSTEMS = (BASE_TAG,)

    def __init__(self):
        super().__init__()
        self.address = f"{SRC_TURBO_WAVESHARE_IP}:{SRC_TURBO_WAVESHARE_PORT}"
        self.link = _make_link()
        self.link.on_problem = self._report_link_problem
        self.comms_ok = False
        self.active_control_word = None
        self._max_hz = 1000
        self._most_recent_error_desc = "No Error"
        self._most_recent_warning_desc = "None"
        self.state.update({"system.connected": 0.0, "system.safety_synced": 0.0, "system.service_hours": 0.0})
        self.slow_tasks = [("bearing_temp", PNU_BEARING_TEMP), ("conv_temp", PNU_CONV_TEMP),
                           ("volts", PNU_ACT_VOLT), ("amps", PNU_ACT_CURR), ("op_hours", PNU_OP_HOURS),
                           ("warnings", PNU_WARN_BITS), ("error", PNU_ERR_CODE)]
        self._slow_idx = 0
        self._warning_bits = None           # the last warning word and error code read (logged when they change)
        self._error_code = None
        guard_config, problems = tg.parse(self.load_config(tg.CONFIG_FILE, {}))
        for problem in problems:
            self.events.log_general(f"{tg.CONFIG_FILE}: {problem} - its default is used", severity="WARNING")
        self.guard = tg.TurboGuard(guard_config)
        self.guard_state_file = os.path.join(paths.DATA_DIR, GUARD_STATE)
        self.guard.restore(self._load_guard_state())
        if self.guard.stage != tg.NORMAL:
            self.events.log_general(f"Source turbo guard restored as {tg.STAGE_NAMES[self.guard.stage]}"
                                    + (f": {self.guard.message}" if self.guard.message else ""),
                                    severity="WARNING", event_type="TURBO_GUARD")
        self._guard_saved = (None, 0.0)
        self._publish_guard()

    def _report_link_problem(self, message):
        if (n := self.throttle.ready(f"link-{message.split(':')[0]}")) is not None:
            self.events.log_general(f"{message}{Throttle.suffix(n)}", severity="WARNING")

    def _transaction(self, pnu, index=0, value=0, ak=AK_READ, control_word=None):
        cw = self.active_control_word if control_word is None else control_word
        return self.link.transaction(pnu, index, value, ak, CW_STOP if cw is None else cw)

    # ------------------------------------------------------------------ connection
    def connect_device(self) -> bool:
        self.link.connect()
        return True

    def is_connected(self) -> bool:
        return self.link.connected

    def close_device(self):
        self.link.close()

    def after_connect(self, previous_failures: int):
        self.state["system.connected"] = 1.0
        self.events.log_general("Connected to simulated TURBOVAC" if simulation_enabled()
                                else f"Connected to {self.address}")
        self.verify_safety_strategy()

    def verify_safety_strategy(self):
        raw_hz, _, _ = self._transaction(PNU_ACT_FREQ, control_word=0x0000)
        if raw_hz is not None and raw_hz > 0:
            self.events.log_general(f"Pump is already SPINNING at {raw_hz} Hz. Adopting LATCHED ON state.")
            self.active_control_word = CW_RUN
        else:
            self.events.log_general("Pump is STOPPED. Adopting LATCHED OFF state.")
            self.active_control_word = CW_STOP

        self.state["system.serial"] = self._transaction(PNU_SERIAL)[0]
        self.state["system.hw_version"] = self._transaction(PNU_HW_VER)[0]
        m_hz, ak_m, _ = self._transaction(PNU_MAX_FREQ)
        if m_hz is not None and ak_m not in NACK:
            self._max_hz = m_hz

        readings = {pnu: self._transaction(pnu, ak=6 if pnu == PNU_RELAY_X1 else AK_READ)
                    for pnu, _, _ in STRATEGY}
        if any(ak == 7 for pnu, (_, ak, _) in readings.items() if pnu != PNU_USS_WATCHDOG):
            self.events.log_general("[!] PUMP RETURNED NACK. Pump is likely saving to flash (Error 102).")
            self.state["system.safety_synced"] = 0.0
            return
        synced = all(readings[pnu][0] == want for pnu, want, _ in STRATEGY)
        self.state["system.safety_synced"] = 1.0 if synced else 0.0
        if synced:
            self.events.log_general("Safety strategy verified and active.")
            return
        if raw_hz != 0:
            self.events.log_general("[!] SAFETY WARNING: Pump is SPINNING but Argon strategy is not loaded!")
            return
        self.events.log_general("Strategy out of sync. Correcting now...")
        for pnu, want, ak in STRATEGY:
            self._transaction(pnu, value=want, ak=ak)
            time.sleep(0.1)
        self.events.log_general("Saving to flash (P8=1)...")
        self._transaction(PNU_SAVE, value=1, ak=AK_WRITE_16)
        # 30 s for the pump to write its flash; keep heartbeating so the manager doesn't restart us
        t_end = time.time() + 30
        while time.time() < t_end:
            self.heartbeat()
            time.sleep(0.25)
        self.events.log_general("Save complete.")

    def permit(self, subsystem: str):
        if not self.link.connected:
            return False, "turbo controller not connected"
        if not self.comms_ok:
            return False, "pump not answering"
        return True, ""

    # ------------------------------------------------------------------ commands
    def _running_well(self) -> bool:
        """Turning, in RUN, with no error: a fault reset now would only send it a stop (CW_RESET has
        the run bit clear)."""
        b = BASE_TAG
        return (self.comms_ok and self.active_control_word == CW_RUN and not self.state.get(f"{b}.stat_error")
                and (self.state.get(f"{b}.rb_speed_hz") or 0) > 0)

    def handle_command(self, cmd, subsystem: str, leaf: str):
        if not self.link.connected:
            cmd.reject("turbo controller not connected")
            return
        if leaf == "cmd_enable":
            # (was `value is True`, which treated a numeric 1 from a recipe as STOP)
            if not cmd.value:
                # Josh, October 2026: the pump is stopped only by trained staff, by hand, at the pump.
                cmd.reject(STOP_REFUSED)
                return
            if self._running_well():
                cmd.ok("already running")
                return
            if self._transaction(PNU_ACT_FREQ, control_word=CW_RESET)[0] is None:   # clear faults first
                cmd.fail("no reply from pump to the fault-reset/start request")
                return
            self.active_control_word = CW_RUN
            cmd.ok("start requested")
        elif leaf == "cmd_reset":
            if not cmd.value:
                cmd.ok("nothing to do")
            elif self._running_well():
                cmd.ok("no fault to reset: the pump is running")
            elif self._transaction(PNU_ACT_FREQ, control_word=CW_RESET)[0] is None:
                cmd.fail("no reply from pump")
            else:
                cmd.ok("fault reset sent")
        else:
            cmd.reject(f"unsupported command '{leaf}'")

    # ------------------------------------------------------------------ telemetry
    def publish_unknown(self):
        self.state["system.connected"] = 0.0
        for key in ("rb_speed_hz", "rb_speed_pct", "stat_ready", "stat_turning", "stat_error", "stat_warning",
                    "stat_trip", "rb_temp_bearing", "rb_temp_converter", "rb_voltage", "rb_current",
                    "rb_status_word"):
            self.state[f"{BASE_TAG}.{key}"] = None
        self.state[f"{BASE_TAG}.stat_comms_fail"] = 1.0

    def poll_device(self):
        b = BASE_TAG
        raw_hz, ak_hz, zsw = self._transaction(PNU_ACT_FREQ)
        if raw_hz is not None and ak_hz not in NACK:
            self.comms_ok = True
            ready = bool(zsw & (1 << 0))
            error_active = bool(zsw & (1 << 3))
            self.state[f"{b}.rb_speed_hz"] = float(raw_hz)
            self.state[f"{b}.stat_ready"] = 1.0 if ready else 0.0
            self.state[f"{b}.stat_turning"] = 1.0 if zsw & (1 << 11) else 0.0
            self.state[f"{b}.stat_error"] = 1.0 if error_active else 0.0
            self.state[f"{b}.stat_warning"] = 1.0 if zsw & (1 << 14) else 0.0
            self.state[f"{b}.stat_trip"] = 1.0 if (error_active and not ready) else 0.0
            self.state[f"{b}.rb_status_word"] = float(zsw)      # (which bit means at speed is still to check)
            if self._max_hz > 0:
                self.state[f"{b}.rb_speed_pct"] = round((raw_hz / self._max_hz) * 100, 1)
            self._run_guard(zsw)
        else:
            self.comms_ok = ak_hz in NACK      # a NACK is still an answer
        comms_fail = not self.link.connected or not self.comms_ok
        self.state[f"{b}.stat_comms_fail"] = 1.0 if comms_fail else 0.0
        if comms_fail:
            self.publish_unknown()
            self.state["system.connected"] = 1.0 if self.link.connected else 0.0
            return

        task, pnu = self.slow_tasks[self._slow_idx]
        self._slow_idx = (self._slow_idx + 1) % len(self.slow_tasks)
        val, ak_val, _ = self._transaction(pnu)
        if val is None or ak_val in NACK:
            return
        if task == "bearing_temp":
            self.state[f"{b}.rb_temp_bearing"] = float(val)
        elif task == "conv_temp":
            self.state[f"{b}.rb_temp_converter"] = float(val)
        elif task == "volts":
            self.state[f"{b}.rb_voltage"] = round(val * 0.1, 1)     # (was published in 0.1 V: 242 for 24.2 V)
        elif task == "amps":
            self.state[f"{b}.rb_current"] = round(val * 0.1, 1)
        elif task == "op_hours":
            self.state["system.service_hours"] = round(val * 0.01, 2)
        elif task == "warnings":
            active = [desc for bit, desc in TURBO_WARNING_DICT.items() if val & (1 << bit)]
            self._most_recent_warning_desc = ", ".join(active) if active else "None"
            if val != self._warning_bits:
                if active:
                    self.events.log_general(f"Source turbo reports: {self._most_recent_warning_desc} "
                                            f"(warning bits {val:#06x})", severity="WARNING")
                elif self._warning_bits:
                    self.events.log_general("Source turbo: its warnings have cleared")
                self._warning_bits = val
        elif task == "error":
            self.state[f"{b}.stat_error_code"] = float(val)
            self._most_recent_error_desc = TURBO_ERROR_DICT.get(val, f"Unknown Error ({val})")
            if self._error_code is not None and val != self._error_code and val:
                self.events.log_general(f"Source turbo error {val}: {self._most_recent_error_desc}",
                                        severity="ERROR")
            self._error_code = val

    # ------------------------------------------------------------------ load guard
    def _run_guard(self, zsw: int):
        b, s = BASE_TAG, self.state
        reading = tg.Reading(speed_pct=s.get(f"{b}.rb_speed_pct"), current_a=s.get(f"{b}.rb_current"),
                             bearing_c=s.get(f"{b}.rb_temp_bearing"), converter_c=s.get(f"{b}.rb_temp_converter"),
                             pump_warning=bool(zsw & (ZSW_WARNING | ZSW_OVERLOAD_WARNING | ZSW_TEMP_WARNING)),
                             pump_error=bool(zsw & ZSW_ERROR), turning=bool(zsw & ZSW_TURNING))
        for severity, text in self.guard.update(time.time(), reading):
            self.events.log_general(text, severity=severity, event_type="TURBO_GUARD")
        self._publish_guard()
        self._save_guard()

    def _publish_guard(self):
        """The stage as one flag each (one alarm each: the warning's goes when the critical one comes),
        the text and the shutdown time for the alarm and the Vacuum panel."""
        b, g = BASE_TAG, self.guard
        stage = g.stage if g.config.enabled else tg.NORMAL
        for flag, at in (("stat_load_warning", tg.WARNING), ("stat_load_critical", tg.CRITICAL),
                         ("stat_load_trip", tg.TRIPPED)):
            self.state[f"{b}.{flag}"] = 1.0 if stage == at else 0.0
        self.state[f"{b}.rb_guard_stage"] = float(stage)
        self.state[f"{b}.guard_message"] = g.message if g.config.enabled else ""
        self.state[f"{b}.guard_shutdown_at"] = g.shutdown_at
        self.state[f"{b}.guard_enabled"] = 1.0 if g.config.enabled else 0.0
        self.state[f"{b}.guard_shuts_down_source"] = 1.0 if g.config.shut_down_source else 0.0

    def _load_guard_state(self):
        try:
            with open(self.guard_state_file, "r") as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            self.events.log_general(f"Source turbo guard: {self.guard_state_file} not read ({e})", severity="WARNING")
            return None

    def _save_guard(self, force: bool = False):
        """At each change of stage, and every minute while an overload's clock runs."""
        g, now = self.guard, time.time()
        stage, at = self._guard_saved
        if not force and stage == g.stage and (g.stage not in (tg.WARNING, tg.CRITICAL) or now - at < GUARD_SAVE_S):
            return
        try:
            os.makedirs(os.path.dirname(self.guard_state_file), exist_ok=True)
            tmp = self.guard_state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(g.dump(), f)
            os.replace(tmp, self.guard_state_file)
            self._guard_saved = (g.stage, now)
        except OSError as e:
            if (n := self.throttle.ready("guard-save")) is not None:
                self.events.log_general(f"Source turbo guard: its state not saved ({e}) - a restart would lose "
                                        f"it{Throttle.suffix(n)}", severity="WARNING")

    def reset_faults(self, cmd):
        """RESET FAULTS (System Health): clears a tripped load guard once the current is back to normal."""
        if self.guard.stage != tg.TRIPPED:
            cmd.ok("nothing to reset here")
            return
        done, why = self.guard.reset(answering=self.link.connected and self.comms_ok)
        if not done:
            cmd.reject(why)
            return
        self.events.log_general(f"Source turbo protection reset ({cmd.origin})", severity="WARNING",
                                event_type="TURBO_GUARD")
        self._publish_guard()
        self._save_guard(force=True)
        cmd.ok(why)

    def run(self):
        self.events.log_general("Starting USS daemon" + (" (SIMULATED PUMP)" if simulation_enabled() else "")
                                + f". Load guard: {self.guard.config.describe()}")
        super().run()


if __name__ == "__main__":
    setup_logging("service_source_turbo")
    harden_windows_process()
    TurbovacMicroservice().run()
