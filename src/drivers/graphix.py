"""Leybold GRAPHIX gauge controllers (RS485 behind a Waveshare TCP bridge, the "noisy rack").

read(node, group, param)   -> the value string, or None (timeout / garbage)
write(node, group, param, value) -> "ACK" | "NACK" | "ERROR"

The bus is noisy: a pressure reply with a garbled header is scavenged for a valid
'd.dddE+dd' number before it is given up on.
"""
import logging
import re
import socket
import time
from typing import Optional

from src.core.log_setup import Throttle

PARAM_PRESSURE = 29
SOCKET_TIMEOUT = 0.3
_PRESSURE_RE = re.compile(r'^\d\.\d{2}[eE][+-]\d{2}>?$')

log = logging.getLogger("driver.graphix")


def _crc(body: bytes) -> int:
    crc = 255 - (sum(body) % 256)
    return crc + 32 if crc < 32 else crc


class GraphixLink:
    def __init__(self, ip: str, port: int, timeout: float = SOCKET_TIMEOUT):
        self.ip, self.port, self.timeout = ip, port, timeout
        self.sock = None
        self.connected = False
        self.stats = {}          # "Node10_Grp1" -> {"attempts", "timeouts", "garbage"}
        self._throttle = Throttle(60.0)

    def connect(self):
        self.close()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        try:
            self.sock.connect((self.ip, self.port))
        except OSError:
            self.sock = None
            raise
        self.connected = True

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.connected = False

    def _clear_stale_buffer(self):
        if not self.connected or not self.sock:
            return
        try:
            self.sock.settimeout(0.0)
            while self.sock.recv(1024):
                pass
        except (BlockingIOError, OSError):
            pass
        finally:
            if self.sock:
                self.sock.settimeout(self.timeout)

    def _stat(self, node, group):
        return self.stats.setdefault(f"Node{node}_Grp{group}", {"attempts": 0, "timeouts": 0, "garbage": 0})

    def read(self, node_id: int, param_group: str, param_no: str) -> Optional[str]:
        stat = self._stat(node_id, param_group)
        stat["attempts"] += 1
        if not self.connected or not self.sock:
            stat["timeouts"] += 1
            return None
        self._clear_stale_buffer()
        body = b'\x0f' + param_group.encode('ascii') + b';' + param_no.encode('ascii')
        payload = f"{node_id:02X}".encode('ascii') + body + bytes([_crc(body)]) + b'\x04'
        try:
            self.sock.sendall(payload)
            buffer = b""
            started = time.perf_counter()
            while b"\x04" not in buffer:
                chunk = self.sock.recv(128)
                if not chunk:
                    break
                buffer += chunk
                if time.perf_counter() - started > self.timeout:
                    raise socket.timeout
            if b"\x04" in buffer:
                eot = buffer.find(b"\x04")
                if b"\x06" in buffer:
                    ack = buffer.find(b"\x06")
                    time.sleep(0.02)
                    return buffer[ack + 1:eot - 1].decode('ascii', errors='ignore').strip()
                if param_group in ("1", "2", "3") and param_no == str(PARAM_PRESSURE):
                    tail = buffer[:eot - 1].decode('ascii', errors='ignore').strip()
                    for i in range(len(tail)):
                        candidate = tail[i:].strip()
                        if candidate and all(32 <= ord(c) <= 126 for c in candidate):
                            if _PRESSURE_RE.match(candidate):
                                time.sleep(0.02)
                                return candidate.replace(">", "")
                            break
                stat["garbage"] += 1
        except socket.timeout:
            stat["timeouts"] += 1
        except OSError as e:
            if (n := self._throttle.ready("read-error")) is not None:
                log.warning("Gauge read failed, reconnecting: %s%s", e, Throttle.suffix(n))
            self.connected = False
            stat["garbage"] += 1
        time.sleep(0.02)
        return None

    def write(self, node_id: int, param_group: str, param_no: str, value: str) -> str:
        if not self.connected or not self.sock:
            return "ERROR"
        self._clear_stale_buffer()
        body = (b'\x0e' + param_group.encode('ascii') + b';' + param_no.encode('ascii') + b';'
                + value.encode('ascii') + b' ')
        payload = f"{node_id:02X}".encode('ascii') + body + bytes([_crc(body)]) + b'\x04'
        status = "ERROR"
        try:
            self.sock.sendall(payload)
            response = self.sock.recv(1024)
            if b"\x06" in response:
                status = "ACK"
            elif b"\x15" in response:
                status = "NACK"
        except OSError as e:
            log.warning("Gauge write failed: %s", e)
            self.connected = False
        time.sleep(0.02)
        return status
