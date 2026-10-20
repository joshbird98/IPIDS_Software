# Services and messaging

The Python services behind the HMI: what each one does, how the service manager keeps them running, and how they talk to each other with telemetry, commands, control leases and permits. It ends with the ports and the instruments' links.

## The services

Each service is a separate Python process in `src/services/`. The service manager starts thirteen of them.

| Service | What it does | Publishes | Cycle |
|---|---|---|---|
| `service_plc` | Reads the PLC's data block DB10 and publishes it; writes commands, the PC watchdog and the other services' status flags to the PLC; writes a FAULT event for each PLC fault bit that changes. | `PLC_DATA` | 100 ms |
| `service_vac_gauge_controllers` | Reads the six gauges on the two GRAPHIX controllers; checks the gauge names and relay set points against `config/vac_gauges_config.json` and corrects them; works out the gate-valve vacuum permissive. | `VACUUM_DATA` | 50 ms |
| `service_source_turbo` | Starts and stops the source turbo pump and reads its speed, temperatures, warnings and errors. On connect it checks the pump's vent strategy and, if the pump is stopped, corrects it and saves it to the pump's flash memory (about 30 s). | `SRC_TURBO_DATA` | 50 ms |
| `service_spellman_mpd` | Drives the Spellman MPD supplies (the source einzel is the one configured): voltage, current limit, output. Detects arcs in software and follows the safety relay. | `SPELLMAN_DATA` | 100 ms |
| `service_magnet_psu` | Drives the analyser magnet supply: current setpoint (the voltage limit follows from it), output, degauss. Checks the coil resistance and follows the safety relay. | `MAGNET_DATA` | 50 ms |
| `service_steerer_magnet_psu` | The same program for the X-steerer supply. The steerer isn't on the safety relay circuit, so this service's relay watch is its only interlock. | `STEEER_MAGNET_DATA` | 50 ms |
| `service_smu` | The Keysight SMU on the Faraday cup: bias voltage, and the beam current with software auto-ranging. Follows the safety relay. | `SMU_DATA` | 100 ms, longer at high NPLC |
| `service_logger` | Records the device telemetry, 20 rows a second, to Parquet files. | none | 50 ms |
| `service_data_compactor` | At 02:00 every night: day files, rollups and the OneDrive backup. | none | |
| `service_events` | Writes every event to `data/events.db`. | none | |
| `service_monitor` | Compares readbacks with their setpoints (`config/readback_tolerance.json`). Information only. | `MONITOR_DATA` | 0.5 s |
| `service_alarms` | The machine's alarm list. | `ALARMS_DATA` | 0.5 s |
| `service_sequencer` | Runs recipes, mass scans and the optimiser, under control leases. | `SEQUENCER_DATA` | 20 ms; publishes every 0.1 s while running, 0.5 s idle |

The service manager itself (`service_manager.py`) publishes `MANAGER` five times a second. There is also a service for the ADAM-6017 current monitor (`service_current_monitor.py`), but the manager doesn't start it: the module is broken.

The topic of the X-steerer is spelt `STEEER_MAGNET_DATA`, with three Es, in `src/core/network_map.py`. Use it as it is when you subscribe to it.

## The service manager

`python src/services/service_manager.py` starts the services and keeps them running. The HMI's **Service Manager** panel shows what it reports and sends it commands.

### Start order

The manager starts the services in four tiers, 2 s apart, so that each finds what it needs already running:

| Tier | Services |
|---|---|
| 0 | `service_events`, `service_logger`, `service_data_compactor` |
| 1 | `service_plc`, `service_monitor`, `service_alarms` |
| 2 | the gauge, turbo, magnet, X-steerer, Spellman and SMU services |
| 3 | `service_sequencer` |

Only one manager can run per set of ports: a second one stops at once with a CRITICAL event. When the manager is stopped (Ctrl+C, Ctrl+Break or a terminate signal), it stops every service. When it starts, it kills service processes left over from an earlier manager of the same installation (same port offset, same real or simulated mode), not those of another, so the simulator or the tests can't stop the real system's services.

### Heartbeats and restarts

Every service sends a heartbeat to the manager every 0.5 s (the compactor every 5 s). The manager restarts a service that exits, that sends no heartbeat within its start-up grace (15 s), or whose last heartbeat is older than its timeout:

| Service | Heartbeat timeout |
|---|---|
| most services | 2.0 s |
| `service_spellman_mpd` | 2.5 s |
| magnet, X-steerer, monitor, alarms and sequencer services | 3.0 s |
| `service_smu` | 10 s |
| `service_data_compactor` | 215 s, with 215 s to start |

A restart waits a little longer each time: 2, 5, 10, 30, 60, 120 and then 300 s for every further attempt. A service that then runs for 120 s starts again from the shortest wait. A service whose device is unplugged doesn't need restarting: it carries on heartbeating and reports the device as not connected.

### States

| State | Meaning |
|---|---|
| STARTING | launched, no heartbeat yet |
| ONLINE | heartbeating |
| HANGING | heartbeats stopped for longer than the timeout; it is about to be killed and restarted |
| CRASHED | the process has exited; it is about to be restarted |
| OFFLINE | not running |
| BACKOFF | waiting before the next restart attempt; the panel shows how long and which attempt |
| STOPPED | stopped on purpose from the HMI; not restarted until it is started again |

The manager takes three commands: **Stop** (kill it and keep it stopped), **Start**, and **Restart** (kill and start at once, clearing the back-off). If a service won't stay up, its log in `SystemLogs/` says why: the manager's event gives the exit code.

## Device services

The seven hardware services (PLC, gauges, turbo, Spellman, magnet, X-steerer, SMU) share one base class, `DeviceService` in `src/core/device_service.py`. Each runs one loop, once per cycle:

1. connect, if not connected, with a growing wait between attempts;
2. read the safety relay state from the PLC telemetry (the services that follow the relay);
3. the service's own per-cycle work (forcing outputs off, a degauss step, ...);
4. answer every command;
5. read the device, or publish its values as unknown if it isn't connected;
6. publish, per subsystem, who holds its lease and whether an operator command would be accepted;
7. publish the telemetry and send a heartbeat.

A failed connection is logged once, then at most once a minute. A command that raises an error fails with the reason; it doesn't stop the service.

### When a device stops answering

| Service | What happens | Reconnects after |
|---|---|---|
| `service_plc` | Every PLC value is published as unknown and `system.connected` as 0. The connection is made in a helper thread, so the service keeps heartbeating. The PLC itself notices the missing watchdog after 3 s. | 1, 2, 5, 10 s, then every 10 s |
| `service_vac_gauge_controllers` | A gauge with no good reading for 3 s is shown as unknown. VG1 or VG2 with no good reading for 10 s withdraws the gate-valve vacuum permissive. With the link down, every gauge is unknown and both controllers' comms flags are set. | 1, 2, 5 s |
| `service_source_turbo` | `stat_comms_fail` set, readbacks unknown. | 1, 2, 5, 10 s |
| `service_spellman_mpd` | A supply that doesn't answer its status query within 1 s gets `stat_comms_fail` and unknown readbacks. Each bus reconnects on its own. | 1, 2, 5, 10 s |
| `service_magnet_psu`, `service_steerer_magnet_psu` | `stat_comms_fail` set, readbacks unknown. A supply that hasn't reported its nominal ratings takes only 0 A and output off. | 0.5, 1, 2, 5 s |
| `service_smu` | Disconnects after 3 query timeouts; `stat_comms_fail`, readbacks unknown. After reconnecting, the bias is put back only if fresh PLC data confirms the relay is on. | 1, 2, 5, 10 s |

### Spellman arc detection

During an arc a Spellman supply runs into its current limit and its output collapses. The Spellman service counts as an arc an output below 10 % of its setpoint, or a current above 500 µA, seen on 2 polls in a row, so one garbled reading can't trip anything. Nothing is judged below a 0.5 kV setpoint. After a switch-on, nothing is judged until the output is up (half its setpoint) or 3 s have passed; while the output moves after a setpoint change, it is judged against the lower of the old and new setpoints.

An arc is latched until **RESET FAULTS**, with a WARNING event. It is published as the supply's `stat_arc_exceeded`, which the PLC latches as a Spellman fault (`Unit1_Arc_Exceeded` for the source einzel) and the alarm service raises as `hv.<supply>.arc`. The settings are under `arc_detection` in `config/mpd_config.json`, read when the service starts.

### Spellman current limit

A Spellman supply holds a current limit of 0 after every power-up, and with none its output can't rise. Before the first non-zero voltage or switch-on after a power-up, unless a limit has been set since, the service reads the supply's limit and, if it holds none, sets `default_current_ua` from `config/mpd_config.json` (50 µA), saying so in its reply. If the supply won't take it, the voltage or switch-on fails with the reason.

## Telemetry

Each service binds its own ZeroMQ PUB socket and publishes, every cycle, one message: the topic, then a JSON object.

- The object is flat: tag name to value. On/off values are 1.0 or 0.0.
- `null` means unknown: the device isn't answering, the supply has no power, the PLC isn't connected. The HMI shows dashes and the logger writes a gap; nothing carries on with an old value.
- Every message has `timestamp` (the sender's clock, Unix seconds); the device services add `system.cycle_time_ms`.
- Device services publish, for each subsystem they control, `<subsystem>.permit`, `<subsystem>.permit_reason`, and for a subsystem that can be leased `<subsystem>.lease_holder` and `<subsystem>.lease_purpose`. Services that follow the relay also publish `system.safe_to_run`.
- A device service's socket keeps at most 5 messages waiting for a slow listener; the listener misses messages rather than holding up the service.

| Listener | Listens to |
|---|---|
| HMI | all twelve publishers: the eight device topics, `MANAGER`, `MONITOR_DATA`, `SEQUENCER_DATA`, `ALARMS_DATA` |
| `service_logger`, the data viewer, `service_monitor`, `service_sequencer` | the eight device topics: `PLC_DATA`, `VACUUM_DATA`, `SRC_TURBO_DATA`, `SPELLMAN_DATA`, `MAGNET_DATA`, `STEEER_MAGNET_DATA`, `SMU_DATA`, `ADAM_DATA` |
| `service_alarms` | the eight device topics, `MANAGER`, `MONITOR_DATA`, `SEQUENCER_DATA` |
| `service_plc` | `VACUUM_DATA`, `SPELLMAN_DATA`, `MAGNET_DATA`, `SRC_TURBO_DATA` |
| Spellman, magnet, X-steerer and SMU services | `PLC_DATA` |

The HMI, the alarm service, the monitor and the sequencer judge each topic's freshness for themselves: a topic silent for longer than 2 s, or five times its usual interval if that is longer, is stale, and every value from it counts as unknown. The logger has its own rule (1 s): see [Data logging and storage](data-logging.md).

Events go the other way: every program publishes them to port 5567, where `service_events` listens and writes them to `data/events.db`. Heartbeats go to the manager on port 5566.

## Commands

Commands are acknowledged: every request gets exactly one reply. Each service binds a ZeroMQ ROUTER socket on its command port, and senders (the HMI, the sequencer, scripts) connect DEALER sockets.

| Message | Fields |
|---|---|
| Request | `id`, `tag`, `value`, `ts` (when it was sent, Unix seconds), `origin` (`hmi`, a recipe, a script...), and `holder` if it is sent under a control lease |
| Reply | `id`, `status`, `reason`, `tag`, `value`, `service`, `ts`, and sometimes `data` (for example a scan's points) |

| Status | Meaning | Given by |
|---|---|---|
| ok | Executed: written to the PLC or the device. | the service |
| accepted | Received and passed every check, with no separate result. A command the service didn't answer is answered this way at its next poll. | the service |
| rejected | Refused before touching the hardware, with the reason: out of limits, past a hard cap, locked out, interlocked, held by a sequence, too old, unknown tag. | the service |
| failed | The service tried, but the write failed or the device didn't answer. | the service |
| timeout | No reply in time (3 s by default): the service is busy, hung or restarted. | the sender |
| unreachable | The service isn't running or not connected; nothing was sent. | the sender |

**Too old.** A command whose `ts` is older than the service's limit when the service gets to it is rejected as stale, so clicks made while a service (or the HMI) was frozen aren't all carried out at once when it recovers.

| Service | Age limit |
|---|---|
| device services | 0.5 s |
| `service_smu` | 0.5 s + 0.1 s × NPLC |
| `service_sequencer` | 1.0 s |
| the service manager, `service_alarms` | 2.0 s |

**Routing.** A sender finds the service for a tag from the tag registry (each tag's `source`), and falls back on the tag's name.

**The audit trail.** Every reply is written to `data/events.db` as a COMMAND event, with the tag, the value, the origin, the lease holder, the status, the reason and the latency, and to the service's log file (not its console). In the HMI every outcome appears in the **Command Log** panel, and a refusal, failure, timeout or unreachable service also pops up, at most once every 5 s for the same command and outcome.

## Control leases

A sequence (a recipe, a mass scan or the optimiser, run by `service_sequencer`) holds a control lease on each subsystem it drives, for example `ion_beam.beamline.magnet`. While it holds the lease, the service that owns the subsystem takes commands for it only from that sequence; anyone else's are rejected with "... is controlled by ... - stop it, or take control, first".

- **Claim.** The sequence claims a subsystem immediately before its first command to it.
- **Renew.** It renews every 1 s; a lease lasts 5 s unless renewed. So a sequencer that dies gives control back within 5 s. A service that restarts loses its leases, and the next renewal claims them again.
- **Release.** The sequence releases its leases when it ends.
- **Take control.** The operator can always take a subsystem back: **TAKE CONTROL** in the Automation bar (clicked twice). The lease is cleared at once, with a WARNING event, and the sequence's next command or renewal is refused with "control was taken by the operator". A recipe then pauses (**Resume** claims again); a mass scan or the optimiser stops.

Lease commands are ordinary commands to the owning service, on the tag `<subsystem>.cmd_lease`, with the operation (claim, renew, release, take), the holder, the purpose and the lease time.

Some controls can never be leased, so the operator always has them: the PLC's `ion_beam.system` controls (safety relay, shutdown, fault reset) and `ion_beam.source.chamber` (the gate valve).

The software services refuse the old `cmd_ctrl_mode` commands: leases replaced them. The PLC's control modes are separate and still real: see [The PLC program](plc.md#control-modes).

## Permits

Each device service publishes, per subsystem, whether an operator command would be accepted now (`<subsystem>.permit`, 1 or 0) and if not, why (`<subsystem>.permit_reason`). The HMI greys out the controls and shows the reason as their tooltip, so the panels don't work out lock-outs for themselves.

| Service | Not permitted when |
|---|---|
| any device service | the device isn't connected; the subsystem is held by a sequence |
| services that follow the relay | the safety relay isn't confirmed energised (or there is no PLC data) |
| `service_plc` | the PLC isn't connected; the PLC reports the PC watchdog lost; for relay-powered supplies (source supplies, caesium oven, steering) the relay is off; for the source deck the remote I/O is lost; for the source supplies an automatic shutdown is running; the subsystem is in a PLC control mode above 0 (except the caesium oven's mode 1); the PLC's lockout is set. The system controls and the gate valve are permitted whenever the PLC is connected and the watchdog is good. |
| `service_spellman_mpd` | the relay is off; the supply isn't in `mpd_config.json`; its bus isn't connected; the supply isn't answering |
| `service_source_turbo` | the controller isn't connected or the pump isn't answering |

A permit is a hint for the HMI. The service still checks every command itself when it arrives.

## Relay watch

The Spellman, magnet, X-steerer and SMU services follow the safety relay from the PLC's telemetry (`src/core/relay_watch.py`). The relay counts as confirmed on only if `ion_beam.facilities.safety_relay_active` is 1, the PLC data is younger than 1.0 s (2.0 s for the Spellman service), and there has been no off reading and no gap in the PLC data since the last check. Null (PLC not connected) counts as off.

| Service | With the relay off or unknown |
|---|---|
| `service_spellman_mpd` | Closes its bus connections; the supplies, powered through the relay, report off with unknown readbacks. Forgets the setpoints and current limits it had set. Refuses commands, except switching an output off. |
| `service_magnet_psu`, `service_steerer_magnet_psu` | Forces 0 A, 0 V and output off at once, then every second while it lasts, and at once if the supply reports its output on. The output doesn't come back on by itself. Refuses commands, except switching the output off. |
| `service_smu` | Forces the bias to 0 V and off, and forgets the bias setpoint. Refuses a bias voltage or switching the bias on. After a reconnect the old bias is put back only if fresh PLC data confirms the relay is on. |

## Status flags for the PLC

`service_plc` copies the other services' status flags into the PLC's request area, where the PLC latches them as faults. It writes a batch only when a flag changes.

| From | Flags |
|---|---|
| `VACUUM_DATA` | both GRAPHIX controllers' comms failure; the gate-valve vacuum permissive (`stat_vac_ok_for_gv`); for each of VG1 to VG6: not found, type mismatch, above set point, rapid rise |
| `SPELLMAN_DATA` | for the source einzel, beamline einzel and both neutral traps: comms failure, overcurrent, overvoltage, fail, arc |
| `MAGNET_DATA` | analyser magnet: comms failure, short circuit, open circuit, unexpected resistance, PSU over-temperature, power fail, overvoltage, overcurrent |
| `SRC_TURBO_DATA` | source turbo: comms failure, error, warning, trip |

A comms-failure flag that a service publishes as unknown is sent to the PLC as a failure. If one of these four services publishes nothing for 2 s, `service_plc` sends its comms flags as failed, and for the vacuum service also withdraws the gate-valve permissive. The X-steerer's and the SMU's flags are not sent to the PLC.

## Ports

Every program connects on the PC's loopback address (`tcp://127.0.0.1`). The ports are in `config/network_config.json`; `SINDIE_PORT_OFFSET` shifts all of them, which is how the simulator (offset 10000) and the tests (offset 20000) run beside the real system.

| Name in the file | Port | Use |
|---|---|---|
| `plc_pub`, `plc_cmd` | 5550, 5551 | `PLC_DATA`; PLC commands |
| `serial_pub`, `serial_cmd` | 5552, 5553 | reserved, not used |
| `logger_cmd` | 5554 | the logger's flush requests (from the data viewer and exports) |
| `vacuum_pub`, `vacuum_cmd` | 5556, 5557 | `VACUUM_DATA`; gauge relay set point commands |
| `src_turbo_pub`, `src_turbo_cmd` | 5558, 5559 | `SRC_TURBO_DATA`; turbo commands |
| `ldlk_turbo_pub`, `ldlk_turbo_cmd` | 5560, 5561 | reserved: there is no loadlock turbo service |
| `spellman_pub`, `spellman_cmd` | 5562, 5563 | `SPELLMAN_DATA`; Spellman commands |
| `magnet_pub`, `magnet_cmd` | 5564, 5565 | `MAGNET_DATA`; magnet commands |
| `heartbeat_pub` | 5566 | heartbeats, received by the service manager |
| `events_pub` | 5567 | events, received by `service_events` |
| `manager_pub`, `manager_cmd` | 5568, 5569 | `MANAGER` (service states); stop, start, restart |
| `smu_pub`, `smu_cmd` | 5570, 5571 | `SMU_DATA`; SMU commands |
| `adam_pub`, `adam_cmd` | 5572, 5573 | `ADAM_DATA`; the ADAM service is not started |
| `steerer_magnet_pub`, `steerer_magnet_cmd` | 5574, 5575 | `STEEER_MAGNET_DATA`; X-steerer commands |
| `monitor_pub` | 5576 | `MONITOR_DATA` |
| `sequencer_pub`, `sequencer_cmd` | 5577, 5578 | `SEQUENCER_DATA`; recipe, mass scan and optimiser commands |
| `alarms_pub`, `alarms_cmd` | 5579, 5580 | `ALARMS_DATA`; acknowledge, shelve, unshelve, reset first-out |

The service manager also holds TCP port 65432 on localhost (less the port offset) as its single-instance lock. A change to `network_config.json` needs the service manager and the HMI restarted by hand: see [Configuration files and the config editor](../engineering/configuration.md).

## The instruments and their links

| Instrument | Link | Address | Service |
|---|---|---|---|
| Siemens S7-1200 PLC | S7 protocol (python-snap7 client); DB10 | 192.168.1.10, rack 0, slot 1 | `service_plc` |
| Leybold GRAPHIX gauge controllers, nodes 10 and 20 (VG1 to VG6) | GRAPHIX protocol on RS485, through a Waveshare TCP bridge | 192.168.1.200:4196 | `service_vac_gauge_controllers` |
| Leybold TURBOVAC source turbo pump | Siemens USS on RS485, through a Waveshare bridge | 192.168.1.201:4196 | `service_source_turbo` |
| Loadlock turbo pump | Waveshare bridge configured | 192.168.1.202:4196 | none |
| Spellman MPD supplies, source bus (source einzel at address 01) | Spellman MPD protocol on RS485, through a Waveshare bridge; the PC talks from 192.168.1.100 | 192.168.1.203:4196 | `service_spellman_mpd` |
| Spellman MPD supplies, beamline bus | as above; in `network_config.json` but with no bus in `mpd_config.json` | 192.168.1.204:4196 | none |
| Analyser magnet supply | Modbus RTU frames over TCP (pymodbus) | 192.168.1.11:502 | `service_magnet_psu` |
| X-steerer magnet supply (same model) | Modbus RTU frames over TCP | 192.168.1.14:5025 | `service_steerer_magnet_psu` |
| Keysight B2900-series SMU | SCPI over TCP | 192.168.1.12:5025 | `service_smu` |
| ADAM-6017 current monitor | Modbus TCP and UDP | 192.168.1.13:502 | not started |

The addresses are in `config/network_config.json`. The serial settings of the Waveshare bridges aren't recorded in the software.

## See also

- [System overview](overview.md)
- [Service Manager](../panels/service-manager.md)
- [Command Log](../panels/command-log.md)
- [The Automation bar](../panels/automation-bar.md)
- [The PLC program](plc.md)
- [Configuration files and the config editor](../engineering/configuration.md)
