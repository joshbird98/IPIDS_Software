import os
import time
import glob
import zmq
from src.core.network_config import ZMQ_PORT_HEARTBEAT, ZMQ_PORT_EVENTS_PUB
import re
import polars as pl
from datetime import datetime

# --- CONFIGURATION ---
DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../data/parquet_logs'))
DAILY_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../data/daily_logs'))

COMPACTION_TIME = "02:00"  # 24-hour format


class DataCompactor:
    def __init__(self):
        os.makedirs(DAILY_DIR, exist_ok=True)

        # ZMQ Heartbeat setup
        self.context = zmq.Context()
        self.hb_socket = self.context.socket(zmq.PUB)
        self.hb_socket.connect(ZMQ_PORT_HEARTBEAT)

    def compact_all_past_dates(self):
        # 1. List all chunk files
        all_chunks = [f for f in os.listdir(DATA_DIR) if f.startswith("chunk_") and f.endswith(".parquet")]

        # 2. Extract unique dates using a Regex (finds YYYYMMDD)
        # Filename example: chunk_20260518_150811.parquet
        date_pattern = re.compile(r"chunk_(\d{8})_")

        found_dates = set()
        today_str = datetime.now().strftime("%Y%m%d")

        for f in all_chunks:
            match = date_pattern.search(f)
            if match:
                date_str = match.group(1)
                # Only process dates that are NOT today (today is still being written to)
                if date_str != today_str:
                    found_dates.add(date_str)

        # 3. Iterate and merge
        for d_str in sorted(list(found_dates)):
            fragments = [os.path.join(DATA_DIR, f) for f in all_chunks if d_str in f]
            output_file = os.path.join(DAILY_DIR, f"day_{d_str}.parquet")

            # Check if we already have a daily file (maybe a previous partial run)
            # In this case, we merge the old daily + the new fragments
            try:
                print(f"[Compactor] Processing {d_str} ({len(fragments)} fragments)...")

                # Use scan_parquet for all fragments
                df = pl.scan_parquet(fragments)
                df = df.sort("timestamp")

                # High compression level for long-term storage
                df.collect().write_parquet(output_file, compression="zstd", compression_level=10)

                # 4. Clean up ONLY after successful write
                for f in fragments:
                    os.remove(f)
                print(f"[Compactor] Finished {d_str}.")

            except Exception as e:
                print(f"[Compactor] Error on date {d_str}: {e}")

    def run(self):
        print(f"[Compactor] Service Online. Scheduled for {COMPACTION_TIME} daily.")

        while True:
            now = datetime.now()
            current_time_str = now.strftime("%H:%M")

            # Heartbeat (so service_manager knows we are alive)
            self.hb_socket.send_json({"service": "service_data_compactor", "ts": time.time()})

            if current_time_str == COMPACTION_TIME:
                self.compact_all_past_dates()
                # Sleep for 70 seconds to ensure we don't trigger twice in the same minute
                time.sleep(70)

            time.sleep(10)


if __name__ == "__main__":
    DataCompactor().run()