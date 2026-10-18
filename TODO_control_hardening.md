# Control hardening - status and to-do

Branch `claude/control-hardening` (from `claude/viewer-stability`). Everything here runs against the
simulator; none of it has been tried on the real hardware yet (see "Lab PC validation").

## After pulling this branch on the lab PC
1. `python src/utils/build_registry.py` - rebuilds config/system_tags.json (picks up the new Spellman
   current limits). Without it nothing breaks, the HMI just keeps not sending the 50 uA current limit.
2. Restart the services and the HMI.

## Done
- Acknowledged commands (ROUTER/DEALER) for every service and the manager: ok / accepted / rejected+reason /
  failed, plus timeout / unreachable on the client side; stale commands still rejected (0.5 s; SMU scales with NPLC).
- HMI: Command Log dock + pop-ups for refused/failed commands; stale telemetry hidden (`---`); arm-then-confirm on
  HV/source/magnet/steering enables and degauss; amber readback when out of tolerance; services panel shows
  BACKOFF/STOPPED with Stop/Start.
- Recipe: Abort renamed Stop (just stops stepping). Steps wait for the service's answer and fail with its reason;
  waits only accept live values; routing by registry.
- Spellman enable-latch bug fixed; services keep heartbeating while their device is unreachable; manager restart
  back-off (2 s ... 5 min); logger crash journal (<= 5 s loss); compactor merges late fragments.
- Readback tolerance monitor (`config/readback_tolerance.json`, TOLERANCE events, not faults); command audit trail
  (event type COMMAND); PLC batched writes honour `writable`; rotating per-process logs.
- Simulator (`run_simulation.py`, fault menu) and tests (`tests/`, CI in `.github/workflows/tests.yml`).
- Josh's decisions (Oct 2026):
  - Spellman arc = output below 10 % of setpoint on 2 polls in a row (so one garbled RS485 reading can't trip
    the PLC), not judged below 0.5 kV. While the output moves after a setpoint change it is judged against the
    lower of the old and new setpoints (so the optimiser's steps don't blind it); after an enable, from when the
    output is up (half the setpoint) or 3 s. Settings in `mpd_config.json` -> `arc_detection`. Latches until
    fault reset (also checked end-to-end into the PLC fault word).
  - Spellman current limits in `mpd_config.json` (0-330 uA, the MPD30's rating, confirmed) - the HMI's automatic
    50 uA limit is now actually sent and accepted.
  - X-steerer: software safety-relay interlock (output forced off and held off while the relay is off or the PLC
    is silent; stays off when the relay comes back).
  - SMU: after a reconnect the previous bias is re-applied only if fresh PLC data confirms the relay is on;
    otherwise 0 V / bias off and the setpoint is forgotten (also if the relay tripped while it was disconnected -
    even a trip-and-reset between two checks, or a gap in the PLC data - or the operator had switched it off).
  - Independent review of the above (Oct 2026) found: arc detection blinded while the setpoint kept stepping, a
    relay trip-and-reset between two checks missed by the SMU/steerer, and (older) a refused Spellman
    enable/disable reported as OK. All fixed, with tests. All four relay-dependent services (Spellman, magnet,
    steerer, SMU) now use the same check, `src/core/relay_watch.py`.
  - Recipes live in `config/recipes/` (in git). The HMI used to save to src/config/recipes/ (not in git); on
    start-up it copies anything there across, never overwriting (a clash is saved as "<name> (from
    src-config).json"). data/recipes/ held stale copies and was removed; its one unique file, an early HV
    conditioning sketch the engine can't run, is in archive/recipe_sketches/.
  - "Calculate & set amps" uses the Mass Scan panel's calibration (AMU = k (I + I_off)^2 / E, from the peaks you
    mark and "Calculate Fit") and the live beam energy (target + extraction), and records which calibration it
    used. (It had always been amps = amu: the placeholder polynomial it meant to use was never found.)
- Found on the way and fixed: the manager's start-up purge killed every IPIDS service process on the PC (so a
  simulator or test run would have killed the live system's services) - now only its own; the HMI config editor
  never rebuilt the registry (wrong script path); the recipe builder's tag picker and fault list looked in
  src/config/ (never found); the viewer saved plot settings to src/config/ (now config/, old file still read);
  HMI no longer fails to start without google-genai; Faraday panel no longer keeps showing the last reading;
  EventHelper used a private ZeroMQ context that could deadlock when garbage-collected together with its socket
  (froze the test run at exit; could have frozen the HMI/viewer if a window holding one was collected).

## To do
1. Lab PC validation (real hardware):
   - every service connects and answers commands; Command Log shows OK / reasons;
   - relay trip: Spellman shows DISABLED, steerer output goes off and stays off, SMU bias to 0 V;
   - einzel: voltage + 50 uA limit accepted, output comes up, enable/disable answered OK (refusals now show as
     failed), **no false arc trips** during normal use (ramps, optimiser, enable/disable) - the main thing to
     watch with the new 10 % arc test;
   - unplug a device: service stays ONLINE with comms-fail (no restart loop); SMU reconnect behaviour as above;
   - turbo start/stop; logger journal file appears in data/parquet_logs; tolerance rules sensible.
2. Take the example parquet data out of git - on the lab PC (the files stay on disk; back up data/ first anyway):
   ```
   git rm -r --cached data/daily_logs data/parquet_logs
   git commit -m "Stop tracking machine data (files stay on disk)"
   ```
   (.gitignore now lists them, so they won't be re-added.) Other clones that pull that commit lose their copies;
   the history keeps ~500 MB unless it is rewritten (git filter-repo + force push - only if wanted). Optional:
   point `IPIDS_DATA_DIR` at a folder outside the repo so git can never touch live data.
3. After the first HMI start on the lab PC: check config/recipes/ for recipes copied from src/config/recipes/
   (the HMI log says which), commit them, then delete src/config/.
4. Possible later: approach the magnet current from below in "Calculate & set amps" (hysteresis - the scan
   calibrates on the way up), if the landing isn't repeatable enough.

## Notes
- If the ADAM module is repaired: its manager key must be `service_adam_monitor` (matches its heartbeat name).
