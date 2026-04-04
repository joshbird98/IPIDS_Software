import json

# Replace with the actual filename of the huge file
filename = "FaultLogs/fault_dump_2026-01-28_10-18-44.jsonl"
line1 = None
line2 = None
try:
    with open(filename, 'r', encoding='utf-8') as f:
        line1 = f.readline()  # Read just the first line
        line2 = f.readline()  # Read just the first line
        data = json.loads(line2)

        print("--- HEADER INFO ---")
        print(f"Timestamp: {data.get('timestamp')}")

        print("\n--- DATA SAMPLE ---")
        # Print the first 500 characters of the data to see if it's arrays or single numbers
        print(str(data.get('data'))[:1000])

        print("\n--- DIAGNOSIS ---")
        sample_value = list(data['data'].values())[0]
        if isinstance(sample_value, list):
            print(f"❌ BUG DETECTED: Values are LISTS (Length {len(sample_value)}) instead of FLOATS!")
        else:
            print("✅ Data structure looks correct (Scalars). Maybe just too many tags?")

except Exception as e:
    print(f"Error: {e}")