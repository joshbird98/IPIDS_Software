import json
import os


class DynamicPayloadMapper:
    def __init__(self):
        self.vacuum_map = {}
        self.turbo_map = {}
        self.valid_keys = set()

        self.STATUS_MAP = {
            "OK": 0.0, "NO-SEN": 1.0, "RANGE?": 2.0, "S-OFF": 3.0,
            "ERROR-H": 4.0, "ERROR-L": 5.0, "ERROR-S": 6.0
        }

        self._build_maps()

    def _build_maps(self):
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(current_dir)
        registry_path = os.path.join(project_root, "system_tags.json")

        try:
            with open(registry_path, "r") as f:
                registry = json.load(f)
        except Exception as e:
            print(f"[Mapper Error] Cannot load registry: {e}")
            return

        self.valid_keys = set(registry.keys())
        turbo_base = None

        for full_tag, meta in registry.items():
            source = meta.get("source")

            # 1. Map Vacuum by Hardware Node/Channel
            if source == "service_vacuum":
                node = str(meta.get("hw_node"))
                ch = str(meta.get("hw_channel"))
                param = full_tag.rsplit('.', 1)[-1]  # "pressure" or "status"
                self.vacuum_map[(node, ch, param)] = full_tag

            # 2. Discover Turbo Base Path dynamically
            elif source == "service_source_turbo" and not turbo_base:
                turbo_base = full_tag.rsplit('.', 1)[0]

        # 3. Build Turbo Structure Map
        if turbo_base:
            self.turbo_map = {
                ("hz",): f"{turbo_base}.speed_hz",
                ("pct",): f"{turbo_base}.speed_pct",
                ("temps", "bearing"): f"{turbo_base}.temp_bearing",
                ("temps", "converter"): f"{turbo_base}.temp_converter",
                ("electrical", "volts"): f"{turbo_base}.voltage",
                ("electrical", "amps"): f"{turbo_base}.current",
                ("status", "turning"): f"{turbo_base}.status_turning",
                ("status", "ready"): f"{turbo_base}.status_ready",
                ("status", "error_active"): f"{turbo_base}.status_error",
            }

    def parse(self, data: dict) -> dict:
        """Parses a raw ZMQ JSON payload and returns a flat dictionary of valid registry keys."""
        flat_data = {}

        # 1. Parse Vacuum Payloads
        for node, n_data in data.items():
            if isinstance(n_data, dict) and "channels" in n_data:
                for ch, ch_data in n_data["channels"].items():
                    for param in ["pressure", "status"]:
                        if param in ch_data:
                            full_tag = self.vacuum_map.get((str(node), str(ch), param))
                            if full_tag:
                                val = ch_data[param]
                                if param == "status":
                                    val = self.STATUS_MAP.get(str(val).strip().upper(), -1.0)
                                flat_data[full_tag] = float(val)

        # 2. Parse Turbo Payloads
        if "temps" in data and "electrical" in data:
            for key_tuple, full_tag in self.turbo_map.items():
                val = data
                try:
                    for k in key_tuple:
                        val = val[k]
                    # Cast booleans to 1.0 / 0.0
                    flat_data[full_tag] = 1.0 if val is True else 0.0 if val is False else float(val)
                except (KeyError, TypeError, ValueError):
                    pass

        # 3. Parse Direct PLC/Flat Payloads
        for k, v in data.items():
            if not isinstance(v, dict) and k in self.valid_keys:
                flat_data[k] = 1.0 if v is True else 0.0 if v is False else float(v)

        return flat_data