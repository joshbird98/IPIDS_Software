"""Simulated Leybold TURBOVAC (USS protocol) for service_source_turbo.

transaction(pnu, index, value, ak, control_word) -> (pwe, ak, zsw) exactly as the
service's own _transaction() returns. The pump spins up while the control word
has the ON bit set, coasts down otherwise, and reports the vent/relay strategy
parameters the service checks as already configured (so no flash write).

Injected faults (SINDIE_SIM_FAULTS): turbo_disconnect, turbo_error.
"""
import threading
import time

from src.sim import Clock, injected_faults, noisy

MAX_HZ = 820
_PARAMS = {1: 3, 2: 123456, 18: MAX_HZ, 134: 19, 29: 4, 247: 500, 248: 5, 182: 0}


class FakeUSSPump:
    def __init__(self):
        self.lock = threading.Lock()
        self.hz = 0.0
        self.clock = Clock()
        self.op_hours = 1234.56
        self.error_code = 0

    def _update(self, control_word):
        dt = self.clock.dt()
        running = bool(control_word & 0x0001) and self.error_code == 0
        if control_word & 0x0080:      # fault reset
            self.error_code = 0
        if "turbo_error" in injected_faults():
            self.error_code = 6        # run-up time error
            running = False
        if running:
            self.hz = min(MAX_HZ, self.hz + dt * MAX_HZ / 40.0)   # ~40 s run-up
        else:
            self.hz = max(0.0, self.hz - dt * MAX_HZ / 90.0)      # ~90 s coast-down
        self.op_hours += dt / 3600.0 if self.hz > 0 else 0.0

    def _zsw(self):
        zsw = 0
        if self.error_code == 0:
            zsw |= 1 << 0
        else:
            zsw |= 1 << 3
        if self.hz > 1:
            zsw |= 1 << 11
        return zsw

    def transaction(self, pnu, index, value=0, ak=1, control_word=0x0400):
        if "turbo_disconnect" in injected_faults():
            time.sleep(1.0)
            return None, None, None
        time.sleep(0.01)
        with self.lock:
            self._update(control_word)
            if ak in (2, 7):   # parameter writes are accepted and echoed
                return value, 2, self._zsw()
            if pnu == 3:
                pwe = int(round(self.hz))
            elif pnu == 4:
                pwe = 48
            elif pnu == 5:
                pwe = int(noisy(30 if 1 < self.hz < MAX_HZ - 1 else 8 if self.hz > 1 else 0, 0.05))
            elif pnu == 11:
                pwe = int(30 + self.hz / 60)
            elif pnu == 125:
                pwe = int(28 + self.hz / 80)
            elif pnu == 184:
                pwe = int(self.op_hours * 100)
            elif pnu == 171:
                pwe = self.error_code
            elif pnu == 227:
                pwe = 0
            else:
                pwe = _PARAMS.get(pnu, 0)
            return pwe, 1, self._zsw()


_PUMP = FakeUSSPump()


def shared_pump() -> FakeUSSPump:
    return _PUMP


class FakeUssLink:
    """Drop-in for src.drivers.turbo_uss.UssLink talking to the shared simulated pump."""

    def __init__(self, ip=None, port=None, timeout=1.0):
        self.ip, self.port = ip, port
        self.connected = False
        self.on_problem = lambda message: None
        self.pump = shared_pump()

    def connect(self):
        if "turbo_disconnect" in injected_faults():
            raise ConnectionError("simulated: Waveshare bridge not reachable")
        self.connected = True

    def close(self):
        self.connected = False

    def transaction(self, pnu, index, value=0, ak=1, control_word=0x0400):
        if not self.connected:
            return None, None, None
        return self.pump.transaction(pnu, index, value, ak, control_word)
