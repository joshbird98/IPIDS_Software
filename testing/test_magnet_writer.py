import zmq
import time

# Adjust import path if running from a different directory level
from src.core.network_map import ZMQ_PORT_MAGNET_CMD
try:
    from testing.cmd_helper import send as send_ack
except ImportError:
    from cmd_helper import send as send_ack


def send_command(pub_socket, cmd_type: str, value: float):
    """Commands are acknowledged now; pub_socket is unused (kept for the call sites)."""
    send_ack(f"ion_beam.beamline.magnet.{cmd_type}", value, subsystem="magnet")


def command_cli():
    pub_socket = None
    print(f"[Command Injector] Magnet commands via {ZMQ_PORT_MAGNET_CMD} (acknowledged)")

    menu = """
    Available Commands:
    1: Set Voltage (V)  [refused: voltage follows the current setpoint]
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
                send_command(pub_socket, "sp_requested_voltage", val)
            elif choice == '2':
                val = float(input("Enter Current Setpoint (A): "))
                send_command(pub_socket, "sp_requested_current", val)
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


if __name__ == "__main__":
    command_cli()