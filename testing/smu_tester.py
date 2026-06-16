import time
import zmq
import json

# Replace with absolute imports if running outside the project root
from src.core.network_map import SMU_IP, ZMQ_PORT_SMU_CMD


def send_command(socket: zmq.Socket, tag: str, value: float):
    payload = {
        "tag": tag,
        "value": value,
        "ts": time.time()
    }
    socket.send_json(payload)
    print(f"[TEST TX] {tag} -> {value}")


def run_test_sequence():
    context = zmq.Context()
    pub_socket = context.socket(zmq.PUB)

    # The microservice binds to this port, so the client must connect to it.
    # Assuming localhost testing. Change to specific IP if running on a different machine.
    pub_socket.connect(f"tcp://127.0.0.1:{ZMQ_PORT_SMU_CMD}")

    # Allow time for the ZMQ connection handshake to complete
    time.sleep(0.5)

    print("--- Starting SMU Command Test Sequence ---")

    try:
        # 1. Ensure output is off initially
        send_command(pub_socket, "ion_beam.beamline.faraday.smu.cmd_enable", 0)
        time.sleep(1.0)

        # 2. Set bias voltage to 15.5V
        send_command(pub_socket, "ion_beam.beamline.faraday.smu.sp_requested_voltage", 15.5)
        time.sleep(1.0)

        # 3. Enable output
        send_command(pub_socket, "ion_beam.beamline.faraday.smu.cmd_enable", 1)
        print("Output enabled. Waiting 5 seconds...")
        time.sleep(5.0)

        # 4. Change voltage while output is live
        send_command(pub_socket, "ion_beam.beamline.faraday.smu.sp_requested_voltage", -10.0)
        print("Voltage changed. Waiting 5 seconds...")
        time.sleep(5.0)

        # 5. Disable output and zero voltage
        send_command(pub_socket, "ion_beam.beamline.faraday.smu.cmd_enable", 0)
        send_command(pub_socket, "ion_beam.beamline.faraday.smu.sp_requested_voltage", 0.0)

        print("--- Test Sequence Complete ---")

    except KeyboardInterrupt:
        print("\nTest interrupted.")
    finally:
        pub_socket.close()
        context.term()


if __name__ == "__main__":
    run_test_sequence()