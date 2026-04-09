import json
import os
import re
from tia_db_parser import parse_tia_db

# Centralized suffix mapping (Lowercased for case-insensitive matching)
UNIT_SUFFIXES = {
    "_w": "W", "_a": "A", "_v": "V", "_mb": "mB", "_c": "°C",
    "_hz": "Hz", "_rpm": "RPM", "_pct": "%", "_bar": "bar"
}


def clean_node_name(node_str):
    """Strips unit suffixes from a single node and formats it beautifully. Returns (Clean String, Unit)."""
    unit = ""
    lower_node = node_str.lower()
    for suffix, u in UNIT_SUFFIXES.items():
        if lower_node.endswith(suffix):
            node_str = node_str[:-len(suffix)]
            unit = u
            break

    # Replace underscores, split camelCase, title case
    s = node_str.replace('_', ' ')
    s = re.sub(r'(?<!^)(?=[A-Z])', ' ', s)
    return " ".join(s.split()).title(), unit


def make_pretty_label(raw_tag_name):
    """Extracts the last two nodes of a tag path, cleans them, and formats them beautifully."""
    parts = raw_tag_name.split('.')
    core_parts = parts[-2:] if len(parts) >= 2 else parts

    cleaned_parts = [clean_node_name(p)[0] for p in core_parts]
    return " ".join(cleaned_parts)


def build_system_registry():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(current_dir)
    registry_path = os.path.join(project_root, "system_tags.json")

    existing_registry = {}
    if os.path.exists(registry_path):
        try:
            with open(registry_path, "r") as f:
                existing_registry = json.load(f)
            print(f"Loaded existing registry with {len(existing_registry)} tags for safe merge.")
        except Exception as e:
            print(f"Could not load existing registry: {e}")

    new_registry = {}

    def get_existing(tag, key, default):
        return existing_registry.get(tag, {}).get(key, default)

    # --- 1. Vacuum System Tags ---
    vacuum_settings_path = os.path.join(project_root, "config", "vacuum_settings.json")
    try:
        with open(vacuum_settings_path, "r") as f:
            vacuum_settings = json.load(f)
    except FileNotFoundError:
        vacuum_settings = {}

    for node in [10, 20]:
        for ch in [1, 2, 3]:
            node_str = str(node)
            ch_str = str(ch)
            subsystem = f"controller_{node}"
            device = f"ch_{ch}"
            custom_name = f"Node {node} Ch {ch}"

            if node_str in vacuum_settings and "channels" in vacuum_settings[node_str]:
                ch_data = vacuum_settings[node_str]["channels"].get(ch_str, {})
                subsystem = ch_data.get("subsystem", subsystem)
                device = ch_data.get("device", device)
                custom_name = ch_data.get("name", custom_name)

            base_tag = f"vacuum.{subsystem}.{device}"

            new_registry[f"{base_tag}.pressure"] = {
                "source": "service_vacuum",
                "hw_node": node,
                "hw_channel": ch,
                "datatype": "REAL",
                "unit": "mB",
                "default_scale": "log",
                "multiplier": get_existing(f"{base_tag}.pressure", "multiplier", 1.0),
                "description": get_existing(f"{base_tag}.pressure", "description", f"{custom_name} Pressure"),
                "default_label": get_existing(f"{base_tag}.pressure", "default_label", f"{custom_name} Pressure"),
                "short_name": "Pressure"  # Explicit short name for the UI tree
            }

            new_registry[f"{base_tag}.status"] = {
                "source": "service_vacuum",
                "hw_node": node,
                "hw_channel": ch,
                "datatype": "INT",
                "unit": "",
                "default_scale": "linear",
                "multiplier": 1.0,
                "description": get_existing(f"{base_tag}.status", "description", f"{custom_name} Status Code"),
                "default_label": get_existing(f"{base_tag}.status", "default_label", f"{custom_name} Status"),
                "short_name": "Status"
            }

    # --- 2. PLC Tags ---
    db_path = os.path.join(project_root, "config", "hmiDB.db")
    try:
        plc_tags = parse_tia_db(db_path)
    except FileNotFoundError:
        print(f"Warning: PLC DB not found at {db_path}. Skipping.")
        plc_tags = {}

    allowed_numeric_types = {"BOOL", "INT", "UINT", "WORD", "DINT", "UDINT", "DWORD", "REAL"}

    for raw_tag_name, tag_info in plc_tags.items():
        tag_type = tag_info["type"]
        tag_comment = tag_info["comment"]

        if tag_type in allowed_numeric_types:
            formatted_tag = f"plc.{raw_tag_name}" if not raw_tag_name.startswith("plc.") else raw_tag_name

            # Extract the raw final node (e.g., "cooling_water_temp_C")
            raw_last_node = raw_tag_name.split('.')[-1]

            # Clean it to get "Cooling Water Temp" and "°C"
            short_name, detected_unit = clean_node_name(raw_last_node)

            # Generate a broader label using the last two nodes
            clean_label = make_pretty_label(raw_tag_name)

            existing_tag = existing_registry.get(formatted_tag, {})
            final_desc = tag_comment if tag_comment else ""
            if existing_tag.get("description"):
                final_desc = existing_tag.get("description")

            new_registry[formatted_tag] = {
                "source": "service_plc",
                "datatype": tag_type,
                "unit": existing_tag.get("unit", detected_unit),
                "default_scale": existing_tag.get("default_scale", "linear"),
                "multiplier": existing_tag.get("multiplier", 1.0),
                "description": final_desc,
                "default_label": existing_tag.get("default_label", clean_label),
                "short_name": existing_tag.get("short_name", short_name)
            }

    # --- 3. Turbovac Tags ---
    turbo_base_tags = {
        "vacuum.source.turbo_1.speed_hz": {"unit": "Hz", "short": "Speed", "desc": "Actual Frequency",
                                                   "label": "Turbo Speed"},
        "vacuum.source.turbo_1.speed_pct": {"unit": "%", "short": "Speed", "desc": "Percent of Max Speed",
                                                    "label": "Turbo Speed"},
        "vacuum.source.turbo_1.temp_bearing": {"unit": "°C", "short": "Bearing Temp",
                                                       "desc": "Bearing Temperature", "label": "Bearing Temp"},
        "vacuum.source.turbo_1.temp_converter": {"unit": "°C", "short": "Converter Temp",
                                                         "desc": "Converter Temperature", "label": "Converter Temp"},
        "vacuum.source.turbo_1.voltage": {"unit": "V", "short": "Voltage", "desc": "Motor Voltage",
                                                  "label": "Turbo Voltage"},
        "vacuum.source.turbo_1.current": {"unit": "A", "short": "Current", "desc": "Motor Current",
                                                  "label": "Turbo Current"},
        "vacuum.source.turbo_1.status_turning": {"unit": "", "short": "Status Turning",
                                                         "desc": "Is rotor turning", "label": "Turbo Turning"},
        "vacuum.source.turbo_1.status_ready": {"unit": "", "short": "Status Ready",
                                                       "desc": "Normal operation reached", "label": "Turbo Ready"},
        "vacuum.source.turbo_1.status_error": {"unit": "", "short": "Status Error",
                                                       "desc": "Active error state", "label": "Turbo Error"}
    }

    for tag, info in turbo_base_tags.items():
        existing_tag = existing_registry.get(tag, {})
        new_registry[tag] = {
            "source": "service_source_turbo",
            "datatype": "REAL",
            "unit": existing_tag.get("unit", info["unit"]),
            "default_scale": existing_tag.get("default_scale", "linear"),
            "multiplier": existing_tag.get("multiplier", 1.0),
            "description": existing_tag.get("description", info["desc"]),
            "default_label": existing_tag.get("default_label", info["label"]),
            "short_name": existing_tag.get("short_name", info["short"])
        }

    # --- 4. Save to Disk ---
    with open(registry_path, "w") as f:
        sorted_registry = {k: new_registry[k] for k in sorted(new_registry.keys())}
        json.dump(sorted_registry, f, indent=4)

    print(f"Registry successfully built with {len(sorted_registry)} tags.")


if __name__ == "__main__":
    build_system_registry()