"""Simulated Modbus-TCP magnet power supply (main analyser magnet and the X steerer).

Same interface as MagnetModbusProtocol in the two magnet services: an
inductive load (current follows the setpoint with a ~1 s time constant),
voltage = I x R, and constant-voltage limiting when the voltage setpoint is
too low for the requested current.

Injected faults (SINDIE_SIM_FAULTS): magnet_disconnect, steerer_disconnect. Setting
ratings_ok False makes it behave like the real driver when the PSU's nominal ratings are unknown:
only zero and output off are accepted, and the V/I readbacks are None.
"""
import threading

from src.drivers.magnet_modbus import RatingsUnknown
from src.sim import Clock, first_order, injected_faults, noisy

_STATE = {}
_LOCK = threading.Lock()


class _PSU:
    def __init__(self, resistance):
        self.resistance = resistance
        self.v_set = 0.0
        self.i_set = 0.0
        self.output = False
        self.i_out = 0.0
        self.clock = Clock()

    def update(self, sag=False):
        dt = self.clock.dt()
        target = 0.0
        if self.output:
            target = min(self.i_set, self.v_set / self.resistance if self.resistance > 0 else self.i_set)
            if sag:
                target *= 0.9
        self.i_out = first_order(self.i_out, target, dt, 1.0)


class FakeMagnetModbus:
    def __init__(self, ip: str, port: int = 502, name: str = "magnet", resistance: float = 0.16,
                 nom_v: float = 30.0, nom_i: float = 100.0):
        self.ip = ip
        self.port = port
        self.name = name
        self.connected = False
        self.ratings_ok = True          # the real driver's flags: nominal ratings known, and if not
        self.ratings_retrying = True    # whether they are asked for again without a reconnect
        self.nom_v = nom_v
        self.nom_i = nom_i
        self.nom_p = nom_v * nom_i
        with _LOCK:
            self.psu = _STATE.setdefault((ip, port), _PSU(resistance))

    def _fault(self):
        return f"{self.name}_disconnect" in injected_faults()

    def connect(self) -> bool:
        if self._fault():
            self.connected = False
            return False
        self.connected = True
        return True

    def disconnect(self):
        if self.connected:
            with _LOCK:
                self.psu.output = False
        self.connected = False

    def _check(self):
        if self._fault():
            self.connected = False
            raise ConnectionError(f"simulated: {self.name} PSU at {self.ip} not responding")
        if not self.connected:
            raise ConnectionError("not connected")

    def _require_ratings(self, what: str):
        if not self.ratings_ok:
            raise RatingsUnknown(f"{what} refused: the PSU's nominal ratings are unknown (simulated)")

    def set_voltage(self, v_set: float) -> bool:
        self._check()
        if v_set:
            self._require_ratings(f"voltage {v_set:g} V")
        with _LOCK:
            self.psu.update()
            self.psu.v_set = max(0.0, min(self.nom_v, float(v_set)))
        return True

    def set_current(self, i_set: float) -> bool:
        self._check()
        if i_set:
            self._require_ratings(f"current {i_set:g} A")
        with _LOCK:
            self.psu.update()
            self.psu.i_set = max(0.0, min(self.nom_i, float(i_set)))
        return True

    def set_output(self, state: bool) -> bool:
        self._check()
        if state:
            self._require_ratings("output on")
        with _LOCK:
            self.psu.update()
            self.psu.output = bool(state)
        return True

    def clear_alarms(self) -> bool:
        self._check()
        return True

    def read_setpoints(self):
        self._check()
        if not self.ratings_ok:
            return None
        with _LOCK:
            return self.psu.v_set, self.psu.i_set

    def read_telemetry(self):
        if not self.connected:
            return None
        if self._fault():
            self.connected = False
            return None
        with _LOCK:
            self.psu.update(sag=f"{self.name}_sag" in injected_faults())
            i = max(0.0, noisy(self.psu.i_out, 0.0005, 0.001))
            v = max(0.0, noisy(self.psu.i_out * self.psu.resistance, 0.001, 0.002))
            alarms = {"OVP": False, "OCP": False, "OPP": False, "OT": False, "PF": False}
            if not self.ratings_ok:
                return None, None, self.psu.output, alarms
            return v, i, self.psu.output, alarms
