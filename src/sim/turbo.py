"""Simulated Leybold TURBOVAC (USS protocol) for service_source_turbo.

transaction(pnu, index, value, ak, control_word) -> (pwe, ak, zsw) exactly as the
service's own _transaction() returns. The pump spins up while the control word
has the ON bit set, coasts down otherwise, and reports the vent/relay strategy
parameters the service checks as already configured (so no flash write).

Injected faults (SINDIE_SIM_FAULTS): turbo_disconnect, turbo_error, turbo_backing_closed (the valve
to the backing pump shut, as on 26 October 2026, but faster: the current up 0.4 A at once, then
0.1 A a minute, the temperatures following; the pump's overload warning from 4.5 A and an overload
failure at 6 A, 48 minutes in - its load guard warns about 16 minutes in, over 1.2 A for 15, and
trips the source about 36 minutes in).

At full speed it reads as the real pump did on 26 October 2026: 0.8 A, bearing 43 degC, converter
28 degC, 24.2 V (P4 is in 0.1 V).
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
        self.load_a = 0.0              # extra current from gas behind the pump (turbo_backing_closed)
        self.heat_c = 0.0              # ...and the extra temperature it brings

    def _update(self, control_word):
        dt = self.clock.dt()
        running = bool(control_word & 0x0001) and self.error_code == 0
        if control_word & 0x0080:      # fault reset
            self.error_code = 0
        faults = injected_faults()
        if "turbo_error" in faults:
            self.error_code = 6        # run-up time error
            running = False
        if "turbo_backing_closed" in faults and self.hz > 1:
            self.load_a = max(self.load_a, 0.4) + dt * 0.1 / 60.0     # 0.4 A at once, then 0.1 A a minute
            self.heat_c += dt * 0.45 / 60.0
        else:
            self.load_a = max(0.0, self.load_a - dt * 0.05)          # gone within a minute or so
            self.heat_c = max(0.0, self.heat_c - dt * 0.3 / 60.0)    # the temperatures much more slowly
        if self.load_a > 5.2 and self.error_code == 0:
            self.error_code = 106      # overload failure (speed below minimum)
            running = False
        if running:
            self.hz = min(MAX_HZ, self.hz + dt * MAX_HZ / 40.0)   # ~40 s run-up
        else:
            self.hz = max(0.0, self.hz - dt * MAX_HZ / 90.0)      # ~90 s coast-down
        self.op_hours += dt / 3600.0 if self.hz > 0 else 0.0

    def _warnings(self) -> int:
        """Warning bits (P227): overload from 4.5 A."""
        return 1 << 11 if self.load_a > 3.7 and self.hz > 1 else 0

    def _zsw(self):
        zsw = 0
        if self.error_code:
            zsw |= 1 << 3
        elif self.hz >= MAX_HZ - 1:    # normal operation, at speed: as the HMI reads bit 0 (to be checked
            zsw |= 1 << 0              # on the real pump; this used to be set whenever there was no error)
        if self.hz > 1:
            zsw |= 1 << 11
        if self._warnings():
            zsw |= (1 << 13) | (1 << 14)     # overload warning, collective warning
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
                pwe = int(noisy(242, 0.002))                        # 0.1 V
            elif pnu == 5:
                base = 30 if 1 < self.hz < MAX_HZ - 1 else 8 if self.hz > 1 else 0
                pwe = int(round(noisy(base + 10 * self.load_a, 0.02)))  # 0.1 A
            elif pnu == 11:
                pwe = int(20 + 8 * self.hz / MAX_HZ + 0.6 * self.heat_c)
            elif pnu == 125:
                pwe = int(30 + 13 * self.hz / MAX_HZ + self.heat_c)
            elif pnu == 184:
                pwe = int(self.op_hours * 100)
            elif pnu == 171:
                pwe = self.error_code
            elif pnu == 227:
                pwe = self._warnings()
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
