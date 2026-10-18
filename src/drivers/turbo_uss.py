"""Leybold TURBOVAC frequency converter, USS protocol over a Waveshare TCP bridge.

transaction(pnu, index, value, ak, control_word) -> (pwe, ak, zsw), or (None, None, None) on a
timeout / short reply. A reply for the wrong parameter (USS desync) or a socket error marks
the link disconnected so the service reconnects.
"""
import logging
import socket
import time

PUMP_ADDRESS = 0
SOCKET_TIMEOUT = 1.0

# Task IDs (AK)
AK_READ = 1
AK_WRITE_16 = 2
AK_WRITE_FIELD_16 = 7

log = logging.getLogger("driver.turbo_uss")


def bcc(frame) -> int:
    out = 0
    for byte in frame:
        out ^= byte
    return out


def frame(pnu, index, value=0, ak=AK_READ, control_word=0x0400) -> bytes:
    stx, lge, adr = 0x02, 22, PUMP_ADDRESS
    pke = (ak << 12) | pnu
    pwe = [(value >> 24) & 0xFF, (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF]
    words = [stx, lge, adr, (pke >> 8) & 0xFF, pke & 0xFF, 0x00, index] + pwe + \
            [(control_word >> 8) & 0xFF, control_word & 0xFF] + [0x00] * 10
    words.append(bcc(words))
    return bytes(words)


class UssLink:
    def __init__(self, ip: str, port: int, timeout: float = SOCKET_TIMEOUT):
        self.ip, self.port, self.timeout = ip, port, timeout
        self.sock = None
        self.connected = False
        self.on_problem = lambda message: log.warning("%s", message)   # the service reports these

    def connect(self):
        self.close()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
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

    def transaction(self, pnu, index, value=0, ak=AK_READ, control_word=0x0400):
        if not self.connected:
            return None, None, None
        try:
            self.sock.setblocking(False)
            try:
                while self.sock.recv(4096):
                    pass
            except (BlockingIOError, OSError):
                pass
            self.sock.setblocking(True)
            self.sock.settimeout(self.timeout)
            self.sock.sendall(frame(pnu, index, value, ak, control_word))
            time.sleep(0.04)

            res = b""
            started = time.time()
            while len(res) < 24:
                chunk = self.sock.recv(24 - len(res))
                if not chunk:
                    break
                res += chunk
                if time.time() - started > self.timeout:
                    break
            if len(res) < 24:
                return None, None, None

            resp_pke = (res[3] << 8) | res[4]
            resp_ak = (resp_pke >> 12) & 0xF
            resp_pnu = resp_pke & 0x07FF
            if resp_pnu != pnu:
                self.on_problem(f"USS desync: asked for PNU {pnu}, got PNU {resp_pnu} - reconnecting")
                self.connected = False
                return None, None, None
            pwe = (res[7] << 24) | (res[8] << 16) | (res[9] << 8) | res[10]
            zsw = (res[11] << 8) | res[12]
            return pwe, resp_ak, zsw
        except socket.timeout:
            return None, None, None
        except OSError as e:
            self.on_problem(f"Socket error: {e}")
            self.connected = False
            return None, None, None
