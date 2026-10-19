"""Magnet power supplies (Modbus TCP): the analyser magnet and the X steerer - same PSU model,
same service; `python service_magnet_psu.py` runs the analyser magnet and
service_steerer_magnet_psu.py the steerer.

Safety: the output is held at 0 A / 0 V / off whenever the safety relay is not confirmed
energised (the steerer PSU is not on the relay circuit at all, so for it this is the only
interlock). The output is forced off when the interlock trips, every second while it lasts,
and at once if the PSU reports its output on anyway; it does not come back on by itself.
"""
import time
from dataclasses import dataclass, field

from src.core import hard_caps
from src.core.device_service import DeviceService
from src.drivers.magnet_modbus import IO_ERRORS, RatingsUnknown
from src.core.log_setup import Throttle, setup_logging
from src.core.os_helper import harden_windows_process
from src.core.paths import simulation_enabled
from src.core.network_map import (
    MAGNET_IP, MAGNET_PORT, ZMQ_PORT_MAGNET_PUB, ZMQ_PORT_MAGNET_CMD, TOPIC_MAGNET_DATA,
    STEERER_MAGNET_IP, STEERER_MAGNET_PORT, ZMQ_PORT_STEERER_MAGNET_PUB, ZMQ_PORT_STEERER_MAGNET_CMD,
    TOPIC_STEERER_MAGNET_DATA)

FORCE_SAFE_REPEAT_S = 1.0     # while interlocked, re-send 'output off' this often
SETTLE_S = 1.5                # after a setpoint change, before judging the coil resistance
HELD_SP_TOLERANCE = 0.05      # A / V: a held setpoint read back this close to its hard cap is taken as at it


@dataclass(frozen=True)
class MagnetKind:
    name: str
    label: str
    base: str
    registry_source: str
    pub: str
    cmd: str
    topic: bytes
    ip: str
    port: int
    config_file: str
    voltage_overhead: float          # voltage limit = I x R_nominal x this
    defaults: dict = field(default_factory=dict)
    sim: dict = field(default_factory=dict)


_COMMON_DEFAULTS = {
    "max_power": 200.0,
    "min_current_for_calc": 2.0,
    "open_circuit_threshold": 5.0,
    "degauss_steps_amps": [50.0, 0.0, 40.0, 0.0, 30.0, 0.0, 20.0, 0.0, 10.0, 0.0, 5.0, 0.0],
    "degauss_step_time_sec": 1.5,
}

KINDS = {
    "magnet": MagnetKind(
        name="service_magnet_psu", label="Magnet", base="ion_beam.beamline.magnet",
        registry_source="service_magnet", pub=ZMQ_PORT_MAGNET_PUB, cmd=ZMQ_PORT_MAGNET_CMD,
        topic=TOPIC_MAGNET_DATA, ip=MAGNET_IP, port=MAGNET_PORT, config_file="magnet_config.json",
        voltage_overhead=1.50,
        defaults=dict(_COMMON_DEFAULTS, max_voltage=20.0, nominal_resistance=0.16, resistance_tolerance=0.05,
                      short_circuit_threshold=0.05),
        sim=dict(name="magnet", resistance=0.16, nom_v=30.0, nom_i=100.0)),
    "steerer": MagnetKind(
        name="service_steerer_magnet_psu", label="Steerer", base="ion_beam.beamline.steerer_magnet",
        registry_source="service_steerer_magnet", pub=ZMQ_PORT_STEERER_MAGNET_PUB,
        cmd=ZMQ_PORT_STEERER_MAGNET_CMD, topic=TOPIC_STEERER_MAGNET_DATA, ip=STEERER_MAGNET_IP,
        port=STEERER_MAGNET_PORT, config_file="steerer_magnet_config.json", voltage_overhead=1.20,
        defaults=dict(_COMMON_DEFAULTS, max_voltage=10.0, nominal_resistance=1.6, resistance_tolerance=0.2,
                      short_circuit_threshold=0.5),
        sim=dict(name="steerer", resistance=0.3, nom_v=40.0, nom_i=80.0)),
}


def make_driver(kind: MagnetKind):
    if simulation_enabled():
        from src.sim.magnet import FakeMagnetModbus
        return FakeMagnetModbus(kind.ip, kind.port, **kind.sim)
    from src.drivers.magnet_modbus import MagnetModbusProtocol
    return MagnetModbusProtocol(kind.ip, kind.port, name=kind.sim.get("name", "magnet"))


class MagnetPsuService(DeviceService):
    POLL_INTERVAL = 0.05
    RECONNECT_DELAYS = (0.5, 1.0, 2.0, 5.0)
    WATCH_RELAY = True
    RELAY_MAX_AGE = 1.0

    def __init__(self, kind: str = "magnet"):
        k = KINDS[kind]
        self.kind = k
        self.NAME = k.name
        self.DEVICE_LABEL = f"{k.label.lower()} PSU"
        self.PUB_ENDPOINT, self.CMD_ENDPOINT, self.TOPIC = k.pub, k.cmd, k.topic
        self.SUBSYSTEMS = (k.base,)
        self.REGISTRY_SOURCE = k.registry_source
        super().__init__()
        self.base = k.base
        self.address = f"{k.ip}:{k.port}"
        self.limits = self.load_config(k.config_file, k.defaults)
        self.cap_current, self.cap_voltage = hard_caps.magnet_caps(k.config_file)
        for key, cap in (("max_current", self.cap_current), ("max_voltage", self.cap_voltage)):
            value = self.limits.get(key)            # a file edited past the editor: the cap is used instead
            if value is not None and (not hard_caps.is_number(value) or cap.breach(value)):
                why = cap.explain(f"{k.config_file} {key}", value) if hard_caps.is_number(value) or \
                    cap.breach(value) else f"{k.config_file} {key} {value!r} is not a number."
                self.events.log_general(f"{why} {cap.high:g} {cap.unit} is used.", severity="ERROR")
                self.limits[key] = cap.high
        self.hw = make_driver(k)
        self.sp_cache = {}
        self.last_setpoint_ts = 0.0
        self.degauss_active = False
        self.degauss_step_idx = 0
        self.degauss_last_step_ts = 0.0
        self._last_force_safe = 0.0
        self._ratings_seen = None       # the ratings state last reported in an event (_report_ratings)

    # ------------------------------------------------------------------ connection
    def connect_device(self) -> bool:
        return self.hw.connect()

    def is_connected(self) -> bool:
        return self.hw.connected

    def after_connect(self, previous_failures: int):
        where = f"{self.kind.ip}:{self.kind.port}"
        self.events.log_general(f"{self.kind.label} PSU reconnected after {previous_failures} failed attempt(s)"
                                if previous_failures else f"Connected to {self.kind.label.lower()} PSU at {where}")

    def close_device(self):
        self.hw.disconnect()

    # ------------------------------------------------------------------ safety
    def on_relay_change(self, ok: bool, why: str):
        if ok:
            self.events.log_general(f"{self.kind.label} safety permissive restored (output stays off until enabled)")
        else:
            self.events.log_general(f"{self.kind.label} output forced off: {why}", severity="WARNING")
            self._last_force_safe = 0.0   # act now, not on the next repeat

    def cycle(self):
        if not self.relay_ok:
            self.force_safe_outputs()
        elif self.hw.connected:
            self.execute_degauss_cycle()

    def force_safe_outputs(self):
        """Interlocked: hold the PSU at 0 A, 0 V, output off. Sent when the interlock trips, then
        every FORCE_SAFE_REPEAT_S, and at once if the PSU reports its output on anyway."""
        self.degauss_active = False
        if not self.hw.connected:
            return
        output_on = self.state.get(f"{self.base}.stat_enabled") == 1.0
        if not output_on and time.time() - self._last_force_safe < FORCE_SAFE_REPEAT_S:
            return
        try:
            self.hw.set_current(0.0)
            self.hw.set_voltage(0.0)
            self.hw.set_output(False)
            self.sp_cache[f"{self.base}.sp_actual_voltage"] = 0.0
            self.sp_cache[f"{self.base}.sp_actual_current"] = 0.0
            self._last_force_safe = time.time()
        except Exception as e:   # whatever stopped it, treat the link as lost: reconnect and force again
            if not isinstance(e, IO_ERRORS):
                self.log.exception("Unexpected error forcing the outputs off")
            if (n := self.throttle.ready("force-safe")) is not None:
                self.events.log_general(f"Could not force {self.kind.label.lower()} outputs off: {e}"
                                        f"{Throttle.suffix(n)}", severity="ERROR")
            self.hw.connected = False

    # ------------------------------------------------------------------ setpoints
    def apply_smart_setpoint(self, target_current: float):
        """Voltage limit from the current (I x R x overhead, at least 1 V, at most max_voltage; 0 V
        for 0 A), raised before the current when going up and lowered after it when going down.
        Never past the hard cap (the commands are checked before; this is the last line)."""
        if self.cap_current.breach(abs(target_current)):
            raise ValueError(self.cap_current.explain("the current", abs(target_current)))
        if target_current == 0:
            target_voltage = 0.0        # as force_safe_outputs; zero is also all the PSU takes without its ratings
        else:
            target_voltage = max(1.0, target_current * self.limits["nominal_resistance"] * self.kind.voltage_overhead)
            target_voltage = min(target_voltage, self.limits["max_voltage"])
        if target_current >= self.sp_cache.get(f"{self.base}.sp_actual_current", 0.0):
            self.hw.set_voltage(target_voltage)
            self.hw.set_current(target_current)
        else:
            self.hw.set_current(target_current)
            self.hw.set_voltage(target_voltage)
        self.sp_cache[f"{self.base}.sp_actual_voltage"] = target_voltage
        self.sp_cache[f"{self.base}.sp_actual_current"] = target_current

    def _held_setpoint_past_cap(self) -> str:
        """Why switching the output on is refused, if the setpoints the PSU holds (e.g. set on its
        front panel) are past the hard caps - switching on would drive them - else ''."""
        held = self.hw.read_setpoints()
        if held is None:
            return ""
        volts, amps = held
        for cap, value, what in ((self.cap_current, amps, "current"), (self.cap_voltage, volts, "voltage")):
            # (read back through the PSU's setpoint resolution: a value set exactly at the cap may read
            # a hair over it)
            if cap.breach(value) and not abs(value) <= cap.high + HELD_SP_TOLERANCE:
                return (f"{cap.explain(f'the {what} setpoint the PSU holds', value)} Set the current here first "
                        "(that also sets the voltage limit).")
        return ""

    def execute_degauss_cycle(self):
        if not self.degauss_active:
            return
        now = time.time()
        steps = self.limits.get("degauss_steps_amps", [])
        if now - self.degauss_last_step_ts <= self.limits.get("degauss_step_time_sec", 10):
            return
        if self.degauss_step_idx >= len(steps):
            self.degauss_active = False
            self.events.log_general("Degaussing cycle completed.")
            return
        amps = steps[self.degauss_step_idx]
        self.log.info("Degauss step %d/%d: %.1f A", self.degauss_step_idx + 1, len(steps), amps)
        try:
            self.apply_smart_setpoint(amps)
        except ValueError as e:       # a step past the hard cap (refused at the command; a file edited since)
            self.degauss_active = False
            self.events.log_general(f"Degauss aborted - {e}", severity="ERROR")
            return
        except RatingsUnknown as e:   # reconnected without the PSU's ratings: end at 0 A, as the full sequence does
            self.degauss_active = False
            try:
                self.apply_smart_setpoint(0.0)
                left = "current set to 0 A"
            except IO_ERRORS as e2:
                left = f"0 A could not be set either: {e2}"
            self.events.log_general(f"Degauss aborted - {e}; {left}", severity="ERROR")
            return
        except Exception as e:   # whatever stopped it, the sequence ends here and is reported
            if not isinstance(e, IO_ERRORS):
                self.log.exception("Unexpected error during degauss")
            self.degauss_active = False
            self.events.log_general(f"Degauss aborted - PSU write failed: {e}", severity="ERROR")
            return
        self.degauss_last_step_ts = now
        self.degauss_step_idx += 1

    # ------------------------------------------------------------------ commands
    def handle_command(self, cmd, subsystem: str, leaf: str):
        value = self.number(cmd)
        if value is None:
            return
        if not self.hw.connected:
            cmd.reject(f"{self.kind.label.lower()} PSU not connected")
            return
        if not self.relay_ok:
            if leaf == "cmd_enable" and not value:
                cmd.ok("already off: safety interlock holds the output off")
            else:
                cmd.reject("safety relay de-energised (or no PLC telemetry) - output held off")
            return
        if not self.hw.ratings_ok and value and leaf in ("sp_requested_current", "cmd_enable", "cmd_degauss"):
            cmd.reject(f"{self.kind.label} PSU didn't report its nominal ratings - only 0 A / output off are "
                       "accepted until it does (setpoints would be scaled wrongly)")
            return
        try:
            if leaf == "sp_requested_voltage":
                cmd.reject("voltage is set automatically from the current setpoint")
            elif leaf == "sp_requested_current":
                if self.within_limits(cmd, value, "A"):
                    self.degauss_active = False
                    self.apply_smart_setpoint(value)
                    self.last_setpoint_ts = time.time()
                    cmd.ok()
            elif leaf == "cmd_enable":
                if value and (why := self._held_setpoint_past_cap()):
                    cmd.reject(why)
                    return
                self.hw.set_output(bool(value))
                self.last_setpoint_ts = time.time()
                cmd.ok("output on" if value else "output off")
            elif leaf == "cmd_degauss":
                over = [abs(s) for s in self.limits.get("degauss_steps_amps", []) if self.cap_current.breach(abs(s))]
                if value and over:
                    cmd.reject(self.cap_current.explain(f"{self.kind.config_file} degauss step", max(over)))
                elif value and (why := self._held_setpoint_past_cap()):     # it switches the output on
                    cmd.reject(why)
                elif value:
                    self.events.log_general("Initiating autonomous Degauss sequence...")
                    self.degauss_active = True
                    self.degauss_step_idx = 0
                    self.degauss_last_step_ts = 0.0
                    self.hw.set_output(True)
                    cmd.ok("degauss started")
                else:
                    cmd.ok("nothing to do")
            else:
                cmd.reject(f"unsupported command '{leaf}'")
        except RatingsUnknown as e:     # the driver refused it (no ratings): nothing wrong with the link
            cmd.reject(str(e))
        except ValueError as e:         # apply_smart_setpoint's hard-cap check (the last line)
            cmd.reject(str(e))
        except IO_ERRORS as e:   # PSU write failed part-way (anything else: DeviceService answers 'internal error')
            cmd.fail(f"PSU write failed: {e}")
            self.log.warning("Command %s failed: %s", cmd, e)
            self.hw.connected = False

    # ------------------------------------------------------------------ telemetry
    def publish_unknown(self):
        b = self.base
        self.state[f"{b}.stat_comms_fail"] = 1.0
        for key in ("rb_voltage", "rb_current", "stat_enabled", "rb_resistance", "stat_psu_overtemp",
                    "stat_psu_powerfail", "stat_psu_ovp", "stat_psu_ovc", "stat_short_circuit",
                    "stat_open_circuit", "stat_unexpected_res"):
            self.state[f"{b}.{key}"] = None
        self.state[f"{b}.stat_degaussing"] = 1.0 if self.degauss_active else 0.0

    def poll_device(self):
        b = self.base
        telemetry = self.hw.read_telemetry()
        if telemetry is None:
            self.publish_unknown()
            return
        v_rb, i_rb, output_on, alarms = telemetry      # v/i None: the PSU's ratings aren't known yet
        self._report_ratings()
        self.state[f"{b}.stat_comms_fail"] = 0.0
        self.state[f"{b}.rb_voltage"] = None if v_rb is None else round(v_rb, 3)
        self.state[f"{b}.rb_current"] = None if i_rb is None else round(i_rb, 3)
        self.state[f"{b}.stat_enabled"] = 1.0 if output_on else 0.0
        self.state[f"{b}.stat_degaussing"] = 1.0 if self.degauss_active else 0.0
        for key in ("sp_actual_voltage", "sp_actual_current"):
            if f"{b}.{key}" in self.sp_cache:
                self.state[f"{b}.{key}"] = self.sp_cache[f"{b}.{key}"]

        self.state[f"{b}.stat_psu_overtemp"] = 1.0 if alarms["OT"] else 0.0
        self.state[f"{b}.stat_psu_powerfail"] = 1.0 if alarms["PF"] else 0.0
        self.state[f"{b}.stat_psu_ovp"] = 1.0 if alarms["OVP"] else 0.0
        self.state[f"{b}.stat_psu_ovc"] = 1.0 if alarms["OCP"] else 0.0
        if any(alarms.values()):
            active = ", ".join(k for k, v in alarms.items() if v)
            if (n := self.throttle.ready(f"alarm-{active}")) is not None:
                self.events.log_general(f"{self.kind.label} PSU alarm: {active} (auto-clearing){Throttle.suffix(n)}",
                                        severity="WARNING")
            try:
                self.hw.clear_alarms()
            except IO_ERRORS as e:   # best effort; the alarm stays visible in the flags above
                self.log.warning("Alarm clear failed: %s", e)

        if v_rb is None or i_rb is None:        # nothing to judge the coil by
            for key in ("rb_resistance", "stat_short_circuit", "stat_open_circuit", "stat_unexpected_res"):
                self.state[f"{b}.{key}"] = None
            return
        short = opened = unexpected = False
        settled = time.time() - self.last_setpoint_ts > SETTLE_S
        if output_on and i_rb > self.limits["min_current_for_calc"] and settled:
            resistance = v_rb / i_rb
            self.state[f"{b}.rb_resistance"] = round(resistance, 3)
            if resistance < self.limits["short_circuit_threshold"]:
                short = True
            elif resistance > self.limits["open_circuit_threshold"]:
                opened = True
            elif abs(resistance - self.limits["nominal_resistance"]) > self.limits["resistance_tolerance"]:
                unexpected = True
        else:
            self.state[f"{b}.rb_resistance"] = 0.0
        self.state[f"{b}.stat_short_circuit"] = 1.0 if short else 0.0
        self.state[f"{b}.stat_open_circuit"] = 1.0 if opened else 0.0
        self.state[f"{b}.stat_unexpected_res"] = 1.0 if unexpected else 0.0

    def _report_ratings(self):
        """An event when the PSU's nominal ratings go missing, and when they arrive again: without
        them the driver takes only 0 A / output off (the commands say why, but this says it once)."""
        if self.hw.ratings_ok:
            state = "ok"
        else:
            state = "retrying" if self.hw.ratings_retrying else "waiting"
        if state == self._ratings_seen:
            return
        before, self._ratings_seen = self._ratings_seen, state
        if state == "ok":
            if before is not None:
                self.events.log_general(f"{self.kind.label} PSU nominal ratings read ({self.hw.nom_v:g} V, "
                                        f"{self.hw.nom_i:g} A): setpoints accepted again")
            return
        again = ("they are asked for again every few seconds" if state == "retrying" else
                 "it didn't answer when asked, so they are only asked for again when the service reconnects - "
                 "restart the service to ask now")
        self.events.log_general(f"{self.kind.label} PSU didn't report its nominal ratings: until it does, only "
                                f"0 A and output off are accepted and its V/I readbacks are unknown ({again})",
                                severity="WARNING")

    def run(self):
        self.events.log_general(f"[{self.kind.label} Service] starting"
                                + (" (SIMULATED PSU)" if simulation_enabled() else ""))
        super().run()


def main(kind: str = "magnet"):
    setup_logging(KINDS[kind].name)
    harden_windows_process()
    MagnetPsuService(kind).run()


MagnetMicroservice = MagnetPsuService     # old name


if __name__ == "__main__":
    main("magnet")
