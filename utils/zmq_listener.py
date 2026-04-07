import zmq
import json
import os
import re
from PyQt6.QtCore import QThread, pyqtSignal


class ZMQLiveEngine(QThread):
    # This signal safely transports a dictionary of { "tag.name": value } to the GUI
    data_ready = pyqtSignal(dict)

    def __init__(self, plc_port, vacuum_port):
        super().__init__()
        self.plc_port = plc_port
        self.vacuum_port = vacuum_port
        self.running = True
        self.vacuum_hw_map = self._build_vacuum_hw_map()

    def _build_vacuum_hw_map(self) -> dict:
        """Creates the same Reverse Lookup table we built for the Logger."""
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(current_dir)
        registry_path = os.path.join(project_root, "system_tags.json")

        hw_map = {}
        try:
            with open(registry_path, "r") as f:
                registry = json.load(f)

            for full_tag, metadata in registry.items():
                if metadata.get("source") == "service_vacuum":
                    node = str(metadata.get("hw_node"))
                    ch = str(metadata.get("hw_channel"))
                    if node and ch and node != "None" and ch != "None":
                        base_path = full_tag.rsplit('.', 1)[0]
                        hw_map[(node, ch)] = base_path
        except Exception as e:
            print(f"[Live Engine] Failed to build HW map: {e}")
        return hw_map

    def _flatten_vacuum_data(self, data: dict) -> dict:
        """Converts raw hardware data into perfect registry keys."""
        flat_data = {}
        for node_id_str, node_data in data.items():
            for ch_id_str, ch_data in node_data.get("channels", {}).items():
                base_path = self.vacuum_hw_map.get((node_id_str, ch_id_str))
                if not base_path: continue

                p = ch_data.get("pressure")
                if p is not None:
                    flat_data[f"{base_path}.pressure"] = float(p)

                # Optionally pass status through if you want to plot integer status codes
                s = ch_data.get("status")
                if s is not None:
                    status_map = {"OK": 0.0, "NO-SEN": 1.0, "RANGE?": 2.0, "S-OFF": 3.0, "ERROR-H": 4.0, "ERROR-L": 5.0,
                                  "ERROR-S": 6.0}
                    flat_data[f"{base_path}.status"] = status_map.get(str(s).strip().upper(), -1.0)
        return flat_data

    def run(self):
        """The background loop that listens to the network."""
        context = zmq.Context.instance()

        sub_socket = context.socket(zmq.SUB)
        sub_socket.connect(self.plc_port)
        sub_socket.connect(self.vacuum_port)

        # Subscribe to everything on those ports
        sub_socket.setsockopt_string(zmq.SUBSCRIBE, "")

        poller = zmq.Poller()
        poller.register(sub_socket, zmq.POLLIN)

        print("[Live Engine] Background Network Thread Started.")

        while self.running:
            try:
                # 100ms timeout so the thread can safely exit if self.running = False
                socks = dict(poller.poll(100))

                if sub_socket in socks:
                    topic, payload = sub_socket.recv_multipart()
                    data = json.loads(payload.decode('utf-8'))

                    # Route the data based on where it came from
                    if b"VACUUM" in topic:
                        flat_data = self._flatten_vacuum_data(data)
                        if flat_data:
                            self.data_ready.emit(flat_data)
                    else:
                        # Assuming PLC data is already a flat dict of {tag: value}
                        self.data_ready.emit(data)

            except Exception as e:
                pass  # Ignore random network dropouts in the GUI thread

        sub_socket.close()
        print("[Live Engine] Thread Stopped.")

    def stop(self):
        self.running = False
        self.wait()