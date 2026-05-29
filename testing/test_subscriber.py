import zmq
import time
import orjson
import sys

# Ensure these match your network_config.py exactly
from src.core.network_config import ZMQ_PORT_VACUUM_PUB, TOPIC_VACUUM_DATA

def main():
    context = zmq.Context()
    sub_socket = context.socket(zmq.SUB)

    print(f"Connecting to ZMQ Publisher at {ZMQ_PORT_VACUUM_PUB}...")

    try:
        sub_socket.connect(ZMQ_PORT_VACUUM_PUB)
    except Exception as e:
        print(f"Connection error to {ZMQ_PORT_VACUUM_PUB}: {e}")
        sys.exit(1)

    # Standardize topic type for setsockopt_string
    topic_str = TOPIC_VACUUM_DATA.decode('utf-8') if isinstance(TOPIC_VACUUM_DATA, bytes) else TOPIC_VACUUM_DATA
    sub_socket.setsockopt_string(zmq.SUBSCRIBE, topic_str)

    print(f"Spying on {ZMQ_PORT_VACUUM_PUB} | Topic: '{topic_str}'")
    print("Waiting for broadcasts (Ctrl+C to exit)...")

    tally = 0
    lates = 0
    times = []
    try:
        while True:
            # Block until multipart payload is received
            multipart_msg = sub_socket.recv_multipart()
            tally += 1
            if len(multipart_msg) == 2:
                recv_topic, payload = multipart_msg
                state_data = orjson.loads(payload)
                cycle_time = state_data["system.cycle_time_ms"]
                times.append(cycle_time)

                if cycle_time > 700:
                    print(f"\n--- SLOW ARRIVING {recv_topic.decode('utf-8')} ---")
                    print(state_data)
                    lates += 1

                if tally > 50:
                    print(f"Time: {time.time()} | 50 messages received: With {lates} lates. | Average cycle time: {sum(times) / len(times)}ms")
                    print(times)
                    tally = 0
                    lates = 0
                    times = []

            else:
                print(f"Warning: Unexpected multipart length ({len(multipart_msg)} frames). Raw: {multipart_msg}")

    except KeyboardInterrupt:
        print("\nTerminating spy process.")
    finally:
        sub_socket.close(linger=0)
        context.term()

if __name__ == "__main__":
    main()