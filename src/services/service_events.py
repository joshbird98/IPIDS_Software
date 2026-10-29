import logging
import os
import sqlite3
import threading
import time

import orjson
import zmq

from src.core import events_db, paths
from src.core.events_db import REPAIR_HINT, create_schema, damaged
from src.core.log_setup import Throttle, setup_logging
from src.core.network_map import ZMQ_PORT_EVENTS_SUB, ZMQ_PORT_HEARTBEAT
from src.core.os_helper import harden_windows_process

# --- CONFIGURATION ---
DB_PATH = paths.EVENTS_DB

log = logging.getLogger("service_events")


class EventLogger:
    """Writes every published event to data/events.db.

    One long-lived connection in WAL mode: readers (the viewer, exports, the nightly
    backup) never block the writer, and everything that arrived in the same poll is
    inserted in one transaction - the command audit trail can add tens of rows per
    second while the optimiser or a mass scan is running.
    """

    def __init__(self, db_path: str = None):
        self.db_path = db_path or DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.conn = None
        self._open_db()

        self.context = zmq.Context()
        # Subscribe to all events
        self.sub_socket = self.context.socket(zmq.SUB)
        self.sub_socket.setsockopt(zmq.RCVHWM, 100000)
        self.sub_socket.bind(ZMQ_PORT_EVENTS_SUB)
        self.sub_socket.setsockopt_string(zmq.SUBSCRIBE, "")

        self.hb_socket = self.context.socket(zmq.PUB)
        self.hb_socket.connect(ZMQ_PORT_HEARTBEAT)
        self._throttle = Throttle(60.0)
        self._damage_report = Throttle(3600.0)
        self.running = True

    def _open_db(self):
        self.conn = sqlite3.connect(self.db_path, timeout=5.0)
        create_schema(self.conn)

    def handle_events(self, payloads):
        """Write a batch of events in one transaction. Returns the number written."""
        rows = []
        deletes = []
        for payload in payloads:
            if payload.get("type") == "DELETE_MARKER":
                try:
                    deletes.append(float(payload.get("ts")))
                except (TypeError, ValueError):
                    log.warning("DELETE_MARKER without a valid timestamp: %r", payload)
                continue
            rows.append((
                payload.get("ts", time.time()),
                payload.get("service", "unknown"),
                payload.get("severity", "INFO"),
                payload.get("type", "GENERAL"),
                payload.get("message", ""),
                orjson.dumps(payload.get("metadata", {}), default=str).decode(),
            ))
        if not rows and not deletes:
            return 0
        try:
            with self.conn:
                if rows:
                    self.conn.executemany('''
                        INSERT INTO events (timestamp, service, severity, event_type, message, metadata)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', rows)
                for target_ts in deletes:
                    # Using an epsilon window of 0.002 seconds to catch float rounding errors
                    cur = self.conn.execute(
                        "DELETE FROM events WHERE abs(timestamp - ?) < 0.002 AND event_type = 'USER_MARKER'",
                        (target_ts,))
                    log.info("Delete requested for marker at %.3f: %d row(s) deleted", target_ts, cur.rowcount)
            return len(rows)
        except sqlite3.Error as e:
            if (n := self._throttle.ready("db")) is not None:
                log.error("DB error, %d event(s) lost: %s%s", len(rows), e, Throttle.suffix(n))
            if damaged(e) and self._damage_report.ready("damaged") is not None:
                log.critical(REPAIR_HINT)
            # Reopen in case the handle is broken (disk unplugged, file replaced, ...)
            try:
                self.conn.close()
            except sqlite3.Error:
                pass
            try:
                self._open_db()
            except sqlite3.Error as e2:
                log.error("Cannot reopen %s: %s", self.db_path, e2)
            return 0

    # Kept for anything that still calls the single-event API
    def handle_event(self, payload: dict):
        return self.handle_events([payload])

    def check_in_background(self) -> threading.Thread:
        """SQLite's check of the database, on a thread and a connection of its own (it reads every
        page: a second or more for a big database, which mustn't hold up the heartbeat). Logged as
        CRITICAL, with how to repair it, if the database is damaged."""
        def check():
            try:
                conn = sqlite3.connect(self.db_path, timeout=5.0)
                try:
                    problem = events_db.check(conn)
                finally:
                    conn.close()
            except sqlite3.Error as e:
                log.warning("Couldn't check %s just now: %s", self.db_path, e)
                return
            if problem:
                log.critical("%s. SQLite's check found: %s", REPAIR_HINT, problem[:500])
        thread = threading.Thread(target=check, name="events-db-check", daemon=True)
        thread.start()
        return thread

    def run(self):
        log.info("Service online. Listening for events on %s, writing %s", ZMQ_PORT_EVENTS_SUB, self.db_path)
        self.check_in_background()
        last_hb = 0

        while self.running:
            batch = []
            if self.sub_socket.poll(timeout=100):
                while len(batch) < 5000:
                    try:
                        topic, msg = self.sub_socket.recv_multipart(zmq.NOBLOCK)
                    except zmq.Again:
                        break
                    except ValueError:
                        continue  # wrong frame count
                    try:
                        batch.append(orjson.loads(msg))
                    except ValueError:
                        log.warning("Unreadable event dropped: %r", msg[:200])
            if batch:
                self.handle_events(batch)

            # 2Hz Heartbeat
            if time.time() - last_hb > 0.5:
                self.hb_socket.send_json({"service": "service_events", "ts": time.time()})
                last_hb = time.time()

        self.conn.close()
        self.sub_socket.close(0)
        self.hb_socket.close(0)


if __name__ == "__main__":
    setup_logging("service_events")
    harden_windows_process()
    EventLogger().run()
