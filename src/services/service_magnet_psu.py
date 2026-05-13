import socket
import time
import zmq
import json
import select
import os
from typing import Dict, Any, Optional

from src.core.network_config import (
    MAGNET_IP, MAGNET_PORT, ZMQ_PORT_MAGNET_PUB, ZMQ_PORT_MAGNET_CMD, TOPIC_MAGNET_DATA, ZMQ_PORT_PLC_PUB
)

# Device Configuration

# Protocol Constants
LF = "\n"
SOCKET_TIMEOUT = 0.25   # 250ms threshold for TCP disconnection (EA responds in <5ms)
POLL_INTERVAL = 0.05    # 50ms polling loop
MAX_CMD_AGE = 0.5       # Max age for UI commands
PLC_WATCHDOG_AGE = 1.0  # Max age for PLC safety permissives

# Magnet Physical Constants (Tune these to your specific coil)
MIN_CURRENT_FOR_CALC = 0.5  # Amps: Minimum current required to perform stable R calculation
NOMINAL_RESISTANCE = 0.35  # Ohms: Expected resistance of the sector magnet coil
RESISTANCE_TOLERANCE = 0.05  # Ohms: Allowed deviation from nominal before triggering warning
SHORT_CIRCUIT_THRESHOLD = 0.05  # Ohms: Value below which a short circuit is declared
OPEN_CIRCUIT_THRESHOLD = 50.0  # Ohms: Value above which an open circuit is declared


class MagnetPSProtocol:
    def __init__(self, ip: str, port: int):
        self.ip = ip
        self.port = port
        self.sock = None
        self.connected = False

    def connect(self, limits: dict):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock.settimeout(SOCKET_TIMEOUT)
            self.sock.connect((self.ip, self.port))
            self.connected = True
            print(f"[Magnet Driver] Connected to {self.ip}:{self.port}")

            # 1. Take remote control
            self.send_command("SYST:LOCK ON")

            # 2. Hardware Watchdog: Drop output if TCP traffic stops for 2s
            self.send_command("SYST:COMM:MON:TIM 2")
            self.send_command("SYST:COMM:MON:ACT ON")

            # 3. Enforce Configuration Limits
            max_v = limits.get("max_voltage", 10.0)
            max_i = limits.get("max_current", 30.0)
            max_p = limits.get("max_power", 200.0)

            # Set Upper Limits
            self.send_command(f"VOLT:LIM:HIGH {max_v:.3f}")
            self.send_command(f"CURR:LIM:HIGH {max_i:.3f}")
            self.send_command(f"POW:LIM:HIGH {max_p:.3f}")

            # Clamp lower bounds to 0 to prevent negative setpoint errors
            self.send_command("VOLT:LIM:LOW 0.000")
            self.send_command("CURR:LIM:LOW 0.000")

            print(f"[Magnet Driver] Safety limits enforced (V:{max_v} I:{max_i} P:{max_p})")

        except Exception as e:
            self.connected = False
            self.sock = None
            print(f"[Magnet Driver] Connection failed: {e}")

    def send_command(self, cmd: str):
        if not self.connected: return
        try:
            frame = f"{cmd}{LF}".encode('ascii')
            self.sock.sendall(frame)
            time.sleep(0.01)
        except Exception as e:
            self.connected = False

    def query(self, cmd: str) -> Optional[str]:
        if not self.connected: return None
        try:
            while select.select([self.sock], [], [], 0.0)[0]:
                self.sock.recv(1024)

            frame = f"{cmd}{LF}".encode('ascii')
            self.sock.sendall(frame)

            res = b""
            start_time = time.time()
            while not res.endswith(LF.encode('ascii')):
                chunk = self.sock.recv(1)
                if not chunk: break
                res += chunk
                if time.time() - start_time > SOCKET_TIMEOUT: break

            return res.decode('ascii').strip()

        except Exception:
            self.connected = False
            return None


class MagnetMicroservice:
    def __init__(self):
        self.context = zmq.Context()

        self.pub_socket = self.context.socket(zmq.PUB)
        self.pub_socket.setsockopt(zmq.SNDHWM, 5)
        self.pub_socket.bind(ZMQ_PORT_MAGNET_PUB)

        self.sub_socket = self.context.socket(zmq.SUB)
        self.sub_socket.setsockopt(zmq.RCVHWM, 5)
        self.sub_socket.bind(ZMQ_PORT_MAGNET_CMD)
        self.sub_socket.setsockopt_string(zmq.SUBSCRIBE, "")

        self.plc_socket = self.context.socket(zmq.SUB)
        self.plc_socket.connect(ZMQ_PORT_PLC_PUB)
        self.plc_socket.setsockopt_string(zmq.SUBSCRIBE, "")

        self.limits = self._load_config()
        self.hw = MagnetPSProtocol(MAGNET_IP, MAGNET_PORT)

        self.coolant_ok = False
        self.last_plc_ts = 0.0
        self.safety_tripped = True  # Start safe

        self.state: Dict[str, Any] = {
            "timestamp": 0.0,
            "system": {"cycle_time_ms": 0.0},
            "telemetry": {},
            "faults": {}
        }

    def _load_config(self) -> dict:
        config_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../config/magnet_config.json'))
        try:
            with open(config_path, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"[Magnet Service] CRITICAL: config load failed, using hardcoded defaults. {e}")
            return {"max_voltage": 5.0, "max_current": 10.0, "max_power": 100.0}

    def _update_safety_permissives(self):
        try:
            while True:
                topic, msg = self.plc_socket.recv_multipart(flags=zmq.NOBLOCK)
                payload = json.loads(msg.decode('utf-8'))

                # --- DEBUG PRINT ---
                print(f"\n[PLC DEBUG] Raw Payload Received at {time.time()}:")
                print(json.dumps(payload, indent=2))
                # -------------------

                # Update timestamp of last PLC message
                self.last_plc_ts = time.time()

                # Extract Coolant Boolean
                plc_coolant = payload.get("telemetry", {}).get("ion_beam.facilities.Stat_Src_Coolant_OK")
                if plc_coolant is not None:
                    self.coolant_ok = plc_coolant

        except zmq.Again:
            pass

        # Evaluate safety condition (Coolant true AND comms alive)
        comms_alive = (time.time() - self.last_plc_ts) < PLC_WATCHDOG_AGE
        self.safety_tripped = not (self.coolant_ok and comms_alive)
        self.state["system"]["safe_to_run"] = not self.safety_tripped

    def _process_commands(self):
        if not self.hw.connected:
            return

        # If PLC interlock is tripped, override incoming commands and force zero/off
        if self.safety_tripped:
            self.hw.send_command("VOLT 0.000")
            self.hw.send_command("CURR 0.000")
            self.hw.send_command("OUTP OFF")

            # Drain the command queue so old commands don't buffer and execute on reset
            try:
                while True: self.sub_socket.recv_json(flags=zmq.NOBLOCK)
            except zmq.Again:
                pass
            return

        try:
            while True:
                msg = self.sub_socket.recv_json(flags=zmq.NOBLOCK)
                tag = msg.get("tag", "")
                value = msg.get("value", 0)
                ts = msg.get("ts", 0.0)

                age = time.time() - ts
                if age > MAX_CMD_AGE:
                    continue

                parts = tag.split('.')
                if len(parts) < 3 or parts[1] != "magnet":
                    continue

                cmd_type = parts[2]

                if cmd_type == "voltage_sp":
                    self.hw.send_command(f"VOLT {value:.3f}")
                elif cmd_type == "current_sp":
                    self.hw.send_command(f"CURR {value:.3f}")
                elif cmd_type == "cmd_enable":
                    state = "ON" if value else "OFF"
                    self.hw.send_command(f"OUTP {state}")

        except zmq.Again:
            pass

    def _poll_device(self):
        if not self.hw.connected:
            return

        prefix = "ion_beam.magnet"

        raw_v = self.hw.query("MEAS:VOLT?")
        raw_i = self.hw.query("MEAS:CURR?")
        raw_outp = self.hw.query("OUTP?")
        raw_status = self.hw.query("STAT:QUES:COND?")

        comms_fail = raw_v is None or raw_status is None

        self.state["faults"]["Magnet_Comms_Fail"] = {
            "active": comms_fail, "severity": 1, "description": "Magnet PSU communication failure."
        } if comms_fail else False

        if comms_fail: return

        v_rb, i_rb = 0.0, 0.0
        outp_enabled = False

        try:
            if raw_v: v_rb = float(''.join(c for c in raw_v if c.isdigit() or c == '.'))
            if raw_i: i_rb = float(''.join(c for c in raw_i if c.isdigit() or c == '.'))
            if raw_outp: outp_enabled = ("1" in raw_outp or "ON" in raw_outp)

            self.state["telemetry"][f"{prefix}.voltage_rb"] = v_rb
            self.state["telemetry"][f"{prefix}.current_rb"] = i_rb
            self.state["telemetry"][f"{prefix}.stat_enabled"] = outp_enabled
        except ValueError:
            pass

        # --- Internal Alarm Decoding ---
        # Evaluate the SCPI Questionable Status Register (16-bit integer)
        try:
            stat_int = int(raw_status)

            psu_ovp = bool(stat_int & (1 << 0))  # Bit 0: Overvoltage
            psu_ocp = bool(stat_int & (1 << 1))  # Bit 1: Overcurrent
            psu_opp = bool(stat_int & (1 << 2))  # Bit 2: Overpower
            psu_ot = bool(stat_int & (1 << 3))  # Bit 3: Overtemperature
            psu_pf = bool(stat_int & (1 << 13))  # Bit 13: Power Fail (AC supply issue)

            self.state["faults"]["Magnet_PSU_OverTemp"] = {"active": psu_ot, "severity": 1,
                                                           "description": "Magnet PSU Internal Overtemperature"} if psu_ot else False
            self.state["faults"]["Magnet_PSU_PowerFail"] = {"active": psu_pf, "severity": 1,
                                                            "description": "Magnet PSU AC Mains Power Fail"} if psu_pf else False
            self.state["faults"]["Magnet_PSU_OVP"] = {"active": psu_ovp, "severity": 1,
                                                      "description": "Magnet PSU Hardware Overvoltage Trip"} if psu_ovp else False
            self.state["faults"]["Magnet_PSU_OCP"] = {"active": psu_ocp, "severity": 1,
                                                      "description": "Magnet PSU Hardware Overcurrent Trip"} if psu_ocp else False

            # If alarms are present, occasionally send clear command so they drop once resolved
            if stat_int > 0:
                self.hw.send_command("SYST:ERR:ALL?")

        except ValueError:
            pass

        # Resistance Calculation and Fault Logic
        short_fault, open_fault, unexp_fault = False, False, False

        if outp_enabled and i_rb > MIN_CURRENT_FOR_CALC:
            resistance = v_rb / i_rb
            self.state["telemetry"][f"{prefix}.resistance_rb"] = resistance

            if resistance < SHORT_CIRCUIT_THRESHOLD:
                short_fault = True
            elif resistance > OPEN_CIRCUIT_THRESHOLD:
                open_fault = True
            elif abs(resistance - NOMINAL_RESISTANCE) > RESISTANCE_TOLERANCE:
                unexp_fault = True
        else:
            self.state["telemetry"][f"{prefix}.resistance_rb"] = 0.0

        # Update Fault Dictionary
        self.state["faults"]["Magnet_Short_Circuit"] = {
            "active": short_fault, "severity": 1, "description": "Magnet PSU appears short-circuited."
        } if short_fault else False

        self.state["faults"]["Magnet_Open_Circuit"] = {
            "active": open_fault, "severity": 1, "description": "Magnet PSU appears open-circuited."
        } if open_fault else False

        self.state["faults"]["Magnet_Unexpected_Res"] = {
            "active": unexp_fault, "severity": 1, "description": "Magnet has unexpected resistance value."
        } if unexp_fault else False

    def run(self):
        print("[Magnet Service] Daemon Starting...")
        while True:
            cycle_start = time.perf_counter()

            if not self.hw.connected:
                self.hw.connect(self.limits)

            self._update_safety_permissives()

            if self.hw.connected:
                self._process_commands()
                self._poll_device()

            self.state["timestamp"] = time.time()
            elapsed = time.perf_counter() - cycle_start
            self.state["system"]["cycle_time_ms"] = elapsed * 1000

            try:
                topic = TOPIC_MAGNET_DATA if isinstance(TOPIC_MAGNET_DATA, bytes) else TOPIC_MAGNET_DATA.encode('utf-8')
                self.pub_socket.send_multipart([topic, json.dumps(self.state).encode('utf-8')])
            except Exception as e:
                print(f"[Magnet Service] ZMQ Publish Error: {e}")

            time.sleep(max(0.0, POLL_INTERVAL - elapsed))


if __name__ == "__main__":
    MagnetMicroservice().run()