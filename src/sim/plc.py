"""Simulated S7-1200 for service_plc: a byte-accurate copy of the interface DB
(layout taken from config/system_tags.json) plus a small model of the PLC
program, so the PLC service's own read/parse/write code runs unchanged.

Modelled behaviour
  * cycle counter, PC->PLC watchdog (relay drops + PC_Watchdog_Timeout fault if
    the PC stops writing ion_beam.system.plc_watchdog for 3 s)
  * safety relay follows cmd_enable_safety (0.5 s "panel start" delay)
  * extraction / target / filament / thermionic supplies, steering, cesium
    oven, gate valve (only opens with the vacuum permissive), Faraday cup
  * comms-fail flags written by the PC are mirrored into the fault words
"""
import json
import threading

from snap7.util import get_bool, get_dword, get_int, get_real, set_bool, set_dint, set_dword, set_int, set_real

from src.core import paths
from src.sim import Clock, first_order, injected_faults, noisy

_SIZES = {"BOOL": 1, "BYTE": 1, "INT": 2, "UINT": 2, "WORD": 2, "DINT": 4, "UDINT": 4, "DWORD": 4, "REAL": 4,
          "TIME": 4}

# supply base tag -> (primary quantity, secondary quantity, time constant s, secondary per primary)
_SUPPLIES = {
    "ion_beam.source.extraction": ("voltage", "current", 0.4, 0.04),    # kV -> mA leakage + beam
    "ion_beam.source.target": ("voltage", "current", 0.4, 0.02),
    "ion_beam.source.filament": ("current", "voltage", 0.8, 0.35),      # A -> V across ~0.35 ohm
    "ion_beam.source.thermionic": ("current", "voltage", 0.3, 0.6),     # mA -> V
}


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

            # Safety relay (the physical panel-start button is assumed pressed 0.5 s later)
            want = bool(self.get("ion_beam.system.cmd_enable_safety"))
            shutdown = bool(self.get("ion_beam.system.cmd_source_shutdown"))
            relay = bool(self.get("ion_beam.facilities.safety_relay_active"))
            if want and not relay and self._relay_request_t is None:
                self._relay_request_t = now
            if not want:
                self._relay_request_t = None
            if want and self._relay_request_t is not None and now - self._relay_request_t > 0.5:
                relay = True
            if not want or comms_lost or shutdown or "plc_relay_trip" in injected_faults():
                relay = False
            self.set("ion_beam.facilities.safety_relay_active", relay)
            self.set("ion_beam.system.machine_state", 2 if relay else 1)

            for base, (pri, sec, tau, k) in _SUPPLIES.items():
                self._supply(base, pri, sec, tau, k, relay, dt)
            self._cesium(relay, dt)
            self._steering(relay)
            self._gate_valve(now)
            fc = bool(self.get("ion_beam.beamline.diagnostics.cmd_insert_fc"))
            self.set("ion_beam.beamline.diagnostics.stat_fc_inserted", fc)
            self.set("ion_beam.beamline.diagnostics.stat_fc_retracted", not fc)
            for base in ("ion_beam.beamline.diagnostics", "ion_beam.beamline.steering"):
                self.set(f"{base}.ctrl_mode", self.get(f"{base}.cmd_ctrl_mode", 0.0))
            self.set("ion_beam.source.chamber.rb_body_temp",
                     noisy(24.0 + 0.002 * self.get("ion_beam.source.filament.rb_current", 0.0) ** 2, 0.001))
            self._fault_words(comms_lost, relay, want)

    def _supply(self, base, pri, sec, tau, k, relay, dt):
        enabled = bool(self.get(f"{base}.cmd_enable")) and relay
        self.set(f"{base}.stat_enabled", enabled)
        self.set(f"{base}.ctrl_mode", self.get(f"{base}.cmd_ctrl_mode", 0.0))
        sp = self.get(f"{base}.sp_requested_{pri}", 0.0)
        lim = self.tags.get(f"{base}.sp_requested_{pri}", {})
        if lim.get("max_val") is not None:
            sp = min(sp, lim["max_val"])
        self.set(f"{base}.sp_actual_{pri}", sp)
        rb = self.get(f"{base}.rb_{pri}", 0.0)
        rb = first_order(rb, sp if enabled else 0.0, dt, tau)
        rb_noisy = max(0.0, noisy(rb, 0.001))
        self.set(f"{base}.rb_{pri}", rb_noisy)
        sec_val = max(0.0, noisy(rb * k, 0.01))
        self.set(f"{base}.rb_{sec}", sec_val)
        power = rb_noisy * sec_val
        if base.endswith("thermionic"):
            power /= 1000.0  # mA * V
        self.set(f"{base}.rb_power", power)
        if base.endswith("filament"):
            self.set(f"{base}.resistance", 0.35 if rb > 0.1 else 0.0)
            self.set(f"{base}.out_cv_current", rb)
        if base.endswith("thermionic"):
            self.set(f"{base}.stat_auto_emission", self.get(f"{base}.cmd_auto_emission", 0.0))

    def _cesium(self, relay, dt):
        base = "ion_beam.source.cesium"
        heating = int(self.get(f"{base}.cmd_ctrl_mode", 0.0)) == 1 and relay
        self.set(f"{base}.ctrl_mode", self.get(f"{base}.cmd_ctrl_mode", 0.0))
        self.set(f"{base}.stat_enabled", heating)
        sp = self.get(f"{base}.sp_requested_temp", 0.0)
        self.set(f"{base}.sp_actual_temp", sp)
        temp = self.get(f"{base}.rb_temp", 22.0)
        target = sp if heating else 22.0
        cooling = bool(self.get(f"{base}.cmd_force_cooling"))
        temp = first_order(temp, target, dt, 25.0 if cooling else 60.0)
        self.set(f"{base}.rb_temp", noisy(temp, 0.0005))
        self.set(f"{base}.estimated_temp", temp)
        self.set(f"{base}.out_cv_heating", max(0.0, min(100.0, (target - temp) * 5.0)) if heating else 0.0)
        self.set(f"{base}.stat_cooling_active", cooling)

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

    def _fault_words(self, comms_lost, relay, want):
        active = {
            "PC_Watchdog_Timeout": comms_lost,
            "Safety_Relay_Tripped": want and not relay and comms_lost,
            "Src_Turbo_Comms_Fail": bool(self.get("ion_beam.source.turbo_pump.stat_comms_fail")),
            "Graphix_1_Comms_Fail": bool(self.get("ion_beam.facilities.graphix1.stat_comms_fail")),
            "Graphix_2_Comms_Fail": bool(self.get("ion_beam.facilities.graphix2.stat_comms_fail")),
            "Unit1_Comms_Fail": bool(self.get("ion_beam.source.einzel.stat_comms_fail")),
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
