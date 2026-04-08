import os
import numpy as np
from utils.data_engine import TimeSeriesEngine

# Setup paths
current_dir = os.path.dirname(os.path.abspath(__file__))
log_dir = os.path.join(os.path.dirname(current_dir), "LogData")
engine = TimeSeriesEngine(log_dir)

# 1. Test standard query
start = 0  # epoch start
end = 2000000000 # future date to catch all logs
ts, vals = engine.query(start, end, stride=1)

print("--- RAW DATA CHECK ---")
print(f"Timestamp Type:  {type(ts)}")      # Expected: <class 'numpy.ndarray'>
print(f"Timestamp Shape: {ts.shape}")     # Expected: (N,)
print(f"Values Type:     {type(vals)}")    # Expected: <class 'numpy.ndarray'>
print(f"Values Shape:    {vals.shape}")    # Expected: (N, M) where M is channel count

# 2. Test stride query
stride_val = 10
ts_s, vals_s = engine.query(start, end, stride=stride_val)

print(f"\n--- STRIDE CHECK (Stride={stride_val}) ---")
if len(ts) > 0:
    expected_len = int(np.ceil(len(ts) / stride_val))
    print(f"Original Length: {len(ts)}")
    print(f"Strided Length:  {len(ts_s)}")
    print(f"Length Match:    {len(ts_s) == expected_len or len(ts_s) == (len(ts)//stride_val)}")