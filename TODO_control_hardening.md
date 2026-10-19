# Control hardening - status and to-do

Branch `claude/control-hardening` (from `claude/viewer-stability`). Everything here runs against the
simulator; none of it has been tried on the real hardware yet (see "Lab PC validation").

## After pulling this branch on the lab PC
1. `python src/utils/build_registry.py` - rebuilds config/system_tags.json (picks up the Spellman current
   limits, and drops the software services' old cmd_ctrl_mode tags - leases replaced them; the PLC's own
   ctrl_mode tags stay).
2. Restart the service manager and the HMI. The manager now also starts `service_sequencer` (recipes, mass
   scans, optimiser) and `service_alarms`.
3. Check that the HMI's alarm horn is audible on the lab PC's speakers (Alarms dock; sounds in
   src/gui/control/assets/sounds/, made by src/utils/make_alarm_sounds.py).
4. The viewer copies workspace profiles from the old src/gui/config/ to config/workspace_profiles.json once
   (its log says which); check them, then delete src/gui/config/.

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
- After Josh's first simulator session (Oct 2026):
  - Simulator source model per his description: thermionic at 2500 V with emission from the filament
    (3 mA at 25 A -> 150 mA at 29 A), auto-emission drives the filament; cesium heater -> setpoint, forced
    cooling -> 20 degC; 50 MOhm bleeds on extraction/target plus SNICS target current (0.1 mA at
    200 W / 4 kV / 50 degC -> 1 mA at 250 W / 8 kV / 100 degC), ~10 % extracted; the Faraday-cup peaks scale
    with the extracted beam; einzel load < 1 uA and noisy.
  - Simulated relay needs the gate valve open; closing it trips the relay, latched (request, supply enables
    and cesium heater cleared; Safety_Relay_Tripped until fault reset). **Assumption about the real PLC
    program - check it matches**, especially whether the PLC clears the supplies' enables on a trip.
  - HMI: filament setpoint locked (badge AUTO-EMIS) while auto-emission is on; every source/optics setpoint
    box limited to the registry range, so values the service would refuse can't be typed (the einzel box
    allowed 30 kV against a 22 kV limit, cesium 200 against 150, thermionic 1000 mA against 150).
  - Mass Scan: the log view is a symmetric log (negative currents in red on their own log scale below
    zero, labelled -1 pA, -10 pA, ...); Y refits to the visible data when switching scale, clearing or
    loading a trace (it used to keep a stale range - data squashed at the top).
- Second round (Oct 2026, from Josh's answers to the review list):
  - `DeviceService` base class (`src/core/device_service.py`, own tests) for every hardware service; drivers
    in `src/drivers/`; one magnet module for the analyser magnet and the X steerer.
  - Control leases (`src/core/leases.py`) instead of the software ctrl_mode / origin bypasses; every service
    publishes per-subsystem permits with the reason; the HMI greys controls out with that reason.
  - Source deck: with the relay off its readbacks are unknown ('---'), the PLC's cesium estimate stays;
    while the PLC says the cesium is hot, switching off target/filament/thermionic (and target voltage below
    its minimum) is refused with the reason. The simulator's threshold (CS_HOT_C = 50 degC) and what exactly
    the PLC holds are **assumptions - check against the PLC program**.
  - `service_sequencer`: recipes, the precision mass scan (the older optics-panel mass scan was removed) and
    the optimiser run there under leases; the HMI only drives and displays them. Operator take-over pauses
    a recipe / stops a scan.
  - `service_alarms` + HMI Alarms dock with a horn (ISA-18.2 states, shelving with reason, first-out,
    suppression, persistence; the HMI alarms by itself if the alarm service is down).
  - Config editor: values keep type and precision (it used to round floats to 6 digits, turn null into
    "None" and empty lists into null); checks on load and on apply (incl. physical ranges); change list +
    reason, an extra tick for any number changing by more than x3; refuses if the file changed on disk
    since it was opened; only changed values rewritten; backups with History; atomic writes; registry
    rebuild with roll-back; git commit; CONFIG_CHANGE event. Keys can no longer be renamed (renaming a
    Spellman supply cut it off from its service, and the PLC then saw it as healthy). Services stopped on
    purpose are left stopped; network_config.json restarts nothing (the manager and HMI read it too -
    restart by hand).
  - Big files split (data viewer 1958 -> 760 lines + mixins; recipe builder; mass scan widget package); bugs
    found on the way fixed (recipe builder: verify-condition buttons crashed; step numbers could repeat and
    a drag then duplicated one step and lost another). Viewer workspace profiles moved to config/.
  - Broad `except Exception` blocks reviewed: narrowed to the errors expected where those are known (files,
    JSON, polars, Modbus, git, ZeroMQ); the rest are deliberate boundaries (one command / widget / job / day
    must not stop the rest) and log the traceback. The PLC service no longer drops the link when one tag
    can't be decoded, and ignores (with an event) fault-map bits outside the 32-bit word. A data export
    with a log file it can't read now says so in its metadata ("INCOMPLETE: ...") instead of only a
    console line.
  - `print` replaced by logging in the services, HMI, viewer and core (the viewer now writes
    SystemLogs/viewer.log); command-line tools in src/utils/ still print.
  - Found by the review: if a magnet PSU (analyser magnet or X steerer - same driver) didn't answer the
    nominal-ratings read on connect, the driver carried on with 1 V / 1 A ratings, so any current request
    of 1 A or more was sent as full scale. Until it has valid ratings it now accepts only zero / output
    off (so the steerer can still be forced off - its only interlock; 0 A now also sets 0 V, not the
    1 V minimum), shows the readbacks as unknown, logs a WARNING event, and re-reads the ratings every
    5 s (only on reconnect if the PSU didn't answer the read at all). A degauss cut short by missing
    ratings ends at 0 A.
  - Found on the way: the mouse wheel changed any HMI setpoint box it passed over (and focused it), and
    the box sends its value when focus leaves it - so scrolling a panel could change e.g. the extraction
    voltage by a step and the next click elsewhere sent it. The wheel now does nothing over value boxes
    and drop-downs in the HMI (`src/gui/control/wheel_guard.py`); values are typed or stepped.
- Found on the way and fixed: the manager's start-up purge killed every SINDIE service process on the PC (so a
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
   - turbo start/stop; logger journal file appears in data/parquet_logs; tolerance rules sensible;
   - closing the gate valve with the relay on: relay drops, supplies show off, Safety_Relay_Tripped shows,
     and the relay can be re-enabled after fault reset (compare with the simulator's assumptions above);
   - auto-emission: filament box locked, shows the PLC's filament current, unlocks when switched off.
2. Take the example parquet data out of git - on the lab PC (the files stay on disk; back up data/ first anyway):
   ```
   git rm -r --cached data/daily_logs data/parquet_logs
   git commit -m "Stop tracking machine data (files stay on disk)"
   ```
   (.gitignore now lists them, so they won't be re-added.) Other clones that pull that commit lose their copies;
   the history keeps ~500 MB unless it is rewritten (git filter-repo + force push - only if wanted). Optional:
   point `SINDIE_DATA_DIR` at a folder outside the repo so git can never touch live data.
3. After the first HMI start on the lab PC: check config/recipes/ for recipes copied from src/config/recipes/
   (the HMI log says which), commit them, then delete src/config/.
4. Possible later: approach the magnet current from below in "Calculate & set amps" (hysteresis - the scan
   calibrates on the way up), if the landing isn't repeatable enough.
5. Lab PC validation of the second round: a recipe and a mass scan under leases on the real supplies; Take
   control mid-run; alarm rules on real data (nuisance alarms? priorities right?); horn audible.
6. Decided (October 2026):
   - vacuum relay set points: the gate-valve permissive is withheld until VG1's and VG2's relays are read
     back holding the configured channel and set points (start-up check, then the round-robin - a front-
     panel change or a controller reset counts too, once three reads in a row give the same other value,
     ~25 s: single answers on that bus can be garbled - the driver can't check replies - and losing
     the permissive can close the gate valve; a deliberate read-back needs two agreeing answers). Any
     relay that differs: ERROR event + alarm (stat_relay_sp_mismatch). A config giving VG1 or VG2 no
     relay holds the permissive back too (it used to ignore that gauge's pressure), and so does VG1 or
     VG2 without a good reading for 10 s (it kept its last value). Relay set point commands: on / off
     only (channels come from the config), within the registry limits, on below off; what they set is
     expected until the service restarts. Found on the way: a controller that didn't answer at all
     (e.g. node 20 off) made the start-up check write to it, the write timed out, the link dropped and
     the whole connect ran again - in a loop, VG1/VG2 unread; it is now left out until it answers.
   - a refused PLC write keeps dropping and re-making the PLC link: snap7 doesn't say why a write failed,
     and reconnecting is the best general answer.
   - logger: up to 2 h of data the disk won't take (by its timestamps - forced flushes make short
     chunks) stays in RAM, then the oldest goes (CRITICAL event); now retried every minute instead of
     at the next 15-min flush, without restarting the crash journal early; WARNING when the data drive
     has less than 2 GB free. Also: a crash journal that can't be recovered at start-up is kept and
     tried again at the next start (it was overwritten), each on its own; a journal write the disk cuts
     short is cut back, so later records stay aligned; CRITICAL at shutdown if data is still waiting;
     a forced flush (viewer start, export) heartbeats while it waits, so a slow disk can't get the
     logger restarted as hung; the I/O thread has its own event socket.
   - config editor: fault_config.json restarts only service_plc and service_alarms (the services that
     use it) instead of every service. The PLC-service restart still pauses the PC watchdog for a few
     seconds, as a restart from the services panel does - never seen to trip anything.
   - magnet services' hung timeout 2 s -> 3 s (one unanswered Modbus request holds them ~2 s).
   - hard caps (src/core/hard_caps.py), Josh's numbers: VG1/VG2 relay points 1e-5 mbar, other relays
     1 mbar, every Spellman 22 kV, magnet 95 A / 25 V, steerer 80 A / 40 V, SMU +-200 V. The editor
     refuses a value past one (no tick gets past it) and marks the field red with the reason; the
     registry's setpoint limits are kept within them; the services refuse commands past them and
     don't use config values past them (magnet: the cap is used, with an ERROR event; vacuum: the
     relay isn't written, and for VG1/VG2 the gate-valve permissive is withheld); ENABLE is refused
     while a Spellman or magnet PSU holds a setpoint past its cap (e.g. set at its front panel); mass
     scans, the optimiser and "Calculate & set amps" name the cap when they refuse; the HMI's setpoint
     boxes name their cap in the tooltip (a value typed past a box's maximum just doesn't go in).
7. Future: a one-page interlock overview (every permissive and what is holding it), as discussed.

## Notes
- If the ADAM module is repaired: its manager key must be `service_adam_monitor` (matches its heartbeat name).
