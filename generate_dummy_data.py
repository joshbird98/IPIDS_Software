import os
import time
import json
import numpy as np
import polars as pl


def create_dummy_data():
    project_root = os.path.abspath(os.path.dirname(__file__))
    data_dir = os.path.join(project_root, "data", "parquet_logs")
    config_dir = os.path.join(project_root, "config")

    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(config_dir, exist_ok=True)

    # 1. Generate system_tags.json
    tags = {
        "TEST_VOLTAGE1": {"unit": "kV", "description": "Dummy High Voltage"},
        "TEST_CURRENT1": {"unit": "uA", "description": "Dummy Beam Current"},

        "TEST_VOLTAGE2": {"unit": "kV", "description": "Dummy High Voltage"},
        "TEST_CURRENT2": {"unit": "uA", "description": "Dummy Beam Current"},

        "TEST_VOLTAGE3": {"unit": "kV", "description": "Dummy High Voltage"},
        "TEST_CURRENT3": {"unit": "uA", "description": "Dummy Beam Current"},

        "TEST_VOLTAGE4": {"unit": "kV", "description": "Dummy High Voltage"},
        "TEST_CURRENT4": {"unit": "uA", "description": "Dummy Beam Current"},

        "TEST_VOLTAGE5": {"unit": "kV", "description": "Dummy High Voltage"},
        "TEST_CURRENT5": {"unit": "uA", "description": "Dummy Beam Current"}
    }
    with open(os.path.join(config_dir, "system_tags.json"), "w") as f:
        json.dump(tags, f, indent=4)

    # 2. Generate Parquet File (20 hours of data at 20Hz)
    now = time.time()
    start_ts = now - (3600 * 20)

    n_points = 144000  # 20 hours * 60 min * 60 sec * 20 Hz

    timestamps = np.linspace(start_ts, now, n_points)

    # Generate some distinct synthetic waves
    voltage_data1 = 30.0 + 5.0 * np.sin(timestamps / 300.0) + np.random.normal(0, 0.2, n_points)
    current_data1 = 150.0 + 50.0 * np.cos(timestamps / 150.0) + np.random.normal(0, 1.0, n_points)

    voltage_data2 = 20.0 + 12.0 * np.sin(timestamps / 200.0) + np.random.normal(0, 0.2, n_points)
    current_data2 = 250.0 + 51.0 * np.cos(timestamps / 120.0) + np.random.normal(0, 1.0, n_points)

    voltage_data3 = 40.0 + 55.0 * np.sin(timestamps / 304.0) + np.random.normal(0, 0.2, n_points)
    current_data3 = 120.0 + 40.0 * np.cos(timestamps / 14.0) + np.random.normal(0, 1.0, n_points)

    voltage_data4 = 70.0 + 3.0 * np.sin(timestamps / 31.0) + np.random.normal(0, 0.2, n_points)
    current_data4 = 50.0 + 30.0 * np.cos(timestamps / 15.0) + np.random.normal(0, 1.0, n_points)

    voltage_data5 = 10.0 + 2.0 * np.sin(timestamps / 23.0) + np.random.normal(0, 0.2, n_points)
    current_data5 = 120.0 + 20.0 * np.cos(timestamps / 300.0) + np.random.normal(0, 1.0, n_points)

    df = pl.DataFrame({
        "timestamp": timestamps,
        "TEST_VOLTAGE1": voltage_data1,
        "TEST_CURRENT1": current_data1,
        "TEST_VOLTAGE2": voltage_data2,
        "TEST_CURRENT2": current_data2,
        "TEST_VOLTAGE3": voltage_data3,
        "TEST_CURRENT3": current_data3,
        "TEST_VOLTAGE4": voltage_data4,
        "TEST_CURRENT4": current_data4,
        "TEST_VOLTAGE5": voltage_data5,
        "TEST_CURRENT5": current_data5
    })

    file_path = os.path.join(data_dir, "dummy_raw.parquet")
    df.write_parquet(file_path)
    print(f"Generated {n_points} rows of dummy Parquet data at: {file_path}")
    print(f"Time span: {start_ts:.1f} to {now:.1f}")


if __name__ == "__main__":
    create_dummy_data()