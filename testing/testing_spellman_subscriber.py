import zmq
import orjson
import time
import os

# Import the port directly from your project's network map
from src.core.network_map import ZMQ_PORT_SPELLMAN_PUB


def run_subscriber():
    context = zmq.Context()
    sub_socket = context.socket(zmq.SUB)

    print(f"[Debug Sub] Connecting to Spellman Publisher at {ZMQ_PORT_SPELLMAN_PUB}...")
    sub_socket.connect(ZMQ_PORT_SPELLMAN_PUB)

    # Subscribe specifically to the SPELLMAN topic being published by the service
    sub_socket.setsockopt_string(zmq.SUBSCRIBE, "SPELLMAN")

    print("[Debug Sub] Listening for telemetry... (Press Ctrl+C to stop)")

    try:
        while True:
            # Receive the multipart message [topic, payload]
            topic, msg = sub_socket.recv_multipart()

            # Decode the JSON payload
            data = orjson.loads(msg)

            # Clear the terminal screen for a static dashboard effect
            os.system('cls' if os.name == 'nt' else 'clear')

            print(f"=== SPELLMAN MPD TELEMETRY ===")
            print(f"Time: {time.strftime('%H:%M:%S')}")
            print(f"Cycle Time: {data.get('system.cycle_time_ms', 0):.2f} ms")
            print("-" * 30)

            # Print all keys sorted alphabetically for easy reading
            for key, value in sorted(data.items()):
                if key not in ["timestamp", "system.cycle_time_ms"]:
                    print(f"{key:<55} : {value}")

    except KeyboardInterrupt:
        print("\n[Debug Sub] Shutting down...")
    finally:
        sub_socket.close()
        context.term()


if __name__ == "__main__":
    run_subscriber()