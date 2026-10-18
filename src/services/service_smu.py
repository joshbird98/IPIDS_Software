"""Faraday-cup SMU (Keysight B2900 over SCPI): bias voltage and beam current, with software
auto-ranging.

Safety: the bias is held at 0 V / off while the safety relay is not confirmed energised, and a
trip forgets the bias setpoint (also while the SMU is disconnected). After a reconnect the bias
that was live is re-applied only if fresh PLC data confirms the relay is on; otherwise the SMU
starts from reset (0 V, bias off) and the operator re-applies it.
"""
from src.core.device_service import DeviceService
from src.core.log_setup import setup_logging
from src.core.os_helper import harden_windows_process
from src.core.paths import simulation_enabled
from src.core.network_map import SMU_IP, SMU_PORT, ZMQ_PORT_SMU_PUB, ZMQ_PORT_SMU_CMD, TOPIC_SMU_DATA
from src.drivers.smu_scpi import CURRENT_RANGES, SmuScpiProtocol

MAX_CMD_AGE = 0.5
BASE = "ion_beam.beamline.faraday.smu"
SP_BIAS = f"{BASE}.sp_actual_voltage"

UP_RANGE_THRESH = 0.95
DOWN_RANGE_THRESH = 0.08
KEYSIGHT_OVERLOAD_VAL = 1e37   # SCPI IEEE 'infinity' on overload


def _make_driver():
    if simulation_enabled():
        from src.sim.smu import make_fake_smu
        return make_fake_smu(SmuScpiProtocol)(SMU_IP, SMU_PORT)
    return SmuScpiProtocol(SMU_IP, SMU_PORT)


class SmuMicroservice(DeviceService):
    NAME = "service_smu"
    DEVICE_LABEL = "SMU"
    PUB_ENDPOINT, CMD_ENDPOINT, TOPIC = ZMQ_PORT_SMU_PUB, ZMQ_PORT_SMU_CMD, TOPIC_SMU_DATA
    POLL_INTERVAL = 0.1     # stretches by itself at high NPLC (the measurement blocks)
    MAX_CMD_AGE = MAX_CMD_AGE
    RECONNECT_DELAYS = (1.0, 2.0, 5.0, 10.0)
    WATCH_RELAY = True
    RELAY_MAX_AGE = 1.0
    SUBSYSTEMS = (BASE,)
    REGISTRY_SOURCE = "service_faraday_smu"

    def __init__(self):
        super().__init__()
        self.address = f"{SMU_IP}:{SMU_PORT}"
        self.limits = self.load_config("smu_config.json", {"compliance_current": 100e-6})
        self.hw = _make_driver()
        self.sp_cache = {}
        self.range_idx = 0                 # 10 nA
        self._lockout_active = False
        self._bias_was_on = False
        self.state[f"{BASE}.stat_range_settled"] = 0.0

    # ------------------------------------------------------------------ connection
    def connect_device(self) -> bool:
        self._bias_was_on = self.hw.output_state
        if not self.hw.connect():
            return False
        self.hw.send_cmd(f":SENS:CURR:PROT {self.limits.get('compliance_current', 100e-6):.6e}")
        self.update_command_age_limit()
        return True

    def is_connected(self) -> bool:
        return self.hw.connected

    def close_device(self):
        self.hw.disconnect()

    def update_command_age_limit(self):
        # A measurement at high NPLC blocks the loop for seconds, so a command can only be read
        # that much later: scale the freshness limit with the integration time.
        self.commands.max_age = MAX_CMD_AGE + 2.5 * 2 * self.hw.current_nplc / 50.0

    def relay_confirmed_on(self) -> bool:
        """Fresh PLC data says the relay is energised. Drains the PLC queue first and goes by the
        PLC's own timestamp, so a reconnect that took a few seconds doesn't act on what the relay
        was doing before it started."""
        return self.update_relay() and self.relay.fresh()

    def after_connect(self, previous_failures: int):
        """*RST on connect leaves the source at 0 V. Put back the bias that was live when the link
        dropped only if the safety relay is confirmed on; otherwise start from reset (0 V, bias
        off) and forget the old setpoint, so the operator re-applies it."""
        last_v = self.sp_cache.get(SP_BIAS) or 0.0
        if self._bias_was_on and last_v != 0.0:
            if self.relay_confirmed_on():
                self.events.log_general(f"SMU reconnected - safety relay confirmed on, restoring the previous "
                                        f"bias of {last_v:g} V")
                self.hw.set_voltage(last_v)
                self.hw.set_output(True)
                return
            self.events.log_general(f"SMU reconnected - previous bias of {last_v:g} V NOT restored (safety relay "
                                    f"not confirmed on); bias reset to 0 V, output off", severity="WARNING")
        else:
            self.events.log_general(f"Connected to SMU at {SMU_IP}:{SMU_PORT} (bias off, 0 V)")
        self.hw.set_voltage(0.0)
        self.hw.set_output(False)
        self.sp_cache[SP_BIAS] = 0.0

    # ------------------------------------------------------------------ safety
    def update_relay(self):
        ok = super().update_relay()
        if not ok:
            # A trip resets the bias - also while the SMU is disconnected, so a later reconnect
            # can't bring back a bias from before the trip.
            self.sp_cache[SP_BIAS] = 0.0
        return ok

    def on_relay_change(self, ok: bool, why: str):
        self.events.log_general("SMU safety permissive restored" if ok
                                else "SMU bias forced to 0 V: safety relay off or no PLC telemetry",
                                severity="INFO" if ok else "WARNING")

    def cycle(self):
        if not self.relay_ok and self.hw.connected:
            on = self.state.get(f"{BASE}.stat_enabled") == 1.0
            rb = self.state.get(f"{BASE}.rb_voltage") or 0.0
            if not self._lockout_active or on or abs(rb) > 0.1:
                self.log.warning("Safety interlock: forcing SMU output off and 0 V.")
                self.hw.set_voltage(0.0)
                self.hw.set_output(False)
                self.sp_cache[SP_BIAS] = 0.0
                self._lockout_active = True
        elif self.relay_ok:
            self._lockout_active = False

    # ------------------------------------------------------------------ commands
    def handle_command(self, cmd, subsystem: str, leaf: str):
        value = self.number(cmd)
        if value is None:
            return
        if not self.hw.connected:
            cmd.reject("SMU not connected")
            return
        if not self.relay_ok and leaf in ("sp_requested_voltage", "cmd_enable") and \
                not (leaf == "cmd_enable" and not value):
            cmd.reject("safety relay de-energised (or no PLC telemetry) - bias held at 0 V")
            return

        if leaf == "sp_requested_voltage":
            if self.state.get(f"{BASE}.stat_enabled", 0.0) != 1.0:
                self.hw.set_output(False)
                cmd.reject("bias output is disabled - enable it before setting a voltage")
            elif self.within_limits(cmd, value, "V"):
                self.hw.set_voltage(value)
                if self.hw.connected:
                    self.sp_cache[SP_BIAS] = value   # only once sent (it may be restored later)
                    cmd.ok()
                else:
                    cmd.fail("SMU connection lost while sending")
        elif leaf == "cmd_enable":
            self.hw.set_output(bool(value))
            if not value:
                self.sp_cache[SP_BIAS] = 0.0         # disabling sends 0 V
            cmd.ok("bias enabled" if value else "bias disabled (0 V)")
        elif leaf == "cmd_nplc":
            self.hw.set_nplc(value)
            self.state[f"{BASE}.rb_nplc"] = self.hw.current_nplc
            self.update_command_age_limit()
            cmd.ok(f"NPLC {self.hw.current_nplc:g}")
        elif leaf == "cmd_range_val":
            self.hw.cached_range_val = float(value)
            if self.hw.connected and not self.hw.cached_auto_range:
                self.hw.send_cmd(f":SENS:CURR:RANG {self.hw.cached_range_val:.2e}")
            cmd.ok()
        elif leaf == "cmd_azer":
            self.hw.cached_azer = bool(value)
            if self.hw.connected:
                self.hw.send_cmd(f":SYST:AZER {'ON' if value else 'OFF'}")
            cmd.ok()
        else:
            cmd.reject(f"unsupported command '{leaf}'")

    # ------------------------------------------------------------------ telemetry
    def publish_unknown(self):
        self.state[f"{BASE}.stat_comms_fail"] = 1.0
        for key in ("rb_voltage", "rb_current", "stat_enabled", "stat_range_settled", "sp_actual_voltage"):
            self.state[f"{BASE}.{key}"] = None

    def poll_device(self):
        telemetry = self.hw.read_telemetry()
        if telemetry is None:
            self.publish_unknown()
            return
        self.state[f"{BASE}.stat_comms_fail"] = 0.0
        v_rb, i_rb, output_on = telemetry

        # Software auto-ranging: step up near full scale (or on overload), down when tiny
        range_val = CURRENT_RANGES[self.range_idx]
        changed = False
        if abs(i_rb) > KEYSIGHT_OVERLOAD_VAL or abs(i_rb) > UP_RANGE_THRESH * range_val:
            if self.range_idx < len(CURRENT_RANGES) - 1:
                self.range_idx += 1
                changed = True
        elif abs(i_rb) < DOWN_RANGE_THRESH * range_val and self.range_idx > 0:
            self.range_idx -= 1
            changed = True
        if changed:
            self.hw.send_cmd(f":SENS:CURR:RANG {CURRENT_RANGES[self.range_idx]:.2e}")
            self.state[f"{BASE}.stat_range_settled"] = 0.0
            return    # discard this reading: the instrument is switching range

        self.state[f"{BASE}.stat_range_settled"] = 1.0
        self.state[f"{BASE}.rb_voltage"] = round(v_rb, 4)
        self.state[f"{BASE}.rb_current"] = i_rb
        self.state[f"{BASE}.stat_enabled"] = 1.0 if output_on else 0.0
        self.state[f"{BASE}.rb_nplc"] = self.hw.current_nplc
        if SP_BIAS in self.sp_cache:
            self.state[SP_BIAS] = self.sp_cache[SP_BIAS]

    def run(self):
        self.events.log_general("[SMU Service] starting" + (" (SIMULATED SMU)" if simulation_enabled() else ""))
        super().run()


if __name__ == "__main__":
    setup_logging("service_smu")
    harden_windows_process()
    SmuMicroservice().run()
