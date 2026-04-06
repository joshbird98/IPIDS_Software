# utils/db_parser.py
import re


def parse_tia_db(filepath: str) -> dict:
    """
    Parses a TIA Portal DB export file and returns a dictionary of tags.
    """
    tags = {}
    stack = []

    re_array_bool = re.compile(r'((?:"[^"]+")|(?:\w+))(?:.*):\s*Array\[(\d+)\.\.(\d+)\]\s*of\s*Bool', re.IGNORECASE)
    re_array_int = re.compile(r'((?:"[^"]+")|(?:\w+))(?:.*):\s*Array\[(\d+)\.\.(\d+)\]\s*of\s*Int', re.IGNORECASE)
    re_std = re.compile(r'((?:"[^"]+")|(?:\w+))(?:.*):\s*(\w+)')

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        print(f"Warning: DB file not found at {filepath}")
        return tags

    in_struct = False

    for line in lines:
        line = line.strip()

        if line.startswith("STRUCT"):
            in_struct = True
            continue
        if line.startswith("END_STRUCT;"):
            if stack:
                stack.pop()
            continue
        if line.startswith("END_DATA_BLOCK"):
            break

        if in_struct and ":" in line and not line.startswith("TITLE"):
            array_match_bool = re_array_bool.search(line)
            array_match_int = re_array_int.search(line)
            std_match = re_std.match(line)

            if array_match_bool:
                name = array_match_bool.group(1).replace('"', '')
                start, end = int(array_match_bool.group(2)), int(array_match_bool.group(3))
                for i in range(start, end + 1):
                    tags[".".join(stack + [f"{name}[{i}]"])] = "BOOL"
            elif array_match_int:
                name = array_match_int.group(1).replace('"', '')
                start, end = int(array_match_int.group(2)), int(array_match_int.group(3))
                for i in range(start, end + 1):
                    tags[".".join(stack + [f"{name}[{i}]"])] = "INT"
            elif std_match:
                name = std_match.group(1).replace('"', '')
                dtype = std_match.group(2).upper()
                if dtype == "STRUCT":
                    stack.append(name)
                else:
                    tags[".".join(stack + [name])] = dtype

    return tags