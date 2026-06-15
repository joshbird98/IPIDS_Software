import orjson
import os
# --- Global Fault Map Parser ---

class FaultRegistry:
    def __init__(self, file_path: str):
        self.fault_structures = {}
        self._load_map(file_path)

    def _load_map(self, file_path: str):
        if not os.path.exists(file_path):
            print(f"[Fault Engine] Critical Error: {file_path} not found.")
            return
        try:
            with open(file_path, "r") as f:
                self.fault_structures = orjson.loads(f.read())
        except Exception as e:
            print(f"[Fault Engine] Failed to parse JSON: {e}")

    def get_active_faults(self, udt_key: str, word_value: int) -> list[dict]:
        active_faults = []
        udt_group = self.fault_structures.get(udt_key)
        if not udt_group or word_value == 0:
            return active_faults

        bits_definition = udt_group.get("bits", {})
        for bit_str, info in bits_definition.items():
            try:
                parts = bit_str.split('.')
                x = int(parts[0])
                y = int(parts[1])
                bit_pos = (3 - x) * 8 + y

                if bool(word_value & (1 << bit_pos)):
                    active_faults.append({
                        "name": info["name"],
                        "severity": info["severity"],
                        "description": info.get("description", "No description provided.")
                    })
            except Exception as e:
                continue
        return active_faults
