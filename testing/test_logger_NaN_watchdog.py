import polars as pl
import os
import glob
import math
from datetime import datetime

# 1. Grab ALL files, not just the newest one

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, "../"))

DEFAULT_LOG_DIRECTORY = os.path.join(PROJECT_ROOT, "data", "parquet_logs")

all_files = glob.glob(os.path.join(DEFAULT_LOG_DIRECTORY , "*.parquet"))

if not all_files:
    print("No Parquet files found!")
    exit()

print(f"Loading and stitching {len(all_files)} Parquet files...")

# Lazy scan, stitch, sort chronologically, and pull into RAM
df = pl.scan_parquet(all_files).sort("timestamp").collect()

test_tag = "ion_beam.source.vacuum_gauge_1.pressure"

if test_tag not in df.columns:
    print(f"Tag {test_tag} not found in database!")
    exit()

nan_mask = df[test_tag].is_nan()

# If the entire column is NaN, the service never sent data
if nan_mask.all():
    print(f"\nAll {len(df)} rows are NaN. {test_tag} was never published during this timeline.")
    exit()

if not nan_mask.any():
    print("No NaNs found. The Watchdog did not trip, or the tag was fully populated.")
    exit()

# 2. Extract indices
nan_indices = df.with_row_index().filter(pl.col(test_tag).is_nan())["index"].to_list()

# We want the first NaN that actually interrupted good data.
# If the logger booted up with NaNs (before the vacuum service connected), we skip those.
first_valid_idx = df.with_row_index().filter(~pl.col(test_tag).is_nan())["index"][0]

# Filter our NaN list to only include NaNs that happened AFTER the first valid data
real_nans = [i for i in nan_indices if i > first_valid_idx]

if not real_nans:
    print("Only boot-up NaNs found. No mid-run Watchdog trips occurred.")
    exit()

first_nan = real_nans[0]
last_nan = real_nans[-1]
total_nans = len(real_nans)

# 3. Block Integrity Check
is_solid = (last_nan - first_nan + 1) == total_nans
duration = (df["timestamp"][last_nan] - df["timestamp"][first_nan])

print("\n" + "=" * 55)
print("WATCHDOG BLOCK ANALYSIS")
print("=" * 55)
print(f"Total rows analyzed:   {len(df)}")
print(f"Total NaNs recorded:   {total_nans} rows")
print(f"Total Downtime logged: {duration:.1f} seconds")

if is_solid:
    print("Block Integrity:       SOLID (No fragmented data)")
else:
    print("Block Integrity:       FRAGMENTED (Service flickered or multiple trips)")


def print_transition(title, center_idx, df_ref):
    print(f"\n--- {title} ---")
    start_idx = max(0, center_idx - 5)
    end_idx = min(len(df_ref), center_idx + 5)

    for i in range(start_idx, end_idx):
        ts = df_ref["timestamp"][i]
        val = df_ref[test_tag][i]

        dt_str = datetime.fromtimestamp(ts).strftime('%H:%M:%S.%f')[:-3]

        if math.isnan(val):
            val_str = "NaN"
            if i == center_idx and "Loss" in title:
                val_str += "       <-- Watchdog Trips Here"
        else:
            val_str = f"{val:.2e}"
            if i == center_idx and "Recovery" in title:
                val_str += "  <-- Stream Recovers Here"

        marker = ">> " if i == center_idx else "   "
        print(f"{marker}{dt_str} | {val_str}")


# 4. Print boundaries
print_transition("Loss of Signal (Hold -> NaN)", first_nan, df)

if last_nan < len(df) - 1:
    print_transition("Signal Recovery (NaN -> Real Data)", last_nan + 1, df)
else:
    print("\n--- Signal Recovery ---")
    print("Log ended before the service recovered.")