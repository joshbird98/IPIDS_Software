import csv
import sys


def convert_csv_delimiter(input_filepath, output_filepath):
    """
    Reads a semicolon-separated CSV and writes it as a comma-separated CSV.
    Uses the csv module to properly handle fields that may inherently contain commas.
    """
    try:
        with open(input_filepath, mode='r', newline='', encoding='utf-8') as infile:
            reader = csv.reader(infile, delimiter=';')

            with open(output_filepath, mode='w', newline='', encoding='utf-8') as outfile:
                writer = csv.writer(outfile, delimiter=',')
                writer.writerows(reader)

    except FileNotFoundError:
        print(f"Error: The file '{input_filepath}' was not found.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"An unexpected error occurred: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    # Execution example. Update paths as required for your environment.
    input_file = 'csv_to_fix.csv'
    output_file = 'output_csv.csv'

    convert_csv_delimiter(input_file, output_file)