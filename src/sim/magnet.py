"""Simulated Modbus-TCP magnet power supply (main analyser magnet and the X steerer).

Same interface as MagnetModbusProtocol in the two magnet services: an
inductive load (current follows the setpoint with a ~1 s time constant),
voltage = I x R, and constant-voltage limiting when the voltage setpoint is
too low for the requested current.

Injected faults (SINDIE_SIM_FAULTS): magnet_disconnect, steerer_disconnect.
"""
import threading

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

    def set_voltage(self, v_set: float) -> bool:
        self._check()
        with _LOCK:
            self.psu.update()
            self.psu.v_set = max(0.0, min(self.nom_v, float(v_set)))
        return True

    def set_current(self, i_set: float) -> bool:
        self._check()
        with _LOCK:
            self.psu.update()
            self.psu.i_set = max(0.0, min(self.nom_i, float(i_set)))
        return True

    def set_output(self, state: bool) -> bool:
        self._check()
        with _LOCK:
            self.psu.update()
            self.psu.output = bool(state)
        return True

    def clear_alarms(self) -> bool:
        self._check()
        return True

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
            return v, i, self.psu.output, alarms
