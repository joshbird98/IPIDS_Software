# network_config.py
"""
ZeroMQ Microservice Network Configuration
Brokerless architecture using 127.0.0.1
"""

HOST = "tcp://127.0.0.1"

NOISY_RACK_WAVESHARE_IP = "192.168.1.200"
NOISY_RACK_WAVESHARE_PORT = 4196

SRC_TURBO_WAVESHARE_IP = "192.168.1.201"
SRC_TURBO_WAVESHARE_PORT = 4196

LDLK_TURBO_WAVESHARE_IP = "192.168.1.202"
LDLK_TURBO_WAVESHARE_PORT = 4196

# --- Spellman MPD Service (RS485/TCP) ---
# Central registry for all Spellman Waveshare adapters
SPELLMAN_WAVESHARES = {
    "beamline_waveshare": {
        "ip": "192.168.1.203",
        "port": 4196
    },
    "endstation_waveshare": {
        "ip": "192.168.1.204",
        "port": 4196
    }
}

# --- PLC Service (Snap7) ---
# PUBlishes 10Hz tag data and faults
ZMQ_PORT_PLC_PUB = f"{HOST}:5550"
# PULLs write commands from the GUI or other services
ZMQ_PORT_PLC_CMD = f"{HOST}:5551"

# --- Vacuum Gauge Service (ASCII/TCP) ---
# PUBlishes vacuum/HV data
ZMQ_PORT_VACUUM_PUB = f"{HOST}:5556"
# PULLs commands for vacuum/HV
ZMQ_PORT_VACUUM_CMD = f"{HOST}:5557"

# --- Source Turbopump Service (TCP) ---
# PUBlishes vacuum/HV data
ZMQ_PORT_SRC_TURBO_PUB = f"{HOST}:5558"
# PULLs commands for vacuum/HV
ZMQ_PORT_SRC_TURBO_CMD = f"{HOST}:5559"

# --- Loadlock Turbopump Service (TCP) ---
# PUBlishes vacuum/HV data
ZMQ_PORT_LDLK_TURBO_PUB = f"{HOST}:5560"
# PULLs commands for vacuum/HV
ZMQ_PORT_LDLK_TURBO_CMD = f"{HOST}:5561"

# PUBlishes spellman voltage/current/status data
ZMQ_PORT_SPELLMAN_PUB = f"{HOST}:5562"
# PULLs commands for spellman
ZMQ_PORT_SPELLMAN_CMD = f"{HOST}:5563"


# --- Serial Service (Modbus/TCP) ---
# PUBlishes serial data
ZMQ_PORT_SERIAL_PUB = f"{HOST}:5552"
# PULLs commands for serial
ZMQ_PORT_SERIAL_CMD = f"{HOST}:5553"

# --- Logger Service ---
# PULLs command to trigger high-speed bursts or manual saves
ZMQ_PORT_LOGGER_CMD = f"{HOST}:5554"

# --- ZMQ Topics ---
TOPIC_PLC_DATA = b"PLC_DATA"
TOPIC_PLC_FAULTS = b"PLC_FAULTS"
TOPIC_VACUUM_DATA = b"VACUUM_DATA"
TOPIC_VACUUM_FAULTS = b"VACUUM_FAULTS"
TOPIC_SRC_TURBO_DATA = b"SRC_TURBO_DATA"
TOPIC_SRC_TURBO_FAULTS = b"SRC_TURBO_FAULTS"
TOPIC_LDLK_TURBO_DATA = b"LDLK_TURBO_DATA"
TOPIC_LDLK_TURBO_FAULTS = b"LDLK_TURBO_FAULTS"
TOPIC_SERIAL_DATA = b"SERIAL_DATA"
TOPIC_SERIAL_FAULTS = b"SERIAL_FAULTS"
TOPIC_SPELLMAN_DATA = b"SPELLMAN_DATA"
TOPIC_SPELLMAN_FAULTS = b"SPELLMAN_FAULTS"