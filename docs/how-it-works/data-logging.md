# Data logging and storage

How SINDIE records the machine: the telemetry logger and its crash journal, the nightly compaction and backup, the events database, the programs' log files, and where each of them is on disk.

## The telemetry logger

`service_logger` listens to the eight device topics (`PLC_DATA`, `VACUUM_DATA`, `SRC_TURBO_DATA`, `SPELLMAN_DATA`, `MAGNET_DATA`, `STEEER_MAGNET_DATA`, `SMU_DATA`, `ADAM_DATA`) and writes a row 20 times a second.

- **What is in a row.** One column for every tag in the tag registry, plus the time. Each row holds the latest value of every channel (zero-order hold), so a value published faster than 20 times a second is sampled, not kept in full. Values the registry doesn't know, such as the permits and the decoded fault bits, aren't logged; the PLC's fault words are.
- **Values.** Stored as 32-bit floats; on/off values as 1.0 and 0.0. A value a service publishes as unknown (null) is written as NaN, a gap, never as the last known value.
- **A service that goes quiet.** If a topic has been silent for more than 1 s, its channels are written as NaN and a WATCHDOG_TRIP event is logged; WATCHDOG_CLEAR follows when it comes back.
- **At start-up** the first row is all NaN, so a plot never joins the data across a restart of the logger.

### Chunks

The rows build up in memory and are written every 15 minutes as one Parquet file (zstd compression) in `data/parquet_logs/`, named `chunk_YYYYmmdd_HHMMSS.parquet` after the time it was written. A chunk is written under a temporary name (`..._temp.parquet`) and renamed when it is complete, so a half-written file is never taken for data.

The data viewer, when it starts, and an export that reaches the newest data ask the logger to write what it holds in memory first (port 5554, `FORCE_FLUSH`), so the history meets the live data without a gap. Such a flush makes a shorter chunk. The logger waits up to 2 s for the file to be written, and turns down a second request within 2 s of the last.

### The crash journal

Without a journal, a crash or a power cut would lose up to 15 minutes of rows. So every 5 s the logger appends the rows logged since to `data/parquet_logs/logger_journal.bin` and forces them to disk; once a chunk is safely written, the journal starts afresh. When the logger starts, any rows left in the journal that aren't already in a chunk become `chunk_..._recovered.parquet`. At most about 5 s is lost.

A journal that can't be turned into a chunk at start-up is kept as `logger_journal.bin.unrecovered_<time>` and tried again at the next start. One that can't be read at all is moved aside, with `.bad_<time>` added to its name.

### When the disk won't take data

If a chunk can't be written (disk full, permissions), the logger keeps it in memory and tries again every minute, oldest first, with an ERROR event at most every 15 minutes. It keeps up to 2 hours of waiting data; beyond that the oldest chunk is dropped, with a CRITICAL event. An event says when the disk takes data again. The logger also warns (a WARNING event, at most once an hour) when the data drive has less than 2 GB free. If it is stopped while data is still waiting, it logs a CRITICAL event: only what the crash journal holds is recovered at the next start.

### Settings

`config/logger_config.json`, read when the logger starts:

| Key | Value | Meaning |
|---|---|---|
| `tick_rate_seconds` | 0.05 | time between rows (20 rows a second) |
| `flush_interval_seconds` | 900 | time between chunks (15 minutes) |
| `journal_interval_seconds` | 5 | time between crash-journal writes |
| `buffer_overhead_multiplier` | 1.1 | spare room in the in-memory buffer; if it fills, the logger writes a chunk early with a WARNING event |

## The nightly compaction

At 02:00 every night (the PC's local time), `service_data_compactor` turns each earlier day's chunks into one day file:

1. It deletes leftover temporary chunk files from earlier days.
2. For each day before today that has chunks, it merges them into `data/daily_logs/day_YYYYMMDD.parquet` (zstd, level 10), sorted by time with duplicate rows removed. If the day file already exists (rows recovered from the journal after a restart, say), the late chunks are merged into it.
3. It writes two rollups next to the day file: `day_YYYYMMDD_macro_1m.parquet` and `day_YYYYMMDD_macro_20m.parquet`, with the minimum and maximum (no mean) of every channel in 1-minute and 20-minute bins.
4. Only then does it delete that day's chunks. A day that fails keeps its chunks for the next night.

If the PC is off at 02:00, nothing is lost: the next run compacts every earlier day still in chunks. Nothing in the software deletes old day files or rollups.

### The OneDrive backup

After compacting, the compactor copies to a OneDrive folder (set as `ONEDRIVE_PATH` in `src/services/service_data_compactor.py`):

- the day files and rollups in `data/daily_logs/`, new or changed ones only;
- a snapshot of `data/events.db`, taken with SQLite's own backup so it is consistent while the database is in use, and checked with SQLite's quick check before it replaces the last backup (a damaged one doesn't: see [Events come and go in the data viewer](../troubleshooting.md#events-come-and-go-in-the-data-viewer));
- everything in `data/exports/`.

The copy is one way: deleting a file locally doesn't delete it in OneDrive. When a file in OneDrive is replaced by a newer one, the older copy is moved to `_Archive_Versions` there, with the time in its name. The simulator's compactor skips the backup.

### How the data viewer reads it

The data viewer reads the chunks, the day files and the rollups for the view and half a view either side, choosing by the time on screen: up to 12 hours, the logged rows (thinned to 8000 points, keeping each bin's minimum and maximum); up to 10 days, the 1-minute rollups; longer, the 20-minute rollups, which it holds in memory. Where a day has no rollups yet (not compacted), it reads the logged rows instead, for views of up to 3.5 days. For the live view it listens to the same eight topics and keeps the last 30 minutes of each. See [Using the data viewer](../viewer/data-viewer.md).

## The events database

`service_events` writes every event that any program publishes to `data/events.db` (SQLite), table `events`:

| Column | Holds |
|---|---|
| `id` | row number |
| `timestamp` | Unix time, seconds |
| `service` | the program that sent it |
| `severity` | INFO, WARNING, ERROR or CRITICAL; for a PLC fault being set, the fault's severity number (1 or 2) from `config/fault_config.json` |
| `event_type` | see below |
| `message` | the text |
| `metadata` | details, as JSON |

| Event type | Written by | When |
|---|---|---|
| GENERAL | any program | start-up, connections, warnings and other messages |
| FAULT | `service_plc` | a PLC fault bit is set ("FAULT ACTIVATED: ...") or clears |
| ALARM | `service_alarms` | an alarm goes active or clears, is acknowledged, shelved or unshelved |
| COMMAND | every service that takes commands | each command and the service's answer: the audit trail |
| CONFIG_CHANGE | the HMI's config editor | a config file is changed or put back |
| PHASE_CHANGE | the HMI, **SET NEW PHASE** | the start of a new phase of work |
| USER_MARKER | the HMI (**ADD MARKER**, and notes from the **Beamline Optics** and **Mass Scan** panels), the data viewer and the optimiser | an operator's marker; a marker can be deleted again from the data viewer |
| WATCHDOG_TRIP, WATCHDOG_CLEAR | `service_logger` | a telemetry topic silent for more than 1 s, and back |
| TOLERANCE | `service_monitor` | a readback out of tolerance, and back |

The database runs in WAL mode, so the data viewer, exports and the nightly backup can read it while it is written. Events that arrive together are written in one transaction. There are many COMMAND rows: leave them out of a query with `WHERE event_type != 'COMMAND'`. The data viewer shows commands only when its **Operator Commands (audit)** overlay is ticked.

### When it is damaged

SQLite keeps the database's recent changes in `events.db-wal` beside it (with an index of them in `events.db-shm`) until it folds them into `events.db`. The three belong together: a copy of `events.db` put over the one in use, while a program has it open, leaves the `-wal` and `-shm` of the other database beside it, and the database is damaged. SQLite then reads some parts of it and not others ("database disk image is malformed"), so the data viewer's events come and go, and the events service can't save new ones.

The events service checks the database when it starts, and says so in its log when the check or a write finds it damaged (at most once an hour). The data viewer checks it once when it opens it, off its window's thread, and says so under **Event markers**. `src/utils/repair_events_db.py` repairs it: see [Events come and go in the data viewer](../troubleshooting.md#events-come-and-go-in-the-data-viewer). The table, its indexes and the check are in `src/core/events_db.py`.

## Log files

Each program writes its own log, `SystemLogs/<program>.log`: `service_manager.log`, one per service, `hmi.log` and `viewer.log`. They hold warnings, errors and the full traceback of anything unhandled, so a crash that made the service manager restart something can be explained afterwards. A file is rotated at 5 MB and five old copies are kept.

The command audit trail is written to each service's log file too, but not to its console.

## Where everything is on disk

| Path | What |
|---|---|
| `data/parquet_logs/` | today's chunks (and any not yet compacted), the crash journal |
| `data/daily_logs/` | day files and their 1-minute and 20-minute rollups |
| `data/events.db` | the events database (with `events.db-wal` and `events.db-shm` while in use); a damaged one a repair has replaced is kept beside it as `events.db.damaged-<time>` |
| `data/exports/` | the data viewer's exports |
| `data/alarm_state.json` | alarm acknowledgements, shelves and first-out |
| `SystemLogs/` | each program's log file |
| `config/backups/` | previous versions of config files changed in the HMI |
| OneDrive folder | the nightly backup |

`.gitignore` keeps machine data out of git. Environment variables move these folders, which is how the simulator and the tests keep apart from the machine's data:

| Variable | Effect |
|---|---|
| `SINDIE_DATA_DIR` | the data folder, instead of `data/`; the logs then go to `SystemLogs/` inside it |
| `SINDIE_LOG_DIR` | the folder for the log files |
| `SINDIE_SIMULATE=1` | simulation: the data folder is `data_sim/`, with its logs in `data_sim/SystemLogs/` |

The older `IPIDS_` names of these variables still work. A variable is read when a program starts, and the service manager passes its own to every service.

## See also

- [Using the data viewer](../viewer/data-viewer.md)
- [Exporting data and figures](../viewer/exporting.md)
- [Recording an experiment](../procedures/recording-an-experiment.md)
- [Services and messaging](services.md)
- [Troubleshooting](../troubleshooting.md)
