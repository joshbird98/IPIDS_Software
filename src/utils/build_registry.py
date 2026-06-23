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
    preserved_fields = [
        "datatype", "unit", "default_scale", "multiplier",
        "description", "default_label", "short_name"
    ]
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

    # --- 1. Global Arbitration Tags ---
    tags["ion_beam.facilities.vacuum.rb_ctrl_mode"] = {
        "source": "service_vacuum", "datatype": "INT", "writable": False,
        "unit": "", "default_scale": "linear", "multiplier": 1.0,
        "description": "Active Control Mode", "default_label": "Ctrl Mode", "short_name": "Mode"
    }
    tags["ion_beam.facilities.vacuum.cmd_ctrl_mode"] = {
        "source": "service_vacuum", "datatype": "INT", "writable": True,
        "unit": "", "default_scale": "linear", "multiplier": 1.0,
        "description": "Request Control Mode (0=HMI, 1=Auto)", "default_label": "Cmd Mode", "short_name": "Cmd Mode"
    }

    # --- 2. Gauge Readbacks ---
    mapping = {
        (10, 1): "ion_beam.source.vacuum_gauge_1",
        (10, 2): "ion_beam.beamline.vacuum_gauge_2",
        (10, 3): "ion_beam.beamline.vacuum_gauge_3",
        (20, 1): "ion_beam.endstation.vacuum_gauge_4",
        (20, 2): "ion_beam.loadlock.vacuum_gauge_5",
        (20, 3): "ion_beam.endstation.vacuum_gauge_6"
    }

    for (node, ch), base_tag in mapping.items():
        node_str = str(node)
        ch_str = str(ch)
        custom_name = f"VG{(node // 10 - 1) * 3 + ch}"

        if node_str in vacuum_settings and "channels" in vacuum_settings[node_str]:
            ch_data = vacuum_settings[node_str]["channels"].get(ch_str, {})
            custom_name = ch_data.get("name", custom_name)

        tags[f"{base_tag}.rb_pressure"] = {
            "source": "service_vacuum", "hw_node": node, "hw_channel": ch,
            "datatype": "REAL", "unit": "mB", "default_scale": "log",
            "multiplier": 1.0, "description": f"{custom_name} Pressure Readback",
            "default_label": f"{custom_name} Pressure", "short_name": "Pressure"
        }
        tags[f"{base_tag}.stat_error_code"] = {
            "source": "service_vacuum", "hw_node": node, "hw_channel": ch,
            "datatype": "INT", "unit": "", "default_scale": "linear",
            "multiplier": 1.0, "description": f"{custom_name} Hardware Status Code",
            "default_label": f"{custom_name} Status Code", "short_name": "Code"
        }
        tags[f"{base_tag}.stat_comms_fail"] = {
            "source": "service_vacuum", "hw_node": node, "hw_channel": ch,
            "datatype": "BOOL", "unit": "", "default_scale": "linear",
            "multiplier": 1.0, "description": f"{custom_name} RS485 Comms Failure",
            "default_label": f"{custom_name} Comms Fail", "short_name": "Comms"
        }

    # --- 3. Relay Setpoints (Auto-Controllable) ---
    # Transducer physical limits for UI clamping
    LEYBOLD_MIN_MBAR = 1.0e-10
    LEYBOLD_MAX_MBAR = 1000.0

    for node_str, node_data in vacuum_settings.items():
        if not node_str.isdigit():
            continue

        node_id = int(node_str)
        if "relays" in node_data:
            for relay_str, relay_params in node_data["relays"].items():
                relay_id = int(relay_str)
                base_relay_tag = None
                if int(node_id) == 10:
                    base_relay_tag = f"ion_beam.facilities.graphix1.relay_{relay_id}"
                elif int(node_id) == 20:
                    base_relay_tag = f"ion_beam.facilities.graphix2.relay_{relay_id}"
                else:
                    continue

                tags[f"{base_relay_tag}_on"] = {
                    "source": "service_vacuum", "datatype": "REAL", "writable": True,
                    "auto_controllable": True, "min_val": LEYBOLD_MIN_MBAR, "max_val": LEYBOLD_MAX_MBAR,
                    "unit": "mB", "default_scale": "log", "multiplier": 1.0,
                    "description": f"Node {node_id} Relay {relay_id} Turn-On Setpoint",
                    "default_label": f"Relay {relay_id} ON", "short_name": "ON SP"
                }

                tags[f"{base_relay_tag}_off"] = {
                    "source": "service_vacuum", "datatype": "REAL", "writable": True,
                    "auto_controllable": True, "min_val": LEYBOLD_MIN_MBAR, "max_val": LEYBOLD_MAX_MBAR,
                    "unit": "mB", "default_scale": "log", "multiplier": 1.0,
                    "description": f"Node {node_id} Relay {relay_id} Turn-Off Setpoint",
                    "default_label": f"Relay {relay_id} OFF", "short_name": "OFF SP"
                }

    return tags

def get_turbo_tags():
    tags = {}
    base_tag = "ion_beam.source.turbo_pump"

    turbo_base = {
        f"{base_tag}.rb_speed_hz": {"dt": "INT", "unit": "Hz", "short": "Speed", "desc": "Actual Frequency",
                                    "label": "Turbo Speed"},
        f"{base_tag}.rb_speed_pct": {"dt": "REAL", "unit": "%", "short": "Speed %", "desc": "Percent of Max Speed",
                                     "label": "Turbo Speed %"},
        f"{base_tag}.rb_temp_bearing": {"dt": "INT", "unit": "°C", "short": "Brg Temp", "desc": "Bearing Temperature",
                                        "label": "Bearing Temp"},
        f"{base_tag}.rb_temp_converter": {"dt": "INT", "unit": "°C", "short": "Conv Temp",
                                          "desc": "Converter Temperature", "label": "Converter Temp"},
        f"{base_tag}.rb_voltage": {"dt": "INT", "unit": "V", "short": "Voltage", "desc": "Motor Voltage",
                                   "label": "Turbo Voltage"},
        f"{base_tag}.rb_current": {"dt": "REAL", "unit": "A", "short": "Current", "desc": "Motor Current",
                                   "label": "Turbo Current"},
        f"{base_tag}.stat_turning": {"dt": "BOOL", "unit": "", "short": "Turning", "desc": "Is rotor turning",
                                     "label": "Turbo Turning"},
        f"{base_tag}.stat_ready": {"dt": "BOOL", "unit": "", "short": "Ready", "desc": "Normal operation reached",
                                   "label": "Turbo Ready"},
        f"{base_tag}.stat_error": {"dt": "BOOL", "unit": "", "short": "Error", "desc": "Active hardware error state",
                                   "label": "Turbo Error"},
        f"{base_tag}.stat_comms_fail": {"dt": "BOOL", "unit": "", "short": "Comms Fail",
                                        "desc": "Service communications offline", "label": "Turbo Comms Fail"},
        f"{base_tag}.cmd_ctrl_mode": {"dt": "INT", "unit": "", "short": "Cmd Ctrl Mode",
                                        "desc": "Command Control Mode", "label": "Cmd Ctrl Mode"},
        f"{base_tag}.rb_ctrl_mode": {"dt": "INT", "unit": "", "short": "Rb Ctrl Mode",
                                        "desc": "Readback Control Mode", "label": "Rb Ctrl Mode"}
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

    location_routing = {
        "source_einzel": "ion_beam.source.einzel",
        "beamline_einzel": "ion_beam.beamline.einzel",
        "neutral_trap_pos": "ion_beam.beamline.neutral_trap_pos",
        "neutral_trap_neg": "ion_beam.beamline.neutral_trap_neg"
    }

    try:
        with open(config_path, "r") as f:
            mpd_config = json.load(f)
    except FileNotFoundError:
        return tags

    for bus in mpd_config.get("buses", []):
        for dev_name, dev_info in bus.get("devices", {}).items():
            base_tag = location_routing.get(dev_name, f"ion_beam.source.{dev_name}")
            nice_name = dev_name.replace('_', ' ').title()

            # Extract hardware limits, converting V to kV to match registry units
            min_v = dev_info.get("min_value")
            max_v = dev_info.get("max_value")
            min_kv = (min_v / 1000.0) if min_v is not None else None
            max_kv = (max_v / 1000.0) if max_v is not None else None

            # Readbacks
            tags[f"{base_tag}.rb_voltage"] = {
                "source": "service_spellman", "datatype": "REAL", "writable": False,
                "unit": "kV", "default_scale": "linear", "multiplier": 1.0,
                "description": f"{nice_name} Voltage Readback", "default_label": f"{nice_name} Voltage",
                "short_name": "Voltage"
            }
            tags[f"{base_tag}.rb_current"] = {
                "source": "service_spellman", "datatype": "REAL", "writable": False,
                "unit": "µA", "default_scale": "linear", "multiplier": 1.0,
                "description": f"{nice_name} Current Readback", "default_label": f"{nice_name} Current",
                "short_name": "Current"
            }
            tags[f"{base_tag}.stat_enabled"] = {
                "source": "service_spellman", "datatype": "BOOL", "writable": False,
                "unit": "", "default_scale": "linear", "multiplier": 1.0,
                "description": f"{nice_name} Output Status", "default_label": f"{nice_name} Output Status",
                "short_name": "Status"
            }
            tags[f"{base_tag}.sp_actual_voltage"] = {
                "source": "service_spellman", "datatype": "REAL", "writable": False,
                "unit": "kV", "default_scale": "linear", "multiplier": 1.0,
                "description": f"{nice_name} Hardware Voltage Setpoint Cache",
                "default_label": f"{nice_name} Voltage SP (Act)", "short_name": "V_SP Act"
            }
            tags[f"{base_tag}.sp_actual_current"] = {
                "source": "service_spellman", "datatype": "REAL", "writable": False,
                "unit": "µA", "default_scale": "linear", "multiplier": 1.0,
                "description": f"{nice_name} Hardware Current Setpoint Cache",
                "default_label": f"{nice_name} Current SP (Act)", "short_name": "I_SP Act"
            }

            # Control Mode Arbitration
            tags[f"{base_tag}.rb_ctrl_mode"] = {
                "source": "service_spellman", "datatype": "INT", "writable": False,
                "unit": "", "default_scale": "linear", "multiplier": 1.0,
                "description": f"{nice_name} Active Control Mode", "default_label": f"{nice_name} Ctrl Mode",
                "short_name": "Mode"
            }
            tags[f"{base_tag}.cmd_ctrl_mode"] = {
                "source": "service_spellman", "datatype": "INT", "writable": True,
                "unit": "", "default_scale": "linear", "multiplier": 1.0,
                "description": f"{nice_name} Request Control Mode (0=HMI, 1=Auto)",
                "default_label": f"{nice_name} Cmd Mode",
                "short_name": "Cmd Mode"
            }

            # Setpoints (auto_controllable)
            tags[f"{base_tag}.sp_requested_voltage"] = {
                "source": "service_spellman", "datatype": "REAL", "writable": True,
                "auto_controllable": True, "min_val": min_kv, "max_val": max_kv,
                "unit": "kV", "default_scale": "linear", "multiplier": 1.0,
                "description": f"{nice_name} Requested Voltage Command", "default_label": f"{nice_name} Voltage Cmd",
                "short_name": "V_Cmd"
            }
            tags[f"{base_tag}.sp_requested_current"] = {
                "source": "service_spellman", "datatype": "REAL", "writable": True,
                "auto_controllable": True, "min_val": None, "max_val": None, # Fails safe until defined in JSON
                "unit": "µA", "default_scale": "linear", "multiplier": 1.0,
                "description": f"{nice_name} Requested Current Command", "default_label": f"{nice_name} Current Cmd",
                "short_name": "I_Cmd"
            }
            tags[f"{base_tag}.cmd_enable"] = {
                "source": "service_spellman", "datatype": "BOOL", "writable": True,
                "unit": "", "default_scale": "linear", "multiplier": 1.0,
                "description": f"{nice_name} Enable Command", "default_label": f"{nice_name} Enable Cmd",
                "short_name": "En_Cmd"
            }

    return tags

def get_magnet_tags(config_dir):
    tags = {}
    base_tag = "ion_beam.beamline.magnet"
    source = "service_magnet"

    config_path = os.path.join(config_dir, "magnet_config.json")
    try:
        with open(config_path, "r") as f:
            mag_config = json.load(f)
    except FileNotFoundError:
        mag_config = {}

    max_i = mag_config.get("max_current")
    min_i = 0.0 if max_i is not None else None

    # Readbacks
    tags[f"{base_tag}.rb_voltage"] = {
        "source": source, "datatype": "REAL", "writable": False, "unit": "V", "default_scale": "linear",
        "multiplier": 1.0, "description": "Magnet Voltage Readback", "default_label": "Magnet Voltage",
        "short_name": "Voltage"
    }
    tags[f"{base_tag}.rb_current"] = {
        "source": source, "datatype": "REAL", "writable": False, "unit": "A", "default_scale": "linear",
        "multiplier": 1.0, "description": "Magnet Current Readback", "default_label": "Magnet Current",
        "short_name": "Current"
    }
    tags[f"{base_tag}.rb_resistance"] = {
        "source": source, "datatype": "REAL", "writable": False, "unit": "Ω", "default_scale": "linear",
        "multiplier": 1.0, "description": "Magnet Coil Resistance", "default_label": "Magnet Resistance",
        "short_name": "Resistance"
    }
    tags[f"{base_tag}.stat_enabled"] = {
        "source": source, "datatype": "BOOL", "writable": False, "unit": "", "default_scale": "linear",
        "multiplier": 1.0, "description": "Magnet Output Status", "default_label": "Magnet Enabled",
        "short_name": "Enabled"
    }
    tags[f"{base_tag}.stat_degaussing"] = {
        "source": source, "datatype": "BOOL", "writable": False, "unit": "", "default_scale": "linear",
        "multiplier": 1.0, "description": "Magnet Degaussing Active", "default_label": "Degaussing Active",
        "short_name": "Degaussing"
    }
    tags[f"{base_tag}.sp_actual_voltage"] = {
        "source": source, "datatype": "REAL", "writable": False, "unit": "V", "default_scale": "linear",
        "multiplier": 1.0, "description": "Magnet Voltage Setpoint Cache", "default_label": "Magnet Voltage SP (Act)",
        "short_name": "V_SP Act"
    }
    tags[f"{base_tag}.sp_actual_current"] = {
        "source": source, "datatype": "REAL", "writable": False, "unit": "A", "default_scale": "linear",
        "multiplier": 1.0, "description": "Magnet Current Setpoint Cache", "default_label": "Magnet Current SP (Act)",
        "short_name": "I_SP Act"
    }

    # Control Mode Arbitration
    tags[f"{base_tag}.rb_ctrl_mode"] = {
        "source": source, "datatype": "INT", "writable": False, "unit": "", "default_scale": "linear",
        "multiplier": 1.0, "description": "Active Control Mode", "default_label": "Ctrl Mode", "short_name": "Mode"
    }
    tags[f"{base_tag}.cmd_ctrl_mode"] = {
        "source": source, "datatype": "INT", "writable": True, "unit": "", "default_scale": "linear",
        "multiplier": 1.0, "description": "Request Control Mode (0=HMI, 1=Auto)", "default_label": "Cmd Mode",
        "short_name": "Cmd Mode"
    }

    # Setpoints (auto_controllable)
    tags[f"{base_tag}.sp_requested_current"] = {
        "source": source, "datatype": "REAL", "writable": True,
        "auto_controllable": True, "min_val": min_i, "max_val": max_i,
        "unit": "A", "default_scale": "linear", "multiplier": 1.0,
        "description": "Magnet Requested Current Command", "default_label": "Magnet Current Cmd", "short_name": "I_Cmd"
    }
    tags[f"{base_tag}.cmd_enable"] = {
        "source": source, "datatype": "BOOL", "writable": True, "unit": "", "default_scale": "linear",
        "multiplier": 1.0, "description": "Magnet Enable Command", "default_label": "Magnet Enable Cmd",
        "short_name": "En_Cmd"
    }
    tags[f"{base_tag}.cmd_degauss"] = {
        "source": source, "datatype": "BOOL", "writable": True, "unit": "", "default_scale": "linear",
        "multiplier": 1.0, "description": "Magnet Degauss Command", "default_label": "Degauss Cmd",
        "short_name": "Deg_Cmd"
    }

    return tags

def get_smu_tags(config_dir):
    tags = {}
    base_tag = "ion_beam.beamline.faraday.smu"
    source = "service_faraday_smu"

    config_path = os.path.join(config_dir, "smu_config.json")
    try:
        with open(config_path, "r") as f:
            smu_config = json.load(f)
    except FileNotFoundError:
        smu_config = {}

    min_v = smu_config.get("min_v")
    max_v = smu_config.get("max_v")

    tags[f"{base_tag}.stat_comms_fail"] = {
        "source": source, "datatype": "BOOL", "writable": False, "unit": "", "default_scale": "linear",
        "multiplier": 1.0, "description": "Faraday SMU Comms Failure", "default_label": "SMU Comms Fail",
        "short_name": "Comms"
    }
    tags[f"{base_tag}.rb_voltage"] = {
        "source": source, "datatype": "REAL", "writable": False, "unit": "V", "default_scale": "linear",
        "multiplier": 1.0, "description": "Faraday SMU Voltage Readback", "default_label": "Faraday Bias Voltage",
        "short_name": "Voltage"
    }
    tags[f"{base_tag}.rb_current"] = {
        "source": source, "datatype": "REAL", "writable": False, "unit": "A", "default_scale": "linear",
        "multiplier": 1.0, "description": "Faraday SMU Beam Current Readback", "default_label": "Faraday Beam Current",
        "short_name": "Current"
    }
    tags[f"{base_tag}.stat_enabled"] = {
        "source": source, "datatype": "BOOL", "writable": False, "unit": "", "default_scale": "linear",
        "multiplier": 1.0, "description": "Faraday SMU Output Status", "default_label": "Faraday Bias Enabled",
        "short_name": "Enabled"
    }
    tags[f"{base_tag}.sp_actual_voltage"] = {
        "source": source, "datatype": "REAL", "writable": False, "unit": "V", "default_scale": "linear",
        "multiplier": 1.0, "description": "Faraday SMU Voltage Setpoint Cache",
        "default_label": "Faraday Bias SP (Act)", "short_name": "V_SP Act"
    }

    # Control Mode Arbitration
    tags[f"{base_tag}.rb_ctrl_mode"] = {
        "source": source, "datatype": "INT", "writable": False, "unit": "", "default_scale": "linear",
        "multiplier": 1.0, "description": "Active Control Mode", "default_label": "Ctrl Mode", "short_name": "Mode"
    }
    tags[f"{base_tag}.cmd_ctrl_mode"] = {
        "source": source, "datatype": "INT", "writable": True, "unit": "", "default_scale": "linear",
        "multiplier": 1.0, "description": "Request Control Mode (0=HMI, 1=Auto)", "default_label": "Cmd Mode",
        "short_name": "Cmd Mode"
    }

    # Setpoints (auto_controllable)
    tags[f"{base_tag}.sp_requested_voltage"] = {
        "source": source, "datatype": "REAL", "writable": True,
        "auto_controllable": True, "min_val": min_v, "max_val": max_v,
        "unit": "V", "default_scale": "linear", "multiplier": 1.0,
        "description": "Faraday SMU Requested Voltage Command", "default_label": "Faraday Bias Cmd",
        "short_name": "V_Cmd"
    }
    tags[f"{base_tag}.cmd_enable"] = {
        "source": source, "datatype": "BOOL", "writable": True, "unit": "", "default_scale": "linear",
        "multiplier": 1.0, "description": "Faraday SMU Enable Command", "default_label": "Faraday Enable Cmd",
        "short_name": "En_Cmd"
    }

    return tags

# --- SCL Parser specific to Siemens S7 Memory Alignment (Nested STRUCTs) ---
def get_plc_tags_from_scl(scl_path, config_dir, db_number=10, machine_root="ion_beam"):
    plc_config_path = os.path.join(config_dir, "plc_config.json")
    try:
        with open(plc_config_path, "r") as f:
            plc_hw_limits = json.load(f)
    except FileNotFoundError:
        plc_hw_limits = {}

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
            align_to_word()
            path_stack.append(struct_open.group(1))
            continue

        if re_struct_close.match(line):
            align_to_word()
            if path_stack:
                path_stack.pop()
            continue

        var_match = re_var.match(line)
        is_udt = False

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
                lower_dt = dt.lower()
                if lower_dt in ['int', 'word']:
                    db_byte_offset += 2
                elif lower_dt in ['real', 'dint', 'dword'] or dt.startswith('UDT_Fault_Word'):
                    db_byte_offset += 4
                elif lower_dt in ['byte', 'sint']:
                    db_byte_offset += 1

            writable = False
            filtered_stack = []

            for layer in path_stack:
                if layer.lower() == "from_pc":
                    writable = True
                elif layer.lower() == "to_pc":
                    writable = False
                else:
                    filtered_stack.append(layer.lower().replace('_', '.', 1))

            subsystem_path = ".".join(filtered_stack)

            if is_udt and dt.lower().startswith('udt_fault_word'):
                fault_suffix = dt.lower().replace("udt_fault_", "")
                tag_name = f"{machine_root}.{subsystem_path}.{fault_suffix}"
            else:
                tag_name = f"{machine_root}.{subsystem_path}.{name.lower()}"

            tag_name = tag_name.replace('..', '.')

            meta = {}
            if "|" in comment:
                parts = comment.split('|')
                meta['desc'] = parts[0].strip()  # The first part is the description
                for p in parts[1:]:
                    if "=" in p:
                        k, v = p.split('=', 1)
                        meta[k.strip().lower()] = v.strip()
            else:
                meta['desc'] = comment if comment else name.replace('_', ' ')

            # --- ADDED: Auto-tag auto_controllable and default limits ---
            is_auto_controllable = name.lower().startswith("sp_requested_")
            default_min = None
            default_max = None

            if is_auto_controllable:
                # Subsystem path is something like "source.extraction" or "beamline.steering"
                # We can map this to the JSON keys
                config_key = subsystem_path.replace(".", "_")  # e.g., source_extraction

                if config_key in plc_hw_limits:
                    # Match specific suffix (e.g., _x for Steering X, _v for Voltage)
                    if name.lower().endswith("_x_volts"):
                        default_min = plc_hw_limits[config_key].get("min_x")
                        default_max = plc_hw_limits[config_key].get("max_x")
                    elif name.lower().endswith("_y_volts"):
                        default_min = plc_hw_limits[config_key].get("min_y")
                        default_max = plc_hw_limits[config_key].get("max_y")
                    elif name.lower().endswith("_voltage"):
                        default_min = plc_hw_limits[config_key].get("min_v")
                        default_max = plc_hw_limits[config_key].get("max_v")
                    elif name.lower().endswith("_current"):
                        default_min = plc_hw_limits[config_key].get("min_i")
                        default_max = plc_hw_limits[config_key].get("max_i")
                    elif name.lower().endswith("_temp"):
                        default_min = plc_hw_limits[config_key].get("min_t")
                        default_max = plc_hw_limits[config_key].get("max_t")

            tag_dict = {
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

            if is_auto_controllable:
                tag_dict["auto_controllable"] = True
                tag_dict["min_val"] = default_min
                tag_dict["max_val"] = default_max

            tags[tag_name] = tag_dict

    return tags


# --- Main Execution ---
def build_system_registry():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    src_dir = os.path.dirname(current_dir)
    project_root = os.path.dirname(src_dir)

    config_dir = os.path.join(project_root, "config")
    registry_path = os.path.join(config_dir, "system_tags.json")

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
    magnet_tags = get_magnet_tags(config_dir)
    smu_tags = get_smu_tags(config_dir)

    scl_path = os.path.join(project_root, "plc", "generated", "PLC_PC_Interface.scl")
    plc_tags = get_plc_tags_from_scl(scl_path, config_dir, db_number=10)

    new_registry.update(vacuum_tags)
    new_registry.update(turbo_tags)
    new_registry.update(plc_tags)
    new_registry.update(spellman_tags)
    new_registry.update(magnet_tags)
    new_registry.update(smu_tags)

    new_registry = apply_existing_overrides(new_registry, existing_registry)

    with open(registry_path, "w") as f:
        sorted_registry = {k: new_registry[k] for k in sorted(new_registry.keys())}
        json.dump(sorted_registry, f, indent=4)

    print(f"Registry successfully built with {len(sorted_registry)} tags.")


if __name__ == "__main__":
    build_system_registry()