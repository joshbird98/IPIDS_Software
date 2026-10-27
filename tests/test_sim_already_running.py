"""run_simulation.py started while the simulator is already running (run_simulation.py).

October 2026 (Josh): the simulator's HMI and viewer had been closed, its services hadn't (its own
window was still open somewhere); "Start the simulator" in the launcher then opened a console that
said so and closed at once, so nothing seemed to happen. It now asks what to do."""
import builtins
import os
import socket
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OFFSET = 36500
LOCK_PORT = 65432 - (OFFSET % 60000)            # as src/core/network_map.py


def _start(answers):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SINDIE_", "IPIDS_"))}
    return subprocess.run([sys.executable, "run_simulation.py", "--port-offset", str(OFFSET), "--no-hmi",
                           "--no-viewer"], cwd=ROOT, input=answers, capture_output=True, text=True,
                          timeout=60, env=env)


def test_started_again_it_says_so_and_asks():
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)     # (a service manager holds this)
    held.bind(("localhost", LOCK_PORT))
    held.listen(1)
    try:
        for answers in ("q\n", "", "x\nq\n"):    # leave it; no answer at all (input closed); a wrong key first
            out = _start(answers)
            assert out.returncode == 0, out.stderr
            assert "already running" in out.stdout and "o  open its HMI and data viewer again" in out.stdout
            assert "Left running." in out.stdout
            assert "Starting the services" not in out.stdout
    finally:
        held.close()


def test_the_answers(monkeypatch):
    import run_simulation
    for typed, meant in (("o", "open"), ("R", "restart"), (" s ", "stop"), ("q", None)):
        monkeypatch.setattr(builtins, "input", lambda prompt="", t=typed: t)
        assert run_simulation._already_running(OFFSET) == meant
