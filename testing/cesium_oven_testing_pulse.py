import time
import zmq
import json
import threading
import sys
import csv

# Import your actual network map here.
from src.core.network_map import ZMQ_PORT_VACUUM_PUB, ZMQ_PORT_PLC_CMD, ZMQ_PORT_PLC_PUB

# --- FAULT BIT MAP ---
BIT_SRC_BODY_TEMP_HIGH = 4
BIT_CESIUM_OVEN_OVERTEMP = 5
BIT_CESIUM_HEAT_FAIL = 6
BIT_CESIUM_COOL_FAIL = 7


class CesiumTransientTester:
    def __init__(self):
        self.running = True
        self.current_temp = 0.0
        self.fault_word_2 = 0

        # Setup ZMQ Context
        self.ctx = zmq.Context.instance()

        # Command Publisher
        self.cmd_socket = self.ctx.socket(zmq.PUB)
        self.cmd_socket.connect(ZMQ_PORT_PLC_CMD)

        # Telemetry Subscriber
        self.telemetry_socket = self.ctx.socket(zmq.SUB)
        self.telemetry_socket.connect(ZMQ_PORT_PLC_PUB)
        self.telemetry_socket.setsockopt(zmq.SUBSCRIBE, b"")

        # Start background telemetry thread
        self.t_thread = threading.Thread(target=self._telemetry_loop, daemon=True)
        self.t_thread.start()
        time.sleep(1)

    def send_command(self, tag: str, value):
        payload = {"tag": tag, "value": value, "ts": time.time()}
        self.cmd_socket.send_json(payload)

    def _telemetry_loop(self):
        while self.running:
            try:
                if self.telemetry_socket.poll(100):
                    topic, payload = self.telemetry_socket.recv_multipart(flags=zmq.NOBLOCK)
                    data = json.loads(payload)

                    if "ion_beam.source.cesium.rb_temp" in data:
                        self.current_temp = float(data["ion_beam.source.cesium.rb_temp"])

                    if "ion_beam.faults.word_2_source" in data:
                        self.fault_word_2 = int(data["ion_beam.faults.word_2_source"])
            except Exception:
                pass

    def check_for_faults(self):
        faults = []
        if bool(self.fault_word_2 & (1 << BIT_SRC_BODY_TEMP_HIGH)): faults.append("Src_Body_Temp_High")
        if bool(self.fault_word_2 & (1 << BIT_CESIUM_OVEN_OVERTEMP)): faults.append("Cesium_Oven_OverTemp")
        if bool(self.fault_word_2 & (1 << BIT_CESIUM_HEAT_FAIL)): faults.append("Cesium_Oven_HeatFail")
        if bool(self.fault_word_2 & (1 << BIT_CESIUM_COOL_FAIL)): faults.append("Cesium_Oven_CoolFail")

        if faults:
            print(f"\n[CRITICAL] Faults detected: {', '.join(faults)}")
            self.graceful_exit()
            sys.exit(1)

    def graceful_exit(self):
        print("\n[CLEANUP] Releasing overrides and zeroing duty cycle.")
        self.send_command("ion_beam.source.cesium.testing_cmd_duty_cycle_value", 0.0)
        self.send_command("ion_beam.source.cesium.cmd_force_cooling", False)
        self.send_command("ion_beam.source.cesium.testing_cmd_force_duty_cycle", False)
        self.running = False

    def run_transient_test(self, pulse_duty=15.0, pulse_duration=60, csv_filename="cesium_transient_data.csv"):
        # 1. Enforce starting conditions
        if self.current_temp > 30.0:
            print(f"[ERROR] Oven is too hot ({self.current_temp}°C). Must start from < 30.0°C.")
            self.graceful_exit()
            return

        try:
            with open(csv_filename, mode='w', newline='') as file:
                writer = csv.writer(file)
                writer.writerow(["Time (s)", "Temperature (C)", "Duty Cycle (%)"])

                print("Starting Transient Profiling Sequence...")

                # 2. Assume control and ensure forced cooling is OFF
                self.send_command("ion_beam.source.cesium.testing_cmd_force_duty_cycle", True)
                self.send_command("ion_beam.source.cesium.cmd_force_cooling", False)

                start_time = time.time()

                # 3. Apply Pulse
                print(f"\n>>> APPLYING {pulse_duty}% STEP PULSE FOR {pulse_duration} SECONDS")
                self.send_command("ion_beam.source.cesium.testing_cmd_duty_cycle_value", pulse_duty)

                while (time.time() - start_time) < pulse_duration:
                    self.check_for_faults()
                    elapsed = time.time() - start_time
                    writer.writerow([round(elapsed, 2), round(self.current_temp, 2), pulse_duty])

                    sys.stdout.write(f"\r[PULSE] T+{elapsed:.1f}s | Temp: {self.current_temp:.2f}°C   ")
                    sys.stdout.flush()
                    time.sleep(0.5)  # 2Hz sampling for dead-time resolution

                # 4. Remove Pulse, monitor natural decay
                print(f"\n\n>>> PULSE COMPLETE. APPLYING 0% AND MONITORING NATURAL DECAY")
                self.send_command("ion_beam.source.cesium.testing_cmd_duty_cycle_value", 0.0)

                # Wait for peak (thermal lag) and decay back to 30C
                while self.current_temp >= 30.0 or (time.time() - start_time) < (pulse_duration + 120):
                    self.check_for_faults()
                    elapsed = time.time() - start_time
                    writer.writerow([round(elapsed, 2), round(self.current_temp, 2), 0.0])

                    sys.stdout.write(
                        f"\r[DECAY] T+{elapsed:.1f}s | Temp: {self.current_temp:.2f}°C (Waiting to cross <30.0°C)  ")
                    sys.stdout.flush()
                    time.sleep(1.0)  # 1Hz sampling is sufficient for the long decay tail

            print("\n\n[SUCCESS] Transient data logged to:", csv_filename)

        except KeyboardInterrupt:
            print("\n[ABORT] Sequence interrupted.")
        finally:
            self.graceful_exit()


if __name__ == "__main__":
    tester = CesiumTransientTester()
    # 15% pulse for 60 seconds
    tester.run_transient_test(pulse_duty=15.0, pulse_duration=60)