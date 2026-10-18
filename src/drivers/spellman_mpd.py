"""Spellman MPD HV supplies on an RS485 bus behind a Waveshare TCP bridge (Manual 48113-21
Issue 3, section 3.2): STX + address + type + command [+ operator + data] + checksum + LF."""
import logging
import select
import socket
import time
from typing import Optional

STX = "\x02"
LF = "\x0A"
SOCKET_TIMEOUT = 1.0
LOCAL_BIND_IP = "192.168.1.100"   # the PC's address on the machine network (the bridge only answers it)

# MPD commands
CMD_REQ_STATUS = "SR"
CMD_REQ_KV = "M0"
CMD_REQ_MA = "M1"
CMD_SET_KV = "V1"
CMD_SET_MA = "I1"
CMD_HV_ENABLE = "EN"


class SpellmanMPDProtocol:
    def __init__(self, ip: str, port: int, bus_id: str):
        self.ip = ip
        self.port = port
        self.bus_id = bus_id
        self.sock = None
        self.connected = False
        self.log = logging.getLogger(f"service_spellman_mpd.{bus_id}")

    def connect(self):
        self.log.debug("Attempting TCP connection to %s:%s", self.ip, self.port)
        if self.sock:
            try:
                self.sock.close()
            except OSError as e:
                self.log.debug("Error closing stale socket: %s", e)
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock.settimeout(SOCKET_TIMEOUT)
            self.sock.bind((LOCAL_BIND_IP, 0))
            self.sock.connect((self.ip, self.port))
            self.connected = True
            self.log.info("Connected to %s:%s", self.ip, self.port)
        except OSError as e:
            self.connected = False
            self.sock = None
            raise ConnectionError(f"{self.bus_id} {self.ip}:{self.port}: {type(e).__name__}: {e}") from e

    def _calculate_checksum(self, payload: str) -> str:
        ascii_sum = sum(ord(c) for c in payload)
        csum = 0x200 - ascii_sum
        csum = csum & 0xFF
        csum = csum & 0x7F
        csum = csum | 0x40
        calc_str = f"{csum:02X}"
        return calc_str

    def _generate_frame(self, address: str, dev_type: str, cmd: str, operator: str = "", data: str = "") -> bytes:
        payload = f"{address}{dev_type}{cmd}{operator}{data}"
        csum = self._calculate_checksum(payload)
        frame = f"{STX}{payload}{csum}{LF}".encode('ascii')
        return frame

    def transaction(self, address: str, dev_type: str, cmd: str, operator: str = "", data: str = "") -> Optional[str]:
        if not self.connected:
            return None

        try:
            # Clear stale data from RX buffer
            while select.select([self.sock], [], [], 0.0)[0]:
                if not self.sock.recv(1024):
                    raise ConnectionError("connection closed by the Waveshare bridge")

            frame = self._generate_frame(address, dev_type, cmd, operator, data)
            self.sock.sendall(frame)
            time.sleep(0.04)

            res = b""
            start_time = time.time()
            while not res.endswith(LF.encode('ascii')):
                chunk = self.sock.recv(1)
                if not chunk:
                    break
                res += chunk
                if time.time() - start_time > SOCKET_TIMEOUT:
                    break

            res_str = res.decode('ascii', errors='replace')

            if res_str.startswith(STX) and res_str.endswith(LF):
                parsed_data = res_str[1:-3]
                return parsed_data
            else:
                return None

        except socket.timeout:
            return None
        except (OSError, ConnectionError, ValueError) as e:
            self.log.warning("Transaction %s%s failed: %s: %s", cmd, operator, type(e).__name__, e)
            self.connected = False
            return None
