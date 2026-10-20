"""The recipe builder's blocks: the kinds of step it offers, named for what they do, and the step
each one starts as. (No Qt here.)"""
import copy
from dataclasses import dataclass, field

from src.core.routines import routine_defaults

GROUPS = ("Set and switch", "Wait", "The operator", "Procedures", "Structure")


@dataclass(frozen=True)
class Block:
    key: str
    title: str              # what the palette calls it
    group: str
    help: str               # one sentence: what it does
    step: dict = field(default_factory=dict)     # what a new step starts as
    in_parallel: bool = True                     # may run inside "Do steps at the same time"

    def new_step(self) -> dict:
        return {"step_id": 0, **copy.deepcopy(self.step)}


def _routine(name: str, **params) -> dict:
    p = routine_defaults(name)
    p.update(params)
    return {"type": "ROUTINE_CALL", "routine_name": name, "parameters": p}


BLOCKS = (
    Block("set", "Set a value", "Set and switch",
          "Send one command or setpoint: a voltage, a current, a switch, the Faraday cup in or out.",
          {"type": "ACTION", "target": "", "value": None}),
    Block("set_check", "Set a value and check it", "Set and switch",
          "Send a command, then wait (up to a time limit) for a reading to show that it worked.",
          {"type": "VERIFIED_ACTION", "target": "", "value": None, "verify_target": "", "verify_condition": "==",
           "verify_value": "", "timeout_sec": 10.0}),
    Block("supply", "Switch on a supply and set it", "Set and switch",
          "Switch a supply on (if it isn't on), send its setpoint and wait for its readback to get there.",
          {"type": "ROUTINE_CALL", "routine_name": "psu_control",
           "parameters": {"target_val": 0.0, "wait_for_readback": True, "tolerance": 0.5, "timeout_sec": 30.0,
                          "requires_relay": True}}),
    Block("wait_time", "Wait a time", "Wait", "Do nothing for a set time.",
          {"type": "WAIT_TIME", "duration_sec": 10.0}),
    Block("wait_for", "Wait for a reading", "Wait",
          "Wait until a reading meets a condition, for example the source pressure below 5e-7 mbar.",
          {"type": "WAIT_TELEMETRY", "target": "", "condition": "<", "value": 0.0, "timeout_sec": 600.0}),
    Block("ask", "Ask the operator", "The operator",
          "Show the operator a message and wait for OK. OK can be refused until conditions hold.",
          {"type": "USER_PROMPT", "prompt_text": "", "verify_conditions": []}, in_parallel=True),
    Block("relay", "Bring the safety relay on", "Procedures",
          "With the operator: waits for the source vacuum, opens the gate valve, asks for START on the High "
          "Voltage Systems Control box.", _routine("safety_relay_init")),
    Block("condition", "Ramp a supply up slowly", "Procedures",
          "HV conditioning: step a supply up to a value, holding while the pressure is high and backing off on "
          "arcs.", _routine("hv_conditioning", max_val=0.0)),
    Block("scan", "Mass scan, then go to the peak", "Procedures",
          "Sweep the analyser magnet and measure the beam on the Faraday cup, then set the magnet on the biggest "
          "peak. Needs the magnet on and the cup in and biased.", _routine("mass_scan")),
    Block("sub", "Run another recipe", "Structure",
          "Run a saved recipe as one step, then carry on with this one.",
          {"type": "SUB_RECIPE", "target_file": ""}, in_parallel=False),
    Block("parallel", "Do steps at the same time", "Structure",
          "The steps inside it run together; it is done when all of them are.",
          {"type": "PARALLEL", "steps": []}),
)
BY_KEY = {b.key: b for b in BLOCKS}
_TYPE_BLOCKS = {"ACTION": "set", "VERIFIED_ACTION": "set_check", "WAIT_TIME": "wait_time",
                "WAIT_TELEMETRY": "wait_for", "USER_PROMPT": "ask", "SUB_RECIPE": "sub", "PARALLEL": "parallel"}
_ROUTINE_BLOCKS = {"psu_control": "supply", "safety_relay_init": "relay", "hv_conditioning": "condition",
                   "mass_scan": "scan"}


def block_for(step) -> Block:
    """The block a step is (None for a step type or routine the builder doesn't know)."""
    if not isinstance(step, dict):
        return None
    kind = step.get("type")
    key = _ROUTINE_BLOCKS.get(step.get("routine_name")) if kind == "ROUTINE_CALL" else _TYPE_BLOCKS.get(kind)
    return BY_KEY.get(key)


def renumber(steps: list):
    """Steps numbered 1..n in order, and a PARALLEL step's own steps <its number>01, 02 ... (The
    sequencer refuses a recipe with a number used twice.)"""
    for i, step in enumerate(s for s in steps if isinstance(s, dict)):
        step["step_id"] = i + 1
        if step.get("type") == "PARALLEL" and isinstance(step.get("steps"), list):
            for j, sub in enumerate(s for s in step["steps"] if isinstance(s, dict)):
                sub["step_id"] = (i + 1) * 100 + j + 1


def next_version(version: str) -> str:
    """'1.1' -> '1.2', '2' -> '3', '1.0.9' -> '1.0.10', 'beta' -> 'beta.1'."""
    text = str(version).strip()
    head, dot, last = text.rpartition(".")
    if last.isdigit():
        return f"{head}{dot}{int(last) + 1}"
    return f"{text}.1" if text else "1.0"
