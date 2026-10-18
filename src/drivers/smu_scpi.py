"""Keysight B2900-series SMU over raw SCPI (TCP port 5025), as used for the Faraday-cup bias
and current measurement. The service does the ranging; this is just the wire protocol."""
import logging
import socket
from typing import Optional, Tuple

log = logging.getLogger("driver.smu")

CONNECT_TIMEOUT = 2.0
# Keysight B2900A series standard current ranges
CURRENT_RANGES = [10e-9, 100e-9, 1e-6, 10e-6, 100e-6, 1e-3]


class SmuScpiProtocol:
    def __init__(self, ip: str, port: int = 5025):
        self.ip = ip
        self.port = port
        self.sock = None
        self.connected = False
        self.terminator = b'\n'
        self.output_state = True

        # State memory for reconnects
        self.current_nplc = 1.0
        self.cached_auto_range = False
        self.cached_range_val = 10e-9
        self.cached_azer = False

        self.timeout_counter = 0

    def connect(self) -> bool:
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        try:
            log.debug("Attempting connection to %s:%s", self.ip, self.port)
            # Without a timeout an unreachable SMU blocked here for ~21 s (Windows default)
            self.sock.settimeout(CONNECT_TIMEOUT)
            self.sock.connect((self.ip, self.port))
            self.connected = True
            log.info("Raw SCPI socket connected to %s:%s", self.ip, self.port)

            # Allow enough time for standard initialization
            self.sock.settimeout(5.0)

            self.send_cmd("*RST")
            self.send_cmd("*CLS")
            self.query("*OPC?")

            self.send_cmd(":DISP:ENAB ON")
            self.send_cmd(":DISP:VIEW SING1")
            self.send_cmd(":DISP:DIG 6")
            self.send_cmd(":DISP:ZOOM OFF")

            self.send_cmd(":SOUR:VOLT:PROT 200")
            self.send_cmd(":SOUR:FUNC:MODE VOLT")
            self.send_cmd(":SENS:CURR:RANG:AUTO OFF")
            self.send_cmd(f":SENS:CURR:RANG {CURRENT_RANGES[0]:.2e}")
            self.send_cmd(":SOUR:VOLT:MODE AUTO")
            self.send_cmd(":SENS:CURR:PROT 1e-4")

            self.send_cmd(":SENS:FUNC:OFF:ALL")
            self.send_cmd(":SENS:FUNC \"VOLT\",\"CURR\"")

            self.send_cmd(":SENS:REM OFF")

            # Restore cached AZER memory
            azer_str = "ON" if self.cached_azer else "OFF"
            self.send_cmd(f":SYST:AZER {azer_str}")

            # Restore cached NPLC memory
            self.set_nplc(self.current_nplc)

            self.send_cmd(":FORM:ELEM:SENS VOLT,CURR")
            self.query("*OPC?")

            self.timeout_counter = 0
            return True

        except OSError as e:
            log.debug("Connection failed: %s", e)
            self.connected = False
            return False

    def disconnect(self):
        if self.connected:
            try:
                log.info("Disconnecting from SMU and forcing output to 0 V.")
                self.set_output(False)
                self.sock.close()
            except OSError as e:
                log.warning("Error while disconnecting from SMU: %s", e)
            self.connected = False

    def send_cmd(self, cmd: str):
        if not self.connected: return
        try:
            log.debug("SCPI TX %s", cmd)
            self.sock.sendall((cmd + '\n').encode('utf-8'))
        except OSError as e:
            log.warning("SCPI send failed (%s): %s", cmd, e)
            self.connected = False

    def query(self, cmd: str) -> Optional[str]:
        if not self.connected: return None
        try:
            log.debug("SCPI TX %s", cmd)
            self.sock.sendall((cmd + '\n').encode('utf-8'))
            response = bytearray()
            while True:
                chunk = self.sock.recv(4096)
                if not chunk:
                    self.connected = False
                    return None
                response.extend(chunk)
                if self.terminator in chunk:
                    break

            resp_str = response.decode('utf-8', errors='replace').strip()
            self.timeout_counter = 0  # Reset strike counter on successful reply
            log.debug("SCPI RX %s", resp_str)
            return resp_str
        except socket.timeout:
            self.timeout_counter += 1
            log.warning("SCPI query timeout (%d/3) for %s", self.timeout_counter, cmd)
            if self.timeout_counter >= 3:
                self.connected = False
            return None
        except OSError as e:
            log.warning("SCPI query failed (%s): %s", cmd, e)
            self.connected = False
            return None

    def set_voltage(self, v_set: float):
        self.send_cmd(f":SOUR:VOLT {v_set:.6f}")

    def set_output(self, state: bool):
        if not self.connected: return
        if not state:
            self.send_cmd(":SOUR:VOLT 0.0")
        self.output_state = state

    def set_nplc(self, nplc: float):
        """Adjusts the aperture integration time and perfectly scales the network socket timeout."""
        if not self.connected: return
        # Valid B2900A NPLC range is typically 0.001 to 100
        nplc = max(0.001, min(100.0, float(nplc)))
        self.send_cmd(f":SENS:CURR:NPLC {nplc}")
        self.current_nplc = nplc

        # Generous safety margin to prevent timeout cascades during auto-ranging or long integrations
        required_timeout = 5.0 + (nplc * 0.1)
        self.sock.settimeout(required_timeout)
        log.debug("Aperture set to %s NPLC; socket timeout %.2f s", nplc, required_timeout)

    def read_telemetry(self) -> Optional[Tuple[float, float, bool]]:
        if not self.connected: return None
        try:
            # Atomic command sequence executed by instrument
            resp = self.query(":MEAS:VOLT?;:MEAS:CURR?")
            if resp is None: return None

            parts = resp.split(';')
            v_rb = float(parts[0])
            i_rb = float(parts[1])

            return v_rb, i_rb, self.output_state

        except (ValueError, IndexError) as e:
            log.warning("Telemetry parse failed: %s", e)
            return None
