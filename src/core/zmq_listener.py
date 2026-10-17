import time

import orjson
import zmq
from PyQt6.QtCore import QThread, pyqtSignal


class ZMQLiveEngine(QThread):
    # Legacy path: emits the decoded dict to the GUI thread (one queued Qt event per
    # message). Used only if no `sink` is given.
    data_ready = pyqtSignal(dict)

    def __init__(self, *ports, sink=None):
        """
        sink: optional callable(source: str, ts: float, data: dict) invoked IN THIS
        network thread for every message. The data cache passes LiveStore.ingest,
        so ingest no longer queues ~120 Qt events/s onto the GUI thread, and each
        sample is timestamped when it arrives rather than when the GUI thread gets
        round to it.
        """
        super().__init__()
        self.ports = ports
        self.sink = sink
        self.running = True
        self._error_counts = {}

    def _report(self, exc):
        key = f"{type(exc).__name__}: {exc}"
        n = self._error_counts.get(key, 0) + 1
        self._error_counts[key] = n
        if n in (1, 10, 100, 1000) or n % 10000 == 0:
            print(f"[Live Engine] message error (x{n}): {key}")

    def run(self):
        """The background loop that listens to the network."""
        context = zmq.Context.instance()
        sub_socket = context.socket(zmq.SUB)

        # Connect to all dynamically provided ports
        for port in self.ports:
            sub_socket.connect(port)

        # Subscribe to all topics on these ports
        sub_socket.setsockopt_string(zmq.SUBSCRIBE, "")

        poller = zmq.Poller()
        poller.register(sub_socket, zmq.POLLIN)

        print(f"[Live Engine] Network Thread Started. Listening on {len(self.ports)} ports.")

        while self.running:
            try:
                # 100ms timeout so the thread can safely exit if self.running = False
                socks = dict(poller.poll(100))
                if sub_socket not in socks:
                    continue
                # Drain everything that is queued, not one message per poll.
                while True:
                    try:
                        parts = sub_socket.recv_multipart(zmq.NOBLOCK)
                    except zmq.Again:
                        break
                    ts = time.time()
                    try:
                        if len(parts) >= 2:
                            topic, payload = parts[0], parts[1]
                        else:
                            topic, payload = b"", parts[0]
                        data = orjson.loads(payload)
                        if not isinstance(data, dict):
                            continue
                        if self.sink is not None:
                            self.sink(topic.decode("utf-8", "replace"), ts, data)
                        else:
                            self.data_ready.emit(data)
                    except Exception as e:
                        # Bad payloads used to be swallowed silently; report (rate-limited).
                        self._report(e)
            except Exception as e:
                # Ignore random network dropouts to prevent thread crashes
                self._report(e)

        sub_socket.close()
        print("[Live Engine] Thread Stopped.")

    def stop(self):
        self.running = False
        self.wait()
