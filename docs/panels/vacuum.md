# Vacuum System

The **Vacuum System** panel shows the six vacuum gauges and the source turbo pump, and opens and closes the source gate valve. The gate valve matters beyond the vacuum: the safety relay can only be switched on while it is fully open, and closing it drops the relay.

![The Vacuum System panel: the six gauges, the source turbo pump at speed and the source gate valve, open](../images/panel-vacuum.png)

## Vacuum gauges

The **Vacuum gauges** group has a card for each gauge on the two GRAPHIX gauge controllers: **Source (VG1)**, **Beamline pre-magnet (VG2)**, **Beamline (VG3)**, **Endstation 1 (VG4)**, **Loadlock (VG5)** and **Endstation 2 (VG6)**. Each card shows the pressure in mbar and a status chip.

Each gauge has relays on its controller, which are on while the pressure is below their set point. Their on and off points are in `config/vac_gauges_config.json`. The vacuum service checks that the controllers hold them when it connects and corrects them if not; a relay that differs raises an alarm. VG1's and VG2's relays decide whether the gate valve may open, so their points are capped at 1×10⁻⁵ mbar (`src/core/hard_caps.py`).

| Chip | Colour | Meaning |
|---|---|---|
| **HEALTHY** | green | The gauge reads normally and its relay is on: the pressure is below the set point. |
| **WARNING** | amber | The relay is still on, but the pressure has risen above its on point, by no more than 25 %. |
| **TRIPPED** | red | The relay is off: the pressure is above the set point. |
| **ONLINE** | green | The gauge reads normally; its relay state hasn't been read yet. |
| **RAPID RISE** | red | The pressure is rising faster than `rapid_rise_thresh_mb_s` in `config/vac_gauges_config.json`. Judged only above 1×10⁻⁶ mbar, and never for VG4. |
| **SENSOR OFF** | grey | The controller reports the gauge switched off. |
| **NOT FOUND** | red | The controller finds no gauge on that channel. |
| **FAULT (n)** | red | The controller reports status code n. |
| **OFFLINE** | dashed outline | No data from the vacuum service. The pressure shows `--- mbar`. |

The PLC service passes each gauge's not-found, type-mismatch, above-set-point and rapid-rise flags to the PLC, which latches them as faults (for example **VG1_ABOVE_SP_WARN**). They stay in the **PLC faults** list on the [System Health](system-health.md) panel until **RESET FAULTS**, even after the pressure has recovered.

## Source turbo pump

The **Source turbo pump** group shows the pump's **Speed** in Hz, its motor **Current** in A, and a status chip.

| Chip | Colour | Meaning |
|---|---|---|
| **AT SPEED** | green | The pump reports normal operation, or turns at 990 Hz or more. |
| **ACCELERATING** | blue | The rotor turns but the pump isn't at speed yet. |
| **IDLE** | grey | The pump is stopped. |
| **TRIPPED** | red | The pump reports an error and isn't in normal operation. |
| **OFFLINE** | dashed outline | No data from the turbo service. Speed and current show dashes. |

The pump's trip, error, warning and comms-failure flags also go to the PLC, which latches them as faults (**SRC_TURBO_TRIP** and the like) until **RESET FAULTS**.

## Source gate valve

The **Source gate valve** group shows the valve's position, from its open and closed limit switches as the PLC reads them:

| Chip | Colour | Meaning |
|---|---|---|
| **OPEN** | green | Fully open. |
| **CLOSED** | grey | Fully closed. |
| **TRAVELLING** | amber | Neither switch is made: the valve is moving, or stuck part-way. |
| **SENSOR CONFLICT** | red | Both switches are made. |
| **UNKNOWN** | dashed outline | No data from the PLC. |

A red chip under it, **FAULT: GATEVALVE_TIMEOUT** or **FAULT: GATEVALVE_CONFLICT**, shows a gate valve fault latched in the PLC: the valve didn't reach the position asked for within 5 s, or both limit switches were made at once.

## Controls

| Control | What it does |
|---|---|
| **START PUMP** | Starts the source turbo pump, with one click. The turbo service first clears any fault the pump holds, then starts it. The button reads **RUNNING** and is greyed out while the pump turns, and reads **-** while the turbo service is silent. |
| **RESET & START** | The same button after the pump has tripped: clears the fault and starts the pump. |
| **OPEN** | Asks the PLC to open the gate valve. Available only while the PLC's open permissive is on and the valve isn't already open. Otherwise its tooltip says "Gate valve is already open." or "Vacuum interlocks not met. Ensure turbo pump is at speed." (what the permissive actually needs is below). |
| **CLOSE** | Asks the PLC to close the gate valve. Available only while the PLC's close permissive is on and the valve isn't already closed. Otherwise its tooltip says "Gate valve is already closed." or "Source must be cold (HV, Filament, and Cesium OFF) to close manually." |

Both gate valve buttons read **OFFLINE** and are greyed out while the PLC link is down, and read **FAULT LOCKOUT** while a gate valve fault is latched (clear it with **RESET FAULTS** on the [System Health](system-health.md) panel). They act on one click, and no sequence can take them over: the gate valve can't be leased.

The panel has no control for stopping the turbo pump.

> [!NOTE]
> How the source turbo pump is stopped when it has to be (at its controller, or otherwise) is local practice, to be confirmed.

## How the PLC handles the gate valve

The PLC decides what the valve does; **OPEN** and **CLOSE** are requests to it.

- **Opening.** The PLC opens the valve only while its open permissive holds and compressed air is available. The open permissive needs:
    - the vacuum service to report the vacuum good for the gate valve: VG1 and VG2 both read within the last 10 s, neither shows **NOT FOUND**, **SENSOR OFF**, **FAULT (n)**, **RAPID RISE** or **TRIPPED**, and the relays of both have been read back holding the channel and set points in `config/vac_gauges_config.json`;
    - none of these faults latched in the PLC: **PC_WATCHDOG_TIMEOUT**, **GRAPHIX_1_COMMS_FAIL**, **GRAPHIX_2_COMMS_FAIL**, or VG1's or VG2's **NOT_FOUND**, **TYPE_MISMATCH**, **RAPID_RISE_WARN** or **ABOVE_SP_WARN**.
- **Closing.** The close permissive needs the source cold and off: extraction and target (cathode) readbacks below 0.15 kV, the filament not hot and the caesium not hot. The PLC judges "hot" from its own models: a filament above 1 A, caesium above 30 °C.
- **The PLC overrides requests.** If the open permissive is lost, the PLC closes the valve, whatever the source is doing. While the close permissive is off and opening is permitted, it keeps the valve open even when asked to close it.
- **Shut by its interlock, it stays shut.** When the loss of the open permissive closes the valve, the open request is cleared (by the PLC, and by the PLC service if it is still set after 3 s, with the event "Gate valve closed by its interlock (vacuum permissive gone) ..."). Press **OPEN** again once the vacuum is good.
- **After a shutdown sequence** has closed the valve, the PLC doesn't let it open again until a fault reset (**RESET FAULTS**). Until then **OPEN** has no effect: the PLC clears the request.

> [!WARNING]
> The safety relay's permissive needs the gate valve open. Closing the valve with the relay on opens the relay and latches **SAFETY_RELAY_TRIPPED**: every supply on the relay loses power at once. Switch the relay off on the [System Health](system-health.md) panel first, unless dropping it is what you want.

## Things to know

> [!TIP]
> If **OPEN** stays greyed out after pumping down, with VG1 and VG2 **HEALTHY**, look in the **PLC faults** list on the System Health panel for a VG1, VG2, GRAPHIX or watchdog fault latched while the pressure was high or a link was down, and press **RESET FAULTS**.

- Before a source start-up, the source pressure (**Source (VG1)**) should be below 5×10⁻⁷ mbar. Degassing a new filament, or one that has been at air, makes it spike, then fall back to a low baseline (see [Bringing up a beam](../procedures/beam-start-up.md)).
- The pressures come from the vacuum service and the turbo readings from the turbo service, not from the PLC; the gate valve's position and permissives come from the PLC.
- With the gate valve closed the safety relay can't be switched on: the **ENABLE SAFETY RELAY** button on the System Health panel stays greyed out until the valve reads **OPEN**.

## See also

- [System Health](system-health.md)
- [How the controls behave](../getting-started/controls-and-colours.md)
- [Bringing up a beam](../procedures/beam-start-up.md)
- [Shutting down](../procedures/beam-shut-down.md)
- [Alarms and faults](../procedures/alarms-and-faults.md)
- [The PLC program](../how-it-works/plc.md)
- [Configuration files and the config editor](../engineering/configuration.md)
