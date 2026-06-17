import time
import zmq
import orjson
import queue
from PyQt6.QtCore import QThread, pyqtSignal
from src.core.network_map import (
    ZMQ_PORT_PLC_CMD,
    ZMQ_PORT_SRC_TURBO_CMD,
    ZMQ_PORT_MANAGER_CMD,
    ZMQ_PORT_SPELLMAN_CMD,
    ZMQ_PORT_MAGNET_CMD,
    ZMQ_PORT_SMU_CMD
)

# --- Background Network Workers ---

class ZMQTelemetryThread(QThread):
    data_received = pyqtSignal(dict)

    def __init__(self, ports: list[str]):
        super().__init__()
        self.ports = ports
        self.running = True

    def run(self):
        ctx = zmq.Context.instance()
        sub_socket = ctx.socket(zmq.SUB)
        for port in self.ports:
            sub_socket.connect(port)
        sub_socket.setsockopt(zmq.SUBSCRIBE, b"")

        poller = zmq.Poller()
        poller.register(sub_socket, zmq.POLLIN)

        try:
            while self.running:
                socks = dict(poller.poll(timeout=100))
                if sub_socket in socks and socks[sub_socket] == zmq.POLLIN:
                    try:
                        topic, payload = sub_socket.recv_multipart(flags=zmq.NOBLOCK)
                        self.data_received.emit(orjson.loads(payload))
                    except Exception as e:
                        print(f"[ZMQ Telemetry Error] {e}") # This will print exact decoding/unpacking errors
                        continue
        finally:
            sub_socket.setsockopt(zmq.LINGER, 0)
            sub_socket.close()

    def stop(self):
        self.running = False
        self.wait()


class ZMQCommandThread(QThread):
    def __init__(self):
        super().__init__()
        self.cmd_queue = queue.Queue()
        self.running = True

    def send_command(self, subsystem: str, tag: str, value: any):
        self.cmd_queue.put((subsystem, tag, value))

    def run(self):
        ctx = zmq.Context.instance()
        sockets = {
            "plc": ctx.socket(zmq.PUB),
            "src_turbo": ctx.socket(zmq.PUB),
            "manager": ctx.socket(zmq.PUB),
            "spellman": ctx.socket(zmq.PUB),
            "magnet": ctx.socket(zmq.PUB),
            "smu": ctx.socket(zmq.PUB)
        }
        sockets["plc"].connect(ZMQ_PORT_PLC_CMD)
        sockets["src_turbo"].connect(ZMQ_PORT_SRC_TURBO_CMD)
        sockets["manager"].connect(ZMQ_PORT_MANAGER_CMD)
        sockets["spellman"].connect(ZMQ_PORT_SPELLMAN_CMD)
        sockets["magnet"].connect(ZMQ_PORT_MAGNET_CMD)
        sockets["smu"].connect(ZMQ_PORT_SMU_CMD)

        try:
            while self.running:
                try:
                    subsystem, tag, value = self.cmd_queue.get(timeout=0.1)
                    if subsystem == "manager":
                        payload = value
                    else:
                        payload = {"tag": tag, "value": value, "ts": time.time()}
                    sockets[subsystem].send_json(payload)
                except queue.Empty:
                    continue
                except:
                    continue
        finally:
            for s in sockets.values():
                s.setsockopt(zmq.LINGER, 0)
                s.close()

    def stop(self):
        self.running = False
        self.wait()
