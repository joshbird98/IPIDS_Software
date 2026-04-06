import socket
import time


def test_tcp_hex_transmission():
    # --- Network Configuration ---
    WAVESHARE_IP = '192.168.1.200'
    # Double-check the configured "Local Port" in the Waveshare web interface
    WAVESHARE_PORT = 4196
    TIMEOUT_SECONDS = 2.0

    # The exact command packet provided
    hex_command = "7e 20 30 35 20 30 31 20 30 30 0D"

    # Convert string of hex pairs into a raw byte payload
    payload = bytes.fromhex(hex_command)

    try:
        # Create a TCP/IP socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(TIMEOUT_SECONDS)
            s.connect((WAVESHARE_IP, WAVESHARE_PORT))

            print(f"Connected to {WAVESHARE_IP}:{WAVESHARE_PORT}")
            print(f"TX -> : {payload}")
            print(f"TX (Hex) : {payload.hex(' ')}")
            print("-" * 40)

            # Transmit
            s.sendall(payload)

            # Wait briefly for device processing turnaround time
            time.sleep(0.1)

            # Read whatever is sitting in the receive buffer
            try:
                reply = s.recv(1024)
                if reply:
                    print(f"RX <- : {reply}")
                    print(f"RX (Hex) : {reply.hex(' ')}")
                else:
                    print("RX <- : [Connection closed by remote host]")
            except socket.timeout:
                print("RX <- : [TIMEOUT - No reply detected within window]")

    except ConnectionRefusedError:
        print(
            f"Network Error: Connection refused. Verify the IP and Port ({WAVESHARE_PORT}) are correct, and the Waveshare is set to TCP Server mode.")
    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    test_tcp_hex_transmission()