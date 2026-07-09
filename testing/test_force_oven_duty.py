import time
import zmq
from src.core.network_map import ZMQ_PORT_PLC_CMD

def main():
    ctx = zmq.Context.instance()
    cmd_socket = ctx.socket(zmq.PUB)
    cmd_socket.connect(ZMQ_PORT_PLC_CMD)

    # Allow ZMQ PUB socket connection to establish
    time.sleep(1)

    # 1. Assume control of the duty cycle
    cmd_socket.send_json({
        "tag": "ion_beam.source.cesium.testing_cmd_force_duty_cycle",
        "value": 15.0,
        "ts": time.time()
    })

    # Allow time for messages to dispatch to the network before exiting
    time.sleep(0.5)

if __name__ == "__main__":
    main()