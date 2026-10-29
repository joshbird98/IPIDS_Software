"""Repair a damaged events database (data/events.db): every event that can still be read from it -
with older copies of it, such as the nightly OneDrive backup, filling in what can't - into a new
events.db. The damaged file is kept beside it, renamed events.db.damaged-<time>.

    python -m src.utils.repair_events_db --check                  is it damaged?
    python -m src.utils.repair_events_db                          repair it (the services stopped)
    python -m src.utils.repair_events_db --backup <copy of events.db> [--backup <another>]

How to tell it is damaged: the events service's log (SystemLogs/service_events.log) says "database
disk image is malformed", the data viewer says so under Event markers, and its events come and go
as you move about (SQLite returns some of them, and an error or nothing for others).

How it gets damaged: most often a copy of events.db put over the one in use while a program had it
open (its events.db-wal and events.db-shm then belong to the other database). Stop the services
and close the data viewer before replacing it, and take events.db-wal and events.db-shm away too.

What it does: reads the damaged database a range of rows at a time, skipping what can't be read (a
range that fails is halved until only the unreadable rows are left), and reads each backup given.
Every distinct event of all of them (an event found in more than one, alike in every column, once)
goes into a new database, in time order, with the indexes the events service makes. The new one is
checked, then swapped in. Nothing is deleted.
"""
import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:                    # (run as a file rather than with -m)
    sys.path.insert(0, ROOT)

from src.core.events_db import create_schema  # noqa: E402

COLUMNS = ("timestamp", "service", "severity", "event_type", "message", "metadata")
CHUNK = 5000                    # rows read at a time
EARLIEST, LATEST = 946684800.0, 4102444800.0     # a timestamp outside 2000-2100 is damage, not an event


class RepairError(RuntimeError):
    pass


def _copy_of(path: str, folder: str) -> str:
    """The database and its -wal (if any) copied into `folder`, so that nothing touches the
    original while it is checked or read (SQLite's recovery of the -wal happens on the copy)."""
    dst = os.path.join(folder, os.path.basename(path))
    shutil.copyfile(path, dst)
    wal = path + "-wal"
    if os.path.isfile(wal) and os.path.getsize(wal) > 0:
        shutil.copyfile(wal, dst + "-wal")
    return dst


def check(path: str) -> list:
    """SQLite's integrity check of the database (of a copy of it): [] if it is sound, else what it
    found (at most 20 lines)."""
    if not os.path.isfile(path):
        raise RepairError(f"there is no {path}")
    with tempfile.TemporaryDirectory(prefix="sindie_events_check_") as folder:
        conn = sqlite3.connect(_copy_of(path, folder))
        try:
            found = [r[0] for r in conn.execute("PRAGMA integrity_check(20)").fetchall()]
        except sqlite3.DatabaseError as e:
            found = [str(e)]
        finally:
            conn.close()
    lines = [ln for f in found for ln in str(f).splitlines() if ln.strip()]
    return [] if lines == ["ok"] else lines


def _sound(row) -> bool:
    ts, _service, _severity, event_type, message, _meta = row
    return (isinstance(ts, (int, float)) and EARLIEST <= ts <= LATEST and isinstance(event_type, str)
            and event_type != "" and isinstance(message, (str, type(None))))


def _top_id(conn) -> int:
    for query in ("SELECT seq FROM sqlite_sequence WHERE name = 'events'", "SELECT max(id) FROM events"):
        try:
            row = conn.execute(query).fetchone()
            if row and row[0]:
                return int(row[0])
        except sqlite3.DatabaseError:
            continue
    top = CHUNK                                         # (neither readable: look for rows further on)
    while top < 10 ** 9:
        try:
            if conn.execute("SELECT 1 FROM events WHERE id > ? LIMIT 1", (top,)).fetchone() is None:
                return top
        except sqlite3.DatabaseError:
            pass
        top *= 2
    return top


def read_events(path: str, take) -> tuple:
    """Every event that can be read from the database at `path` (a copy of it is read), handed to
    take(rows) a chunk at a time (tuples of COLUMNS). Returns (events read, unreadable), the row
    numbers that couldn't be read as [first, last] ranges."""
    query = f"SELECT id, {', '.join(COLUMNS)} FROM events WHERE id BETWEEN ? AND ?"
    unreadable, count = [], [0]
    with tempfile.TemporaryDirectory(prefix="sindie_events_read_") as folder:
        conn = sqlite3.connect(_copy_of(path, folder))
        try:
            conn.execute("PRAGMA cell_size_check=ON")

            def read(lo, hi):
                try:
                    got = conn.execute(query, (lo, hi)).fetchall()
                except sqlite3.DatabaseError:
                    if lo >= hi:
                        if unreadable and unreadable[-1][1] == lo - 1:
                            unreadable[-1][1] = lo
                        else:
                            unreadable.append([lo, lo])
                        return
                    mid = (lo + hi) // 2
                    read(lo, mid)
                    read(mid + 1, hi)
                    return
                rows = [tuple(r[1:]) for r in got if lo <= r[0] <= hi and _sound(r[1:])]
                count[0] += len(rows)
                take(rows)

            top = _top_id(conn)
            for lo in range(1, top + 1, CHUNK):
                read(lo, min(top, lo + CHUNK - 1))
            read(top + 1, top + 10 * CHUNK)             # (rows past the recorded top: a damaged counter)
        except sqlite3.DatabaseError as e:
            raise RepairError(f"{path} can't be read at all: {e}") from None
        finally:
            conn.close()
    return count[0], unreadable


def repair(db: str, backups=(), say=print, stamp: str = None) -> dict:
    """Repair the events database at `db` (see the module's doc). Returns what it did. The services
    and the data viewer must be closed: the database is swapped."""
    if not os.path.isfile(db):
        raise RepairError(f"there is no {db}")
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    new = db + ".repaired"
    if os.path.exists(new):
        raise RepairError(f"{new} is in the way (from a repair that didn't finish?): move it somewhere else first")
    conn = sqlite3.connect(new)
    try:
        create_schema(conn)
        conn.execute("PRAGMA temp_store=FILE")
        conn.execute(f"CREATE TEMP TABLE staging (source INTEGER, {', '.join(COLUMNS)})")
        unreadable = []
        for source, path in enumerate([db, *backups]):
            say(f"Reading {path} ...")

            def take(rows, s=source):
                conn.executemany(f"INSERT INTO staging VALUES ({', '.join('?' * (len(COLUMNS) + 1))})",
                                 [(s, *r) for r in rows])
            count, bad = read_events(path, take)
            lost = sum(b - a + 1 for a, b in bad)
            say(f"  {count} events read" + (f"; {lost} row(s) couldn't be read (rows {_ranges(bad)})" if lost else ""))
            if source == 0:
                unreadable = bad
        cols = ", ".join(COLUMNS)
        readable = conn.execute(f"SELECT count(*) FROM (SELECT DISTINCT {cols} FROM staging WHERE source = 0)").fetchone()[0]
        conn.execute(f"INSERT INTO events ({cols}) SELECT DISTINCT {cols} FROM staging ORDER BY timestamp")
        conn.execute("DROP TABLE staging")
        conn.commit()
        total = conn.execute("SELECT count(*) FROM events").fetchone()[0]
        found = [r[0] for r in conn.execute("PRAGMA integrity_check(5)").fetchall()]
        if found != ["ok"]:
            raise RepairError(f"the new database didn't pass SQLite's check: {'; '.join(found)}")
    finally:
        conn.close()
    damaged = f"{db}.damaged-{stamp}"
    moved = []
    try:
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(db + suffix):
                os.replace(db + suffix, damaged + suffix)
                moved.append(suffix)
        os.replace(new, db)
    except OSError as e:
        for suffix in reversed(moved):                  # (put back what was moved: nothing half done)
            try:
                os.replace(damaged + suffix, db + suffix)
            except OSError:
                pass
        raise RepairError(f"the database couldn't be swapped ({e}): is something still using it - the services, the "
                          f"data viewer? The repaired one is {new}") from None
    lost = sum(b - a + 1 for a, b in unreadable)
    say(f"Done: {db} holds {total} events: {readable} read from it, {total - readable} more from the backups"
        + (f" ({lost} of its rows couldn't be read)" if lost else "") + f". The damaged one is {damaged}.")
    return {"events": total, "read": readable, "from_backups": total - readable, "unreadable": lost, "damaged": damaged}


def _ranges(spans) -> str:
    text = ", ".join(f"{a}-{b}" if a != b else str(a) for a, b in spans[:5])
    return text + (f" and {len(spans) - 5} more ranges" if len(spans) > 5 else "")


def main(argv=None) -> int:
    from src.core import paths
    ap = argparse.ArgumentParser(description="Repair a damaged events database (data/events.db).")
    ap.add_argument("--db", default=paths.EVENTS_DB, help=f"the database (default: {paths.EVENTS_DB})")
    ap.add_argument("--backup", action="append", default=[],
                    help="an older copy of it, to fill in what can't be read (the OneDrive backup's events.db)")
    ap.add_argument("--check", action="store_true", help="only check it")
    args = ap.parse_args(argv)
    try:
        found = check(args.db)
    except RepairError as e:
        print(f"Couldn't check it: {e}")
        return 2
    if args.check:
        print(f"{args.db}: " + ("sound" if not found else "DAMAGED:\n  " + "\n  ".join(found)))
        return 0 if not found else 1
    if not found and not args.backup:
        print(f"{args.db} is sound: nothing to repair.")
        return 0
    print(f"{args.db} " + (f"is damaged ({found[0]}{' ...' if len(found) > 1 else ''})." if found else
                           "is sound: the backups' events are added to it."))
    from src.launcher import programs
    if programs.manager_running():
        print("The services are running: stop them first (the launcher's Stop services...), and close the data "
              "viewer, then run this again.")
        return 1
    try:
        repair(args.db, args.backup)
    except RepairError as e:
        print(f"Not repaired: {e}")
        return 1
    print("Start the services again when you're ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
