import zmq
import json
from src.core.network_map import ZMQ_PORT_ADAM_PUB, TOPIC_ADAM_DATA


def test_subscriber():
    context = zmq.Context()
    socket = context.socket(zmq.SUB)

    # The microservice binds to the PUB port, so the subscriber must connect
    socket.connect(ZMQ_PORT_ADAM_PUB)

    # Ensure the topic matches the byte/string format required by your network map
    topic_str = TOPIC_ADAM_DATA if isinstance(TOPIC_ADAM_DATA, str) else TOPIC_ADAM_DATA.decode('utf-8')
    socket.setsockopt_string(zmq.SUBSCRIBE, topic_str)

    print(f"Listening for '{topic_str}' on {ZMQ_PORT_ADAM_PUB} (Press Ctrl+C to stop)...")

    try:
        while True:
            # Block until a message is received
            topic, msg = socket.recv_multipart()

            # Decode and parse the JSON payload
            payload = json.loads(msg.decode('utf-8'))

            print("\n--- ZMQ Payload Received ---")
            print(json.dumps(payload, indent=4))

    except KeyboardInterrupt:
        print("\nTest subscriber stopped.")
    except Exception as e:
        print(f"\nException encountered: {e}")
    finally:
        socket.close()
        context.term()


if __name__ == '__main__':
    test_subscriber()