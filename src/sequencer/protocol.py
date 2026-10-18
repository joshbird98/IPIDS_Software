"""Names shared by the sequencer service and its clients (the HMI, scripts, tests).

Commands go to the "sequencer" command endpoint (src/core/network_map.py):

    sequencer.recipe.cmd_start      {"file": "<name>.json"}   (a file in config/recipes)
    sequencer.recipe.cmd_pause / cmd_resume / cmd_stop
    sequencer.recipe.cmd_ack        {"seq": n}                 answer operator prompt n
    sequencer.mass_scan.cmd_start   {start_a, stop_a, step_a, tolerance, settle_time, samples,
                                     sample_interval, nplc, loop, export, export_path}
    sequencer.mass_scan.cmd_stop
    sequencer.optimiser.cmd_start   {x_tag, x_start, x_stop, x_steps, y_tag, ..., dwell, rb_tag}
    sequencer.optimiser.cmd_stop
    sequencer.optimiser.cmd_goto    {"x": .., "y": ..}         end the sweep there instead of restoring
    sequencer.<scan>.cmd_fetch      {"run": r, "pass": p}       reply data: every point of that pass

Telemetry (topic SEQUENCER_DATA), per slot: sequencer.<slot>.<key>, see the job classes.
"""
SERVICE = "service_sequencer"
ROUTE = "sequencer"
PREFIX = "sequencer"

RECIPE = "recipe"
MASS_SCAN = "mass_scan"
OPTIMISER = "optimiser"
SLOTS = (RECIPE, MASS_SCAN, OPTIMISER)

# Job states
IDLE = "idle"
RUNNING = "running"
PAUSED = "paused"
PROMPT = "prompt"          # waiting for the operator to acknowledge a prompt
STOPPING = "stopping"      # finishing off (restoring settings) after a stop or a failure
COMPLETE = "complete"
STOPPED = "stopped"
FAILED = "failed"
ACTIVE = frozenset({RUNNING, PAUSED, PROMPT, STOPPING})
FINAL = frozenset({COMPLETE, STOPPED, FAILED})


def key(slot: str, name: str) -> str:
    """Telemetry key, e.g. key("recipe", "state") -> "sequencer.recipe.state"."""
    return f"{PREFIX}.{slot}.{name}"


def command(slot: str, verb: str) -> str:
    """Command tag, e.g. command("recipe", "start") -> "sequencer.recipe.cmd_start"."""
    return f"{PREFIX}.{slot}.cmd_{verb}"


def parse_command(tag: str):
    """(slot, verb) of a sequencer command tag, or (None, None)."""
    parts = tag.split(".")
    if len(parts) != 3 or parts[0] != PREFIX or not parts[2].startswith("cmd_"):
        return None, None
    return parts[1], parts[2][4:]
