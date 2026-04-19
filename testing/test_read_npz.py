import numpy as np
import json
import sys
from datetime import datetime

def inspect_file(filepath):
    try:
        print(f"\n--- Inspecting: {filepath} ---")
        data = np.load(filepath, allow_pickle=True)

        print(f"Keys in file: {list(data.keys())}")

        # Check shapes
        if 'timestamps' in data and 'values' in data:
            ts = data['timestamps']
            vals = data['values']
            print(f"Timestamps shape: {ts.shape}")
            print(f"Values shape:     {vals.shape}")

            if len(ts) > 0:
                start_time = datetime.fromtimestamp(ts[0]).strftime('%Y-%m-%d %H:%M:%S')
                end_time = datetime.fromtimestamp(ts[-1]).strftime('%Y-%m-%d %H:%M:%S')

                print(f"\n--- Timestamp Analysis ---")
                print(f"Start Time:  {start_time} (UNIX: {ts[0]:.3f})")
                print(f"End Time:    {end_time} (UNIX: {ts[-1]:.3f})")
                print(f"Total Span:  {(ts[-1] - ts[0]) / 60:.2f} minutes")

                if len(ts) > 1:
                    diffs = np.diff(ts)
                    print(f"Avg Interval: {np.mean(diffs):.3f} seconds")
                    print(f"Max Gap:      {np.max(diffs):.3f} seconds")

                    print("\nLast 5 recorded times:")
                    for t in ts[-5:]:
                        print(f"  {datetime.fromtimestamp(t).strftime('%H:%M:%S.%f')}")
            else:
                print("\nArray is empty! No timestamps recorded.")

        # Read metadata if it exists (for burst files)
        if 'metadata' in data:
            meta_str = str(data['metadata'])
            try:
                meta_dict = json.loads(meta_str)
                print("\nMetadata (Reasons):")
                print(json.dumps(meta_dict, indent=2))
            except:
                print("\nMetadata (Raw String):")
                print(meta_str)

    except Exception as e:
        print(f"Failed to read file: {e}")

if __name__ == "__main__":
    import os
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(current_dir)
    file_path = os.path.join(project_root, "telemetry", "archived_daily_trend_20260408.npz")
    inspect_file(file_path)