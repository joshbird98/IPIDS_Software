import socket
import time


def calc_graphix_crc(body: bytes) -> bytes:
    """Calculates the CRC based on the Graphix manual specification."""
    val = 255 - (sum(body) % 256)
    if val < 32:
        val += 32
    return bytes([val])


def generate_rs232_read_frame(param_group: str, param_no: str, channel: str = "") -> bytes:
    """Generates an RS232 frame for the Graphix 3."""
    si = b'\x0f'
    eot = b'\x04'

    if channel:
        body_str = f"{channel};{param_group};{param_no}"
    else:
        body_str = f"{param_group};{param_no}"

    body = si + body_str.encode('ascii')
    crc_byte = calc_graphix_crc(body)

    return body + crc_byte + eot


def scan_parameter_group(group_num: str, start_param: int = 1, end_param: int = 38):
    # Network Configuration
    WAVESHARE_IP = '192.168.1.200'
    WAVESHARE_PORT = 4196
    TIMEOUT_SECONDS = 0.5  # Shorter timeout for faster scanning of empty registers

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(TIMEOUT_SECONDS)
            s.connect((WAVESHARE_IP, WAVESHARE_PORT))

            print(f"Connected to Waveshare RS232 at {WAVESHARE_IP}:{WAVESHARE_PORT}")
            print(f"Scanning Group {group_num}, Parameters {start_param} to {end_param}...")
            print("-" * 50)

            for param_no in range(start_param, end_param + 1):
                payload = generate_rs232_read_frame(param_group=group_num, param_no=str(param_no))

                # Transmit
                s.sendall(payload)

                # Receive
                try:
                    reply = s.recv(1024)
                    if reply:
                        print(f"Param {param_no:02d} | TX: {payload} | RX: {reply}")
                    else:
                        print(f"Param {param_no:02d} | TX: {payload} | RX: [Connection closed]")
                except socket.timeout:
                    print(f"Param {param_no:02d} | TX: {payload} | RX: [TIMEOUT]")

                # Hardware delay between queries
                time.sleep(0.2)

            print("-" * 50)
            print("Scan Complete.")

    except ConnectionRefusedError:
        print(f"Network Error: Connection refused at {WAVESHARE_IP}:{WAVESHARE_PORT}")
    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    scan_parameter_group(group_num="2", start_param=1, end_param=38)