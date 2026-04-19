import socket
import time
import math
from collections import deque
import matplotlib.pyplot as plt
from datetime import datetime


def calc_graphix_crc(body: bytes) -> bytes:
    val = 255 - (sum(body) % 256)
    if val < 32:
        val += 32
    return bytes([val])


def generate_rs232_read_frame(param_group: str, param_no: str, channel: str = "") -> bytes:
    si = b'\x0f'
    eot = b'\x04'
    body_str = f"{channel};{param_group};{param_no}" if channel else f"{param_group};{param_no}"
    body = si + body_str.encode('ascii')
    return body + calc_graphix_crc(body) + eot


def parse_graphix_float(reply: bytes) -> float:
    """Extracts the pressure float string and converts it to a standard Python float."""
    if len(reply) >= 4 and reply[0] == 0x06 and reply[-1] == 0x04:
        try:
            data_str = reply[1:-2].decode('ascii')
            # Python's float() natively handles scientific notation like '3.11e-08'
            return float(data_str)
        except ValueError:
            return math.nan
    return math.nan


def monitor_and_plot():
    # --- Network Configuration ---
    WAVESHARE_IP = '192.168.1.200'
    WAVESHARE_PORT = 4196
    TIMEOUT_SECONDS = 1.0

    groups = ["1", "2", "3"]
    param_no = "29"

    # --- Plot Setup ---
    plt.ion()  # Turn on interactive mode for live updates
    fig, ax = plt.subplots(figsize=(10, 6))

    # Use deques for a rolling window of the last 150 data points
    MAX_POINTS = 150
    times = deque(maxlen=MAX_POINTS)
    ch1_data = deque(maxlen=MAX_POINTS)
    ch2_data = deque(maxlen=MAX_POINTS)
    ch3_data = deque(maxlen=MAX_POINTS)

    # Initialize the lines
    line1, = ax.plot([], [], label='Ch 1', color='#1f77b4', marker='.')
    line2, = ax.plot([], [], label='Ch 2', color='#2ca02c', marker='.')
    line3, = ax.plot([], [], label='Ch 3', color='#d62728', marker='.')

    # Format the graph for vacuum pressures
    ax.set_yscale('log')
    ax.set_title('Live Vacuum Pressure (Graphix 3)')
    ax.set_ylabel('Pressure (mbar)')
    ax.set_xlabel('Elapsed Time (Seconds)')
    ax.grid(True, which="both", ls="--", alpha=0.3)
    ax.legend(loc='upper right')

    print("Initiating Live Pressure Plotter...")
    print("Close the graph window or press Ctrl+C in the terminal to terminate.")
    print("-" * 65)

    start_time = time.time()

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(TIMEOUT_SECONDS)
            s.connect((WAVESHARE_IP, WAVESHARE_PORT))

            # Loop runs as long as the matplotlib window is open
            while plt.fignum_exists(fig.number):
                current_elapsed = time.time() - start_time
                times.append(current_elapsed)

                current_readings = []
                console_log = [f"[{datetime.now().strftime('%H:%M:%S')}]"]

                # Poll each channel sequentially
                for group in groups:
                    payload = generate_rs232_read_frame(param_group=group, param_no=param_no)
                    s.sendall(payload)

                    val = math.nan
                    try:
                        reply = s.recv(1024)
                        if reply:
                            val = parse_graphix_float(reply)
                            display_str = f"{val:.2e}" if not math.isnan(val) else "ERR"
                            console_log.append(f"Ch{group}: {display_str}")
                        else:
                            console_log.append(f"Ch{group}: Closed")
                    except socket.timeout:
                        console_log.append(f"Ch{group}: T/O")

                    current_readings.append(val)
                    time.sleep(0.1)  # 100ms hardware delay between commands

                # Print to console
                print(" | ".join(console_log))

                # Append to plotting data arrays
                ch1_data.append(current_readings[0])
                ch2_data.append(current_readings[1])
                ch3_data.append(current_readings[2])

                # Update the graph lines
                line1.set_data(times, ch1_data)
                line2.set_data(times, ch2_data)
                line3.set_data(times, ch3_data)

                # Dynamically scale the X and Y axes to fit new data
                ax.relim()
                ax.autoscale_view()
                ax.set_xlim(left=max(0, current_elapsed - MAX_POINTS), right=current_elapsed + 5)

                # plt.pause processes GUI system_logs and acts as our polling delay
                # We use 0.7s because the 3x 100ms delays above already took 0.3s
                plt.pause(0.7)

    except KeyboardInterrupt:
        print("\nMonitoring terminated by user.")
    except ConnectionRefusedError:
        print(f"\nNetwork Error: Connection refused at {WAVESHARE_IP}:{WAVESHARE_PORT}")
    except Exception as e:
        print(f"\nRuntime Error: {e}")
    finally:
        plt.close('all')


if __name__ == "__main__":
    monitor_and_plot()