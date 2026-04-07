import socket
import time
import zmq
import json
import os

# Ensure these match your updated network_config.py
from network_config import (
    ZMQ_PORT_VACUUM_PUB, ZMQ_PORT_VACUUM_CMD, TOPIC_VACUUM_DATA,
    NOISY_RACK_WAVESHARE_IP, NOISY_RACK_WAVESHARE_PORT
)

# --- CONFIGURATION ---
SOCKET_TIMEOUT = 0.3
POLL_INTERVAL = 0.05

NODE_IDS = [10, 20]
CHANNELS = [1, 2, 3]
RELAYS = [1, 2, 3]

# Read Parameter Mapping: (Group, Parameter)
PARAM_PRESSURE = 29  # Group = Channel
PARAM_STATUS = 24  # Group = Channel
PARAM_NAME = 5  # Group = Channel
PARAM_TYPE = 4  # Group = Channel
PARAM_SERIAL = 2  # Group = 5 (Shared)

# Hardcoded Relay Parameter Matrix (Group 4)
# Parameters: Channel Assignment, Turn ON, Turn OFF, Current Status
RELAY_PARAMS = {
    1: {"ch": "1", "on": "2", "off": "3", "status": "4"},
    2: {"ch": "5", "on": "6", "off": "7", "status": "8"},
    3: {"ch": "9", "on": "10", "off": "11", "status": "12"}
}


class VacuumMicroservice:
    def __init__(self):
        self.context = zmq.Context()

        # 1. ZMQ Publisher (Broadcasting Data)
        self.pub_socket = self.context.socket(zmq.PUB)
        self.pub_socket.bind(ZMQ_PORT_VACUUM_PUB)

        # 2. ZMQ Subscriber (Listening for Commands)
        self.sub_socket = self.context.socket(zmq.SUB)
        self.sub_socket.bind(ZMQ_PORT_VACUUM_CMD)
        self.sub_socket.setsockopt_string(zmq.SUBSCRIBE, "")

        # 3. TCP Socket State
        self.sock = None
        self.connected = False

        # 4. Data Structure Initialization
        self.state = {}
        for node in NODE_IDS:
            self.state[node] = {
                "serial_number": None,
                "comms_ok": False,
                "channels": {},
                "relays": {}
            }
            # Initialize Channels
            for ch in CHANNELS:
                self.state[node]["channels"][ch] = {
                    "name": None, "type": None, "pressure": None,
                    "status": None, "last_update": 0.0
                }
            # Initialize Relays
            for sp in RELAYS:
                self.state[node]["relays"][sp] = {
                    "assigned_ch": None, "on_val": None,
                    "off_val": None, "status": None
                }

        # 5. Build the Slow-Poll Round-Robin Queue
        # This prevents blocking the fast pressure loop by only checking one slow parameter per cycle
        self.slow_tasks = []
        for ch in CHANNELS:
            self.slow_tasks.append(("gauge_status", ch))
        for sp in RELAYS:
            self.slow_tasks.append(("relay_status", sp))
            self.slow_tasks.append(("relay_on", sp))
            self.slow_tasks.append(("relay_off", sp))
            self.slow_tasks.append(("relay_ch", sp))

        self._slow_task_idx = 0

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
            print(f"[Vacuum Service] Connected to {NOISY_RACK_WAVESHARE_IP}:{NOISY_RACK_WAVESHARE_PORT}")
        except Exception as e:
            self.connected = False
            self.sock = None
            print(f"[Vacuum Service] Connection failed: {e}")

    # --- READ PROTOCOL ---
    def _generate_read_frame(self, node_id: int, param_group: str, param_no: str) -> bytes:
        address = f"{node_id:02X}".encode('ascii')
        body = b'\x0f' + param_group.encode('ascii') + b';' + param_no.encode('ascii')
        crc_val = 255 - (sum(body) % 256)
        if crc_val < 32: crc_val += 32
        return address + body + bytes([crc_val]) + b'\x04'

    def _read_transaction(self, node_id: int, param_group: str, param_no: str) -> str:
        if not self.connected: return None
        payload = self._generate_read_frame(node_id, param_group, param_no)
        try:
            self.sock.sendall(payload)
            response = self.sock.recv(1024)
            if b"\x06" in response:
                ack_idx = response.find(b"\x06")
                return response[ack_idx + 1:-2].decode('ascii', errors='ignore').strip()
            return None
        except socket.timeout:
            return None  # Normal RS485 delay, do not drop connection
        except Exception:
            self.connected = False
            return None

    def _read_transaction_with_retry(self, node_id, param_group, param_no, max_retries=50):
        for _ in range(max_retries):
            val = self._read_transaction(node_id, param_group, param_no)
            if val is not None: return val
            time.sleep(POLL_INTERVAL)
        return None

    def _write_transaction(self, node_id: int, param_group: str, param_no: str, value: str) -> str:
        """Sends a Write Command and classifies the hardware response."""
        if not self.connected: return "ERROR"

        address = f"{node_id:02X}".encode('ascii')
        body = b'\x0e' + param_group.encode('ascii') + b';' + param_no.encode('ascii') + b';' + value.encode(
            'ascii') + b' '

        crc_val = 255 - (sum(body) % 256)
        if crc_val < 32: crc_val += 32

        payload = address + body + bytes([crc_val]) + b'\x04'

        try:
            self.sock.sendall(payload)
            response = self.sock.recv(1024)

            if b"\x06" in response:
                return "ACK"
            elif b"\x15" in response:
                return "NACK"
            else:
                return "ERROR"  # Garbage response

        except socket.timeout:
            return "ERROR"
        except Exception:
            self.connected = False
            return "ERROR"

    def _write_transaction_with_retry(self, node_id: int, param_group: str, param_no: str, value: str,
                                      max_retries: int = 10) -> bool:
        """Retries on comms errors, but fails fast if the controller explicitly NACKs."""
        for attempt in range(max_retries):
            status = self._write_transaction(node_id, param_group, param_no, value)

            if status == "ACK":
                print(f"[Vacuum Service] Node {node_id} Grp {param_group} Param {param_no} Set to '{value}'")
                return True

            elif status == "NACK":
                print(f"[!] LEYBOLD REJECTED (NACK): Node {node_id} Grp {param_group} Param {param_no} = '{value}'")
                print(f"[!] -> This is usually a hysteresis violation or out-of-range value.")
                return False  # Do not retry a logic rejection

            # If "ERROR" (Garbage/Timeout), sleep and try again
            time.sleep(POLL_INTERVAL)

        print(f"[Vacuum Service] Write failed after {max_retries} comms errors (Node {node_id}).")
        return False

    def _process_commands(self):
        """Checks the ZMQ SUB socket for incoming UI commands."""
        try:
            while True:
                msg = self.sub_socket.recv_json(flags=zmq.NOBLOCK)

                if msg.get("action") == "set_relay":
                    node = msg.get("node")
                    relay = msg.get("relay")
                    channel = msg.get("channel")
                    on_val = msg.get("on_val")
                    off_val = msg.get("off_val")

                    # 1. Enforce Atomic Pair: Require all values
                    if None in (node, relay, channel, on_val, off_val) or relay not in RELAY_PARAMS:
                        print("[Vacuum Service] Warning: Incomplete relay command dropped.")
                        continue

                    # 2. Enforce Hysteresis Safety Math
                    try:
                        on_float = float(on_val)
                        off_float = float(off_val)
                        if on_float >= off_float:
                            print(
                                f"[Vacuum Service] SAFETY REJECT: Node {node} Relay {relay}. ON ({on_float}) must be < OFF ({off_float}).")
                            continue
                    except ValueError:
                        print("[Vacuum Service] Error: Setpoints must be valid floats.")
                        continue

                    print(f"\n[Vacuum Service] Executing Relay {relay} config on Node {node}...")

                    p_ch = RELAY_PARAMS[relay]["ch"]
                    p_on = RELAY_PARAMS[relay]["on"]
                    p_off = RELAY_PARAMS[relay]["off"]

                    # 3. Format and Write (ON first, then OFF)
                    success = self._write_transaction_with_retry(node, "4", p_ch, str(int(channel)))
                    time.sleep(0.2)

                    if success:
                        # 2. Write ON Pressure
                        on_success = self._write_transaction_with_retry(node, "4", p_on, f"{on_float:.1E}")
                        time.sleep(0.2)

                        # 3. Write OFF Pressure (Only if ON succeeded)
                        if on_success:
                            self._write_transaction_with_retry(node, "4", p_off, f"{off_float:.1E}")
                            print("[Vacuum Service] Relay Config Complete.\n")
                        else:
                            print("[Vacuum Service] Aborted Relay Config due to NACK on Turn ON pressure.\n")

                elif msg.get("action") == "set_channel_name":
                    node = msg.get("node")
                    channel = msg.get("channel")
                    new_name = str(msg.get("name", ""))[:10]  # Truncate to 10 chars

                    if node is not None and channel is not None and new_name:
                        print(f"\n[Vacuum Service] Updating Node {node} Ch {channel} name to '{new_name}'...")
                        # Group = Channel, Param = 5
                        self._write_transaction_with_retry(node, str(channel), "5", new_name)
                        print("[Vacuum Service] Name Update Complete.\n")

        except zmq.Again:
            pass

    def _read_static_data(self):
        """One-off initialization reads."""
        print("[Vacuum Service] Reading static sensor profiles...")
        for node in NODE_IDS:
            val = self._read_transaction_with_retry(node, "5", str(PARAM_SERIAL), 50)
            if val: self.state[node]["serial_number"] = val
            for ch in CHANNELS:
                name_val = self._read_transaction_with_retry(node, str(ch), str(PARAM_NAME), 50)
                type_val = self._read_transaction_with_retry(node, str(ch), str(PARAM_TYPE), 50)
                if name_val: self.state[node]["channels"][ch]["name"] = name_val
                if type_val: self.state[node]["channels"][ch]["type"] = type_val

    def _enforce_startup_config(self):
        """Reads the config, enforces Names and Relay setpoints if mismatched."""
        import os
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(current_dir)
        config_path = os.path.join(project_root, "config", "vacuum_settings.json")

        if not os.path.exists(config_path):
            return

        print("\n[Vacuum Service] --- VERIFYING STARTUP CONFIG ---")
        try:
            with open(config_path, "r") as f:
                config_data = json.load(f)

            for node_str, node_data in config_data.items():
                node_id = int(node_str)
                if node_id not in NODE_IDS: continue

                # --- 1. Enforce Channel Names ---
                if "channels" in node_data:
                    for ch_str, ch_params in node_data["channels"].items():
                        ch = int(ch_str)
                        target_name = str(ch_params.get("name", ""))[:10]  # Leybold limit is 10 chars

                        if target_name:
                            # Group = Channel, Param = 5 (Name)
                            curr_name = self._read_transaction_with_retry(node_id, str(ch), "5", 3)
                            if curr_name != target_name:
                                print(f"-> Node {node_id} Ch {ch}: Name mismatch. Updating to '{target_name}'...")
                                self._write_transaction_with_retry(node_id, str(ch), "5", target_name)
                                time.sleep(0.2)

                # --- 2. Enforce Relay Interlocks ---
                if "relays" in node_data:
                    for relay_str, params in node_data["relays"].items():
                        relay_id = int(relay_str)
                        if relay_id not in RELAY_PARAMS: continue

                        channel = params.get("channel")
                        on_val = params.get("on_val")
                        off_val = params.get("off_val")

                        if None in (channel, on_val, off_val): continue

                        p_ch = RELAY_PARAMS[relay_id]["ch"]
                        p_on = RELAY_PARAMS[relay_id]["on"]
                        p_off = RELAY_PARAMS[relay_id]["off"]

                        target_ch_str = str(int(channel))
                        target_on_str = f"{float(on_val):.1E}"
                        target_off_str = f"{float(off_val):.1E}"

                        curr_ch = self._read_transaction_with_retry(node_id, "4", p_ch, 3)
                        curr_on = self._read_transaction_with_retry(node_id, "4", p_on, 3)
                        curr_off = self._read_transaction_with_retry(node_id, "4", p_off, 3)

                        match_found = False
                        if curr_ch == target_ch_str and curr_on is not None and curr_off is not None:
                            try:
                                if abs(float(curr_on) - float(on_val)) < 1e-12 and abs(
                                        float(curr_off) - float(off_val)) < 1e-12:
                                    match_found = True
                            except ValueError:
                                pass

                        if match_found:
                            continue  # OK, skip writes


                        success = False
                        on_success = False
                        print(f"-> Node {node_id} Relay {relay_id}: Mismatch found! Overwriting...")
                        success = self._write_transaction_with_retry(node_id, "4", p_ch, target_ch_str)
                        time.sleep(0.2)

                        if success:

                            current_off_float = float(curr_off) if curr_off else 1000.0
                            if float(on_val) < current_off_float:
                                on_success = self._write_transaction_with_retry(node_id, "4", p_on, target_on_str)
                                time.sleep(0.2)
                                if on_success: self._write_transaction_with_retry(node_id, "4", p_off, target_off_str)
                            else:
                                on_success = self._write_transaction_with_retry(node_id, "4", p_off, target_off_str)
                                time.sleep(0.2)
                                if on_success: self._write_transaction_with_retry(node_id, "4", p_on, target_on_str)
                            time.sleep(0.2)

                        if not (success and on_success):
                            print("[Vacuum Service] Aborted Relay Config due to NACK on writes.\n")

            print("[Vacuum Service] --- VERIFICATION COMPLETE ---\n")
        except Exception as e:
            print(f"[Vacuum Service] Failed to enforce startup config: {e}")

    def run(self):
        """Main operational daemon loop."""
        print("[Vacuum Service] Daemon starting...")
        while True:
            if not self.connected:
                self._connect_socket()
                if self.connected:
                    self._read_static_data()
                    self._enforce_startup_config()
                else:
                    time.sleep(2.0); continue

            cycle_start = time.perf_counter()
            self._process_commands()

            # Identify the single slow task to execute this cycle
            slow_task_name, slow_task_target = self.slow_tasks[self._slow_task_idx]

            for node in NODE_IDS:
                node_comms_success = False

                # 1. High-Priority Loop: Pressures (Every Cycle)
                for ch in CHANNELS:
                    raw_p = self._read_transaction(node, str(ch), str(PARAM_PRESSURE))
                    if raw_p is not None:
                        try:
                            self.state[node]["channels"][ch]["pressure"] = float(raw_p)
                            self.state[node]["channels"][ch]["last_update"] = time.time()
                            node_comms_success = True
                        except ValueError:
                            pass
                    time.sleep(POLL_INTERVAL)

                val = None

                # 2. Low-Priority Loop: Interlaced Slow Tasks (One per cycle)
                if slow_task_name == "gauge_status":
                    val = self._read_transaction(node, str(slow_task_target), str(PARAM_STATUS))
                    if val: self.state[node]["channels"][slow_task_target]["status"] = val

                elif slow_task_name == "relay_status":
                    p = RELAY_PARAMS[slow_task_target]["status"]
                    val = self._read_transaction(node, "4", p)
                    if val: self.state[node]["relays"][slow_task_target]["status"] = val

                elif slow_task_name == "relay_on":
                    p = RELAY_PARAMS[slow_task_target]["on"]
                    val = self._read_transaction(node, "4", p)
                    if val: self.state[node]["relays"][slow_task_target]["on_val"] = val

                elif slow_task_name == "relay_off":
                    p = RELAY_PARAMS[slow_task_target]["off"]
                    val = self._read_transaction(node, "4", p)
                    if val: self.state[node]["relays"][slow_task_target]["off_val"] = val

                elif slow_task_name == "relay_ch":
                    p = RELAY_PARAMS[slow_task_target]["ch"]
                    val = self._read_transaction(node, "4", p)
                    if val: self.state[node]["relays"][slow_task_target]["assigned_ch"] = val

                if val is not None:
                    node_comms_success = True

                self.state[node]["comms_ok"] = node_comms_success
                time.sleep(POLL_INTERVAL)

            # Advance the slow task pointer
            self._slow_task_idx = (self._slow_task_idx + 1) % len(self.slow_tasks)

            # 3. Broadcast to ZMQ
            try:
                payload = json.dumps(self.state).encode('utf-8')

                # Safely handle the topic whether it is a normal string or already bytes
                if isinstance(TOPIC_VACUUM_DATA, str):
                    topic_bytes = TOPIC_VACUUM_DATA.encode('utf-8')
                else:
                    topic_bytes = TOPIC_VACUUM_DATA

                self.pub_socket.send_multipart([topic_bytes, payload])
            except Exception as e:
                print(f"[Vacuum Service] ZMQ Publish Error: {e}")

            elapsed = time.perf_counter() - cycle_start
            time.sleep(max(0.0, 0.05 - elapsed))


if __name__ == "__main__":
    service = VacuumMicroservice()
    service.run()