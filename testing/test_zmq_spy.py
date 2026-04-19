import zmq
import json
from config.network_config import ZMQ_PORT_VACUUM_PUB, TOPIC_VACUUM_DATA

context = zmq.Context()
sock = context.socket(zmq.SUB)
sock.connect(ZMQ_PORT_VACUUM_PUB)
sock.setsockopt(zmq.SUBSCRIBE, TOPIC_VACUUM_DATA)

print("Spying on Vacuum Service... (Ctrl+C to stop)")
while True:
    topic, payload = sock.recv_multipart()
    data = json.loads(payload.decode())
    print(f"Incoming Data: {json.dumps(data, indent=2)}")