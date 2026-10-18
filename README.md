# IPIDS software

Control and data system for the IPIDS ion implanter (Surrey Ion Beam Centre):
hardware services that talk to the PLC and the supplies, the operator HMI, and the
data viewer for the logged telemetry.

```
src/services/       one process per device (PLC, Spellman, magnet, steerer, SMU, turbo, gauges)
                    plus logger, compactor, events database, readback monitor and the service manager
src/gui/control/    operator HMI (python src/hmi.py)
src/gui/data_viewer data viewer (python src/gui/data_viewer/viewer_main.py)
src/core/           shared code: commands, telemetry cache, paths, logging, recipes
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

The manager restarts a crashed service with increasing delays (2 s ... 5 min) and shows
BACKOFF / STOPPED in the HMI's services panel, where services can also be stopped and started.

## Simulator (no hardware needed)

```
python run_simulation.py
```

Starts every service against simulated devices, then the HMI and the viewer. It uses its own
ports and keeps its data and logs in `data_sim/`, so it can run on the lab PC next to the real
system without touching it. The window it runs in has a menu for injecting faults (PLC lost,
safety relay trip, Spellman arc, magnet 10 % low, ...). As on the real machine, open the gate
valve and energise the safety relay before the supplies accept commands.

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

## Logs and records

- `SystemLogs/<process>.log`: one rotating log per process (warnings, errors, anything unhandled).
- `data/events.db` (SQLite), table `events`, by `event_type`:
  `GENERAL`, `FAULT`, `PHASE_CHANGE`, `USER_MARKER`, `WATCHDOG_TRIP/CLEAR`,
  `TOLERANCE` (readback out of / back in tolerance) and `COMMAND` - the audit trail: every
  command sent by the HMI, recipes, optimiser or scans, with what the service answered
  (ok / rejected + reason / failed). There are a lot of COMMAND rows; filter them out with
  `WHERE event_type != 'COMMAND'`. The viewer shows them only when its "Operator Commands"
  overlay is ticked.
- `data/parquet_logs/`: 15-minute telemetry chunks, plus `logger_journal.bin` (the logger's
  crash journal, so at most ~5 s is lost if it dies); `data/daily_logs/`: the nightly compacted
  day files. Machine data is not kept in git.
