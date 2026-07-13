import sys
import zmq
import json

# TODO: Verify this port against your src.core.network_map
# Replace with the actual port your Command Thread sends data to (e.g., ZMQ_PORT_MANAGER_CMD)
MANAGER_CMD_PORT = "tcp://127.0.0.1:5555"


def force_release_arbitration():
    """
    Bypasses the HMI GUI to send a direct cmd_ctrl_mode = 0 payload
    to the magnet microservice.
    """
    context = zmq.Context()
    socket = context.socket(zmq.REQ)
    socket.setsockopt(zmq.RCVTIMEO, 2000)

    print(f"Connecting to manager at {MANAGER_CMD_PORT}...")

    try:
        socket.connect(MANAGER_CMD_PORT)

        # Construct the payload mimicking the cmd_thread.send_command formatting
        payload = {
            "target": "magnet",
            "tag": "ion_beam.beamline.magnet.cmd_ctrl_mode",
            "value": 0,
            "origin": "standalone_failsafe"
        }

        print(f"Sending payload: {payload}")
        socket.send_string(json.dumps(payload))

        reply = socket.recv_string()
        print(f"Success. Service replied: {reply}")

    except zmq.error.Again:
        print("Timeout Error: The target microservice or manager did not respond within 2000ms.")
        sys.exit(1)
    except Exception as e:
        print(f"Execution Error: {e}")
        sys.exit(1)
    finally:
        socket.close()
        context.term()


if __name__ == "__main__":
    force_release_arbitration()