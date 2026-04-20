import zmq
import time

from network_config import ZMQ_PORT_PLC_CMD


def run_commander():
    context = zmq.Context()

    # The microservice binds a SUB socket, so the commander must use a PUB socket to connect
    pub_socket = context.socket(zmq.PUB)
    pub_socket.setsockopt(zmq.SNDHWM, 5)  # Drop outbound messages if queue exceeds 5

    print(f"Connecting Commander to {ZMQ_PORT_PLC_CMD}...")
    pub_socket.connect(ZMQ_PORT_PLC_CMD)

    # ZMQ PUB/SUB architecture requires a brief handshake period after connecting.
    # Without this sleep, the first few messages will be dropped into the void.
    time.sleep(0.5)

    print("Starting Triangle Wave Generator... (Press Ctrl+C to stop)\n")

    # Triangle wave parameters
    sp_voltage = 0.0
    step_size = 1
    max_volts = 20
    min_volts = 0

    toggle_bool = True

    try:
        while True:
            # 1. Update Triangle Wave Math
            sp_voltage += step_size

            # Reverse direction at the peaks
            if sp_voltage >= max_volts or sp_voltage <= min_volts:
                step_size = -step_size
                # Toggle the boolean state only at the wave peaks
                toggle_bool = not toggle_bool

            current_time = time.time()

            # 2. Create the JSON payloads
            msg_analog = {
                "tag": "ion_beam.source.extraction.sp_voltage",
                "value": round(sp_voltage, 2),
                "ts": current_time
            }

            msg_bool = {
                "tag": "ion_beam.source.extraction.cmd_enable",
                "value": toggle_bool,
                "ts": current_time
            }

            # 3. Send commands sequentially
            # The microservice's _process_commands() method loops through the socket
            # buffer with zmq.NOBLOCK, so it will instantly pick up both of these writes.
            pub_socket.send_json(msg_analog)
            pub_socket.send_json(msg_bool)

            print(f"Sent -> Voltage SP: {msg_analog['value']:05.2f} V | Enable CMD: {msg_bool['value']} @ {current_time}")

            # 300ms delay yields a visually smooth wave in TIA Portal (10Hz update rate)
            time.sleep(0.5)

    except KeyboardInterrupt:
        print("\nCommander stopped.")
    finally:
        pub_socket.close()
        context.term()


if __name__ == "__main__":
    run_commander()