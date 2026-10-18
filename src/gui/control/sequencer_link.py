"""The HMI's side of the sequencer service (src/services/service_sequencer.py).

Recipes, mass scans and the optimiser run in the sequencer; the panels only send it commands
and show its telemetry (sequencer.<slot>.<name>), so closing the HMI does not stop them.
"""
from src.sequencer import protocol as P
from src.sequencer.points import PointFollower

FETCH_ORIGIN = "hmi.fetch"      # point fetches: not operator actions, kept out of the command log
OFFLINE_TEXT = "The sequencer service is not running (see the Service Manager panel)."


def slot_values(data: dict, slot: str) -> dict:
    prefix = f"{P.PREFIX}.{slot}."
    return {k[len(prefix):]: v for k, v in data.items() if k.startswith(prefix)}


class SequencerSlot:
    """One slot (recipe, mass_scan or optimiser) as a panel sees it."""

    def __init__(self, cmd_thread, slot: str):
        self.cmd_thread = cmd_thread
        self.slot = slot
        self.values = {}

    def update(self, data: dict):
        """data: the HMI's fresh telemetry view (stale keys are absent)."""
        self.values = slot_values(data, self.slot)

    def get(self, name, default=None):
        return self.values.get(name, default)

    @property
    def online(self) -> bool:
        return "state" in self.values

    @property
    def state(self):
        return self.values.get("state")

    @property
    def active(self) -> bool:
        return self.state in P.ACTIVE

    def send(self, verb: str, value=None, origin: str = "hmi"):
        """Send a command to this slot; returns the ticket (outcome also goes to the command log)."""
        return self.cmd_thread.send_command(P.ROUTE, P.command(self.slot, verb), value, origin=origin)

    def follower(self) -> PointFollower:
        return PointFollower(lambda run, pass_no: self.send("fetch", {"run": run, "pass": pass_no},
                                                            origin=FETCH_ORIGIN))
