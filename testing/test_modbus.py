from pymodbus.client import ModbusTcpClient

# The modern v3.x way uses the FramerType enum
from pymodbus.framer import FramerType

IP = "192.168.1.11"
PORT = 503


def test_rtu_over_tcp():
    print(f"Connecting to {IP}:{PORT} using Modbus RTU-over-TCP framing...")

    # Pass FramerType.RTU directly to the client
    client = ModbusTcpClient(IP, port=PORT, framer=FramerType.RTU, timeout=1.0)

    if not client.connect():
        print("Failed to open TCP socket.")
        return

    print("Socket connected! Requesting Actual Voltage (Register 507)...")

    # 0x01FB is 507 in decimal. Using device_id=0
    res = client.read_holding_registers(address=507, count=1, device_id=0)
    # Note: If slave=0 gives a warning, try device_id=0 depending on your exact minor version

    if res.isError():
        print(f"Failed to read: {res}")
    else:
        print("\nSUCCESS!")
        print(f"Raw Register 507 Value: {res.registers[0]}")

    client.close()


if __name__ == "__main__":
    test_rtu_over_tcp()