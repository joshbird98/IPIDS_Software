# Alarm handling

How `service_alarms` turns the machine's telemetry into one list of alarms: where alarms come from, their priorities, the states an alarm goes through, acknowledging, shelving, first-out and suppression, the horn, and how the rules are set. The **Alarms** panel in the HMI shows the list and sends the operator's actions back.

## How it works

`service_alarms` listens to every device service, the service manager, the readback monitor and the sequencer. Twice a second it works out from the rules which alarms exist and whether each one's condition is true, keeps every alarm's state, and publishes the list on `ALARMS_DATA`, most urgent first. The HMI's **Alarms** panel displays that list, plays the horn, and sends acknowledgements and shelves back as commands.

Every change of state (active, cleared, acknowledged, shelved, unshelved) is written to `data/events.db` as an ALARM event.

An unknown value never changes an alarm: if a service's data goes stale, its alarms stay as they were, and the loss of the data raises an alarm of its own.

## Where alarms come from

These are the rules in `config/alarm_config.json` as it is in the repository.

| Alarm id | Active while | Priority | Delay |
|---|---|---|---|
| `plc.<Name>` | a PLC fault bit is set: one alarm per bit in `config/fault_config.json`, with the bit's description | 2 (fault severity 1) or 3 (severity 2); `Safety_Relay_Tripped` and `PC_Watchdog_Timeout` are 1 | the `_Comms_Fail` bits: 10 s |
| `comms.plc_service` | no data from the PLC service | 1 | 3 s |
| `comms.plc` | the PLC service has lost the PLC | 1 | 3 s |
| `comms.service_manager` | no data from the service manager | 2 | 5 s |
| `service.<name>` | a service the manager runs isn't ONLINE or STARTING | 2; a service stopped on purpose is 4 | 10 s |
| `comms.<device>` | a device's `stat_comms_fail` flag: gauges, controllers, supplies, pumps | 3 | 5 s |
| `vacuum.<gauge>.above_sp` | a gauge is above its relay set point | 2 | |
| `vacuum.<gauge>.approaching_sp` | a gauge is approaching its relay set point | 3 | 2 s |
| `vacuum.<gauge>.rapid_rise` | a gauge's pressure is rising rapidly | 2 | |
| `vacuum.relay_set_points` | the gauge controllers' relay set points aren't confirmed against `config/vac_gauges_config.json` (for VG1 and VG2 this also holds the gate valve shut) | 2 | |
| `turbo.source.load_warning` | the source turbo's load guard warns: its current above 1.2 A at full speed for 15 min without a break; or too hot (bearing above 65 °C, converter above 50 °C), or its own warning, for 1 min; see [The source turbo's load guard](turbo-guard.md) | 2 | |
| `turbo.source.load_critical` | the guard: still overloaded 10 min after the warning; the source will be shut down | 1 | |
| `turbo.source.load_trip` | the guard has tripped: the source is shut down; latched until **RESET FAULTS** once the current is back under 1.2 A | 1 | |
| `hv.<supply>.arc` | the Spellman service has latched an arc on a supply (output collapsed); latched until fault reset | 2 | |
| `sequencer.<slot>.failed` | the last recipe, mass scan or optimiser run failed | 3 | |

A delay is how long the condition must hold before the alarm goes active, so that an input that flickers doesn't raise it. The PLC service reports a service's data to the PLC as lost after 2 s without it, which is normal for a few seconds while the services start: that is why the PLC's comms-failure alarms wait 10 s. The two "no data" alarms aren't raised in the first 10 s after the alarm service starts.

## Priorities

| Priority | Name | Horn |
|---|---|---|
| 1 | critical | sounds |
| 2 | high | sounds |
| 3 | medium | sounds |
| 4 | low | listed only, never sounds |

Priorities 1 to 3 each have their own sound, loudest for critical. PLC faults take their priority from the severity in `config/fault_config.json`: severity 1 gives priority 2, severity 2 gives priority 3. The overrides in `config/alarm_config.json` then raise `plc.Safety_Relay_Tripped` and `plc.PC_Watchdog_Timeout` to priority 1. In the event log, an alarm going active is an ERROR event at priority 1 and a WARNING otherwise; its other changes are INFO.

## Alarm states

The alarm states follow ISA-18.2.

| State | Condition | Acknowledged | In the list |
|---|---|---|---|
| NORMAL | clear | (nothing to acknowledge) | no |
| UNACK | active | no | yes; it sounds (priorities 1 to 3) |
| ACKED | active | yes | yes |
| RTN_UNACK | cleared before anyone acknowledged it | no | yes, until acknowledged |

```
               condition active                    acknowledge
    NORMAL -----------------------> UNACK -----------------------> ACKED
     ^  ^                            |  ^                            |
     |  |           condition clears |  | condition active again     |
     |  |                            v  |                            |
     |  +------ acknowledge ------ RTN_UNACK                         |
     |                                                               |
     +---------------------- condition clears -----------------------+
```

An alarm stays in the list until it has been acknowledged and its condition has cleared. In the Alarms panel an unacknowledged active alarm flashes; one that cleared unacknowledged is shown steady in its full colour and in italics; an acknowledged one is shown in a softer colour.

## Acknowledging and resetting

Acknowledging tells the alarm service that someone has seen the alarm. It stops the horn for that alarm and nothing else. **Acknowledge selected** and **ACK ALL** in the Alarms panel send it; the event records who (the sending program) and when.

Acknowledging does not reset anything in the machine. A PLC fault stays latched in the PLC, and its alarm stays active, until **RESET FAULTS** in the **System Health** panel resets it and the bit clears. The same button clears the Spellman service's arc latches. Once the condition has cleared, an acknowledged alarm goes back to NORMAL and leaves the list. See [The PLC program](plc.md#resetting-faults) for when the PLC accepts a reset.

## Shelving

Shelving takes one alarm out of annunciation for a while, for example while a gauge is being replaced. Select the alarm and press **Shelve...**:

- it needs a reason, and a time of 1 to 480 minutes (`shelve_max_minutes` in `config/alarm_config.json` sets the service's limit; the HMI's dialog offers 1 to 480);
- a shelved alarm keeps following its condition, but doesn't sound or flash, and is listed greyed at the bottom with when its shelf ends and why it was shelved;
- when the time is up it is unshelved by itself; **Unshelve** puts it back sooner.

Every shelve and unshelve is an ALARM event with who, how long and the reason.

## First-out

The PLC's fault alarms form a first-out group. The first of them to go active after the first-out was last reset is marked first-out, with a star in the Alarms panel, so the cause of a cascade of trips stands out. No other PLC alarm is marked until **Reset first-out** in the Alarms panel resets the group, even after the first one has cleared. **RESET FAULTS** doesn't reset it.

## Suppression

An alarm can be suppressed by another active alarm that explains it. A suppressed alarm is listed greyed, with "(suppressed: explained by another alarm)", and doesn't sound. The rules in the repository suppress every gauge's comms alarm while a gauge controller's comms alarm (`comms.facilities.graphix1` or `graphix2`) is active, since a controller that isn't answering takes its gauges with it.

## The horn

The alarm service works out which priority should sound: the most urgent alarm that is unacknowledged, active, not shelved and not suppressed. The HMI loops that priority's sound (`src/gui/control/assets/sounds/`) until it is acknowledged. A cleared alarm waiting for acknowledgement doesn't sound, and priority 4 never does.

**SILENCE** stops the sound without acknowledging. A new alarm sounds again. Silencing is local to the HMI it is pressed in.

## Persistence

The alarm service saves its state to `data/alarm_state.json` at most every 2 s after a change, and when it stops: every alarm's state, acknowledgements, shelves and the first-out mark. When it starts again it restores them, so a restart of the alarm service or of the PC doesn't bring back alarms already acknowledged, and a shelf keeps its end time. The conditions themselves are worked out afresh from the live telemetry.

## Rules and overrides

`config/alarm_config.json` sets the rules. It holds:

- `rules`: the list of rules, which replaces the built-in list. Each has a `kind`: `plc_faults` (one alarm per PLC fault bit), `flag` (one alarm per telemetry tag matching a pattern, or for one tag), `stale` (a service has stopped publishing), `services` (a service isn't running), `sequencer` (a run failed).
- `overrides`: changes to single alarms, by id or by a pattern such as `plc.*_Comms_Fail`. Patterns apply first, then an exact id. An override can set `priority`, `message`, `delay_on`, `delay_off`, `group` and `suppressed_by`, and `"enabled": false` removes the alarm altogether.
- `first_out_groups` (`plc`) and `shelve_max_minutes` (480).

Common keys of a rule: `priority` (1 to 4), `delay_on` and `delay_off` (seconds), `group`, and `suppressed_by` (alarm id patterns). A `flag` rule can also have `message_key`: a text in the telemetry used as the message while the alarm is active (the source turbo's load guard's says what is happening and when the source will be shut down). Every key is described at the top of `src/alarms/rules.py`. The alarm service reads the file when it starts: changing it in the HMI's config editor restarts the alarm service. See [Configuration files and the config editor](../engineering/configuration.md).

> [!NOTE]
> Which alarms turn out to be nuisances, and which priorities suit the lab, is for the lab to decide. A shelf lasts at most 480 minutes: to change an alarm for good, change its rule or add an override in `config/alarm_config.json`.

## If the alarm service isn't running

Then nothing watches the alarms. The PLC's interlocks, and the list of latched PLC faults in the System Health panel, don't depend on it.

The HMI notices when `ALARMS_DATA` goes quiet (after about 2.5 s). It shows its own critical alarm, "The alarm service is not running - alarms are NOT being monitored", with **ALARM SERVICE NOT RUNNING** above the list, and sounds the critical horn. Restart the alarm service from the **Service Manager** panel; the service manager also restarts it by itself if it crashes or stops heartbeating for 3 s. Acknowledgements and shelves come back from `data/alarm_state.json`.

## See also

- [Alarms](../panels/alarms.md)
- [Alarms and faults](../procedures/alarms-and-faults.md)
- [The PLC program](plc.md)
- [Services and messaging](services.md)
- [Configuration files and the config editor](../engineering/configuration.md)
