import time
import zmq
import orjson
import os
import json
from src.core.network_map import ZMQ_PORT_EVENTS_PUB


class EventHelper:
    def __init__(self, service_name: str):
        self.service_name = service_name

        # Automatic ZMQ Setup
        self.context = zmq.Context()
        self.pub = self.context.socket(zmq.PUB)
        # We CONNECT because service_events.py BINDs
        self.pub.connect(ZMQ_PORT_EVENTS_PUB)

        # Load fault map for the log_fault utility
        self.fault_map = self._load_fault_map()

    def _load_fault_map(self):
        config_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../config/fault_config.json'))
        flat_map = {}
        try:
            with open(config_path, "r") as f:
                raw_json = json.load(f)
                # Flatten the nested UDT structure for O(1) key lookups
                for udt_group in raw_json.values():
                    if "bits" in udt_group:
                        for bit_data in udt_group["bits"].values():
                            name = bit_data["name"]
                            flat_map[name] = bit_data
        except Exception as e:
            print(f"[EventHelper] Failed to load fault map: {e}")
        return flat_map

    def log_phase(self, phase_name: str):
        """Logs a transition to a new operational phase."""
        self.log_general(
            message=f"PHASE CHANGE: {phase_name.upper()}",
            event_type="PHASE_CHANGE",
            severity="INFO",
            metadata={"new_phase": phase_name.upper()}
        )

    def log_fault(self, fault_key: str, active: bool, metadata: dict = None):
        fault_info = self.fault_map.get(fault_key, {})
        description = fault_info.get("description", fault_key)
        severity = fault_info.get("severity", "CRITICAL")  # Using your int/string choice

        status_str = "ACTIVATED" if active else "CLEARED"
        self.log_general(
            message=f"FAULT {status_str}: {description}",
            severity=severity if active else "INFO",
            event_type="FAULT",
            metadata={"fault_key": fault_key, "is_active": active, **(metadata or {})}
        )

    def log_general(self, message: str, severity: str = "INFO", event_type: str = "GENERAL", metadata: dict = None,
                    timestamp_override: float = None):
        event = {
            "ts": timestamp_override if timestamp_override is not None else time.time(),
            "service": self.service_name,
            "severity": severity,
            "type": event_type,
            "message": message,
            "metadata": metadata or {}
        }
        self.pub.send_multipart([b"EVENTS", orjson.dumps(event)])
        print(f"[{self.service_name}]: {message}")

    def log_user_marker(self, marker_ts: float, text: str, color: str):
        """Publishes a new user marker to the database via ZMQ."""
        self.log_general(
            message=text,
            severity="INFO",
            event_type="USER_MARKER",
            metadata={"color": color},
            timestamp_override=marker_ts
        )

    def delete_user_marker(self, marker_ts: float):
        """Publishes a command to delete a specific marker from the database."""
        self.log_general(
            message="Delete marker command",
            severity="INFO",
            event_type="DELETE_MARKER",
            metadata={},
            timestamp_override=marker_ts
        )