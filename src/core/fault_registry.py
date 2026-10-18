"""PLC fault words (config/fault_config.json): which faults a 32-bit fault word says are active.

Bit "x.y" is byte x, bit y of the Siemens DWORD (big-endian), i.e. bit (3 - x) * 8 + y of the
integer value.
"""
import logging

import orjson

log = logging.getLogger("fault_registry")


def bit_position(bit_str: str) -> int:
    byte_idx, bit_idx = (int(p) for p in bit_str.split("."))
    return (3 - byte_idx) * 8 + bit_idx


class FaultRegistry:
    def __init__(self, file_path: str):
        self.fault_structures = {}
        self._load_map(file_path)

    def _load_map(self, file_path: str):
        try:
            with open(file_path, "rb") as f:
                self.fault_structures = orjson.loads(f.read())
        except FileNotFoundError:
            log.error("Fault map %s not found - PLC faults will not be named", file_path)
        except (OSError, ValueError) as e:
            log.error("Fault map %s not readable: %s", file_path, e)

    def get_active_faults(self, udt_key: str, word_value: int) -> list:
        udt_group = self.fault_structures.get(udt_key)
        if not udt_group or not word_value:
            return []
        active = []
        for bit_str, info in udt_group.get("bits", {}).items():
            try:
                if word_value & (1 << bit_position(bit_str)):
                    active.append({"name": info["name"], "severity": info.get("severity", 1),
                                   "description": info.get("description", "No description provided.")})
            except (ValueError, KeyError, TypeError) as e:
                log.warning("Bad fault map entry %s / %s: %s", udt_key, bit_str, e)
        return active
