import numpy as np
import os

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
logfile_path = os.path.join(project_root, "telemetry", "archived_daily_trend_20260407.npz")

try:
    data = np.load(logfile_path)
    keys = data['keys']

    # Grab the very last recorded time step (the most recent row)
    latest_values = data['values'][-1]

    print("--- LATEST LOGGED VALUES ---")
    for i, key in enumerate(keys):
        # Only print vacuum keys so we don't spam the console
        if "vacuum" in key:
            print(f"{key}: {latest_values[i]}")

except Exception as e:
    print(f"Error reading file: {e}")