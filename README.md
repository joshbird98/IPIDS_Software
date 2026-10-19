# SINDIE software

Control and data system for the SINDIE ion implanter (Surrey Ion Beam Centre):
hardware services that talk to the PLC and the supplies, the operator HMI, and the
data viewer for the logged telemetry.

(The machine used to be called IPIDS. The old `IPIDS_*` environment variables still work, and
the HMI and viewer pick up their saved layouts and export settings from the old name once.)

```
src/services/       one process per device (PLC, Spellman, magnet, steerer, SMU, turbo, gauges)
                    plus logger, compactor, events database, readback monitor, sequencer, alarms
                    and the service manager
src/drivers/        the device wire protocols (Modbus, SCPI, USS, RS485) used by the services
src/sequencer/      recipes, mass scans and the optimiser (run by service_sequencer)
src/alarms/         alarm states and the rules that raise them (run by service_alarms)
src/gui/control/    operator HMI (python src/hmi.py)
src/gui/data_viewer data viewer (python src/gui/data_viewer/viewer_main.py)
src/core/           shared code: device service base, commands, leases, telemetry cache, paths,
                    logging, recipe steps, config checking
src/sim/            simulated devices (see below)
config/             configuration files (JSON)
plc/                PLC interface definition (PLC_PC_Interface.scl)
tests/              automated tests (pytest)
testing/            hand-run scripts and tools
```

## Running on the lab PC

```
python src/services/service_manager.py      starts and supervises every service
python src/hmi.py                           operator HMI
python src/gui/data_viewer/viewer_main.py   data viewer
```

In the data viewer, double-click the plot to add a marker. Hover over a marker line for its full
text; right-click it to delete a user marker. Zoomed out, events within a few pixels of each
other are drawn as one line ("12 events"), and labels that don't fit (or would sit under the
legend) are left off but still show on hover. A marker that doesn't reach the event database
within a few seconds (events service not running for that viewer) is taken off with a warning.
The export dialog starts from the Export Settings folder/name in the sidebar and writes its
choice back there.

The manager restarts a crashed service with increasing delays (2 s ... 5 min) and shows
BACKOFF / STOPPED in the HMI's services panel, where services can also be stopped and started.

### Sequences and control leases

Recipes, mass scans and the optimiser run in `service_sequencer`, not in the HMI: closing or
restarting the HMI doesn't stop them, and the HMI panels only start, pause, stop and display them.
A sequence claims a **control lease** on each subsystem it drives (the analyser magnet, the
einzel, ...); while it holds one, that subsystem's service takes commands for it only from the
sequence. The Automation bar at the top of the HMI shows which sequence holds what, and the
source panel marks a leased supply "🤖 SEQUENCE". Leases are renewed every second and lapse after
5 s, so a sequencer that dies gives control back by itself. The operator can always take control
(the Automation bar's Take control button, clicked twice): a recipe then pauses (Resume repeats
the step it was on), a scan or optimiser run stops.
Safety-relay, shutdown, fault-reset and gate-valve controls can never be leased.

Every service also publishes, per subsystem, whether an operator command would be accepted now
(`<subsystem>.permit`) and why not (`permit_reason`: relay off, PLC in control, cesium hot,
leased, ...); the HMI greys out those controls with the reason as their tooltip.

### Alarms

`service_alarms` turns PLC fault bits, comms losses, stale services, vacuum set points, arcs
and failed sequences into alarms (rules and per-alarm overrides in `config/alarm_config.json`).
They follow ISA-18.2: an alarm is unacknowledged until the operator acknowledges it, even if it
has cleared (it then flashes until acknowledged); shelving one needs a reason and expires; the
first PLC trip after a reset is marked first-out (⭐); gauge comms alarms are suppressed while
their controller's own comms alarm is active. Acknowledgements and shelves survive a restart
(`data/alarm_state.json`). In the HMI's Alarms dock the horn sounds the most urgent
unacknowledged alarm (priority 1-3; 4 is listed only) until it is acknowledged or silenced; a new
alarm sounds again. If the alarm service itself isn't running the HMI raises its own alarm.

## Simulator (no hardware needed)

```
python run_simulation.py
```

Starts every service against simulated devices, then the HMI and the viewer. It uses its own
ports and keeps its data and logs in `data_sim/`, so it can run on the lab PC next to the real
system without touching it. The window it runs in has a menu for injecting faults (PLC lost,
safety relay trip, Spellman arc, magnet 10 % low, ...). As on the real machine, open the gate
valve and energise the safety relay before the supplies accept commands.

The simulated source behaves roughly like the real SNICS source (details in `src/sim/plc.py`):
closing the gate valve trips the safety relay (latched: supplies off, fault reset and enable the
relay again); the thermionic supply sits at 2500 V and its current comes from the filament
(~3 mA at 25 A, ~150 mA at 29 A), with auto-emission driving the filament; the cesium oven heats
to its setpoint, forced cooling takes it towards 20 °C; extraction and target draw 0.02 mA/kV
through their bleed resistors, and beam (target current, ~10 % of it extracted, and peaks on the
Faraday cup) appears once thermionic > 200 W, target > 4 kV and cesium > 50 °C. The einzel draws
under 1 µA, noisily.

Each program also takes `--sim` on its own, e.g. `python src/hmi.py --sim`.

## Tests

```
pip install -r requirements-dev.txt
python -m pytest -m "not slow"     about a minute
python -m pytest                   also starts the whole simulated system and the HMI (offscreen)
```

They run on their own ports, in a temporary data folder and with a tag registry built fresh
from the config files, so they are also safe to run on the lab PC while the system is running.
GitHub runs them on every push (`.github/workflows/tests.yml`).

## Configuration

The HMI's config editor (Config in the menu bar, engineering password) checks a file when it
opens it and again before applying a change (types, physical ranges, limits the right way round,
duplicate ports, gauge set points the controller can store, ...), lists the changes for
confirmation with a reason - a number that changes by more than a factor of 3 needs an extra tick -
and refuses if the file was changed on disk since it was opened. It rewrites only the changed
values, keeps the previous version in `config/backups/` (not in git; "History..." puts one back),
commits the file to git (so the lab PC gets local commits - pull with `git pull --rebase`),
records a CONFIG_CHANGE event, rebuilds the tag registry where needed (and puts the old file back
if that fails), and restarts what reads the file - a service stopped on purpose is left stopped;
after a change to `network_config.json` it restarts nothing and asks for the manager and the HMI to
be restarted.
Names (keys) can't be changed there: the services look supplies, gauges and ports up by name.
Some values have **hard caps** (`src/core/hard_caps.py`): VG1/VG2 relay points 1e-5 mbar (they open
the gate valve), other gauge relays 1 mbar, every Spellman 22 kV, magnet 95 A / 25 V, steerer
80 A / 40 V, SMU ±200 V. Past one, the editor refuses the file and marks the field red with the
reason; the registry's setpoint limits stay within them; and the services check them again at
start-up and on every command. Raising a cap is a code change.

- `config/system_tags.json` (the tag registry, not in git) is **generated** from the other config
  files and `plc/generated/PLC_PC_Interface.scl` by `python src/utils/build_registry.py`. Re-run it
  after changing limits in `mpd_config.json`, `magnet_config.json`, `steerer_magnet_config.json`,
  `smu_config.json`, `vac_gauges_config.json`, `plc_config.json` or `fault_config.json` (the HMI's
  config editor does this itself), then restart the services and the HMI.
- `mpd_config.json`: Spellman supplies - voltage and current-limit ranges, and the software arc
  detection (`arc_detection`: an output collapsed below 10 % of its setpoint on two polls in a row
  latches an arc fault until fault reset; not judged below 0.5 kV).
- `config/recipes/`: the recipes (in git). The HMI's recipe panel and builder read and save
  here; a sub-recipe is looked up in the same folder.
- Mass calibration: the Mass Scan panel fits `AMU = k (I + I_off)^2 / E` to peaks you mark
  (Ctrl+click, then "Calculate Fit"); "Calculate & set amps" in Beamline Optics uses that fit and
  the live beam energy (target + extraction).
- `readback_tolerance.json`: readback-vs-setpoint tolerances. Out of tolerance is information,
  not a fault: an amber outline on the readback in the HMI and a TOLERANCE event. Edits are
  picked up while running.
- `alarm_config.json`: alarm rules and overrides (priority, delays, disabling one alarm or a
  pattern such as `plc.*_Comms_Fail`).
- `workspace_profiles.json`, `plot_settings.json`: the data viewer's saved layouts and settings
  (per machine, not in git).

## Logs and records

- `SystemLogs/<process>.log`: one rotating log per process, the HMI and the viewer included
  (warnings, errors, anything unhandled; programs log rather than print).
- `data/events.db` (SQLite), table `events`, by `event_type`:
  `GENERAL`, `FAULT`, `ALARM` (raised / cleared / acknowledged / shelved), `CONFIG_CHANGE`,
  `PHASE_CHANGE`, `USER_MARKER`, `WATCHDOG_TRIP/CLEAR`,
  `TOLERANCE` (readback out of / back in tolerance) and `COMMAND` - the audit trail: every
  command sent by the HMI, recipes, optimiser or scans, with what the service answered
  (ok / rejected + reason / failed). There are a lot of COMMAND rows; filter them out with
  `WHERE event_type != 'COMMAND'`. The viewer shows them only when its "Operator Commands"
  overlay is ticked.
- `data/parquet_logs/`: 15-minute telemetry chunks, plus `logger_journal.bin` (the logger's
  crash journal, so at most ~5 s is lost if it dies); `data/daily_logs/`: the nightly compacted
  day files. Machine data is not kept in git. If the disk won't take a chunk (full, permissions),
  the logger keeps it in RAM and tries again every minute, for up to 2 h of data (then the oldest
  goes, with a CRITICAL event); it warns when the data drive has less than 2 GB free.
