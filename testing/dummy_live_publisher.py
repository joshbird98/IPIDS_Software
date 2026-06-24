import time
import math
import zmq
import orjson
from src.core.network_map import ZMQ_PORT_MAGNET_PUB, TOPIC_MAGNET_DATA


def run_dummy_publisher():
    context = zmq.Context()
    socket = context.socket(zmq.PUB)

    # Bind to the magnet telemetry port
    socket.bind(ZMQ_PORT_MAGNET_PUB)

    # Ensure topic is bytes
    topic = TOPIC_MAGNET_DATA if isinstance(TOPIC_MAGNET_DATA, bytes) else TOPIC_MAGNET_DATA.encode('utf-8')

    print(f"[Dummy Publisher] Bound to {ZMQ_PORT_MAGNET_PUB}")
    print(f"[Dummy Publisher] Streaming 20Hz telemetry on topic: {topic.decode('utf-8')} ...")

    t0 = time.time()
    try:
        while True:
            t = time.time() - t0

            # Match the flattened dictionary structure of service_magnet.py
            # Including both the TEST tags and standard magnet tags for registry compliance
            payload = {
                "timestamp": time.time(),
                "TEST_VOLTAGE1": 30.0 + 5.0 * math.sin(t / 1.0),
                "TEST_CURRENT1": 150.0 + 50.0 * math.cos(t / 3.5),

                "TEST_VOLTAGE2": 20.0 + 3.0 * math.sin(t / 2.1),
                "TEST_CURRENT2": 140.0 + 20.0 * math.cos(t / 3.3),

                "TEST_VOLTAGE3": 10.0 + 1.0 * math.sin(t / 5.0),
                "TEST_CURRENT3": 180.0 + 80.0 * math.cos(t / 2.5),

                "TEST_VOLTAGE4": 14.0 + 25.0 * math.sin(t / 9.0),
                "TEST_CURRENT4": 10.0 + 1.0 * math.cos(t / 4.5),

                "TEST_VOLTAGE5": 90.0 + 3.0 * math.sin(t / 3.0),
                "TEST_CURRENT5": 190.0 + 20.0 * math.cos(t / 1.5),

                "ion_beam.beamline.magnet.rb_voltage": 30.0 + 5.0 * math.sin(t / 5.0),
                "ion_beam.beamline.magnet.rb_current": 150.0 + 50.0 * math.cos(t / 2.5)
            }

            # Send as standard PyZMQ multipart: [b"TOPIC", b"JSON"]
            socket.send_multipart([topic, orjson.dumps(payload)])

            time.sleep(0.05)  # 20Hz

    except KeyboardInterrupt:
        print("\n[Dummy Publisher] Shutting down.")
        socket.close()
        context.term()


if __name__ == "__main__":
    run_dummy_publisher()