import json
import os
import sys
import numpy as np
from datetime import datetime


def analyze(filename):
    print(f"--- Analyzing {filename} ---")
    file_size = os.path.getsize(filename)
    print(f"Size: {file_size / (1024 * 1024):.2f} MB")

    timestamps = []
    line_lengths = []
    total_lines = 0
    headers = []

    # Read first line for headers
    with open(filename, 'r', encoding='utf-8') as f:
        first = f.readline()
        try:
            h = json.loads(first)
            if "headers" in h: headers = h["headers"]
        except:
            pass

    print(f"Tags found: {len(headers)}")
    print(f"Header sample: {headers[:5]}")

    # Scan file (efficiently)
    with open(filename, 'r', encoding='utf-8') as f:
        # Skip header
        f.readline()

        last_ts = 0
        duplicates = 0

        for line in f:
            total_lines += 1
            line_lengths.append(len(line))

            try:
                if "[" not in line: continue
                # We do a 'lazy parse' - just find the first number
                # Full JSON parse is too slow for this diagnostic
                end_bracket = line.find(',')
                if end_bracket == -1: continue

                ts_str = line[1:end_bracket]
                ts = float(ts_str)
                timestamps.append(ts)

                if ts == last_ts:
                    duplicates += 1
                last_ts = ts

            except:
                continue

            if total_lines % 50000 == 0:
                print(f"Scanned {total_lines} lines...")

    # Statistics
    timestamps = np.array(timestamps)
    intervals = np.diff(timestamps)

    print("\n--- DIAGNOSTIC RESULTS ---")
    print(f"Total Data Points: {total_lines}")
    print(f"Avg Line Size:     {np.mean(line_lengths):.1f} bytes (Huge if > 500)")
    print(f"Duplicate Timestamps: {duplicates} (Should be 0)")

    if len(intervals) > 0:
        avg_dt = np.mean(intervals)
        freq = 1.0 / avg_dt if avg_dt > 0 else 0
        print(f"Avg Interval:      {avg_dt * 1000:.2f} ms")
        print(f"Effective Freq:    {freq:.2f} Hz")
        print(f"Min Interval:      {np.min(intervals) * 1000:.2f} ms")
        print(f"Max Interval:      {np.max(intervals):.2f} s")

    # Suggestion
    est_binary_size = total_lines * len(headers) * 8  # 8 bytes per double
    print(f"\n--- OPTIMIZATION PREDICTION ---")
    print(f"Current JSON Size: {file_size / (1024 * 1024):.2f} MB")
    print(f"Est. Binary Size:  {est_binary_size / (1024 * 1024):.2f} MB")
    print(f"Potential Reduction: {100 - (est_binary_size / file_size) * 100:.1f}%")


if __name__ == "__main__":
    # Change this to your actual log file name
    target_file = "C:/Users/lenovo/Documents/IPIDS_Software/SystemLogs/hmi_data_2026-01-11.jsonl"
    if len(sys.argv) > 1: target_file = sys.argv[1]

    if os.path.exists(target_file):
        analyze(target_file)
    else:
        print(f"File not found: {target_file}")