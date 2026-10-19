"""Modbus-TCP (RTU framing) magnet power supply - the analyser magnet and the X steerer use
the same PSU model. Setpoints are scaled to 0..52428 counts of the supply's nominal ratings,
read from holding registers 121..126 on connect.

Without valid ratings the PSU is still connected - so it can always be switched off and set to
zero, which needs no scaling - but a non-zero setpoint or switching the output on is refused
(RatingsUnknown) and the voltage/current readbacks are unknown (None). The ratings are read
again every RATINGS_RETRY_S, after a telemetry read the PSU answered, until they arrive - unless
the PSU didn't answer the ratings read at all (ratings_retrying False): an unanswered request
holds the poll loop for about 2 s (4 tries x 0.5 s), close to the manager's 3 s hung-service
timeout for the magnet services, so then they are only read again on the next reconnect. A poll
never waits for more than one unanswered request. (The driver used to carry on with 1 V / 1 A ratings, which turned
any request of 1 A or more into a full-scale current.)
"""
import logging
import math
import struct
import time
from typing import Optional, Tuple

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException
from pymodbus.framer import FramerType

FULL_SCALE = 52428.0
RATINGS_RETRY_S = 5.0
# What talking to the PSU can raise: pymodbus' own errors, sockets, and this driver's IOError /
# ConnectionError (both OSError)
IO_ERRORS = (OSError, ModbusException)


class RatingsUnknown(IOError):
    """A non-zero setpoint, or output on, while the PSU's nominal ratings are unknown."""


class MagnetModbusProtocol:
    def __init__(self, ip: str, port: int = 502, name: str = "magnet", clock=time.monotonic):
        self.ip = ip
        self.port = port
        self.client = None
        self.connected = False
        self.ratings_ok = False
        self.nom_v = self.nom_i = self.nom_p = None
        self._clock = clock
        self._ratings_tried = None
        self.ratings_retrying = True    # False after the ratings read got no answer (see the module docstring)
        self._ratings_warned = False
        self.log = logging.getLogger(f"driver.{name}_psu")

    def connect(self) -> bool:
        if self.client:
            try:
                self.client.close()
            except IO_ERRORS as e:
                self.log.debug("close before reconnect: %s", e)
        self.client = ModbusTcpClient(self.ip, port=self.port, framer=FramerType.RTU, timeout=0.5, retries=3)
        self.ratings_ok = False
        try:
            if not self.client.connect():
                return False
            self.connected = True
            self.client.write_coil(402, True)
            self.client.write_coil(411, True)
        except IO_ERRORS as e:
            self.log.debug("connect failed: %s", e)
            self.connected = False
            return False
        try:
            self._read_ratings()
        except IO_ERRORS as e:      # no answer: stay connected all the same - zero and off need no ratings
            self._no_ratings(f"no answer: {e}", retry=False)
        if self.ratings_ok:
            self.log.info("Modbus TCP connected to %s:%s (%.1f V, %.1f A nominal)", self.ip, self.port,
                          self.nom_v, self.nom_i)
        return True

    def _read_ratings(self) -> bool:
        """Read the nominal ratings (an I/O error propagates); sets ratings_ok."""
        self._ratings_tried = self._clock()
        res = self.client.read_holding_registers(address=121, count=6)
        ratings = None if res.isError() else self._ratings(getattr(res, "registers", None))
        if ratings is None:
            self._no_ratings(res, retry=True)
            return False
        self.nom_v, self.nom_i, self.nom_p = ratings
        self.ratings_ok = True
        self._ratings_warned = False
        return True

    def _no_ratings(self, why, retry: bool):
        self.ratings_ok = False
        self.ratings_retrying = retry
        if not self._ratings_warned:            # once, not at every retry or reconnect
            self._ratings_warned = True
            again = f"read again every {RATINGS_RETRY_S:g} s" if retry else "read again on the next reconnect"
            self.log.warning("Nominal ratings not readable from %s:%s (%s) - only zero / output off until they are "
                             "(setpoints would be scaled wrongly); %s", self.ip, self.port, why, again)

    def disconnect(self):
        if self.connected:
            try:
                self.client.write_coil(405, False)
                self.client.write_coil(402, False)
            except IO_ERRORS as e:   # still close the socket
                self.log.warning("Could not switch output off while disconnecting: %s", e)
            self.client.close()
            self.connected = False

    @staticmethod
    def _ratings(regs):
        """(nominal V, A, W) from holding registers 121..126, or None unless V and A are positive
        finite numbers (the power rating isn't used for scaling)."""
        if regs is None or len(regs) < 6:
            return None
        v, i, p = (struct.unpack('>f', struct.pack('>HH', regs[k], regs[k + 1]))[0] for k in (0, 2, 4))
        if not all(math.isfinite(x) and x > 0 for x in (v, i)):
            return None
        return v, i, (p if math.isfinite(p) else None)

    @staticmethod
    def _to_counts(value: float, nominal: float) -> int:
        if value == 0:
            return 0
        if not nominal or nominal <= 0:
            raise RatingsUnknown("nominal rating unknown")
        return max(0, min(int(FULL_SCALE), int((value * FULL_SCALE) / nominal)))

    @staticmethod
    def _from_counts(counts: int, nominal: float) -> float:
        return (counts * nominal) / FULL_SCALE

    @staticmethod
    def _checked(res, what):
        if res is not None and hasattr(res, "isError") and res.isError():
            raise IOError(f"PSU refused {what}: {res}")
        return True

    def _require(self):
        if not self.connected:
            raise ConnectionError("PSU not connected")

    def _require_ratings(self, what: str):
        if not self.ratings_ok:
            raise RatingsUnknown(f"{what} refused: the PSU's nominal ratings are unknown (only zero / output off "
                                 "until it reports them)")

    def set_voltage(self, v_set: float):
        self._require()
        if v_set:
            self._require_ratings(f"voltage {v_set:g} V")
        return self._checked(self.client.write_register(500, self._to_counts(v_set, self.nom_v)), "voltage")

    def set_current(self, i_set: float):
        self._require()
        if i_set:
            self._require_ratings(f"current {i_set:g} A")
        return self._checked(self.client.write_register(501, self._to_counts(i_set, self.nom_i)), "current")

    def set_output(self, state: bool):
        self._require()
        if state:
            self._require_ratings("output on")
        return self._checked(self.client.write_coil(405, state), "output enable")

    def clear_alarms(self):
        self._require()
        return self._checked(self.client.write_coil(411, True), "alarm clear")

    def read_telemetry(self) -> Optional[Tuple[Optional[float], Optional[float], bool, dict]]:
        """(volts, amps, output on, alarms) or None (and connected=False) if the read failed.
        Volts and amps are None while the nominal ratings are unknown."""
        if not self.connected:
            return None
        try:
            res = self.client.read_holding_registers(address=505, count=4)
            if res.isError():
                self.connected = False
                return None
            regs = res.registers
            status_word = (regs[0] << 16) | regs[1]
            v_counts, i_counts = regs[2], regs[3]
        except IO_ERRORS + (IndexError, struct.error) as e:    # I/O error or a short answer: link lost
            self.log.warning("Telemetry read failed: %s", e)
            self.connected = False
            return None
        alarms = {
            "OVP": bool(status_word & 0x00004000),
            "OCP": bool(status_word & 0x00008000),
            "OPP": bool(status_word & 0x00010000),
            "OT": bool(status_word & 0x00020000),
            "PF": bool(status_word & 0x00800000),
        }
        output_on = bool(status_word & 0x00000080)
        if not self.ratings_ok:
            self._retry_ratings()
            if not self.ratings_ok:
                return None, None, output_on, alarms
        return self._from_counts(v_counts, self.nom_v), self._from_counts(i_counts, self.nom_i), output_on, alarms

    def _retry_ratings(self):
        """Read the ratings again if one is due - only after a telemetry read the PSU answered, so a
        poll never waits for two unanswered requests."""
        if not self.ratings_retrying or self._clock() - (self._ratings_tried or 0.0) < RATINGS_RETRY_S:
            return
        try:
            self._read_ratings()
        except IO_ERRORS as e:      # the next telemetry read shows whether the link itself is gone
            self._no_ratings(f"no answer: {e}", retry=False)
