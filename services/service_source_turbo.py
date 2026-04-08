import socket
import time
import zmq
import json
import select
from typing import Dict, Any

# Ensure these match your network_config.py
from network_config import (
    ZMQ_PORT_SRC_TURBO_PUB, ZMQ_PORT_SRC_TURBO_CMD, TOPIC_SRC_TURBO_DATA,
    SRC_TURBO_WAVESHARE_IP, SRC_TURBO_WAVESHARE_PORT
)

# --- USS PROTOCOL CONSTANTS ---
PUMP_ADDRESS = 0
SOCKET_TIMEOUT = 1.0
POLL_INTERVAL = 0.05  # 50ms loop

# Task IDs (AK)
AK_READ = 1
AK_WRITE_16 = 2
AK_WRITE_FIELD_16 = 7

# Parameter Mapping
PNU_HW_VER = 1
PNU_SERIAL = 2
PNU_ACT_FREQ = 3  # Hz
PNU_ACT_VOLT = 4  # V
PNU_ACT_CURR = 5  # 0.1A
PNU_SAVE = 8
PNU_CONV_TEMP = 11  # °C
PNU_MAX_FREQ = 18  # Hz (Used for % calculation)
PNU_RELAY_X1 = 29  # Indexed
PNU_BEARING_TEMP = 125  # °C
PNU_X201_FUNC = 134  # Non-indexed for 350i
PNU_OP_HOURS = 184  # 0.01h
PNU_ERR_CODE = 171  # Latest error
PNU_WARN_BITS = 227  # Warning bitfield
PNU_VENT_ON_FREQ = 247
PNU_VENT_OFF_FREQ = 248

TURBO_ERROR_DICT = {
    0: "No Error",
    1: "Overspeed warning",
    2: "Pass through time / Bearing temperature too high",
    4: "Short circuit in motor coil or converter",
    5: "Converter temperature error",
    6: "Run-up time error",
    7: "Motor temperature error",
    61: "Bearing temperature warning",
    83: "Motor undertemperature warning",
    84: "Motor temperature warning",
    85: "Converter overtemperature warning",
    86: "Pump temperature 6 warning",
    87: "Pump temperature 6 failure",
    94: "Pump temperature 4 warning",
    95: "Pump temperature 4 failure",
    96: "Pump temperature 5 warning",
    97: "Pump temperature 5 failure",
    101: "Overload warning (speed dropped)",
    103: "Supply voltage warning",
    106: "Overload Failure (speed below minimum)",
    111: "Motor undertemperature error",
    116: "Permanent overload error",
    117: "Motor current error",
    143: "Overspeed failure",
    213: "Supply voltage error (overvoltage)",
    221: "Checksum error 1",
    225: "Bearing run-in active",
    227: "Frequency converter collective error",
    228: "Frequency converter collective error",
    229: "Frequency converter collective error",
    230: "Frequency converter collective error",
    231: "Supply voltage error (overvoltage)",
    232: "Supply voltage error (undervoltage)",
    233: "Supply voltage error (overvoltage)",
    234: "Supply voltage error (undervoltage)",
    235: "Frequency converter collective error",
    236: "Startup failure (mechanical block/high gas load)",
    237: "Frequency converter collective error",
    238: "Frequency converter collective error",
    239: "Frequency converter collective error",
    240: "Checksum error 2",
    241: "Supply voltage is not 24V",
    242: "Supply voltage is not 48V",
    252: "Hardware plausibility error (Converter/Front-end mismatch)"
}

TURBO_WARNING_DICT = {
    0: "Pump temperature 1 too high",
    1: "Pump temperature 2 too high",
    2: "Pump temperature 3 too high",
    3: "Ambient temperature too low",
    6: "Overspeed warning",
    7: "Pump temperature 4 too high",
    11: "Overload warning",
    12: "Pump temperature 5 too high",
    13: "Pump temperature 6 too high",
    14: "Power supply voltage warning"
}

class TurbovacMicroservice:
    def __init__(self):
        self.context = zmq.Context()

        # 1. ZMQ Comms
        self.pub_socket = self.context.socket(zmq.PUB)
        self.pub_socket.bind(ZMQ_PORT_SRC_TURBO_PUB)

        self.sub_socket = self.context.socket(zmq.SUB)
        self.sub_socket.bind(ZMQ_PORT_SRC_TURBO_CMD)
        self.sub_socket.setsockopt_string(zmq.SUBSCRIBE, "")

        # 2. State
        self.sock = None
        self.connected = False
        self.active_control_word = 0x0400  # Default to Remote + Stop
        self.state: Dict[str, Any] = {
            "serial": None,
            "hw_version": None,
            "comms_ok": False,
            "safety_synced": False,
            "hz": 0,
            "pct": 0.0,
            "max_hz": 1000,
            "temps": {"bearing": 0, "converter": 0},
            "electrical": {"volts": 0, "amps": 0.0},
            "service": {"hours": 0.0},
            # Active states derived from every packet's Status Word
            "status": {
                "ready": False,
                "turning": False,
                "accelerating": False,
                "decelerating": False,
                "normal_operation": False,
                "error_active": False,
                "warning_active": False
            },
            # Historical logs fetched from parameter memory
            "history": {
                "most_recent_error_code": 0,
                "most_recent_error_desc": "No Error",
                "most_recent_warning_bits": 0,
                "most_recent_warning_desc": "None"
            }
        }

        # 3. Slow Task Queue (Round-Robin)
        self.slow_tasks = [
            ("bearing_temp", PNU_BEARING_TEMP),
            ("conv_temp", PNU_CONV_TEMP),
            ("volts", PNU_ACT_VOLT),
            ("amps", PNU_ACT_CURR),
            ("op_hours", PNU_OP_HOURS),
            ("warnings", PNU_WARN_BITS),
            ("error", PNU_ERR_CODE)
        ]
        self._slow_idx = 0

    # --- USS LOW LEVEL ---
    def _calculate_bcc(self, frame):
        bcc = 0
        for byte in frame: bcc ^= byte
        return bcc

    def _generate_frame(self, pnu, index, value=0, ak=AK_READ, control_word=0x0400):
        """Constructs 24-byte USS frame. Bit 10 of control word always 1 for remote."""
        stx, lge, adr = 0x02, 22, PUMP_ADDRESS
        pke = (ak << 12) | pnu
        pwe = [(value >> 24) & 0xFF, (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF]
        # PZD1 is the Control Word
        pzd1_hi, pzd1_lo = (control_word >> 8) & 0xFF, control_word & 0xFF

        frame = [stx, lge, adr, (pke >> 8) & 0xFF, pke & 0xFF, 0x00, index] + pwe + [pzd1_hi, pzd1_lo] + ([0x00] * 10)
        frame.append(self._calculate_bcc(frame))
        return bytes(frame)

    def _transaction(self, pnu, index, value=0, ak=AK_READ, control_word=0x0400):
        """
        Returns (Value, ResponseAK, StatusWord).
        """
        if not self.connected: return None, None, None

        try:
            while select.select([self.sock], [], [], 0.0)[0]:
                self.sock.recv(1024)

            self.sock.sendall(self._generate_frame(pnu, index, value, ak, control_word))
            time.sleep(0.02)
            res = self.sock.recv(1024)

            if len(res) < 24: return None, None, None

            resp_pke = (res[3] << 8) | res[4]
            resp_ak = (resp_pke >> 12) & 0xF
            res_pwe = (res[7] << 24) | (res[8] << 16) | (res[9] << 8) | res[10]

            # Extract ZSW (Status Word) from PZD1
            zsw = (res[11] << 8) | res[12]

            return res_pwe, resp_ak, zsw

        except socket.timeout:
            return None, None, None
        except Exception as e:
            print(f"[Turbo Service] Socket error: {e}")
            self.connected = False
            return None, None, None

    # --- INITIALIZATION & HANDSHAKE ---
    def _connect_socket(self):
        if self.sock: self.sock.close()
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(SOCKET_TIMEOUT)
            self.sock.connect((SRC_TURBO_WAVESHARE_IP, SRC_TURBO_WAVESHARE_PORT))
            self.connected = True
            print(f"[Turbo Service] Connected to {SRC_TURBO_WAVESHARE_IP}")
        except Exception as e:
            self.connected = False
            print(f"[Turbo Service] Connection failed: {e}")

    def _verify_safety_strategy(self):
        print("\n" + "=" * 40)
        print("[Turbo Service] VERIFYING SAFETY STRATEGY...")

        # 1. Read static hardware info
        self.state["serial"], _, _ = self._transaction(PNU_SERIAL, 0)
        self.state["hw_version"], _, _ = self._transaction(PNU_HW_VER, 0)
        m_hz, ak_m, _ = self._transaction(PNU_MAX_FREQ, 0)
        if m_hz is not None and ak_m not in [7, 8]:
            self.state["max_hz"] = m_hz

        # 2. Read Argon Strategy Parameters
        val_x201, ak_x201, zsw = self._transaction(PNU_X201_FUNC, 0, ak=1)
        val_relay, ak_relay, zsw = self._transaction(PNU_RELAY_X1, 0, ak=6)
        val_vent_on, ak_on, zsw = self._transaction(PNU_VENT_ON_FREQ, 0, ak=1)
        val_vent_off, ak_off, zsw = self._transaction(PNU_VENT_OFF_FREQ, 0, ak=1)

        print(f"-> X201 Func:   {val_x201} (RespAK: {ak_x201})")
        print(f"-> X1 Relay:    {val_relay} (RespAK: {ak_relay})")
        print(f"-> Vent ON Hz:  {val_vent_on} (RespAK: {ak_on})")
        print(f"-> Vent OFF Hz: {val_vent_off} (RespAK: {ak_off})")

        # 3. Handle NACKs (Busy State)
        if 7 in [ak_x201, ak_relay, ak_on, ak_off]:
            print("[!] PUMP RETURNED NACK. Pump is likely saving to flash (Error 102).")
            print("[Turbo Service] Skipping verification until flash write completes.")
            self.state["safety_synced"] = False
            print("=" * 40 + "\n")
            return  # Abort verification safely

        # 4. Logic: Strategy is valid only if all match
        is_synced = (val_x201 == 19 and val_relay == 4 and val_vent_on == 500 and val_vent_off == 5)
        self.state["safety_synced"] = is_synced

        if not is_synced:
            freq, ak_freq, _ = self._transaction(PNU_ACT_FREQ, 0)
            # Ensure we have a valid frequency read, not an error code
            if freq is not None and ak_freq not in [7, 8] and freq == 0:
                print("[Turbo Service] Strategy out of sync. Correcting now...")
                self._transaction(PNU_X201_FUNC, 0, value=19, ak=AK_WRITE_16)
                time.sleep(0.1)  # Brief pause between writes

                self._transaction(PNU_RELAY_X1, 0, value=4, ak=AK_WRITE_FIELD_16)
                time.sleep(0.1)

                self._transaction(PNU_VENT_ON_FREQ, 0, value=500, ak=AK_WRITE_16)
                time.sleep(0.1)

                self._transaction(PNU_VENT_OFF_FREQ, 0, value=5, ak=AK_WRITE_16)
                time.sleep(0.1)

                print("[Turbo Service] Saving to flash (P8=1)...")
                self._transaction(8, 0, value=1, ak=AK_WRITE_16)

                # RESTORED: Give the pump 30 seconds to write EEPROM without interruption
                print("[Turbo Service] Waiting 30 seconds for non-volatile write...")
                time.sleep(30)
                print("[Turbo Service] Save complete. Resuming normal operations.")

            else:
                print("[!] SAFETY WARNING: Pump is SPINNING but Argon strategy is not loaded!")
        else:
            print("[Turbo Service] Safety strategy verified and active.")
        print("=" * 40 + "\n")

    # --- LOOP TASKS ---
    def _process_commands(self):
        try:
            while True:
                msg = self.sub_socket.recv_json(flags=zmq.NOBLOCK)
                action = msg.get("action")

                if action == "start":
                    print("[Turbo Service] Command received: LATCHING START")
                    self.active_control_word = 0x0401  # Latch Remote + Start
                elif action == "stop":
                    print("[Turbo Service] Command received: LATCHING STOP")
                    self.active_control_word = 0x0400  # Latch Remote + Stop
                elif action == "reset_error":
                    print("[Turbo Service] Command received: ERROR RESET")
                    # Fault resets are an edge-trigger, so a one-off transaction is perfect
                    self._transaction(3, 0, control_word=0x0480)
        except zmq.Again:
            pass

    def run(self):
        print("[Turbo Service] Starting USS Daemon...")
        while True:
            if not self.connected:
                self._connect_socket()
                if self.connected:
                    self._verify_safety_strategy()
                else:
                    time.sleep(2.0); continue

            # 1. High-Priority Poll (Frequency)
            raw_hz, ak_hz, zsw = self._transaction(PNU_ACT_FREQ, 0, control_word=self.active_control_word)

            # Filter out NACKs (AK 7 or 8) so error codes don't become hz values
            if raw_hz is not None and ak_hz not in [7, 8]:
                self.state["hz"] = raw_hz
                self.state["comms_ok"] = True

                # Map active ZSW bits
                self.state["status"]["ready"] = bool(zsw & (1 << 0))
                self.state["status"]["error_active"] = bool(zsw & (1 << 3))
                self.state["status"]["accelerating"] = bool(zsw & (1 << 4))
                self.state["status"]["decelerating"] = bool(zsw & (1 << 5))
                self.state["status"]["normal_operation"] = bool(zsw & (1 << 10))
                self.state["status"]["turning"] = bool(zsw & (1 << 11))
                self.state["status"]["warning_active"] = bool(zsw & (1 << 14))

                max_hz = self.state.get("max_hz", 1000)
                if max_hz and max_hz > 0:
                    self.state["pct"] = round((raw_hz / max_hz) * 100, 1)
                else:
                    self.state["pct"] = 0.0
            elif ak_hz in [7, 8]:
                # Valid comms, but pump returned an error (likely busy)
                self.state["comms_ok"] = True
            else:
                self.state["comms_ok"] = False

            # 2. Process Commands
            self._process_commands()

            # 3. Slow-Priority Poll (Round Robin)
            task_name, pnu = self.slow_tasks[self._slow_idx]
            val, ak_val, _ = self._transaction(pnu, 0, control_word=self.active_control_word)

            if val is not None and ak_val not in [7, 8]:
                if task_name == "bearing_temp":
                    self.state["temps"]["bearing"] = val
                elif task_name == "conv_temp":
                    self.state["temps"]["converter"] = val
                elif task_name == "volts":
                    self.state["electrical"]["volts"] = val
                elif task_name == "amps":
                    self.state["electrical"]["amps"] = val * 0.1
                elif task_name == "op_hours":
                    self.state["service"]["hours"] = round(val * 0.01, 2)
                elif task_name == "warnings":
                    self.state["history"]["most_recent_warning_bits"] = val
                    active_warnings = [desc for bit, desc in TURBO_WARNING_DICT.items() if val & (1 << bit)]
                    self.state["history"]["most_recent_warning_desc"] = ", ".join(active_warnings) if active_warnings else "None"
                elif task_name == "error":
                    self.state["history"]["most_recent_error_code"] = val
                    self.state["history"]["most_recent_error_desc"] = TURBO_ERROR_DICT.get(val, f"Unknown Error ({val})")

            self._slow_idx = (self._slow_idx + 1) % len(self.slow_tasks)

            # 4. Broadcast
            try:
                # Type-check to prevent encode error
                if isinstance(TOPIC_SRC_TURBO_DATA, str):
                    topic = TOPIC_SRC_TURBO_DATA.encode('utf-8')
                else:
                    topic = TOPIC_SRC_TURBO_DATA

                self.pub_socket.send_multipart([topic, json.dumps(self.state).encode('utf-8')])
            except Exception as e:
                print(f"ZMQ Publish Error: {e}")

            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    TurbovacMicroservice().run()