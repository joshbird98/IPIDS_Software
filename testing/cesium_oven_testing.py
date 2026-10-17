import time
import zmq
import json
import threading
import sys
import csv

from src.core.network_map import ZMQ_PORT_VACUUM_PUB, ZMQ_PORT_PLC_CMD, ZMQ_PORT_PLC_PUB
try:
    from testing.cmd_helper import send as send_ack
except ImportError:
    from cmd_helper import send as send_ack

# --- FAULT BIT MAP ---
BIT_SRC_BODY_TEMP_HIGH = 4
BIT_CESIUM_OVEN_OVERTEMP = 5
BIT_CESIUM_HEAT_FAIL = 6
BIT_CESIUM_COOL_FAIL = 7


class CesiumFOPDTTester:
    def __init__(self):
        self.running = True
        self.current_temp = 0.0
        self.fault_word_2 = 0
        self.ctx = zmq.Context.instance()
        # Commands are acknowledged now: they go through cmd_helper (see send_command)
        self.telemetry_socket = self.ctx.socket(zmq.SUB)
        self.telemetry_socket.connect(ZMQ_PORT_PLC_PUB)
        self.telemetry_socket.setsockopt(zmq.SUBSCRIBE, b"")

        self.t_thread = threading.Thread(target=self._telemetry_loop, daemon=True)
        self.t_thread.start()
        time.sleep(1)

    def send_command(self, tag: str, value):
        reply = send_ack(tag, value, subsystem="plc", verbose=False)
        if reply.get("status") not in ("ok", "accepted"):
            print(f"[!] {tag} = {value} NOT executed: {reply.get('status')} - {reply.get('reason')}")
        return reply

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

    def run_step_response(self, step_duty=8.0, max_temp=75.0, decay_minutes=20, csv_filename="cesium_fopdt_data.csv"):
        if self.current_temp > 30.0:
            print(f"[ERROR] Oven is too hot ({self.current_temp}°C). Must start from < 30.0°C.")
            self.graceful_exit()
            return

        try:
            with open(csv_filename, mode='w', newline='') as file:
                writer = csv.writer(file)
                writer.writerow(["Time (s)", "Temperature (C)", "Duty Cycle (%)"])

                print("Starting FOPDT Step Response Profiling...")
                self.send_command("ion_beam.source.cesium.testing_cmd_force_duty_cycle", True)
                self.send_command("ion_beam.source.cesium.cmd_force_cooling", False)
                start_time = time.time()

                # --- PHASE 1: CONTINUOUS STEP ---
                print(f"\n>>> APPLYING {step_duty}% CONTINUOUS STEP (Target limit: {max_temp}°C)")
                self.send_command("ion_beam.source.cesium.testing_cmd_duty_cycle_value", step_duty)

                while self.current_temp < max_temp and (time.time() - start_time) < 2700:  # 45 min timeout
                    self.check_for_faults()
                    elapsed = time.time() - start_time
                    writer.writerow([round(elapsed, 2), round(self.current_temp, 2), step_duty])

                    sys.stdout.write(f"\r[HEATING] T+{elapsed:.0f}s | Temp: {self.current_temp:.2f}°C   ")
                    sys.stdout.flush()
                    time.sleep(3.0)

                    # --- PHASE 2: NATURAL DECAY ---
                print(f"\n\n>>> MAXIMUM TEMPERATURE REACHED. APPLYING 0% AND LOGGING DECAY.")
                self.send_command("ion_beam.source.cesium.testing_cmd_duty_cycle_value", 0.0)
                decay_start_time = time.time()
                decay_duration_sec = decay_minutes * 60

                while (time.time() - decay_start_time) < decay_duration_sec:
                    self.check_for_faults()
                    elapsed = time.time() - start_time
                    decay_elapsed = time.time() - decay_start_time
                    writer.writerow([round(elapsed, 2), round(self.current_temp, 2), 0.0])

                    sys.stdout.write(
                        f"\r[DECAY] T+{elapsed:.0f}s (Decay Time: {decay_elapsed:.0f}/{decay_duration_sec}s) | Temp: {self.current_temp:.2f}°C   ")
                    sys.stdout.flush()
                    time.sleep(3.0)

            print("\n\n[SUCCESS] FOPDT data logged to:", csv_filename)

        except KeyboardInterrupt:
            print("\n[ABORT] Sequence interrupted.")
        finally:
            self.graceful_exit()


if __name__ == "__main__":
    tester = CesiumFOPDTTester()
    tester.run_step_response(step_duty=10.0, max_temp=87.0, decay_minutes=60)