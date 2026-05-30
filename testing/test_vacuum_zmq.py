import zmq
import json
import threading
import time

# --- CONFIGURATION ---
# Adjust these to match your actual network_map.py
from src.core.network_map import ZMQ_PORT_VACUUM_PUB, ZMQ_PORT_VACUUM_CMD, TOPIC_VACUUM_DATA


def monitor_stream():
    """Background thread to listen to the live data stream."""
    context = zmq.Context()
    sub_socket = context.socket(zmq.SUB)
    sub_socket.connect(ZMQ_PORT_VACUUM_PUB)
    topic_bytes = TOPIC_VACUUM_DATA if isinstance(TOPIC_VACUUM_DATA, bytes) else TOPIC_VACUUM_DATA.encode('utf-8')
    sub_socket.setsockopt(zmq.SUBSCRIBE, topic_bytes)

    print(f"[*] Monitor Thread connected to {ZMQ_PORT_VACUUM_PUB}")

    # Keep track of the last known state so we only print when it actually changes
    last_known_on = None
    last_known_off = None

    while True:
        try:
            # Receive multipart message: [Topic, Payload]
            parts = sub_socket.recv_multipart()
            if len(parts) == 2:
                topic, payload = parts
                data = json.loads(payload.decode('utf-8'))

                # Check Node 10, Relay 1
                if '10' in data and 'relays' in data['10']:
                    r1 = data['10']['relays'].get('1', {})
                    current_on = r1.get('on_val')
                    current_off = r1.get('off_val')

                    # Only print if the value has successfully updated
                    if current_on != last_known_on or current_off != last_known_off:
                        print(
                            f"\n[LIVE UPDATE] Node 10 Relay 1 -> ON: {current_on}, OFF: {current_off}, CH: {r1.get('assigned_ch')}")
                        print("Select option (1 to set, 2 to exit): ", end="", flush=True)
                        last_known_on = current_on
                        last_known_off = current_off

        except Exception as e:
            print(f"[!] Monitor error: {e}")
            time.sleep(1)


def main():
    context = zmq.Context()
    cmd_socket = context.socket(zmq.PUB)
    cmd_socket.connect(ZMQ_PORT_VACUUM_CMD)

    # Start the background listener
    monitor_thread = threading.Thread(target=monitor_stream, daemon=True)
    monitor_thread.start()

    # Give the ZMQ sockets 500ms to handshake before sending data
    time.sleep(0.5)

    print("\n=== Leybold Relay Command Tester ===")

    while True:
        print("\nOptions:")
        print("1. Send Relay Update (Node 10, Relay 1, Channel 1)")
        print("2. Exit")

        choice = input("Select option (1 to set, 2 to exit): ")

        if choice == '1':
            try:
                print("\n(Use scientific notation. e.g., 5.0e-5)")
                on_val = float(input("Enter Turn ON Pressure: "))
                off_val = float(input("Enter Turn OFF Pressure: "))

                payload = {
                    "action": "set_relay",
                    "node": 10,
                    "relay": 1,
                    "channel": 1,
                    "on_val": on_val,
                    "off_val": off_val
                }

                cmd_socket.send_json(payload)
                print(f"[*] Command Sent: {payload}")
                print("[*] Waiting for the round-robin queue to read it back...")

            except ValueError:
                print("[!] Invalid input. Please enter a valid float like 5.0e-5")
        elif choice == '2':
            print("Exiting...")
            break


if __name__ == "__main__":
    main()