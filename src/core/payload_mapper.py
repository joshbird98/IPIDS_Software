import json
import logging
import math

log = logging.getLogger("payload_mapper")


class DynamicPayloadMapper:
    def __init__(self):
        self.valid_keys = set()
        self.unknown_keys = set()
        self._load_registry()

    def _load_registry(self):
        from src.core.paths import REGISTRY_PATH as registry_path

        try:
            with open(registry_path, "r") as f:
                registry = json.load(f)
                self.valid_keys = set(registry.keys())
        except (OSError, ValueError) as e:
            log.critical("Cannot load registry at %s - nothing will be logged: %s", registry_path, e)

    # --- ADDED 'topic' ARGUMENT HERE ---
    def parse(self, payload: dict, topic: str = "UNKNOWN") -> dict:
        parsed_data = {}

        for key, val in payload.items():
            if key in self.valid_keys:
                if val is None:
                    # Services publish null for "unknown" (device unreachable, supply unpowered):
                    # log a gap rather than holding the last value
                    parsed_data[key] = math.nan
                    continue
                try:
                    if isinstance(val, bool):
                        parsed_data[key] = 1.0 if val else 0.0
                    else:
                        parsed_data[key] = float(val)
                except (ValueError, TypeError):
                    pass
            else:
                # Upgraded Warning with Topic and Value Type
                if key not in self.unknown_keys:     # not in the registry: not logged (said once, at DEBUG)
                    log.debug("Not logged (not in the registry): %s/%s (%s)", topic, key, type(val).__name__)
                    self.unknown_keys.add(key)

        return parsed_data