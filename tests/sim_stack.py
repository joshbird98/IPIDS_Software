"""Starts the real service manager with simulated devices for end-to-end tests."""
import os
import signal
import subprocess
import sys
import time

import orjson
import psutil
import zmq

from src.core import commands as C

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class Stack:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        env = dict(os.environ, IPIDS_SIMULATE="1", IPIDS_DATA_DIR=str(data_dir),
                   IPIDS_LOG_DIR=str(data_dir / "logs"), PYTHONPATH=ROOT)
        env.pop("IPIDS_SIM_FAULTS", None)
        self.log = open(data_dir / "manager_console.log", "w")
        self.proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "src", "services", "service_manager.py")],
                                     env=env, cwd=ROOT, stdout=self.log, stderr=subprocess.STDOUT)
        from src.core.network_map import ZMQ_PORT_MANAGER_PUB, ZMQ_PORT_PLC_PUB, ZMQ_PORT_SPELLMAN_PUB, \
            ZMQ_PORT_MONITOR_PUB, ZMQ_PORT_MAGNET_PUB
        ctx = zmq.Context.instance()
        self.sub = ctx.socket(zmq.SUB)
        self.sub.setsockopt(zmq.SUBSCRIBE, b"")
        for ep in (ZMQ_PORT_MANAGER_PUB, ZMQ_PORT_PLC_PUB, ZMQ_PORT_SPELLMAN_PUB, ZMQ_PORT_MONITOR_PUB,
                   ZMQ_PORT_MAGNET_PUB):
            self.sub.connect(ep)
        self.latest = {}
        self.client = C.CommandClient(default_timeout=4.0)

    def pump(self, seconds=0.2):
        end = time.time() + seconds
        while time.time() < end:
            if self.sub.poll(50):
                while True:
                    try:
                        topic, payload = self.sub.recv_multipart(zmq.NOBLOCK)
                    except zmq.Again:
                        break
                    self.latest.setdefault(topic.decode(), {}).update(orjson.loads(payload))

    def wait(self, predicate, timeout, what):
        end = time.time() + timeout
        while time.time() < end:
            self.pump(0.2)
            try:
                if predicate(self.latest):
                    return
            except (KeyError, TypeError):
                pass
        raise AssertionError(f"timed out waiting for {what}; manager says "
                             f"{self.latest.get('MANAGER', {}).get('manager.services')}")

    def services(self):
        return self.latest.get("MANAGER", {}).get("manager.services", {})

    def cmd(self, subsystem, tag, value):
        return self.client.request(subsystem, tag, value, origin="test", timeout=4.0)

    def stop(self):
        self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(20)
        finally:
            if self.proc.poll() is None:
                self.proc.kill()
            self.sub.close(0)
            self.client.close()
            self.log.close()




def start_stack(data_dir, timeout=60):
    s = Stack(data_dir)
    s.wait(lambda l: l["MANAGER"]["manager.services"] and
           all(v == "ONLINE" for v in l["MANAGER"]["manager.services"].values()), timeout, "all services ONLINE")
    return s


def stop_stack(s):
    children = []
    try:
        children = psutil.Process(s.proc.pid).children(recursive=True)
    except psutil.NoSuchProcess:
        pass
    s.stop()
    time.sleep(1.0)
    alive = [p for p in children if p.is_running() and p.status() != psutil.STATUS_ZOMBIE]
    for p in alive:
        p.kill()
    return alive
