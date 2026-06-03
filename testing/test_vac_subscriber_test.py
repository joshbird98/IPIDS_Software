# src/tools/debug_vacuum_subscriber.py

import sys
import os
import zmq
import json

# Adjust path to find src directory if executing directly from subfolders
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.core.network_map import ZMQ_PORT_VACUUM_PUB, TOPIC_VACUUM_DATA


def run_vacuum_debugger():
    context = zmq.Context()
    socket = context.socket(zmq.SUB)

    socket.connect(ZMQ_PORT_VACUUM_PUB)

    topic = TOPIC_VACUUM_DATA if isinstance(TOPIC_VACUUM_DATA, bytes) else TOPIC_VACUUM_DATA.encode('utf-8')
    socket.setsockopt(zmq.SUBSCRIBE, topic)

    print(f"[DEBUG] Connected to Vacuum Stream at {ZMQ_PORT_VACUUM_PUB}")
    print(f"[DEBUG] Subscribed to Topic: {TOPIC_VACUUM_DATA}")
    print("=" * 90)

    try:
        while True:
            topic_recv, msg_bytes = socket.recv_multipart()
            payload = json.loads(msg_bytes)

            timestamp = payload.get("timestamp", 0.0)
            cycle_time = payload.get("system.cycle_time_ms", 0.0)
            connected = payload.get("system.connected", 0.0)
            gv_permissive = payload.get("ion_beam.vacuum.gv_permissive_ready", 0.0)

            os.system('cls' if os.name == 'nt' else 'clear')

            print(f"=== VACUUM BUS DIAGNOSTIC MONITOR ===")
            print(f"Unix Timestamp: {timestamp:.3f} | Loop Execution Headroom: {cycle_time:.2f} ms")
            print(
                f"Waveshare Link State: {'ONLINE' if connected == 1.0 else 'OFFLINE':<8} | GV Safety Permissive: {'READY' if gv_permissive == 1.0 else 'LOCKED'}")
            print("-" * 90)

            # 1. Gauge Status Table
            print(f"{'GAUGE':<7} | {'PRESSURE (mB)':<13} | {'HW STATUS':<10} | {'ALARM / LATCH STATES':<45}")
            print("-" * 90)
            for i in range(1, 7):
                vg_name = f"vg{i}"

                pressure_val = "N/A"
                for key, val in payload.items():
                    if key.endswith(".pressure") and (f"vacuum_gauge_{i}" in key or f"gauge_{i}" in key):
                        pressure_val = f"{val:.3e}" if val is not None else "None"
                        break

                not_found = payload.get(f"ion_beam.gauges.status.stat_{vg_name}_not_found", 0.0)
                mismatch = payload.get(f"ion_beam.gauges.status.stat_{vg_name}_mismatch", 0.0)
                rapid_rise = payload.get(f"ion_beam.gauges.status.stat_{vg_name}_rapid_rise", 0.0)
                above_sp = payload.get(f"ion_beam.gauges.status.stat_{vg_name}_above_sp", 0.0)
                approaching = payload.get(f"ion_beam.gauges.status.stat_{vg_name}_approaching_sp", 0.0)
                relay_active = payload.get(f"ion_beam.gauges.status.stat_{vg_name}_relay_active", 0.0)

                hw_status = "OK"
                if not_found == 1.0:
                    hw_status = "NOT_FOUND"
                elif mismatch == 1.0:
                    hw_status = "MISMATCH"

                alerts = []
                if rapid_rise == 1.0:
                    alerts.append("[CRIT_RAPID_RISE]")
                if above_sp == 1.0:
                    alerts.append("[ABOVE_SETPOINT]")
                if approaching == 1.0:
                    alerts.append("[WARN_80%_APPROACH (ORANGE)]")
                if relay_active == 1.0:
                    alerts.append("[RELAY_CLOSED (RED)]")

                alert_str = ", ".join(alerts) if alerts else "NOMINAL"

                print(f"{vg_name.upper():<7} | {pressure_val:<13} | {hw_status:<10} | {alert_str:<45}")

            # 2. Dynamic Relay Hardware Mappings
            print("-" * 90)
            print("=== RELAY TO VACUUM CHANNEL INTERLOCK MAPPINGS ===")
            for node in NODE_IDS if 'NODE_IDS' in locals() else [10, 20]:
                node_mappings = []
                for r in range(1, 7):
                    ch_map_val = payload.get(f"ion_beam.vacuum.controller_{node}.relay_{r}_ch_sp")
                    on_sp_val = payload.get(f"ion_beam.vacuum.controller_{node}.relay_{r}_on_sp")

                    if ch_map_val is not None:
                        ch_idx = int(ch_map_val)
                        sp_text = f"{on_sp_val:.1E}" if on_sp_val is not None else "Unknown"
                        node_mappings.append(f"R{r} -> Ch {ch_idx} (Threshold: {sp_text} mB)")

                if node_mappings:
                    print(f"  Controller Node {node}:")
                    for mapping in node_mappings:
                        print(f"    * {mapping}")
                else:
                    print(f"  Controller Node {node}: [Scanning/Awaiting slow task telemetry entries...]")

            # 3. Raw Bitfield Operational Diagnostics
            print("-" * 90)
            print("=== RAW CONTROLLER RELAY OPERATIONAL STATES (0=OPEN, 1=CLOSED) ===")
            for node in [10, 20]:
                relay_bits = []
                for r in range(1, 7):
                    r_stat = payload.get(f"ion_beam.vacuum.controller_{node}.relay_{r}_status")
                    if r_stat is not None:
                        relay_bits.append(f"R{r}:{int(r_stat)}")
                if relay_bits:
                    print(f"  Controller Node {node} -> {'  '.join(relay_bits)}")

    except KeyboardInterrupt:
        print("\n[DEBUG] Terminating trace subscriber session safely.")
    finally:
        socket.close()
        context.term()


if __name__ == "__main__":
    run_vacuum_debugger()