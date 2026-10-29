"""A damaged events database: found, and said so (the events service's log, the data viewer, the
nightly backup, which keeps the last sound copy), and repaired (src/utils/repair_events_db.py) -
every event that can still be read, with a backup filling in the rest - without deleting anything.

(October 2026: a laptop's events.db, damaged by a copy put over it while it was in use, returned
some events and not others, so the data viewer's came and went as it was panned and zoomed.)"""
import logging
import os
import random
import sqlite3

import pytest

from src.core import events_db
from src.core.log_setup import Throttle
from src.utils import repair_events_db as repair

T0 = 1793000000.0
TYPES = ("USER_MARKER", "FAULT", "COMMAND", "WATCHDOG_TRIP", "TOLERANCE")
COLUMNS = "timestamp, service, severity, event_type, message, metadata"


def make_db(path, n=20000):
    conn = sqlite3.connect(path)
    events_db.create_schema(conn)
    rng = random.Random(1)
    conn.executemany(f"INSERT INTO events ({COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)",
                     [(T0 + i, "service_x", "INFO", TYPES[i % len(TYPES)], f"event {i} " + "x" * rng.randint(0, 200),
                       '{"n": %d}' % i) for i in range(n)])
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()


def damage(path, pages=(40, 41, 42, 43, 60)):
    """Garbage over some of the table's pages, as a copy put over a database in use can leave."""
    size = os.path.getsize(path)
    with open(path, "r+b") as f:
        for page in pages:
            assert (page + 1) * 4096 < size
            f.seek(page * 4096)
            f.write(random.Random(page).randbytes(4096))


def events(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT {COLUMNS} FROM events ORDER BY timestamp").fetchall()
    finally:
        conn.close()


def says_damaged(text):
    """What SQLite says of a damaged page: its check's finding, or the error it stopped at."""
    return "Tree" in text or "malformed" in text


def copy(src, dst):
    with open(src, "rb") as a, open(dst, "wb") as b:
        b.write(a.read())
    return dst


@pytest.fixture
def db(tmp_path):
    """A sound events.db, and a copy of it taken before it was damaged (the backup)."""
    path = str(tmp_path / "events.db")
    make_db(path)
    return path, copy(path, str(tmp_path / "backup_events.db"))


def test_a_damaged_database_is_found(db):
    path, _backup = db
    assert repair.check(path) == []
    damage(path)
    found = repair.check(path)
    assert found and says_damaged(" ".join(found))
    conn = sqlite3.connect(path)
    try:
        assert says_damaged(events_db.check(conn))
    finally:
        conn.close()
    assert events_db.damaged(sqlite3.DatabaseError("database disk image is malformed"))
    assert not events_db.damaged(sqlite3.OperationalError("database is locked"))


def test_what_can_be_read_is_kept_and_the_backup_fills_in_the_rest(db):
    path, backup = db
    whole = events(backup)
    damage(path)
    said = []
    report = repair.repair(path, [backup], say=said.append, stamp="20261029-100000")
    assert repair.check(path) == [] and events(path) == whole        # every event, once, in time order
    assert report["events"] == len(whole) and report["unreadable"] > 0
    assert 0 < report["read"] < len(whole) and report["read"] + report["from_backups"] == len(whole)
    assert os.path.exists(path + ".damaged-20261029-100000")          # (kept, not deleted)
    assert any("couldn't be read" in s for s in said)


def test_without_a_backup_the_rest_is_kept(db):
    path, backup = db
    whole = set(events(backup))
    damage(path)
    report = repair.repair(path, say=lambda s: None)
    got = events(path)
    assert set(got) < whole and len(got) == report["events"] > len(whole) * 0.9
    conn = sqlite3.connect(path)                    # (with the events service's indexes)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    conn.close()
    assert {"idx_timestamp", "idx_type_timestamp"} <= names


def test_events_from_a_newer_copy_are_added_too(db, tmp_path):
    """The laptop's case: its own events, and the lab's newer ones from the OneDrive backup."""
    path, _backup = db
    newer = str(tmp_path / "newer.db")
    make_db(newer, n=25000)                         # the same first 20000, and 5000 more
    damage(path)
    repair.repair(path, [newer], say=lambda s: None)
    assert events(path) == events(newer)


def test_a_database_in_use_is_left_as_it_was(db, monkeypatch):
    path, backup = db
    damage(path)
    before = open(path, "rb").read()
    real = os.replace

    def busy(a, b):
        if a.endswith(".repaired"):
            raise PermissionError("The process cannot access the file because it is being used by another process")
        return real(a, b)
    monkeypatch.setattr(repair.os, "replace", busy)
    with pytest.raises(repair.RepairError, match="still using it"):
        repair.repair(path, [backup], say=lambda s: None)
    assert open(path, "rb").read() == before and os.path.exists(path + ".repaired")
    assert not [f for f in os.listdir(os.path.dirname(path)) if ".damaged-" in f]       # (all put back)


def test_the_command_line(db, monkeypatch, capsys):
    from src.launcher import programs
    path, backup = db
    assert repair.main(["--check", "--db", path]) == 0 and "sound" in capsys.readouterr().out
    damage(path)
    assert repair.main(["--check", "--db", path]) == 1 and "DAMAGED" in capsys.readouterr().out
    monkeypatch.setattr(programs, "manager_running", lambda: True)
    assert repair.main(["--db", path]) == 1 and "stop them first" in capsys.readouterr().out
    monkeypatch.setattr(programs, "manager_running", lambda: False)
    assert repair.main(["--db", path, "--backup", backup]) == 0
    assert repair.check(path) == [] and events(path) == events(backup)


def _event_logger(path):
    from src.services.service_events import EventLogger
    logger = EventLogger.__new__(EventLogger)       # (without its sockets)
    logger.db_path = path
    logger._open_db()
    logger._throttle, logger._damage_report = Throttle(60.0), Throttle(3600.0)
    return logger


def test_the_events_service_says_so(db, caplog, monkeypatch):
    path, _backup = db
    damage(path)
    logger = _event_logger(path)
    with caplog.at_level(logging.INFO, logger="service_events"):
        logger.check_in_background().join(30)      # (as run() starts it)
    critical = [r.getMessage() for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(critical) == 1 and "repair_events_db" in critical[0] and says_damaged(critical[0])

    class Damaged:                                  # an insert that meets the damaged pages
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def executemany(self, *_):
            raise sqlite3.DatabaseError("database disk image is malformed")

        def close(self):
            pass
    caplog.clear()
    monkeypatch.setattr(logger, "_open_db", lambda: None)
    logger.conn = Damaged()
    with caplog.at_level(logging.ERROR, logger="service_events"):
        for _ in range(3):
            assert logger.handle_events([{"ts": T0, "type": "GENERAL", "message": "m"}]) == 0
    critical = [r.getMessage() for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(critical) == 1 and "repair_events_db" in critical[0]        # (once an hour, not every insert)


def test_a_sound_database_is_not_reported(db, caplog):
    path, _backup = db
    logger = _event_logger(path)
    with caplog.at_level(logging.INFO, logger="service_events"):
        logger.check_in_background().join(30)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_the_nightly_backup_keeps_the_last_sound_copy(db, tmp_path, caplog):
    from src.services.service_data_compactor import OneDriveArchiver
    path, _backup = db
    onedrive = tmp_path / "OneDrive"
    archiver = OneDriveArchiver(local_dir=str(tmp_path / "daily"), onedrive_dir=str(onedrive))
    assert archiver.sync_database(path)
    kept = (onedrive / "events.db").read_bytes()
    assert events(str(onedrive / "events.db")) == events(path)
    os.utime(path, (os.path.getmtime(path) + 60,) * 2)
    damage(path)
    with caplog.at_level(logging.ERROR, logger="service_data_compactor"):
        assert not archiver.sync_database(path)
    assert (onedrive / "events.db").read_bytes() == kept           # (not replaced, and not archived)
    assert not list((onedrive / "_Archive_Versions").iterdir())
    critical = [r.getMessage() for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(critical) == 1 and "still the last sound one" in critical[0]


@pytest.fixture
def qt_app():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_the_data_viewer_says_so(db, qt_app):
    from PyQt6.QtWidgets import QLabel
    from src.gui.data_viewer.viewer_markers import MarkersMixin
    path, _backup = db

    class Viewer(MarkersMixin):
        def __init__(self):
            self.events_warning = QLabel()
            self.events_warning.hide()
    sound, broken = Viewer(), Viewer()
    sound._check_events_db(path)
    sound._show_events_damage()
    assert sound.events_warning.isHidden()
    damage(path)
    broken._check_events_db(path)
    broken._show_events_damage()
    assert not broken.events_warning.isHidden()
    assert "events.db is damaged" in broken.events_warning.text()
    assert "repair_events_db" in broken.events_warning.text() and says_damaged(broken.events_warning.toolTip())
