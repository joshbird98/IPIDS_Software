"""Simulated Leybold GRAPHIX gauge controllers (nodes 10 and 20, three gauges each).

Answers the same (node, parameter group, parameter number) reads as the real
controllers with plausible pumped-down pressures that wander slightly, relay
set points taken from vac_gauges_config.json (so the service's start-up
'enforce config' step finds everything already matching), and accepts writes.

Injected faults (IPIDS_SIM_FAULTS): vacuum_disconnect, vacuum_vent_source (VG1 rises to atmosphere).
"""
import json
import math
import random
import threading
import time

from src.core import paths
from src.sim import injected_faults

_BASE_PRESSURES = {(10, 1): 2.1e-7, (10, 2): 3.4e-7, (10, 3): 5.2e-7,
                   (20, 1): 4.0e-7, (20, 2): 8.0e-2, (20, 3): 3.0e-7}
_RELAY_CH, _RELAY_ON, _RELAY_OFF, _RELAY_STATUS = {}, {}, {}, {}
for _r, (_c, _on, _off, _st) in {1: (1, 2, 3, 4), 2: (5, 6, 7, 8), 3: (9, 10, 11, 12), 4: (13, 14, 15, 16),
                                  5: (17, 18, 19, 20), 6: (21, 22, 23, 24)}.items():
    _RELAY_CH[str(_c)], _RELAY_ON[str(_on)], _RELAY_OFF[str(_off)], _RELAY_STATUS[str(_st)] = _r, _r, _r, _r


class FakeGraphix:
    def __init__(self):
        self.lock = threading.Lock()
        self.t0 = time.monotonic()
        self.names = {}
        self.relays = {}   # (node, relay) -> {"ch": int, "on": float, "off": float}
        try:
            with open(paths.config_path("vac_gauges_config.json"), "r") as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            cfg = {}
        for node_str, node in cfg.items():
            if not node_str.isdigit():
                continue
            n = int(node_str)
            for ch, ch_cfg in node.get("channels", {}).items():
                self.names[(n, int(ch))] = str(ch_cfg.get("name", ""))[:10].strip()
            for r, r_cfg in node.get("relays", {}).items():
                self.relays[(n, int(r))] = {"ch": int(r_cfg.get("channel", 1)), "on": float(r_cfg.get("on_val", 1e-5)),
                                            "off": float(r_cfg.get("off_val", 2e-5))}

    def pressure(self, node, ch):
        base = _BASE_PRESSURES.get((node, ch), 1e-6)
        t = time.monotonic() - self.t0
        if (node, ch) == (10, 1) and "vacuum_vent_source" in injected_faults():
            return min(1000.0, base * math.exp(t / 5.0))
        drift = 1.0 + 0.08 * math.sin(t / 37.0 + node + ch)
        return base * drift * (1.0 + random.gauss(0, 0.01))

    def read(self, node, group, param):
        if "vacuum_disconnect" in injected_faults():
            return None
        with self.lock:
            if group in ("1", "2", "3"):
                ch = int(group)
                if param == "29":
                    return f"{self.pressure(node, ch):.2E}"
                if param == "24":
                    return "0"
                if param == "5":
                    return self.names.get((node, ch), f"VG{ch}")
                if param == "4":
                    return "PTR90N"
            if group == "5" and param == "2":
                return f"SIM-{node}"
            if group == "4":
                if param in _RELAY_STATUS:
                    r = self.relays.get((node, _RELAY_STATUS[param]))
                    if not r:
                        return "0"
                    return "1" if self.pressure(node, r["ch"]) < r["on"] else "0"
                for table, key in ((_RELAY_CH, "ch"), (_RELAY_ON, "on"), (_RELAY_OFF, "off")):
                    if param in table:
                        r = self.relays.setdefault((node, table[param]), {"ch": 1, "on": 1e-5, "off": 2e-5})
                        return str(r["ch"]) if key == "ch" else f"{r[key]:.1E}"
            return None

    def write(self, node, group, param, value):
        if "vacuum_disconnect" in injected_faults():
            return "ERROR"
        with self.lock:
            try:
                if group in ("1", "2", "3") and param == "5":
                    self.names[(node, int(group))] = value.strip()
                elif group == "4":
                    for table, key in ((_RELAY_CH, "ch"), (_RELAY_ON, "on"), (_RELAY_OFF, "off")):
                        if param in table:
                            r = self.relays.setdefault((node, table[param]), {"ch": 1, "on": 1e-5, "off": 2e-5})
                            r[key] = int(float(value)) if key == "ch" else float(value)
            except ValueError:
                return "NACK"
        return "ACK"


_GRAPHIX = None


def shared_graphix() -> FakeGraphix:
    global _GRAPHIX
    if _GRAPHIX is None:
        _GRAPHIX = FakeGraphix()
    return _GRAPHIX
