import pandas as pd

# Define input and output filenames
input_file = 'Slits_Suggesting_Halo_Log_Gemini_20260724_210435.csv'
output_file = 'downsampled_' + input_file

# Load the CSV file
df = pd.read_csv(input_file)

# Discard the first two columns (iloc[:, 2:] selects all rows, and columns from index 2 to the end)
df = df.iloc[:, 2:]

# Downsample by a factor of 100 using a mean average
# df.index // 100 creates groups of 100 rows (0-99, 100-199, etc.)
df_downsampled = df.groupby(df.index // 20).mean()

# Export the downsampled dataframe to a new CSV file
# index=False prevents the grouped index from being written as an additional column
df_downsampled.to_csv(output_file, index=False)