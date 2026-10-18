import os
import json

from src.core.env import getenv

# Define absolute path to the config file relative to this script
CONFIG_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../config/network_config.json'))

# Load the JSON map into memory
with open(CONFIG_PATH, 'r') as f:
    _cfg = json.load(f)

HOST = _cfg["host"]

# SINDIE_PORT_OFFSET shifts every ZeroMQ port, so the automated tests (and a
# second copy of the stack) can run on the same PC as the live system without
# talking to it. Unset in normal use.
PORT_OFFSET = int(getenv("PORT_OFFSET", "0") or 0)

# Ports added after a config file was written fall back to these defaults.
_DEFAULT_PORTS = {"monitor_pub": 5576}


def _endpoint(name: str) -> str:
    port = _cfg["zmq_ports"].get(name, _DEFAULT_PORTS.get(name))
    if port is None:
        raise KeyError(f"network_config.json has no zmq_ports entry '{name}'")
    return f"{HOST}:{int(port) + PORT_OFFSET}"

# --- PLC Service (Snap7) ---
PLC_IP = _cfg["plc"]["ip"]
PLC_RACK = _cfg["plc"]["rack"]
PLC_SLOT = _cfg["plc"]["slot"]
DB_INTERFACE_NUM = _cfg["plc"]["db_interface"]
DB_RETAIN_NUM = _cfg["plc"]["db_retain"]

# --- Magnet Power Supply Comms ---
MAGNET_IP = _cfg["magnet_psu"]["ip"]
MAGNET_PORT = _cfg["magnet_psu"]["port"]

# --- SMU Comms
SMU_IP = _cfg["smu"]["ip"]
SMU_PORT = _cfg["smu"]["port"]

# --- ADAM Comms
ADAM_IP = _cfg["adam"]["ip"]
ADAM_PORT = _cfg["adam"]["port"]

# --- ADAM Comms
STEERER_MAGNET_IP = _cfg["steerer_magnet_psu"]["ip"]
STEERER_MAGNET_PORT = _cfg["steerer_magnet_psu"]["port"]

# --- Waveshare Hardware Endpoints ---
NOISY_RACK_WAVESHARE_IP = _cfg["waveshares"]["noisy_rack"]["ip"]
NOISY_RACK_WAVESHARE_PORT = _cfg["waveshares"]["noisy_rack"]["port"]

SRC_TURBO_WAVESHARE_IP = _cfg["waveshares"]["src_turbo"]["ip"]
SRC_TURBO_WAVESHARE_PORT = _cfg["waveshares"]["src_turbo"]["port"]

LDLK_TURBO_WAVESHARE_IP = _cfg["waveshares"]["ldlk_turbo"]["ip"]
LDLK_TURBO_WAVESHARE_PORT = _cfg["waveshares"]["ldlk_turbo"]["port"]

SPELLMAN_WAVESHARES = _cfg["waveshares"]["spellman"]

# --- ZeroMQ Socket Strings ---
ZMQ_PORT_PLC_PUB = _endpoint("plc_pub")
ZMQ_PORT_PLC_CMD = _endpoint("plc_cmd")

ZMQ_PORT_VACUUM_PUB = _endpoint("vacuum_pub")
ZMQ_PORT_VACUUM_CMD = _endpoint("vacuum_cmd")

ZMQ_PORT_SRC_TURBO_PUB = _endpoint("src_turbo_pub")
ZMQ_PORT_SRC_TURBO_CMD = _endpoint("src_turbo_cmd")

ZMQ_PORT_LDLK_TURBO_PUB = _endpoint("ldlk_turbo_pub")
ZMQ_PORT_LDLK_TURBO_CMD = _endpoint("ldlk_turbo_cmd")

ZMQ_PORT_SPELLMAN_PUB = _endpoint("spellman_pub")
ZMQ_PORT_SPELLMAN_CMD = _endpoint("spellman_cmd")

ZMQ_PORT_SERIAL_PUB = _endpoint("serial_pub")
ZMQ_PORT_SERIAL_CMD = _endpoint("serial_cmd")

ZMQ_PORT_LOGGER_CMD = _endpoint("logger_cmd")

ZMQ_PORT_MAGNET_PUB = _endpoint("magnet_pub")
ZMQ_PORT_MAGNET_CMD = _endpoint("magnet_cmd")

ZMQ_PORT_HEARTBEAT = _endpoint("heartbeat_pub")

ZMQ_PORT_EVENTS_SUB = _endpoint("events_pub")
ZMQ_PORT_EVENTS_PUB = _endpoint("events_pub")

ZMQ_PORT_MANAGER_PUB = _endpoint("manager_pub")
ZMQ_PORT_MANAGER_CMD = _endpoint("manager_cmd")

ZMQ_PORT_SMU_PUB = _endpoint("smu_pub")
ZMQ_PORT_SMU_CMD = _endpoint("smu_cmd")

ZMQ_PORT_ADAM_PUB = _endpoint("adam_pub")
ZMQ_PORT_ADAM_CMD = _endpoint("adam_cmd")

ZMQ_PORT_STEERER_MAGNET_PUB = _endpoint("steerer_magnet_pub")
ZMQ_PORT_STEERER_MAGNET_CMD = _endpoint("steerer_magnet_cmd")

# Readback-vs-setpoint tolerance monitor (service_monitor.py)
ZMQ_PORT_MONITOR_PUB = _endpoint("monitor_pub")

# Port of the service manager's single-instance lock (plain TCP, not ZMQ); moved down by the
# offset so a test/simulation stack has its own lock
MANAGER_LOCK_PORT = 65432 - (PORT_OFFSET % 60000)

# Where each subsystem accepts commands (acknowledged ROUTER sockets, see src/core/commands.py)
COMMAND_ENDPOINTS = {
    "plc": ZMQ_PORT_PLC_CMD,
    "vacuum": ZMQ_PORT_VACUUM_CMD,
    "src_turbo": ZMQ_PORT_SRC_TURBO_CMD,
    "spellman": ZMQ_PORT_SPELLMAN_CMD,
    "magnet": ZMQ_PORT_MAGNET_CMD,
    "steerer_magnet": ZMQ_PORT_STEERER_MAGNET_CMD,
    "smu": ZMQ_PORT_SMU_CMD,
    "adam": ZMQ_PORT_ADAM_CMD,
    "manager": ZMQ_PORT_MANAGER_CMD,
}

# --- ZMQ Topics ---
TOPIC_PLC_DATA = b"PLC_DATA"
TOPIC_VACUUM_DATA = b"VACUUM_DATA"
TOPIC_SRC_TURBO_DATA = b"SRC_TURBO_DATA"
TOPIC_LDLK_TURBO_DATA = b"LDLK_TURBO_DATA"
TOPIC_SERIAL_DATA = b"SERIAL_DATA"
TOPIC_SPELLMAN_DATA = b"SPELLMAN_DATA"
TOPIC_MAGNET_DATA = b"MAGNET_DATA"
TOPIC_SMU_DATA = b"SMU_DATA"
TOPIC_ADAM_DATA = b"ADAM_DATA"
TOPIC_STEERER_MAGNET_DATA = b"STEEER_MAGNET_DATA"
TOPIC_MONITOR_DATA = b"MONITOR_DATA"
TOPIC_MANAGER = b"MANAGER"