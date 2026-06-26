import os
import time
import glob
import zmq
import re
import shutil
import sqlite3
import polars as pl
from pathlib import Path
from datetime import datetime
from src.core.network_map import ZMQ_PORT_HEARTBEAT, ZMQ_PORT_EVENTS_PUB
from src.core.os_helper import harden_windows_process

# --- CONFIGURATION ---
DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../data/parquet_logs'))
DAILY_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../data/daily_logs'))
DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../data/events.db'))

COMPACTION_TIME = "02:00"  # 24-hour format

ONEDRIVE_PATH = r"C:\Users\100kV\Josh\OneDrive\OneDrive - University of Surrey\IPIDS_Project\IPIDS_Data_Backup"


class OneDriveArchiver:
    def __init__(self, local_dir: str, onedrive_dir: str):
        self.local_dir = Path(local_dir)
        self.onedrive_dir = Path(onedrive_dir)
        self.archive_dir = self.onedrive_dir / "_Archive_Versions"

        # Ensure target directories exist
        self.onedrive_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    def sync_daily_files(self) -> bool:
        """
        Executes a one-way incremental sync for Parquet files.
        Local deletions are ignored. Overwrites are archived.
        """
        if not self.local_dir.exists():
            print(f"[Archiver] Error: Local directory not found: {self.local_dir}")
            return False

        print(f"[Archiver] Scanning {self.local_dir} for missing or updated backups...")
        success = True
        sync_count = 0

        for local_file in self.local_dir.glob("*.parquet"):
            cloud_file = self.onedrive_dir / local_file.name

            try:
                # Condition A: File does not exist in OneDrive -> Copy
                if not cloud_file.exists():
                    print(f"[Archiver] Backing up new file: {local_file.name}")
                    shutil.copy2(local_file, cloud_file)
                    sync_count += 1

                # Condition C: File exists, but local is newer -> Archive & Replace
                elif local_file.stat().st_mtime > cloud_file.stat().st_mtime:
                    print(f"[Archiver] Modification detected for {local_file.name}. Archiving old version.")

                    timestamp = time.strftime("%Y%m%d_%H%M%S")
                    archive_name = f"{cloud_file.stem}_{timestamp}{cloud_file.suffix}"
                    archive_path = self.archive_dir / archive_name

                    shutil.move(str(cloud_file), str(archive_path))
                    shutil.copy2(local_file, cloud_file)
                    sync_count += 1

            except Exception as e:
                print(f"[Archiver] Failed to sync {local_file.name}: {e}")
                success = False

        print(f"[Archiver] Parquet backup scan complete. Synced {sync_count} files.")
        return success

    def sync_database(self, local_db_path: str) -> bool:
        """
        Safely snapshots a live SQLite database and syncs it to OneDrive.
        """
        db_file = Path(local_db_path)
        if not db_file.exists():
            print(f"[Archiver] Database not found: {db_file}")
            return False

        cloud_file = self.onedrive_dir / db_file.name

        try:
            # Check if an archive is necessary
            if cloud_file.exists() and db_file.stat().st_mtime > cloud_file.stat().st_mtime:
                print(f"[Archiver] Modification detected for {db_file.name}. Archiving old version.")
                timestamp = time.strftime("%Y%m%d_%H%M%S")
                archive_name = f"{cloud_file.stem}_{timestamp}{cloud_file.suffix}"
                archive_path = self.archive_dir / archive_name
                shutil.move(str(cloud_file), str(archive_path))

            print(f"[Archiver] Safely snapshotting live database: {db_file.name}...")
            temp_cloud_db = cloud_file.with_suffix('.db.tmp')

            # 1. Open connections
            src = sqlite3.connect(db_file)
            dst = sqlite3.connect(temp_cloud_db)

            try:
                # 2. Perform the backup safely
                src.backup(dst)
            finally:
                # 3. CRITICAL: Explicitly close connections to release Windows file locks
                dst.close()
                src.close()

            # Now that Python has released the lock, we can safely swap the files
            os.replace(temp_cloud_db, cloud_file)
            print(f"[Archiver] Database snapshot successfully backed up.")
            return True

        except Exception as e:
            print(f"[Archiver] Failed to sync database {db_file.name}: {e}")
            return False

class DataCompactor:
    def __init__(self):
        os.makedirs(DAILY_DIR, exist_ok=True)

        # ZMQ Heartbeat setup
        self.context = zmq.Context()
        self.hb_socket = self.context.socket(zmq.PUB)
        self.hb_socket.connect(ZMQ_PORT_HEARTBEAT)

    def compact_all_past_dates(self):
        today_str = datetime.now().strftime("%Y%m%d")

        # 1. Clean up orphaned, corrupted temp files from previous days
        for f in os.listdir(DATA_DIR):
            if f.startswith("chunk_") and "_temp.parquet" in f:
                # If the temp file is NOT from today, it is dead. Delete it.
                if today_str not in f:
                    dead_file = os.path.join(DATA_DIR, f)
                    try:
                        os.remove(dead_file)
                        print(f"[Compactor] Deleted orphaned temporary file: {f}")
                    except OSError:
                        pass

        # 2. List all valid chunk files (Explicitly excluding any active temp files)
        all_chunks = [f for f in os.listdir(DATA_DIR)
                      if f.startswith("chunk_")
                      and f.endswith(".parquet")
                      and "_temp" not in f]

        # 3. Extract unique dates using a Regex (finds YYYYMMDD)
        date_pattern = re.compile(r"chunk_(\d{8})_")
        found_dates = set()

        for f in all_chunks:
            match = date_pattern.search(f)
            if match:
                date_str = match.group(1)
                # Only process dates that are NOT today
                if date_str != today_str:
                    found_dates.add(date_str)

        # 4. Iterate and merge
        for d_str in sorted(list(found_dates)):
            fragments = [os.path.join(DATA_DIR, f) for f in all_chunks if d_str in f]
            output_file = os.path.join(DAILY_DIR, f"day_{d_str}.parquet")

            try:
                print(f"[Compactor] Processing {d_str} ({len(fragments)} fragments)...")

                lfs = [pl.scan_parquet(f) for f in fragments]
                df = pl.concat(lfs, how="diagonal").sort("timestamp")

                # 1. Collect into RAM ONCE
                daily_df = df.collect()

                # 2. ATOMIC WRITE: Write to a temporary file first
                temp_output = output_file + ".tmp"
                daily_df.write_parquet(temp_output, compression="zstd", compression_level=10)

                # os.replace is an atomic filesystem operation. It swaps the files instantly.
                os.replace(temp_output, output_file)

                # 3. Generate the UI rollups
                self.generate_macro_rollups(daily_df, f"day_{d_str}", DAILY_DIR)

                # 4. Clean up ONLY after the atomic swap is complete
                for f in fragments:
                    try:
                        os.remove(f)
                    except OSError:
                        pass
                print(f"[Compactor] Finished {d_str}.")

            except Exception as e:
                print(f"[Compactor] Error on date {d_str}: {e}")

    def generate_macro_rollups(self, df: pl.DataFrame, base_filename: str, output_dir: str):
        """
        Takes the massive 24hr raw DataFrame and generates two tiered min/max
        aggregations to guarantee instant UI rendering at any zoom level.
        """
        numeric_cols = [col for col in df.columns if
                        df[col].dtype in [pl.Float64, pl.Float32, pl.Int64, pl.Int32] and col != "timestamp"]

        tiers = {
            "macro_1m": 60.0,
            "macro_20m": 1200.0
        }

        for suffix, window_sec in tiers.items():
            print(f"Generating {suffix} rollup...")

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

            macro_df = lf.group_by("bin").agg(agg_exprs).sort("bin").drop("bin").collect()

            out_path = os.path.join(output_dir, f"{base_filename}_{suffix}.parquet")
            temp_path = out_path + ".tmp"

            macro_df.write_parquet(temp_path)
            os.replace(temp_path, out_path)

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

                # --- Backup Sequence ---
                try:
                    print("[Compactor] Initiating OneDrive Backup Sequence...")
                    archiver = OneDriveArchiver(local_dir=DAILY_DIR, onedrive_dir=ONEDRIVE_PATH)
                    archiver.sync_daily_files()
                    archiver.sync_database(DB_PATH)  # Snapshot the event log
                except Exception as e:
                    print(f"[Compactor] FATAL ERROR during backup sequence: {e}")

                # Sleep for 70 seconds to ensure we don't trigger twice in the same minute
                time.sleep(70)

            time.sleep(10)

    def test_run(self):
        """Executes a single, immediate pass of the compaction and backup sequence for debugging."""
        print("[Test Mode] Forcing compaction and backup sequence NOW...")

        # 1. Run Compaction (Will safely ignore today's data)
        self.compact_all_past_dates()

        # 2. Run Backup
        try:
            print("[Test Mode] Initiating OneDrive Backup Sequence...")
            archiver = OneDriveArchiver(local_dir=DAILY_DIR, onedrive_dir=ONEDRIVE_PATH)
            archiver.sync_daily_files()
            archiver.sync_database(DB_PATH)
        except Exception as e:
            print(f"[Test Mode] FATAL ERROR during backup sequence: {e}")

        print("[Test Mode] Sequence Complete. Exiting.")


if __name__ == "__main__":
    import sys

    harden_windows_process()

    compactor = DataCompactor()

    # If launched with a test flag, run once and exit. Otherwise, run forever.
    if "--test" in sys.argv:
        compactor.test_run()
    else:
        compactor.run()