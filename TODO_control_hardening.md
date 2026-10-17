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
  - Spellman arc = output below 10 % of setpoint, not judged below 0.5 kV, nor while the output is still coming
    up after an enable/setpoint change (until it reaches half the setpoint, max 3 s). Settings in
    `mpd_config.json` -> `arc_detection`. Latches until fault reset (also checked end-to-end into the PLC fault word).
  - Spellman current limits in `mpd_config.json` (0-330 uA, the MPD30's rating) - the HMI's automatic 50 uA limit
    is now actually sent and accepted.
  - X-steerer: software safety-relay interlock (output forced off and held off while the relay is off or the PLC
    is silent; stays off when the relay comes back).
  - SMU: after a reconnect the previous bias is re-applied only if fresh PLC data confirms the relay is on;
    otherwise 0 V / bias off and the setpoint is forgotten (also if the relay tripped while it was disconnected,
    or the operator had switched the bias off).
- Found on the way and fixed: the manager's start-up purge killed every IPIDS service process on the PC (so a
  simulator or test run would have killed the live system's services) - now only its own; the HMI config editor
  never rebuilt the registry (wrong script path); the recipe builder's tag picker and fault list looked in
  src/config/ (never found); the viewer saved plot settings to src/config/ (now config/, old file still read);
  HMI no longer fails to start without google-genai; Faraday panel no longer keeps showing the last reading.

## To do
1. Lab PC validation (real hardware):
   - every service connects and answers commands; Command Log shows OK / reasons;
   - relay trip: Spellman shows DISABLED, steerer output goes off and stays off, SMU bias to 0 V;
   - einzel: voltage + 50 uA limit accepted, output comes up, **no false arc trips** during normal use (ramps,
     optimiser, enable/disable) - this is the main thing to watch with the new 10 % arc test;
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
3. Check the first GitHub Actions run (Linux) - the Qt libraries step may need adjusting.

## Decisions needed from Josh
- Is the source einzel supply an MPD30 (30 kV, 0.33 mA)? max_current_ua = 330 assumes so.
- "Calculate & set amps" (Beamline Optics) has always used amps = amu: the widget looks for magnet_config.json
  in src/config/, which doesn't exist. Fixing the path would switch it to `mass_calibration_poly` in
  config/magnet_config.json ([0, 1.25, -0.005, 0] - looks like a placeholder; the mass scan uses k = 1391.9,
  I_off = 0.223 instead). Which calibration should it use? Not changed yet.
- Recipes saved from the HMI go to src/config/recipes/ (created automatically, not in git), while the recipes in
  git are in config/recipes/. Move the HMI to config/recipes/ (and copy yours over)? Not changed yet.
- If the ADAM module is repaired: its manager key must be `service_adam_monitor` (matches its heartbeat name).
