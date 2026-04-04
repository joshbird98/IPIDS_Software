import snap7
import os

PLC_IP = "192.168.0.10"

client = snap7.client.Client()
client.connect(PLC_IP, 0, 1)   # (IP, Rack, Slot)

data = client.get_connected()
print(f"Connected: {data}")

data = client.get_order_code()
print(f"Code: {data.OrderCode}, with firmware V{data.V1}.{data.V2}.{data.V3}")


# Read 4 bytes from DB1 starting at byte 0
data = client.db_read(1, 0, 4)
print("Raw bytes:", list(data))

client.disconnect()