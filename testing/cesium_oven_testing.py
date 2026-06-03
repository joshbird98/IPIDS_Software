import time
import zmq
import json
import threading
import sys
from collections import deque

# Import your actual network map here. Assuming standard structure:
from src.core.network_map import ZMQ_PORT_VACUUM_PUB, ZMQ_PORT_PLC_CMD, ZMQ_PORT_PLC_PUB, ZMQ_PORT_PLC_CMD

# --- FAULT BIT MAP ---
# Update these integer bit positions based on your UDT_Fault_Word_2_Source
BIT_SRC_BODY_TEMP_HIGH = 4  # Example bit position
BIT_CESIUM_OVEN_OVERTEMP = 5  # Example bit position
BIT_CESIUM_HEAT_FAIL = 6  # Example bit position
BIT_CESIUM_COOL_FAIL = 7  # Example bit position


class CesiumAutomatedTester:
    def __init__(self):
        self.running = True
        self.current_temp = 0.0
        self.fault_word_2 = 0

        # 20 minutes = 1200 seconds. If we sample at 1Hz, we need 1200 data points.
        self.SETTLING_TIME_SEC = 1200
        self.temp_history = deque(maxlen=self.SETTLING_TIME_SEC)

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
        time.sleep(1)  # Allow connection to establish

    def send_command(self, tag: str, value):
        payload = {"tag": tag, "value": value, "ts": time.time()}
        self.cmd_socket.send_json(payload)

    def _telemetry_loop(self):
        while self.running:
            try:
                # Use poll to allow graceful thread exit
                if self.telemetry_socket.poll(100):
                    topic, payload = self.telemetry_socket.recv_multipart(flags=zmq.NOBLOCK)
                    data = json.loads(payload)

                    if "ion_beam.source.cesium.rb_temp" in data:
                        self.current_temp = float(data["ion_beam.source.cesium.rb_temp"])

                    if "ion_beam.faults.word_2_source" in data:
                        self.fault_word_2 = int(data["ion_beam.faults.word_2_source"])
            except Exception as e:
                pass

    def check_for_faults(self):
        faults = []
        if bool(self.fault_word_2 & (1 << BIT_SRC_BODY_TEMP_HIGH)): faults.append("Src_Body_Temp_High")
        if bool(self.fault_word_2 & (1 << BIT_CESIUM_OVEN_OVERTEMP)): faults.append("Cesium_Oven_OverTemp")
        if bool(self.fault_word_2 & (1 << BIT_CESIUM_HEAT_FAIL)): faults.append("Cesium_Oven_HeatFail")
        if bool(self.fault_word_2 & (1 << BIT_CESIUM_COOL_FAIL)): faults.append("Cesium_Oven_CoolFail")

        if faults:
            print(f"\n[CRITICAL] Faults detected: {', '.join(faults)}")
            print("[CRITICAL] Aborting test sequence immediately.")
            self.graceful_exit()
            sys.exit(1)

    def wait_for_settling(self):
        print(f"Waiting for 20-minute settling period... (Max allowed Δ: < 0.5°C)")
        self.temp_history.clear()

        while self.running:
            self.check_for_faults()

            # Record 1 data point per second
            self.temp_history.append(self.current_temp)
            time.sleep(1)

            if len(self.temp_history) == self.SETTLING_TIME_SEC:
                temp_min = min(self.temp_history)
                temp_max = max(self.temp_history)
                delta = temp_max - temp_min

                # Dynamic print to show progress
                sys.stdout.write(f"\r[Settling Window] Current: {self.current_temp:.1f}°C | 20m Δ: {delta:.2f}°C    ")
                sys.stdout.flush()

                if delta < 0.5:
                    print("\n[SUCCESS] Thermal equilibrium achieved.")
                    return

    def cool_down_phase(self):
        print("\n--- INITIATING FORCED COOLDOWN ---")
        self.send_command("ion_beam.source.cesium.testing_cmd_duty_cycle_value", 0.0)
        self.send_command("ion_beam.source.cesium.cmd_force_cooling", True)

        while self.running:
            self.check_for_faults()

            sys.stdout.write(f"\rCooling... Current Temp: {self.current_temp:.1f}°C (Target < 30.0°C)   ")
            sys.stdout.flush()

            if self.current_temp < 30.0:
                print("\n[SUCCESS] Cooldown complete.")
                self.send_command("ion_beam.source.cesium.cmd_force_cooling", False)
                return
            time.sleep(1)

    def graceful_exit(self):
        print("\n[CLEANUP] Releasing open-loop overrides and zeroing duty cycle.")
        self.send_command("ion_beam.source.cesium.testing_cmd_duty_cycle_value", 0.0)
        self.send_command("ion_beam.source.cesium.cmd_force_cooling", False)
        # Yield control back to normal PLC PI loops
        self.send_command("ion_beam.source.cesium.testing_cmd_force_duty_cycle", False)
        self.running = False

    def run_sequence(self):
        steps = [1.5, 3.0, 5.0, 7.5, 10.0, 12.5, 15.0, 17.5, 20.0, 22.5, 25.0, 27.5, 30.0]

        try:
            print("Starting Automated Cesium Thermal Profiling...")
            self.send_command("ion_beam.source.cesium.testing_cmd_force_duty_cycle", True)

            for step in steps:
                print(f"\n========================================")
                print(f"STEP: {step}% DUTY CYCLE")
                print(f"========================================")

                self.send_command("ion_beam.source.cesium.testing_cmd_duty_cycle_value", step)
                self.wait_for_settling()

                # Log the final settled temperature
                print(f">>> DATA POINT: {step}% Duty Cycle yields {self.current_temp:.1f}°C")

                # Only run the cooldown if we aren't at the very last step
                if step != steps[-1]:
                    self.cool_down_phase()

            print("\n========================================")
            print("AUTOMATED PROFILING COMPLETE.")
            print("========================================")

        except KeyboardInterrupt:
            print("\n[ABORT] User interrupted sequence.")
        finally:
            self.graceful_exit()


if __name__ == "__main__":
    tester = CesiumAutomatedTester()
    tester.run_sequence()