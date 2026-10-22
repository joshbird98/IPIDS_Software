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
5. The HMI opens once with the new default layout (layouts saved by older versions aren't restored);
   arrange it and save it under View > Layouts if wanted. Both programs start dark: View > Theme or
   Ctrl+Shift+T for light (remembered in config/appearance.json, per machine).
6. `pip install -r requirements.txt` - adds the Markdown package the help pages use (Help, F1). Without it
   the help still opens, plainer (no call-out boxes or table borders). It keeps google-genai (2.8.0) for the
   recipe builder's drafting; python-dotenv is no longer needed (the key is read from .env directly), but
   testing/test_api.py still uses it.
7. Drafting in the recipe builder needs the Gemini key in `.env` in the SINDIE folder (the line
   GEMINI_API_KEY=..., as the old assistant had it). First `python -m src.core.recipe_ai.cli check` (or Test the
   connection in the builder's Draft tab): it says whether this PC reaches the model, and how fast. Then try it
   once: open the builder, type a request, Draft. The model is gemini-3.8-flash (config/recipe_ai.json);
   `python -m src.core.recipe_ai.cli draft "..."` runs a draft from a terminal. Each draft is logged in
   SystemLogs/recipe_ai/, and each request (with how long its answer took) in the HMI's log.
8. The launcher, so PyCharm isn't needed (docs/getting-started/starting-the-software.md): stop the service
   manager running in its command window (Ctrl+C), then run `main_launcher.py` once from PyCharm (it uses
   PyCharm's project interpreter, which has every library: nothing to install). Press **Start services**,
   **Put shortcuts on the desktop**, and tick **Start with Windows** (the launcher then starts in the tray
   at log-on and keeps the service manager running). From then on: the SINDIE shortcuts and the tray icon.
9. The Interlocks panel (a tab behind System Health): with the machine safe, refuse the relay on purpose
   (key at DISABLE, ENABLE SAFETY RELAY, no START): the Safety relay card should read REFUSED and leave the
   doors, ESOs, key and START. Find which GRAPHIX output is wired into the safety relay's loop and set it in
   the config editor: vac_gauges_config.json, system_interlocks.safety_loop_relays, e.g. ["10.4"] (until
   then every relay of controller 1 is checked; with the safety relay closed, the card says which open
   relays can't be the one). An amber "doesn't match" note on a card means the PLC program differs from
   docs/how-it-works/plc.md there, or (on the Safety relay card) that safety_loop_relays is set wrongly:
   tell Claude what it said.
10. The network test for alerts and remote viewing: `python src/utils/network_probe.py` on the lab PC (and
    `--send-test` with the ntfy topic it prints open on a phone), then send SystemLogs/network_probe_<date>.txt.

## Done
- 22 October 2026, at the lab PC: after the registry was rebuilt (step 1 above) the PLC service lost the PLC
  at once: the PLC refused the 316-byte read of DB10 ("Address out of range"). The PLC's DB_PC_Interface is
  314 bytes, without the Beamline_Steerer_Magnet block that plc/generated/PLC_PC_Interface.scl had at byte
  278 (it matches the PLC program export; docs/how-it-works/plc.md, known issues). The block is commented
  out in the .scl, so the registry gives 314 bytes and the PLC's offsets again; the PLC service's message
  now says when DB10 is shorter than the registry. tests/test_service_plc.py pins the layout.
- Josh's notes of 21 October 2026, afternoon ("the interlock overview and the launcher; the rest later"):
  - Interlocks panel (src/core/interlocks.py works it out, widgets/interlocks_widget.py draws it; help page
    docs/panels/interlocks.md): every permissive as a chain of live conditions - safety relay (the PLC's
    enable, then its hardwired loop), gate valve, source cold (what the PLC holds on while hot), source
    supplies, caesium oven, fault reset (would a reset be accepted now, and what is latched), and the services'
    permits and leases. A rail down each card is solid while the conditions hold and dashed after the first
    that doesn't; that one is shaded and named in the card's summary. After a refusal or trip it rules out
    what the PC can see in the loop (the gauge controller's relay, from the vacuum service; the magnet
    coolant switch, from its PLC input) and names what is left: a door, an ESO, the key switch or START,
    which nothing can tell apart (Josh). Where the PLC publishes its own verdict (machine state Ready, the gate
    valve's open and close permissives, the caesium lockout) a mismatch lasting 3 s is flagged: the PLC
    program would then differ from docs/how-it-works/plc.md. A tab behind System Health; a layout saved
    before it existed gets it there too.
  - The gauge relay is ruled out only on a current reading: its state read as closed within 25 s (the
    vacuum service now publishes when it read each relay, and starts with every relay unknown rather than
    the last run's values), its gauge answering with a pressure no higher than the relay's off point, and
    the set points confirmed on the controller; a pressure above the off point counts as open. The service
    still publishes a relay as unknown after 30 s unread or with the controllers unreachable.
    vac_gauges_config.json: system_interlocks.safety_loop_relays (shipped [], checked by the editor).
  - After an independent review of the panel: an open relay that may not be in the loop leaves the doors,
    ESOs, key and START in the diagnosis; a PLC condition holding now doesn't hide what the loop leaves;
    the summaries go by the PLC's own verdicts; Source cold says HOT (not HELD ON) with the relay open;
    the shutdown sequence isn't a contactor condition; the remote I/O is unknown with the relay open (the
    PLC only reports a loss after 25 s closed); a reset at PC step 2 warns that it ends the cool-down;
    forced cooling left on after a cancelled shutdown is named. The simulator's Ready follows the
    documented permissive alone, and its gauge relays have hysteresis.
  - Simulator: the interlock bits as plc.md describes them - machine state 99/3/2/1, the relay permissive
    with air and both coolants, the open permissive with the watchdog, GRAPHIX and VG1/VG2 faults, the close
    permissive (no HV, filament and caesium cold, with a filament model), the caesium lockout, gauge flags in
    word 5. New faults: safety_loop_open (refused after 5 s, or tripped), air_low.
  - Launcher (main_launcher.py, src/launcher/): starts the HMI, the data viewer, the help and the simulator
    without PyCharm, with the Python that started it (PyCharm's project interpreter: nothing to install).
    Starts the service manager in the background with pythonw (no command window to close by mistake);
    "Keep the services running" starts it again if it stops (5 s, 30 s, then every 2 min; a manager just
    started gets 30 s to come up), except after "Stop services...", which explains the watchdog shutdown
    and needs STOP typed: that stop is recorded for the system (data/launcher_state_<offset>.json) and
    every launcher leaves the services stopped until Start services. The manager now takes a confirmed
    "shutdown" command (every service, the last started first and the event log last, then itself). Tray
    icon with the services' state as a dot; closing the window or the launcher never stops the services.
    Desktop shortcuts and Start with Windows (Startup folder shortcut, --tray --keep-running), icon
    src/launcher/sindie.ico (src/utils/make_launcher_icon.py). One copy of the launcher, the HMI and the
    data viewer per system and user: a second start brings the first to the front (one that doesn't
    answer is reported, not doubled). HMI: View > Open the data viewer; viewer: View > Open the operator
    HMI.
  - src/utils/network_probe.py: what the lab PC's network lets through (push services, Teams workflows,
    Microsoft 365, SMTP, GitLab/GitHub, Cloudflare and Tailscale, Gemini, pip, Backblaze, the research share
    and the OneDrive folder); standard library only, outbound only, read-only unless --send-test.
- Josh's notes of 21 October 2026:
  - The start-up recipe stopped in the simulation: "could not keep control of the einzel: stale: sent 0.73 s
    ago". A recipe renews its control lease on each supply every second, and the Spellman service refuses any
    command that reaches it more than 0.5 s after it was sent; one renewal reached it late, and the refusal
    counted as losing control (over a long start-up that is thousands of renewals, so one slow moment was
    enough). Now the services never refuse a lease command as too old (commands.py), the sequencer sends a
    command refused as too old again, twice at most (port.py: it was never carried out, and is still
    wanted), and a renewal refused for that keeps the lease.
  - Drafting seemed to hang: google-genai gives every phase of a request one limit (120 s) and overrides a
    client's own, so a PC that couldn't reach the service waited 2 minutes to say so; and the model thinks
    before each answer while the line under the box only said "Waiting for ...". Now the service has 15 s to
    accept the connection and timeout_s to answer (a hook on our own HTTP client); connection, proxy,
    certificate and answer time-outs are told apart; the first wait says it often takes 10-60 s; each
    request and its answer time go to the HMI's log; Test the connection (the Draft tab) and
    `python -m src.core.recipe_ai.cli check` say what answered, how fast and where the key was read from.
    The default model is now gemini-3.8-flash, the newest stable Flash (gemini-flash-latest can point to a
    preview with tighter rate limits; it is the last fallback, after gemini-3.6-flash).
  - The builder's look: the steps are drawn as a flow - a node per step on a line from the first to the
    last, the block's icon in it, the steps of "Do steps at the same time" branching off it; grey nodes for
    most steps, blue for the operator, violet for procedures (the HMI's own meanings), hollow for waits,
    square for structure blocks; the blocks have the same icons, and the step form's heading too; the three
    columns are panes (look.py, outline.py).
- Josh's requests of 20 October 2026, evening:
  - AI drafting back in the recipe builder, rebuilt (src/core/recipe_ai, the builder's drafting.py). Not a
    chat: a box above the steps takes a request ("after step 3, wait for VG1 below 5e-6"; the selected step is
    "this step"); the model (Gemini, through google-genai) works with tools - it searches the tag catalogue,
    reads the help's procedures and the templates, checks its draft with the checks the sequencer makes, and
    proposes; a proposal with errors is sent back to it (three times at most). The draft is shown in the list
    of steps as tracked changes (+ added, - removed, ~ changed, with what it was), each of which can be left
    out (its tick in the Draft tab, or a click on its mark); Keep makes the ticked ones as one undoable change,
    Refine changes the draft, Discard drops it. While a draft runs or is shown the recipe can't be edited, so
    what is kept is what was shown. Review reads the recipe against the procedures and leaves notes under the
    steps (concern / suggestion / question) with a fix each can draft. The prompt is made from the same
    catalogue, step fields, routine parameters, supplies, faults and templates as the checks (about 15k tokens
    with every template, against 42k for the old one's registry dump), with rules and typical values from the
    procedures; it was tried with stand-in models on eight requests (new recipes, edits, an unsafe request, a
    review) and corrected where they stumbled. Each run is logged (SystemLogs/recipe_ai/). Settings in
    config/recipe_ai.json (the config editor saves it without restarting anything); the key from the
    environment or .env, never logged or shown.
  - From an independent review of it, fixed: a draft runs in a thread and hands back through one bridge
    owned by the application (each run had its own Qt object, often deleted in the run's thread, which Qt
    doesn't allow - the tests crashed once in about 20 runs), with the garbage collector kept to the GUI
    thread while a run's thread is alive (calibre's way); the Draft tab and its buttons are locked while a
    refinement runs (a change unticked then was kept anyway); a draft that can't be shown says so and leaves
    the builder unlocked; a 429 with a short retryDelay was retried for ever, and a model named twice in the
    fallbacks asked for again and again; a change to a "Do steps at the same time" step left out showed the
    draft's sub-steps (it shows the step as it stays now); a review note whose step changed offers no fix; the
    Stop conditions tab shows the draft's list while one is shown; closing the window cancels a draft only if
    it closes. A step type or stop condition that isn't text or an object (a hand-edited file, or a draft)
    raised in the checks, the text and the builder: now an error, as any other.
  - Found on the way, fixed: a degauss needs the safety relay (the magnet service refuses it without), but the
    checks didn't say so - they now treat a degauss as switching the magnet on (relay needed, and a
    Safety_Relay_Tripped stop); the degauss template waits for the relay first and stops if it trips;
    test_sequence.json (1.2) did the same, and gave the 2-minute degauss only 60 s (it always failed); the
    mass scan template and auto_tune.json (1.3) insert and bias the cup before the magnet comes on, as the
    tuning procedure does; fault 2.6's description named VG1 for VG6.
  - Help: both source deck doors are interlocked - the gullwing door over the source body and the hinged mesh
    door for the source trolley, each with a red dual-channel magnetic switch (Josh) - in the safety rules,
    the start-up procedure, troubleshooting, the overview and the glossary. The turbo's RUN word stays as it
    is for now (Josh), in "To check on the machine" below.
- Josh's requests of 20 October 2026:
  - Help: the page reloaded itself several times a second for as long as it was open (the wait cursor
    flashing): the scrollbar changed the text column, and a new column reloaded the page for its screenshots,
    which hid and showed the scrollbar. The column now ignores the scrollbar, and a resize scales the
    screenshots in place.
  - Vacuum System panel: a gauge or the turbo pump without a live reading keeps its last value, greyed, with
    a STALE chip and how long ago (tooltips say why: service silent, controllers or pump not answering, no
    good reading for 3 s); NO READING / OFFLINE when there never was one. The vacuum service now also
    withdraws a pressure the controller answers with something that isn't a number (it used to stay forever).
    The OPEN tooltip says what the permissive needs (it named the turbo pump, which isn't part of it). The
    Faraday cup button reads UNKNOWN without PLC data (it kept its last position and colour).
  - Source turbo pump: never stopped from the software (Josh: trained staff stop it by hand, at the pump).
    The service refuses cmd_enable false/0, and doesn't send a start or fault reset to a pump running without
    a fault (the reset word, 0x0480, has the run bit clear: it was a stop pulse). The panel tells ACCELERATING
    from SLOWING DOWN. The simulator reports status bit 0 (AT SPEED) only at full speed (it was set at 0 Hz).
  - Vacuum-service restart: service_plc keeps giving the PLC the vacuum service's last judgment (gate-valve
    permissive, GRAPHIX comms and gauge flags) while it is silent or still starting, for at most
    VACUUM_HOLD_S = 60 s after it; the restarted service says when it has judged (ion_beam.vacuum.stat_judged:
    the permissive given, or JUDGE_GRACE_S = 20 s after its start-up check). Nothing is written when the new
    judgment matches the held one, so no momentary drop; a bad judgment passes at once; a running service's
    own comms loss isn't held. The panel shows VACUUM PERMISSIVE HELD: n s LEFT. It used to drop the
    permissive 2 s into the restart and send GRAPHIX comms-fail (latched: state 99, valve shut, relay dropped,
    reset needed). tests/test_vacuum_restart.py, with a real restart of both services (slow).
  - Help pages: Josh's answers in place of the "to be confirmed" notes (the High Voltage Systems Control box,
    with the key switch, START and an ESO; the hardwired loop's contents; the ESO places; Units 2-4 not
    installed; marker colours; the isotope list; the turbo stopped by hand; only Josh deletes files on the
    lab PC, OneDrive backup); photos of the box (the key blurred: the repository is public) and the source
    deck; the watchdog closing the gate valve is intended.
  - Recipe builder rebuilt, the AI assistant removed (Josh: "not very practical"; a review found it sent the
    whole registry - about 42k tokens - with every click, with wrong facts in its prompt, and nothing checked
    what it wrote). In its place, one set of checks (src/core/recipe_check.py) used by the builder as you
    type, by LOAD RECIPE, and by the sequencer before it starts a recipe (a recipe with an error is refused,
    with the reasons): tags known and settable, values within the registry's ranges and the hard caps, fault
    resets and a turbo stop refused, sub-recipes there and not calling each other, stop conditions the
    sequencer can evaluate; warnings for what runs but probably isn't meant (a setpoint before its supply is
    on, relay outputs without a relay step or a Safety_Relay_Tripped stop, the gate valve opened without a
    vacuum wait, a degauss with the cup out, a mass scan without the magnet on and the cup in, a wait with no
    time limit, == on an analogue value ...). Recipes are shown as plain sentences (src/core/recipe_text.py)
    in the builder, the panel and the sequencer's status. The builder (src/gui/control/dialogs/recipe_builder/)
    has blocks named for what they do, forms made for each step (a tag typed by what it is called and shown
    with its unit and range; On/Off for switches; a supply chosen from a list), undo/redo, a stop-conditions
    tab with the recommended preset, Read as text, six templates from the start-up and shutdown procedures
    (config/recipe_templates/), and saving that keeps the file it opened, never overwrites another recipe or a
    change made meanwhile, backs up to config/backups/recipes/, offers the next version and commits to git
    (src/core/recipe_store.py). It isn't modal any more.
  - Recipe engine and sequencer, bugs fixed on the way: a sub-recipe with a bad value ended the sequencer; a
    missing or circular sub-recipe failed only when reached (now refused at the start, with the whole tree
    loaded and checked first); fault policies were ignored without "type", with a bad bit or below the first
    sub-recipe (now BIT_FAULT and CONDITION entries, the whole stack, an entry that can't be evaluated
    refuses the start); the mass_scan routine accepted loop (and never ended), didn't save its CSV, and
    scanned with the magnet off or the cup out; psu_control was done before its setpoint was answered (a
    refusal went unseen); pause didn't freeze the routines' timers; hv_conditioning ramped a supply that was
    off; a step raising outside execute() ended every job (the service now fails that job alone); the start is
    logged with each file's version and SHA-256. Shipped recipes: test_sequence.json degaussed with the cup
    out (now in); auto_tune.json switches the magnet on and biases the cup; test_faults_and_prompts.json's
    condition works. The panel: LOAD checks and keeps START off on errors, reports a broken file instead of
    raising, lists steps as sentences, loads a recipe saved in the builder again, and no longer keeps
    SEQUENCER OFFLINE after the sequencer is back.
- Help (October 2026): Help in the HMI and the data viewer opens the help window on the Markdown pages in
  docs/ (contents, search, back/forward, print or save as PDF, follows the theme); F1 opens the page of the
  panel under the mouse pointer; Help > About SINDIE shows the version (git commit) and the folders in use.
  Pages: safety rules, starting the software, a tour of the HMI, the controls, beam start-up and shutdown,
  alarms and faults, tuning, recording an experiment, every panel, the data viewer, troubleshooting,
  messages, how it works (PLC, services, alarms, logging, simulator), configuration, tests, glossary.
  Screenshots from the simulator: python src/utils/make_help_screenshots.py. tests/test_help.py checks every
  page, link, section link, screenshot and panel page; CI now checks out docs/.
- Found on the way: `--sim` on the HMI, the viewer or the manager now also selects the simulator's ports
  (offset 10000 unless SINDIE_PORT_OFFSET is set) - a `--sim` HMI on the lab PC used to talk to the real
  services; two recipes (sub_startup.json, so master_implant.json, and test_sequence.json) named an old tag
  path (ion_beam.beamline_diagnostics...) that the PLC service refuses - fixed, and a test checks every
  recipe's tags; the source panel shows the source body temperature (read only; the start-up checks it).
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
    cooling -> 20 degC; 50 MOhm bleeds on extraction/target plus the sputter target current (0.1 mA at
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
- After Josh's second simulator session (Oct 2026):
  - **The HMI sent setpoints nobody typed** (simulator and real machine alike). Qt reports "editing finished"
    on every focus loss - focus passing through, the window losing focus, the box being locked - and the
    setpoint boxes sent their value then, while a box with focus didn't follow the live setpoint. At start-up
    the box with the window's first focus still held its default 0; the first telemetry (relay off) locked
    it, and as the focus hopped down the source panel each box sent its 0. Reproduced in the simulator:
    einzel (refused: relay off - the message Josh saw), then extraction, target, filament, thermionic and
    cesium, all accepted by the PLC. On the real machine with the source running they would have been obeyed.
    Now (`src/gui/control/setpoint_box.py`) a box sends only what the operator typed or stepped, on Enter or
    when moving on to another control; nothing when the window loses focus (the edit waits) or the box is
    locked mid-edit (dropped); Escape drops it; until edited the box follows the live setpoint, focused or
    not. No control has the keyboard focus when the HMI opens (Qt gave it to the gate valve's OPEN button).
    Same fix for the magnet box, which never showed the magnet's setpoint at all (0.00 A after every HMI
    start, an arrow-key step from sending ~0 A), and the mass calibration boxes (any click through them
    relabelled a fitted calibration "entered by hand"). The slit boxes no longer send a command (the
    slits are manual; there is no such tag, so each was refused) - the width still goes into the event log.
    An independent review of that fix found more, all fixed with tests: a lock arriving while a box had the
    keyboard passed it to the next box, text selected, so the rest of the typing set another supply (the
    keyboard now goes to the window - for any control locked or hidden - and the dropped entry is reported
    with a "Not sent" note); empty text or a lone '-' re-sent the old value (or a box's default); a box
    stepped from its default or a stale number while the setpoint was unknown (now empty, no steps; the
    Y-steerer box shows the PLC's held setpoint, also with the steering off); a switched-away dock tab kept
    the keyboard off-screen and the next click sent what was typed into it (dropped now); an entry made
    before someone else changed the setpoint was sent (dropped); a passing mouse wheel took the focus from
    a box being edited, sending its half-typed value (no wheel focus for value boxes and drop-downs).
    Alarms: a Shift+click range could start from the wrong alarm after a redraw; Unshelve asked for
    alarms that weren't shelved.
  - Einzel at 10 kV stayed at 0 V and latched an arc: the simulator was running on a system_tags.json from
    before the einzel's current limits, so the HMI never sent its 50 uA and the supply (0 uA after every
    power-up) couldn't raise its output. The Spellman service now sets the current limit itself before the
    first non-zero voltage or enable after power-up, if the supply holds none (it asks it) and none has been
    set since - a limit sent just before, or an explicit 0, is kept (`default_current_ua` in mpd_config.json,
    50 uA) - whoever sends the voltage (recipes, optimiser) - and refuses the voltage / enable, saying why,
    if the supply won't take one. The HMI sends just the voltage. The service also forgets what it last set
    when the relay drops (the supplies do), so a supply not answering a setpoint query can't be shown
    holding its pre-trip setpoints. run_simulation.py builds its own registry from the config files at
    every start (data_sim/system_tags.json).
  - Alarms panel: a redraw (any change in the list) re-selected rows one by one with Qt's selectRow, which
    follows the keyboard - only the last alarm stayed selected, or none with Ctrl held. Selection now
    follows the alarms through redraws and re-sorting; the buttons are enabled only when they would act
    on something, and their tooltips say how to select.
  - Gate valve shut by its interlock (vacuum permissive gone): the PLC service clears the OPEN request
    (cmd_open_gv) once the valve has been fully closed, permissive gone and OPEN still set, for 3 s in a
    row (a flickering permissive or a valve still travelling is left to the PLC), with a WARNING event.
    It then stays shut until OPEN is pressed again. (The simulated PLC re-opened it by itself.)
  - Tests no longer use the real saved HMI layout (SINDIE_SETTINGS_DIR): a test window saved its layout
    over the operator's when it closed, and started from whatever was saved last.
- Found on the way and fixed: the manager's start-up purge killed every SINDIE service process on the PC (so a
  simulator or test run would have killed the live system's services) - now only its own; the HMI config editor
  never rebuilt the registry (wrong script path); the recipe builder's tag picker and fault list looked in
  src/config/ (never found); the viewer saved plot settings to src/config/ (now config/, old file still read);
  HMI no longer fails to start without google-genai; Faraday panel no longer keeps showing the last reading;
  EventHelper used a private ZeroMQ context that could deadlock when garbage-collected together with its socket
  (froze the test run at exit; could have frozen the HMI/viewer if a window holding one was collected).
- Look and layout (October 2026), after a first run on a laptop took about a minute to become usable,
  with most panels off-screen:
  - cause: the first start (no saved layout) opened every panel at once, and the window's minimum size
    was then 1718 x 2517 px - taller than two laptop screens; every panel refreshed 10 times a second,
    open or not, re-styling its labels each time (~600 restyles a second), and the services each
    busy-waited 2 ms per cycle. Later starts restored the operator's four-panel layout, so were fine.
  - panels scroll when made smaller than their contents and fold their cards into fewer columns
    (src/gui/reflow.py); the default layout is four panels in view and the rest as tabs, sized for the
    screen; the window is kept on a screen and no bigger than it; View > Reset layout and named layouts
    (View > Layouts); a tab group can be dragged as a whole. Panels closed or behind a tab aren't
    refreshed (they catch up when shown).
  - one theme module for both programs (src/gui/theme.py; dark default, light), switched live from
    View > Theme / Ctrl+Shift+T and followed by the other program; widgets set a tone (ok / on / warn /
    fault / info / seq / unknown...) instead of their own colours (src/gui/style.py), restyled only when
    it changes. Viewer: resizable sidebar, colour-blind-checked line colours kept per channel per lane,
    event colours by kind (a user's white marker is drawn darker on the light theme). Emoji taken off
    buttons (they don't survive the F12 PDF/SVG figure capture). src/core/theme.py and
    config/theme_config.json are gone.
  - services sleep to their tick instead of spinning the last 2 ms (device_service, logger, ADAM).
  - the HMI no longer has the services' 200 MB working-set ceiling on the lab PC (it runs at ~200 MB).

## To do
1. Lab PC validation (real hardware):
   - every service connects and answers commands; Command Log shows OK / reasons;
   - relay trip: Spellman shows DISABLED, steerer output goes off and stays off, SMU bias to 0 V;
   - einzel: after a relay cycle the supply reports a 0 uA current limit and the service sets 50 uA with the
     first voltage or enable (the reply says so); output comes up, enable/disable answered OK (refusals now
     show as failed), **no false arc trips** during normal use (ramps, optimiser, enable/disable) - the main
     thing to watch with the new 10 % arc test;
   - restart the HMI with the source running: nothing is sent (Command Log stays empty), every box shows the
     live setpoint, the magnet box included;
   - **gate valve after an interlock**: close it by withdrawing the vacuum permissive (e.g. a gauge
     controller unplugged), then restore it. It must stay shut until OPEN is pressed again: the PLC service
     clears cmd_open_gv once the valve has been shut by its interlock for 3 s (decided Oct 2026; the
     simulated PLC re-opened it by itself otherwise). Check the real PLC agrees (its seq_cmd_open_gv), and
     that OPEN afterwards opens it;
   - unplug a device: service stays ONLINE with comms-fail (no restart loop); SMU reconnect behaviour as above;
   - turbo start/stop; logger journal file appears in data/parquet_logs; tolerance rules sensible;
   - closing the gate valve with the relay on: relay drops, supplies show off, Safety_Relay_Tripped shows,
     and the relay can be re-enabled after fault reset (compare with the simulator's assumptions above);
   - auto-emission: filament box locked, shows the PLC's filament current, unlocks when switched off;
   - Interlocks panel against the real PLC: each card's conditions as the machine is (see step 9 above),
     the source-cold card while hot, a fault reset refused during HOLD HOT PHASE;
   - the launcher: services started from it survive closing it, PyCharm and the HMI; Start with Windows
     brings them back after a reboot; Stop services... stops them (PLC watchdog shutdown, as expected)
     and they stay stopped across a launcher restart until Start services; the tray dot follows the
     services; Start the simulator opens run_simulation.py in its own console with a working fault menu.
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
7. Later (agreed with Josh, 21 October 2026: "the rest we will come back to"):
   - **Operator log-in.** Josh Bird and Dr Luke Antwis to start with, each with a name and a password
     (stored salted and hashed, per machine, not in git); an operator role and an engineer role (the config
     editor, service restarts), replacing the config editor's password in the source. The HMI starts
     logged out, and logged out allows only viewing, STOP / ABORT of a running sequence, and INITIATE
     SHUTDOWN (Josh: that list is right). Every command in the audit trail names who sent it. Auto-lock
     after 1 hour without input (Josh: a long timeout); locking never sends a command or stops anything.
     Windows going to its lock screen must cause no problems: the HMI keeps running and refreshing, sends
     nothing, and is as it was on unlocking (locked if the hour has passed). Test that with a recipe running.
   - **Alerts for unattended runs.** Outbound only (nothing on the lab PC listens for connections: Josh
     suspects the university firewall reacts to a PC acting as a server). Pick the channel from the network
     probe: ntfy.sh (phone app, plain HTTPS), Pushover, a Teams workflow webhook (Microsoft 365), or e-mail
     through Office 365. Plus a dead-man's switch (healthchecks.io): the PC pings it every few minutes, and
     it alerts when the pings stop, which covers the PC or its network being down. An "Unattended" switch
     in the HMI choosing which alarm priorities alert, with a test button. Josh: "maybe good, will have to see
     what is possible". GitLab worked before but only at about one update a minute; other things didn't work.
   - **Remote viewing.** Read-only. Live: a status snapshot (machine state, key readbacks, active alarms)
     sent out on request or every few minutes through the alert channel, a Teams message, or a small file
     on OneDrive / the research share. History: the data viewer on a laptop reading a copy of the data (see
     housekeeping). No inbound access to the lab PC.
   - **Remote shutdown request.** INITIATE SHUTDOWN only (the PLC's operator shutdown), nothing else, ever.
     Armed at the machine by a logged-in operator before leaving it unattended (not armed: refused), a
     one-time authenticator code (TOTP, per user) and a second confirmation for each request, rate-limited,
     every request logged and alerted, through the alert channel (the PC fetches requests; nothing listens).
     The PLC's own rules still decide whether it starts. After the log-in. Josh: armed at the machine and an
     authenticator code are good.
   - **Data housekeeping.** Versioned backups instead of the compactor's OneDrive copy (_Archive_Versions):
     restic (or Kopia) of data/ (daily files, events.db) and config/ to the research share
     \\surrey.ac.uk\research\ibcfac2 and/or OneDrive, keeping daily versions for a month and weekly for a
     year, checked (restic check, a weekly test restore of one file), with the last good backup shown in the
     HMI and an alarm after 36 h without one. It never deletes anything on the lab PC (Josh's rule): pruning
     only removes the backup's own old versions. The network probe says whether the share is reachable.
8. On the lab PC and a laptop: the new look in both themes (alarm colours, the safety-relay countdown on
   ENABLE SAFETY RELAY), dragging panels into a narrow column, Reset layout, and the CPU use with the
   default layout (the simulator measured here: see the commit message).
9. Help pages, for Josh: add the Ion Beam Centre's local rules (docs/safety.md, "Local rules") and the
   caesium safe operating procedure; read the procedures (docs/procedures/) against practice; check the
   photo captions in docs/safety.md (the source deck, the High Voltage Systems Control box). What is still
   open is the PLC's behaviour, to check on the machine: which way the Faraday cup moves on a latched fault,
   and the relay trip that starts no shutdown sequence (grep -rn "checked on the machine\|To be confirmed" docs/).

10. Found while writing the help (October 2026), not fixed - to review (file:line at the time):
    - Simulator: INITIATE SHUTDOWN trips the relay (no shutdown sequencer; cmd_source_shutdown never
      cleared), and after a fault reset the relay is refused until service_plc restarts (src/sim/plc.py:205);
      caesium "hot" at 50 degC and target minimum 0.5 kV instead of the PLC's 30 degC and 3 kV. (The machine
      state and the close permissive are modelled now: 21 October 2026.)
    - service_plc: the steerer's and SMU's status flags are never written to the PLC although word 4 has
      Str_Magnet_* bits. (The PLC's DB10 has no Beamline_Steerer_Magnet block after all: it was left out of
      the PC's copy of the interface on 22 October 2026, which also stops PLC_DATA publishing never-written
      steerer_magnet.stat_* tags under the steerer service's names.) The '"From_PC" in tag' test at :335
      never matches (registry tags are lower case).
    - Spellman: over-temperature and rail collapse (status bits 0x30) are folded into stat_fail ("failed to
      track setpoint"); the PLC's Unit*_OverTemp / Unit*_Supply_Rail bits can never be set (service_spellman_mpd.py:414).
    - Alarms panel: probably no start-up grace (a fresh HMI may show ALARM SERVICE NOT RUNNING and sound the
      critical horn until the first alarm list arrives, alarm_widget.py:214-226); the banner colour counts
      shelved and suppressed alarms (:233); the shelve dialog's maximum is fixed at 480 min whatever
      shelve_max_minutes says (:46, :199); a cleared-unacknowledged alarm is drawn steady, although the README
      and alarms/model.py say it flashes (:305).
    - Mass scan: Calculate fit writes the calibration into the last loaded trace file even when the plot shows
      a live scan (widget.py:130, 558-563); loading a trace during a scan mixes the live points into it (:431);
      peaks come back sorted by index, not mass (peaks.py:50).
    - Optimiser: choosing a new parameter relabels the old map's axes, and a box drag then sweeps the new
      parameter in the old map's coordinates (optimiser_widget.py:176-185); the picker offers the GRAPHIX relay
      set points, which can never be swept (optimiser_tag_dialog.py:33); transient status texts are overwritten
      within 100 ms (also mass_scan/widget.py:494-497).
    - Take control: a scan's or sweep's clean-up commands are then refused, so the SMU stays at the scan's
      NPLC and the optimiser's setpoints stay at the last point; tests/test_sequencer_mass_scan.py takes only
      the magnet, the Automation bar takes every subsystem.
    - Source panel: AUTO EMISSION: ON text can go stale; the HEATING badge shows while the PLC holds the oven at
      20 degC during a shutdown; the CASCADED tooltip names the target.
    - Config: fault_config.json's VG6_Not_Found description says VG1, PC_Watchdog_Timeout says "1Hz heartbeat";
      plc_config.json says its limits mirror the PLC's, but filament 33 A and caesium 0-150 degC differ from the
      PLC's 0-40 A and 20-200 degC; after a change that rebuilds the registry the config editor restarts only
      the file's own service (the HMI and service_sequencer keep the old limits until restarted).
    - HMI texts: the RECIPE badge ("Control yielded to active Recipe.",
      PLC mode 2 - recipes use leases, and the PLC ignores mode 2) and the mode-1 tooltip ("automated
      sequencer" - it is the PLC's shutdown sequence) mislead (source_widget.py:442-446); the services word
      "relay off" refusals differently (service_plc.py:695, service_spellman_mpd.py:168/255,
      service_magnet_psu.py:255, service_smu.py:138, device_service.py:171/275).
    - Spellman: the arc over-current threshold in mpd_config.json (500 uA) is above the source einzel's 330 uA
      rating, so that test can never fire for the only configured supply (mpd_config.json:9, :23).
    - Beamline Optics: rows for the beamline einzel and both neutral traps, which mpd_config.json doesn't
      configure, so every command to them is refused (optics_widget.py:280-282).
    - PLC (to confirm on the machine): every shutdown that reaches step 2 closes the gate valve, which latches
      Safety_Relay_Tripped, so a planned or automatic shutdown also raises the critical "Hardware safety loop
      broken" alarm; which way the Faraday cup moves on a latched fault (FB20 sets Cmd_Insert_FC FALSE, OB100 and
      the shutdown sequences insert it); a relay trip doesn't start a shutdown sequence (facts report 12.2).
    - The config editor's password is written in the source (control_main.py, _show_config_editor).
    - The Vacuum panel shows AT SPEED / RUNNING whenever the TURBOVAC's status bit 0 is set (vacuum_widget.py)
      - check what bit 0 means on the real pump (the simulator now sets it only at full speed, src/sim/turbo.py).
    - PLC: a PC watchdog timeout closes the gate valve at once, so the relay drops (intended, Josh, October
      2026: without the PC the vacuum is unknown); so GRACE PERIOD's 75 % extraction hold acts on supplies
      without power - the step could be simplified.
    - Source turbo pump, to check on the machine (Josh: trained staff stop it by hand, at the pump): the service
      sends the run control word (0x0401) with every message while the pump runs (and adopts RUN when it finds
      the pump spinning at start-up). Check that a stop made at the pump isn't overridden by that word (if the
      pump obeys the serial link while it is the control source, it may start again). If it is, the service
      should stop asserting RUN once the pump slows down without a command from it, or the pump's control
      source should be set so a local stop wins. Josh (20 October 2026): leave the turbo control as it is for
      now - a TODO, not to be changed yet.
    - service_plc: permit() leaves out the caesium-hot hold (HELD_WHILE_CS_HOT), so the target, filament and
      thermionic switches aren't greyed out while the caesium is hot - the operator gets a refusal pop-up
      instead (service_plc.py:632); that refusal credits the PLC with the hold (:727), which the PC applies.
    - Current Monitoring reads "system.facilities.adam.stat_comms_fail", but the cache stores it under
      "ADAM_DATA/system...", so every card would stay OFFLINE even with the ADAM working (current_monitor_widget.py:91).
    - Data viewer: the export and F12 folders are fixed to data/exports, ignoring SINDIE_DATA_DIR and --sim, so
      simulator exports land in the machine's folder (backed up to OneDrive) (viewer_main.py:290,
      figure_capture.py:46; paths.EXPORTS_DIR exists); an unknown value shows as "nan" in Values and the
      Inspector (viewer_lanes.py:340); Delete removes the "Default" profile (viewer_profiles.py:118 - the
      protecting context menu at :104 is never connected); the follow-live tooltip says zooming stops
      following (only a drag does); config_manager.py's plot_settings.json functions are dead code.
    - Small: the SHUTDOWN SEQ chip keeps old text with src_auto_sdown on at step 0 or an unknown step
      (system_health_widget.py:140-153, possible); "finishing: restoring the SMU integration time" never reaches
      the job status (jobs.py:206-215); the alarm reads "<service> is crashed" (rules.py:41); FAULT events carry
      the numeric fault severity in the severity field (event_helper.py:64).
    - Small: tests/conftest.py uses setdefault, so a SINDIE_DATA_DIR or port offset set in the shell is used by
      the tests; the ADAM service hard-codes its address; VG4 is left out of rapid-rise detection
      (service_vac_gauge_controllers.py:554); the Service Manager's Start button keeps the Stop tooltip.

11. Caesium oven cooling air (agreed with Josh, October 2026). As the PLC program reads, it opens the cooling
    air whenever the safety relay is open, however cold the oven is, so after a shutdown it stays on until the
    relay closes again. The HMI now shows this honestly: FORCED COOLING shows only the operator's own request
    (FORCED COOLING: ON), and the oven's second line says "cooling air on" with the reason in its tooltip
    (source_widget.py _show_cooling; the simulator now opens the air with the relay off too). Proposed PLC
    change (FB3/FB20 cooling demand), so the air stops by itself once the oven is cold:
    - replace the plain "relay open" term with "relay open AND (Cs_Is_Hot OR the cool-down isn't finished)";
    - a cool-down is finished when the PLC's caesium estimate has read cold (<= 30 degC) for 10 min AND the
      automatic cooling has run for at least 30 min in all (Josh's practice), so a hot oven always gets its
      30 minutes and the run-on covers the model's error;
    - keep the PC's force-cooling request as a plain on/off override, and the over-temperature, cool-fail and
      shutdown-sequence cooling as they are;
    - optionally a To_PC bit saying the cooling is the PLC's own (the HMI infers it now).
    First check that the cooler's solenoid can be driven with the relay open: the output is SOV_Stop
    (energised = air off), so if it is powered from the source deck through the relay, the air flows whenever
    the relay is open whatever the PLC does, and the automatic stop would need it powered from the ground side.
    The PLC is the right place (not the PC): it owns the oven, the valve and the thermal model, and keeps working
    when the PC doesn't. Update docs/procedures/beam-shut-down.md ("After the shutdown") and
    docs/panels/ion-source.md when it's done.

## Notes
- If the ADAM module is repaired: its manager key must be `service_adam_monitor` (matches its heartbeat name).
