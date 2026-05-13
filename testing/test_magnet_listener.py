import zmq
import json
import time

# Adjust import path if running from a different directory level
from src.core.network_config import ZMQ_PORT_MAGNET_PUB, TOPIC_MAGNET_DATA


def monitor_magnet():
    context = zmq.Context()
    sub_socket = context.socket(zmq.SUB)

    # Connect to the PUB socket bound by the microservice
    sub_socket.connect(ZMQ_PORT_MAGNET_PUB)

    # Subscribe to the specific topic
    topic = TOPIC_MAGNET_DATA if isinstance(TOPIC_MAGNET_DATA, bytes) else TOPIC_MAGNET_DATA.encode('utf-8')
    sub_socket.setsockopt(zmq.SUBSCRIBE, topic)

    print(f"[Monitor] Listening for telemetry on {ZMQ_PORT_MAGNET_PUB} (Topic: {topic.decode('utf-8')})")

    try:
        while True:
            # Blocks until a message is received
            recv_topic, payload = sub_socket.recv_multipart()
            state = json.loads(payload.decode('utf-8'))

            # Clear console for readable live updating (optional)
            print("\033c", end="")
            print(f"=== Magnet Telemetry Monitor | TS: {state['timestamp']:.3f} ===")
            print(f"Cycle Time: {state['system']['cycle_time_ms']:.2f} ms\n")

            print("--- Telemetry ---")
            for k, v in state.get('telemetry', {}).items():
                print(f"  {k}: {v}")

            print("\n--- Faults ---")
            for k, v in state.get('faults', {}).items():
                if v:  # If fault is an active dictionary
                    print(f"  [ACTIVE] {k}: {v['description']} (Sev: {v['severity']})")
                else:
                    print(f"  [CLEAR]  {k}")

            time.sleep(0.1)  # Prevent console thrashing

    except KeyboardInterrupt:
        print("\n[Monitor] Shutting down.")
    finally:
        sub_socket.close()
        context.term()


if __name__ == "__main__":
    monitor_magnet()