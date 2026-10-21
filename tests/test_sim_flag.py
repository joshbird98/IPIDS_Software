"""`--sim` on the HMI or the data viewer (src/core/env.py: use_simulator).

Found while writing the help (October 2026): `--sim` used to switch on simulation mode without
the simulator's port offset, so `python src/hmi.py --sim` on the lab PC talked to the real
services while its title bar said SIMULATION."""
import os
import subprocess
import sys

from src.core.env import SIM_PORT_OFFSET, use_simulator

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def test_sim_uses_the_simulators_ports():
    env = {}
    use_simulator(env)
    assert env == {"SINDIE_SIMULATE": "1", "SINDIE_PORT_OFFSET": str(SIM_PORT_OFFSET)}


def test_an_offset_already_chosen_is_kept():
    env = {"SINDIE_PORT_OFFSET": "31000"}
    use_simulator(env)
    assert env["SINDIE_PORT_OFFSET"] == "31000"
    legacy = {"IPIDS_PORT_OFFSET": "32000"}                  # the machine's old name still counts
    use_simulator(legacy)
    assert "SINDIE_PORT_OFFSET" not in legacy


def test_the_simulator_and_sim_agree():
    """run_simulation.py's default offset is the one --sim uses."""
    out = subprocess.run([sys.executable, "-c", "import run_simulation as r; print(r.SIM_PORT_OFFSET)"],
                         cwd=ROOT, capture_output=True, text=True, timeout=60,
                         env={k: v for k, v in os.environ.items() if not k.startswith(("SINDIE_", "IPIDS_"))})
    assert out.stdout.strip() == str(SIM_PORT_OFFSET), out.stderr


def test_hmi_sim_flag_sets_the_offset():
    """What the HMI, the data viewer, the service manager and the launcher do with --sim, in a fresh
    process (nothing set beforehand)."""
    for script in ("src/hmi.py", "src/gui/data_viewer/viewer_main.py", "src/services/service_manager.py",
                   "main_launcher.py"):
        with open(os.path.join(ROOT, script), "r", encoding="utf-8") as f:
            text = f.read()
        branch = text[text.index('if "--sim" in sys.argv[1:] or flag("SIMULATE"):'):][:400]
        assert "use_simulator()" in branch, script
    code = ("import sys, os; sys.argv = ['hmi.py', '--sim']; "
            "from src.core.env import use_simulator; use_simulator(); "
            "from src.core import network_map; print(network_map.PORT_OFFSET, os.environ['SINDIE_SIMULATE'])")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SINDIE_", "IPIDS_"))}
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60,
                         env=dict(env, PYTHONPATH=ROOT))
    assert out.stdout.split() == [str(SIM_PORT_OFFSET), "1"], out.stderr


def test_the_simulate_flag_alone_gets_the_simulators_ports_too():
    """SINDIE_SIMULATE=1 without a port offset (a PyCharm run configuration, say): the launcher
    and the programs take the simulator's ports as well, never the simulator's data with the real
    system's ports."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SINDIE_", "IPIDS_"))}
    code = ("import sys; sys.argv = ['main_launcher.py']; import main_launcher; "
            "from src.core import network_map, paths; print(network_map.PORT_OFFSET, paths.simulation_enabled())")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60,
                         env=dict(env, PYTHONPATH=ROOT, SINDIE_SIMULATE="1"))
    assert out.stdout.split() == [str(SIM_PORT_OFFSET), "True"], out.stderr
