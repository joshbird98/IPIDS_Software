import json
import os

class DynamicPayloadMapper:
    def __init__(self):
        self.valid_keys = set()
        self.unknown_keys = set()
        self._load_registry()

    def _load_registry(self):
        registry_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../config/system_tags.json'))

        try:
            with open(registry_path, "r") as f:
                registry = json.load(f)
                self.valid_keys = set(registry.keys())
        except Exception as e:
            print(f"[Mapper] CRITICAL: Cannot load registry at {registry_path}: {e}")

    # --- ADDED 'topic' ARGUMENT HERE ---
    def parse(self, payload: dict, topic: str = "UNKNOWN") -> dict:
        parsed_data = {}

        for key, val in payload.items():
            if key in self.valid_keys:
                try:
                    if isinstance(val, bool):
                        parsed_data[key] = 1.0 if val else 0.0
                    else:
                        parsed_data[key] = float(val)
                except (ValueError, TypeError):
                    pass
            else:
                # Upgraded Warning with Topic and Value Type
                if key not in self.unknown_keys:
                    print(f"\n[Mapper WARNING] Topic: {topic} | Dropped Key: '{key}' | Val Type: {type(val).__name__}")
                    self.unknown_keys.add(key)

        return parsed_data