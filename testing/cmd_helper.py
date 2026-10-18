"""Send commands to the SINDIE services from a script and see what each service said.

Commands are acknowledged now (see src/core/commands.py): every one comes back
ok / accepted / rejected (with the reason) / failed, or timeout / unreachable.
The old pattern - a PUB socket, sleep(0.2), send_json, hope - no longer connects
to the services at all.

    from cmd_helper import send            # when running a script in testing/
    send("ion_beam.source.chamber.cmd_open_gv", True)

Command line:
    python testing/cmd_helper.py ion_beam.system.cmd_fault_reset true
    python testing/cmd_helper.py ion_beam.beamline.magnet.sp_requested_current 12.5
"""
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.core.commands import SUCCESS, CommandClient, describe, route_for_tag  # noqa: E402

_client = None


def send(tag, value, subsystem=None, origin="script", timeout=3.0, verbose=True) -> dict:
    """Send one command and wait for the service's answer. Returns the reply dict."""
    global _client
    if _client is None:
        _client = CommandClient()
    reply = _client.request(subsystem or route_for_tag(tag), tag, value, origin=origin, timeout=timeout)
    if verbose:
        print(("  OK    " if reply.get("status") in SUCCESS else "  NOT DONE  ") + describe(reply))
    return reply


def ok(reply) -> bool:
    return reply.get("status") in SUCCESS


def _parse(text):
    low = text.strip().lower()
    if low in ("true", "on", "yes"):
        return True
    if low in ("false", "off", "no"):
        return False
    try:
        return int(text)
    except ValueError:
        return float(text)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    r = send(sys.argv[1], _parse(sys.argv[2]), subsystem=sys.argv[3] if len(sys.argv) > 3 else None)
    sys.exit(0 if ok(r) else 1)
