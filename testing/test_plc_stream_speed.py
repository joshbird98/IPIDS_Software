import zmq
import time
from network_config import ZMQ_PORT_PLC_PUB, TOPIC_PLC_DATA


def run_benchmark():
    context = zmq.Context()
    sub_socket = context.socket(zmq.SUB)

    print(f"Connecting to PLC Data Stream at {ZMQ_PORT_PLC_PUB}...")
    sub_socket.connect(ZMQ_PORT_PLC_PUB)

    topic_data = TOPIC_PLC_DATA if isinstance(TOPIC_PLC_DATA, bytes) else TOPIC_PLC_DATA.encode('utf-8')
    sub_socket.setsockopt(zmq.SUBSCRIBE, topic_data)

    print("Benchmarking stream speed... (Press Ctrl+C to stop)\n")

    message_count = 0
    start_time = time.time()

    try:
        while True:
            # Block until message arrives
            frames = sub_socket.recv_multipart()

            if len(frames) == 2:
                message_count += 1

                # Calculate Hz every 1 second
                current_time = time.time()
                elapsed = current_time - start_time

                if elapsed >= 1.0:
                    hz = message_count / elapsed

                    # Print exact frequency
                    print(f"PLC Stream Speed: {hz:.2f} Hz ({message_count} messages/sec)")

                    # Reset counters for the next second
                    message_count = 0
                    start_time = current_time

    except KeyboardInterrupt:
        print("\nBenchmark terminated.")
    finally:
        sub_socket.close()
        context.term()


if __name__ == "__main__":
    run_benchmark()