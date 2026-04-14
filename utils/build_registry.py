import json
import os
import re

# --- Configuration & Constants ---
UNIT_SUFFIXES = {
    "_w": "W", "_a": "A", "_v": "V", "_mb": "mB", "_c": "°C",
    "_hz": "Hz", "_rpm": "RPM", "_pct": "%", "_bar": "bar"
}


# --- Formatting Helpers ---
def clean_node_name(node_str):
    unit = ""
    lower_node = node_str.lower()
    for suffix, u in UNIT_SUFFIXES.items():
        if lower_node.endswith(suffix):
            node_str = node_str[:-len(suffix)]
            unit = u
            break

    s = node_str.replace('_', ' ')
    s = re.sub(r'(?<!^)(?=[A-Z])', ' ', s)
    return " ".join(s.split()).title(), unit


def make_pretty_label(raw_tag_name):
    parts = raw_tag_name.split('.')
    core_parts = parts[-2:] if len(parts) >= 2 else parts
    cleaned_parts = [clean_node_name(p)[0] for p in core_parts]
    return " ".join(cleaned_parts)


def apply_existing_overrides(new_tags, existing_registry):
    preserved_fields = ["datatype", "unit", "default_scale", "multiplier", "description", "default_label", "short_name"]
    for tag_name, tag_data in new_tags.items():
        if tag_name in existing_registry:
            existing_data = existing_registry[tag_name]
            for field in preserved_fields:
                if field in existing_data:
                    tag_data[field] = existing_data[field]
    return new_tags


# --- Subsystem Tag Builders ---
def get_vacuum_tags(project_root):
    """Distributes vacuum components to strict geographical locations"""
    tags = {}
    vacuum_settings_path = os.path.join(project_root, "config", "vacuum_settings.json")

    try:
        with open(vacuum_settings_path, "r") as f:
            vacuum_settings = json.load(f)
    except FileNotFoundError:
        vacuum_settings = {}

    # Base routing by hardware node (Fallback)
    node_routing = {
        "10": "source",
        "20": "beamline"
    }

    for node in [10, 20]:
        for ch in [1, 2, 3]:
            node_str = str(node)
            ch_str = str(ch)

            geo_location = node_routing.get(node_str, f"controller_{node}")
            device = f"vacuum_gauge_{ch}"
            custom_name = f"Node {node} Ch {ch}"

            # Pull from JSON if it exists
            if node_str in vacuum_settings and "channels" in vacuum_settings[node_str]:
                ch_data = vacuum_settings[node_str]["channels"].get(ch_str, {})
                device = ch_data.get("device", device)
                custom_name = ch_data.get("name", custom_name)

            # --- DYNAMIC GEOGRAPHY OVERRIDE ---
            # Intercept gauges based on their name and force them into the correct ISA-95 area
            lower_name = custom_name.lower()

            if any(k in lower_name for k in ["source", "src"]):
                geo_location = "source"
            elif any(k in lower_name for k in ["premag", "bline", "beamline"]):
                geo_location = "beamline"
            elif any(k in lower_name for k in ["ends", "endstation"]):
                geo_location = "endstation"
            elif any(k in lower_name for k in ["ldlk", "loadlock"]):
                geo_location = "loadlock"

            # E.g., ion_beam.endstation.vg4.pressure
            base_tag = f"ion_beam.{geo_location}.{device}"

            tags[f"{base_tag}.pressure"] = {
                "source": "service_vacuum",
                "hw_node": node,
                "hw_channel": ch,
                "datatype": "REAL",
                "unit": "mB",
                "default_scale": "log",
                "multiplier": 1.0,
                "description": f"{custom_name} Pressure",
                "default_label": f"{custom_name} Pressure",
                "short_name": "Pressure"
            }

            tags[f"{base_tag}.status"] = {
                "source": "service_vacuum",
                "hw_node": node,
                "hw_channel": ch,
                "datatype": "INT",
                "unit": "",
                "default_scale": "linear",
                "multiplier": 1.0,
                "description": f"{custom_name} Status Code",
                "default_label": f"{custom_name} Status",
                "short_name": "Status"
            }
    return tags


def get_turbo_tags():
    """Distributes the turbo pump directly to the source module"""
    tags = {}
    turbo_base = {
        "ion_beam.source.turbo_pump.speed_hz": {"dt": "INT", "unit": "Hz", "short": "Speed", "desc": "Actual Frequency",
                                                "label": "Turbo Speed"},
        "ion_beam.source.turbo_pump.speed_pct": {"dt": "REAL", "unit": "%", "short": "Speed",
                                                 "desc": "Percent of Max Speed", "label": "Turbo Speed"},
        "ion_beam.source.turbo_pump.temp_bearing": {"dt": "INT", "unit": "°C", "short": "Bearing Temp",
                                                    "desc": "Bearing Temperature", "label": "Bearing Temp"},
        "ion_beam.source.turbo_pump.temp_converter": {"dt": "INT", "unit": "°C", "short": "Converter Temp",
                                                      "desc": "Converter Temperature", "label": "Converter Temp"},
        "ion_beam.source.turbo_pump.voltage": {"dt": "INT", "unit": "V", "short": "Voltage", "desc": "Motor Voltage",
                                               "label": "Turbo Voltage"},
        "ion_beam.source.turbo_pump.current": {"dt": "REAL", "unit": "A", "short": "Current", "desc": "Motor Current",
                                               "label": "Turbo Current"},
        "ion_beam.source.turbo_pump.status_turning": {"dt": "BOOL", "unit": "", "short": "Status Turning",
                                                      "desc": "Is rotor turning", "label": "Turbo Turning"},
        "ion_beam.source.turbo_pump.status_ready": {"dt": "BOOL", "unit": "", "short": "Status Ready",
                                                    "desc": "Normal operation reached", "label": "Turbo Ready"},
        "ion_beam.source.turbo_pump.status_error": {"dt": "BOOL", "unit": "", "short": "Status Error",
                                                    "desc": "Active error state", "label": "Turbo Error"}
    }

    for tag_name, info in turbo_base.items():
        tags[tag_name] = {
            "source": "service_source_turbo",
            "datatype": info.get("dt", "REAL"),
            "unit": info["unit"],
            "default_scale": "linear",
            "multiplier": 1.0,
            "description": info["desc"],
            "default_label": info["label"],
            "short_name": info["short"]
        }
    return tags


# --- SCL Parser specific to Siemens S7 Memory Alignment ---
def get_plc_tags_from_scl(scl_path, db_number=10, machine_root="ion_beam"):
    tags = {}
    if not os.path.exists(scl_path):
        print(f"Warning: PLC SCL file not found at {scl_path}. Skipping.")
        return tags

    s7_type_map = {
        'Bool': 'S7WLBit',
        'Int': 'S7WLWord',
        'DInt': 'S7WLDWord',
        'DWord': 'S7WLDWord',
        'Real': 'S7WLReal',
        'Byte': 'S7WLByte'
    }

    with open(scl_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    udt_templates = {}
    current_udt = None
    in_db = False

    re_type = re.compile(r'^TYPE\s+"(UDT_[^"]+)"', re.IGNORECASE)
    re_var = re.compile(r'^\s*([a-zA-Z0-9_]+)\s*:\s*([a-zA-Z0-9_]+)\s*;\s*(?://(.*))?')
    re_db_any = re.compile(r'^DATA_BLOCK\s+"([^"]+)"', re.IGNORECASE)
    re_db_var = re.compile(r'^\s*([a-zA-Z0-9_]+)\s*:\s*"(UDT_[^"]+)"\s*;')

    for line in lines:
        type_match = re_type.match(line)
        if type_match:
            current_udt = type_match.group(1)
            udt_templates[current_udt] = []
            continue

        if line.strip() == "END_TYPE":
            current_udt = None
            continue

        if current_udt:
            var_match = re_var.match(line)
            if var_match:
                name = var_match.group(1)
                dt = var_match.group(2)
                comment = var_match.group(3) or ""
                udt_templates[current_udt].append({"name": name, "dt": dt, "comment": comment.strip()})

    db_byte_offset = 0
    current_db_number = db_number

    for line in lines:
        db_match = re_db_any.match(line)
        if db_match:
            in_db = True
            db_byte_offset = 0

            db_name = db_match.group(1)
            if db_name == "DB_PC_Interface":
                current_db_number = 10
            elif db_name == "DB_PC_Retain":
                current_db_number = 11
            continue

        if in_db and line.strip() == "END_DATA_BLOCK":
            in_db = False
            continue

        if in_db:
            db_var_match = re_db_var.match(line)
            if db_var_match:
                # E.g., 'Source_Extraction' becomes 'source.extraction'
                subsystem_path = db_var_match.group(1).lower().replace('_', '.')
                udt_name = db_var_match.group(2)

                if udt_name in udt_templates:
                    udt_bit_offset = 0

                    for var in udt_templates[udt_name]:
                        dt = var['dt']

                        if dt == 'Bool':
                            if udt_bit_offset > 7:
                                db_byte_offset += 1
                                udt_bit_offset = 0
                            var_byte = db_byte_offset
                            var_bit = udt_bit_offset
                            udt_bit_offset += 1
                        else:
                            if udt_bit_offset > 0:
                                db_byte_offset += 1
                                udt_bit_offset = 0

                            if dt in ['Int', 'Word', 'Real', 'DInt', 'DWord']:
                                if db_byte_offset % 2 != 0:
                                    db_byte_offset += 1

                            var_byte = db_byte_offset
                            var_bit = 0

                            if dt in ['Int', 'Word']:
                                db_byte_offset += 2
                            elif dt in ['Real', 'DInt', 'DWord']:
                                db_byte_offset += 4
                            elif dt in ['Byte', 'SInt']:
                                db_byte_offset += 1

                        tag_name = f"{machine_root}.{subsystem_path}.{var['name'].lower()}"

                        meta = {}
                        comment = var['comment']
                        if "=" in comment:
                            pairs = [p.strip() for p in comment.replace(',', '|').split('|')]
                            for p in pairs:
                                if "=" in p:
                                    k, v = p.split('=', 1)
                                    meta[k.strip().lower()] = v.strip()
                        else:
                            meta['desc'] = comment if comment else var['name'].replace('_', ' ')

                        tags[tag_name] = {
                            "source": "service_plc_snap7",
                            "datatype": dt.upper(),
                            "unit": meta.get("unit", ""),
                            "default_scale": meta.get("scale", "linear"),
                            "multiplier": float(meta.get("mult", 1.0)),
                            "description": meta.get("desc", ""),
                            "default_label": meta.get("label", var['name'].replace('_', ' ')),
                            "short_name": meta.get("short", var['name'].split('_')[-1]),
                            "db_number": current_db_number,
                            "byte_offset": var_byte,
                            "bit_offset": var_bit,
                            "snap7_type": s7_type_map.get(dt, "S7WLByte")
                        }

                    if udt_bit_offset > 0:
                        db_byte_offset += 1
                    if db_byte_offset % 2 != 0:
                        db_byte_offset += 1

    return tags


# --- Main Execution ---
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

    vacuum_tags = get_vacuum_tags(project_root)
    turbo_tags = get_turbo_tags()

    scl_path = os.path.join(project_root, "utils", "PLC_PC_Interface.scl")
    plc_tags = get_plc_tags_from_scl(scl_path, db_number=10)

    new_registry.update(vacuum_tags)
    new_registry.update(turbo_tags)
    new_registry.update(plc_tags)

    new_registry = apply_existing_overrides(new_registry, existing_registry)

    with open(registry_path, "w") as f:
        sorted_registry = {k: new_registry[k] for k in sorted(new_registry.keys())}
        json.dump(sorted_registry, f, indent=4)

    print(f"Registry successfully built with {len(sorted_registry)} tags.")


if __name__ == "__main__":
    build_system_registry()