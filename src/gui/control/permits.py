"""What the services say about operator control, for the panels.

Every device service publishes, per subsystem (src/core/device_service.py):
    <subsystem>.permit          1.0 if an operator command would be accepted now
    <subsystem>.permit_reason   why not (not connected, relay off, PLC mode, held by a sequence...)
    <subsystem>.lease_holder    the sequence holding it (null when free), and .lease_purpose

So a panel no longer works out its own lock-out rules: it shows the service's reason. A held
subsystem is released by stopping the sequence, or taken back with take_control().
"""
from src.core.commands import route_for_tag
from src.core.leases import LEASE_SUFFIX

PERMIT = ".permit"
HOLDER = ".lease_holder"


def permit_reason(data: dict, subsystem: str, what: str = None) -> str:
    """'' if the subsystem's service would accept an operator command now, else the reason."""
    permit = data.get(subsystem + PERMIT)
    if permit is None:
        return f"no live data from the {what or subsystem.split('.')[-1].replace('_', ' ')} service"
    if permit:
        return ""
    return data.get(subsystem + ".permit_reason") or "not available"


def leases(data: dict) -> dict:
    """{subsystem: purpose} for every subsystem a sequence holds now."""
    out = {}
    for k, holder in data.items():
        if k.endswith(HOLDER) and holder:
            sub = k[:-len(HOLDER)]
            out[sub] = data.get(sub + ".lease_purpose") or holder
    return out


def take_control(cmd_thread, subsystem: str):
    """The operator takes a subsystem back from the sequence holding it (which then pauses or
    stops). Returns the command ticket."""
    tag = subsystem + LEASE_SUFFIX
    return cmd_thread.send_command(route_for_tag(f"{subsystem}.cmd_enable"), tag, {"op": "take"}, origin="hmi")
