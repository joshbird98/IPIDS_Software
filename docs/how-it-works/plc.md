# The PLC program

What the Siemens S7-1200 PLC does and how it decides it: machine states, the safety relay enable, interlocks and latched faults, the shutdown sequences, arc quench, the gate valve, the caesium oven, auto-emission, setpoint arbitration, and the data block it shares with the PC. It ends with the known issues in the program.

> [!NOTE]
> The HMI's [Interlocks](../panels/interlocks.md) panel follows this page (`src/core/interlocks.py`): it shows each permissive below with what holds it, and flags a PLC verdict that doesn't match. If the program changes, change both.

> [!NOTE]
> The PLC program is kept in its TIA Portal project, not in this repository. The repository holds the interface block the PC reads (`plc/generated/PLC_PC_Interface.scl`) and the script that makes the fault data types (`plc/build_plc_types.py`). This page describes the program from an export of it; names such as FB20 are its blocks. If the program has been changed since, check the PLC.

## How the program runs

| Block | Runs | What it does |
|---|---|---|
| OB100 | once, when the PLC starts | Sets machine state 0, clears every enable and command, puts the Faraday cup in, restores the requested setpoints and control modes from DB11, re-allows the gate valve and clears the relay kill. |
| OB1 | every cycle, as fast as the PLC runs it | FC1 reads the digital inputs. FC2 reads the analogue inputs (relay state, scaling, calibration). FB2 runs the thermal models, FB1 the PC watchdog, FB20 the interlocks, faults, machine state, permissives and outputs, FB5 the shutdown sequences and fault reset. FC4 copies the fault words, handles the fault reset and keeps setpoints and modes. FC99 and FB6 write the outputs and arbitrate the setpoints. A cycle counter is incremented last. |
| OB30 | cyclic interrupt; the code assumes every 20 ms | FB3, the caesium oven heater loop, and FB4, the auto-emission loop. |

The functions and function blocks are written in SCL. The OB30 period itself isn't recorded in the software.

## Machine states

The PLC publishes one machine state, `ion_beam.system.machine_state`. The conditions are tested in this order, and the first that holds wins:

| State | Name | When |
|---|---|---|
| 99 | Fault | A fault is latched in word 0, 1, 2, 4 or 5. Word 3 (Spellman) is left out. |
| 3 | Live / HV | The safety relay is closed. |
| 2 | Ready | The relay permissive is made (see below), relay open. |
| 1 | Safe / Not Ready | None of the above. |
| 0 | Init | Set at start-up by OB100, until the first pass of FB20. |

## The safety relay

The PLC cannot close the safety relay. It gives the relay loop an enable, and reads the relay's state back.

### The enable and the permissive

The enable output, `DO_SAF_HVRelay_En`, is `Cmd_Enable_Safety` AND `Saf_Rel_Permissive`. `Cmd_Enable_Safety` is the PC's request, `ion_beam.system.cmd_enable_safety`, sent by **ENABLE SAFETY RELAY** in the **System Health** panel. The relay permissive needs all of these:

- compressed air pressure OK;
- source coolant flow OK;
- magnet coolant OK;
- the gate valve open;
- no relay kill from the shutdown sequencer;
- no `Cesium_Oven_OverTemp` fault.

So the PLC opens the loop on a loss of compressed air, a loss of either coolant, a gate valve that isn't open, caesium over-temperature, or the relay kill at step 3 of a shutdown sequence.

The relay's state comes back over a fibre to an analogue input: the PLC reads the relay as closed when the raw value is above 5000. It is published as `ion_beam.facilities.safety_relay_active`.

### Refused, tripped and dropped on purpose

- **Refused.** If the relay hasn't closed 5 s after the enable, the PLC latches `Safety_Relay_Refused`. The timer is published as `ion_beam.system.safety_timer_elapsed_ms` and `safety_timer_total_ms`; the HMI shows it as the **PRESS PANEL START** countdown on the button.
- **Dropped on purpose.** If the PC drops `Cmd_Enable_Safety` while the relay is closed and the permissive is made, the PLC treats the opening as intentional: no fault.
- **Tripped.** Any other opening latches `Safety_Relay_Tripped` and puts the machine in state 99. This includes an opening caused by the permissive, such as the gate valve closing.

The PLC clears `Cmd_Enable_Safety` itself if the relay hasn't closed within the 5 s, during any shutdown step, or when the permissive breaks.

### Held on while the source is hot

After those rules, the PLC sets `Cmd_Enable_Safety` back on whenever the gate valve's close permissive is false: high voltage present, filament hot or caesium hot. Neither the PC nor the rules above can then drop the relay while the source is hot. The PLC can still open the relay through the permissive, for example when the gate valve closes. The HMI shows the button as **RELAY ENERGIZED (LOCKED)** then.

### Beacons and buzzer

| Output | On when |
|---|---|
| Red beacon | the relay is closed |
| Amber beacon | a fault is latched in word 0, 1, 2, 4 or 5 |
| Green beacon | the relay is open |
| Buzzer | for 3 s when the relay closes |

### The remote I/O on the source deck

The source deck's I/O is a Profinet device reached over fibre. `Remote_IO_Lost` is set when that device reports a fault and the relay has been closed for at least 25 s; it starts shutdown scenario A. Because of the 25 s condition it also clears whenever the relay opens. The contactors of the target (cathode), filament and thermionic supplies drop at once, because their permissive needs the remote I/O. What the module itself does with its outputs when the link goes isn't recorded in the software.

## Interlock inputs

| Input | Published as | Used for |
|---|---|---|
| `DI_PNM_PS_OK`, compressed air pressure switch | `ion_beam.facilities.stat_air_press_ok` | Relay permissive; `Air_Pressure_Low` at once. The gate valve and Faraday cup outputs need air. |
| `DI_SRC_FS_OK`, source coolant (Syltherm) flow switch | `ion_beam.facilities.stat_src_coolant_ok` | Relay permissive and source supply permissive, both at once; `Src_Coolant_Flow_Low` after 2 s. |
| `DI_MAG_COOL_OK`, magnet water flow and thermal cut-off switch | `ion_beam.facilities.stat_mag_coolant_ok` | Relay permissive; `Mag_Coolant_Flow_Low` at once. |
| `DI_SRC_GV_ZSO`, `DI_SRC_GV_ZSC`, gate valve open and closed limit switches | `ion_beam.source.chamber.stat_gv_open`, `stat_gv_closed` | Relay permissive (valve open); `GateValve_Timeout`; `GateValve_Conflict`. |
| `AI_SRC_Body_TI`, source body temperature | `ion_beam.source.chamber.rb_body_temp` | `Src_Body_Temp_High`. |
| `AI_SRC_CsOven_TI`, caesium oven temperature | `ion_beam.source.cesium.rb_temp` | `Cesium_Oven_OverTemp`; the heater loop. |
| `AI_Fibre_Receiver_Raw`, the relay state | `ion_beam.facilities.safety_relay_active` | Machine state, trips, everything that needs the relay. |

The oven thermocouple is a 4 to 20 mA transmitter scaled 0 to 1100 °C. The program subtracts a fixed 121 °C from it; the comments beside that line give other figures (92 °C, and "approx 46C too high").

The PLC has no vacuum input. Vacuum status reaches it from the PC: the gate-valve vacuum permissive (`Stat_Vac_OK_For_GV`) and the gauge flags, written by `service_plc` from the vacuum service's telemetry.

Two more permissives, besides the relay's:

- **Source supply permissive** (target, filament and thermionic contactors): remote I/O present, source coolant OK, and the PC's `Stat_Vac_OK_For_GV`.
- **Extraction** needs only the relay closed.

## Faults

### Latching

Every fault latches in the retained block DB11 and is copied to DB10 every cycle, where the PC reads it. It clears only on a fault reset. There are six fault words of 32 bits each:

| Word | Tag | Holds |
|---|---|---|
| 0 System | `ion_beam.faults.word_0_system` | safety relay, air, coolants, gate valve, PC watchdog, remote I/O |
| 1 Pumps | `ion_beam.faults.word_1_pumps` | source and loadlock turbo pumps |
| 2 Source | `ion_beam.faults.word_2_source` | source body, caesium oven, filament, extraction, target, thermionic |
| 3 Spellman | `ion_beam.faults.word_3_spellman` | Spellman units 1 to 4 |
| 4 Magnet | `ion_beam.faults.word_4_magnet` | analyser magnet and X-steerer supplies |
| 5 Gauges | `ion_beam.faults.word_5_gauges` | GRAPHIX controllers and gauges VG1 to VG6 |

### Faults the PLC detects

Every fault in words 0, 1, 2, 4 and 5 also puts the machine in state 99 and lights the amber beacon. The table gives what each one does besides.

| Fault | Condition | Also |
|---|---|---|
| `Safety_Relay_Refused` | relay not closed 5 s after the enable | |
| `Safety_Relay_Tripped` | relay opened other than by an intentional drop | |
| `Air_Pressure_Low` | air switch open, at once | relay permissive lost while the switch is open |
| `Src_Coolant_Flow_Low` | source coolant switch open for 2 s | extraction, target and filament switched off; auto-emission off; hardware abort |
| `Mag_Coolant_Flow_Low` | magnet coolant switch open, at once | relay permissive lost while the switch is open |
| `GateValve_Timeout` | valve not at the requested position within 5 s | |
| `GateValve_Conflict` | both limit switches made | |
| `PC_Watchdog_Timeout` | PC watchdog unchanged for 3 s | gate-valve open permissive lost; shutdown scenario B |
| `Remote_IO_Lost` | remote I/O fault, with the relay closed for 25 s | shutdown scenario A |
| `Src_Body_Temp_High` | above 40 °C for 200 ms, relay closed | extraction, target and filament switched off; hardware abort |
| `Cesium_Oven_OverTemp` | above 200 °C for 200 ms | relay permissive lost; heating locked out; cooling; hardware abort |
| `Cesium_Oven_HeatFail` | heater on and error above 500 °C for 600 s | heating locked out; oven mode 0, setpoint 20 °C |
| `Cesium_Oven_CoolFail` | cooler on, above 30 °C, error below −5 °C for 40,000 s | as `HeatFail`, plus cooling |
| `Filament_OverCurrent` | above 38 A | contactor off; hardware abort |
| `Filament_OverVoltage` | above 10 V | |
| `Filament_Fail` | readback more than 0.5 A from the setpoint for 1.5 s (10 s in mode 3), after a 3 s grace | contactor off |
| `Extraction_OverCurrent`, `Extraction_OverVoltage` | above 10 mA, above 30 kV | extraction switched off |
| `Extraction_Fail` | error above 0.6 kV for 1 s, not while arcing | extraction switched off |
| `Target_OverCurrent`, `Target_OverVoltage` | above 15 mA, above 10 kV | target switched off |
| `Target_Fail` | error above 0.5 kV for 2 s | contactor off |
| `Thermionic_OverCurrent`, `Thermionic_OverVoltage` | above 200 mA, above 2600 V | auto-emission off |
| `Thermionic_Fail` | below 10 mA for 5 s while above 200 V with the filament above 36 A | contactor off |
| `Extraction_Sustained_Arc`, `Target_Sustained_Arc`, `Thermionic_Sustained_Arc` | 3 arcs (see Arc quench) | hardware abort |

### Faults reported by the PC

The PLC doesn't talk to the turbo pumps, the Spellman supplies, the magnet supplies or the gauge controllers. The PC writes their status flags into DB10, and the PLC latches each flag as a fault: words 1, 3, 4 and 5. The flags the PC sends are listed in [Services and messaging](services.md#status-flags-for-the-plc).

- Faults in words 1, 4 and 5 put the machine in state 99, light the amber beacon and set the Faraday cup request off. GRAPHIX comms faults and VG1 or VG2 faults also block the gate valve.
- Faults in word 3 (Spellman) are latched, but left out of the fault summary: they set neither state 99 nor the amber beacon.

### Faraday cup

On any latched fault in words 0, 1, 2, 4 or 5, or a remote I/O loss, the PLC sets the Faraday cup request `Cmd_Insert_FC` off. The shutdown sequences set it on (cup in), and OB100 puts the cup in at start-up. See the known issues for how the output follows the request.

### Resetting faults

**RESET FAULTS** in the System Health panel sends `ion_beam.system.cmd_fault_reset` to the PLC service, and to the Spellman service for its own arc latches. A recipe may not send it. In the PLC, two blocks act on it in the same cycle:

1. FB5 accepts it if the remote I/O sequence is at step 0, 3 or 99, the PC sequence is at step 0, 2, 3 or 99, and the PC watchdog is not lost. It then returns both sequences to step 0, clears the relay kill and forced cooling, re-allows the gate valve and ends the automatic shutdown.
2. FC4, later in the cycle, clears all six fault words if both sequences are now at step 0, 3 or 99.
3. FC4 clears the reset command every cycle, whether or not the reset was accepted. A refused reset has to be sent again later.

So a reset is refused during a hold (step 1), during step 2 of a remote I/O shutdown, and during an operator shutdown (steps 10 and 11). It is accepted at step 2 of scenario B, where it ends the cool-down: see the known issues.

Acknowledging an alarm in the HMI doesn't reset a PLC fault. See [Alarm handling](alarms.md).

## Shutdown sequences

FB5 runs three shutdown scenarios on two step counters: the remote I/O sequence (`ion_beam.system.remote_shutdown_step`) and the PC sequence (`ion_beam.system.pc_shutdown_step`). The System Health panel shows the higher of the two.

While any of them runs, `ion_beam.system.src_auto_sdown` is on and the PLC takes over the source supplies in mode 1: extraction, target and filament follow the sequencer's setpoints, the thermionic current is held at 0 mA and the caesium oven at 20 °C. The PLC copies these setpoints back into the PC's requests.

A **hardware abort** sends the PC sequence straight to step 2. Its causes are:

- a sustained arc on the extraction, target or thermionic supply;
- `Filament_OverCurrent`;
- `Src_Body_Temp_High`;
- `Src_Coolant_Flow_Low`;
- `Cesium_Oven_OverTemp`.

**Sweeping.** While the filament model is above 20 A and the caesium is hot, the sequence keeps the target (at least 3 kV) and the filament on while the caesium cools. This applies in scenario B step 1 and in scenario C.

### Scenario A: remote I/O lost

| Step | System Health shows | Entered | The PLC | Leaves to |
|---|---|---|---|---|
| 1 Hold | GRACE PERIOD (75% EXTR) | `Remote_IO_Lost`; the PC sequence is set to 0 | Extraction to 75 % of its readback; auto-emission off; Faraday cup in; forced cooling if the caesium is hot. | 0 if the remote I/O comes back; 2 after 60 s or on a hardware abort |
| 2 Cooling | CESIUM COOLDOWN | from 1, or at once if a PC sequence is at step 2 to 9 | Extraction off, setpoint 0; gate valve closed, which opens the relay; forced cooling. | 99 when `Remote_IO_Lost` clears, which it does as the relay opens; otherwise 3 once the caesium isn't hot |
| 3 Tripped | FINAL KILL | from 2 | Relay kill; target, filament and thermionic off. | 0 on a fault reset |
| 99 Latched | LATCHED SAFE (RESET REQ) | from 2 | Waits for a fault reset. | 0 on a fault reset |

### Scenario B: PC watchdog lost, or a hardware abort

| Step | System Health shows | Entered | The PLC | Leaves to |
|---|---|---|---|---|
| 1 Hold | GRACE PERIOD (75% EXTR) | PC watchdog lost, from step 0, 10, 11 or 99 | Extraction to 75 % of its readback. Sweeping: target at least 3 kV, filament at 75 % of its readback, kept within 20 to 38 A. Otherwise target off and filament setpoint to 0 at once. | 0 when the watchdog comes back; 2 after 60 s or on a hardware abort |
| 2 Cooling | CESIUM COOLDOWN | a hardware abort from step 0, 10, 11 or 99, or from 1 | Extraction off; gate valve closed, which opens the relay; forced cooling. Sweeping: target at least 3 kV, filament from its readback (20 to 38 A) ramping down at 1 A/s. | 3 when the caesium isn't hot and the filament is cold at 0 A; 0 on a fault reset |
| 3 Tripped | FINAL KILL | from 2 | Relay kill; target, filament and thermionic off. | 99 when the watchdog is back and no abort is active |
| 99 Latched | LATCHED SAFE (RESET REQ) | from 3 | Waits for a fault reset. | 0 on a fault reset |

A lost PC watchdog also latches `PC_Watchdog_Timeout`, which takes away the gate valve's open permissive (see [Gate valve](#gate-valve)). The PLC then closes the valve at once, which opens the relay, so the step-1 hold acts on supplies without power. This is intended (Josh Bird, October 2026): without the PC the PLC has no vacuum readings, so it assumes the worst. The gauge controller's set-point relay is in the safety relay's hardwired loop as well.

### Scenario C: operator shutdown

| Step | System Health shows | Entered | The PLC | Leaves to |
|---|---|---|---|---|
| 10 Hold hot | HOLD HOT PHASE | **INITIATE SHUTDOWN** (`ion_beam.system.cmd_source_shutdown`), with the relay closed and both sequences at 0 | Extraction and thermionic off; Faraday cup in; forced cooling. Sweeping: target at least 3 kV, filament at most 15 A. | 11 when the caesium isn't hot; 0 on **ABORT SHUTDOWN** (`cmd_cancel_shutdown`) |
| 11 Ramp down | FILAMENT RAMP DOWN | from 10 | Target setpoint 0; filament ramps down at 1 A/s. | 0 at 0 A, with target and filament off, the gate valve closed and the relay enable dropped |

The HMI offers **ABORT SHUTDOWN** only at step 10, and **INITIATE SHUTDOWN** only with no shutdown sequence running, the relay closed, and a supply on or the source not cold. The step comments in the interface block list only steps 0, 1, 2, 3 and 99.

## Arc quench

The PLC watches the extraction, target and thermionic supplies for arcs.

| Case | Rule |
|---|---|
| Arc, extraction and target | readback below 0.5 kV, current above 5.0 mA, applied setpoint above 1.0 kV, supply on, relay closed |
| Arc, thermionic | readback below 50 V, current above 100 mA, supply on, relay closed |
| At each new arc | the supply's arc count goes up by 1 and it is quenched |
| Quench | for 1000 ms the applied setpoint is 0 and the supply's `ctrl_mode` reads 4; for the thermionic supply the contactor is opened too. The PC's requested value and mode are kept, so the setpoint comes back afterwards. |
| Count | falls by 1 every 10 s |
| Count reaches 3 | the supply's `_Sustained_Arc` fault latches: a hardware abort, scenario B step 2 |

The source panel shows **ARC QUENCH** on a supply in mode 4. The Spellman supplies have their own arc detection, in the Spellman service: see [Services and messaging](services.md#spellman-arc-detection).

## Gate valve

| Item | Rule |
|---|---|
| Open permissive (`stat_gv_open_permissive`) | the PC's `Stat_Vac_OK_For_GV`, and none of these latched: `PC_Watchdog_Timeout`, GRAPHIX 1 or 2 comms, VG1 or VG2 not found, type mismatch, rapid rise or above set point |
| Close permissive (`stat_gv_close_permissive`) | extraction and target readbacks below 0.15 kV, filament not hot, caesium not hot |
| Output | the request (`Cmd_Open_GV`) AND the sequencer's permission AND air OK AND the open permissive |
| `GateValve_Timeout` | the valve hasn't reached the requested position within 5 s |
| `GateValve_Conflict` | both limit switches are made |

The PLC overrides the PC's request in both directions:

- it sets `Cmd_Open_GV` off when the open permissive is lost or the shutdown sequencer forbids opening, hot or not;
- it sets `Cmd_Open_GV` on when closing isn't permitted and opening is. So a PC request to close the valve is overridden while the source is hot or at high voltage.

The PLC itself still closes the valve when the open permissive goes, and at shutdown step 2. Closing the valve opens the safety relay, because the relay permissive needs the valve open.

On the PC side, `service_plc` clears the OPEN request once the valve has been shut by its interlock for 3 s in a row (closed, permissive gone, OPEN still set), with a WARNING event. The valve then stays shut until **OPEN** is pressed again. The gate valve controls can never be leased by a sequence.

## Caesium oven

The oven heater loop runs in FB3 in OB30. The rates below assume the 20 ms period the code is written for.

| Item | How |
|---|---|
| Setpoint ramp | up 0.0005 °C per call (1.5 °C/min), down 0.01 °C per call (30 °C/min) |
| Controller | PI with feed-forward. Thermal resistance interpolated from 7.2 at 47 °C to 4.36 at 80 °C, kept within 2.5 to 9; feed-forward power on a 125 W scale; Kp 0.183, Ki 0.00026, set in the code; integration only within ±2 °C, integral within ±50; duty cycle at most 50 %. |
| Output | pulse-density: on every fifth call, a full-scale pulse on `AO_SRC_CsOven_PWM` whenever the accumulator passes 100 |
| Mode and setpoint | forced to mode 1 at 20 °C during a shutdown, during forced cooling, or when the heating permissive is lost; otherwise the PC's mode, with the setpoint kept within 20 to 200 °C |
| Heating lockout (`stat_lockout`) | relay open, or `Cesium_Oven_OverTemp`, `HeatFail` or `CoolFail` latched |

For the oven, mode 1 is how the PC switches the heater on, and the source panel shows **HEATING**.

**Cooling.** A cooling demand opens the oven's air valve (`DO_SRC_CsCooler_SOV_Stop` off) if the air pressure is OK. Cooling is demanded on any of: forced cooling by a shutdown sequence; the relay dropping while the oven is hot; over-temperature while hot; loss of the heating permissive while hot; the PC's force-cooling command (`ion_beam.source.cesium.cmd_force_cooling`); a latched `Cesium_Oven_CoolFail`; the relay open.

## Thermal models

With the relay open there are no readbacks from the source deck, so FB2 keeps two models that drive the "hot" interlocks in their place. FB2 runs on a 100 ms timer.

| Model | Behaviour | Hot when |
|---|---|---|
| Filament, `Fil_Thermal_Amps` | moves up by at most 0.5 A and down by 0.2 A per tick | above 1.0 A (`Fil_Is_Hot`) |
| Caesium, `Cs_Thermal_Temp` | follows the thermocouple while the relay is closed; with it open, falls 0.01 °C per tick with forced air or 0.0025 °C without, down to 20 °C | above 30 °C (`Cs_Is_Hot`, published as `ion_beam.internal.cs_is_hot`) |

While the relay is closed, FC2 overwrites both model values with the live readbacks, as long as the raw value is below 27000. Both values are retained in DB11, so they survive a PLC restart. The PLC publishes its estimate of the oven temperature as `ion_beam.source.cesium.estimated_temp`.

## Auto-emission

With auto-emission on (`ion_beam.source.thermionic.cmd_auto_emission`), FB4 sets the filament current to hold the thermionic emission current at its setpoint.

| Parameter | Value |
|---|---|
| Input filter | α = 0.06 |
| Gains | Kp 0.005 and Ki 0.05, scaled by (25 / max(25, filament current in A))³: lower above 25 A, unchanged below |
| Cold start | at least 20 A |
| Ramp up | 0.02 A per call below 20 A, 0.1 A per call above |
| Ramp down | 1.0 A per call |
| Maximum | 36 A |
| Anti-windup | back-calculation |
| Filament mode while active | 3, cascaded |
| Drops out when | the filament or the thermionic supply is off, the filament is ramping down, or a thermionic fault is latched |

The source panel locks the filament setpoint and shows **AUTO-EMIS** while auto-emission is on.

## Setpoint arbitration and control modes

FB6 decides, every cycle, what each source supply is actually given (its `sp_actual_` tag) from what the PC requested (its `sp_requested_` tag).

First, if the relay is open, the PLC zeroes the PC's requested setpoints (the caesium oven's to 20 °C), so no old value is waiting when the relay closes again. Then, for each supply, the first case that applies:

1. **Shutdown sequence running.** Mode 1; the sequencer's setpoint (thermionic 0 mA, caesium 20 °C), copied back into the PC's request.
2. **Arc quench** (extraction, target, thermionic). Mode 4; applied setpoint 0; the PC's request is left alone.
3. **Auto-emission** (filament). Mode 3; the setpoint comes from FB4.
4. **Normal.** The mode is the PC's `cmd_ctrl_mode`. The request is zeroed if the supply is off. Otherwise the applied setpoint is the request kept within the PLC's limits.

### Control modes

| Mode | Name | Set when | The source panel shows |
|---|---|---|---|
| 0 | Normal | the PC's default | no badge |
| 1 | PLC auto | the PLC runs a shutdown sequence (all five source supplies); for the caesium oven, the heater is on | **AUTO**; **HEATING** for the oven |
| 2 | Recipe | requested by the PC; the PLC passes it through and nothing in the program acts on it | **RECIPE** |
| 3 | Cascaded | the filament while auto-emission drives it | **AUTO-EMIS** while auto-emission is on |
| 4 | Arc quench | a supply during its 1 s quench | **ARC QUENCH** |

The PC refuses commands to a subsystem whose PLC mode is above 0 ("locked out: ... automatic control mode"), except a change of mode itself and the caesium oven's mode 1. Recipes and other sequences don't use the PLC's modes: they hold control leases in the PC instead (see [Services and messaging](services.md#control-leases)).

### Limits

| Supply | PLC limit | PC limit (from `config/plc_config.json`) |
|---|---|---|
| Extraction | 0 to 20 kV | 0 to 20 kV |
| Target | `Target_Min_Voltage` to 10 kV: the minimum is 3.0 kV while the caesium is hot, otherwise 0 | 0 to 10 kV |
| Filament | 0 to 40 A, slewed at 0.1 A per 100 ms | 0 to 33 A |
| Thermionic | 0 to 150 mA | 0 to 150 mA |
| Caesium oven | 20 to 200 °C | 0 to 150 °C |
| Steering X and Y | ±200 V, and 0 unless switched on with the relay closed | ±200 V |

The PC refuses a request outside its own limits before it reaches the PLC; the PLC then applies its own. While the PLC reports the caesium hot (`ion_beam.internal.cs_is_hot`), the PC also refuses to switch off the target, filament or thermionic supply, or to set a target voltage below `ion_beam.internal.target_min_voltage`, so that caesium vapour can't coat the source's ceramics.

The extraction supply has a fixed calibration: output (V − 0.0304) / 0.9752, readback 1.0224 × V − 0.0125.

### What survives a PLC restart

FC4 saves the five source setpoints and control modes in DB11 when they change, and OB100 restores them at start-up. Every enable starts off.

## DB10 and DB11

The PLC and the PC share DB10, `DB_PC_Interface`: a non-optimised block, not retained, laid out by `plc/generated/PLC_PC_Interface.scl`.

| Area | Bytes (repository layout) | Written by |
|---|---|---|
| `To_PC` | 0 to 195 | the PLC: states, readbacks, applied setpoints, modes, fault words |
| `From_PC` | 196 to 295 | the PC: commands, setpoints, the watchdog, and status flags from its other services. The PLC also writes here: it consumes commands, drops enables and replaces setpoints. |
| `Internal` | 296 to 315 | the PLC: sequencer setpoints, thermal-model and quench flags. The PC reads it too (`ion_beam.internal.cs_is_hot`, `ion_beam.internal.target_min_voltage`). |

DB11, `DB_PC_Retain`, is retained and the PC doesn't touch it. It holds the six fault words, the requested setpoints and modes, the two thermal-model values, the dose count and the relay's previous state.

### The 100 ms poll

`service_plc` reads the whole of DB10 in one read every 100 ms and publishes every tag on `PLC_DATA`, the PC's own requests included, as the PLC holds them. While the PLC's cycle counter changes between reads, the service reports the CPU as `RUN`, otherwise `STOP`.

- A command writes only its own bytes. A single bit is a read-modify-write of its byte.
- Status flags from the other services are written in batches, and only when they change.
- If a read or write fails, the service drops the connection and makes it again; meanwhile it publishes every PLC value as unknown.

### The watchdog

The PC increments `ion_beam.system.plc_watchdog` every 0.5 s (counting to 1000, then from 0 again). If it is unchanged for 3 s, FB1 sets `ion_beam.system.pc_plc_comms_lost`, latches `PC_Watchdog_Timeout`, withdraws the gate valve's open permissive and starts shutdown scenario B.

> [!NOTE]
> Restarting `service_plc`, from the **Service Manager** panel or by the config editor, stops the watchdog for a few seconds.

## The shared fault registry

The fault bits are defined once, in `config/fault_config.json`: six words (`UDT_Fault_Word_0_System` to `UDT_Fault_Word_5_Gauges`), each with its registry tag and its named bits, and for each bit a severity (1 or 2) and a description.

- **The PLC side.** `python plc/build_plc_types.py` turns the file into `plc/generated/faults.udt`, a TIA Portal source with one data type per word; unused bits are named `Spare_<byte>_<bit>`. DB10 and DB11 use these types. A bit added to the file means nothing until the PLC program sets it.
- **The PC side.** A bit written `x.y` is byte x, bit y of the word as Siemens stores it, which is bit (3 − x) × 8 + y of the 32-bit value the PC reads (`src/core/fault_registry.py`). `service_plc` publishes each bit as `faults.<Name>` and writes a FAULT event on each change. The alarm service raises one alarm per bit, and the System Health panel lists the latched ones by name and description.

Changing the file in the HMI's config editor restarts `service_plc` and `service_alarms`; restart the HMI too, as it reads the file when it starts.

## Known issues in the PLC program

These were found while checking the program and the software against each other, and are under review. Until they are resolved, allow for them.

- **DB10 layout.** The repository's interface has a `Beamline_Steerer_Magnet` block of status flags at byte 278 that the program export doesn't: 316 bytes against 314, and every tag from byte 278 on 2 bytes apart. Check which layout the PLC holds. With the shorter one the PC's 316-byte read would fail, so the PC would stop the watchdog and the PLC would start scenario B.
- **A relay trip doesn't start a shutdown.** The trigger meant for a relay trip compares a value FB20 has already updated earlier in the same cycle, so it never fires. `Safety_Relay_Tripped` latches, but no sequence starts, even with the source hot.
- **Step 2 opens the relay at once.** Step 2 closes the gate valve, and the relay permissive needs the valve open, so the relay opens straight away. The target hold and the filament ramp of step 2 then act on supplies without power, and in scenario A step 3 is never reached.
- **A fault reset at step 2 ends the cool-down.** FB5 accepts a reset at PC step 2 and returns the sequence to 0, with the caesium possibly still hot.
- **A stale intentional-drop flag can hide a trip.** If the PC drops the enable while the source is hot, the drop is flagged as intentional, but the hot hold puts the enable back and the flag stays set. A later real trip may then not latch `Safety_Relay_Tripped`.
- **Extraction arc detection can't trigger.** It needs more than 5.0 mA on a readback that is scaled 0 to 5.0 mA and clipped there. `Extraction_OverCurrent` (10 mA) and `Extraction_OverVoltage` (30 kV) are also beyond their readback scales, and `Target_OverVoltage` (10 kV) equals its scale maximum.
- **The Faraday cup output is written twice.** FB20 writes it as the request AND air OK; FC99, which runs later, writes it as NOT the request. Check which way the cup actually moves.
- **Spellman faults are left out of the fault summary.** Word 3 sets neither machine state 99 nor the amber beacon.
- **`Cesium_Oven_HeatFail` can't trigger.** It needs an error above 500 °C, but the setpoint is limited to 200 °C.
- **An oven above 180 °C reads as cold with the relay open.** FC2 sets the caesium model to 0 whenever it is above 180 °C, so with no readback the oven isn't treated as hot. The 200 °C setpoint limit allows such temperatures.
- **The active-cooling request is never set.** Both branches in FB3 write it off.
- **`stat_cooling_wait` is always off.** The compressor timers behind it are commented out.
- **Shutdown flags are left set.** Cancelling at step 10 leaves forced cooling on. Finishing step 11 leaves the sequencer's gate-valve permission off until a fault reset, so the gate valve won't open until then.
- **Not saved, never written.** The steering setpoints are restored at start-up but never saved, and the dose count in DB11 is never written.
- **FB2 timing.** FB2 runs every 100 ms, but its comments speak of 1 s ("1Hz", "-0.2A eq. per sec"), so the model rates may not be what was meant.
- **Batched status writes can undo a PLC change (PC side).** `service_plc` writes the other services' flags by reading a span of bytes, patching it and writing it back. The span includes bits the PLC writes, such as `Cmd_Open_GV` next to `Stat_Vac_OK_For_GV`, and `Cmd_Insert_FC`; a change the PLC makes between the read and the write would be undone.

## See also

- [Interlocks](../panels/interlocks.md)
- [System overview](overview.md)
- [Services and messaging](services.md)
- [Alarm handling](alarms.md)
- [System Health](../panels/system-health.md)
- [Alarms and faults](../procedures/alarms-and-faults.md)
- [Shutting down](../procedures/beam-shut-down.md)
