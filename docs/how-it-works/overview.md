# System overview

How SINDIE's control system is put together: the three layers of control and what each may and may not do, the programs and how they talk to each other, how tags are named, and where the data ends up.

## Three layers of control

SINDIE, the Surrey Implanter for Negative-ions, Defect Implantation, and Enrichment, is controlled at three levels, each one beneath the next, and each can overrule the one above it: the PLC applies its own limits and sequences whatever the PC asks, and the safety relay removes power from what it feeds whatever the PLC and the PC do.

```
+---------------------------------------------------------------+
| Python SCADA (the control PC)                                 |
|   services, HMI, data viewer, logger, alarms, sequencer       |
|   requests -> DB10 From_PC             machine state <- To_PC |
+-------------------------------+-------------------------------+
                                |  S7 protocol over TCP/IP, read every 100 ms
+-------------------------------v-------------------------------+
| PLC machine logic (Siemens S7-1200)                           |
|   interlocks, latched faults, shutdown sequences,             |
|   setpoint arbitration, caesium oven and auto-emission loops  |
|        relay enable |                ^ relay state (fibre)     |
+---------------------|----------------|------------------------+
                      v                |
+---------------------------------------------------------------+
| Hardwired safety layer: the safety relay and its loop         |
+---------------------------------------------------------------+
```

> [!CAUTION]
> The software is not a safety system. People and the machine are protected by the safety relay and its hardwired loop, and by the interlocks in the PLC, whatever the HMI shows or sends.

### The hardwired safety layer

| May | May not |
|---|---|
| Close while the PLC gives its enable. After **ENABLE SAFETY RELAY** the HMI asks the operator to press **START** on the **High Voltage Systems Control** box, on the operator's desk; if the relay hasn't closed 5 s after the enable, the PLC latches `Safety_Relay_Refused`. | Be closed by the PLC or the PC: they can only give or withdraw the enable. |
| Remove power from what it feeds, whatever the PLC or the PC do. The source supplies, the caesium oven, the electrostatic steering and the Spellman supplies are powered through it. | Tell the PLC why it opened. The PLC sees only its state, as a light level on a fibre into an analogue input. |

The relay's loop holds:

- two door switches, on the source deck's gullwing door (over the source body) and its mesh door (to the source trolley): red, dual-channel magnetic switches;
- the two emergency switch-offs (ESOs): one on the source deck's side panel, one on the **High Voltage Systems Control** box;
- the key switch, on the same box;
- the vacuum gauge controller's set-point relay;
- the PLC's enable, a switch the PLC drives over a fibre link;
- the magnet coolant's flow meter and thermal cut-off switch.

The PLC program has no inputs for any of these: it sees only whether the relay is closed. The compressed-air pressure switch and the source coolant's flow and temperature switches are wired to PLC inputs instead, and the PLC withdraws its enable when they trip. The X-steerer magnet supply is not on the relay circuit. See [Safety rules](../safety.md#the-high-voltage-systems-control-box) for photos of the box and the source deck.

### The PLC machine logic

| May | May not |
|---|---|
| Switch the extraction supply, the target (cathode), the filament and thermionic supplies, the caesium oven, the electrostatic steering, the source gate valve, the Faraday cup and the beacons. | Close the safety relay by itself. |
| Withdraw the relay enable, which opens the loop. | Drive the Spellman supplies, the magnet supplies, the SMU, the gauge controllers or the turbo pump. It only receives their status flags from the PC (none for the SMU or the X-steerer) and latches them as faults. |
| Apply its own limits, permissives and shutdown sequences over anything the PC asks for, and write back into the PC's request area: it consumes commands, drops enables and replaces setpoints. | Read the switches in the safety loop. |
| Keep the gate valve open and the relay enable on, against the PC's requests, while the source is hot. | Read the vacuum. The gate-valve vacuum permissive comes from the PC, and is lost if the PC's watchdog or the vacuum data stops. |

The PLC program is described in [The PLC program](plc.md).

### The Python SCADA layer

| May | May not |
|---|---|
| Write requests (commands, setpoints, status flags) into the PLC's interface block. | Write the PLC's own status area. The PLC service never writes a tag the registry marks read-only, and takes commands only for `cmd_`, `sp_requested_` and `testing_cmd_` tags. |
| Drive the serial and network instruments directly: Spellman supplies, magnet supplies, SMU, gauge controllers, turbo pump. The Spellman, magnet, X-steerer and SMU services hold their outputs off while they can't confirm the safety relay is energised. | Keep on an output the PLC switches off, or make the PLC apply a setpoint outside its own limits. |
| Log, display, raise alarms and run recipes, mass scans and the optimiser. | Send a fault reset from a recipe, or make the PLC accept a reset in a state where it refuses one. |
| Check every setpoint it sends against the registry limits and the hard caps. | Hand the safety relay, shutdown, fault reset or gate valve controls to a sequence: they can never be leased. |

## The programs and how they connect

Every program runs on the control PC. Each instrument, or bus of instruments, has its own service: a separate Python process that owns the connection to the device, publishes what it reads, and carries out commands for it. The service manager starts and supervises them all. The HMI and the data viewer are separate programs: they listen to the services, and the HMI sends them commands. Closing or restarting either of them leaves the machine as it was.

```
 Instrument          link                  service                    topic
 ----------          ----                  -------                    -----
 S7-1200 PLC    <==  S7, DB10        ==>  service_plc                PLC_DATA
 GRAPHIX x2     <--  RS485 (bridge)  -->  service_vac_gauge_...      VACUUM_DATA
 Source turbo   <--  RS485 (bridge)  -->  service_source_turbo       SRC_TURBO_DATA
 Spellman MPD   <--  RS485 (bridge)  -->  service_spellman_mpd       SPELLMAN_DATA
 Magnet PSU     <--  Modbus over TCP -->  service_magnet_psu         MAGNET_DATA
 X-steerer PSU  <--  Modbus over TCP -->  service_steerer_magnet_psu STEEER_MAGNET_DATA
 SMU            <--  SCPI over TCP   -->  service_smu                SMU_DATA
                                                                         |
          +--------------------------------------------------------------+
          |  ZeroMQ publish/subscribe on 127.0.0.1
          v
   HMI   service_logger   data viewer   service_alarms   service_monitor
         service_sequencer   (and service_plc: see below)

   HMI, service_sequencer --- command + reply ---> any service
   every program ------------ events ------------> service_events --> data/events.db
   every service ------------ heartbeats --------> service manager --> MANAGER topic
```

The other connections:

- `PLC_DATA` also goes to the Spellman, magnet, X-steerer and SMU services, which watch the safety relay in it.
- `VACUUM_DATA`, `SPELLMAN_DATA`, `MAGNET_DATA` and `SRC_TURBO_DATA` also go to `service_plc`, which writes their status flags (comms failures, gauge faults, supply faults, the gate-valve vacuum permissive) into the PLC's interface block.
- `service_monitor` publishes `MONITOR_DATA` (readback out of tolerance), `service_alarms` publishes `ALARMS_DATA` (the alarm list) and `service_sequencer` publishes `SEQUENCER_DATA` (what each sequence is doing). The HMI listens to all three.
- The PLC and the PC share one data block, DB10, which `service_plc` reads every 100 ms. The PC writes only its request area; the PLC writes the status area and may also write back into the requests (to consume a command, or to show the setpoint it is applying).

The messages, commands and control leases are described in [Services and messaging](services.md).

### What the HMI does and doesn't hold

The HMI holds no machine state. It shows what the services publish, ten times a second, and shows a row of dashes for any value that is unknown or comes from a service that has gone quiet. A command from the HMI goes to the service that owns the tag, which answers ok, accepted, rejected (with the reason) or failed. Recipes, mass scans and the optimiser run in `service_sequencer`, not in the HMI.

## Tag names

Each value that is measured, set or logged has a tag name in the tag registry, `config/system_tags.json`. Most follow the pattern:

```
ion_beam.<area>.<unit>.<prefix>_<name>
```

| Part | Values |
|---|---|
| area | `source`, `beamline`, `endstation`, `loadlock`, `facilities`, `system`, `faults`, `internal` |
| unit | the device or subsystem, such as `extraction`, `cesium`, `magnet`, `vacuum_gauge_1` |
| prefix | what kind of value it is (below) |

| Prefix | Meaning | Example |
|---|---|---|
| `rb_` | readback: what the device measures | `ion_beam.source.extraction.rb_voltage` |
| `sp_requested_` | setpoint requested by the PC | `ion_beam.source.extraction.sp_requested_voltage` |
| `sp_actual_` | setpoint in use: what the PLC applies after its own checks, or what the device holds | `ion_beam.source.extraction.sp_actual_voltage` |
| `cmd_` | command | `ion_beam.source.chamber.cmd_open_gv` |
| `stat_` | status flag | `ion_beam.source.chamber.stat_gv_open` |

A few tags sit directly under their area, such as `ion_beam.facilities.safety_relay_active` and `ion_beam.system.cmd_fault_reset`, and a few have no prefix, such as `ion_beam.source.cesium.estimated_temp`. The PLC's fault words are `ion_beam.faults.word_0_system` to `ion_beam.faults.word_5_gauges`.

The services also publish values that are not tags: `faults.<Name>` for each decoded PLC fault bit, `system.connected` and `system.cycle_time_ms` from the services, and per subsystem `<subsystem>.permit`, `.permit_reason`, `.lease_holder` and `.lease_purpose`. These aren't logged.

The registry is generated from the config files: see [Configuration files and the config editor](../engineering/configuration.md).

## Where the data goes

```
 device topics --> service_logger --> data/parquet_logs/   15-minute chunks
                                           |
                                           |  02:00 every night
                                           v
                                     service_data_compactor --> data/daily_logs/
                                                                 day files, rollups
                                                                 --> OneDrive copy
 events from every program --> service_events --> data/events.db
 each program's own log ------------------------> SystemLogs/<program>.log
```

| What | Where |
|---|---|
| Telemetry, 20 rows a second | `data/parquet_logs/` until the nightly compaction, then `data/daily_logs/` |
| Events: faults, alarms, commands, markers, phase changes, config changes | `data/events.db` |
| Alarm acknowledgements and shelves | `data/alarm_state.json` |
| Warnings, errors and tracebacks of each program | `SystemLogs/` |
| Previous versions of config files changed in the HMI | `config/backups/` |

Details are in [Data logging and storage](data-logging.md). The simulator and the tests keep their data elsewhere, so they never mix with the machine's.

## See also

- [The PLC program](plc.md)
- [Services and messaging](services.md)
- [Alarm handling](alarms.md)
- [Data logging and storage](data-logging.md)
- [Safety rules](../safety.md)
- [Glossary](../glossary.md)
