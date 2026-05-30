import zmq
import json

# Ensure this matches ZMQ_PORT_LOGGER_CMD in network_map.py
PORT = "tcp://127.0.0.1:5554"


def main():
    context = zmq.Context()
    socket = context.socket(zmq.PUSH)
    socket.connect(PORT)

    payload = {
        "action": "TRIGGER_BURST",
        "source": "TEST_SCRIPT",
        "reason": "Manual verification test"
    }

    print(f"Sending trigger command to {PORT}...")
    socket.send_json(payload)
    print("Command sent.")


if __name__ == "__main__":
    main()