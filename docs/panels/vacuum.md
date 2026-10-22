# Vacuum System

The **Vacuum System** panel shows the six vacuum gauges and the source turbo pump, and opens and closes the source gate valve. The gate valve matters beyond the vacuum: the safety relay can only be switched on while it is fully open, and closing it drops the relay.

![The Vacuum System panel: the six gauges, the source turbo pump at speed and the source gate valve, open](../images/panel-vacuum.png)

## Vacuum gauges

The **Vacuum gauges** group has a card for each gauge on the two GRAPHIX gauge controllers: **Source (VG1)**, **Beamline pre-magnet (VG2)**, **Beamline (VG3)**, **Endstation 1 (VG4)**, **Loadlock (VG5)** and **Endstation 2 (VG6)**. Each card shows the pressure in mbar and a status chip.

Each gauge has relays on its controller, which are on while the pressure is below their set point. Their on and off points are in `config/vac_gauges_config.json`. The vacuum service checks that the controllers hold them when it connects and corrects them if not; a relay that differs raises an alarm. VG1's and VG2's relays decide whether the gate valve may open, so their points are capped at 1×10⁻⁵ mbar (`src/core/hard_caps.py`). VG1 is the exception: it has a software trip that rides through short spikes, and its relays are only a high backstop (see [Riding through short spikes (VG1)](#riding-through-short-spikes-vg1)).

| Chip | Colour | Meaning |
|---|---|---|
| **HEALTHY** | green | The gauge reads normally and its relay is on: the pressure is below the set point. |
| **WARNING** | amber | The relay is still on, but the pressure has risen above its on point, by no more than 25 %. For VG1: the pressure is between its software trip's reset and trip points. |
| **SPIKE** | amber | VG1 only: the pressure went above its software trip point less than the ride-through (2.5 s) ago, and isn't back down to its reset point yet. If it comes back down in time, the spike is only logged. |
| **TRIPPED** | red | The relay is off: the pressure is above the set point. For VG1: the pressure went above its software trip point and wasn't back down to its reset point within the ride-through, or a backstop relay is open. |
| **ONLINE** | green | The gauge reads normally; its relay state hasn't been read yet. |
| **RAPID RISE** | red | The pressure is rising faster than `rapid_rise_thresh_mb_s` in `config/vac_gauges_config.json`. Judged only above 1×10⁻⁶ mbar, and never for VG4. |
| **SENSOR OFF** | grey | The controller reports the gauge switched off. |
| **NOT FOUND** | red | The controller finds no gauge on that channel. |
| **FAULT (n)** | red | The controller reports status code n. |
| **STALE** *time* | dashed outline | No live reading: the pressure shown, greyed, is the last one read, *time* ago. See [Readings that have stopped](#readings-that-have-stopped). |
| **NO READING** | dashed outline | The vacuum service is running, but hasn't had a reading from this gauge since the HMI started. The pressure shows `--- mbar`. |
| **OFFLINE** | dashed outline | No data from the vacuum service since the HMI started. The pressure shows `--- mbar`. |

### Riding through short spikes (VG1)

Powdered targets give VG1 short pressure bursts, up to about 1 mbar for a few readings (Josh Bird, October 2026). With VG1's relays at the gate valve's set points, each one opened the relay in the safety relay's hardwired loop and dropped the high voltage. VG1 is now judged in software instead, with a ride-through that was approved for this:

- **It works as VG1's relay did, with a delay.** An excursion starts when VG1 reads above the trip point (8×10⁻⁶ mbar), and ends when it reads at or below the reset point (5.5×10⁻⁶ mbar): the relay's own off and on points. Readings in between don't end it, so a pressure hovering about the trip point counts as one excursion. Coming down counts only when the next reading agrees: one stray low answer on the gauge bus doesn't end an excursion.
- **Short spikes are ridden through.** Back down to the reset point within the ride-through (2.5 s), and nothing is reported to the PLC. Meanwhile the card shows **SPIKE**; afterwards the event log says `VG1 spike ridden through: up to ... mbar for ... s`. The time given runs to the first reading back down.
- **A longer one trips.** Not back down within 2.5 s, and the vacuum service reports VG1 above its set point. It logs `VG1 above 8.0E-06 mbar for 2.5 s ...`, and the card shows **TRIPPED** (**RAPID RISE** if it rose quickly). The PLC latches **VG1_ABOVE_SP_WARN** (or **VG1_RAPID_RISE_WARN**) and takes away the gate valve's open permissive: the gate valve closes and the safety relay opens, as a relay trip did before.
- **It clears at the reset point.** Once VG1 is back at or below 5.5×10⁻⁶ mbar (two readings in a row) the vacuum service stops reporting it. The PLC's fault stays latched until **RESET FAULTS**; then open the gate valve again.
- **VG1's relays are a backstop.** Both of VG1's relays on gauge controller 1 (relays 1 and 4) are set at 5 mbar on and 10 mbar off, above the spikes, so they open only for a gross loss of vacuum. The vacuum service reads each relay's state about every 10 s. One read open counts as tripped at once if VG1 agrees (above 5 mbar, or no reading), else at the next read that says open too; it logs `VG1's backstop relay ... is open`.
- **Without readings.** The ride-through runs on if VG1 stops answering. With no relay below 10 mbar behind it, VG1 without a good reading for 3 s (the card shows **STALE**) loses the gate valve's permissive; VG2 still has 10 s. Answers that aren't a pressure (0, below 0) don't count as readings.
- **The timing.** The ride-through is counted from the first reading above the trip point. The service reads VG1 on every pass over the gauges (about every 0.4 s on the gauge bus), so the report reaches the PLC between 2.5 and about 3.5 s after the pressure went up. While the service checks a controller's relays (at start-up, or when a controller answers again), which takes several seconds without a pass, it keeps reading VG1 between the checks' reads and writes, and passes a trip on at once.

The values are in `config/vac_gauges_config.json`, under `system_interlocks.software_trips` (see [Configuration](../engineering/configuration.md#software-trips-with-a-ride-through)). The hard caps hold them: a trip point of at most 1×10⁻⁵ mbar and a ride-through of at most 2.5 s, as approved. Only while VG1 has a valid software trip may its relays be set as high as 20 mbar; without one they go back to the 1×10⁻⁵ mbar cap, and the config editor refuses a file that leaves them high. At start-up the vacuum service logs `Software trip in use: ...` (or, for an entry it can't use, `Software trip not used: ...`, as an error).

In the simulator, the fault `vacuum_spike_source` gives VG1 a spike to about 1 mbar for 0.8 s every 6 s.

The PLC service passes each gauge's not-found, type-mismatch, above-set-point and rapid-rise flags to the PLC, which latches them as faults (for example **VG1_ABOVE_SP_WARN**). They stay in the **PLC faults** list on the [System Health](system-health.md) panel until **RESET FAULTS**, even after the pressure has recovered.

### Readings that have stopped

A gauge with no live reading keeps the last pressure read, greyed, and its chip reads **STALE** with how long ago that was: **STALE 12 s**, **STALE 3 min**. Hover over the pressure or the chip for the reason and the time of day it was read. The reason is one of:

- the vacuum service isn't sending data: it has stopped, or is restarting;
- the gauge controllers aren't answering;
- the controller answers, but hasn't given a good reading from this gauge for 3 s or more.

If the controller reports why there's no reading, the chip shows that instead (**NOT FOUND**, **SENSOR OFF** or **FAULT (n)**), and the last pressure stays greyed. The pressure goes back to normal, and the chip to the gauge's state, with the first live reading.

> [!CAUTION]
> A **STALE** pressure is not the pressure now. Don't decide from it that the vacuum is good.

## Source turbo pump

The **Source turbo pump** group shows the pump's **Speed** in Hz, its motor **Current** in A, and a status chip.

| Chip | Colour | Meaning |
|---|---|---|
| **AT SPEED** | green | The pump reports normal operation, or turns at 990 Hz or more. |
| **ACCELERATING** | blue | The rotor turns, faster than a moment ago, but the pump isn't at speed yet. |
| **TURNING** | blue | The rotor turns below its speed, and hasn't sped up or slowed down by 1 Hz in the last 10 s. |
| **SLOWING DOWN** | blue | The rotor turns, slower than a moment ago: the pump has been stopped, or has tripped. |
| **IDLE** | grey | The pump is stopped. |
| **TRIPPED** | red | The pump reports an error and isn't in normal operation. |
| **STALE** *time* | dashed outline | No live reading: the turbo service has stopped or is restarting, its link to the pump is down, or the pump isn't answering. The speed and current shown, greyed, are the last ones read, *time* ago; hover for the reason. |
| **OFFLINE** | dashed outline | No data from the turbo service since the HMI started. Speed and current show dashes. |

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

An amber chip under it, **VACUUM PERMISSIVE HELD:** *n* **s LEFT**, shows that the vacuum service is restarting and the PLC is being given its last judgment of the vacuum, for *n* s more at most. See [When the vacuum service restarts](#when-the-vacuum-service-restarts).

## Controls

| Control | What it does |
|---|---|
| **START PUMP** | Starts the source turbo pump, with one click. The turbo service first clears any fault the pump holds, then starts it. The button reads **RUNNING** and is greyed out while the pump runs up or turns at speed. It is greyed out, with the reason in its tooltip, while the pump is slowing down and while its readings aren't live. |
| **RESET & START** | The same button after the pump has tripped: clears the fault and starts the pump. |
| **OPEN** | Asks the PLC to open the gate valve. Available only while the PLC's open permissive is on and the valve isn't already open. Otherwise its tooltip says "Gate valve is already open." or "the PLC doesn't allow the valve to open" with what the permissive needs (below). |
| **CLOSE** | Asks the PLC to close the gate valve. Available only while the PLC's close permissive is on and the valve isn't already closed. Otherwise its tooltip says "Gate valve is already closed." or "Source must be cold (HV, Filament, and Cesium OFF) to close manually." |

Both gate valve buttons read **OFFLINE** and are greyed out while the PLC link is down, and read **FAULT LOCKOUT** while a gate valve fault is latched (clear it with **RESET FAULTS** on the [System Health](system-health.md) panel). They act on one click, and no sequence can take them over: the gate valve can't be leased.

> [!IMPORTANT]
> Only trained staff stop the source turbo pump, by hand, at the pump. The software can't: the panel has no stop, and the turbo service refuses a stop command from anything (`the source turbo pump is never stopped from the software: trained staff stop it by hand, at the pump`). It doesn't send a fault reset or a start to a pump already running without a fault either, as the reset message carries a stop.

## How the PLC handles the gate valve

The PLC decides what the valve does; **OPEN** and **CLOSE** are requests to it.

- **Opening.** The PLC opens the valve only while its open permissive holds and compressed air is available. The open permissive needs:
    - the vacuum service to report the vacuum good for the gate valve: VG1 and VG2 both read within the last 10 s, neither shows **NOT FOUND**, **SENSOR OFF**, **FAULT (n)**, **RAPID RISE** or **TRIPPED**, and the relays of both have been read back holding the channel and set points in `config/vac_gauges_config.json` (VG1 showing **SPIKE** still counts as good, until its ride-through runs out);
    - none of these faults latched in the PLC: **PC_WATCHDOG_TIMEOUT**, **GRAPHIX_1_COMMS_FAIL**, **GRAPHIX_2_COMMS_FAIL**, or VG1's or VG2's **NOT_FOUND**, **TYPE_MISMATCH**, **RAPID_RISE_WARN** or **ABOVE_SP_WARN**.
- **Closing.** The close permissive needs the source cold and off: extraction and target (cathode) readbacks below 0.15 kV, the filament not hot and the caesium not hot. The PLC judges "hot" from its own models: a filament above 1 A, caesium above 30 °C.
- **The PLC overrides requests.** If the open permissive is lost, the PLC closes the valve, whatever the source is doing. While the close permissive is off and opening is permitted, it keeps the valve open even when asked to close it.
- **Shut by its interlock, it stays shut.** When the loss of the open permissive closes the valve, the open request is cleared (by the PLC, and by the PLC service if it is still set after 3 s, with the event "Gate valve closed by its interlock (vacuum permissive gone) ..."). Press **OPEN** again once the vacuum is good.
- **After a shutdown sequence** has closed the valve, the PLC doesn't let it open again until a fault reset (**RESET FAULTS**). Until then **OPEN** has no effect: the PLC clears the request.

> [!WARNING]
> The safety relay's permissive needs the gate valve open. Closing the valve with the relay on opens the relay and latches **SAFETY_RELAY_TRIPPED**: every supply on the relay loses power at once. Switch the relay off on the [System Health](system-health.md) panel first, unless dropping it is what you want.

## When the vacuum service restarts

The PLC has no vacuum gauges of its own: the vacuum permissive, the GRAPHIX comms flags and the gauge flags all come from the vacuum service, through the PLC service. A restart of the vacuum service, by hand on the [Service Manager](service-manager.md) panel or by the config editor after a change to `vac_gauges_config.json`, used to close the gate valve: the PLC service told the PLC the vacuum was unknown 2 s into the restart, and the PLC latched the GRAPHIX comms faults.

Now, while the vacuum service is silent or still starting, the PLC service keeps giving the PLC the vacuum service's last judgment: the permissive and the flags as they were. It does so for at most 60 s after that judgment. Meanwhile:

- the gate valve group shows **VACUUM PERMISSIVE HELD:** *n* **s LEFT** (amber);
- the gauges show **STALE**;
- the event log says `Vacuum service ... : the PLC keeps its last judgment of the vacuum ...`.

The restarted service has judged the vacuum when it gives the permissive itself, or 20 s after its start-up check if it still doesn't: VG1 or VG2 not read, or a relay not confirmed. VG1's software trip, or one of its backstop relays open, counts as a judgment at once, even during the start-up check. From then on the PLC follows it again. When the judgment is the same as before, nothing changes in the PLC at all, so there's no moment without the permissive. A judgment that the vacuum isn't good is passed on at once.

If the vacuum service hasn't judged the vacuum again 60 s after its last judgment, the PLC is told the vacuum is unknown, as before: an open gate valve closes and the safety relay drops. The event log says `No judgment of the vacuum from the vacuum service for 60 s ...`.

Only a vacuum service that is silent or still starting gets this hold, as after a restart; one that has stopped answering (hung) gets it too. A running vacuum service that loses its gauge controllers reports it at once, and the PLC acts on it at once.

> [!NOTE]
> During the hold the PLC acts on readings up to a minute old. The gauge controller's set-point relay is also wired into the safety relay's loop, so a gross loss of vacuum still drops the relay.

> [!WARNING]
> VG1's software trip runs in the vacuum service. From the moment the old service stops until the restarted one is reading VG1 again (it reads VG1 during its start-up check, and passes a trip on at once), VG1 is watched only by its backstop relays (10 mbar), and VG2's relays through the open gate valve. That is a few seconds for a restart; for a service that has crashed or hung, up to the hold's 60 s. Restart the vacuum service with the source off, or with the gate valve closed, where you can.

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
