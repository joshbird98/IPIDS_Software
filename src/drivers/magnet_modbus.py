"""Modbus-TCP (RTU framing) magnet power supply - the analyser magnet and the X steerer use
the same PSU model. Setpoints are scaled to 0..52428 counts of the supply's nominal ratings,
read from holding registers 121..126 on connect."""
import logging
import math
import struct
from typing import Optional, Tuple

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException
from pymodbus.framer import FramerType

FULL_SCALE = 52428.0
# What talking to the PSU can raise: pymodbus' own errors, sockets, and this driver's IOError /
# ConnectionError (both OSError)
IO_ERRORS = (OSError, ModbusException)


class MagnetModbusProtocol:
    def __init__(self, ip: str, port: int = 502, name: str = "magnet"):
        self.ip = ip
        self.port = port
        self.client = None
        self.connected = False
        self.nom_v = 1.0
        self.nom_i = 1.0
        self.nom_p = 1.0
        self.log = logging.getLogger(f"driver.{name}_psu")

    def connect(self) -> bool:
        if self.client:
            try:
                self.client.close()
            except IO_ERRORS as e:
                self.log.debug("close before reconnect: %s", e)
        self.client = ModbusTcpClient(self.ip, port=self.port, framer=FramerType.RTU, timeout=0.5)
        try:
            if not self.client.connect():
                return False
            # Setpoints are sent as a fraction of the supply's nominal ratings, so without them
            # the PSU is not used at all. (It used to carry on with 1.0 V / 1.0 A, which turned any
            # request of 1 A or more into a full-scale current.)
            res = self.client.read_holding_registers(address=121, count=6)
            ratings = None if res.isError() else self._ratings(res.registers)
            if ratings is None:
                self.log.warning("Nominal ratings not readable from %s:%s (%s) - not using the PSU, its setpoints "
                                 "would be scaled wrongly", self.ip, self.port, res)
                self.client.close()
                self.connected = False
                return False
            self.nom_v, self.nom_i, self.nom_p = ratings
            self.connected = True
            self.log.info("Modbus TCP connected to %s:%s (%.1f V, %.1f A, %.0f W nominal)", self.ip, self.port,
                          *ratings)
            self.client.write_coil(402, True)
            self.client.write_coil(411, True)
            return True
        except IO_ERRORS + (IndexError, struct.error) as e:    # also a short answer to the ratings read
            self.log.debug("connect failed: %s", e)
            self.connected = False
            return False

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
        """(nominal V, A, W) from holding registers 121..126, or None unless all three are positive
        finite numbers."""
        if regs is None or len(regs) < 6:
            return None
        vals = tuple(struct.unpack('>f', struct.pack('>HH', regs[i], regs[i + 1]))[0] for i in (0, 2, 4))
        return vals if all(math.isfinite(v) and v > 0 for v in vals) else None

    @staticmethod
    def _to_counts(value: float, nominal: float) -> int:
        if nominal <= 0:
            return 0
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

    def set_voltage(self, v_set: float):
        self._require()
        return self._checked(self.client.write_register(500, self._to_counts(v_set, self.nom_v)), "voltage")

    def set_current(self, i_set: float):
        self._require()
        return self._checked(self.client.write_register(501, self._to_counts(i_set, self.nom_i)), "current")

    def set_output(self, state: bool):
        self._require()
        return self._checked(self.client.write_coil(405, state), "output enable")

    def clear_alarms(self):
        self._require()
        return self._checked(self.client.write_coil(411, True), "alarm clear")

    def read_telemetry(self) -> Optional[Tuple[float, float, bool, dict]]:
        """(volts, amps, output on, alarms) or None (and connected=False) if the read failed."""
        if not self.connected:
            return None
        try:
            res = self.client.read_holding_registers(address=505, count=4)
            if res.isError():
                self.connected = False
                return None
            regs = res.registers
            status_word = (regs[0] << 16) | regs[1]
            alarms = {
                "OVP": bool(status_word & 0x00004000),
                "OCP": bool(status_word & 0x00008000),
                "OPP": bool(status_word & 0x00010000),
                "OT": bool(status_word & 0x00020000),
                "PF": bool(status_word & 0x00800000),
            }
            return (self._from_counts(regs[2], self.nom_v), self._from_counts(regs[3], self.nom_i),
                    bool(status_word & 0x00000080), alarms)
        except IO_ERRORS + (IndexError, struct.error) as e:    # I/O error or a short answer: link lost
            self.log.warning("Telemetry read failed: %s", e)
            self.connected = False
            return None
