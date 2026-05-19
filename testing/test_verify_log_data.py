import polars as pl
import os
import sys

# --- UPDATE THIS FILENAME to match what your logger just generated ---
PARQUET_FILE = "chunk_20260518_172119.parquet"


def verify_parquet():
    # 1. Find the file in your data directory

    CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
    PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, "../"))

    DEFAULT_LOG_DIRECTORY = os.path.join(PROJECT_ROOT, "data", "parquet_logs")
    file_path = os.path.join(DEFAULT_LOG_DIRECTORY, PARQUET_FILE)


    if not os.path.exists(file_path):
        print(f"File not found: {file_path}")
        sys.exit(1)

    # 2. Load the file using the Polars Engine
    print(f"Loading {PARQUET_FILE}...")
    df = pl.read_parquet(file_path)

    # 3. Print the overall health
    print("\n" + "=" * 50)
    print("PARQUET FILE HEALTH METRICS")
    print("=" * 50)
    print(f"Total Rows Captured: {df.height} (At 20Hz, {df.height / 20.0:.1f} seconds of data)")
    print(f"Total Columns Mapped: {df.width}")

    # 4. Check for Nulls (NaNs) in critical columns
    print("\n" + "=" * 50)
    print("DATA INTEGRITY CHECK (Last 5 Rows)")
    print("=" * 50)

    # Select a few representative columns from different microservices
    test_columns = [
        "timestamp",
        "ion_beam.system.cycle_time_ms",  # From PLC
        "ion_beam.source.vacuum_gauge_1.pressure",  # From Vacuum
        "ion_beam.source.turbo_pump.speed_hz",  # From Turbo
        "ion_beam.source.extraction.rb_current"  # From Extraction
    ]

    # Filter only the columns that actually exist in the dataframe to prevent crashes
    available_cols = [col for col in test_columns if col in df.columns]

    if available_cols:
        print(df.select(available_cols).tail(5))
    else:
        print("None of the test columns were found in the Parquet file!")


if __name__ == "__main__":
    verify_parquet()