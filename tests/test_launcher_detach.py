"""src/launcher/detach.py: a program started through it is nobody's child, so ending the process
tree of whatever started it (PyCharm's Stop button) can't take it down."""
import os
import subprocess
import sys
import time

import psutil

from tests.conftest import ROOT


def test_the_program_is_not_a_child_of_its_starter(tmp_path):
    log = tmp_path / "child.log"
    out = subprocess.run([sys.executable, "-m", "src.launcher.detach", str(log), sys.executable, "-c",
                          "import time; print('child running', flush=True); time.sleep(30)"],
                         cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    pid = int(out.stdout.strip())
    try:
        child = psutil.Process(pid)
        assert child.is_running()
        assert child.ppid() != os.getpid()                            # not ours ...
        mine = {p.pid for p in psutil.Process(os.getpid()).children(recursive=True)}
        assert pid not in mine                                        # ... nor in our tree
        deadline = time.time() + 10
        while "child running" not in (log.read_text() if log.exists() else "") and time.time() < deadline:
            time.sleep(0.1)
        assert "child running" in log.read_text()
    finally:
        try:
            psutil.Process(pid).kill()
        except psutil.NoSuchProcess:
            pass


def test_usage():
    out = subprocess.run([sys.executable, "-m", "src.launcher.detach"], cwd=ROOT, capture_output=True, text=True,
                         timeout=30)
    assert out.returncode == 2 and "Start a program and leave it running" in out.stderr
