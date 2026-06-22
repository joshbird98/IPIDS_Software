import pandas as pd


def read_parquet_telemetry(file_path: str) -> pd.DataFrame:
    """
    Reads a Parquet file and returns a pandas DataFrame.
    """
    try:
        # Load the parquet file using the pyarrow engine
        df = pd.read_parquet(file_path, engine='pyarrow')

        # Extract column names
        columns = df.columns.tolist()
        for item in columns:
            if "vacuum_gauge_1" in item:
                print(item)

        return df

    except Exception as e:
        print(f"Failed to read Parquet file: {e}")
        return pd.DataFrame()


if __name__ == "__main__":
    file_path = "example_log.parquet"

    telemetry_data = read_parquet_telemetry(file_path)