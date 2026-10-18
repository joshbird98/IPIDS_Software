"""Modbus-TCP (RTU framing) magnet power supply - the analyser magnet and the X steerer use
the same PSU model. Setpoints are scaled to 0..52428 counts of the supply's nominal ratings,
read from holding registers 121..126 on connect."""
import logging
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
            self.connected = True
            self.log.info("Modbus TCP connected to %s:%s", self.ip, self.port)
            res = self.client.read_holding_registers(address=121, count=6)
            if not res.isError():
                regs = res.registers
                self.nom_v = struct.unpack('>f', struct.pack('>HH', regs[0], regs[1]))[0]
                self.nom_i = struct.unpack('>f', struct.pack('>HH', regs[2], regs[3]))[0]
                self.nom_p = struct.unpack('>f', struct.pack('>HH', regs[4], regs[5]))[0]
            else:
                self.log.warning("Could not read nominal ratings (%s); setpoint scaling may be wrong", res)
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
