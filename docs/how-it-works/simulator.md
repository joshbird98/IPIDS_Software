# The simulator

How to run SINDIE without the implanter: what `python run_simulation.py` starts, how it keeps apart from the real system, the fault menu, how the simulated machine behaves, and where it differs from the real one.

## Starting it

```
python run_simulation.py
```

It starts the service manager in simulation mode, which starts every service with simulated devices in place of the real ones. When the services are up (it prints their progress, and waits up to 90 s) it opens the HMI and the data viewer. The window it runs in then shows the fault menu. Type `q`, or close that window, to stop everything.

| Option | Effect |
|---|---|
| `--no-hmi` | don't open the HMI |
| `--no-viewer` | don't open the data viewer |
| `--port-offset N` | shift the ZeroMQ ports by N instead of 10000 |

Everything above the device drivers is the real code: the services' command handling, interlocks and telemetry, the logger, the alarms, the sequencer, the HMI and the data viewer. Only the devices are simulated. The HMI's title bar says **[SIMULATION - fake devices]**.

As on the real machine, open the gate valve and energise the safety relay before the supplies accept commands.

## Kept apart from the real system

The simulator can run on the lab PC while the real system is running, without touching it:

- **Its own ports.** Every ZeroMQ port is moved up by 10000 (so `PLC_DATA` is on 15550), and the service manager's lock port moves too. It refuses to start if a service manager is already running on those ports.
- **Its own data.** Data and logs go to `data_sim/` instead of `data/`: chunks, day files, `events.db`, `alarm_state.json`, and the logs in `data_sim/SystemLogs/`, with the console output of the manager, the HMI and the data viewer in `simulation_manager_console.log`, `simulation_hmi_console.log` and `simulation_viewer_console.log`.
- **Its own tag registry.** At every start it builds `data_sim/system_tags.json` from the config files (keeping the hand-made labels of `config/system_tags.json`), so it always matches them even if `config/system_tags.json` hasn't been rebuilt since a pull.
- **Its own services.** The service manager's start-up clean-up only kills leftover processes of its own installation, so it never stops the real services.

The nightly compaction runs in the simulator too, but skips the OneDrive backup.

Each program also takes `--sim` on its own: `python src/services/service_manager.py --sim`, `python src/hmi.py --sim`, `python src/gui/data_viewer/viewer_main.py --sim`. This is how `run_simulation.py` starts them. `--sim` switches to the simulated devices, to `data_sim/` and to the simulator's ports (offset by 10000), so a program started that way by hand on the lab PC joins the simulator, never the real system. A port offset already set in `SINDIE_PORT_OFFSET` is kept (this is how `run_simulation.py --port-offset` and the tests choose their own).

## Injecting faults

The fault menu lists the faults with their numbers. Type a number to switch that fault on or off, `c` to clear them all, `q` to quit.

| Fault | What happens |
|---|---|
| `plc_disconnect` | the PLC can't be reached |
| `plc_relay_trip` | the safety relay trips (latched: reset the faults, then energise it again) |
| `spellman_no_reply` | the Spellman supply stops answering (timeouts) |
| `spellman_refuse` | the Spellman supply rejects every setting |
| `spellman_arc` | the einzel output collapses into its current limit, as in an arc |
| `magnet_disconnect` | the analyser magnet supply can't be reached |
| `magnet_sag` | the analyser magnet delivers 10 % less current than set |
| `steerer_disconnect` | the X-steerer supply can't be reached |
| `smu_disconnect` | the SMU can't be reached |
| `turbo_disconnect` | the turbo pump's bridge can't be reached |
| `turbo_error` | the turbo pump reports a run-up error |
| `turbo_backing_closed` | the valve to the turbo's backing pump shut: its current goes up 0.4 A at once, then 0.1 A a minute, and its temperatures follow; its overload warning from 4.5 A, an overload failure at 6 A (48 min in). The load guard warns after 2 min and trips the source after 22 min. Clearing the fault opens the valve again: the current falls back within a minute or so |
| `vacuum_disconnect` | the gauge controllers' bridge can't be reached |
| `vacuum_vent_source` | the source vacuum (VG1) rises to atmosphere |
| `vacuum_spike_source` | VG1 spikes to about 1 mbar for 0.8 s every 6 s, as with a powdered target: VG1's software trip rides each one through (**SPIKE**, then `VG1 spike ridden through` in the event log) |
| `safety_loop_open` | a door, an ESO or the key switch is open in the safety relay's hardwired loop: the relay won't close (refused after 5 s), and a closed relay opens (tripped) |
| `air_low` | the compressed air pressure switch opens: the gate valve closes and the relay drops |

The menu writes the faults to `data_sim/sim_faults.txt`, which every simulated device reads. A single process can also be given faults with the environment variable `SINDIE_SIM_FAULTS` (comma-separated).

## How the simulated machine behaves

The simulated PLC (`src/sim/plc.py`) holds a copy of DB10, laid out from the tag registry, so the PLC service's own reading and writing code runs unchanged. On it runs a small model of the machine, written from a description of the real one; the simulated source behaves roughly like the real TESS (Thermionic Emission Sputter Source):

- **Watchdog.** If the PC stops writing the watchdog for 3 s, the PLC reports lost communication and `PC_Watchdog_Timeout`, and the relay drops.
- **Safety relay.** **ENABLE SAFETY RELAY** requests it, and the panel's start button is taken as pressed 0.5 s later. It needs the gate valve open, the compressed air and both coolants OK, the PC watchdog and no shutdown request; asked for without them it is refused. With the fault `safety_loop_open` (a door, an ESO or the key switch open) the PLC gives its enable but the relay doesn't close, so after 5 s it latches `Safety_Relay_Refused` and drops the request. Once in, the relay holds until it is dropped or a permissive goes: closing the gate valve trips it. A trip is latched: the PLC clears the request, the supplies' enables, auto-emission and the caesium heater, and sets `Safety_Relay_Tripped` until a fault reset.
- **Interlock bits.** The machine state follows the PLC's rules (99 with a fault latched, 3 with the relay closed, 2 with the relay permissive made, 1 otherwise). For state 2 the permissive is the one the PLC program has: the gate valve open, the air and both coolants OK. The simulator's own extras, the watchdog and the shutdown request, don't count there. The gate valve's open permissive needs the PC's vacuum flag and no watchdog, GRAPHIX or VG1 / VG2 fault; its close permissive needs the extraction and target below 0.15 kV and the filament and caesium cold (the filament model follows its readback, and cools at 2 A/s with the relay off). The caesium heating is locked out with the relay open. There are no shutdown sequences, so the sequencer always allows the gate valve and never kills the relay. The [Interlocks](../panels/interlocks.md) panel reads these.
- **Source deck.** With the relay off there are no readbacks from the caesium oven, filament or thermionic supply (the PLC service shows them as unknown). The oven's estimated temperature keeps running.
- **Thermionic and filament.** The thermionic supply sits at 2500 V when on, and its current comes from the filament: about 3 mA at 25 A, about 150 mA at 29 A, none below 18 A, at most 200 mA. The filament current ramps up at 1 A/s and down at 2 A/s, and its temperature lags its current. Auto-emission drives the filament (up to 31 A here) to hold the thermionic setpoint; when it is switched off, the filament setpoint carries on from where it got to.
- **Caesium oven.** With the heater on (mode 1, relay in) it heats towards its setpoint; with forced cooling it goes towards 20 °C; otherwise slowly back to room temperature.
- **Hot caesium.** While the estimated temperature is 50 °C or more, the PLC keeps the target (cathode), filament and thermionic supplies on if they are on, and the target at least at its minimum voltage (0.5 kV here).
- **Extraction and target.** Each draws 0.02 mA per kV through its 50 MΩ bleed resistor. A target (sputter) current appears once the thermionic power is above 200 W, the target above 4 kV and the caesium above 50 °C: about 0.1 mA there, about 1 mA at 250 W, 8 kV and 100 °C. About 10 % of it is extracted (with the extraction at 5 kV or more), and shows in the extraction current.
- **Gate valve.** Opens 2 s after **OPEN** if the vacuum permissive is there; closes when the request or the permissive goes.
- **Faraday cup** moves as soon as it is asked.
- **Faults.** Only a few fault bits are set: the watchdog, the relay refused or tripped, low air, the comms failures and gauge flags reported by the PC, and the source einzel's flags. They follow their causes rather than latching (the relay's two latch until a reset).

The other simulated devices, in `src/sim/`:

| Device | Behaviour |
|---|---|
| GRAPHIX gauges | Pumped-down pressures that wander a little; relay set points from `config/vac_gauges_config.json`, so the service's start-up check finds them matching; each relay closes below its on point and opens above its off point; writes accepted. |
| Spellman supplies | A model per address with an output that lags its setpoint. The output is limited by the current limit, which is 0 after every power-up. Closing the bus (relay off) loses their settings, as on the real units. The einzel draws under 1 µA, noisily. |
| Magnet supplies | An inductive load: the current follows its setpoint with a time constant of about 1 s; voltage is current times resistance. |
| SMU | A small mass spectrum on the Faraday cup: peaks where the Mass Scan panel's default calibration expects oxygen, the silicon isotopes and others, growing with the extracted beam. Nothing arrives until the source is running. |
| Turbo pump | Spins up to 820 Hz in about 40 s while it is told to run, and coasts down otherwise; reports normal operation (status bit 0, **AT SPEED**) only at full speed; reports the vent strategy as already set, so nothing is written to its memory. |

## What it isn't

The simulator is a set of simple models with a little noise, made to exercise the software and for demonstrations and screenshots away from the lab. It is not the machine, and its PLC is not the PLC program. In particular:

- **No shutdown sequencer.** The simulated PLC never runs a shutdown: the step counters stay at 0. **INITIATE SHUTDOWN** drops the relay as a trip, and the request is never cleared, so the relay can't be energised again until the simulated PLC starts afresh (restart `service_plc` from the **Service Manager** panel, or the simulator).
- **No hot hold.** The close permissive is reported and the HMI acts on it: it shows **RELAY ENERGIZED (LOCKED)** and won't close the gate valve while the source is hot. But the simulated PLC doesn't put the relay request or the gate valve request back on while the source is hot, as the real one does.
- **No arc quench, no control-mode logic, no supply lockouts** (only the caesium oven's), and only the few fault bits listed above. The control mode reads back whatever the PC asked for. The filament model is simpler than the PLC's: it follows the readback, and cools at 2 A/s with the relay off.
- **Machine state 0** (Init) is never reported.
- **Assumed values.** Its "hot caesium" threshold (50 °C) and target minimum (0.5 kV) are guesses; the PLC program uses 30 °C and 3.0 kV. How the real PLC treats the supplies when the relay trips is also an assumption here.
- **The physics** of the source, the beam and the vacuum is not modelled beyond the rough figures above.

Check behaviour that matters on the machine itself. The PLC's real behaviour is described in [The PLC program](plc.md).

## See also

- [Tests](../engineering/testing.md)
- [Starting the software](../getting-started/starting-the-software.md)
- [System overview](overview.md)
- [The PLC program](plc.md)
- [Writing help pages](../engineering/writing-help-pages.md)
