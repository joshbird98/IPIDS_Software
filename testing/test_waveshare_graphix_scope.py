import socket
import time

# --- CONFIGURATION ---
WAVESHARE_IP = "192.168.1.200"
WAVESHARE_PORT = 4196
TIMEOUT = 0.3  # Short timeout to keep the loop moving if a packet drops
RS485_ADDRESS = 11  # Matches your Graphix 3 node
POLL_INTERVAL = 0.06  # 1 second gap between packets for clear scoping


def calculate_crc(payload_bytes):
    byte_sum = sum(payload_bytes)
    crc_val = 255 - (byte_sum % 256)
    if crc_val < 32:
        crc_val += 32
    return bytes([crc_val])


def build_read_command(address, param_group, param_no):
    addr_str = f"{address:02X}".encode('ascii')
    payload = b"\x0f" + str(param_group).encode('ascii') + b";" + str(param_no).encode('ascii')
    crc = calculate_crc(payload)
    eot = b"\x04"
    return addr_str + payload + crc + eot


def generate_graphix_frame(node_id: int, param_group: str, param_no: str) -> bytes:
    # 1. Format Address (2-character ASCII Hex)
    address = f"{node_id:02X}".encode('ascii')

    # 2. Construct Body: [SI] + Parameter Group + ';' + Parameter No.
    # [SI] is 0x0F
    body = b'\x0f' + param_group.encode('ascii') + b';' + param_no.encode('ascii')

    # 3. Calculate CRC (excluding address)
    byte_sum = sum(body)
    crc_val = 255 - (byte_sum % 256)
    if crc_val < 32:
        crc_val += 32
    crc_byte = bytes([crc_val])

    # 4. End of Transmission [EOT] is 0x04
    eot = b'\x04'

    # 5. Assemble Payload
    return address + body + crc_byte + eot

def continuous_poll():

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(TIMEOUT)
                print(f"Connecting to {WAVESHARE_IP}:{WAVESHARE_PORT}...")
                s.connect((WAVESHARE_IP, WAVESHARE_PORT))

                print("Connected! Starting continuous polling.")
                print("Press Ctrl+C to stop.\n" + "-" * 40)
                broken_count_10 = 0
                okay_count_10 = 0
                broken_count_20 = 0
                okay_count_20 = 0
                while True:

                    # Encode the string into an ASCII byte array for the serial port
                    test_payload = generate_graphix_frame(node_id=10, param_group="5", param_no="2")
                    #print(f"TX -> : {test_payload}")
                    s.sendall(test_payload)

                    try:
                        # Read response from the converter
                        response = s.recv(1024)
                        #print(f"RX <- : {response}")

                        # Basic parser for visual feedback
                        if b"\x06" in response:
                            ack_idx = response.find(b"\x06")
                            val_data = response[ack_idx + 1:-2]
                            #print(f"Data  : {val_data.decode('ascii', errors='ignore')}")
                            okay_count_10 += 1
                        elif b"\x15" in response:
                            nack_idx = response.find(b"\x15")
                            err_data = response[nack_idx + 1:-2]
                            print(f"Error : {err_data.decode('ascii', errors='ignore')}")
                        else:
                            #(f"Broken message - {broken_count}")
                            broken_count_10 += 1

                    except socket.timeout:
                        print("RX <- : [TIMEOUT - No reply detected]")

                    time.sleep(POLL_INTERVAL)

                    # Encode the string into an ASCII byte array for the serial port
                    test_payload = generate_graphix_frame(node_id=20, param_group="5", param_no="2")
                    # print(f"TX -> : {test_payload}")
                    s.sendall(test_payload)

                    try:
                        # Read response from the converter
                        response = s.recv(1024)
                        # print(f"RX <- : {response}")

                        # Basic parser for visual feedback
                        if b"\x06" in response:
                            ack_idx = response.find(b"\x06")
                            val_data = response[ack_idx + 1:-2]
                            #print(f"Data  : {val_data.decode('ascii', errors='ignore')}")
                            okay_count_20 += 1
                        elif b"\x15" in response:
                            nack_idx = response.find(b"\x15")
                            err_data = response[nack_idx + 1:-2]
                            print(f"Error : {err_data.decode('ascii', errors='ignore')}")
                        else:
                            #print(f"Broken message - {broken_count_20}")
                            broken_count_20 += 1

                    except socket.timeout:
                        print("RX <- : [TIMEOUT - No reply detected]")

                    print(f"Total loops: {okay_count_10 + broken_count_10 + okay_count_20 + broken_count_20}")
                    print(f"ID10 {100 * okay_count_10 / (okay_count_10 + broken_count_10)}%")
                    print(f"ID20 {100 * okay_count_20 / (okay_count_20 + broken_count_20)}%")
                    time.sleep(POLL_INTERVAL)


        except KeyboardInterrupt:
            print("\nPolling stopped by user.")
        except Exception as e:
            print(f"\nConnection Error: {e}")


if __name__ == "__main__":
    continuous_poll()