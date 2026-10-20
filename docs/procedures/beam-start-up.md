# Bringing up a beam

This is the usual order for taking SINDIE from a cold, closed source to a beam:

1. checks at the machine;
2. the vacuum and the safety relay;
3. the high-voltage supplies;
4. the filament and the ioniser;
5. the caesium oven.

The values given are SINDIE's typical operating values. They are not limits.

> [!IMPORTANT]
> Operate SINDIE only if you are authorised and trained: see [Safety rules](../safety.md).

## At the machine

1. **Cooling.** The chiller unit and the coolant pumps are on, and the chiller's temperature setpoint is below 15 °C.
2. **Source body.** Look the source body over for coolant leaks and condensation, and fix or clean them as needed. Check the cabling by eye.
3. **Trolley.** The trolley is in the correct position.
4. **Target.**
    - The target material is the one you want.
    - It has lifetime left: look back through its previous use in the logs.
    - The target rod is inserted to the position you want.
5. **Doors.** Close all the doors: the source deck and the source trolley.

## In the HMI: faults, vacuum and the safety relay

6. **Faults.** Look for anything wrong, and deal with each problem and reset it before you go on (see [Alarms and faults](alarms-and-faults.md)):
    - the PLC faults on the **System Health** panel;
    - the **Alarms** panel;
    - any service on the **Service Manager** panel that isn't **ONLINE**.
7. **Vacuum.** On the **Vacuum System** panel, the source pressure (**Source (VG1)**) is below 5×10⁻⁷ mbar.
8. **Gate valve.** Open the source gate valve with **OPEN** on the **Vacuum System** panel. **OPEN** is available only while the PLC allows the valve to open: the vacuum is good and there are no gauge faults.
9. **Safety relay.**
    1. The safety relay needs its key (see [Safety rules](../safety.md#the-high-voltage-area)).
    2. Press **ENABLE SAFETY RELAY** on the **System Health** panel. The button counts down **PRESS PANEL START (5s)**.
    3. Press the START button on the panel before the count reaches zero.
10. **Beacon and buzzer.** Check that the beacon has turned red and that the buzzer has sounded. In the HMI, **SAFETY: LIVE** shows in amber and the button now reads **DISABLE SAFETY RELAY**.
11. **Readbacks at rest.** Check the readbacks on the **TESS Ion Source Control** panel with nothing yet switched on:
    - the source body and caesium oven temperatures are between 10 °C and room temperature;
    - every power supply reads zero.

> [!NOTE]
> If the relay doesn't come on, either the PLC hasn't given its permission or START wasn't pressed in time: see [The safety relay does not switch on](../troubleshooting.md#the-safety-relay-does-not-switch-on).

## High voltage

These supplies are all on the **TESS Ion Source Control** panel.

For each supply:

1. Switch it on: **ENABLE**, then the confirming second click.
2. Type the setpoint and press Enter.

The PLC sets a switched-off supply's request to zero, so a setpoint typed before **ENABLE** doesn't stay.

With no beam yet, the extraction and target supplies each draw only the current through their 50 MΩ bleed resistor: 0.02 mA per kV.

12. **Extraction.** Switch on and set the extraction voltage, typically 15 to 20 kV. Check that:
    - the readback voltage follows;
    - the current is the bleed current: 0.30 mA at 15 kV, 0.40 mA at 20 kV.
13. **Target (cathode).** Switch on and set the target voltage, typically 5 to 9 kV. Check that:
    - the readback voltage follows;
    - the current is the bleed current: 0.10 mA at 5 kV, 0.18 mA at 9 kV.
14. **Ioniser.** Switch on the **Thermionic** supply (the ioniser). It goes straight to about 2500 V. No current flows yet, because the filament is still cold.
15. **Beam energy.** The beam energy is the extraction voltage plus the target voltage. For example, 15 kV + 5 kV gives a 20 keV beam of singly charged ions. Check that the two add up to the energy you want. (**CALCULATE & SET AMPS** on the **Beamline Optics** panel uses the same sum.)
16. **Einzel lens.** Switch on and set the source einzel (**Source Einzel**), typically 8 to 16 kV. Check that the readback voltage follows and that the current is close to zero. You tune the exact voltage later, on the beam.

> [!WARNING]
> An einzel current above 50 µA means the einzel electrode insulators are failing. Switch the einzel off and report it.

## Filament and ioniser

Switch the filament on first (**ENABLE**, then the confirming click): the PLC zeroes the setpoint of a supply that is off.

17. **Degassing.** Do this if the filament is new, or the source has been at air:
    - set the filament to 15 A;
    - wait for it to degas. The source pressure spikes, then falls back to a low baseline. Watch it in the data viewer: the source gauge, VG1, on a log axis.
18. **Operating point.** Set the filament to its operating point, typically 28 to 30 A. The PLC raises the filament current at no more than 1 A per second, so 30 A takes about half a minute.
19. **Emission.** As the filament warms, over about 15 seconds, the **Thermionic** (ioniser) current rises. Its typical operating point is 100 to 150 mA.
20. **Auto-emission** (optional). To keep the ioniser power steady:
    - type the thermionic current you want on the **Thermionic** row;
    - press **AUTO EMISSION**.

    The PLC then adjusts the filament current itself to hold that emission current. While auto-emission is on, the filament's setpoint box is locked and shows the PLC's value.

## Caesium oven

21. **Cooling off.** Make sure the oven's forced cooling is off: the button on the **Cesium oven** row reads **FORCED COOLING**, not **FORCED COOLING: ON**, and the row's second line doesn't say **cooling air on**.
22. **Heat.**
    1. Switch the oven heater on: **HEATER ON**, then the confirming click.
    2. Type the oven setpoint, typically 100 to 150 °C.
23. **Wait.** Over the next 15 minutes or so, the caesium temperature rises and settles.
24. **Target and extraction currents.** As the caesium heats, the target and extraction currents rise above their bleed currents. Aim for a target current of about 1 mA. The right value depends on the target material.

Once the caesium is hot, the target, the filament and the ioniser stay on until it has cooled again: the HMI won't let you switch them off, and the PLC keeps the target at 3 kV or more. To switch them off, use a shutdown (see [Shutting down](beam-shut-down.md)).

## Beam

25. **Wait for the beam.** How long the beam current takes to rise depends on the target material and on whether it has been run before. Expect it within 10 to 60 minutes. To read it, the Faraday cup must be inserted and biased above 50 V (see [Selecting a mass and tuning the beam](tuning-the-beam.md)).
26. **Tune.** The source is now running. Carry on with [Selecting a mass and tuning the beam](tuning-the-beam.md).

> [!TIP]
> Set a new experiment phase and add markers as you go (for example "Filament degassed", "Caesium at 140 °C"). The next person, and you months later, will see on the data viewer's plots what was done when. See [Recording an experiment](recording-an-experiment.md).

## See also

- [Safety rules](../safety.md)
- [Shutting down](beam-shut-down.md)
- [TESS Ion Source Control](../panels/ion-source.md)
- [System Health](../panels/system-health.md)
- [Vacuum System](../panels/vacuum.md)
