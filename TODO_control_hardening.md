# Control hardening - status and to-do

Branch `claude/control-hardening` (from `claude/viewer-stability`). Paused at the usage limit.

## Done (code + tests written; `pytest -m "not slow"` passes, the sim-stack end-to-end test passed)
- Acknowledged commands (ROUTER/DEALER) for every service and the manager: ok / accepted / rejected+reason / failed,
  plus timeout / unreachable on the client side. Stale-command rejection kept (0.5 s; SMU scales with NPLC).
  `src/core/commands.py`; HMI `ZMQCommandThread.send_command()` keeps its signature and returns a ticket.
- HMI: Command Log dock + pop-ups for refused/failed commands; stale telemetry hidden (`---`) via
  `src/core/telemetry_cache.py`; arm-then-confirm on HV/source/magnet/steering enables and degauss
  (`src/gui/control/arming.py`); amber readback when out of tolerance; services panel shows BACKOFF/STOPPED with Stop/Start.
- Recipe: Abort renamed Stop (just stops stepping, nothing reverted). Steps wait for the service's ack and fail with its
  reason; waits only accept live values; routing by registry (fixes VERIFIED_ACTION going to Spellman).
- Spellman enable-latch bug fixed (relay off -> disabled; supply silent -> unknown; no 5 s blocking polls).
- Services keep heartbeating while their device is unreachable (stops the restart loops); manager restart back-off
  (2 s ... 5 min, reset after 2 min stable), honours boot_grace, stops children on SIGTERM.
- Logger crash journal (<= 5 s loss instead of 15 min), compactor merges late fragments instead of overwriting a day.
- Readback tolerance monitor `service_monitor.py` + `config/readback_tolerance.json` (TOLERANCE events, not faults).
- Audit trail: every command outcome in events.db as event type COMMAND (viewer overlay checkbox, off by default).
- PLC: batched writes honour `writable`; commands may only write cmd_/sp_requested_ tags (status/permissive tags refused).
- Logging: rotating per-process logs in SystemLogs/, swallowed errors now logged.
- Simulation mode (IPIDS_SIMULATE=1 / `--sim`): fake PLC, Spellman, magnets, turbo, SMU (with a mass spectrum),
  gauges; data in data_sim/; runtime fault injection via data_sim/sim_faults.txt.

## To do next
1. Run and fix `tests/test_hmi_sim.py` (HMI offscreen against the sim stack; currently skipped - never run).
2. Re-run `testing/viewer_smoke_test.py` (viewer now uses src/core/paths.py and has two new overlay checkboxes).
3. `run_simulation.py` at the project root: start manager --sim + HMI (+ viewer), refuse if a real manager is running,
   simple fault-injection menu.
4. Repo hygiene: requirements.txt is a UTF-16 full `pip freeze` -> runtime `requirements.txt` + `requirements-dev.txt`
   (pytest, pytest-timeout); untrack the ~600 MB parquet data (`git rm --cached`, .gitignore, safe-checkout notes);
   delete stale `src/gui/data_viewer/temp.py`.
5. GitHub Actions CI: `pytest -m "not slow"` on push (+ slow suite optionally).
6. Remaining broad/bare excepts: `src/gui/data_viewer/config_manager.py`, `data_engine.sync_active_chunks`.
7. Short README section: running the simulator, the tests, where logs/audit live.
8. Lab PC validation (real hardware): every service connects and acks; Spellman relay trip shows DISABLED;
   unplugged device -> service stays ONLINE with comms-fail (no restart loop); turbo start/stop; SMU connect timeout;
   logger journal file appears in data/parquet_logs; tolerance rules sensible for the real supplies.

## Decisions needed from Josh
- Spellman arc detection compares volts with kV (`volts_rb < sp_kV * 0.25`), so it only fires on total collapse.
  Kept as-is; make it a real 25 %-of-setpoint test? (It feeds a PLC fault, so needs care on hardware.)
- Spellman current limit: the HMI's automatic 50 uA write was always silently refused (no limits in the registry).
  Add `"min_current_ua"`/`"max_current_ua"` to config/mpd_config.json and rebuild system_tags.json if you want it.
- Steerer magnet service has no safety-relay interlock (main magnet and SMU do) - intended?
- SMU re-applies its last bias automatically after a reconnect - intended?
- If the ADAM module is repaired: its manager key must be `service_adam_monitor` (matches its heartbeat name).
