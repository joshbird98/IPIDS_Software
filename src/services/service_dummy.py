import sys
import time
import zmq
from src.core.network_config import ZMQ_PORT_HEARTBEAT


def run_dummy(mode):
    context = zmq.Context()
    hb_socket = context.socket(zmq.PUB)
    hb_socket.connect(ZMQ_PORT_HEARTBEAT)

    print(f"[DUMMY] Started in {mode} mode.")
    start_time = time.time()
    last_hb_time = 0.0

    while True:
        current_time = time.time()

        # 2Hz Heartbeat
        if current_time - last_hb_time >= 0.5:
            hb_socket.send_json({"service": "service_dummy", "ts": current_time})
            last_hb_time = current_time

        # Trigger failure after 5 seconds
        if current_time - start_time > 5.0:
            if mode == "crash":
                print("[DUMMY] Simulating fatal exception...")
                raise RuntimeError("Simulated memory segmentation fault.")

            elif mode == "hang":
                print("[DUMMY] Simulating deadlock (blocking thread)...")
                time.sleep(10.0)  # Blocks the heartbeat loop

        time.sleep(0.1)


if __name__ == "__main__":
    # Default to crash if no arg provided
    failure_mode = sys.argv[1] if len(sys.argv) > 1 else "crash"
    run_dummy(failure_mode)