"""The events database (data/events.db): its table, and how to tell it is damaged.

Written by the events service (src/services/service_events.py); read by the data viewer and
exports; backed up each night to OneDrive (service_data_compactor.py); repaired by
src/utils/repair_events_db.py.

A damaged events.db (most often: a copy put over it while a program had it open, leaving the
-wal and -shm of another database beside it) returns some events and not others - an error for
one query, nothing for the next - so the data viewer's events come and go, and the events service
can't write new ones. SQLite says "database disk image is malformed".
"""
import sqlite3

REPAIR_HINT = ("events.db is damaged: events are being lost, and the data viewer shows some of the old ones and "
               "not others. Stop the services, then run: python -m src.utils.repair_events_db "
               "(docs/troubleshooting.md, \"Events come and go in the data viewer\")")


def create_schema(conn: sqlite3.Connection):
    """The events table and its indexes, in WAL mode (the events service's, and a repaired one's)."""
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
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
    # Filtering by type (e.g. hiding the COMMAND audit trail) without scanning everything
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_type_timestamp ON events(event_type, timestamp)')
    conn.commit()


def damaged(error) -> bool:
    """An SQLite error that means the database file itself is damaged (not merely busy)."""
    text = str(error).lower()
    return "malformed" in text or "not a database" in text or "corrupt" in text


def check(conn: sqlite3.Connection, most: int = 3) -> str:
    """SQLite's quick check of the database open on `conn` (it reads every page: about a second for
    100 MB): '' if it is sound, otherwise what it found (at most `most` problems). An error that
    isn't damage (the database busy, say) is raised: it can't be checked just now."""
    try:
        found = [str(r[0]) for r in conn.execute(f"PRAGMA quick_check({int(most)})").fetchall()]
    except sqlite3.Error as e:
        if damaged(e):
            return str(e)
        raise
    return "" if found == ["ok"] else " ".join(" ".join(f.split()) for f in found)
