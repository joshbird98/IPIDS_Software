import json
import os
import re
from tia_db_parser import parse_tia_db


def make_pretty_label(raw_tag_name):
    """Extracts the last two nodes of a tag path and formats them beautifully."""
    # 1. Split by dots and grab the last two segments (e.g., ["filament", "readback"])
    parts = raw_tag_name.split('.')
    core_parts = parts[-2:] if len(parts) >= 2 else parts

    # Join them with a space
    s = " ".join(core_parts)

    # 2. Replace underscores with spaces
    s = s.replace('_', ' ')

    # 3. Split camelCase (e.g., 'coolantStatus' -> 'coolant Status')
    s = re.sub(r'(?<!^)(?=[A-Z])', ' ', s)

    # 4. Capitalize and clean up
    return " ".join(s.split()).title()

def build_system_registry():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(current_dir)
    registry_path = os.path.join(project_root, "system_tags.json")

    # --- SAFE MERGE: Load existing registry to preserve custom edits ---
    existing_registry = {}
    if os.path.exists(registry_path):
        try:
            with open(registry_path, "r") as f:
                existing_registry = json.load(f)
            print(f"Loaded existing registry with {len(existing_registry)} tags for safe merge.")
        except Exception as e:
            print(f"Could not load existing registry: {e}")

    new_registry = {}

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

            # Default fallbacks if the config is missing
            subsystem = f"controller_{node}"
            device = f"ch_{ch}"
            custom_name = f"Node {node} Ch {ch}"

            # Extract the ISA-95 routing and custom name from config
            if node_str in vacuum_settings and "channels" in vacuum_settings[node_str]:
                ch_data = vacuum_settings[node_str]["channels"].get(ch_str, {})
                subsystem = ch_data.get("subsystem", subsystem)
                device = ch_data.get("device", device)
                custom_name = ch_data.get("name", custom_name)

            # 1. Format the Base Tag (ISA-95 Standard)
            # Creates e.g., "vacuum.source_chamber.gauge_vg1"
            base_tag = f"vacuum.{subsystem}.{device}"

            def get_existing(tag, key, default):
                return existing_registry.get(tag, {}).get(key, default)

            # 2. Build the Pressure Tag
            new_registry[f"{base_tag}.pressure"] = {
                "source": "service_vacuum",
                "hw_node": node,
                "hw_channel": ch,
                "datatype": "REAL",
                "unit": "mB",
                "default_scale": "log",
                "multiplier": get_existing(f"{base_tag}.pressure", "multiplier", 1.0),
                "description": get_existing(f"{base_tag}.pressure", "description", f"{custom_name} Pressure"),
                "default_label": get_existing(f"{base_tag}.pressure", "default_label", f"{custom_name} Pressure")
            }

            # 3. Build the Status Tag
            new_registry[f"{base_tag}.status"] = {
                "source": "service_vacuum",
                "hw_node": node,
                "hw_channel": ch,
                "datatype": "INT",
                "unit": "",
                "default_scale": "linear",
                "multiplier": 1.0,
                "description": get_existing(f"{base_tag}.status", "description", f"{custom_name} Status Code"),
                "default_label": get_existing(f"{base_tag}.status", "default_label", f"{custom_name} Status")
            }

    # --- 2. PLC Tags ---
    db_path = os.path.join(project_root, "config", "hmiDB.db")
    try:
        plc_tags = parse_tia_db(db_path)
    except FileNotFoundError:
        print(f"Warning: PLC DB not found at {db_path}. Skipping.")
        plc_tags = {}

    allowed_numeric_types = {"BOOL", "INT", "UINT", "WORD", "DINT", "UDINT", "DWORD", "REAL"}

    # Define unit suffixes to auto-strip
    unit_suffixes = {
        "_W": "W", "_A": "A", "_V": "V", "_mB": "mB", "_C": "°C",
        "_Hz": "Hz", "_rpm": "RPM", "_pct": "%", "_bar": "bar"
    }

    for raw_tag_name, tag_info in plc_tags.items():
        tag_type = tag_info["type"]
        tag_comment = tag_info["comment"]

        if tag_type in allowed_numeric_types:
            formatted_tag = f"plc.{raw_tag_name}" if not raw_tag_name.startswith("plc.") else raw_tag_name

            # 1. Auto-detect Unit and strip suffix for the clean label
            unit = ""
            raw_base_name = raw_tag_name
            for suffix, u in unit_suffixes.items():
                if raw_tag_name.endswith(suffix):
                    unit = u
                    raw_base_name = raw_tag_name[:-len(suffix)]  # Strip the suffix
                    break

            # Make it beautiful (e.g., "coolantStatus" -> "Coolant Status")
            clean_label = make_pretty_label(raw_base_name)

            # 2. Safe Merge existing data
            existing_tag = existing_registry.get(formatted_tag, {})

            # Priority: Existing Description > TIA Portal Comment > Blank
            final_desc = tag_comment if tag_comment else ""
            if existing_tag.get("description"):
                final_desc = existing_tag.get("description")

            new_registry[formatted_tag] = {
                "source": "service_plc",
                "datatype": tag_type,
                "unit": existing_tag.get("unit", unit),
                "default_scale": existing_tag.get("default_scale", "linear"),
                "multiplier": existing_tag.get("multiplier", 1.0),
                "description": final_desc,
                "default_label": existing_tag.get("default_label", clean_label)
            }

    # --- 3. Turbovac Tags ---
    turbo_base_tags = {
        "vacuum.source_chamber.turbo_1.speed_hz": {"unit": "Hz", "desc": "Actual Frequency", "label": "Turbo Speed"},
        "vacuum.source_chamber.turbo_1.speed_pct": {"unit": "%", "desc": "Percent of Max Speed", "label": "Turbo %"},
        "vacuum.source_chamber.turbo_1.temp_bearing": {"unit": "°C", "desc": "Bearing Temperature",
                                                       "label": "Bearing Temp"},
        "vacuum.source_chamber.turbo_1.temp_converter": {"unit": "°C", "desc": "Converter Temperature",
                                                         "label": "Converter Temp"},
        "vacuum.source_chamber.turbo_1.voltage": {"unit": "V", "desc": "Motor Voltage", "label": "Turbo Voltage"},
        "vacuum.source_chamber.turbo_1.current": {"unit": "A", "desc": "Motor Current", "label": "Turbo Current"},
        "vacuum.source_chamber.turbo_1.status_turning": {"unit": "Bool", "desc": "Is rotor turning",
                                                         "label": "Turbo Turning"},
        "vacuum.source_chamber.turbo_1.status_ready": {"unit": "Bool", "desc": "Normal operation reached",
                                                       "label": "Turbo Ready"},
        "vacuum.source_chamber.turbo_1.status_error": {"unit": "Bool", "desc": "Active error state",
                                                       "label": "Turbo Error"}
    }

    for tag, info in turbo_base_tags.items():
        existing_tag = existing_registry.get(tag, {})
        new_registry[tag] = {
            "source": "service_source_turbo",
            "datatype": "REAL",  # Logged as floats for PyQtGraph compatibility
            "unit": existing_tag.get("unit", info["unit"]),
            "default_scale": existing_tag.get("default_scale", "linear"),
            "multiplier": existing_tag.get("multiplier", 1.0),
            "description": existing_tag.get("description", info["desc"]),
            "default_label": existing_tag.get("default_label", info["label"])
        }

    # --- 4. Save to Disk ---
    with open(registry_path, "w") as f:
        sorted_registry = {k: new_registry[k] for k in sorted(new_registry.keys())}
        json.dump(sorted_registry, f, indent=4)

    print(f"Registry successfully built with {len(sorted_registry)} tags.")

if __name__ == "__main__":
    build_system_registry()