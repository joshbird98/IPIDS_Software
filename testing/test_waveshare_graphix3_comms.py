import socket

# --- CONFIGURATION ---
WAVESHARE_IP = "192.168.1.200"
WAVESHARE_PORT = 4196
TIMEOUT = 3.0
RS485_ADDRESS = 1  # Verify this matches the address assigned in the Graphix 3 front panel settings


def calculate_crc(payload_bytes):
    """
    Calculates the Leybold CRC.
    Payload must start with SI/SO and exclude the RS485 address.
    """
    byte_sum = sum(payload_bytes)
    crc_val = 255 - (byte_sum % 256)
    if crc_val < 32:
        crc_val += 32
    return bytes([crc_val])


def build_read_command(address, param_group, param_no):
    """Builds the raw RS485 byte string for a read command."""
    # Format address as 2-character hex string (e.g., 1 -> "01", 10 -> "0A")
    addr_str = f"{address:02X}".encode('ascii')

    # Payload structure: [SI] + Group + ";" + Param
    payload = b"\x0f" + str(param_group).encode('ascii') + b";" + str(param_no).encode('ascii')

    crc = calculate_crc(payload)
    eot = b"\x04"

    return addr_str + payload + crc + eot


def main():
    # Hello World test: Read Hardware and Software version (Group 5, Parameter 1)
    command = build_read_command(RS485_ADDRESS, 5, 1)

    print(f"Connecting to {WAVESHARE_IP}:{WAVESHARE_PORT}...")
    print(f"Transmitting Command Bytes: {command}")

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(TIMEOUT)
            s.connect((WAVESHARE_IP, WAVESHARE_PORT))
            s.sendall(command)

            # Read response from the converter
            response = s.recv(1024)
            print(f"Raw Response Bytes: {response}")

            if len(response) > 0:
                # Check for ACK (0x06)
                if b"\x06" in response:
                    print("\nStatus: ACK (Success)")
                    # Response structure: [Addr1][Addr2][ACK][Value][CRC][EOT]
                    ack_idx = response.find(b"\x06")
                    # Slice out the value between ACK and the last two bytes (CRC + EOT)
                    val_data = response[ack_idx + 1:-2]
                    print(f"Parsed Value: {val_data.decode('ascii', errors='ignore')}")

                # Check for NACK (0x15)
                elif b"\x15" in response:
                    print("\nStatus: NACK (Error)")
                    nack_idx = response.find(b"\x15")
                    err_data = response[nack_idx + 1:-2]
                    print(f"Error Code: {err_data.decode('ascii', errors='ignore')}")

                else:
                    print("\nStatus: Unknown response format.")
            else:
                print("\nError: Empty response. Transmitted successfully but no reply received.")

    except socket.timeout:
        print("\nError: Socket timeout. The Waveshare is reachable, but the Graphix 3 did not reply.")
    except Exception as e:
        print(f"\nConnection/Transmission Error: {e}")


if __name__ == "__main__":
    main()