# Troubleshooting

Common problems, each named by what you see, with the likely causes in the order to check them and where to look. The messages quoted here are explained in [Messages and what they mean](messages.md).

For a problem you can't fix, contact Josh Bird (j.m.bird@surrey.ac.uk). For the safety relay's key and for safety matters, contact Dr Luke Antwis, the lab manager. Emergency numbers are on the [Safety rules](safety.md) page.

## Typical values

What the machine typically shows when all is well. They are a guide, not limits.

| What | Typical | Notes |
|---|---|---|
| Source pressure (VG1) before start-up | below 5×10⁻⁷ mbar | |
| Source body and caesium oven temperatures, relay just on | between 10 °C and room temperature | Every supply reads zero at that point. |
| Extraction voltage | 15 to 20 kV | Extraction plus target is the beam energy. |
| Target (cathode) voltage | 5 to 9 kV | |
| Einzel voltage | 8 to 16 kV | Tuned on the beam. |
| Extraction and target (cathode) current with no beam | the current through their 50 MΩ bleed resistors, 0.02 mA per kV | Both rise as the caesium heats. |
| Target current | about 1 mA | The current to aim for; it varies with the target material. |
| Einzel current | close to zero | Above 50 µA, see [The einzel current is high](#the-einzel-current-is-high). |
| Filament current | 28 to 30 A | A new filament, or one after the source has been at air, first degasses at 15 A: the pressure spikes, then recovers. |
| Thermionic current | 100 to 150 mA | |
| Caesium oven | 100 to 150 °C, settling over about 15 minutes | Forced cooling must be off for the oven to heat. |
| Beam on the Faraday cup | within 10 to 60 minutes, as the caesium heats up | Depending on the target material, and on whether it has been run before. |
| Faraday cup bias | more than 50 V | Needed for a correct beam-current reading. |
| Slits | 10 mm each for a first tune | Set by hand. |
| Aperture ladder | fully retracted is the largest opening | Set by hand. |

## A value shows dashes

`---` means the value isn't known. The HMI never shows a number it can't vouch for: it leaves out every value from a service that has gone quiet, and every value a service reports as unknown.

Check, in order:

1. **Is the service running?** The HMI drops the values of a service that has been silent for about 2 s (longer for one that publishes slowly). In the **Service Manager** panel the service should be **ONLINE**. If it isn't, see [A service shows BACKOFF](#a-service-shows-backoff).
2. **Can the service reach its device?** A service that can't reach its device keeps running and reports its values as unknown. Look in the **Alarms** panel for `<device>: communication lost` or `The PLC service has lost the PLC`, and check the device, its power and its network connection.
3. **Is the safety relay off?** With the relay off, the source deck (filament, thermionic, caesium oven) and the Spellman supplies have no power, so their readbacks are unknown. The caesium oven then shows the PLC's estimate, marked `~`.
4. **A vacuum gauge or the turbo pump.** The vacuum service withdraws a gauge's pressure after 3 s without a good reading. The **Vacuum System** panel then shows the last value it had, greyed, with **STALE** and how long ago, rather than dashes; dashes mean it has had no value since the HMI started. Hover over the value for the reason. See [Readings that have stopped](panels/vacuum.md#readings-that-have-stopped).
5. **The analyser magnet or the X-steerer.** Until the supply has reported its nominal ratings, its voltage and current readbacks are unknown: see [Magnet and steerer services](messages.md#magnet-and-steerer-services).
6. **An empty setpoint box.** The setpoint isn't known yet, or is outside the box's range. You can still type a whole value; the arrow keys do nothing until the setpoint is known.

In the **TESS Ion Source Control** panel, ` [?]` after a readback marks a value that may be old: PLC comms are lost, or (for the source einzel) the Spellman service isn't **ONLINE**.

## A service shows BACKOFF

The service manager starts every service, and restarts one that exits, doesn't send its first heartbeat within 15 s of starting (215 s for `service_data_compactor`), or stops sending heartbeats for longer than its timeout: 2 s for most services, 2.5 s for the Spellman service, 3 s for the magnet, steerer, monitor, alarm and sequencer services, 10 s for the SMU service and 215 s for the compactor. **BACKOFF** means the manager is waiting to restart it: after 2, 5, 10, 30, 60 and 120 s, then every 5 minutes. A service that has then run for 120 s has its count reset.

The row says `restart in <n> s, failure <n>`. Hover over it for the reason:

- `process exited with code <code> (details in SystemLogs/<service>.log)`: the service crashed. Its log has the traceback.
- `no heartbeat within <t> s of start`: it didn't start properly.
- `heartbeat stopped for more than <t> s (hung)`: it hung, and the manager killed it.

What to do:

1. Read `SystemLogs/<service>.log`. A configuration file the service can't read, or a missing Python package, shows there.
2. Fix the cause, then press **Restart**. It also clears the back-off.
3. **Stop** keeps a service stopped (**STOPPED**) until you press **Start**: the manager doesn't restart it meanwhile.

A device that can't be reached doesn't cause **BACKOFF**. The device services keep running and heartbeating while their device is unreachable, and report it as a comms failure instead.

The other states are **STARTING** (started, no heartbeat yet), **OFFLINE** (not running), **CRASHED** and **HANGING** (seen only briefly, before the manager restarts the service) and **UNKNOWN** (no data from the service manager itself: start it with the launcher's **Start services**, or `python src/services/service_manager.py`). After 10 s in **BACKOFF**, **CRASHED**, **HANGING** or **OFFLINE** the alarm service raises `<service> is waiting to be restarted` (or `is crashed`, `is hanging (no heartbeat)`, `is not running`). See [Service Manager](panels/service-manager.md).

## PLC COMMS shows OFFLINE

The **System Health** panel shows **PLC COMMS: OFFLINE** when the HMI has no live data from the PLC service, when the PLC service has lost the PLC, or when the PLC reports the PC's watchdog lost. **SAFETY** and **SHUTDOWN SEQ** then read **UNKNOWN**, and the PLC's controls are greyed out.

1. **Is `service_plc` running?** Check **Service Manager**. When it isn't, the alarm is `No data from the PLC service - machine state unknown`. Start or restart it.
2. **Has the PLC service lost the PLC?** The alarm is `The PLC service has lost the PLC`, and `SystemLogs/service_plc.log` has `PLC connection lost: <reason>` or `PLC connection to <address> failed: <error>`. The service tries again after 1, 2 and 5 s, then every 10 s. Check the PLC is powered and running, and its network connection. Its address is in `config/network_config.json`.
3. **Does the PLC report the PC watchdog lost?** The PLC service changes a watchdog value in the PLC every 0.5 s. If the PLC sees it unchanged for 3 s, it latches `PC_Watchdog_Timeout` (a critical alarm) and starts its comms-loss shutdown: **SHUTDOWN SEQ: GRACE PERIOD (75% EXTR)**, extraction held at 75 % of its readback for up to 60 s, then **CESIUM COOLDOWN**. `PC_Watchdog_Timeout` also takes away the gate valve's open permissive, so the valve closes and the safety relay opens. Restarting `service_plc` stops the watchdog for a few seconds. Once the watchdog is back, press **RESET FAULTS**, but not during **CESIUM COOLDOWN**: see [A shutdown sequence started by itself](#a-shutdown-sequence-started-by-itself).
4. **It connects, then loses the PLC at once with a read failure.** `service_plc.log` says `read failed: ... Address out of range - the PLC's DB10 is shorter than ...`. The PLC service reads the PLC's data block DB10 (`DB_PC_Interface`) with the layout in `plc/generated/PLC_PC_Interface.scl`, through `config/system_tags.json`, which is built from it. The PLC is answering, but its DB10 is shorter than that layout says. Compare the file with `DB_PC_Interface` in TIA Portal (a standard data block: TIA shows each variable's offset), make the file match the PLC, then run `python src/utils/build_registry.py` and restart `service_plc`. Never the other way round without checking: with a layout that doesn't match, a read can succeed and put the PC's flags on the wrong bits of the PLC. See [The PLC program](how-it-works/plc.md#known-issues-in-the-plc-program).

## The gate valve does not open

The **Gate valve** card on the [Interlocks](panels/interlocks.md) panel shows each of the conditions below, live, and which one holds the valve shut. Check, in order:

1. **OPEN is greyed out.** Hover over it in the **Vacuum System** panel:
    - `Disabled: PLC telemetry stream is dead.` (the buttons read **OFFLINE**): see [PLC COMMS shows OFFLINE](#plc-comms-shows-offline).
    - `Disabled: Gate valve hardware fault detected.` (the buttons read **FAULT LOCKOUT**): `GateValve_Timeout` or `GateValve_Conflict` is latched. Check the valve, its compressed air and its limit switches, then press **RESET FAULTS**.
    - `Disabled: the PLC doesn't allow the valve to open. ...`: the PLC's open permissive is false. Go on to step 2.
2. **The open permissive.** The PLC needs the vacuum permissive from the PC (`ion_beam.source.chamber.stat_vac_ok_for_gv`), and none of these faults latched: `PC_Watchdog_Timeout`, `Graphix_1_Comms_Fail`, `Graphix_2_Comms_Fail`, or VG1's or VG2's `Not_Found`, `Type_Mismatch`, `Above_SP_Warn` and `Rapid_Rise_Warn`.
    - VG1 (**Source (VG1)**) and VG2 (**Beamline pre-magnet (VG2)**) must show a live pressure and **HEALTHY**, **WARNING** or **ONLINE** (or, for VG1, **SPIKE**): not **TRIPPED** (above its set point: for VG1, above its software trip point and not back down within 2.5 s, or a backstop relay open), **NOT FOUND**, **RAPID RISE**, **SENSOR OFF**, `FAULT (<code>)`, **STALE** or **OFFLINE**.
    - The vacuum service gives the permissive only while it has had a good reading from VG1 and VG2 within the last 10 s, and has read back VG1's and VG2's relays holding the channel and set points in `config/vac_gauges_config.json`. The alarm `Gauge relay set points not confirmed against vac_gauges_config.json - see the event log (VG1/VG2 relays: gate valve held shut)` means it hasn't: `SystemLogs/service_vac_gauge_controllers.log` says which relay, and what it holds.
    - While the vacuum service restarts, the PLC service keeps giving the PLC its last judgment of the vacuum, for at most 60 s (**VACUUM PERMISSIVE HELD** on the Vacuum System panel). If it hasn't judged the vacuum again by then, the PLC is told the vacuum is unknown, as it is when the vacuum service isn't running: the permissive goes, and while the vacuum service is silent both gauge controllers are reported failed, which latches their faults. See [Vacuum System](panels/vacuum.md#when-the-vacuum-service-restarts).
    - The faults listed above stay latched after the vacuum has recovered: check the **PLC faults** list in **System Health**, and press **RESET FAULTS** once the vacuum is good.
3. **OPEN is accepted, but the valve doesn't move.** The PLC opens the valve only with the compressed air OK (`Air_Pressure_Low`) and with the shutdown sequencer's permission: a shutdown at **CESIUM COOLDOWN** closes the valve and keeps it closed. As the PLC program reads, its permission also stays off after an operator shutdown has finished, until a fault reset (see [The PLC program](how-it-works/plc.md)): press **RESET FAULTS**. If the valve hasn't reached its position 5 s after the request, the PLC latches `GateValve_Timeout`.
4. **The valve opened, then closed by itself.** The PLC closes it when the open permissive goes. Once it has been shut that way for 3 s, the PLC service clears the OPEN request so that it stays shut, with the event `Gate valve closed by its interlock (vacuum permissive gone): its OPEN request is cleared, so it stays shut. Press OPEN again once the vacuum is OK.` Find why the permissive went (step 2), then press **OPEN** again.

Closing the gate valve with the safety relay on opens the relay. See [Vacuum System](panels/vacuum.md).

## The safety relay does not switch on

The **Safety relay** card on the [Interlocks](panels/interlocks.md) panel shows the PLC's conditions below, live, and after a refusal it rules out what the PC can see in the relay's loop (the gauge controller's relay and the magnet coolant switch), leaving what has to be checked by hand.

1. **ENABLE SAFETY RELAY is greyed out.** `Disabled: The Gate Valve must be fully OPEN before the safety circuit can be energized.`: open the gate valve first ([The gate valve does not open](#the-gate-valve-does-not-open)). `Disabled: GUI to PLC communications are offline.`: see [PLC COMMS shows OFFLINE](#plc-comms-shows-offline).
2. **START.** After you press **ENABLE SAFETY RELAY** the button counts down, `PRESS PANEL START (<n>s)`. Press **START** on the **High Voltage Systems Control** box, on the operator's desk, before the count ends. The PLC can only allow the relay to close; it closes when the button is pressed.
3. **SAFETY: TRIPPED/REFUSED when the count ends.** The relay didn't close within 5 s, so the PLC dropped the request and latched `Safety_Relay_Refused`. Press **RESET FAULTS** before you try again.
4. **The PLC's relay permissive.** The PLC passes the enable to the relay only when all of these hold. **ENABLE SAFETY RELAY** checks only the gate valve; the Interlocks panel shows the rest:
    - the compressed air is OK (fault `Air_Pressure_Low`);
    - the source coolant flow is OK (`Src_Coolant_Flow_Low`);
    - the magnet coolant is OK (`Mag_Coolant_Flow_Low`; **MAG COOLING** in **Beamline Optics**);
    - the gate valve is open;
    - no shutdown sequence has killed the relay (**SHUTDOWN SEQ: FINAL KILL**; cleared by **RESET FAULTS**);
    - `Cesium_Oven_OverTemp` isn't latched.

    The **PLC faults** list in **System Health** shows the latched faults. The air and coolant switches themselves can be plotted in the data viewer as `ion_beam.facilities.stat_air_press_ok`, `ion_beam.facilities.stat_src_coolant_ok` and `ion_beam.facilities.stat_mag_coolant_ok`, and the relay kill as `ion_beam.internal.seq_cmd_kill_relay`.
5. **The hardwired safety loop.** The relay's own loop, which the software doesn't see, can also hold it open. Check each of:
    - the key, turned to **ENABLE** on the **High Voltage Systems Control** box (for the key, ask Dr Luke Antwis, the lab manager);
    - both emergency switch-offs (ESOs), released: on the box, and on the source deck's side panel;
    - both source deck doors, shut: the gullwing door over the source body and the mesh door to the source trolley (a switch on each);
    - the vacuum: the gauge controller's set-point relay in the loop opens when the pressure is above its set point;
    - the magnet coolant: its flow meter and thermal cut-off switch.

When the relay closes, the buzzer sounds for 3 s and the beacon shows red instead of green. See [Safety rules](safety.md#the-high-voltage-systems-control-box) for photos of the box and the source deck.

In the simulator the start button is taken as pressed 0.5 s after the request, and the simulated relay needs the gate valve open. A recipe's `safety_relay_init` step waits 6 s for the relay, then fails with `safety relay did not energise: physical PANEL START not pressed in time`.

## The safety relay dropped out

**System Health** shows **SAFETY: TRIPPED/REFUSED**, with `Safety_Relay_Tripped` latched and the critical alarm `PLC: Hardware safety loop broken. HV disabled.` The PLC latches it whenever the relay opens without the PC dropping it on purpose.

Likely causes:

1. **The gate valve closed.** Closing the valve opens the relay. The PLC closes it when its vacuum permissive goes (see [The gate valve does not open](#the-gate-valve-does-not-open), step 2), for example when VG1 or VG2 rises above its set point: look for a pressure spike, such as a new filament degassing (see [Typical values](#typical-values)). VG1 rides through spikes shorter than 2.5 s: the event log says `VG1 above ... mbar for 2.5 s` when one lasted longer, and `VG1 spike ridden through` for the ones that didn't (see [Riding through short spikes (VG1)](panels/vacuum.md#riding-through-short-spikes-vg1)). It also closes it at shutdown step 2, **CESIUM COOLDOWN**: after a hardware abort, or 60 s after losing the remote I/O. Losing the PC watchdog closes it at once, by design: `PC_Watchdog_Timeout` takes away its open permissive, as without the PC the PLC has no vacuum readings. Look at **SHUTDOWN SEQ**, and at the alarm marked first-out (a star in the **Alarms** panel).
2. **The compressed air, the source coolant or the magnet coolant was lost**, or the caesium oven went over temperature. Their faults are in the **PLC faults** list.
3. **The hardwired loop opened**, for example because an ESO was pressed or one of the source deck's doors, which are both interlocked, was opened.

The beacon then shows green, and amber while `Safety_Relay_Tripped` is latched.

When the relay drops, the Spellman supplies are reported off; the analyser magnet and X-steerer outputs are forced to 0 A, 0 V and off, and stay off when the relay comes back; the SMU bias goes to 0 V and its setpoint is forgotten; the PLC zeroes every requested setpoint (the caesium oven's to 20 °C). Nothing comes back on by itself.

To recover: clear the cause, let any shutdown sequence reach **FINAL KILL** or **LATCHED SAFE (RESET REQ)**, press **RESET FAULTS**, open the gate valve if it closed, press **ENABLE SAFETY RELAY** and **START** on the **High Voltage Systems Control** box, then switch the supplies on and set them again.

> [!WARNING]
> As the PLC program reads, a relay trip doesn't start a shutdown sequence, even with the source hot; the caesium oven's cooling air is still opened while the relay is open. To be confirmed on the machine: see [The PLC program](how-it-works/plc.md).

## A shutdown sequence started by itself

**SHUTDOWN SEQ** in **System Health** shows a step you didn't start:

| Shows | Why | What the PLC does | What to do |
|---|---|---|---|
| **GRACE PERIOD (75% EXTR)** | Step 1: the PC watchdog has been lost for 3 s (`PC_Watchdog_Timeout`), or the remote I/O on the source deck (`Remote_IO_Lost`). | Holds extraction at 75 % of its readback for up to 60 s. Goes back to **INACTIVE** if the link returns, otherwise on to step 2. A lost PC watchdog also closes the gate valve at once, by design, which opens the relay. | See [PLC COMMS shows OFFLINE](#plc-comms-shows-offline). A fault reset is refused during this step. |
| **CESIUM COOLDOWN** | Step 2: a hardware abort (`Src_Coolant_Flow_Low`, `Src_Body_Temp_High`, `Cesium_Oven_OverTemp`, `Filament_OverCurrent` or a sustained arc), or step 1 ran out. | Extraction off; the gate valve closed, so the relay opens; forced cooling. | Find the cause in the **PLC faults** list; the first-out alarm is marked with a star. Let the oven cool: see the warning below. |
| **FINAL KILL** | Step 3, once the caesium isn't hot and the filament is at 0 A. | Kills the relay; target, filament and thermionic off. | Clear the cause, then press **RESET FAULTS**. |
| **LATCHED SAFE (RESET REQ)** | Step 99: the sequence has ended. | Holds everything off. | Clear the cause, then press **RESET FAULTS**. |

While a sequence runs, the source supplies are greyed out with `automated source shutdown in progress`, and the PLC drives them in mode 1 (badge **AUTO**; the caesium oven's badge reads **HEATING**, although the PLC holds it at 20 °C). See [Shutting down](procedures/beam-shut-down.md).

> [!WARNING]
> **RESET FAULTS** during **CESIUM COOLDOWN** ends the cool-down at once, with the caesium possibly still hot. Wait for **FINAL KILL** or **LATCHED SAFE (RESET REQ)**.

## RESET FAULTS does not clear a fault

1. **The PLC refused the reset.** The PLC takes a reset only when the remote I/O sequence is at step 0, 3 or 99, the PC sequence at step 0, 2, 3 or 99, and the PC watchdog isn't lost. It refuses one during **GRACE PERIOD (75% EXTR)**, during **HOLD HOT PHASE** and **FILAMENT RAMP DOWN**, during step 2 of a remote I/O shutdown, and while the PC watchdog is lost. The **Command Log** still shows `written to PLC`: the PLC drops a refused reset. Press it again once the sequence has moved on. The **Fault reset** card on the [Interlocks](panels/interlocks.md) panel says whether a reset would be accepted now.
2. **The cause is still there.** A fault whose condition still holds latches again (after its delay). Clear the cause first.
3. **You acknowledged the alarm.** Acknowledging an alarm doesn't reset the fault. Only **RESET FAULTS** does.
4. **RESET FAULTS is greyed out**, with `Disabled: GUI to PLC communications are offline.`: see [PLC COMMS shows OFFLINE](#plc-comms-shows-offline).
5. **From a recipe.** Recipes can't reset faults: a recipe that tries is refused before it starts (`recipes may not reset faults ...`).

**RESET FAULTS** also clears the Spellman service's arc latches, and sends a fault clear to the Spellman supplies. See [Alarms and faults](procedures/alarms-and-faults.md).

## A supply does not switch on

1. **ENABLE is greyed out.** Hover over it and look the reason up in [Why a control is greyed out](messages.md#why-a-control-is-greyed-out). The usual ones: the safety relay is off; `automated source shutdown in progress`; `remote I/O to the source deck lost` (press **RESET FAULTS** once it is back); a sequence holds the supply (badge **SEQUENCE**); the service can't reach the device.
2. **It takes two clicks.** Switching on takes two clicks within 4 s: the first only arms the button, which then asks to confirm (for example **CONFIRM EXTRACTION?**).
3. **The command was refused.** The pop-up and the **Command Log** give the reason. Common ones:
    - `safety relay de-energised - it can't be switched on` (the PLC's supplies), `safety relay de-energised - supply is unpowered` (Spellman), `safety relay de-energised (or no PLC telemetry) - output held off` (magnet, steerer);
    - `locked out: <subsystem> is in automatic control mode <n> (<mode>)`;
    - for a Spellman supply, `output not enabled: the supply holds no current limit ...` and `Hard cap: the voltage setpoint the supply holds ...`;
    - for the magnets, `... didn't report its nominal ratings ...`.
4. **Accepted, but the output stays off.** The PLC still applies its own interlocks. The target, filament and thermionic contactors need the remote I/O, the source coolant flow and the vacuum permissive (`ion_beam.source.chamber.stat_vac_ok_for_gv`); extraction needs only the relay. A latched fault can hold a supply off: `Src_Coolant_Flow_Low`, for example, switches extraction, target and filament off. Read the **PLC faults** list.
5. **The badge reads RECIPE, or AUTO with no shutdown running.** The supply's PLC control mode is 2 (or 1), which only a `cmd_ctrl_mode` command sets, and the PLC keeps the modes over a restart. The PLC service accepts `cmd_ctrl_mode` 0 even while the supply is locked out, but no HMI control sends it except **HEATER ON** for the caesium oven. Ask an engineer.

## A setpoint goes back to zero

1. **A PLC source supply that is switched off.** The PLC zeroes the request of a supply that isn't on. Switch the supply on first, then set it.
2. **The safety relay is off.** The PLC zeroes every requested setpoint (the caesium oven's to 20 °C), so that no old value is waiting when the relay closes. The Spellman supplies lose their setpoints when unpowered, and the Spellman service forgets what it set. The analyser magnet and X-steerer are held at 0 A, and a relay trip forgets the SMU bias.
3. **A shutdown sequence** sets its own setpoints and copies them into the requests.
4. **An arc quench** sets the applied setpoint to 0 for 1 s (badge **ARC QUENCH**). The requested setpoint is kept, and comes back.

## The caesium oven does not heat

1. **HEATER ON is greyed out.** Read its tooltip. The oven can't be switched on with the relay off, with the remote I/O lost, or during a shutdown.
2. **The badge reads LOCKED.** The PLC locks the heater out while the relay is open, and while `Cesium_Oven_OverTemp`, `Cesium_Oven_HeatFail` or `Cesium_Oven_CoolFail` is latched. Clear the cause, let any shutdown sequence reach **FINAL KILL** or **LATCHED SAFE (RESET REQ)**, then press **RESET FAULTS**.
3. **It is held at 20 °C.** Forced cooling must be off for the oven to heat. The PLC sets the oven to 20 °C during a shutdown sequence, while forced cooling is on, and when the oven's heating permissive is lost. The **FORCED COOLING** button reads **COOLING ACTIVE** whenever the cooling air is on, whoever asked for it. As the PLC program reads, an operator shutdown stopped with **ABORT SHUTDOWN** leaves the PLC's own forced cooling on until a fault reset.
4. **It heats slowly.** The PLC raises the oven's setpoint gradually, by 0.0005 °C each time its heater loop runs, and limits the heater to half power. The program assumes the loop runs every 20 ms, which makes 1.5 °C per minute; the period set in the PLC isn't recorded. Typically the oven runs at 100 to 150 °C, settling over about 15 minutes. The setpoint box takes only the range in the tag registry; the PLC keeps the setpoint within 20 to 200 °C.
5. **The temperature shown.** With the relay off or the remote I/O lost, `~` marks the PLC's estimate, not a measurement.

## AUTO EMISSION drops out

The **AUTO EMISSION: ON** button goes back to **AUTO EMISSION** when the PLC drops auto-emission. It does that when:

1. the filament or the thermionic supply is switched off, or the filament is ramping down (**DISARMING...**);
2. a thermionic fault is latched: `Thermionic_OverCurrent` (above 200 mA), `Thermionic_OverVoltage` (above 2600 V), `Thermionic_Fail` (below 10 mA for 5 s at above 200 V with the filament above 36 A, which also opens the contactor) or `Thermionic_Sustained_Arc`;
3. `Src_Coolant_Flow_Low` latches, or the remote I/O is lost.

While it runs, auto-emission drives the filament current between 20 A (its cold start) and 36 A. Check the **PLC faults** list, clear the cause, let any shutdown sequence reach **FINAL KILL** or **LATCHED SAFE (RESET REQ)**, press **RESET FAULTS**, switch the supplies on again, then **AUTO EMISSION**. See [TESS Ion Source Control](panels/ion-source.md).

## The einzel current is high

The einzel's current, the second line of the **Source Einzel** readback in **TESS Ion Source Control**, is typically close to zero. Above 50 µA indicates a failure of the einzel electrode's insulators: switch the einzel off (one click on its **ENABLE** button) and report it to Josh Bird.

Before the first voltage setpoint or enable after a power-up, the Spellman service gives the supply a current limit of 50 µA, unless one has been set since (`default_current_ua` in `config/mpd_config.json`). When a supply runs into its current limit, its output voltage collapses. If the output falls below 10 % of its setpoint on 2 polls in a row, or the current rises above 500 µA, the service latches an arc: the alarm `source einzel: arc (output collapsed) - latched until fault reset` and the PLC fault `UNIT1_ARC_EXCEEDED` (see [Fault word 3 Spellman](messages.md#fault-word-3-spellman)).

## No beam on the Faraday cup

In the **Faraday Cup & SMU** panel:

1. **The beam current reads NO MEASUREMENT.** With **STATUS: SMU OFFLINE**: no data from the SMU service, the SMU not answering, or no PLC data. With **STATUS: SAFETY LOCK**: the safety relay is off, and the reading isn't valid.
2. **The bias is off or too low.** The beam-current reading is correct only with more than 50 V of bias (typical): press **ENABLE BIAS** and set it. A relay trip takes the bias to 0 V and forgets it.
3. **The cup isn't in the beam.** The button should read **FARADAY CUP: INSERTED**. **FARADAY CUP: TRAVELLING** (neither position switch made) or **FARADAY CUP: LOGIC ERROR** (both made): check the compressed air and the cup's switches. The cup can't move without air (`Disabled: Facility air pressure is lost. Actuator cannot be moved.`).
4. **The analyser magnet isn't set for the mass.** It must be on, at the current for the ion's mass at the beam energy: **CALCULATE & SET AMPS** in **Beamline Optics**, and the **Mass Scan** panel. See [Selecting a mass and tuning the beam](procedures/tuning-the-beam.md).
5. **The slits and the aperture ladder.** Both are set by hand (the slit boxes in **Beamline Optics** only record the widths in the event log). Typically the slits are 10 mm each for a first tune; the aperture ladder fully retracted is the largest opening.
6. **The source isn't producing beam yet.** Beam typically appears within 10 to 60 minutes as the caesium heats up, depending on the target material and on whether it has been run before. Until then the extraction and target currents are only the current through their 50 MΩ bleed resistors (0.02 mA per kV); they rise as the caesium heats, and about 1 mA of target current is the aim. Check the readbacks in **TESS Ion Source Control** against [Typical values](#typical-values), and the lenses and steering in **Beamline Optics**.
7. **The reading pauses for a moment** when the SMU changes range: the reading taken while it switches is discarded, and the one before stays on screen.
8. **Readings come slowly** after a mass scan was stopped with **TAKE CONTROL**: the SMU may still be at the scan's NPLC (see [A scan or the optimiser stops or does not start](#a-scan-or-the-optimiser-stops-or-does-not-start)).

In the simulator, beam appears only once the thermionic power is above 200 W, the target above 4 kV and the caesium above 50 °C.

> [!NOTE]
> On a latched fault the PLC changes its Faraday cup request; which way the cup then moves hasn't been checked on the machine. See [The PLC program](how-it-works/plc.md).

## A command was refused

1. Read the reason, in the pop-up or in the **Command Log** panel (tick **Problems only**), and look it up in [Why a command was refused](messages.md#why-a-command-was-refused).
2. The commands of a recipe, a mass scan or the optimiser aren't in the **Command Log**. Their refusal is on the sequence's status line and in its failure alarm, and every command is a COMMAND event in the event log.
3. `stale: sent <age> s ago (limit <limit> s)`: the service was busy. See [Commands time out](#commands-time-out).

See [Command Log](panels/command-log.md).

## Commands time out

The pop-up **No reply to command** (TIMEOUT, `no reply from <service> within <n> s`), or refusals as `stale`.

1. **Is the service running?** **Service not running** (`<service> service is not running or not connected`) means it isn't: check **Service Manager**.
2. **Is it busy?** A service takes commands once per cycle, and a device that doesn't answer makes a cycle longer:
    - one unanswered Modbus request holds a magnet service for about 2 s;
    - each Spellman query that isn't answered waits up to 1 s;
    - the SMU measures for longer at a high NPLC; its command time limit grows to match;
    - when the source turbo service connects to a stopped pump whose settings differ from what it needs, it corrects them and saves them to the pump's memory, taking no commands for about 30 s;
    - the vacuum service checks the gauge controllers' relay set points when it connects.
3. **Was the service restarted** while it handled the command? The **Service Manager** row shows recent restarts.

A command that timed out may still have been carried out: check the readback before you send it again.

## A recipe stopped

1. Read the status line in the **Recipe Execution** panel, `FINISHED (<recipe>): <message>`, and see which step is marked failed in the list. [Recipe messages](messages.md#recipe-messages) explains each message.
2. The usual causes:
    - a step's command was refused: `Step <n> failed: <tag> = <value>: rejected - <reason>`, where the reason is the service's;
    - a wait timed out, or its value had no live data for 10 s;
    - one of the recipe's stop conditions held (`Stopped at step <n>: fault <name> is active.`, or a condition and its message), or a reading they need had no live value for 3 s;
    - another sequence holds a subsystem the recipe needs;
    - the sequencer was stopped or restarted (`Stopped: the sequencer is shutting down ...`).
3. A recipe that only paused (`Paused: the operator took control of the ...`) carries on with **RESUME**.

Stopping or failing switches nothing off: outputs stay as the completed steps left them. Check the machine before you start again; a recipe always starts again from its first step. See [Recipe Execution and the recipe builder](panels/recipes.md).

## A scan or the optimiser stops or does not start

1. **START is greyed out.** Hover over it. The **Mass Scan** panel needs the magnet and SMU services running and permitting, and the analyser magnet switched on. The **Optimiser** panel needs both parameters chosen, their services permitting, and their outputs on.
2. **The start was refused.** The status line says `Status: start refused - <reason>`. See [Mass scan messages](messages.md#mass-scan-messages) and [Optimiser messages](messages.md#optimiser-messages).
3. **It stopped part-way.** The status line says why. A failed run also raises an alarm.
4. **After TAKE CONTROL.** A scan or the optimiser stops, and can't then use what you took: a mass scan can't put the SMU back to NPLC 1, and the optimiser can't put its parameters back. Set them yourself.

See [Mass Scan](panels/mass-scan.md) and [Optimiser](panels/optimiser.md).

## The horn keeps sounding

The horn plays the sound of the most urgent unacknowledged alarm of priority critical, high or medium, over and over, until that alarm is acknowledged or you press **SILENCE**. Low-priority, shelved and suppressed alarms never sound.

1. **Acknowledge the alarm.** Select it and press **Acknowledge selected**, or press **ACK ALL**. **SILENCE** stops the sound without acknowledging; a new alarm sounds again.
2. **It keeps coming back.** An alarm that goes active again is unacknowledged again, and sounds. Find and fix the cause. Meanwhile you can **Shelve...** it, with a reason, for 1 to 480 minutes. An engineer can give it a delay, or switch it off, in `config/alarm_config.json`, which the alarm service reads when it starts.
3. **ALARM SERVICE NOT RUNNING.** While `service_alarms` isn't running, the HMI sounds its own critical alarm, which can't be acknowledged. Start `service_alarms` in **Service Manager**; **SILENCE** stops the sound meanwhile.

A PLC fault's alarm stays in the list while the fault is latched, even when acknowledged: press **RESET FAULTS** once the cause is cleared.

If no sound plays at all, `SystemLogs/hmi.log` says `Alarm sound missing: <file> (run src/utils/make_alarm_sounds.py)`, or the PC has no audio device. See [Alarms](panels/alarms.md).

## The data viewer shows no live data

1. **Is it following live data?** After you pan or zoom, the sidebar button reads `Browsing - click to follow live`. Click it to follow the newest data again.
2. **Are the services running?** The viewer listens to the device services (PLC, vacuum, source turbo, Spellman, magnet, steerer, SMU and current monitor) itself. Check **Service Manager** in the HMI. The current monitor's service isn't started at present, so its channels have no data: see [Current Monitoring](panels/current-monitoring.md).
3. **Is the channel chosen?** Only the ticked channels are drawn, 20 at most.
4. **Is it the same system?** The services talk only to programs on the same PC, and the simulator uses its own ports (the real ones plus 10000). A viewer started by `run_simulation.py`, or by hand with `--sim`, listens to the simulator; one started without `--sim` listens to the real system. With `SINDIE_PORT_OFFSET` set, `--sim` keeps that offset instead.

A marker you add is written by the events service. **Marker not saved** means the events service of this system didn't receive it, for the same reasons as step 4. See [Using the data viewer](viewer/data-viewer.md).

## Gaps in the logged data

The logger writes the latest value of every channel 20 times a second. A gap, a break in a line, means the value was unknown then.

1. **A service was silent.** After 1 s of silence from a service, the logger writes that service's channels as unknown, with the events `Network Timeout: <topic> silent for >1s. Forcing NaNs.` and, when it is back, `Network Recovery: <topic> data stream restored.` Tick **Watchdog Drops** under **Event markers** in the data viewer to see them. Find out why the service stopped publishing: **Service Manager**, and its log.
2. **The device was unreachable or unpowered.** A service reports what it can't measure as unknown: its device's comms failed, or the relay was off (the source deck and Spellman readbacks).
3. **The logger wasn't running.** Then there are no rows at all. After a crash, its crash journal recovers the rows logged until about 5 s before it (`Recovered <n> logged rows from the crash journal`). Each start of the logger also writes one row of unknowns, so a line never joins across a restart.
4. **The disk wouldn't take the data.** See [The disk is filling up](#the-disk-is-filling-up). Up to 2 h of data waits in memory; beyond that the oldest is dropped.
5. **The last minutes are missing from the history.** The logger writes a file every 15 minutes. The viewer asks it to write what it holds when the viewer starts; if that isn't confirmed (`Logger flush not confirmed - the last minutes of history may be missing until its next flush` in `SystemLogs/viewer.log`), those minutes appear after the logger's next file.
6. **In live view**, samples more than 2 s apart are drawn with a break.

See [Data logging and storage](how-it-works/data-logging.md).

## The disk is filling up

When less than 2 GB is free, the logger warns, at most once an hour: `The data drive is low on space: <n> GB free for <folder> - the logger can't save data once it is full`. When the disk won't take a file, `[Logger I/O Error] Cannot write <chunk>: ...` follows: the data waits in memory and is tried again every minute for up to 2 h, and then the oldest is dropped (`Dropped unwritable chunk <chunk>: the disk hasn't taken data for over 2 h`).

What grows, in the data folder (normally `data/`) and the project folder:

| Folder or file | Holds | Cleared by the software |
|---|---|---|
| `data/parquet_logs/` | today's 15-minute files, and the logger's crash journal | Yes: at 02:00 each past day's files are merged into a day file, then deleted. A day that can't be merged stays and is tried again the next night. |
| `data/daily_logs/` | one file per day, with its 1-minute and 20-minute rollups | No |
| `data/events.db` | every event, including a COMMAND row for every command | No |
| `data/exports/` | the data viewer's exports and saved mass scans | No |
| `SystemLogs/` | one log per program, rotated at 5 MB with 5 old copies kept (30 MB at most each) | Yes, by rotation |
| `config/backups/` | the config editor's backups, the newest 50 of each file | Yes |

Each night the compactor also copies new and changed day files, a snapshot of `events.db` and the exports to OneDrive. The copy goes one way: deleting a file here doesn't delete its copy there. In simulation it is skipped.

> [!IMPORTANT]
> Don't delete anything from the lab PC: only Josh Bird deletes files there. If the disk is filling up, tell him. The software never deletes day files, events or exports. They are backed up to OneDrive each night; archiving to external drives, and to more than one cloud, is planned.

## A panel is missing or off the screen

1. **View > Panels** lists every panel: tick one to show it.
2. The panel may be a tab behind another one: look for its tab at the top of the group.
3. **View > Reset layout** puts every panel back where it starts, sized for this screen: **Vacuum System** over **System Health** on the left, the source panels over **Alarms** on the right, and the rest as tabs.
4. **View > Layouts** brings back a layout you saved. One saved by an older version of the HMI can't be restored: `The layout '<name>' could not be restored (saved by an older version?).`
5. The window is kept on a screen, and no bigger than it, when it opens. Drag a floating panel back by its title bar, or use **View > Reset layout**.

Panels that are closed or behind a tab aren't refreshed; they catch up as soon as they are shown. In the data viewer, **View > Sidebar** shows and hides the sidebar. See [A tour of the HMI](getting-started/screen-tour.md).

## The simulator does not start

`python run_simulation.py` starts the service manager in simulation mode, waits for the services, then opens the HMI and the data viewer. Its own window then shows the fault menu.

1. `A service manager is already running on port offset 10000 (another run_simulation.py?). Stop it first, or use --port-offset.`: another simulator is running. Stop it (type `q` in its window), or start this one with `--port-offset <n>`.
2. `Not every service came up - carrying on; check the services panel in the HMI.`: some services weren't **ONLINE** within 90 s. See [A service shows BACKOFF](#a-service-shows-backoff); the logs are in `data_sim/SystemLogs/`.
3. `The service manager has stopped - see service_manager.log in data_sim/SystemLogs`: read that log.
4. **A program stops at once** with a Python import error in its log: a package is missing. The packages are listed in `requirements.txt` (`pip install -r requirements.txt`).
5. **`Tag registry: <warning>` lines.** The simulator builds its own tag registry, `data_sim/system_tags.json`, from the configuration files at every start. A warning there (a missing PLC interface file, say) means the registry may be incomplete.

What the programs it starts print goes to `data_sim/SystemLogs/simulation_manager_console.log`, `simulation_hmi_console.log` and `simulation_viewer_console.log`.

The simulator uses ports offset by 10000 and keeps its data in `data_sim/`, so it runs beside the real system without touching it. An HMI or data viewer started by hand with `--sim` joins it: `--sim` chooses the simulator's ports (unless `SINDIE_PORT_OFFSET` is already set) and the `data_sim/` folder.

See [The simulator](how-it-works/simulator.md).

## See also

- [Messages and what they mean](messages.md)
- [Alarms and faults](procedures/alarms-and-faults.md)
- [System Health](panels/system-health.md)
- [Service Manager](panels/service-manager.md)
- [The PLC program](how-it-works/plc.md)
- [Starting the software](getting-started/starting-the-software.md)
- [Glossary](glossary.md)
