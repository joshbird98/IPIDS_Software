import os
import time
import glob
import zmq
from src.core.network_map import ZMQ_PORT_HEARTBEAT, ZMQ_PORT_EVENTS_PUB
import re
import polars as pl
from datetime import datetime
from src.core.os_helper import harden_windows_process

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

                # --- NEW CODE (Diagonal Merge for Schema Evolution) ---
                print(f"[Compactor] Processing {d_str} ({len(fragments)} fragments)...")

                lfs = [pl.scan_parquet(f) for f in fragments]
                df = pl.concat(lfs, how="diagonal").sort("timestamp")

                # 1. Collect into RAM ONCE
                daily_df = df.collect()

                # 2. Write the high-resolution raw file
                daily_df.write_parquet(output_file, compression="zstd", compression_level=10)

                # 3. Generate the UI rollups using the data already sitting in RAM
                # Note: We pass the base filename "day_YYYYMMDD" so the macro
                # files get named day_YYYYMMDD_macro_1m.parquet, etc.
                self.generate_macro_rollups(daily_df, f"day_{d_str}", DAILY_DIR)

                # 4. Clean up ONLY after successful writes
                for f in fragments:
                    os.remove(f)
                print(f"[Compactor] Finished {d_str}.")

            except Exception as e:
                print(f"[Compactor] Error on date {d_str}: {e}")

    def generate_macro_rollups(self, df: pl.DataFrame, base_filename: str, output_dir: str):
        """
        Takes the massive 24hr raw DataFrame and generates two tiered min/max
        aggregations to guarantee instant UI rendering at any zoom level.
        """
        # 1. Identify valid sensor channels (ignoring strings/booleans)
        numeric_cols = [col for col in df.columns if
                        df[col].dtype in [pl.Float64, pl.Float32, pl.Int64, pl.Int32] and col != "timestamp"]

        # Define rollups (Key: Suffix, Value: Window in Seconds)
        tiers = {
            "macro_1m": 60.0,
            "macro_20m": 1200.0
        }

        for suffix, window_sec in tiers.items():
            print(f"Generating {suffix} rollup...")

            # Calculate time bins based on the window size
            lf = df.lazy().with_columns(
                (pl.col("timestamp") / window_sec).floor().alias("bin")
            )

            agg_exprs = [
                pl.col("timestamp").min().alias("timestamp_min"),
                pl.col("timestamp").max().alias("timestamp_max"),
            ]

            for col in numeric_cols:
                agg_exprs.append(pl.col(col).min().cast(pl.Float64, strict=False).alias(f"{col}_min"))
                agg_exprs.append(pl.col(col).max().cast(pl.Float64, strict=False).alias(f"{col}_max"))

            # Execute aggregation and drop the temporary 'bin' column
            macro_df = lf.group_by("bin").agg(agg_exprs).sort("bin").drop("bin").collect()

            # Save to disk
            out_path = os.path.join(output_dir, f"{base_filename}_{suffix}.parquet")
            macro_df.write_parquet(out_path)
            print(f"Saved {out_path} ({len(macro_df)} rows)")

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
    harden_windows_process()
    DataCompactor().run()