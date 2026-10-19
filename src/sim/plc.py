"""Simulated S7-1200 for service_plc: a byte-accurate copy of the interface DB
(layout taken from config/system_tags.json) plus a small model of the PLC
program, so the PLC service's own read/parse/write code runs unchanged.

Modelled behaviour (from Josh's description of the machine, Oct 2026)
  * cycle counter, PC->PLC watchdog (relay drops + PC_Watchdog_Timeout fault if
    the PC stops writing ion_beam.system.plc_watchdog for 3 s)
  * safety relay: cmd_enable_safety requests it, the panel-start button is assumed pressed
    0.5 s later. It needs the gate valve open (and PC comms, no shutdown). Once in, it holds
    until the operator drops it or a permissive goes - closing the gate valve trips it. A
    trip is latched like the real contactor loop: the PLC clears the request (and the
    supplies' enables and the cesium heater, so nothing comes back on by itself) and sets
    Safety_Relay_Tripped until fault reset; the relay has to be enabled again.
  * thermionic: when enabled its voltage goes to 2500 V; the emission current is set by the
    filament temperature (~3 mA at 25 A filament, ~150 mA at 29 A, exponential in between,
    Richardson-Dushman style). Auto-emission: the PLC drives the filament current to hold the
    thermionic current setpoint (the filament setpoint is ignored meanwhile; on leaving auto
    the filament setpoint takes over the last auto value, bumplessly).
  * cesium oven: heater on -> towards its setpoint; forced cooling -> towards 20 degC;
    otherwise slowly back to ambient. The PLC's thermal model (estimated_temp) always runs;
    the thermocouple readback (rb_temp) needs the source deck powered.
  * source deck (cesium oven, filament, thermionic/ioniser): powered through the safety relay,
    so with the relay off there is no readback from it (reported as 0 here; the PLC service
    publishes them as unknown).
  * while the cesium is hot (internal.cs_is_hot, from the estimated temperature) the PLC keeps
    the target and the ioniser (filament + thermionic) on - a request to switch them off is
    held back until it has cooled - and the target voltage at least internal.target_min_voltage,
    so cesium vapour stays ionised and directed instead of coating the isolator ceramics.
    ASSUMED from Josh's description (Oct 2026): the threshold (CS_HOT_C) and exactly what the
    real PLC holds are guesses - check against the PLC program.
  * extraction / target: 50 MOhm bleed resistors (current proportional to voltage) plus,
    SNICS-style, a target (sputter) current once thermionic > 200 W, target > 4 kV and
    cesium > 50 degC: ~0.1 mA there, ~1 mA at 250 W / 8 kV / 100 degC. The extracted beam is
    ~10 % of it and shows in the extraction current (the simulated Faraday cup scales with it).
  * steering, gate valve (only opens with the vacuum permissive), Faraday cup.
    ASSUMED: OPEN (cmd_open_gv) is a standing request - the valve closes when the permissive goes
    and would open again by itself 2 s after it returns. The PLC service clears the request once
    the valve has been shut by its interlock for 3 s, so it stays shut until OPEN is pressed again
    (Josh, Oct 2026). Check the real program's seq_cmd_open_gv against this.
  * comms-fail flags and the source einzel's status/arc flags written by the PC are
    mirrored into the fault words
"""
import json
import math
import threading

from snap7.util import get_bool, get_dword, get_int, get_real, set_bool, set_dint, set_dword, set_int, set_real

from src.core import paths
from src.sim import Clock, first_order, injected_faults, noisy

_SIZES = {"BOOL": 1, "BYTE": 1, "INT": 2, "UINT": 2, "WORD": 2, "DINT": 4, "UDINT": 4, "DWORD": 4, "REAL": 4,
          "TIME": 4}

SRC = "ion_beam.source"
HV_SUPPLIES = (f"{SRC}.extraction", f"{SRC}.target", f"{SRC}.filament", f"{SRC}.thermionic")
BLEED_MA_PER_KV = 1.0 / 50.0       # 50 MOhm bleed resistor: kV / MOhm = mA
THERMIONIC_V = 2500.0
FILAMENT_OHMS = 0.35
FILAMENT_SLEW_A_S = 1.0            # the filament supply ramps rather than jumps
FILAMENT_HEAT_TAU = 2.0            # the filament's temperature lags its current
FILAMENT_AUTO_MAX_A = 31.0
EMISSION_MAX_MA = 200.0            # the thermionic supply's own current limit
CS_AMBIENT, CS_FORCED = 22.0, 20.0
CS_HOT_C = 50.0                    # assumed: estimated cesium temperature the PLC treats as "hot"


def emission_ma(filament_a: float) -> float:
    """Thermionic emission for a filament current: 3 mA at 25 A, 150 mA at 29 A."""
    if filament_a < 18.0:
        return 0.0
    return 3.0 * 50.0 ** ((filament_a - 25.0) / 4.0)


def target_beam_ma(thermionic_w: float, target_kv: float, cesium_c: float) -> float:
    """Sputter (target) current: none until 200 W / 4 kV / 50 degC, 0.1 mA there, 1 mA at
    250 W / 8 kV / 100 degC (geometric in between, a little beyond)."""
    if thermionic_w <= 200.0 or target_kv <= 4.0 or cesium_c <= 50.0:
        return 0.0
    a = min((thermionic_w - 200.0) / 50.0, 1.5)
    b = min((target_kv - 4.0) / 4.0, 1.5)
    c = min((cesium_c - 50.0) / 50.0, 1.5)
    return 0.1 * 10.0 ** ((a + b + c) / 3.0)


class SimulatedPLC:
    def __init__(self, registry_path=None):
        registry_path = registry_path or paths.config_path("system_tags.json")
        with open(registry_path, "r") as f:
            reg = json.load(f)
        self.tags = {t: m for t, m in reg.items() if m.get("source") == "service_plc_snap7" and "byte_offset" in m}
        size = 0
        for m in self.tags.values():
            size = max(size, m["byte_offset"] + _SIZES.get(m.get("datatype"), 2))
        self.dbs = {}
        for m in self.tags.values():
            db = m.get("db_number")
            self.dbs.setdefault(db, bytearray(size + (size % 2) + 16))
        self.lock = threading.RLock()
        self.clock = Clock()
        self.cycle = 0
        self._wd_last_value = None
        self._wd_last_change = None
        self._relay_request_t = None
        self._relay_tripped = False      # latched until fault reset
        self._relay_refused = False
        self._fil_heat = 0.0             # filament temperature, as the current it corresponds to
        self._auto_fil = None            # filament current the auto-emission loop is driving
        self._sputter = 0.0              # target (sputter) current, mA
        self._kv = {}                    # extraction / target output without readback noise
        self._held = set()               # supplies the PLC keeps on while the cesium is hot
        self._gv_cmd_t = None
        self._last_fault_flags = {}
        with open(paths.config_path("fault_config.json"), "r") as f:
            self.faults = json.load(f)
        self._init_values()

    # ------------------------------------------------------------------ raw access
    def _meta(self, tag):
        return self.tags.get(tag)

    def get(self, tag, default=None):
        m = self._meta(tag)
        if m is None:
            return default
        buf = self.dbs[m["db_number"]]
        o, dt = m["byte_offset"], m.get("datatype")
        if dt == "BOOL":
            return 1.0 if get_bool(buf, o, m.get("bit_offset", 0)) else 0.0
        if dt == "REAL":
            return get_real(buf, o)
        if dt == "INT":
            return float(get_int(buf, o))
        if dt in ("DINT", "UDINT", "DWORD"):
            return float(get_dword(buf, o))
        return default

    def set(self, tag, value):
        m = self._meta(tag)
        if m is None:
            return
        buf = self.dbs[m["db_number"]]
        o, dt = m["byte_offset"], m.get("datatype")
        if dt == "BOOL":
            set_bool(buf, o, m.get("bit_offset", 0), bool(value))
        elif dt == "REAL":
            set_real(buf, o, float(value))
        elif dt == "INT":
            set_int(buf, o, int(value))
        elif dt in ("DINT", "UDINT"):
            set_dint(buf, o, int(value) & 0x7FFFFFFF)
        elif dt == "DWORD":
            set_dword(buf, o, int(value) & 0xFFFFFFFF)

    def _init_values(self):
        for tag in ("ion_beam.facilities.stat_air_press_ok", "ion_beam.facilities.stat_src_coolant_ok",
                    "ion_beam.facilities.stat_mag_coolant_ok", "ion_beam.beamline.diagnostics.stat_fc_retracted",
                    "ion_beam.source.chamber.stat_gv_closed", "ion_beam.source.chamber.stat_gv_close_permissive"):
            self.set(tag, 1)
        self.set("ion_beam.source.chamber.rb_body_temp", 24.0)
        self.set("ion_beam.source.cesium.rb_temp", 22.0)
        self.set("ion_beam.source.cesium.estimated_temp", 22.0)
        self.set("ion_beam.internal.target_min_voltage", 0.5)

    # ------------------------------------------------------------------ model
    def step(self):
        with self.lock:
            dt = self.clock.dt()
            self.cycle += max(1, int(dt * 1000))
            self.set("ion_beam.system.plc_cycle_count", self.cycle)

            # PC -> PLC watchdog: the real PLC trips the relay if the PC stops counting
            wd = self.get("ion_beam.system.plc_watchdog")
            now = self.clock.last
            if wd != self._wd_last_value:
                self._wd_last_value, self._wd_last_change = wd, now
            comms_lost = self._wd_last_change is not None and now - self._wd_last_change > 3.0
            self.set("ion_beam.system.pc_plc_comms_lost", comms_lost)

            if self.get("ion_beam.system.cmd_fault_reset"):
                self._relay_tripped = self._relay_refused = False
                self.set("ion_beam.system.cmd_fault_reset", 0)      # a pulse: the PLC clears it

            self._gate_valve(now)
            relay = self._safety_relay(now, comms_lost)
            self.set("ion_beam.system.machine_state", 2 if relay else 1)

            self._source(relay, dt)
            self._steering(relay)
            fc = bool(self.get("ion_beam.beamline.diagnostics.cmd_insert_fc"))
            self.set("ion_beam.beamline.diagnostics.stat_fc_inserted", fc)
            self.set("ion_beam.beamline.diagnostics.stat_fc_retracted", not fc)
            for base in ("ion_beam.beamline.diagnostics", "ion_beam.beamline.steering"):
                self.set(f"{base}.ctrl_mode", self.get(f"{base}.cmd_ctrl_mode", 0.0))
            self.set("ion_beam.source.chamber.rb_body_temp",
                     noisy(24.0 + 0.002 * self.get("ion_beam.source.filament.rb_current", 0.0) ** 2, 0.001))
            self._fault_words(comms_lost)

    def _safety_relay(self, now, comms_lost):
        """Pulls in on request + panel start (assumed 0.5 s later) with the gate valve open,
        then holds itself until dropped or a permissive goes (a latched trip)."""
        want = bool(self.get("ion_beam.system.cmd_enable_safety"))
        relay = bool(self.get("ion_beam.facilities.safety_relay_active"))
        permissive = (bool(self.get("ion_beam.source.chamber.stat_gv_open")) and not comms_lost
                      and not self.get("ion_beam.system.cmd_source_shutdown")
                      and "plc_relay_trip" not in injected_faults())
        if relay:
            if not want:
                relay = False                                   # the operator dropped it
            elif not permissive:
                relay = False
                self._relay_tripped = True
                self.set("ion_beam.system.cmd_enable_safety", 0)
            if not relay:
                self._drop_outputs()
        elif want:
            if not permissive:
                self._relay_refused = True                      # e.g. gate valve not open
                self._relay_request_t = None
                self.set("ion_beam.system.cmd_enable_safety", 0)
            else:
                if self._relay_request_t is None:
                    self._relay_request_t = now
                if now - self._relay_request_t > 0.5:
                    relay = True
                    self._relay_request_t = None
        else:
            self._relay_request_t = None
        waiting = self._relay_request_t is not None
        self.set("ion_beam.system.safety_timer_total_ms", 5000)
        self.set("ion_beam.system.safety_timer_elapsed_ms",
                 int((now - self._relay_request_t) * 1000) if waiting else 0)
        self.set("ion_beam.facilities.safety_relay_active", relay)
        return relay

    def _drop_outputs(self):
        """Relay gone: the PLC clears the enables, so nothing comes back on with the relay."""
        for base in HV_SUPPLIES + ("ion_beam.beamline.steering",):
            self.set(f"{base}.cmd_enable", 0)
        self.set(f"{SRC}.thermionic.cmd_auto_emission", 0)
        self.set(f"{SRC}.cesium.cmd_ctrl_mode", 0)

    # ------------------------------------------------------------------ ion source
    def _setpoint(self, base, quantity):
        sp = self.get(f"{base}.sp_requested_{quantity}", 0.0)
        lim = self.tags.get(f"{base}.sp_requested_{quantity}", {})
        if lim.get("max_val") is not None:
            sp = min(sp, lim["max_val"])
        return max(0.0, sp)

    def _enabled(self, base, relay):
        enabled = (bool(self.get(f"{base}.cmd_enable")) or base in self._held) and relay
        self.set(f"{base}.stat_enabled", enabled)
        self.set(f"{base}.ctrl_mode", self.get(f"{base}.cmd_ctrl_mode", 0.0))
        return enabled

    def _source(self, relay, dt):
        fil, th, tgt = f"{SRC}.filament", f"{SRC}.thermionic", f"{SRC}.target"
        cs_hot = self.get(f"{SRC}.cesium.estimated_temp", CS_AMBIENT) >= CS_HOT_C
        self.set("ion_beam.internal.cs_is_hot", cs_hot)
        for base in (tgt, th, fil):          # cesium hot: what is on stays on
            if cs_hot and relay and self.get(f"{base}.stat_enabled"):
                self._held.add(base)
            elif not cs_hot or not relay:
                self._held.discard(base)
        fil_on, th_on = self._enabled(fil, relay), self._enabled(th, relay)

        # thermionic: fixed 2500 V; its current is whatever the filament emits
        th_v = first_order(self.get(f"{th}.rb_voltage", 0.0), THERMIONIC_V if th_on else 0.0, dt, 0.3)
        fil_rb = self.get(f"{fil}.rb_current", 0.0)
        self._fil_heat = first_order(self._fil_heat, fil_rb, dt, FILAMENT_HEAT_TAU)
        th_i = emission_ma(self._fil_heat) * min(1.0, th_v / THERMIONIC_V) ** 1.5
        th_i = max(0.0, noisy(min(EMISSION_MAX_MA, th_i), 0.01))
        th_sp = self._setpoint(th, "current")
        self.set(f"{th}.sp_actual_current", th_sp)
        th_w = th_v * th_i / 1000.0
        self.set(f"{th}.rb_voltage", max(0.0, noisy(th_v, 0.0005)) if relay else 0.0)   # deck unpowered:
        self.set(f"{th}.rb_current", th_i if relay else 0.0)                              # no readback
        self.set(f"{th}.rb_power", th_w if relay else 0.0)

        # filament: the operator's setpoint, or the auto-emission loop's
        auto = bool(self.get(f"{th}.cmd_auto_emission"))
        self.set(f"{th}.stat_auto_emission", auto)
        if auto:
            if self._auto_fil is None:
                self._auto_fil = self.get(f"{fil}.sp_actual_current", 0.0)
            if fil_on and th_on:
                err = math.log(max(th_sp, 0.01) / max(th_i, 0.01))
                self._auto_fil += max(-0.5, min(0.3, 0.12 * err)) * dt
                self._auto_fil = max(0.0, min(FILAMENT_AUTO_MAX_A, self._auto_fil))
            fil_sp = self._auto_fil
        else:
            if self._auto_fil is not None:            # leaving auto: carry on from where it got to
                self.set(f"{fil}.sp_requested_current", self._auto_fil)
                self._auto_fil = None
            fil_sp = self._setpoint(fil, "current")
        self.set(f"{fil}.sp_actual_current", fil_sp)
        aim = fil_sp if fil_on else 0.0
        ramp = FILAMENT_SLEW_A_S * dt
        fil_rb = fil_rb + max(-2.0 * ramp, min(ramp, aim - fil_rb)) if relay else 0.0   # contactor open
        self.set(f"{fil}.stat_disarming", relay and not fil_on and fil_rb > 0.05)        # ramping down
        fil_v = max(0.0, noisy(fil_rb * FILAMENT_OHMS, 0.002))
        self.set(f"{fil}.rb_current", fil_rb)
        self.set(f"{fil}.rb_voltage", fil_v)
        self.set(f"{fil}.rb_power", fil_rb * fil_v)
        self.set(f"{fil}.resistance", FILAMENT_OHMS if fil_rb > 0.1 else 0.0)
        self.set(f"{fil}.out_cv_current", fil_rb)

        cesium_c = self._cesium(relay, dt)

        # extraction and target: bleed + (SNICS) target current; extracted beam ~10 % of it
        kv = {}
        for name in ("extraction", "target"):
            base = f"{SRC}.{name}"
            on = self._enabled(base, relay)
            sp = self._setpoint(base, "voltage")
            if name == "target" and cs_hot and on:
                sp = max(sp, self.get("ion_beam.internal.target_min_voltage", 0.0))
            self.set(f"{base}.sp_actual_voltage", sp)
            kv[name] = first_order(self._kv.get(name, 0.0), sp if on else 0.0, dt, 0.4)
        self._kv = kv
        want = target_beam_ma(th_w, kv["target"], cesium_c)
        self._sputter = first_order(self._sputter, want, dt, 3.0 if want > self._sputter else 0.3)
        extracted = 0.1 * self._sputter * min(1.0, kv["extraction"] / 5.0)
        for name, beam in (("extraction", extracted), ("target", self._sputter)):
            base = f"{SRC}.{name}"
            v = max(0.0, noisy(kv[name], 0.001))
            i = max(0.0, noisy(kv[name] * BLEED_MA_PER_KV, 0.002) + noisy(beam, 0.05))
            self.set(f"{base}.rb_voltage", v)
            self.set(f"{base}.rb_current", i)
            self.set(f"{base}.rb_power", v * i)

    def _cesium(self, relay, dt):
        base = f"{SRC}.cesium"
        heating = int(self.get(f"{base}.cmd_ctrl_mode", 0.0)) == 1 and relay
        cooling = bool(self.get(f"{base}.cmd_force_cooling"))
        self.set(f"{base}.ctrl_mode", self.get(f"{base}.cmd_ctrl_mode", 0.0))
        self.set(f"{base}.stat_enabled", heating)
        sp = self.get(f"{base}.sp_requested_temp", 0.0)
        self.set(f"{base}.sp_actual_temp", sp)
        temp = self.get(f"{base}.estimated_temp", CS_AMBIENT)
        if heating:
            target, tau = sp, 60.0
        elif cooling:
            target, tau = CS_FORCED, 40.0
        else:
            target, tau = CS_AMBIENT, 300.0
        temp = first_order(temp, target, dt, tau)
        # The thermocouple is read on the source deck: no readback without the relay. The PLC's
        # thermal model (estimated_temp) keeps tracking it either way.
        self.set(f"{base}.rb_temp", noisy(temp, 0.0005) if relay else 0.0)
        self.set(f"{base}.estimated_temp", temp)
        self.set(f"{base}.out_cv_heating", max(0.0, min(100.0, (sp - temp) * 5.0)) if heating else 0.0)
        self.set(f"{base}.stat_cooling_active", cooling)
        return temp

    def _steering(self, relay):
        base = "ion_beam.beamline.steering"
        enabled = bool(self.get(f"{base}.cmd_enable")) and relay
        self.set(f"{base}.stat_enabled", enabled)
        for axis in ("x", "y"):
            sp = self.get(f"{base}.sp_requested_{axis}_volts", 0.0)
            self.set(f"{base}.sp_actual_{axis}_volts", sp if enabled else 0.0)

    def _gate_valve(self, now):
        base = "ion_beam.source.chamber"
        permissive = bool(self.get(f"{base}.stat_vac_ok_for_gv"))
        self.set(f"{base}.stat_gv_open_permissive", permissive)
        want_open = bool(self.get(f"{base}.cmd_open_gv"))
        is_open = bool(self.get(f"{base}.stat_gv_open"))
        if want_open and permissive and not is_open:
            if self._gv_cmd_t is None:
                self._gv_cmd_t = now
            elif now - self._gv_cmd_t > 2.0:
                is_open = True
        elif (not want_open or not permissive) and is_open:
            is_open = False
            self._gv_cmd_t = None
        if not want_open:
            self._gv_cmd_t = None
        self.set(f"{base}.stat_gv_open", is_open)
        self.set(f"{base}.stat_gv_closed", not is_open and not (want_open and self._gv_cmd_t is not None))

    def _fault_words(self, comms_lost):
        active = {
            "PC_Watchdog_Timeout": comms_lost,
            "Safety_Relay_Refused": self._relay_refused,
            "Safety_Relay_Tripped": self._relay_tripped,
            "Src_Turbo_Comms_Fail": bool(self.get("ion_beam.source.turbo_pump.stat_comms_fail")),
            "Graphix_1_Comms_Fail": bool(self.get("ion_beam.facilities.graphix1.stat_comms_fail")),
            "Graphix_2_Comms_Fail": bool(self.get("ion_beam.facilities.graphix2.stat_comms_fail")),
            "Unit1_Comms_Fail": bool(self.get("ion_beam.source.einzel.stat_comms_fail")),
            "Unit1_OverCurrent": bool(self.get("ion_beam.source.einzel.stat_overcurrent")),
            "Unit1_OverVoltage": bool(self.get("ion_beam.source.einzel.stat_overvoltage")),
            "Unit1_Fail": bool(self.get("ion_beam.source.einzel.stat_fail")),
            "Unit1_Arc_Exceeded": bool(self.get("ion_beam.source.einzel.stat_arc_exceeded")),
            "Magnet_Comms_Fail": bool(self.get("ion_beam.beamline.magnet.stat_comms_fail")),
            "Str_Magnet_Comms_Fail": bool(self.get("ion_beam.beamline.steerer_magnet.stat_comms_fail")),
        }
        for udt in self.faults.values():
            tag = udt.get("registry_tag")
            m = self._meta(tag)
            if m is None:
                continue
            buf = self.dbs[m["db_number"]]
            for pos, bit in udt.get("bits", {}).items():
                byte_idx, bit_idx = (int(x) for x in pos.split("."))
                set_bool(buf, m["byte_offset"] + byte_idx, bit_idx, bool(active.get(bit["name"], False)))


_PLC = None
_PLC_LOCK = threading.Lock()


def shared_plc() -> SimulatedPLC:
    """One simulated PLC per process: client objects come and go on reconnect, the PLC keeps its state."""
    global _PLC
    with _PLC_LOCK:
        if _PLC is None:
            _PLC = SimulatedPLC()
        return _PLC


class FakeSnap7Client:
    """Drop-in for snap7.client.Client covering what service_plc uses."""

    def __init__(self):
        self.plc = shared_plc()
        self._connected = False

    def connect(self, address, rack, slot, tcp_port=102):
        if "plc_disconnect" in injected_faults():
            raise RuntimeError(f"simulated: PLC at {address} not reachable")
        self._connected = True
        return self

    def disconnect(self):
        self._connected = False
        return 0

    def get_connected(self):
        return self._connected and "plc_disconnect" not in injected_faults()

    def db_read(self, db_number, start, size):
        if not self.get_connected():
            raise RuntimeError("simulated: not connected")
        self.plc.step()
        with self.plc.lock:
            buf = self.plc.dbs.setdefault(db_number, bytearray(max(start + size, 512)))
            if len(buf) < start + size:
                buf.extend(bytearray(start + size - len(buf)))
            return bytearray(buf[start:start + size])

    def db_write(self, db_number, start, data):
        if not self.get_connected():
            raise RuntimeError("simulated: not connected")
        with self.plc.lock:
            buf = self.plc.dbs.setdefault(db_number, bytearray(max(start + len(data), 512)))
            if len(buf) < start + len(data):
                buf.extend(bytearray(start + len(data) - len(buf)))
            buf[start:start + len(data)] = data
        return 0


__all__ = ["FakeSnap7Client", "SimulatedPLC", "shared_plc"]
