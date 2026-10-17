"""The service manager's start-up purge of orphaned services only touches its own
instance - so the simulator or the end-to-end tests can't kill the live system."""
import os
import subprocess
import sys
import time

from src.services.service_manager import find_orphans, instance_key

SCRIPT = "service_purge_probe.py"


def spawn(folder, env_extra):
    folder.mkdir()
    script = folder / SCRIPT
    script.write_text("import time\ntime.sleep(60)\n")
    env = {k: v for k, v in os.environ.items() if k not in ("IPIDS_PORT_OFFSET", "IPIDS_SIMULATE")}
    env.update(env_extra)
    return subprocess.Popen([sys.executable, str(script)], env=env)


def test_instance_key():
    assert instance_key({}) == (0, False)
    assert instance_key({"IPIDS_PORT_OFFSET": "10000", "IPIDS_SIMULATE": "1"}) == (10000, True)
    assert instance_key({"IPIDS_PORT_OFFSET": "", "IPIDS_SIMULATE": "0"}) == (0, False)


def test_only_this_instances_orphans_are_found(tmp_path):
    live = spawn(tmp_path / "live", {})                                        # the real system
    sim = spawn(tmp_path / "sim", {"IPIDS_PORT_OFFSET": "10000", "IPIDS_SIMULATE": "1"})
    mine = spawn(tmp_path / "mine", {"IPIDS_PORT_OFFSET": "31000", "IPIDS_SIMULATE": "1"})
    try:
        time.sleep(0.5)
        found = {p.pid for p, _ in find_orphans([SCRIPT], my_instance=(31000, True))}
        assert mine.pid in found and live.pid not in found and sim.pid not in found
        found = {p.pid for p, _ in find_orphans([SCRIPT], my_instance=(0, False))}   # the real manager
        assert found & {live.pid, sim.pid, mine.pid} == {live.pid}
    finally:
        for p in (live, sim, mine):
            p.kill()
            p.wait()
