import json
import os

# Define absolute paths relative to this script's location
CONFIG_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '../config/fault_config.json'))
OUTPUT_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), 'generated/faults.udt'))


def build_udt_file():
    """Reads the JSON SSOT and compiles a padded Siemens SCL UDT file."""

    # 1. Load the SSOT Configuration
    try:
        with open(CONFIG_PATH, 'r') as f:
            fault_map = json.load(f)
    except FileNotFoundError:
        print(f"ERROR: Cannot find {CONFIG_PATH}")
        return
    except json.JSONDecodeError:
        print(f"ERROR: Invalid JSON formatting in {CONFIG_PATH}")
        return

    # Ensure the output directory exists
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)

    # 2. Generate the SCL Syntax
    with open(OUTPUT_PATH, 'w') as f:
        for udt_name, data in fault_map.items():
            # Target the bits dictionary
            fault_definitions = data.get("bits", {})
            f.write(f'TYPE "{udt_name}"\n')
            f.write('VERSION : 0.1\n')
            f.write('   STRUCT\n')

            # Iterate through a 32-bit DWORD matrix (4 bytes, 8 bits)
            for byte_index in range(4):
                for bit_index in range(8):
                    offset_key = f"{byte_index}.{bit_index}"

                    # Apply defined fault or generate a placeholder pad
                    if offset_key in fault_definitions:
                        tag_name = fault_definitions[offset_key]["name"]
                    else:
                        tag_name = f"Spare_{byte_index}_{bit_index}"

                    f.write(f'      {tag_name} : Bool;\n')

            f.write('   END_STRUCT;\n')
            f.write('END_TYPE\n\n')

    print(f"SUCCESS: Generated TIA Portal source file at: {OUTPUT_PATH}")


if __name__ == "__main__":
    build_udt_file()