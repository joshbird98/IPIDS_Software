import socket
import time

# --- CONFIGURATION ---
WAVESHARE_IP = "192.168.1.200"
WAVESHARE_PORT = 4196

# Payload of twenty 0x55 bytes (01010101 binary) to generate a clear square wave
TEST_PAYLOAD = b'\x55' * 20


def continuous_transmit():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(3.0)
            s.connect((WAVESHARE_IP, WAVESHARE_PORT))

            print(f"Connected to {WAVESHARE_IP}:{WAVESHARE_PORT}.")
            print("Continuously transmitting 0x55 square wave pattern...")
            print("Press Ctrl+C to stop.")

            while True:
                s.sendall(TEST_PAYLOAD)
                # 50ms delay between packets to allow scope to re-arm trigger
                time.sleep(0.05)

    except KeyboardInterrupt:
        print("\nTransmission stopped.")
    except ConnectionRefusedError:
        print("\nError: Connection refused. Verify IP/Port.")
    except Exception as e:
        print(f"\nConnection Error: {e}")


if __name__ == "__main__":
    continuous_transmit()