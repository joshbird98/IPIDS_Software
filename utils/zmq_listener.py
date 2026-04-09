import zmq
import json
from PyQt6.QtCore import QThread, pyqtSignal


class ZMQLiveEngine(QThread):
    # This signal safely transports the raw dictionary to the Cache
    data_ready = pyqtSignal(dict)

    def __init__(self, *ports):
        super().__init__()
        self.ports = ports
        self.running = True

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

                if sub_socket in socks:
                    topic, payload = sub_socket.recv_multipart()
                    data = json.loads(payload.decode('utf-8'))

                    # Pass the raw payload to data_cache.py
                    # The cache will handle the ISA-95 flattening
                    self.data_ready.emit(data)

            except Exception:
                # Ignore random network dropouts or decode errors to prevent thread crashes
                pass

        sub_socket.close()
        print("[Live Engine] Thread Stopped.")

    def stop(self):
        self.running = False
        self.wait()