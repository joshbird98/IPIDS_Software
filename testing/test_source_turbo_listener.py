import zmq
import json
import sys
import os

try:
    from network_config import ZMQ_PORT_SRC_TURBO_PUB, TOPIC_SRC_TURBO_DATA
except ImportError:
    ZMQ_PORT_SRC_TURBO_PUB = "tcp://*:5556"
    TOPIC_SRC_TURBO_DATA = b"src_turbo_data"


def run_subscriber():
    context = zmq.Context()
    socket = context.socket(zmq.SUB)

    port = ZMQ_PORT_SRC_TURBO_PUB.split(":")[-1]
    connect_address = f"tcp://localhost:{port}"

    try:
        socket.connect(connect_address)
        if isinstance(TOPIC_SRC_TURBO_DATA, bytes):
            socket.setsockopt(zmq.SUBSCRIBE, TOPIC_SRC_TURBO_DATA)
        else:
            socket.setsockopt_string(zmq.SUBSCRIBE, TOPIC_SRC_TURBO_DATA)
    except Exception as e:
        print(f"Failed to connect: {e}")
        return

    # Enable ANSI escape sequence processing on Windows command prompts
    if os.name == 'nt':
        os.system('')

    # Initial full screen clear
    sys.stdout.write('\033[2J')
    sys.stdout.flush()

    try:
        while True:
            topic, payload = socket.recv_multipart()
            data = json.loads(payload.decode('utf-8'))

            status = data.get('status', {})
            history = data.get('history', {})
            temps = data.get('temps', {})
            elec = data.get('electrical', {})
            service = data.get('service', {})

            # Move cursor to home position (top left) without clearing to prevent flicker
            sys.stdout.write('\033[H')

            # Format string with trailing space padding to overwrite previous, longer strings
            output = (
                f"==================================================\n"
                f" TURBO PUMP STATUS | Serial: {str(data.get('serial')):<8} | HW: {str(data.get('hw_version')):<8}\n"
                f"--------------------------------------------------\n"
                f" [ LIVE TELEMETRY ]                               \n"
                f" Speed:        {data.get('hz', 0):>6} Hz ({data.get('pct', 0.0):>5.1f}% of Max)    \n"
                f" Electrical:   {elec.get('volts', 0):>6} V, {elec.get('amps', 0.0):>5.1f} A      \n"
                f" Temperatures: Bearing: {temps.get('bearing', 0):>3}°C, Converter: {temps.get('converter', 0):>3}°C \n"
                f" Op Hours:     {service.get('hours', 0.0):>8.2f} h                             \n"
                f"--------------------------------------------------\n"
                f" [ ACTIVE LOGIC STATUS ]                          \n"
                f" Comms OK:     {str(data.get('comms_ok')):<10} | Safety Synced: {str(data.get('safety_synced')):<10}\n"
                f" Spinning:     {str(status.get('turning')):<10} | Ready:         {str(status.get('ready')):<10}\n"
                f" Accelerating: {str(status.get('accelerating')):<10} | Decelerating:  {str(status.get('decelerating')):<10}\n"
                f" Normal Op:    {str(status.get('normal_operation')):<10} |                                \n"
                f" ACTIVE ERROR: {'YES' if status.get('error_active') else 'No':<10} | ACTIVE WARN:   {'YES' if status.get('warning_active') else 'No':<10}\n"
                f"--------------------------------------------------\n"
                f" [ HARDWARE MEMORY / LOGS ]                       \n"
                f" Latest Error:   {str(history.get('most_recent_error_code')):<4} - {str(history.get('most_recent_error_desc')):<40}\n"
                f" Latest Warning: {str(history.get('most_recent_warning_desc')):<50}\n"
                f"==================================================\n"
            )
            sys.stdout.write(output)
            sys.stdout.flush()

    except KeyboardInterrupt:
        # Move cursor down completely before exiting so it doesn't overwrite the dashboard
        sys.stdout.write('\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\nSubscriber terminated by user.\n')
    finally:
        socket.close()
        context.term()


if __name__ == "__main__":
    run_subscriber()