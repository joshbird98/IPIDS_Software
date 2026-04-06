# network_config.py
"""
ZeroMQ Microservice Network Configuration
Brokerless architecture using 127.0.0.1
"""

HOST = "tcp://127.0.0.1"

NOISY_RACK_WAVESHARE_IP = "192.168.1.200"
NOISY_RACK_WAVESHARE_PORT = 4196

# --- PLC Service (Snap7) ---
# PUBlishes 10Hz tag data and faults
ZMQ_PORT_PLC_PUB = f"{HOST}:5550"
# PULLs write commands from the GUI or other services
ZMQ_PORT_PLC_CMD = f"{HOST}:5551"

# --- Serial Service (Modbus/TCP) ---
# PUBlishes vacuum/HV data
ZMQ_PORT_SERIAL_PUB = f"{HOST}:5552"
# PULLs commands for vacuum/HV
ZMQ_PORT_SERIAL_CMD = f"{HOST}:5553"

# --- Logger Service ---
# PULLs command to trigger high-speed bursts or manual saves
ZMQ_PORT_LOGGER_CMD = f"{HOST}:5554"

# --- ZMQ Topics ---
TOPIC_PLC_DATA = b"PLC_DATA"
TOPIC_PLC_FAULTS = b"PLC_FAULTS"
TOPIC_SERIAL_DATA = b"SERIAL_DATA"
TOPIC_SERIAL_FAULTS = b"SERIAL_FAULTS"