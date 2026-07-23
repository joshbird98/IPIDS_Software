import socket
import time

IP_ADDRESS = "192.168.1.14"
PORT = 5025
TIMEOUT = 60.0  # Self-tests block the instrument until complete.


def run_selftest():
    print(f"Connecting to {IP_ADDRESS}:{PORT}...")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(TIMEOUT)
        sock.connect((IP_ADDRESS, PORT))
        print("Connected.\n")

        # Clear existing errors from memory
        sock.sendall(b"*CLS\n")
        time.sleep(0.1)

        # Start self-test
        print("Initiating *TST? (Please wait, this blocks execution until finished...)")
        sock.sendall(b"*TST?\n")

        # Wait for response
        tst_result = sock.recv(1024).decode('utf-8').strip()
        print(f"\nSelf-Test Result Code: {tst_result}")

        if tst_result == "0" or tst_result == "+0":
            print("Status: PASS (No hardware faults detected).")
        else:
            print("Status: FAIL (Hardware faults detected).")

        # Drain and print the error queue
        print("\nPolling Error Queue (:SYST:ERR?)...")
        while True:
            sock.sendall(b":SYST:ERR?\n")
            err_str = sock.recv(1024).decode('utf-8').strip()

            # SCPI standard format for no error is +0,"No error" or 0,"No error"
            if err_str.startswith('+0,') or err_str.startswith('0,'):
                print("End of error queue.")
                break

            print(f"Instrument Error: {err_str}")

    except socket.timeout:
        print(f"\n[TIMEOUT] The instrument did not return a result within {TIMEOUT} seconds.")
    except Exception as e:
        print(f"\n[ERROR] Execution failed: {e}")
    finally:
        sock.close()
        print("\nConnection closed.")


if __name__ == "__main__":
    run_selftest()