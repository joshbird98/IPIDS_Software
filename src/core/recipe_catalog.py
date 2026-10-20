"""What the recipe builder, the recipe checks and the recipes' plain-language text know about the
machine, from the tag registry and fault_config.json:

    catalog = Catalog.load()
    catalog.tag("ion_beam.source.extraction.sp_requested_voltage")
        -> Tag: label "Extraction voltage setpoint", unit kV, limits 0..20, settable
    catalog.settable()      tags a recipe may set (commands and setpoints), by name
    catalog.readable()      tags a recipe may wait on or check
    catalog.supplies        the outputs a "switch on and set" step can drive (Supply)
    catalog.faults          the PLC faults a recipe can stop on (Fault)

No Qt here: the sequencer uses it too, to check a recipe before it runs.
"""
import json
import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from src.core import paths

log = logging.getLogger("recipe_catalog")

FAULT_RESET = "ion_beam.system.cmd_fault_reset"
SAFETY_RELAY = "ion_beam.facilities.safety_relay_active"
SAFETY_ENABLE = "ion_beam.system.cmd_enable_safety"
TURBO_ENABLE = "ion_beam.source.turbo_pump.cmd_enable"
CESIUM_HEATER = "ion_beam.source.cesium.cmd_ctrl_mode"     # 1 heater on, 0 off (the panel's HEATER ON)

# What the panels call each part of the machine
SUBSYSTEM_NAMES = {
    "ion_beam.source.extraction": "extraction",
    "ion_beam.source.target": "target (cathode)",
    "ion_beam.source.filament": "filament",
    "ion_beam.source.thermionic": "thermionic (ioniser)",
    "ion_beam.source.einzel": "source einzel",
    "ion_beam.source.cesium": "caesium oven",
    "ion_beam.source.chamber": "source",
    "ion_beam.source.turbo_pump": "source turbo pump",
    "ion_beam.source.vacuum_gauge_1": "source pressure (VG1)",
    "ion_beam.beamline.vacuum_gauge_2": "beamline pre-magnet pressure (VG2)",
    "ion_beam.beamline.vacuum_gauge_3": "beamline pressure (VG3)",
    "ion_beam.endstation.vacuum_gauge_4": "endstation 1 pressure (VG4)",
    "ion_beam.loadlock.vacuum_gauge_5": "loadlock pressure (VG5)",
    "ion_beam.endstation.vacuum_gauge_6": "endstation 2 pressure (VG6)",
    "ion_beam.endstation": "endstation",
    "ion_beam.loadlock.turbo_pump": "loadlock turbo pump",
    "ion_beam.beamline.magnet": "analyser magnet",
    "ion_beam.beamline.steerer_magnet": "X-steerer magnet",
    "ion_beam.beamline.steering": "electrostatic steering",
    "ion_beam.beamline.diagnostics": "Faraday cup",
    "ion_beam.beamline.faraday.smu": "Faraday cup SMU",
    "ion_beam.beamline.faraday": "Faraday cup",
    "ion_beam.beamline.image_slits": "image slits",
    "ion_beam.beamline.object_slits": "object slits",
    "ion_beam.beamline.straight_through": "straight-through",
    "ion_beam.beamline.einzel": "beamline einzel",
    "ion_beam.beamline.neutral_trap_pos": "positive neutral trap",
    "ion_beam.beamline.neutral_trap_neg": "negative neutral trap",
    "ion_beam.facilities": "",
    "ion_beam.facilities.graphix1": "gauge controller 1",
    "ion_beam.facilities.graphix2": "gauge controller 2",
    "ion_beam.facilities.vacuum": "vacuum service",
    "ion_beam.system.facilities.adam": "ADAM aux",
    "ion_beam.internal": "PLC internal",
    "ion_beam.system": "",
    "ion_beam.vacuum": "",
}
# ...and each kind of tag. A pressure gauge's 'pressure' is in its name already.
LEAF_NAMES = {
    "sp_requested_voltage": "voltage setpoint", "sp_requested_current": "current setpoint",
    "sp_requested_temp": "temperature setpoint", "sp_requested_x_volts": "X voltage setpoint",
    "sp_requested_y_volts": "Y voltage setpoint",
    "sp_actual_voltage": "voltage setpoint in use", "sp_actual_current": "current setpoint in use",
    "sp_actual_temp": "temperature setpoint in use", "sp_actual_x_volts": "X voltage setpoint in use",
    "sp_actual_y_volts": "Y voltage setpoint in use",
    "rb_voltage": "voltage", "rb_current": "current", "rb_power": "power", "rb_temp": "temperature",
    "rb_pressure": "", "rb_speed_hz": "speed", "rb_speed_pct": "speed (%)", "rb_resistance": "resistance",
    "rb_body_temp": "body temperature", "rb_temp_bearing": "bearing temperature",
    "rb_temp_converter": "converter temperature", "estimated_temp": "temperature (PLC estimate)",
    "cmd_enable": "output", "stat_enabled": "output on", "cmd_insert_fc": "insert",
    "stat_fc_inserted": "inserted", "stat_fc_retracted": "retracted", "cmd_degauss": "degauss",
    "stat_degaussing": "degaussing", "cmd_open_gv": "gate valve open request", "stat_gv_open": "gate valve open",
    "stat_gv_closed": "gate valve closed", "stat_gv_open_permissive": "gate valve allowed to open",
    "stat_gv_close_permissive": "gate valve allowed to close", "stat_vac_ok_for_gv": "vacuum good for the gate "
    "valve (PLC)", "cmd_auto_emission": "auto-emission",
    "stat_auto_emission": "auto-emission on", "cmd_force_cooling": "forced cooling",
    "stat_cooling_active": "cooling air on", "stat_cooling_wait": "cooling wait",
    "cmd_ctrl_mode": "control mode request", "ctrl_mode": "control mode", "rb_ctrl_mode": "control mode",
    "safety_relay_active": "safety relay on", "stat_air_press_ok": "compressed air OK",
    "stat_src_coolant_ok": "source coolant OK", "stat_mag_coolant_ok": "magnet coolant OK",
    "cmd_enable_safety": "safety relay enable request", "cmd_source_shutdown": "source shutdown request",
    "cmd_cancel_shutdown": "source shutdown cancel request", "cmd_fault_reset": "fault reset",
    "stat_ready": "at speed", "stat_turning": "turning", "stat_trip": "tripped", "stat_lockout": "locked out",
    "stat_comms_fail": "communication lost", "stat_arc_exceeded": "arc limit exceeded",
    "stat_overcurrent": "over-current", "stat_overvoltage": "over-voltage",
    "cmd_range": "current monitor range", "rb_range_idx": "current monitor range in use",
    "rb_op_mode": "current monitor mode", "stat_overrange": "over range", "stat_ranging": "changing range",
    "stat_psu_ovc": "PSU over-current", "stat_psu_ovp": "PSU over-voltage", "stat_psu_overtemp": "PSU over-temperature",
    "stat_psu_powerfail": "PSU power fail", "stat_unexpected_res": "unexpected resistance",
    "stat_above_sp": "above its set point", "stat_error_code": "error code", "stat_rapid_rise": "rapid rise",
    "stat_mismatch": "relay set point mismatch", "stat_not_found": "not found",
    "out_cv_heating": "heater output", "out_cv_current": "current controller output", "rb_dose_count": "dose count",
    "machine_state": "machine state", "pc_plc_comms_lost": "PC link lost (watchdog)",
    "src_auto_sdown": "automatic shutdown running", "pc_shutdown_step": "PC shutdown step",
    "remote_shutdown_step": "remote shutdown step", "remote_io_lost": "remote I/O lost",
    "plc_cycle_count": "PLC cycle count", "plc_watchdog": "PC watchdog count",
    "safety_timer_elapsed_ms": "safety relay timer (ms)", "safety_timer_total_ms": "safety relay timer total (ms)",
}
# Whole names that read better said differently
TAG_NAMES = {
    SAFETY_RELAY: "safety relay on",
    CESIUM_HEATER: "caesium oven heater",
    "ion_beam.source.cesium.ctrl_mode": "caesium oven heater mode",
    "ion_beam.vacuum.gv_permissive_ready": "vacuum good for the gate valve",
    "ion_beam.internal.cs_is_hot": "caesium hot (the PLC's flag)",
    "ion_beam.internal.fil_is_hot": "filament hot (the PLC's flag)",
}
# Units as the help and the panels write them
UNIT_NAMES = {"mB": "mbar"}
# Whole-number commands that are really on/off switches (1 on, 0 off)
SWITCHES = {CESIUM_HEATER}
_CHOICE = re.compile(r"(-?\d+)\s*=\s*([^,()]+)")

# Never set by a recipe, whatever the registry says
NOT_SETTABLE = {
    FAULT_RESET: "recipes may not reset faults: press RESET FAULTS yourself",
    "ion_beam.system.plc_watchdog": "the PC watchdog is the PLC service's own",
}
# Commands the services take that aren't in the registry
EXTRA_SETTABLE = {
    TURBO_ENABLE: {"datatype": "BOOL", "unit": "", "label": "source turbo pump start"},
    "ion_beam.source.turbo_pump.cmd_reset": {"datatype": "BOOL", "unit": "", "label": "source turbo pump fault reset"},
}
# Values a recipe may wait on that aren't in the registry
EXTRA_READABLE = {
    "ion_beam.vacuum.gv_permissive_ready": {"datatype": "BOOL", "unit": "", "label": "vacuum good for the gate valve"},
}


@dataclass(frozen=True)
class Tag:
    name: str
    label: str              # "extraction voltage setpoint"
    unit: str
    datatype: str           # BOOL, REAL, INT, DINT ... ("" if unknown)
    min_val: Optional[float]
    max_val: Optional[float]
    settable: bool          # a recipe may set it
    not_settable: str       # why not, if not
    in_registry: bool
    description: str = ""   # the registry's
    choices: tuple = ()     # ((value, meaning), ...) for a whole number that codes something

    @property
    def is_bool(self) -> bool:
        return self.datatype == "BOOL"

    @property
    def is_switch(self) -> bool:
        """On or off: a BOOL, or a whole-number command used as one (the caesium heater)."""
        return self.is_bool or self.name in SWITCHES

    @property
    def is_number(self) -> bool:
        return self.datatype not in ("BOOL", "STRING", "")

    @property
    def is_whole(self) -> bool:
        return self.datatype in ("INT", "DINT", "UINT", "WORD", "DWORD", "SINT", "USINT", "LINT")

    def choice(self, value) -> str:
        """What a coded value means ('' if nothing says)."""
        for v, meaning in self.choices:
            try:
                if float(value) == v:
                    return meaning
            except (TypeError, ValueError):
                return ""
        return ""

    @property
    def subsystem(self) -> str:
        return self.name.rpartition(".")[0]

    @property
    def leaf(self) -> str:
        return self.name.rpartition(".")[2]

    def range_text(self) -> str:
        if self.min_val is None or self.max_val is None:
            return ""
        u = f" {self.unit}" if self.unit else ""
        return f"{self.min_val:g} to {self.max_val:g}{u}"


@dataclass(frozen=True)
class Supply:
    """An output a 'switch on and set' step drives: psu_control's four tags, and its setpoint's range."""
    key: str                # the subsystem, e.g. ion_beam.source.extraction
    name: str               # "Extraction"
    enable_tag: str
    enabled_tag: str
    setpoint_tag: str
    readback_tag: str
    unit: str
    min_val: Optional[float]
    max_val: Optional[float]
    wait_by_default: bool   # the thermionic current follows the filament, not its setpoint
    tolerance: float        # a sensible readback tolerance, in the unit
    timeout_s: float        # a sensible time for the readback to get there


@dataclass(frozen=True)
class Fault:
    name: str
    description: str
    word_tag: str
    bit_str: str

    def policy_entry(self) -> dict:
        return {"type": "BIT_FAULT", "registry_tag": self.word_tag, "bit_str": self.bit_str, "name": self.name,
                "action": "ABORT"}


# (subsystem, name, setpoint leaf, readback leaf, wait for the readback, tolerance, timeout s)
SUPPLY_DEFS = (
    ("ion_beam.source.extraction", "Extraction", "sp_requested_voltage", "rb_voltage", True, 0.2, 60),
    ("ion_beam.source.target", "Target (cathode)", "sp_requested_voltage", "rb_voltage", True, 0.2, 60),
    ("ion_beam.source.einzel", "Source einzel", "sp_requested_voltage", "rb_voltage", True, 0.2, 60),
    ("ion_beam.source.filament", "Filament", "sp_requested_current", "rb_current", True, 0.5, 90),
    ("ion_beam.source.thermionic", "Thermionic (ioniser)", "sp_requested_current", "rb_current", False, 5.0, 60),
    ("ion_beam.beamline.magnet", "Analyser magnet", "sp_requested_current", "rb_current", True, 0.05, 60),
    ("ion_beam.beamline.steerer_magnet", "X-steerer magnet", "sp_requested_current", "rb_current", True, 0.05, 60),
    ("ion_beam.beamline.faraday.smu", "Faraday cup bias (SMU)", "sp_requested_voltage", "rb_voltage", True, 1.0, 20),
)
# Recommended stop conditions for a recipe that switches the source or the magnets on
RECOMMENDED_FAULTS = ("Safety_Relay_Tripped",)


def _words(leaf: str) -> str:
    for prefix in ("cmd_", "stat_", "sp_requested_", "sp_actual_", "sp_", "rb_"):
        if leaf.startswith(prefix):
            leaf = leaf[len(prefix):]
            break
    return leaf.replace("_", " ")


def subsystem_name(base: str) -> str:
    """'extraction', 'image slits left', 'ADAM aux ch6' ... for a tag's base (all of its name but
    the last part)."""
    sub = SUBSYSTEM_NAMES.get(base)
    if sub is None:
        sub = SUBSYSTEM_NAMES.get(base.rpartition(".")[0])
        if sub is not None:
            sub = f"{sub} {base.rpartition('.')[2].replace('_', ' ')}".strip()
        else:
            sub = base.rpartition(".")[2].replace("_", " ")
    return sub


def tag_label(name: str) -> str:
    """'extraction voltage setpoint', 'source pressure (VG1)', 'safety relay on' ..."""
    if name in TAG_NAMES:
        return TAG_NAMES[name]
    base, _, leaf = name.rpartition(".")
    sub = subsystem_name(base)
    relay = re.fullmatch(r"relay_(\d+)_(on|off)", leaf)
    what = f"relay {relay.group(1)} {relay.group(2)} point" if relay else LEAF_NAMES.get(leaf, _words(leaf))
    return f"{sub} {what}".strip()


def _choices(meta: dict) -> tuple:
    """((0, 'Auto'), (1, '150mV') ...) from a description such as 'Set Range (0=Auto, 1=150mV ...)'."""
    if meta.get("datatype") not in ("INT", "DINT", "UINT", "WORD", "SINT", "USINT"):
        return ()
    found = _CHOICE.findall(str(meta.get("description") or ""))
    return tuple((float(v), text.strip()) for v, text in found) if len(found) >= 2 else ()


def _settability(name: str, meta: dict) -> tuple:
    leaf = name.rpartition(".")[2]
    if name in NOT_SETTABLE:
        return False, NOT_SETTABLE[name]
    if leaf.startswith("testing_cmd_"):
        return False, "a test command, not for recipes"
    if leaf == "cmd_ctrl_mode" and not name.startswith("ion_beam.source.cesium."):
        return False, "the PLC's control modes aren't set by recipes (control leases replaced them)"
    if not (leaf.startswith("cmd_") or leaf.startswith("sp_requested_")):
        return False, "a reading or a status, not a command"
    if not meta.get("writable"):
        return False, "not writable"
    return True, ""


class Catalog:
    def __init__(self, registry: dict, fault_config: dict = None):
        self.registry = registry or {}
        self._tags: Dict[str, Tag] = {}
        for name, meta in self.registry.items():
            if not isinstance(meta, dict):
                continue
            ok, why = _settability(name, meta)
            unit = meta.get("unit") or ""
            self._tags[name] = Tag(name, tag_label(name), UNIT_NAMES.get(unit, unit), meta.get("datatype") or "",
                                   _num(meta.get("min_val")), _num(meta.get("max_val")), ok, why, True,
                                   str(meta.get("description") or "").strip(), _choices(meta))
        for name, extra in EXTRA_SETTABLE.items():
            self._tags.setdefault(name, Tag(name, extra["label"], extra["unit"], extra["datatype"], None, None,
                                            True, "", False))
        for name, extra in EXTRA_READABLE.items():
            self._tags.setdefault(name, Tag(name, extra["label"], extra["unit"], extra["datatype"], None, None,
                                            False, "a reading, not a command", False))
        self.supplies: List[Supply] = []
        for key, name, sp, rb, wait, tol, timeout in SUPPLY_DEFS:
            sp_tag = self._tags.get(f"{key}.{sp}")
            if sp_tag is None or f"{key}.cmd_enable" not in self._tags:
                continue
            self.supplies.append(Supply(key, name, f"{key}.cmd_enable", f"{key}.stat_enabled", sp_tag.name,
                                        f"{key}.{rb}", sp_tag.unit, sp_tag.min_val, sp_tag.max_val, wait, tol,
                                        timeout))
        self.faults: List[Fault] = []
        for word in (fault_config or {}).values():
            if not isinstance(word, dict):
                continue
            for bit_str, info in (word.get("bits") or {}).items():
                if isinstance(info, dict) and info.get("name"):
                    self.faults.append(Fault(info["name"], info.get("description", ""), word.get("registry_tag", ""),
                                             bit_str))
        self.faults.sort(key=lambda f: f.name)

    @classmethod
    def load(cls, registry_path: str = None, fault_path: str = None) -> "Catalog":
        """From the tag registry and fault_config.json (an empty catalogue for a file that can't be
        read: the tag checks are then left out, see src/core/recipe_check.py)."""
        return cls(read_json(registry_path or paths.REGISTRY_PATH),
                   read_json(fault_path or paths.config_path("fault_config.json")))

    @classmethod
    def for_registry(cls, registry: dict, fault_path: str = None) -> "Catalog":
        """For a service that has read the registry already (the sequencer)."""
        return cls(registry, read_json(fault_path or paths.config_path("fault_config.json")) if registry else {})

    # ---------------------------------------------------------------- tags
    def tag(self, name) -> Optional[Tag]:
        return self._tags.get(name) if isinstance(name, str) else None

    def settable(self) -> List[Tag]:
        return sorted((t for t in self._tags.values() if t.settable), key=lambda t: t.label)

    def readable(self) -> List[Tag]:
        """Everything a recipe can wait on or check: every tag but the raw fault words and the
        commands' own echoes are useful, so all of them."""
        return sorted((t for t in self._tags.values() if not t.name.startswith("ion_beam.faults.word_")),
                      key=lambda t: t.label)

    def label(self, name: str) -> str:
        t = self.tag(name)
        return t.label if t is not None else (name or "?")

    def unit(self, name: str) -> str:
        t = self.tag(name)
        return t.unit if t is not None else ""

    # ---------------------------------------------------------------- supplies and faults
    def supply(self, key_or_tag: str) -> Optional[Supply]:
        """The supply a subsystem or any of its four tags belongs to."""
        for s in self.supplies:
            if key_or_tag in (s.key, s.enable_tag, s.enabled_tag, s.setpoint_tag, s.readback_tag):
                return s
        return None

    def fault(self, name: str = None, word_tag: str = None, bit_str: str = None) -> Optional[Fault]:
        for f in self.faults:
            if (name and f.name == name) or (word_tag and f.word_tag == word_tag and f.bit_str == bit_str):
                return f
        return None


def _num(v) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def read_json(path: str) -> dict:
    """A JSON object from a file, or {} (logged) if it can't be read."""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as e:
        log.error("Recipe catalogue: %s not readable: %s", path, e)
        return {}


_shared = None


def shared() -> Catalog:
    """One catalogue per process, loaded on first use."""
    global _shared
    if _shared is None:
        _shared = Catalog.load()
    return _shared
