import logging
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import polars as pl
import zmq

from src.core import events_db, paths
from src.core.log_setup import setup_logging
from src.core.network_map import ZMQ_PORT_HEARTBEAT
from src.core.os_helper import harden_windows_process

# --- CONFIGURATION ---
DATA_DIR = paths.PARQUET_DIR
DAILY_DIR = paths.DAILY_DIR
DB_PATH = paths.EVENTS_DB
EXPORTS_DIR = paths.EXPORTS_DIR

log = logging.getLogger("service_data_compactor")

COMPACTION_TIME = "02:00"  # 24-hour format

# A real folder on the lab PC (still under the machine's old name): rename it there before changing it here
ONEDRIVE_PATH = r"C:\Users\100kV\Josh\OneDrive\OneDrive - University of Surrey\IPIDS_Project\IPIDS_Data_Backup"


class OneDriveArchiver:
    def __init__(self, local_dir: str, onedrive_dir: str):
        self.local_dir = Path(local_dir)
        self.onedrive_dir = Path(onedrive_dir)
        self.archive_dir = self.onedrive_dir / "_Archive_Versions"

        # Ensure target directories exist
        self.onedrive_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    def sync_files(self, file_pattern: str = "*.parquet", recursive: bool = False) -> bool:
        """
        Executes a one-way incremental sync for files matching the pattern.
        Local deletions are ignored. Overwrites are archived.
        Preserves relative directory structure if recursive is True.
        """
        if not self.local_dir.exists():
            log.error(f"Local directory not found: {self.local_dir}")
            return False

        log.info(f"Scanning {self.local_dir} for missing or updated backups...")
        success = True
        sync_count = 0

        file_iterator = self.local_dir.rglob(file_pattern) if recursive else self.local_dir.glob(file_pattern)

        for local_file in file_iterator:
            if not local_file.is_file():
                continue

            # Calculate relative path to maintain folder structure in cloud
            relative_path = local_file.relative_to(self.local_dir)
            cloud_file = self.onedrive_dir / relative_path

            # Ensure the specific target subdirectory exists
            cloud_file.parent.mkdir(parents=True, exist_ok=True)

            try:
                # Condition A: File does not exist in OneDrive -> Copy
                if not cloud_file.exists():
                    log.info(f"Backing up new file: {relative_path}")
                    shutil.copy2(local_file, cloud_file)
                    sync_count += 1

                # Condition C: File exists, but local is newer -> Archive & Replace
                elif local_file.stat().st_mtime > cloud_file.stat().st_mtime:
                    log.info(f"Modification detected for {relative_path}. Archiving old version.")

                    timestamp = time.strftime("%Y%m%d_%H%M%S")
                    archive_name = f"{cloud_file.stem}_{timestamp}{cloud_file.suffix}"
                    archive_path = self.archive_dir / archive_name

                    shutil.move(str(cloud_file), str(archive_path))
                    shutil.copy2(local_file, cloud_file)
                    sync_count += 1

            except OSError as e:     # (shutil.Error is an OSError) - the next night tries again
                log.error(f"Failed to sync {relative_path}: {e}")
                success = False

        log.info(f"Backup scan complete. Synced {sync_count} files.")
        return success

    def sync_database(self, local_db_path: str) -> bool:
        """
        Safely snapshots a live SQLite database and syncs it to OneDrive. A snapshot that fails
        SQLite's check (a damaged events.db) doesn't replace the backup: the last sound one stays.
        """
        db_file = Path(local_db_path)
        if not db_file.exists():
            log.error(f"Database not found: {db_file}")
            return False

        cloud_file = self.onedrive_dir / db_file.name
        temp_cloud_db = cloud_file.with_suffix('.db.tmp')

        try:
            log.info(f"Snapshotting live database: {db_file.name}...")

            # 1. Open connections
            src = sqlite3.connect(db_file)
            dst = sqlite3.connect(temp_cloud_db)

            try:
                # 2. Perform the backup safely, and check the copy (the pages as they will be kept)
                src.backup(dst)
                problem = events_db.check(dst)
            finally:
                # 3. CRITICAL: Explicitly close connections to release Windows file locks
                dst.close()
                src.close()

            if problem:
                log.critical(f"{events_db.REPAIR_HINT}. Tonight's copy failed SQLite's check ({problem[:300]}), so "
                             f"it has not replaced the backup: {cloud_file} is still the last sound one (the "
                             f"copy is {temp_cloud_db}).")
                return False

            # 4. Archive the backup it replaces, if the database has changed since
            if cloud_file.exists() and db_file.stat().st_mtime > cloud_file.stat().st_mtime:
                log.info(f"Modification detected for {db_file.name}. Archiving old version.")
                timestamp = time.strftime("%Y%m%d_%H%M%S")
                archive_name = f"{cloud_file.stem}_{timestamp}{cloud_file.suffix}"
                archive_path = self.archive_dir / archive_name
                shutil.move(str(cloud_file), str(archive_path))

            # Now that Python has released the lock, we can safely swap the files
            os.replace(temp_cloud_db, cloud_file)
            log.info("Database snapshot backed up.")
            return True

        except (OSError, sqlite3.Error) as e:
            log.error(f"Failed to sync database {db_file.name}: {e}")
            return False


class DataCompactor:
    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(DAILY_DIR, exist_ok=True)
        os.makedirs(EXPORTS_DIR, exist_ok=True)

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
                        log.info(f"Deleted orphaned temporary file: {f}")
                    except OSError as e:
                        log.warning(f"Could not delete orphaned temporary file {f}: {e}")

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
                log.info(f"Processing {d_str} ({len(fragments)} fragments)...")

                lfs = [pl.scan_parquet(f) for f in fragments]
                if os.path.exists(output_file):
                    # Late fragments for a day that was already compacted (e.g. rows recovered
                    # from the logger's crash journal after a restart): MERGE with the existing
                    # day file. Writing only the new fragments would replace the whole day.
                    log.warning(f"day_{d_str}.parquet already exists - merging {len(fragments)} late fragment(s) into it")
                    lfs.insert(0, pl.scan_parquet(output_file))
                df = (pl.concat(lfs, how="diagonal_relaxed")
                      .unique(subset=["timestamp"], keep="first", maintain_order=True)
                      .sort("timestamp"))

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
                    except OSError as e:
                        log.warning(f"Could not delete compacted fragment {f}: {e} (it will be merged again, harmlessly)")
                log.info(f"Finished {d_str}.")

            except Exception as e:   # one bad day must not stop the others (its fragments stay for next night)
                log.exception(f"Error on date {d_str}: {e}")

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
            log.info(f"Generating {suffix} rollup...")

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

            log.info(f"Saved {out_path} ({len(macro_df)} rows)")

    def _nightly_job(self):
        try:
            self.compact_all_past_dates()
        except Exception:   # the nightly job must not take the service down; the backup still runs
            log.exception("Compaction failed")

        if paths.simulation_enabled():
            log.info("Simulation mode: skipping the OneDrive backup.")
            return

        # --- Backup Sequence ---
        try:
            log.info("Initiating OneDrive backup sequence...")

            # 1. Daily Logs & DB
            archiver = OneDriveArchiver(local_dir=DAILY_DIR, onedrive_dir=ONEDRIVE_PATH)
            archiver.sync_files(file_pattern="*.parquet", recursive=False)
            archiver.sync_database(DB_PATH)

            # 2. Exports Directory (Recursive Sync)
            exports_onedrive_path = os.path.join(ONEDRIVE_PATH, "exports")
            exports_archiver = OneDriveArchiver(local_dir=EXPORTS_DIR, onedrive_dir=exports_onedrive_path)
            exports_archiver.sync_files(file_pattern="*", recursive=True)

        except Exception as e:   # as above: logged, tried again tomorrow
            log.exception(f"Backup sequence failed: {e}")

    def run(self):
        import threading
        log.info(f"Service online. Compaction scheduled for {COMPACTION_TIME} daily ({DATA_DIR} -> {DAILY_DIR}).")
        job = None
        last_run_day = None

        while True:
            now = datetime.now()

            # Heartbeat (so service_manager knows we are alive). The nightly job runs in a
            # thread so the heartbeat keeps going however long a big day takes to compact.
            self.hb_socket.send_json({"service": "service_data_compactor", "ts": time.time()})

            if now.strftime("%H:%M") == COMPACTION_TIME and last_run_day != now.date() \
                    and (job is None or not job.is_alive()):
                last_run_day = now.date()
                job = threading.Thread(target=self._nightly_job, name="nightly-compaction", daemon=True)
                job.start()

            time.sleep(5)


if __name__ == "__main__":
    setup_logging("service_data_compactor")
    harden_windows_process()
    compactor = DataCompactor()
    compactor.run()