import zmq
import orjson
import time

# Use the same port string, but use CONNECT
ZMQ_PORT_EVENTS_PUB = "tcp://127.0.0.1:5567"


def send_fake_event():
    context = zmq.Context()
    pub = context.socket(zmq.PUB)

    # CRITICAL: We CONNECT here because the Logger is already BINDING
    pub.connect(ZMQ_PORT_EVENTS_PUB)

    # ZMQ needs a tiny moment to hand-shake after connecting
    time.sleep(0.1)

    event = {
        "ts": time.time(),
        "service": "test_script",
        "severity": "INFO",
        "type": "USER_ACTION",
        "message": "Manual test event fired from Pycharm",
        "metadata": {"test_status": "success", "debug": True}
    }

    # Send with the "EVENTS" topic prefix
    pub.send_multipart([b"EVENTS", orjson.dumps(event)])
    print("[Test] Event sent!")

    pub.close()
    context.term()


if __name__ == "__main__":
    send_fake_event()