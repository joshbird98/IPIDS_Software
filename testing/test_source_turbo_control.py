import zmq
import time

# Attempt to load the port dynamically, with a fallback
try:
    from config.network_config import ZMQ_PORT_SRC_TURBO_CMD

    # Ensure it has the tcp:// prefix
    if not ZMQ_PORT_SRC_TURBO_CMD.startswith("tcp://"):
        COMMAND_PORT = f"tcp://{ZMQ_PORT_SRC_TURBO_CMD}"
    else:
        COMMAND_PORT = ZMQ_PORT_SRC_TURBO_CMD
except ImportError:
    # Fallback assuming localhost if network_config isn't in the same folder
    COMMAND_PORT = "tcp://127.0.0.1:5559"


def turn_pump_on():
    context = zmq.Context()

    # Microservice uses SUB, so we use PUB
    socket = context.socket(zmq.PUB)

    print(f"Connecting to Turbo microservice at {COMMAND_PORT}...")
    socket.connect(COMMAND_PORT)

    # CRITICAL: Allow the ZMQ PUB/SUB handshake to complete
    time.sleep(0.2)

    # NEW ARCHITECTURE: Tag-Based Routing Payload
    payload = {
        "tag": "ion_beam.source.turbo_pump.cmd_enable",
        "value": True,
        "ts": time.time()
    }

    print(f"Sending command: {payload}")
    socket.send_json(payload)

    # Allow a split second for the actual network transmission before closing
    time.sleep(0.1)

    socket.close()
    context.term()
    print("Command transmitted. Check the microservice terminal to verify it latched!")


if __name__ == "__main__":
    turn_pump_on()