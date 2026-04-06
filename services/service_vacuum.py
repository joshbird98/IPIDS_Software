import socket
import time
import zmq
import json
import os
from datetime import datetime

from network_config import ZMQ_PORT_SERIAL_PUB, TOPIC_SERIAL_DATA, NOISY_RACK_WAVESHARE_IP, NOISY_RACK_WAVESHARE_PORT
from hmi_config import DEFAULT_LOG_DIRECTORY

# --- CONFIGURATION ---
SOCKET_TIMEOUT = 0.3
POLL_INTERVAL = 0.05

NODE_IDS = [10, 20]
CHANNELS = [1, 2, 3]

# Parameter Mapping: (Group, Parameter)
PARAM_PRESSURE = 29  # Group = Channel
PARAM_STATUS = 24  # Group = Channel
PARAM_NAME = 5  # Group = Channel
PARAM_TYPE = 4  # Group = Channel
PARAM_SERIAL = 2  # Group = 5 (Shared)


class VacuumMicroservice:
    def __init__(self):
        # 1. ZMQ Context & Sockets
        self.context = zmq.Context()
        self.pub_socket = self.context.socket(zmq.PUB)
        self.pub_socket.bind(ZMQ_PORT_SERIAL_PUB)

        # 2. TCP Socket State
        self.sock = None
        self.connected = False

        # 3. Data Structure Initialization
        self.state = {}
        for node in NODE_IDS:
            self.state[node] = {"serial_number": None, "comms_ok": False, "channels": {}}
            for ch in CHANNELS:
                self.state[node]["channels"][ch] = {
                    "name": None,
                    "type": None,
                    "pressure": None,
                    "status": None,
                    "last_update": 0.0
                }

        self._status_rotation_idx = 0

    def _save_static_metadata(self):
        """Saves static sensor profiles to disk once read from RS485."""
        metadata = {"vacuum_config": {}}
        for node in NODE_IDS:
            metadata["vacuum_config"][node] = {
                "serial": self.state[node]["serial_number"],
                "channels": {}
            }
            for ch in CHANNELS:
                metadata["vacuum_config"][node]["channels"][ch] = {
                    "name": self.state[node]["channels"][ch]["name"],
                    "type": self.state[node]["channels"][ch]["type"]
                }

        os.makedirs(DEFAULT_LOG_DIRECTORY, exist_ok=True)
        filepath = os.path.join(DEFAULT_LOG_DIRECTORY, f"vacuum_config_{datetime.now().strftime('%Y%m%d')}.json")
        try:
            with open(filepath, 'w') as f:
                json.dump(metadata, f, indent=4)
            print(f"[Vacuum Service] System config saved to {filepath}")
        except Exception as e:
            print(f"[Vacuum Service] Failed to save config: {e}")

    def _connect_socket(self):
        """Establish or re-establish TCP socket connection."""
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass

        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(SOCKET_TIMEOUT)
            self.sock.connect((NOISY_RACK_WAVESHARE_IP, NOISY_RACK_WAVESHARE_PORT))
            self.connected = True
            print(f"[Vacuum Service] Connected to TCP/RS485 adapter at {NOISY_RACK_WAVESHARE_IP}:{NOISY_RACK_WAVESHARE_PORT}")
        except Exception as e:
            self.connected = False
            self.sock = None
            print(f"[Vacuum Service] Connection failed: {e}")

    def _generate_frame(self, node_id: int, param_group: str, param_no: str) -> bytes:
        """Constructs Graphix 3 Telegram per Leybold protocol."""
        address = f"{node_id:02X}".encode('ascii')
        body = b'\x0f' + param_group.encode('ascii') + b';' + param_no.encode('ascii')

        byte_sum = sum(body)
        crc_val = 255 - (byte_sum % 256)
        if crc_val < 32:
            crc_val += 32

        return address + body + bytes([crc_val]) + b'\x04'

    def _transaction(self, node_id: int, param_group: str, param_no: str) -> str:
        """Sends request and parses the raw response."""
        if not self.connected:
            return None

        payload = self._generate_frame(node_id, param_group, param_no)

        try:
            self.sock.sendall(payload)
            response = self.sock.recv(1024)

            if b"\x06" in response:
                ack_idx = response.find(b"\x06")
                # Exclude ACK byte and trailing checksum/EOT
                val_data = response[ack_idx + 1:-2]
                print(val_data)
                return val_data.decode('ascii', errors='ignore').strip()
            elif b"\x15" in response:
                # NAK received
                print("NAK!")
                return None
            else:
                # Malformed frame
                print("MALFORMED!")
                return None

        except socket.timeout:
            return None
        except Exception as e:
            print(f"[Vacuum Service] Socket error during transaction: {e}")
            self.connected = False
            return None

    def _transaction_with_retry(self, node_id: int, param_group: str, param_no: str, max_retries: int = 50) -> str:
        """Attempts a transaction up to max_retries times before giving up."""
        for attempt in range(max_retries):
            val = self._transaction(node_id, param_group, param_no)
            if val is not None:
                return val
            time.sleep(POLL_INTERVAL)

        print(
            f"[Vacuum Service] Failed to read Node {node_id}, Group {param_group}, Param {param_no} after {max_retries} attempts.")
        return None

    def _read_static_data(self):
        """One-off reads for initialization (Serial, Type, Name) with aggressive retries."""
        print("[Vacuum Service] Reading static sensor profiles...")
        for node in NODE_IDS:
            # Shared Serial (Group 5)
            val = self._transaction_with_retry(node, "5", str(PARAM_SERIAL), max_retries=50)
            if val is not None:
                self.state[node]["serial_number"] = val

            for ch in CHANNELS:
                name_val = self._transaction_with_retry(node, str(ch), str(PARAM_NAME), max_retries=50)
                type_val = self._transaction_with_retry(node, str(ch), str(PARAM_TYPE), max_retries=50)

                if name_val is not None:
                    self.state[node]["channels"][ch]["name"] = name_val
                if type_val is not None:
                    self.state[node]["channels"][ch]["type"] = type_val

        self._save_static_metadata()

    def run(self):
        """Main operational daemon loop."""
        print("[Vacuum Service] Daemon starting...")

        while True:
            if not self.connected:
                self._connect_socket()
                if self.connected:
                    self._read_static_data()
                else:
                    time.sleep(2.0)
                    continue

            cycle_start = time.perf_counter()
            current_status_ch = CHANNELS[self._status_rotation_idx]

            for node in NODE_IDS:
                node_comms_success = False

                # 1. High-Priority Loop: Read all pressures
                for ch in CHANNELS:
                    raw_p = self._transaction(node, str(ch), str(PARAM_PRESSURE))
                    if raw_p is not None:
                        try:
                            # Note: Double check float casting matches Leybold string output format
                            self.state[node]["channels"][ch]["pressure"] = float(raw_p)
                            self.state[node]["channels"][ch]["last_update"] = time.time()
                            node_comms_success = True
                        except ValueError:
                            pass
                    time.sleep(POLL_INTERVAL)  # Space out packets for Waveshare buffer

                # 2. Low-Priority Interlacing: Read one status channel per cycle
                raw_s = self._transaction(node, str(current_status_ch), str(PARAM_STATUS))
                if raw_s is not None:
                    self.state[node]["channels"][current_status_ch]["status"] = raw_s
                    node_comms_success = True

                self.state[node]["comms_ok"] = node_comms_success
                time.sleep(POLL_INTERVAL)

            # Advance status rotation sequence
            self._status_rotation_idx = (self._status_rotation_idx + 1) % len(CHANNELS)

            # 3. Broadcast to ZMQ
            try:
                payload = json.dumps(self.state).encode('utf-8')
                self.pub_socket.send_multipart([TOPIC_SERIAL_DATA, payload])
            except Exception as e:
                print(f"[Vacuum Service] ZMQ Publish Error: {e}")

            # Minimal backoff for loop stability
            elapsed = time.perf_counter() - cycle_start
            sleep_time = max(0, 0.05 - elapsed)
            time.sleep(sleep_time)


if __name__ == "__main__":
    service = VacuumMicroservice()
    service.run()