import zmq
import time
import json
import sys

# Import the command port from your project's network map
from src.core.network_map import ZMQ_PORT_SPELLMAN_CMD
try:
    from testing.cmd_helper import send as send_ack
except ImportError:
    from cmd_helper import send as send_ack


def run_commander():
    # Change this if you need to control a different Spellman unit (tag prefix in the registry)
    target_device = "ion_beam.source.einzel"

    def send_command(cmd_type: str, value: float):
        send_ack(f"{target_device}.{cmd_type}", value, subsystem="spellman")

    print("\n=== SPELLMAN MPD COMMANDER ===")
    print(f"Targeting: {target_device}")
    print("-" * 30)
    print("  e      : Enable High Voltage")
    print("  d      : Disable High Voltage")
    print("  v <kV> : Set Voltage (e.g., 'v 12.5')")
    print("  q      : Quit")
    print("-" * 30)

    try:
        while True:
            user_input = input("\nCMD > ").strip().lower()

            if not user_input:
                continue

            if user_input == 'q' or user_input == 'quit':
                print("Exiting commander...")
                break

            elif user_input == 'e':
                send_command("cmd_enable", 1.0)

            elif user_input == 'd':
                send_command("cmd_enable", 0.0)

            elif user_input.startswith('v '):
                try:
                    # Extract the voltage float from the string
                    kv_req = float(user_input.split(' ')[1])

                    # 1. Dispatch Voltage Setpoint
                    send_command("sp_requested_voltage", kv_req)

                    # 2. Dispatch Current Setpoint
                    send_command("sp_requested_current", 0.1)

                except ValueError:
                    print("  -> ERROR: Invalid voltage format. Example usage: 'v 5.0'")
            else:
                print("  -> ERROR: Unknown command.")

    except KeyboardInterrupt:
        print("\nExiting commander...")
    finally:
        pass


if __name__ == "__main__":
    run_commander()