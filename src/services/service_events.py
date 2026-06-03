import sqlite3
import os
import zmq
import orjson
import time
from src.core.network_map import ZMQ_PORT_EVENTS_SUB, ZMQ_PORT_HEARTBEAT
from src.core.os_helper import harden_windows_process

# --- CONFIGURATION ---
DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../data/events.db'))


class EventLogger:
    def __init__(self):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        self._init_db()

        self.context = zmq.Context()
        # Subscribe to all events
        self.sub_socket = self.context.socket(zmq.SUB)
        self.sub_socket.bind(ZMQ_PORT_EVENTS_SUB)
        self.sub_socket.setsockopt_string(zmq.SUBSCRIBE, "")

        self.hb_socket = self.context.socket(zmq.PUB)
        self.hb_socket.connect(ZMQ_PORT_HEARTBEAT)

    def _init_db(self):
        """Creates the events table if it doesn't exist."""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL,
                service TEXT,
                severity TEXT,
                event_type TEXT,
                message TEXT,
                metadata TEXT
            )
        ''')
        # Indexing timestamp for fast GUI queries later
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_timestamp ON events(timestamp)')
        conn.commit()
        conn.close()

    def handle_event(self, payload: dict):
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            if payload.get("type") == "DELETE_MARKER":
                target_ts = float(payload.get("ts"))

                # Using an epsilon window of 0.002 seconds to catch float rounding errors
                cursor.execute(
                    """
                    DELETE FROM events 
                    WHERE abs(timestamp - ?) < 0.002 
                    AND event_type = 'USER_MARKER'
                    """,
                    (target_ts,)
                )

                # Diagnostic feedback so you know it actually worked
                deleted_count = cursor.rowcount
                print(f"[Event Logger] Delete requested for {target_ts:.3f}. Rows deleted: {deleted_count}")

                conn.commit()
                conn.close()

            else:
                # Serialize metadata dict to string if it exists
                meta = orjson.dumps(payload.get("metadata", {})).decode()

                cursor.execute('''
                    INSERT INTO events (timestamp, service, severity, event_type, message, metadata)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (
                    payload.get("ts", time.time()),
                    payload.get("service", "unknown"),
                    payload.get("severity", "INFO"),
                    payload.get("type", "GENERAL"),
                    payload.get("message", ""),
                    meta
                ))
                conn.commit()
                conn.close()

        except Exception as e:
            print(f"[Event Logger] DB Error: {e}")

    def run(self):
        print(f"[Event Logger] Service Online. Listening for events on {ZMQ_PORT_EVENTS_SUB}")
        last_hb = 0

        while True:
            try:
                # Non-blocking check for new events
                if self.sub_socket.poll(timeout=100):
                    topic, msg = self.sub_socket.recv_multipart()
                    payload = orjson.loads(msg)

                    self.handle_event(payload)

                # 2Hz Heartbeat
                if time.time() - last_hb > 0.5:
                    self.hb_socket.send_json({"service": "service_events", "ts": time.time()})
                    last_hb = time.time()

            except Exception as e:
                print(f"[Event Logger] Loop Error: {e}")
                time.sleep(1)


if __name__ == "__main__":
    harden_windows_process()
    EventLogger().run()