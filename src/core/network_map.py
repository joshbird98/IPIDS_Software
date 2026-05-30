import os
import json

# Define absolute path to the config file relative to this script
CONFIG_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../config/network_config.json'))

# Load the JSON map into memory
with open(CONFIG_PATH, 'r') as f:
    _cfg = json.load(f)

HOST = _cfg["host"]

# --- PLC Service (Snap7) ---
PLC_IP = _cfg["plc"]["ip"]
PLC_RACK = _cfg["plc"]["rack"]
PLC_SLOT = _cfg["plc"]["slot"]
DB_INTERFACE_NUM = _cfg["plc"]["db_interface"]
DB_RETAIN_NUM = _cfg["plc"]["db_retain"]

# --- Magnet Power Supply Comms ---
MAGNET_IP = _cfg["magnet_psu"]["ip"]
MAGNET_PORT = _cfg["magnet_psu"]["port"]

# --- Waveshare Hardware Endpoints ---
NOISY_RACK_WAVESHARE_IP = _cfg["waveshares"]["noisy_rack"]["ip"]
NOISY_RACK_WAVESHARE_PORT = _cfg["waveshares"]["noisy_rack"]["port"]

SRC_TURBO_WAVESHARE_IP = _cfg["waveshares"]["src_turbo"]["ip"]
SRC_TURBO_WAVESHARE_PORT = _cfg["waveshares"]["src_turbo"]["port"]

LDLK_TURBO_WAVESHARE_IP = _cfg["waveshares"]["ldlk_turbo"]["ip"]
LDLK_TURBO_WAVESHARE_PORT = _cfg["waveshares"]["ldlk_turbo"]["port"]

SPELLMAN_WAVESHARES = _cfg["waveshares"]["spellman"]

# --- ZeroMQ Socket Strings ---
ZMQ_PORT_PLC_PUB = f"{HOST}:{_cfg['zmq_ports']['plc_pub']}"
ZMQ_PORT_PLC_CMD = f"{HOST}:{_cfg['zmq_ports']['plc_cmd']}"

ZMQ_PORT_VACUUM_PUB = f"{HOST}:{_cfg['zmq_ports']['vacuum_pub']}"
ZMQ_PORT_VACUUM_CMD = f"{HOST}:{_cfg['zmq_ports']['vacuum_cmd']}"

ZMQ_PORT_SRC_TURBO_PUB = f"{HOST}:{_cfg['zmq_ports']['src_turbo_pub']}"
ZMQ_PORT_SRC_TURBO_CMD = f"{HOST}:{_cfg['zmq_ports']['src_turbo_cmd']}"

ZMQ_PORT_LDLK_TURBO_PUB = f"{HOST}:{_cfg['zmq_ports']['ldlk_turbo_pub']}"
ZMQ_PORT_LDLK_TURBO_CMD = f"{HOST}:{_cfg['zmq_ports']['ldlk_turbo_cmd']}"

ZMQ_PORT_SPELLMAN_PUB = f"{HOST}:{_cfg['zmq_ports']['spellman_pub']}"
ZMQ_PORT_SPELLMAN_CMD = f"{HOST}:{_cfg['zmq_ports']['spellman_cmd']}"

ZMQ_PORT_SERIAL_PUB = f"{HOST}:{_cfg['zmq_ports']['serial_pub']}"
ZMQ_PORT_SERIAL_CMD = f"{HOST}:{_cfg['zmq_ports']['serial_cmd']}"

ZMQ_PORT_LOGGER_CMD = f"{HOST}:{_cfg['zmq_ports']['logger_cmd']}"

ZMQ_PORT_MAGNET_PUB = f"{HOST}:{_cfg['zmq_ports']['magnet_pub']}"
ZMQ_PORT_MAGNET_CMD = f"{HOST}:{_cfg['zmq_ports']['magnet_cmd']}"

ZMQ_PORT_HEARTBEAT = f"{HOST}:{_cfg['zmq_ports']['heartbeat_pub']}"

ZMQ_PORT_EVENTS_SUB = f"{HOST}:{_cfg['zmq_ports']['events_pub']}"
ZMQ_PORT_EVENTS_PUB = f"{HOST}:{_cfg['zmq_ports']['events_pub']}"

ZMQ_PORT_MANAGER_PUB = f"{HOST}:{_cfg['zmq_ports']['manager_pub']}"
ZMQ_PORT_MANAGER_CMD = f"{HOST}:{_cfg['zmq_ports']['manager_cmd']}"

# --- ZMQ Topics ---
TOPIC_PLC_DATA = b"PLC_DATA"
TOPIC_VACUUM_DATA = b"VACUUM_DATA"
TOPIC_SRC_TURBO_DATA = b"SRC_TURBO_DATA"
TOPIC_LDLK_TURBO_DATA = b"LDLK_TURBO_DATA"
TOPIC_SERIAL_DATA = b"SERIAL_DATA"
TOPIC_SPELLMAN_DATA = b"SPELLMAN_DATA"
TOPIC_MAGNET_DATA = b"MAGNET_DATA"