import pandas as pd
import sys


def subtract_mass_scans(file1_path, file2_path, output_path,
                        mag_col='Measured_Magnet_A', beam_col='Measured_Beam_A',
                        tolerance_amps=0.020):
    """
    Synchronizes two mass-scan CSV files based on measured magnet current
    and subtracts the beam measurements.
    """
    try:
        df1 = pd.read_csv(file1_path)
        df2 = pd.read_csv(file2_path)

        for df, filepath in [(df1, file1_path), (df2, file2_path)]:
            if mag_col not in df.columns or beam_col not in df.columns:
                raise ValueError(f"Columns '{mag_col}' or '{beam_col}' not found in {filepath}. "
                                 f"Available columns: {list(df.columns)}")

        # Isolate necessary columns to prevent merge-suffix bloat from unused sensor data
        df1 = df1[[mag_col, beam_col]].copy()
        df2 = df2[[mag_col, beam_col]].copy()

        # merge_asof requires both DataFrames to be sorted by the key
        df1 = df1.sort_values(by=mag_col)
        df2 = df2.sort_values(by=mag_col)

        # Merge matching the nearest measured magnet current within the specified tolerance
        merged = pd.merge_asof(
            df1,
            df2,
            on=mag_col,
            direction='nearest',
            tolerance=tolerance_amps,
            suffixes=('_base', '_bg')
        )

        # Drop rows where no background scan match was found within the tolerance window
        merged = merged.dropna(subset=[f'{beam_col}_base', f'{beam_col}_bg'])

        if merged.empty:
            raise ValueError(
                "No matching rows found. Please manually double-check your tolerance_amps setting against the variation in Measured_Magnet_A.")

        # Compute the subtracted beam current
        merged['Subtracted_Beam_A'] = merged[f'{beam_col}_base'] - merged[f'{beam_col}_bg']

        # Format output
        output_df = merged[[mag_col, 'Subtracted_Beam_A']]
        output_df.to_csv(output_path, index=False)

        print(f"Successfully synchronized {len(output_df)} data points.")
        print(f"Output written to {output_path}")

    except Exception as e:
        print(f"Error processing files: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    FILE_1 = 'scan_base.csv'
    FILE_2 = 'scan_background.csv'
    OUTPUT_FILE = 'subtracted_scan.csv'

    # Headers updated to match the provided ion implanter data structure
    MAGNET_COLUMN = 'Measured_Magnet_A'
    BEAM_COLUMN = 'Measured_Beam_A'

    # The provided data shows Requested_Magnet_A steps of 40mA (0.04A).
    # Measured_Magnet_A steps fluctuate between ~32mA and ~50mA.
    # A 20mA (0.020A) tolerance provides a strict half-step boundary.
    TOLERANCE = 0.020

    subtract_mass_scans(FILE_1, FILE_2, OUTPUT_FILE,
                        mag_col=MAGNET_COLUMN,
                        beam_col=BEAM_COLUMN,
                        tolerance_amps=TOLERANCE)