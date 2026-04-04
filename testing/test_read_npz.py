import numpy as np
import json
import sys


def inspect_file(filepath):
    try:
        print(f"\n--- Inspecting: {filepath} ---")
        data = np.load(filepath, allow_pickle=True)

        print(f"Keys in file: {list(data.keys())}")

        # Check shapes
        if 'timestamps' in data and 'values' in data:
            print(f"Timestamps shape: {data['timestamps'].shape}")
            print(f"Values shape:     {data['values'].shape}")

        # Read metadata if it exists (for burst files)
        if 'metadata' in data:
            # Metadata is stored as a 0D numpy array of a string
            meta_str = str(data['metadata'])
            meta_dict = json.loads(meta_str)
            print("\nMetadata (Reasons):")
            print(json.dumps(meta_dict, indent=2))

    except Exception as e:
        print(f"Failed to read file: {e}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        inspect_file(sys.argv[1])
    else:
        print("Usage: python test_read_npz.py <path_to_file.npz>")