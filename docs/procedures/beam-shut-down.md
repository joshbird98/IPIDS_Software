# Shutting down

How to take SINDIE from a running source to a cold, safe one. You can let the PLC do it (**INITIATE SHUTDOWN**, about 20 minutes) or do it by hand. Either way, the caesium is cooled before the source is let go. This page also covers what the PLC does by itself when it loses the PC or detects a hardware fault.

## The automatic way: INITIATE SHUTDOWN

**INITIATE SHUTDOWN** on the **System Health** panel hands the shutdown to the PLC. It takes about 20 minutes. The button is available while the safety relay is on and something in the source is still on or hot.

The PLC works through two steps, shown on the panel's **SHUTDOWN SEQ** chip:

| Step | Chip | What the PLC does | Moves on when |
|---|---|---|---|
| 10 | **HOLD HOT PHASE** | Switches the extraction and the ioniser (thermionic) off, inserts the Faraday cup and cools the caesium oven with air. If the filament is above 20 A and the caesium is hot, it keeps the target (cathode) at 3 kV or more and the filament at 15 A or less while the caesium cools. This keeps the caesium vapour ionised instead of coating the source's insulators. | the caesium is no longer hot (about 30 °C) |
| 11 | **FILAMENT RAMP DOWN** | Sets the target to zero and lowers the filament at 1 A per second. | the filament is at 0 A. The PLC then switches the target and filament off, closes the gate valve and lets the safety relay drop. |

The hold-hot step lasts as long as the caesium takes to cool. While the sequence runs, the PLC drives the extraction, target, filament, thermionic and caesium oven itself, so their controls on the **TESS Ion Source Control** panel are greyed out.

**ABORT SHUTDOWN** cancels the shutdown, but only during the hold-hot step (step 10). The supplies stay as the PLC left them. To bring them back up, follow [Bringing up a beam](beam-start-up.md).

> [!NOTE]
> As the PLC program stands:
>
> - cancelling during step 10 leaves the forced cooling on;
> - a completed shutdown leaves the gate valve barred from opening until **RESET FAULTS** is pressed.
>
> See [Known issues in the PLC program](../how-it-works/plc.md#known-issues-in-the-plc-program).

## By hand

1. **Cool the caesium.** On the **Cesium oven** row of the **TESS Ion Source Control** panel:
    - switch the heater off (click **HEATER ON** once);
    - switch forced cooling on (**FORCED COOLING**, which then reads **FORCED COOLING: ON**).
2. **Wait about 20 minutes**, until the caesium temperature has levelled off near room temperature. The target and extraction currents fall with it.
3. **Filament to zero.** Once the caesium temperature has levelled off, set the filament current to 0 A.
4. **Supplies off.** Switch off every power supply: einzel, extraction, target, thermionic and filament on the source panel, and the analyser magnet and steering on the **Beamline Optics** panel.
5. **Safety relay off.** Press **DISABLE SAFETY RELAY** on the **System Health** panel.

While the caesium is still hot, the HMI won't let you switch the target, the filament or the ioniser off (a **Command refused** message says why), and the PLC won't let the safety relay drop: the relay button is greyed out and reads **RELAY ENERGIZED (LOCKED)**. If you see either, the source isn't cool enough yet: wait.

## After the shutdown

1. Check that:
    - the **SHUTDOWN SEQ** chip reads **INACTIVE**;
    - the safety relay is off (**SAFETY: DE-ENERGIZED**, beacon green);
    - after **INITIATE SHUTDOWN**, the gate valve is shut (the PLC closes it).
2. **Keep the caesium cooling on for at least 30 minutes.** It can be left on indefinitely without causing a fault, but it is best switched off after 30 minutes: click **FORCED COOLING: ON** once.

    As the PLC program stands, while the safety relay is off the PLC keeps the cooling air on whatever you ask: the **Cesium oven** row still says **cooling air on**, and its tooltip says why. That does no harm. Stopping the air by itself once the caesium is cold would need a change to the PLC program.
3. Leave the services running. They keep logging the vacuum and the temperatures, and the alarms stay live.
4. Add a marker in the **Experiment Logging** panel saying how you left the machine, for the next person.

## When the PLC shuts down by itself

The PLC starts a shutdown of its own in two situations: when it loses the PC or the remote I/O, and when it detects a hardware fault. The **SHUTDOWN SEQ** chip shows the step, and the alarms say why.

| Step | Chip | What the PLC does |
|---|---|---|
| 1 | **GRACE PERIOD (75% EXTR)** | The PC has gone quiet (its watchdog unchanged for 3 seconds), or the remote I/O on the source deck is lost. The extraction is lowered to 75 % of its reading. After a loss of the PC, the target is also switched off and the filament set to 0 A, unless the filament is above 20 A and the caesium is hot; and the watchdog fault the PLC latches makes it close the gate valve, which drops the safety relay. If the PC or the remote I/O returns within 60 seconds, the sequence ends, but its fault stays latched until **RESET FAULTS**. |
| 2 | **CESIUM COOLDOWN** | Reached after 60 seconds, or straight away on a hardware fault: a sustained arc, filament over-current, the source body over 40 °C, source cooling water lost, or caesium over-temperature. The extraction is switched off, the gate valve is closed (which drops the safety relay) and the caesium is cooled with air. |
| 3 | **FINAL KILL** | When the caesium and the filament are cold, the target, filament and ioniser are switched off. |
| 99 | **LATCHED SAFE (RESET REQ)** | Finished. The source stays off until the cause is dealt with and **RESET FAULTS** is pressed. |

> [!WARNING]
> **RESET FAULTS** during step 2 ends the PLC's cool-down. Press it only once the caesium is cold.

After the source has been shut down by a fault, follow [Alarms and faults](alarms-and-faults.md) to find the cause before starting again.

## See also

- [Safety rules](../safety.md)
- [System Health](../panels/system-health.md)
- [The PLC program](../how-it-works/plc.md)
- [Alarms and faults](alarms-and-faults.md)
