# Alarms and faults

What to do when something goes wrong: how alarms and PLC faults differ, the order to work in, and how to bring the machine back afterwards.

## Alarms and faults are different things

| | Alarm | PLC fault |
|---|---|---|
| What it is | A message from the alarm service saying something needs your attention: a PLC fault, a lost connection, a service that has stopped, a pressure above its set point, an arc, a sequence that failed | A condition the PLC has latched, and acted on, in its own memory: for example a lost coolant flow or a gate valve that didn't move |
| Where you see it | The **Alarms** panel, with the horn for priorities 1 to 3 | The **PLC faults** list on the **System Health** panel (and as an alarm) |
| What clears it | Acknowledging it says you have seen it; it leaves the list once acknowledged and its condition has gone | Only **RESET FAULTS**, once the condition has gone and the PLC accepts the reset |

Acknowledging an alarm never resets a PLC fault, and resetting the faults never acknowledges an alarm.

## When an alarm sounds

1. **Read it.** The most urgent alarms are at the top of the **Alarms** panel, coloured by priority: **CRITICAL** (red), **HIGH** (orange), **MEDIUM** (yellow), **LOW** (blue). **SILENCE** stops the horn without acknowledging anything; a new alarm sounds again.
2. **Find the cause.**
    - If the PLC tripped, the alarm marked with a star (first-out) is the first PLC fault of the trip; the others usually followed from it.
    - The **System Health** panel lists the latched PLC faults and shows any shutdown step the PLC is running (see [Shutting down](beam-shut-down.md#when-the-plc-shuts-down-by-itself)).
    - The **Service Manager** panel shows a service that has stopped or keeps restarting; the **Command Log** shows commands that were refused and why.
    - [Messages and what they mean](../messages.md) explains each alarm, PLC fault and refusal.
3. **Make it safe**, then put the cause right at the machine. Don't reset anything you don't understand.
4. **Acknowledge** the alarms: select them and press **Acknowledge selected**, or **ACK ALL**.
5. **Reset the faults**: **RESET FAULTS** on the **System Health** panel. This clears the PLC's latched faults and the Spellman service's arc latches. A fault whose condition is still there latches again at once.
6. **Reset the first-out marker** (**Reset first-out** on the **Alarms** panel) once you know what started it, so the next trip is marked afresh.
7. **Bring the machine back** from where it stopped, following [Bringing up a beam](beam-start-up.md).

> [!WARNING]
> **RESET FAULTS** while the PLC is cooling the source after a fault (**SHUTDOWN SEQ: CESIUM COOLDOWN**) ends the cool-down. Reset only once the caesium is cold.

## When the PLC refuses a reset

The PLC accepts **RESET FAULTS** only when no hold is running and the PC connection is good. It refuses it:

- during **GRACE PERIOD (75% EXTR)**, while it waits to see whether the PC or the remote I/O comes back;
- during the operator shutdown (**HOLD HOT PHASE**, **FILAMENT RAMP DOWN**).

Wait for the step to finish, then reset.

## After the safety relay trips

A safety relay that opens when nobody asked for it latches **SAFETY_RELAY_TRIPPED**: the machine state goes to fault, the readbacks of the source supplies go to zero, and the supplies the PC drives see the relay off and switch their outputs off.

1. Find out why it opened: an emergency stop, a door, loss of compressed air or cooling water, the gate valve leaving its open position, a caesium over-temperature (the first-out alarm and the PLC faults list say which the PLC saw).
2. Put the cause right, then press **RESET FAULTS**.
3. With the gate valve open, switch the relay on again (the key at **ENABLE**, **ENABLE SAFETY RELAY**, then **START** on the **High Voltage Systems Control** box within 5 seconds), and bring the supplies back up in order.

> [!NOTE]
> The PLC cools the caesium with air while the relay is off, but a relay trip doesn't start its shutdown sequence. If the source was hot, keep an eye on the caesium and filament as they cool.

## Shelving an alarm

If an alarm keeps coming back for a known reason that can't be fixed now (a gauge being replaced, say), **Shelve...** hides it for a set time, from 1 minute to 8 hours, with a reason that is logged. It comes back by itself when the time is up, or with **Unshelve**. Shelve only what you understand, and tell the next person.

## See also

- [Alarms](../panels/alarms.md)
- [System Health](../panels/system-health.md)
- [Messages and what they mean](../messages.md)
- [Alarm handling](../how-it-works/alarms.md)
