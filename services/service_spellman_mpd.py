import socket
import time
import zmq
import json
import select
from typing import Dict, Any, Tuple, Optional

# Protocol Constants
STX = "\x02"
LF = "\x0A"
SOCKET_TIMEOUT = 1.0
POLL_INTERVAL = 0.05


class SpellmanMPDProtocol:
    def __init__(self, ip: str, port: int):
        self.ip = ip
        self.port = port
        self.sock = None
        self.connected = False

    def connect(self):
        """Establishes the TCP connection to the Waveshare adapter."""
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(SOCKET_TIMEOUT)
            self.sock.connect((self.ip, self.port))
            self.connected = True
            print(f"[MPD Service] Connected to {self.ip}:{self.port}")
        except Exception as e:
            self.connected = False
            self.sock = None
            print(f"[MPD Service] Connection failed: {e}")

    def _calculate_checksum(self, payload: str) -> str:
        """
        Calculates the Spellman MPD checksum.
        Algorithm from Appendix 2:
        1. Sum ASCII values of payload.
        2. Subtract from 512 (0x200).
        3. Truncate to 8 bits.
        4. Clear bit 7 (AND 0x7F).
        5. Set bit 6 (OR 0x40).
        """
        ascii_sum = sum(ord(c) for c in payload)
        csum = 0x200 - ascii_sum
        csum = csum & 0xFF
        csum = csum & 0x7F
        csum = csum | 0x40
        return f"{csum:02X}"  # Return as 2-character uppercase hex string

    def _generate_frame(self, address: str, dev_type: str, cmd: str, operator: str = "", data: str = "") -> bytes:
        """Constructs the full ASCII frame."""
        payload = f"{address}{dev_type}{cmd}{operator}{data}"
        csum = self._calculate_checksum(payload)
        frame = f"{STX}{payload}{csum}{LF}"
        return frame.encode('ascii')

    def transaction(self, address: str, dev_type: str, cmd: str, operator: str = "", data: str = "") -> Optional[str]:
        """Executes a thread-safe send/receive cycle."""
        if not self.connected:
            return None

        try:
            # 1. Flush stale TCP buffer data
            while select.select([self.sock], [], [], 0.0)[0]:
                self.sock.recv(1024)

            # 2. Transmit
            frame = self._generate_frame(address, dev_type, cmd, operator, data)
            self.sock.sendall(frame)

            # 3. RS485 Turnaround Delay
            time.sleep(0.05)

            # 4. Receive
            res = self.sock.recv(1024).decode('ascii')

            # Validate response framing
            if res.startswith(STX) and res.endswith(LF):
                # Strip STX (index 0) and CSUM/LF (last 3 chars)
                return res[1:-3]
            return None

        except socket.timeout:
            return None
        except Exception as e:
            print(f"[MPD Service] Socket error: {e}")
            self.connected = False
            return None