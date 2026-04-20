import socket
import time
import select

# --- CONFIGURATION ---
WAVESHARE_IP = "192.168.1.203"
WAVESHARE_PORT = 4196

# The physical RS485 address of the new unit (Default is usually "01")
RS485_ADDRESS = "01"

# Device Type: "09" is explicitly for the 30kV MPD models
DEVICE_TYPE = "09"

# Protocol framing bytes
STX = "\x02"
LF = "\x0A"


def calculate_checksum(payload: str) -> str:
    """Calculates the specific Spellman MPD checksum."""
    ascii_sum = sum(ord(c) for c in payload)
    csum = 0x200 - ascii_sum
    csum = csum & 0xFF
    csum = csum & 0x7F
    csum = csum | 0x40
    return f"{csum:02X}"


def generate_frame(address: str, dev_type: str, cmd: str, operator: str = "", data: str = "") -> bytes:
    """Wraps the command in STX, CSUM, and LF bytes."""
    payload = f"{address}{dev_type}{cmd}{operator}{data}"
    csum = calculate_checksum(payload)
    frame = f"{STX}{payload}{csum}{LF}"
    return frame.encode('ascii')


def test_communication():
    print(f"Attempting to connect to Waveshare at {WAVESHARE_IP}:{WAVESHARE_PORT}...")

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect((WAVESHARE_IP, WAVESHARE_PORT))
        print("TCP Connection Established!\n")
    except Exception as e:
        print(f"FAILED to connect to Waveshare: {e}")
        return

    # Commands to test (Safe commands that do not enable HV)
    # Format: (Command, Operator)
    test_commands = [
        ("SR", "?", "Status Register"),
        ("M0", "?", "Voltage Monitor"),
        ("V1", "?", "Programmed Voltage Setpoint"),
        ("EN", "?", "Enable State")
    ]

    for cmd, op, description in test_commands:
        print(f"--- Querying: {description} ({cmd}{op}) ---")

        # 1. Flush stale TCP buffer data
        while select.select([sock], [], [], 0.0)[0]:
            sock.recv(1024)

        # 2. Transmit
        frame = generate_frame(RS485_ADDRESS, DEVICE_TYPE, cmd, op)
        print(f"TX (Raw): {frame}")
        sock.sendall(frame)

        # 3. RS485 Turnaround Delay
        time.sleep(0.1)

        # 4. Receive
        try:
            res = sock.recv(1024)
            print(f"RX (Raw): {res}")

            res_str = res.decode('ascii')
            if res_str.startswith(STX) and res_str.endswith(LF):
                payload = res_str[1:-3]  # Strip STX, CSUM, LF
                print(f"RX (Decoded Payload): {payload}\n")
            else:
                print("WARNING: Response framing is invalid.\n")

        except socket.timeout:
            print("ERROR: Timeout waiting for response. Check RS485 wiring, Address, and DevType.\n")

        time.sleep(0.5)  # Breath between commands

    sock.close()
    print("Test complete. Socket closed.")


if __name__ == "__main__":
    test_communication()