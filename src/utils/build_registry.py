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
def get_vacuum_tags(config_dir):
    tags = {}
    vacuum_settings_path = os.path.join(config_dir, "vac_gauges_config.json")

    try:
        with open(vacuum_settings_path, "r") as f:
            vacuum_settings = json.load(f)
    except FileNotFoundError:
        vacuum_settings = {}

    node_routing = {"10": "source", "20": "beamline"}

    for node in [10, 20]:
        for ch in [1, 2, 3]:
            node_str = str(node)
            ch_str = str(ch)

            geo_location = node_routing.get(node_str, f"controller_{node}")
            device = f"vacuum_gauge_{ch}"
            custom_name = f"Node {node} Ch {ch}"

            if node_str in vacuum_settings and "channels" in vacuum_settings[node_str]:
                ch_data = vacuum_settings[node_str]["channels"].get(ch_str, {})
                device = ch_data.get("device", device)
                custom_name = ch_data.get("name", custom_name)

            lower_name = custom_name.lower()
            if any(k in lower_name for k in ["source", "src"]):
                geo_location = "source"
            elif any(k in lower_name for k in ["premag", "bline", "beamline"]):
                geo_location = "beamline"
            elif any(k in lower_name for k in ["ends", "endstation"]):
                geo_location = "endstation"
            elif any(k in lower_name for k in ["ldlk", "loadlock"]):
                geo_location = "loadlock"

            base_tag = f"ion_beam.{geo_location}.{device}"

            tags[f"{base_tag}.pressure"] = {
                "source": "service_vacuum", "hw_node": node, "hw_channel": ch,
                "datatype": "REAL", "unit": "mB", "default_scale": "log",
                "multiplier": 1.0, "description": f"{custom_name} Pressure",
                "default_label": f"{custom_name} Pressure", "short_name": "Pressure"
            }
            tags[f"{base_tag}.status"] = {
                "source": "service_vacuum", "hw_node": node, "hw_channel": ch,
                "datatype": "INT", "unit": "", "default_scale": "linear",
                "multiplier": 1.0, "description": f"{custom_name} Status Code",
                "default_label": f"{custom_name} Status", "short_name": "Status"
            }
    return tags


def get_turbo_tags():
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
            "source": "service_source_turbo", "datatype": info.get("dt", "REAL"),
            "unit": info["unit"], "default_scale": "linear", "multiplier": 1.0,
            "description": info["desc"], "default_label": info["label"], "short_name": info["short"]
        }
    return tags

def get_spellman_tags(config_dir):
    tags = {}
    config_path = os.path.join(config_dir, "mpd_config.json")

    try:
        with open(config_path, "r") as f:
            mpd_config = json.load(f)
    except FileNotFoundError:
        return tags

    for bus in mpd_config.get("buses", []):
        for dev_name, dev_info in bus.get("devices", {}).items():
            base_tag = f"ion_beam.spellman.{dev_name}"

            # Readbacks (Telemetry)
            tags[f"{base_tag}.voltage_rb"] = {
                "source": "service_spellman", "datatype": "REAL", "writable": False,
                "unit": "kV", "description": f"{dev_name} Voltage Readback"
            }
            tags[f"{base_tag}.current_rb"] = {
                "source": "service_spellman", "datatype": "REAL", "writable": False,
                "unit": "mA", "description": f"{dev_name} Current Readback"
            }
            tags[f"{base_tag}.stat_enabled"] = {
                "source": "service_spellman", "datatype": "BOOL", "writable": False,
                "unit": "", "description": f"{dev_name} HV Output Status"
            }

            # Setpoints (Commands sent directly to the ZMQ service)
            tags[f"{base_tag}.voltage_sp"] = {
                "source": "service_spellman", "datatype": "REAL", "writable": True,
                "unit": "kV", "description": f"{dev_name} Voltage Setpoint"
            }
            tags[f"{base_tag}.current_sp"] = {
                "source": "service_spellman", "datatype": "REAL", "writable": True,
                "unit": "mA", "description": f"{dev_name} Current Setpoint"
            }
            tags[f"{base_tag}.cmd_enable"] = {
                "source": "service_spellman", "datatype": "BOOL", "writable": True,
                "unit": "", "description": f"{dev_name} HV Enable Command"
            }

    return tags

# --- SCL Parser specific to Siemens S7 Memory Alignment (Nested STRUCTs) ---
def get_plc_tags_from_scl(scl_path, db_number=10, machine_root="ion_beam"):
    tags = {}
    if not os.path.exists(scl_path):
        print(f"Warning: PLC SCL file not found at {scl_path}. Skipping.")
        return tags

    s7_type_map = {
        'Bool': 'S7WLBit', 'Int': 'S7WLWord', 'DInt': 'S7WLDWord',
        'DWord': 'S7WLDWord', 'Real': 'S7WLReal', 'Byte': 'S7WLByte'
    }

    with open(scl_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    in_db = False
    db_byte_offset = 0
    udt_bit_offset = 0
    path_stack = []

    re_db_any = re.compile(r'^DATA_BLOCK\s+"([^"]+)"', re.IGNORECASE)
    re_struct_open = re.compile(r'^\s*([a-zA-Z0-9_]+)\s*:\s*STRUCT', re.IGNORECASE)
    re_struct_close = re.compile(r'^\s*END_STRUCT', re.IGNORECASE)
    re_var = re.compile(r'^\s*([a-zA-Z0-9_]+)\s*:\s*(Bool|Int|DInt|DWord|Real|Byte|Word|SInt)\s*;\s*(?://(.*))?',
                        re.IGNORECASE)
    re_udt = re.compile(r'^\s*([a-zA-Z0-9_]+)\s*:\s*"?(UDT_[a-zA-Z0-9_]+)"?\s*;\s*(?://(.*))?', re.IGNORECASE)

    def align_to_word():
        """Forces the current byte offset to an even Word boundary."""
        nonlocal db_byte_offset, udt_bit_offset
        if udt_bit_offset > 0:
            db_byte_offset += 1
            udt_bit_offset = 0
        if db_byte_offset % 2 != 0:
            db_byte_offset += 1

    for line in lines:
        if not in_db:
            db_match = re_db_any.match(line)
            if db_match and db_match.group(1) == "DB_PC_Interface":
                in_db = True
                db_byte_offset = 0
                udt_bit_offset = 0
            continue

        if line.strip() == "END_DATA_BLOCK":
            in_db = False
            continue

        struct_open = re_struct_open.match(line)
        if struct_open:
            align_to_word()  # Structs always start on an even byte
            path_stack.append(struct_open.group(1))
            continue

        if re_struct_close.match(line):
            align_to_word()  # Structs always end on an even byte
            if path_stack:
                path_stack.pop()
            continue

        # Check for standard primitives
        var_match = re_var.match(line)
        is_udt = False

        # If not primitive, check for Fault Word UDTs
        if not var_match:
            var_match = re_udt.match(line)
            is_udt = bool(var_match)

        if var_match:
            name = var_match.group(1)
            dt = var_match.group(2)
            comment = var_match.group(3) or ""

            if dt.lower() == 'bool':
                if udt_bit_offset > 7:
                    db_byte_offset += 1
                    udt_bit_offset = 0
                var_byte = db_byte_offset
                var_bit = udt_bit_offset
                udt_bit_offset += 1
            else:
                align_to_word()
                var_byte = db_byte_offset
                var_bit = 0

                # Advance offset based on datatype size
                lower_dt = dt.lower()
                if lower_dt in ['int', 'word']:
                    db_byte_offset += 2
                elif lower_dt in ['real', 'dint', 'dword'] or dt.startswith('UDT_Fault_Word'):
                    db_byte_offset += 4
                elif lower_dt in ['byte', 'sint']:
                    db_byte_offset += 1

            # Build Tag Path
            writable = False
            filtered_stack = []

            # Use To_PC/From_PC to determine writability, but strip them from the final tag name
            for layer in path_stack:
                if layer.lower() == "from_pc":
                    writable = True
                elif layer.lower() == "to_pc":
                    writable = False
                else:
                    filtered_stack.append(layer.lower().replace('_', '.'))

            subsystem_path = ".".join(filtered_stack)

            # --- OVERRIDE FOR FAULT MAP ALIGNMENT ---
            if is_udt and dt.lower().startswith('udt_fault_word'):
                # Extracts "word_0_system" from "UDT_Fault_Word_0_System"
                fault_suffix = dt.lower().replace("udt_fault_", "")
                tag_name = f"{machine_root}.{subsystem_path}.{fault_suffix}"
            else:
                tag_name = f"{machine_root}.{subsystem_path}.{name.lower()}"

            tag_name = tag_name.replace('..', '.')  # Cleanup if stack was empty

            # Parse Comments for Metadata
            meta = {}
            if "=" in comment:
                pairs = [p.strip() for p in comment.replace(',', '|').split('|')]
                for p in pairs:
                    if "=" in p:
                        k, v = p.split('=', 1)
                        meta[k.strip().lower()] = v.strip()
            else:
                meta['desc'] = comment if comment else name.replace('_', ' ')

            tags[tag_name] = {
                "source": "service_plc_snap7",
                "datatype": "DWORD" if is_udt else dt.upper(),
                "writable": writable,
                "unit": meta.get("unit", ""),
                "default_scale": meta.get("scale", "linear"),
                "multiplier": float(meta.get("mult", 1.0)),
                "description": meta.get("desc", ""),
                "default_label": meta.get("label", name.replace('_', ' ')),
                "short_name": meta.get("short", name.split('_')[-1]),
                "db_number": db_number,
                "byte_offset": var_byte,
                "bit_offset": var_bit,
                "snap7_type": "S7WLDWord" if is_udt else s7_type_map.get(dt, "S7WLByte")
            }

    return tags


# --- Main Execution ---
def build_system_registry():
    # Dynamic Path Resolution (Assuming script is in src/utils/)
    current_dir = os.path.dirname(os.path.abspath(__file__))
    src_dir = os.path.dirname(current_dir)
    project_root = os.path.dirname(src_dir)

    config_dir = os.path.join(project_root, "config")
    registry_path = os.path.join(config_dir, "system_tags.json")

    # Ensure config directory exists
    os.makedirs(config_dir, exist_ok=True)

    existing_registry = {}
    if os.path.exists(registry_path):
        try:
            with open(registry_path, "r") as f:
                existing_registry = json.load(f)
            print(f"Loaded existing registry with {len(existing_registry)} tags for safe merge.")
        except Exception as e:
            print(f"Could not load existing registry: {e}")

    new_registry = {}

    vacuum_tags = get_vacuum_tags(config_dir)
    turbo_tags = get_turbo_tags()
    spellman_tags = get_spellman_tags(config_dir)

    scl_path = os.path.join(project_root, "plc", "generated", "PLC_PC_Interface.scl")
    plc_tags = get_plc_tags_from_scl(scl_path, db_number=10)

    new_registry.update(vacuum_tags)
    new_registry.update(turbo_tags)
    new_registry.update(plc_tags)
    new_registry.update(spellman_tags)

    new_registry = apply_existing_overrides(new_registry, existing_registry)

    with open(registry_path, "w") as f:
        sorted_registry = {k: new_registry[k] for k in sorted(new_registry.keys())}
        json.dump(sorted_registry, f, indent=4)

    print(f"Registry successfully built with {len(sorted_registry)} tags.")


if __name__ == "__main__":
    build_system_registry()