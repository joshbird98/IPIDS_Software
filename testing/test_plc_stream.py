import zmq
import json
from datetime import datetime

# Import configurations to match the microservice exact binds
from config.network_config import ZMQ_PORT_PLC_PUB, TOPIC_PLC_DATA, TOPIC_PLC_FAULTS


def run_tester():
    context = zmq.Context()
    sub_socket = context.socket(zmq.SUB)

    print(f"Connecting to PLC Data Stream at {ZMQ_PORT_PLC_PUB}...")
    # The microservice BINDS to the port. The tester CONNECTS to it.
    sub_socket.connect(ZMQ_PORT_PLC_PUB)

    # Ensure topics are formatted as bytes
    topic_data = TOPIC_PLC_DATA if isinstance(TOPIC_PLC_DATA, bytes) else TOPIC_PLC_DATA.encode('utf-8')
    topic_faults = TOPIC_PLC_FAULTS if isinstance(TOPIC_PLC_FAULTS, bytes) else TOPIC_PLC_FAULTS.encode('utf-8')

    # Subscribe to specifically the Data and Fault topics
    sub_socket.setsockopt(zmq.SUBSCRIBE, topic_data)
    sub_socket.setsockopt(zmq.SUBSCRIBE, topic_faults)

    print("Listening for ZMQ messages... (Press Ctrl+C to stop)\n")

    try:
        while True:
            # Blocks until a multi-part message arrives
            frames = sub_socket.recv_multipart()

            if len(frames) == 2:
                topic = frames[0].decode('utf-8')
                payload_raw = frames[1]

                try:
                    # Decode bytes to string, then parse the JSON
                    payload_dict = json.loads(payload_raw.decode('utf-8'))

                    # Print header with millisecond timestamp
                    timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                    print(f"--- [{timestamp}] NEW MESSAGE | TOPIC: {topic} ---")

                    # Pretty-print the dictionary with a 2-space indent
                    print(json.dumps(payload_dict, indent=2))
                    print("\n")

                except json.JSONDecodeError:
                    print(f"[!] Failed to parse JSON on topic {topic}: {payload_raw}")
            else:
                print(f"[!] Received unexpected frame format: {frames}")

    except KeyboardInterrupt:
        print("\nTest stream terminated by user.")
    finally:
        sub_socket.close()
        context.term()


if __name__ == "__main__":
    run_tester()