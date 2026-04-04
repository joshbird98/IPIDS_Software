import zmq
import time
import json

# Ensure these match your network_config.py exactly
ZMQ_PORT_PLC_PUB = "tcp://127.0.0.1:5550"
TOPIC_PLC_DATA = b"PLC_DATA"


def main():
    context = zmq.Context()
    socket = context.socket(zmq.SUB)

    print(f"Connecting to ZMQ Publisher at {ZMQ_PORT_PLC_PUB}...")
    socket.connect(ZMQ_PORT_PLC_PUB)

    # Subscribe to the specific topic (must be bytes)
    socket.setsockopt(zmq.SUBSCRIBE, TOPIC_PLC_DATA)

    print("Listening for PLC_DATA. Press Ctrl+C to exit.\n")

    last_time = time.time()

    try:
        while True:
            # recv_multipart() blocks until a message arrives
            topic, payload = socket.recv_multipart()

            # Calculate frequency
            current_time = time.time()
            delta_t = current_time - last_time
            hz = 1.0 / delta_t if delta_t > 0 else 0.0
            last_time = current_time

            # Parse payload
            data = json.loads(payload.decode('utf-8'))

            # Print metrics and a truncated view of the data dictionary
            print(
                f"[{hz:5.1f} Hz] Topic: {topic.decode('utf-8')} | Tags Received: {len(data)} | Data: {str(data)[:80]}...")

    except KeyboardInterrupt:
        print("\nTest terminated by user.")
    finally:
        socket.close()
        context.term()


if __name__ == "__main__":
    main()