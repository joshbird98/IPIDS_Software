import zmq
import time

# Adjust import path if running from a different directory level
from src.core.network_map import ZMQ_PORT_MAGNET_CMD


def send_command(pub_socket: zmq.Socket, cmd_type: str, value: float):
    tag = f"ion_beam.magnet.{cmd_type}"
    payload = {
        "tag": tag,
        "value": value,
        "ts": time.time()
    }
    # Microservice uses recv_json directly on the SUB socket
    pub_socket.send_json(payload)
    print(f"[Command Injector] Sent -> {tag}: {value}")


def command_cli():
    context = zmq.Context()
    pub_socket = context.socket(zmq.PUB)

    # Connect to the SUB socket bound by the microservice
    pub_socket.connect(ZMQ_PORT_MAGNET_CMD)

    # Allow ZMQ connection to establish
    time.sleep(0.5)
    print(f"[Command Injector] Connected to {ZMQ_PORT_MAGNET_CMD}")

    menu = """
    Available Commands:
    1: Set Voltage (V)
    2: Set Current (A)
    3: Enable Output
    4: Disable Output
    q: Quit
    """

    try:
        while True:
            print(menu)
            choice = input("Select an option: ").strip().lower()

            if choice == 'q':
                break

            elif choice == '1':
                val = float(input("Enter Voltage Setpoint (V): "))
                send_command(pub_socket, "voltage_sp", val)
            elif choice == '2':
                val = float(input("Enter Current Setpoint (A): "))
                send_command(pub_socket, "current_sp", val)
            elif choice == '3':
                send_command(pub_socket, "cmd_enable", 1)
            elif choice == '4':
                send_command(pub_socket, "cmd_enable", 0)
            else:
                print("Invalid choice.")

    except ValueError:
        print("Invalid input. Please enter a numerical value.")
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[Command Injector] Shutting down.")
        pub_socket.close()
        context.term()


if __name__ == "__main__":
    command_cli()