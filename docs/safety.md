# Safety rules

Who may operate SINDIE, what you must know before you do, what protects people and the machine, and what to do in an emergency. Read this page before you operate SINDIE, and again whenever the software changes.

> [!IMPORTANT]
> These rules come on top of the Surrey Ion Beam Centre's local rules, risk assessments and training, never instead of them. Where this page and the local rules differ, the local rules apply.

## Who may operate SINDIE

**Authorised personnel:** Josh Bird and Dr Luke Antwis, Surrey Ion Beam Centre.

Before you operate SINDIE:

- **Radiation.** The laboratory is a radiation supervised area. You must have completed the FEPS Radiation Induction training, and you must wear a radiation monitoring badge.
- **Signals.** You must know what the beacon signals and the alarm sounds mean (see [Signals you must know](#signals-you-must-know)). This is covered in training.
- **Emergency switches.** You must know where both emergency switch-offs (ESOs) are (see [Emergency switch-off](#emergency-switch-off)).
- **Lone working** must be approved beforehand, in writing, by Dr Luke Antwis (lab manager). A lone worker must use a lone-working alarm the whole time.

**Personal protective equipment** is not needed to operate SINDIE. It is needed for servicing and for changing the target, which have their own safe operating procedure.

## The high-voltage area

- **The source deck is the high-voltage area.** It is marked with high-voltage signs.
- **The source deck door is interlocked.**
- **Switching the safety relay on needs the key.** For the key, contact Dr Luke Antwis.

High voltage can be present only while the safety relay is on. While it is on:

- the beacon is red;
- the **System Health** panel shows **SAFETY: LIVE** in amber.

## Signals you must know

| Signal | What it means |
|---|---|
| Beacon red | The safety relay is on: there may be high voltage on the source deck |
| Beacon amber | The PLC has latched a fault |
| Beacon green | The safety relay is off |
| Buzzer, 3 seconds | The safety relay has just switched on |
| HMI: fast two-tone (high, low), repeating | A critical alarm (priority 1) |
| HMI: three beeps, then a pause | A high-priority alarm (priority 2) |
| HMI: soft two-note chime, then a long pause | A medium-priority alarm (priority 3) |

The beacon and the buzzer are driven by the PLC. The alarm sounds come from the HMI (the computer's speakers) and repeat until the alarm is acknowledged or silenced. A low-priority alarm (priority 4) is listed without a sound. See [Alarms](panels/alarms.md).

## Emergency switch-off

There are two emergency switch-offs (ESOs):

- on the side of the source deck panel, at arm height;
- on the operator's desk box.

Learn where both are before you operate SINDIE. Test each one every week.

Both ESOs are wired directly into the safety relay's loop. No software or PLC is involved.

## Caesium, venting and the target

- **Handling caesium is forbidden** unless Dr Luke Antwis has fully trained you. Caesium work needs supervision, and its safe operating procedure must be followed.
- **Venting any part of SINDIE, or changing the target, is forbidden** unless Josh Bird or Dr Luke Antwis has trained you beforehand. Done wrongly, either can cause a caesium reaction.

Caesium reacts violently with water and burns in air.

## Emergency contacts

| Who | When | Contact |
|---|---|---|
| Dr Luke Antwis, lab manager | Emergencies, safety, the safety relay key, approval for lone working | +44 (0)1483 689145 |
| Campus Safety | Emergencies | +44 (0)1483 683333 |
| Josh Bird | Anything else about SINDIE and its software | j.m.bird@surrey.ac.uk |

## Local rules

The Surrey Ion Beam Centre's local rules for SINDIE will be added here.

## What protects you

Three layers protect people and the machine. Only the first two are safety systems.

| Layer | What it does | What it cannot do |
|---|---|---|
| The safety relay and its hardwired loop, with the emergency switch-offs, the door interlock and the key | Switches the high-voltage supplies off when the loop opens. | Be closed by software: the PLC can only allow it to close, and withdraw that permission. |
| The PLC | Applies its own limits and interlocks whatever the PC asks. It withdraws the safety relay without compressed air, without source or magnet cooling water, with the gate valve not open, or with the caesium oven over temperature. It holds the target (cathode) and filament while the caesium is hot, quenches arcs and runs shutdown sequences. | Control the supplies the PC drives directly: the Spellman einzel supplies, the magnet and steerer power supplies, and the SMU. |
| The software (the HMI and its services) | Shows the machine's state, sends your commands, keeps the alarms and logs everything. | Protect anyone. A readout can be stale or wrong; a command can be lost. |

> [!CAUTION]
> Never decide from what the HMI shows that something is safe to touch. Before any work on the source, the deck or the beamline, isolate it by the lab's procedure.

## Rules for operating with the software

- **Check which system you are looking at.** The simulator's HMI says **[SIMULATION - fake devices]** in its title bar. If you run the simulator on the lab PC, close it before working on the machine.
- **A dash is not a zero.** `---` means the value isn't known: its service has stopped reporting it, or never has. It says nothing about whether the supply is on.
- **Shut the source down before you stop the services.** The PLC watches a watchdog that the PC writes. After 3 seconds without it, the PLC closes the gate valve, which drops the safety relay, and starts its own shutdown. See [Starting the software](getting-started/starting-the-software.md#stopping).
- **Acknowledging an alarm doesn't clear its cause.** It only says you have seen it. A PLC fault stays latched until its cause is fixed and **RESET FAULTS** is pressed. See [Alarms and faults](procedures/alarms-and-faults.md).
- **A stopped recipe leaves everything as it is.** **STOP** ends a recipe without switching anything off. If a sequence is doing something you don't want, take control on the Automation bar, then set things as they should be.
- **Check every recipe before you run it**, and especially any recipe the recipe builder's AI assistant writes. A recipe can switch supplies on and set voltages. The one thing recipes are barred from is resetting faults.
- **Don't edit configuration files by hand on the lab PC.** Use **Config** in the HMI. It checks every value against the hard caps and the physical limits, keeps a backup and records the change. A hard cap (for example 22 kV on every Spellman supply) can only be raised by changing the code.
- **Never defeat an interlock** in the PLC, the software or the hardware, and never force a PLC value to get past one.

## Hazards and what the system does about them

### High voltage

The source deck and its supplies run at tens of kilovolts:

- extraction up to 20 kV;
- the target (cathode) up to 10 kV;
- the einzel lenses on their own supplies.

> [!CAUTION]
> While there is high voltage on the extraction or target supply, or the filament or the caesium is hot, you can't switch the safety relay off from the HMI. Its button reads **RELAY ENERGIZED (LOCKED)**, and the PLC would keep the relay on anyway, so that the source isn't dropped hot from the HMI; the PLC's own interlocks can still drop it. To remove the high voltage at once, use an ESO. For a controlled shutdown, see [Shutting down](procedures/beam-shut-down.md).

### Magnet power supplies

The analyser magnet and the X-steerer magnet have their own power supplies, driven by the PC.

- When the safety relay drops, their services switch the outputs off (0 A) and keep them off until you switch them on again.
- The X-steerer's power supply is not powered through the safety relay. When the relay is off, only its service holds it off, so a relay that is off doesn't mean the X steerer is dead.

### Caesium

- The PLC treats the caesium as hot above about 30 °C. It uses its own estimate of the temperature, which it keeps up even when the relay is off and the thermocouple can't be read.
- While the caesium is hot, the PLC keeps the target at 3 kV or more, and the HMI won't let you switch the target, the filament or the ioniser off, so that caesium vapour can't coat the source's ceramics. The PLC also won't close the gate valve, and won't let the safety relay be dropped from the HMI.
- Above 200 °C the PLC latches a caesium over-temperature fault. This withdraws the safety relay and starts cooling.
- While the relay is off, the PLC opens the oven's air-cooling valve, if there is compressed air.

> [!CAUTION]
> Never vent or open the source while the caesium is hot, and only if you have been trained to (see [Caesium, venting and the target](#caesium-venting-and-the-target)).

### Vacuum

- The source gate valve opens only while all of these hold:
    - the vacuum service reports the vacuum good enough on VG1 and VG2, by their controllers' relay set points (which can't be set above 1×10⁻⁵ mbar);
    - no fault is latched for the gauge controllers, VG1 or VG2;
    - no PC watchdog fault is latched.
- Before you start, the source pressure should be below 5×10⁻⁷ mbar.
- The PLC won't close the gate valve while there is high voltage or the source is hot: it overrides a **CLOSE** request. It closes the valve itself, hot or not, when any of the conditions above is lost, and that drops the safety relay.

### Cooling water and compressed air

- Losing source or magnet cooling water, or compressed air, withdraws the safety relay at once.
- If source cooling water is lost for 2 seconds, the PLC also latches a fault, switches off the extraction, target and filament, and starts a shutdown sequence.
- The gate valve and the Faraday cup are driven by compressed air.
- Before you start, the chiller and the coolant pumps are on, and the chiller's setpoint is below 15 °C (see [Bringing up a beam](procedures/beam-start-up.md)).

## In an emergency

1. **Remove the hazard.** Press the nearest ESO.
2. **Make yourself and others safe**, and get help: Dr Luke Antwis on +44 (0)1483 689145, or Campus Safety on +44 (0)1483 683333.
3. **Then, in the HMI,** find out what happened. Read the **Alarms** panel (the first PLC trip is marked first-out) and the PLC faults on the **System Health** panel. Don't reset anything until you understand the cause.

If the source is hot when the relay drops, the PLC cools the caesium with air. A safety relay that opens unexpectedly latches **SAFETY_RELAY_TRIPPED**. The machine then stays off until the cause is fixed, the faults are reset and the relay is switched on again. See [Alarms and faults](procedures/alarms-and-faults.md).

## See also

- [Bringing up a beam](procedures/beam-start-up.md)
- [Shutting down](procedures/beam-shut-down.md)
- [System Health](panels/system-health.md)
- [The PLC program](how-it-works/plc.md)
