# Messages and what they mean

The texts SINDIE shows when it refuses a command, greys out a control, drops a setpoint entry, stops a sequence, raises an alarm or latches a PLC fault, with what each one means and what to do about it. To find a message, type a fixed part of it into the help window's search box.

## Where messages appear

| Where | What you see |
|---|---|
| **Command Log** panel | Every command this HMI sends, with its answer: OK, ACCEPTED, REJECTED or FAILED from the service, or TIMEOUT or UNREACHABLE from the HMI itself, and the reason. |
| Pop-up at the top right of the HMI | A command that was not carried out: **Command refused**, **Command failed**, **No reply to command** or **Service not running**, with `<tag> = <value>: <result> - <reason>`. One pop-up per command and result every 5 s; it closes after 7 s. |
| Tooltip of a greyed-out control | `Disabled: <reason>`. Hover over the control to read it. |
| **Not sent** pop-up at the top right | A setpoint entry that was dropped, and why. |
| Status lines of the **Recipe Execution**, **Mass Scan** and **Optimiser** panels | Why a sequence was refused, stopped or failed. |
| **Alarms** panel | The alarms raised by the alarm service, and the HMI's own alarm when that service isn't running. |
| **PLC faults** list in the **System Health** panel | The latched PLC faults, as `<NAME>: <description>`. |
| The event log | `data/events.db` (table `events`) and each program's log file in `SystemLogs/`. The data viewer shows some kinds of event on its plots (**Event markers** in its sidebar). |

In the tables, `<...>` stands for the part of a message that changes. In a command's summary, `<tag>` is the last two parts of the tag name, for example `extraction.cmd_enable`.

Commands sent by a recipe, a mass scan or the optimiser come from the sequencer service, not from the HMI, so they don't appear in the **Command Log** panel or as pop-ups. If one is refused, the sequence stops or fails and its status line gives the service's reason: see [Recipe messages](#recipe-messages). Every command, from anywhere, is also recorded as a COMMAND event: tick **Operator Commands (audit)** in the data viewer to see them on the plots.

Where a table says to report something, or for any problem you can't fix, contact Josh Bird (j.m.bird@surrey.ac.uk). For the safety relay's key and for safety matters, contact Dr Luke Antwis, the lab manager. Emergency numbers are on the [Safety rules](safety.md) page.

## Why a command was refused

Every command gets one answer. REJECTED means the service refused it before touching the hardware. FAILED means the service tried, and the device didn't take it or didn't answer. The pop-up and the **Command Log** give the service's reason; the tables below list them by service.

### Refusals any service can give

| Reason | What it means | What to do |
|---|---|---|
| `stale: sent <age> s ago (limit <limit> s)` | The command reached the service later than its limit, so it was not carried out: the service, or the HMI, was busy. The limit is 0.5 s for the device services, 0.5 s plus 0.1 s per NPLC for the SMU, 1 s for the sequencer, and 2 s for the alarm service and the service manager. Clicks made while a program was frozen are not carried out in a burst when it recovers. | Check you still want it, and send it again. If it keeps happening, see [Commands time out](troubleshooting.md#commands-time-out). |
| `not a <device> tag` | The tag doesn't belong to the service it was sent to. `<device>` is `PLC`, `Spellman supply`, `magnet PSU`, `steerer PSU`, `SMU`, `gauge controllers` or `source turbo controller`. | Check the tag name, for example in a recipe step. |
| `unsupported command '<name>'` | The service has no such command for that subsystem. | Check the tag name. |
| `value <value> is not a number` | The value sent isn't a number. | Send a number. |
| `<value> <unit> is outside the allowed range [<min>, <max>] <unit>` | Outside the setpoint limits in the tag registry, `config/system_tags.json`. | Use a value in the range. The limits come from the configuration files: see [Configuration files and the config editor](engineering/configuration.md). |
| `no min/max limits configured for this tag in system_tags.json` | The tag registry has no limits for this setpoint, so the service refuses every value for it. | Ask an engineer to rebuild the registry (`python src/utils/build_registry.py`) and restart the service. |
| `Hard cap: <what> <value> <unit> is above the <cap> <unit> cap - <why>. Fixed in src/core/hard_caps.py: raising it is a code change.` (or `below`) | The value is past a hard cap: the highest value allowed whatever the configuration files say. The caps are ±22 kV for every Spellman supply, 95 A and 25 V for the analyser magnet, 80 A and 40 V for the X-steerer, ±200 V for the SMU bias, 1×10⁻⁵ mbar for the VG1 and VG2 relay points and 1 mbar for the other gauge relays. | Use a value within the cap. Raising a cap is a change to the code. |
| `<subsystem> is controlled by <purpose> - stop it, or take control, first` | A sequence holds a control lease on the subsystem, for example `magnet is controlled by mass scan - stop it, or take control, first`. `<purpose>` is `recipe '<name>'`, `mass scan` or `optimiser`. | Stop the sequence, or click **TAKE CONTROL** twice in the Automation bar. See [The Automation bar](panels/automation-bar.md). |
| `control was taken by the operator` | The answer a sequence gets after the operator took its subsystem. | A recipe pauses: press **RESUME** to carry on. A scan or the optimiser stops. |
| `control modes were replaced by control leases: a running sequence holds what it drives (stop it or take control in the HMI)` | A `cmd_ctrl_mode` command to a service other than the PLC. Old recipes and scripts send it. | Take the step out of the recipe or script. |
| `malformed request: <error>`, `request has no tag` | The request couldn't be read. | A software fault: report it, with the time. |
| `internal error in <service>: <error>` (FAILED) | The service raised an error while handling the command. It carries on running. | Report it, with the time. The traceback is in `SystemLogs/<service>.log`. |
| `received (no explicit result reported)` (ACCEPTED) | Not a refusal: the service took the command but reported no result of its own. | Check the readback. |

### Results added by the HMI

`<service>` in these is the name the HMI sends to: `plc`, `spellman`, `magnet`, `steerer_magnet`, `smu`, `vacuum`, `src_turbo`, `adam`, `sequencer`, `alarms` or `manager`.

| Result and reason | What it means | What to do |
|---|---|---|
| TIMEOUT `no reply from <service> within <n> s` | No answer in time: 3 s for the HMI's commands. The service may be busy, hung or restarting. The command may still have been carried out. | Check the readback before you send it again. See [Commands time out](troubleshooting.md#commands-time-out). |
| UNREACHABLE `<service> service is not running or not connected` | Nothing was sent: the service isn't there to take it. | Check the service in the **Service Manager** panel. See [A service shows BACKOFF](troubleshooting.md#a-service-shows-backoff). |
| UNREACHABLE `no command endpoint for subsystem '<name>'` | The HMI doesn't know where to send the command. | A software fault: report it. |
| UNREACHABLE `send failed: <error>` | The HMI couldn't send it. | Report it. |
| REJECTED `value cannot be serialised: <error>` | The value couldn't be put into a message. | Report it. |
| TIMEOUT `HMI closing` | The HMI was closed while the command waited for its answer. | Check the readback. |

### PLC service

`service_plc` takes the commands for the source supplies (extraction, target (cathode), filament, thermionic, caesium oven), the steering, the Faraday cup, the gate valve and the system controls (safety relay, shutdown, fault reset).

| Reason | What it means | What to do |
|---|---|---|
| `PLC not connected` | The service has no connection to the PLC. | See [PLC COMMS shows OFFLINE](troubleshooting.md#plc-comms-shows-offline). |
| `locked out: <subsystem> is in automatic control mode <n> (<mode>)` | The PLC has the supply under its own control: mode 1 `PLC Auto` (a shutdown sequence), 2 `Recipe`, 3 `Cascaded (PID slave)` (the filament under auto-emission) or 4 `Arc quench`. Every command but a change of mode is refused, switching off included. For the caesium oven, mode 1 means the heater is on, and doesn't lock it. | Wait: an arc quench lasts 1 s, and a shutdown sequence ends by itself or at **SHUTDOWN SEQ: LATCHED SAFE (RESET REQ)**, which needs **RESET FAULTS**. For mode 3, switch **AUTO EMISSION** off. See [A supply does not switch on](troubleshooting.md#a-supply-does-not-switch-on). |
| `the cesium is hot (<t> degC estimated): the PLC keeps the target and the ioniser on until it has cooled, so cesium vapour can't coat the source's ceramics - use forced cooling or a source shutdown` | A command to switch off the target, filament or thermionic supply while the PLC says the caesium is hot (`ion_beam.internal.cs_is_hot`: its thermal model above 30 °C). The ioniser is the thermionic supply. | Let the oven cool: **FORCED COOLING** in the source panel, or **INITIATE SHUTDOWN** in **System Health**. |
| `the cesium is hot (<t> degC estimated): the target must stay at or above <v> kV until it has cooled` | A target setpoint below the PLC's minimum while the caesium is hot (`ion_beam.internal.target_min_voltage`, 3.0 kV). | Keep the target at or above the minimum until the oven has cooled. |
| `safety relay de-energised - it can't be switched on` | A switch-on for a subsystem powered through the safety relay (extraction, target, filament, thermionic, caesium oven, steering) while the relay is off. | Switch the safety relay on first: see [The safety relay does not switch on](troubleshooting.md#the-safety-relay-does-not-switch-on). |
| `<value> is outside the allowed range [<min>, <max>]` | Outside the registry limits. | Use a value in the range. |
| `no min/max limits configured for this tag in system_tags.json`, `value <value> is not a number` | As for any service, above. | |
| `unknown PLC tag (not in system_tags.json)` | The tag isn't a PLC tag in the registry. | Check the tag name. |
| `no value given` | The command had no value. | Send a value. |
| `not a command or setpoint tag - status/permissive tags are set by the PC services only` | Only `cmd_`, `sp_requested_` and `testing_cmd_` tags can be written. | Check the tag name. |
| `tag is read-only` | The registry marks the tag as not writable. | Check the tag name. |
| `PLC write failed: <error>` (FAILED) | The write to the PLC failed. The service drops the connection and makes it again. | Wait for **PLC COMMS: OK** in **System Health**, check the state, and send it again. |
| `bad value for <type>: <error>`, `datatype <type> not supported for writes` (FAILED) | The value couldn't be written as the tag's data type. | Report it. |
| `written to PLC` (OK) | The PLC has the request. It still applies its own limits, permissives and sequences. | Watch the readback. |

> [!NOTE]
> The PLC zeroes the request of a source supply that is switched off, and while the safety relay is off it zeroes every requested setpoint (the caesium oven's to 20 °C). Switch a supply on before you set it. See [A setpoint goes back to zero](troubleshooting.md#a-setpoint-goes-back-to-zero).

### Spellman service

`service_spellman_mpd` takes the commands for the Spellman high-voltage supplies: the source einzel and, once they are configured, the beamline einzel and the two neutral traps.

| Reason | What it means | What to do |
|---|---|---|
| `safety relay de-energised - supply is unpowered` | The supplies are powered through the safety relay. The service treats the relay as off when the PLC says so, and when there has been no PLC data for 2 s. | Switch the safety relay on. |
| `already off: safety relay de-energised` (OK) | A switch-off with the relay off: there was nothing to do. | |
| `<supply> is not configured in mpd_config.json` | The supply has no entry in `config/mpd_config.json`. Only the source einzel (`source_einzel`) is configured. | Ask an engineer to configure the supply. |
| `bus <bus> not connected` | The service can't reach the supply's RS485 bus through its network bridge. It tries again after 1, 2 and 5 s, then every 10 s. | Check the bridge and its network connection. The addresses are in `config/network_config.json`. |
| `Hard cap: the voltage setpoint the supply holds <v> kV is above the 22 kV cap - the cap for every Spellman supply. Fixed in src/core/hard_caps.py: raising it is a code change. Set the voltage here first.` | **ENABLE** refused: the supply holds a setpoint past the cap, set on its front panel for example. Switching it on would drive it. | Set the voltage from the HMI first, then switch the supply on. |
| `voltage not set: the supply holds no current limit and did not accept <n> uA (reply: <reply>); without one its output can't rise` (FAILED) | Every power-up leaves a supply's current limit at 0, and with none its output can't rise. Before the first voltage or switch-on after power-up the service sets one if the supply holds none (`default_current_ua`, 50 µA). The supply refused it. | Check the supply; report it. |
| `output not enabled: the supply holds no current limit and did not accept <n> uA (reply: <reply>); without one its output can't rise` (FAILED) | The same, for **ENABLE**. | As above. |
| `supply did not accept the setpoint (reply: <reply>)`, `supply did not accept the current limit (reply: <reply>)` (FAILED) | The supply refused the value, or didn't answer. | Check the supply and its comms (alarm `source einzel: communication lost`). |
| `no reply from supply` (FAILED) | No answer to a switch-on or switch-off. | As above. After a failed switch-off the output may still be on: check the readback. |
| `supply refused the enable (reply: <reply>)`, `supply refused the disable (reply: <reply>)` (FAILED) | The supply refused. | As above. |
| `supply accepted <v> V (current limit set to <n> uA - the supply held none)`, `output enabled (current limit set to <n> uA - the supply held none)` (OK) | Carried out. The service also gave the supply its current limit. | |
| `software arc latches cleared; hardware fault clear sent to <n> supply(ies)` (OK) | The answer to **RESET FAULTS**. | |

### Magnet and steerer services

`service_magnet_psu` (the analyser magnet, `magnet PSU`) and `service_steerer_magnet_psu` (the X-steerer, `steerer PSU`) are the same program and give the same answers.

| Reason | What it means | What to do |
|---|---|---|
| `magnet PSU not connected`, `steerer PSU not connected` | The service can't reach the power supply over the network. It tries again after 0.5, 1 and 2 s, then every 5 s. | Check the supply is on and connected (alarm `beamline magnet: communication lost` or `beamline steerer magnet: communication lost`). |
| `safety relay de-energised (or no PLC telemetry) - output held off` | The service holds the output at 0 A, 0 V and off while the relay isn't confirmed on: the relay is off, or there has been no PLC data for 1 s. For the X-steerer this is its only interlock. | Switch the safety relay on. The output then stays off until you switch it on again. |
| `already off: safety interlock holds the output off` (OK) | A switch-off with the relay off: there was nothing to do. | |
| `Magnet PSU didn't report its nominal ratings - only 0 A / output off are accepted until it does (setpoints would be scaled wrongly)` (`Steerer PSU` for the steerer) | The service hasn't read the supply's nominal voltage and current, which it needs to scale setpoints. Until it has, it takes only 0 A and output off, and its readbacks show `---`. | Wait: the service asks again every few seconds. If its event says the supply didn't answer at all, restart the service in **Service Manager**. |
| `<what> refused: the PSU's nominal ratings are unknown (only zero / output off until it reports them)` | The same, from the supply's driver. | As above. |
| `voltage is set automatically from the current setpoint` | The voltage can't be set: the service sets the voltage limit from the current. | Set the current. |
| `Hard cap: the setpoint <v> A is above the <cap> A cap - the magnet PSU's current cap. Fixed in src/core/hard_caps.py: raising it is a code change.` (`steerer PSU's` for the steerer) | A current past the cap. | Use a smaller current. |
| `Hard cap: the current setpoint the PSU holds <v> A is above the <cap> A cap - ... Set the current here first (that also sets the voltage limit).` (or `the voltage setpoint`) | **ENABLE** or **DEGAUSS ROUTINE** refused: the supply holds a setpoint past its cap, set on its front panel for example. | Set the current from the HMI first. |
| `Hard cap: magnet_config.json degauss step <v> A is above the 95 A cap - ...` | **DEGAUSS ROUTINE** refused: a step in `degauss_steps_amps` is past the cap. | Ask an engineer to correct `config/magnet_config.json`. |
| `PSU write failed: <error>` (FAILED) | The write failed part-way. The service drops the connection and makes it again. | Check the readbacks before you send it again. |
| `output on`, `output off`, `degauss started`, `nothing to do` (OK) | Carried out. | |

### SMU service

| Reason | What it means | What to do |
|---|---|---|
| `SMU not connected` | The service can't reach the SMU. It tries again after 1, 2 and 5 s, then every 10 s. | Check the SMU is on and connected (alarm `beamline faraday smu: communication lost`). |
| `safety relay de-energised (or no PLC telemetry) - bias held at 0 V` | The bias is held at 0 V and off while the relay isn't confirmed on. A relay trip also forgets the bias setpoint. | Switch the safety relay on, then **ENABLE BIAS** and set the bias again. |
| `bias output is disabled - enable it before setting a voltage` | A bias setpoint with the bias output off. | Press **ENABLE BIAS** first. |
| `SMU connection lost while sending` (FAILED) | The link dropped while the bias was being set. | Wait for the SMU to reconnect, then set the bias again. |
| `bias enabled`, `bias disabled (0 V)`, `NPLC <n>` (OK) | Carried out. | |

### Source turbo service

| Reason | What it means | What to do |
|---|---|---|
| `turbo controller not connected` | The service can't reach the pump's network bridge. | Check the bridge (alarm `source turbo pump: communication lost`). |
| `no reply from pump to the fault-reset/start request` (FAILED) | **START PUMP** or **RESET & START** got no answer from the pump. | Check the pump controller, and try again. |
| `no reply from pump` (FAILED) | A fault reset got no answer. | As above. |
| `the source turbo pump is never stopped from the software: trained staff stop it by hand, at the pump` | A stop command (`cmd_enable` false or 0), from a script or a recipe: the panel has no stop. | If the pump must be stopped and you are trained to, stop it by hand at the pump. |
| `already running` (OK) | A start for a pump already running without a fault: nothing was sent. | |
| `no fault to reset: the pump is running` (OK) | A fault reset for a pump running without a fault: nothing was sent, as the reset message carries a stop. | |
| `start requested`, `fault reset sent` (OK) | Carried out. | |

### Vacuum service

These answer commands to a gauge relay's set points. No HMI control sends them; scripts and recipes can.

| Reason | What it means | What to do |
|---|---|---|
| `only a relay's on / off set points can be set here (its channel comes from vac_gauges_config.json)` | A relay's channel can't be changed by a command. | Change `config/vac_gauges_config.json` instead. |
| `Hard cap: node <node> relay <relay> on point <value> mbar is above the <cap> mbar cap - <why>. Fixed in src/core/hard_caps.py: raising it is a code change.` (or `off point`) | Past the cap: 1×10⁻⁵ mbar for the relays of VG1 and VG2, which decide when the gate valve may open, and 1 mbar for the others. | Use a lower point. |
| `<value> mbar is outside <min>..<max> mbar` | Outside the registry limits. | Use a value in the range. |
| `the on point must stay below the off point (on <on>, off <off> mbar)` | | Change the other point first. |
| `no limits for this relay set point in system_tags.json` | | Ask an engineer to rebuild the registry. |
| `gauge controllers not connected` | The service can't reach the gauge controllers. | Check the bridge (alarm `facilities graphix1: communication lost`). |
| `controller did not acknowledge the write` (FAILED) | | Try again. |
| `unsupported vacuum command`, `cannot parse controller/relay/parameter from tag` | The tag isn't of the form `ion_beam.vacuum.controller_<node>.relay_<relay>_on` (or `_off`). | Check the tag name. |
| `relay <relay> on set to <value>` (OK; or `off`) | Set. The new point is expected until the service restarts; it then writes `config/vac_gauges_config.json` back to the controller. | Change the file as well to keep the new point. |

### Control leases

The answers to the lease commands the sequences send, and to **TAKE CONTROL**. A refused lease pauses a recipe or stops a scan: see [Recipe messages](#recipe-messages).

| Reason | What it means | What to do |
|---|---|---|
| `already controlled by <purpose>` | A sequence tried to take a subsystem another sequence holds. | Stop the other sequence first. |
| `controlled by <purpose>` | A sequence's renewal was refused: another sequence holds the subsystem now. | As above. |
| `held by <purpose>, not by <holder>` | A release from a sequence that doesn't hold the subsystem. | None needed. |
| `<subsystem> can't be leased (operator controls must stay available)` | The system controls (safety relay, shutdown, fault reset) and the gate valve are never leased. | None needed: a sequence sends these without a lease. |
| `took control from <purpose>`, `not held by anything` (OK) | The answer to **TAKE CONTROL**. | |
| `lease value must be {op, holder, purpose, ttl}`, `bad ttl <value>`, `unknown lease op <op> (expected one of claim, renew, release, take)`, `lease commands need a holder id`, `no subsystem <subsystem> here` | A lease command that couldn't be read. | Report it. |

### Sequencer service

| Reason | What it means | What to do |
|---|---|---|
| `the sequencer is starting up - try again in a moment` | For 2 s after it starts, the sequencer collects telemetry before it starts anything. | Try again. |
| `<purpose> is already running - stop it first` | One recipe, one mass scan and one optimiser run can run at a time. | Stop the running one first. |
| `no recipe has run since the sequencer started` (or `mass scan`, `optimiser`) | A stop, pause or other command with nothing to act on, for example after the sequencer has restarted. | None needed. |
| `'<verb>' is not a <kind> command` | The sequence doesn't have that command. | Report it. |
| `not a sequencer command` | | Report it. |
| `internal error in the sequencer: <error>` (FAILED) | | Report it. |

Why a start is refused is given under [Recipe messages](#recipe-messages), [Mass scan messages](#mass-scan-messages) and [Optimiser messages](#optimiser-messages).

### Alarm service

| Reason | What it means | What to do |
|---|---|---|
| `nothing to acknowledge: give ids, or all` | **Acknowledge selected** with nothing selected. | Select the alarms first. |
| `no alarm '<id>'` | The alarm service has no alarm of that name, for example just after it has restarted. | Try again once the list has refreshed. |
| `shelving needs a reason` | | Type a reason in the **Shelve alarm** box. |
| `shelve for 1 to <max> minutes` | Shelving is limited to `shelve_max_minutes` in `config/alarm_config.json` (480 minutes). | Choose a shorter time. |
| `not an alarm command`, `internal error in the alarm service: <error>` | | Report it. |
| `<n> acknowledged`, `shelved for <n> min`, `unshelved`, `was not shelved`, `first-out reset` (OK) | Carried out. | |

### Service manager

| Reason | What it means | What to do |
|---|---|---|
| `unknown service '<name>'` | The manager doesn't run a service of that name. | Report it. |
| `unknown manager command '<action>' (use restart / stop / start)` | | Report it. |
| `<service> restarted`, `<service> stopped`, `<service> started` (OK) | Carried out. A stopped service stays stopped until it is started again. | |

## Why a control is greyed out

Every service publishes, for each subsystem it controls, whether it would accept an operator command now (`<subsystem>.permit`) and, if not, why (`<subsystem>.permit_reason`). The panels grey out those controls and give the reason in the tooltip, as `Disabled: <reason>`. Some panels make checks of their own as well; those are in the second table.

### Reasons published by the services

| Reason | From | What it means | What to do |
|---|---|---|---|
| `no live data from the <name> service` | the HMI | There is no current permit: the service isn't running, or hasn't published for a few seconds. | Check the service in the **Service Manager** panel. |
| `not available` | the HMI | The service refused without giving a reason. | Report it. |
| `PLC not connected` | PLC | The PLC service has no connection to the PLC. | See [PLC COMMS shows OFFLINE](troubleshooting.md#plc-comms-shows-offline). |
| `the PLC reports lost communication with the PC (watchdog)` | PLC | The PLC saw the PC's watchdog unchanged for 3 s (`ion_beam.system.pc_plc_comms_lost`). It latches `PC_Watchdog_Timeout` and starts its comms-loss shutdown. | As above. |
| `safety relay de-energised` | PLC | Extraction, target, filament, thermionic, caesium oven and steering need the safety relay on. | Switch the relay on. |
| `remote I/O to the source deck lost` | PLC | Filament, thermionic and caesium oven: the PLC has lost the remote I/O on the source deck, or `Remote_IO_Lost` is still latched. | Once the link is back, press **RESET FAULTS**. |
| `automated source shutdown in progress` | PLC | Extraction, target, filament, thermionic and caesium oven: a PLC shutdown sequence is running, or has ended latched. **SHUTDOWN SEQ** in **System Health** shows which step. | Let it finish. At **SHUTDOWN SEQ: LATCHED SAFE (RESET REQ)**, press **RESET FAULTS**. See [A shutdown sequence started by itself](troubleshooting.md#a-shutdown-sequence-started-by-itself). |
| `<mode>: the PLC is controlling this (ctrl_mode <n>)` | PLC | The PLC has the supply in mode 1 to 4, as `locked out` in [PLC service](#plc-service). | As there. |
| `PLC interlock (lockout)` | PLC | The PLC's own lockout for the subsystem is set (`<subsystem>.stat_lockout`): an interlock blocks switching it on or changing its setpoint. For the caesium oven: the relay is open, or `Cesium_Oven_OverTemp`, `Cesium_Oven_HeatFail` or `Cesium_Oven_CoolFail` is latched. | Read the **PLC faults** list in **System Health**. |
| `safety relay de-energised - supply unpowered` | Spellman | The relay is off, or there has been no PLC data for 2 s. | Switch the relay on. |
| `not configured in mpd_config.json` | Spellman | The supply has no entry in `config/mpd_config.json`. | Ask an engineer to configure it. |
| `bus <bus> not connected` | Spellman | The supply's RS485 bridge can't be reached. | Check the bridge. |
| `supply not answering` | Spellman | The supply didn't answer its last poll (`stat_comms_fail`). | Check the supply. |
| `magnet PSU not connected`, `steerer PSU not connected`, `SMU not connected`, `gauge controllers not connected` | magnet, steerer, SMU, vacuum | The service can't reach its device. | Check the device is on and connected. |
| `safety relay de-energised (or no PLC data)` | magnet, steerer, SMU | The relay isn't confirmed on: it is off, or there has been no PLC data for 1 s. | Switch the relay on; check **PLC COMMS**. |
| `turbo controller not connected`, `pump not answering` | source turbo | The bridge can't be reached, or the pump doesn't answer through it. | Check the bridge and the pump controller. |
| `<subsystem> is controlled by <purpose> - stop it, or take control, first` | any | A sequence holds the subsystem. | Stop the sequence, or click **TAKE CONTROL** twice. |
| `permit check failed: <error>` | any | The check itself failed, so the control is treated as not available. | Report it. |

### Reasons given by the panels

| Text | Panel and control | What it means | What to do |
|---|---|---|---|
| `Disabled: GUI to PLC communications are offline.` | **System Health**: every button. **TESS Ion Source Control**: **FORCED COOLING**. | No PLC data, or the PLC reports the PC watchdog lost. | See [PLC COMMS shows OFFLINE](troubleshooting.md#plc-comms-shows-offline). |
| `Disabled: The Gate Valve must be fully OPEN before the safety circuit can be energized.` | **ENABLE SAFETY RELAY** | The gate valve isn't fully open. | Open the gate valve first. |
| `Disabled: You cannot drop the safety relay while the source is hot or active. Run a shutdown first.` | **RELAY ENERGIZED (LOCKED)** | The gate valve's close permissive is false: extraction or target above 0.15 kV, or the filament or caesium hot. The PLC holds the relay on then in any case. | Use **INITIATE SHUTDOWN**. |
| `Disabled: Shutdown sequence already in progress.` | **INITIATE SHUTDOWN** | A shutdown step is showing. | Let it finish. |
| `Disabled: Safety Relay is dead. Source is already powered off.` | **INITIATE SHUTDOWN** | The relay is off. | None needed. |
| `Disabled: All power supplies are off and the source is already cold.` | **INITIATE SHUTDOWN** | Nothing to shut down. | None needed. |
| `Disabled: Abort is only permitted during the initial 'Hold Hot' phase.` | **ABORT SHUTDOWN** | An operator shutdown can be stopped only at **HOLD HOT PHASE** (step 10). | None. |
| `Disabled: PLC telemetry stream is dead.` | **Vacuum System**: **OPEN** and **CLOSE** (both read **OFFLINE**). **Faraday Cup & SMU**: the Faraday cup button. | No PLC data. | See [PLC COMMS shows OFFLINE](troubleshooting.md#plc-comms-shows-offline). |
| `Disabled: Gate valve hardware fault detected.` | **OPEN** and **CLOSE** (both read **FAULT LOCKOUT**) | `GateValve_Timeout` or `GateValve_Conflict` is latched. | Check the valve, its air and its limit switches, then press **RESET FAULTS**. |
| `Disabled: Gate valve is already open.`, `Disabled: Gate valve is already closed.` | **OPEN**, **CLOSE** | | None needed. |
| `Disabled: the PLC doesn't allow the valve to open. It needs VG1 and VG2 read, healthy and below their set points, and no VG1, VG2, GRAPHIX or watchdog fault latched (F1 for more).` | **OPEN** | The PLC's gate-valve open permissive (`ion_beam.source.chamber.stat_gv_open_permissive`) is false. | See [The gate valve does not open](troubleshooting.md#the-gate-valve-does-not-open). |
| `Disabled: Source must be cold (HV, Filament, and Cesium OFF) to close manually.` | **CLOSE** | The gate valve's close permissive is false (see above). | Use **INITIATE SHUTDOWN**. |
| `Not available: the turbo service isn't sending data (stopped, restarting or not running).`, `Not available: the link to the pump's controller is down.`, `Not available: the pump isn't answering.` | **START PUMP** | The pump's readings aren't live (the chip reads **STALE** or **OFFLINE**). | Check `service_source_turbo` in **Service Manager**, then the pump's network bridge and the pump. |
| `The pump is slowing down. It can be started again once it has stopped.` | **START PUMP** | The pump has been stopped, or has tripped, and is running down. | |
| `Disabled: Hardware interlock or fault is active.` | source panel setpoint (badge **LOCKED**) | The PLC's lockout for the supply is set (`stat_lockout`). | Read the **PLC faults** list in **System Health**. |
| `Disabled: auto-emission is on - the PLC sets the filament current to hold the thermionic emission setpoint.` | the filament setpoint box (badge **AUTO-EMIS**) | Auto-emission drives the filament. | Switch **AUTO EMISSION** off to set the filament by hand. |
| `Disabled: Control yielded to automated sequencer.` | source panel setpoint (badge **AUTO**) | PLC mode 1 on a supply other than the oven: a PLC shutdown sequence has it. | Let the sequence finish. |
| `Disabled: Control yielded to active Recipe.` | source panel setpoint (badge **RECIPE**) | PLC mode 2. No part of the software sends mode 2 now: recipes use control leases. | Ask an engineer: see [A supply does not switch on](troubleshooting.md#a-supply-does-not-switch-on). |
| `Disabled: Target is dynamically cascaded from a master PID loop.` | source panel setpoint (badge **CASCADED**) | PLC mode 3. In the PLC only the filament takes mode 3, while auto-emission drives it, although the tooltip says target. | Switch **AUTO EMISSION** off. |
| `Disabled: Output temporarily held by Arc Quench algorithm.` | source panel setpoint (badge **ARC QUENCH**) | Mode 4: after an arc the PLC sets the supply's applied setpoint to 0 for 1 s (extraction, target, thermionic; the thermionic contactor opens too). The requested setpoint comes back afterwards. | Wait 1 s. Three arcs close together latch a `_Sustained_Arc` fault: see [PLC faults](#plc-faults). |
| `Held by <purpose> (stop it, or take control in the Automation bar)` | source panel badge **SEQUENCE** (its tooltip) | A sequence holds the supply. | Stop the sequence, or click **TAKE CONTROL** twice. |
| `Contactor remains closed while safe step-down ramp drops current to 0A.` | filament button **DISARMING...** | The PLC is ramping the filament down. | Wait. |
| `Disabled: Magnet hardware interlock/cooling fault.` | **Beamline Optics**: the magnet controls | The magnet coolant switch is open (`ion_beam.facilities.stat_mag_coolant_ok`): **MAG COOLING: FAULT**. | Restore the magnet cooling. |
| `Disabled: Events microservice is offline (Cannot log manual adjustments).` | **Beamline Optics**: the slit width boxes | Slit widths only go into the event log, and `service_events` isn't ONLINE. | Start `service_events` in **Service Manager**. |
| `Disabled: Facility air pressure is lost. Actuator cannot be moved.` | **Faraday Cup & SMU**: the Faraday cup button | The compressed-air switch is open (`ion_beam.facilities.stat_air_press_ok`). | Restore the compressed air. |
| `Disabled: Faraday Cup is locked <state> by an active PLC sequence.` | the Faraday cup button | The PLC's lockout for the cup is set. | Let the PLC sequence finish. |
| `Disabled: SMU Microservice or PLC telemetry is offline.` | **ENABLE BIAS**, the bias setpoint, the beam current readout | No data from the SMU service, the SMU not answering, or no PLC data. | Check `service_smu` and **PLC COMMS**. |
| `Disabled: Bias requires Safety Relay to be closed.` | **ENABLE BIAS**, the bias setpoint | The relay is off. | Switch the relay on. |
| `Disabled: Service Manager is offline.`, `Disabled: service_events is <state>.` | **Experiment Logging**: every control | Phases and markers go to `service_events`, which must be ONLINE. | Start it in **Service Manager**. |
| `Load a recipe first (and the sequencer must be idle).` | **START** in **Recipe Execution** | No recipe loaded, or a start is still waiting for its answer. | Press **LOAD RECIPE**. |
| `A recipe is running - stop it first.` | **START** | | Stop the running recipe first. |
| `Stop is only available while a recipe is running.` | **STOP** | | None needed. |
| `The sequencer service is not running (see the Service Manager panel).` | the start buttons of **Recipe Execution**, **Mass Scan** and **Optimiser** | No data from `service_sequencer`. | Start it in **Service Manager**. |
| `Disabled: Magnet service not running (no live data).`, `Disabled: SMU service not running (no live data).` | **START PRECISION SWEEP** in **Mass Scan** | | Start the service in **Service Manager**. |
| `Disabled: Magnet: <reason>`, `Disabled: SMU: <reason>` | **START PRECISION SWEEP** | The magnet's or the SMU's reason, from the first table. | As there. |
| `Disabled: Magnet PSU must be enabled.` | **START PRECISION SWEEP** | The analyser magnet is switched off. | **ENABLE MAGNET** in **Beamline Optics**. |
| `Disabled: Select both X and Y parameters to sweep.` | **START 2D RASTER SWEEP** in **Optimiser** | | Choose both parameters. |
| `Disabled: No live data from the service that drives <tag>.` | **START 2D RASTER SWEEP** | | Check the service in **Service Manager**. |
| `Disabled: <subsystem>: <reason>` | **START 2D RASTER SWEEP** | The reason of the subsystem one of the parameters belongs to. | As in the first table. |
| `Disabled: Hardware output for selected target is disabled.` | **START 2D RASTER SWEEP** | The supply of one of the parameters is switched off. | Switch it on. |

Also greyed out, without a reason of their own:

- **Stop**, **Start** and **Restart** in **Service Manager**, while it shows **SERVICE MANAGER: OFFLINE**.
- The range boxes in **Current Monitoring**, while the cards show **OFFLINE** (the current monitor's service isn't started).
- **Acknowledge selected**, **Shelve...** and **Unshelve** in **Alarms**, until alarms they can act on are selected. Their tooltips say how to select.

## Status texts in the panels

Texts in the panels that mean something is wrong or not known.

| Text | Panel | What it means | What to do |
|---|---|---|---|
| **PLC COMMS: OFFLINE** | System Health | No data from the PLC service, the service has lost the PLC, or the PLC reports the PC watchdog lost. | See [PLC COMMS shows OFFLINE](troubleshooting.md#plc-comms-shows-offline). |
| **SAFETY: UNKNOWN**, **SHUTDOWN SEQ: UNKNOWN**, `Telemetry offline: fault status unknown` | System Health | Shown with **PLC COMMS: OFFLINE**. | As above. |
| **SAFETY: TRIPPED/REFUSED** | System Health | The relay is off with `Safety_Relay_Tripped` or `Safety_Relay_Refused` latched. | See [The safety relay dropped out](troubleshooting.md#the-safety-relay-dropped-out). |
| **SAFETY: NO REMOTE IO** | System Health | The relay is off with `Remote_IO_Lost` latched or reported. | Once the link is back, press **RESET FAULTS**. |
| **SAFETY: DE-ENERGIZED** | System Health | The relay is off, with no relay fault. | Normal with the relay off. |
| **SAFETY: LIVE** | System Health | The relay is on: high voltage may be present. | |
| `PRESS PANEL START (<n>s)` | System Health: **ENABLE SAFETY RELAY** | The PLC is waiting for the relay to close. | Press the panel's start button before the count ends. |
| **SHUTDOWN SEQ: GRACE PERIOD (75% EXTR)** | System Health | Step 1: the PC watchdog or the remote I/O is lost. The PLC holds extraction at 75 % of its readback for up to 60 s. | See [A shutdown sequence started by itself](troubleshooting.md#a-shutdown-sequence-started-by-itself). |
| **SHUTDOWN SEQ: CESIUM COOLDOWN** | System Health | Step 2: extraction off, gate valve closed (so the relay opens), forced cooling. | Let the caesium cool. See the warning under [PLC faults](#plc-faults). |
| **SHUTDOWN SEQ: FINAL KILL** | System Health | Step 3: the relay kill is on; target, filament and thermionic off. | Wait for the next step, or press **RESET FAULTS** when the cause is cleared. |
| **SHUTDOWN SEQ: LATCHED SAFE (RESET REQ)** | System Health | Step 99: the sequence has ended and holds everything off. | Clear the cause, then press **RESET FAULTS**. |
| **SHUTDOWN SEQ: HOLD HOT PHASE**, **SHUTDOWN SEQ: FILAMENT RAMP DOWN** | System Health | Steps 10 and 11 of an operator shutdown (**INITIATE SHUTDOWN**). | Let it finish; **ABORT SHUTDOWN** works only at step 10. |
| **NOT FOUND** | Vacuum System: a gauge card | The controller reports no gauge on the channel (status 5). The PLC latches `VG<n>_Not_Found`. | Check the gauge head and its cable. |
| **RAPID RISE** | Vacuum System: a gauge card | The pressure rises faster than `rapid_rise_thresh_mb_s` in `config/vac_gauges_config.json`. The PLC latches `VG<n>_Rapid_Rise_Warn`. | Look for the cause: a leak, or a burst of gas such as a new filament degassing. |
| **SENSOR OFF** | Vacuum System: a gauge card | The controller reports the gauge switched off (status 3). The PLC latches `VG<n>_Type_Mismatch` for it. | Switch the gauge on at the controller. |
| `FAULT (<code>)` | Vacuum System: a gauge card | Another status code from the controller. The PLC latches `VG<n>_Type_Mismatch`. | Check the gauge and the controller. |
| **TRIPPED** | Vacuum System: a gauge card | The gauge's relay is off: the pressure is above its set point. The PLC latches `VG<n>_Above_SP_Warn`. | Find why the pressure is up. |
| **WARNING** | Vacuum System: a gauge card | The pressure is above the relay's on point by up to 25 %. The relay drops at its off point. | Watch the pressure. |
| **STALE** *time*, the value greyed | Vacuum System: gauge cards, turbo | No live reading: the value shown is the last one read, *time* ago. The tooltip says why: the service isn't sending data, the controllers or the pump aren't answering, or no good reading from that gauge for 3 s. | Check the service in **Service Manager**, then the device. See [Readings that have stopped](panels/vacuum.md#readings-that-have-stopped). |
| **NO READING** | Vacuum System: a gauge card | The vacuum service is running but hasn't had a reading from this gauge since the HMI started. | Check the gauge and its controller. |
| **OFFLINE** | Vacuum System: gauge cards, turbo | No data from the vacuum or turbo service since the HMI started. | Check the service in **Service Manager**. |
| **ACCELERATING**, **TURNING**, **SLOWING DOWN** | Vacuum System: source turbo pump | The rotor turns below its speed: faster than a moment ago, steady, or slower (stopped, or tripped). | |
| **VACUUM PERMISSIVE HELD:** *n* **s LEFT** | Vacuum System: gate valve | The vacuum service is restarting; the PLC is given its last judgment of the vacuum for *n* s more at most. | Let the vacuum service finish starting. See [When the vacuum service restarts](panels/vacuum.md#when-the-vacuum-service-restarts). |
| **TRIPPED** | Vacuum System: source turbo pump | The pump reports an error and isn't ready. | **RESET & START**; then **RESET FAULTS** for `Src_Turbo_Trip`. |
| **UNKNOWN**, **TRAVELLING**, **SENSOR CONFLICT** | Vacuum System: gate valve | No PLC data; neither limit switch made; both made. | A valve still travelling after 5 s latches `GateValve_Timeout`. **SENSOR CONFLICT** latches `GateValve_Conflict`. |
| **FAULT: GATEVALVE_TIMEOUT**, **FAULT: GATEVALVE_CONFLICT** | Vacuum System: gate valve | The fault is latched. | See [Fault word 0 System](#fault-word-0-system). |
| ` [?]` after a readback | TESS Ion Source Control | The value may be old: PLC comms are lost, or (source einzel) the Spellman service isn't ONLINE. | Check **PLC COMMS** and **Service Manager**. |
| `~` before the caesium temperature | TESS Ion Source Control | With the relay off or the remote I/O lost there is no thermocouple reading, so the PLC's estimate (its thermal model) is shown. | |
| `---` | any | Not known. | See [A value shows dashes](troubleshooting.md#a-value-shows-dashes). |
| **MAG COOLING: FAULT**, **MAG COOLING: UNKNOWN** | Beamline Optics | The magnet coolant switch is open; no PLC data. | Restore the magnet cooling; check **PLC COMMS**. |
| **FARADAY CUP: TRAVELLING**, **FARADAY CUP: LOGIC ERROR**, **FARADAY CUP: UNKNOWN** | Faraday Cup & SMU | Neither position switch made (unpowered or moving); both made; no PLC data. | Check the compressed air and the cup's switches. |
| **STATUS: SMU OFFLINE**, **NO MEASUREMENT** | Faraday Cup & SMU | No data from the SMU service, the SMU not answering, or no PLC data. | Check `service_smu` and **PLC COMMS**. |
| **STATUS: SAFETY LOCK**, **NO MEASUREMENT** | Faraday Cup & SMU | The relay is off: the bias is held at 0 V and the reading isn't valid. | Switch the relay on. |
| **SERVICE MANAGER: OFFLINE**, **UNKNOWN** | Service Manager | No data from the service manager for 2.5 s. | Start the service manager. |
| **OFFLINE**, **STARTING**, **CRASHED**, **HANGING**, **BACKOFF**, **STOPPED** | Service Manager: a service | See [A service shows BACKOFF](troubleshooting.md#a-service-shows-backoff). | |
| **OFFLINE**, **OVERRANGE**, `CLIP [<range>]`, **RANGING...** | Current Monitoring | No data from the current monitor; a channel past its input range; changing range. | The current monitor's service isn't started at present, so the cards show **OFFLINE**. |
| **ALARM SERVICE NOT RUNNING** | Alarms | See [Message boxes and banners in the HMI](#message-boxes-and-banners-in-the-hmi). | |

## Setpoint entries that were not sent

A setpoint box sends only what you type or step, when you press Enter or move on to another control. An entry that is dropped is reported in an amber **Not sent** pop-up at the top right of the HMI (for 8 s) and in `SystemLogs/hmi.log`. `<box>` is the box's name, for example `Extraction setpoint`, `Magnet setpoint` or `SMU bias setpoint`; `<entry>` is what you typed (`your entry` if nothing can be shown).

| Note | What it means | What to do |
|---|---|---|
| `<box>: <entry> not sent - the box was locked while you were typing (its tooltip says why).` | The box was greyed out before you pressed Enter, for example by a relay trip or a sequence taking the subsystem. The keyboard goes back to the window, not to the next control. | Hover over the box to read why. Enter the value again once the box is available. |
| `<box>: <entry> not sent - its panel was closed or switched away before you pressed Enter.` | You closed the panel, or chose another tab in its group, with the entry not yet sent. | Go back to the panel and enter the value again. |
| `<box>: <entry> not sent - its panel was switched away before you pressed Enter.` | You typed into a box whose panel's tab isn't showing. | As above. |
| `<box>: '<entry>' is not a value - nothing sent. Type a value and press Enter, or Escape to go back to the setpoint.` | You pressed Enter on text that isn't a number: empty, or a lone `-`. The entry stays in the box. | Type a number and press Enter, or press Escape. |
| `<box>: <entry> not sent - '<entry>' is not a value.` | You moved on from text that isn't a number. | Type a number. |
| `<box>: <entry> not sent - the setpoint changed to <value> while you were typing - enter it again if you still want it.` | Something else set this setpoint (a recipe, a scan, another HMI) after you started typing. | Check the new setpoint, and enter yours again if you still want it. |

Escape drops an entry without a note. If the HMI window loses the focus (another program, or a dialog), the entry waits until you come back. While a setpoint isn't known the box is empty and the arrow keys do nothing; you can still type a whole value. See [How the controls behave](getting-started/controls-and-colours.md).

## Message boxes and banners in the HMI

| Title or text | Where | What it means | What to do |
|---|---|---|---|
| **Command refused**, **Command failed**, **No reply to command**, **Service not running** (**Command problem** otherwise): `<tag> = <value>: <result> - <reason>` | pop-up, top right | A command was not carried out. Refused and failed show in red, no reply and not running in amber. | See [Why a command was refused](#why-a-command-was-refused). |
| **Not sent** | pop-up, top right | A setpoint entry was dropped. | See [Setpoint entries that were not sent](#setpoint-entries-that-were-not-sent). |
| **ALARM SERVICE NOT RUNNING**, with the alarm `The alarm service is not running - alarms are NOT being monitored. Restart it from the Service Manager panel.` | Alarms | No data from `service_alarms`. The HMI sounds this critical alarm itself; it can't be acknowledged. | Start `service_alarms` in **Service Manager**. **SILENCE** stops the sound meanwhile. |
| `Alarms: waiting for the alarm service...` | Alarms | No alarm data yet since the HMI started. | Wait a few seconds. |
| `SEQUENCER OFFLINE - The sequencer service is not running (see the Service Manager panel).` | Recipe Execution | No data from `service_sequencer`. | Start it in **Service Manager**. |
| `<VERB> refused: <reason>` | Recipe Execution status line | **START**, **PAUSE**, **RESUME** or **STOP** was refused. | See [Recipe messages](#recipe-messages). |
| `Not accepted - <reason>` | Recipe Execution, under a prompt | The recipe didn't accept your acknowledgement. | See [Recipe messages](#recipe-messages). |
| `Status: <verb> refused - <reason>` | Mass Scan, Optimiser status lines | A start or stop was refused. | See [Mass scan messages](#mass-scan-messages) and [Optimiser messages](#optimiser-messages). |
| `NOT LOADED: <file> - <reason>` | Recipe Execution status line, after **LOAD RECIPE** | The file can't be read as a recipe. | See [Recipe messages](#recipe-messages); open it with **EDIT** to put it right. |
| `NOT READY: <name> (version <v>) has errors`, and above the steps `Can't be started - the sequencer would refuse it (<n> errors): ...` | Recipe Execution, after **LOAD RECIPE** | The checks found errors: the sequencer would refuse to start it, so **START** stays off. | Open it with **EDIT**: the builder lists every problem. See [The checks](panels/recipes.md#the-checks). |
| `<n> warnings (it can still run): ...` | Recipe Execution, above the steps | The checks found something probably not meant. | Read them; **EDIT** shows them all. |
| **Access Denied**: `Incorrect password.` | **Config** in the menu bar | The engineering password was wrong. | |
| **Layout**: `The layout '<name>' could not be restored (saved by an older version?).` | **View > Layouts** | A layout saved by an older version of the HMI can't be used. | Arrange the panels and save the layout again. |
| **No mass calibration**: `The Mass Scan panel's calibration is not available.` | **CALCULATE & SET AMPS** in Beamline Optics | The **Mass Scan** panel's calibration couldn't be read. | Report it. |
| **Beam energy unknown**: `No live target / extraction voltage readback, so the beam energy is unknown.` | **CALCULATE & SET AMPS** | The beam energy is taken from the target and extraction readbacks, and one has no live value. | Check the source supplies and **PLC COMMS**. |
| **Calculation error**: `Beam energy is <E> keV - cannot resolve a mass.` | **CALCULATE & SET AMPS** | The beam energy (target plus extraction readback) is zero or less, or the calibration's k is, so no current can be worked out. | Check the supplies and the calibration. |
| **Hard cap**: `<m> amu at <E> keV needs <I> A.` and the cap | **CALCULATE & SET AMPS** | The current needed is past the magnet's cap. | Nothing is sent. |
| **Out of range**: `<m> amu at <E> keV needs <I> A, outside 0 - <max> A.` | **CALCULATE & SET AMPS** | Past the magnet setpoint's range. | Nothing is sent. |
| **Invalid Parameters**: `Start and Stop values cannot be identical.` | Mass Scan, Optimiser | | Change start or stop. |
| **Dependency Error**: `scipy library required for peak detection. (pip install scipy)` | Mass Scan: **Auto-detect peaks** | | Ask an engineer to install it. |
| **Analysis Error**: `Peak detection failed: <error>` | Mass Scan | | Try other settings, or report it. |
| `No peaks detected.` | Mass Scan, below the peak table | | Lower the sensitivity, or check the trace. |
| **Load Error**: `Failed to parse CSV: <error>`; **Missing Data**: `CSV missing voltage columns; peak mass analysis may use defaults.` | Mass Scan: **Load trace...** | The file isn't a mass scan trace, or has no voltages (30 keV is then assumed). | |
| **No Data**: `No trace data available to save.`; **Save Error**: `Could not save the trace: <error>` | Mass Scan: **Save trace** | | Check the export folder. |
| **Fit Error**: `<error>` | Mass Scan: **Calculate fit** | The calibration points can't be fitted. | Check the points. |
| **Can't open the recipe**: `<file> can't be opened: <error>` | recipe builder | The file isn't a recipe (not valid JSON, or not a JSON object). | |
| **Not saved**: `The recipe has errors - the sequencer would refuse to start it:` and the errors | recipe builder: **Save** | The same checks the sequencer makes before it starts a recipe. | Put them right: the **Problems** tab lists them. |
| **Not saved**: `<File> has changed since it was opened here (saved from another recipe builder, edited by hand, or a git pull): open it again and make the change again, so that change isn't undone.` | recipe builder: **Save** | Someone changed the file after you opened it. Nothing was written. | Open the recipe again (**Open**) and make your change again. |
| **Not saved**: `<File> has been deleted or renamed since it was opened here.` | recipe builder: **Save** | | Use **Save as new recipe**. |
| **Not saved**: `<File> could not be written: <error>.` | recipe builder: **Save** | The folder or the file can't be written. | |
| **Unsaved changes**: `'<name>' has changes that aren't saved.` | recipe builder, closing it or opening another recipe | | **Save**, **Discard the changes** or **Cancel**. |
| `Saved <file>, version <v>: committed to git (<id>)` or `: not committed to git: <why>` | recipe builder, bottom line | The recipe is saved; whether git has it (not when the folder isn't a git work tree). | |
| `The tag registry isn't loaded: this tag can't be checked` | recipe builder, under a tag | The tag registry file is missing or can't be read: the builder can't check tags, and offers none to choose. | Ask an engineer to build it (see [Configuration files](engineering/configuration.md#the-tag-registry)). |

## Recipe messages

### Starting and controlling a recipe

A refused **START** shows as `START refused: <reason>` on the panel's status line; the other buttons likewise.

| Message | What it means | What to do |
|---|---|---|
| `no recipe file given` | | Load a recipe. |
| `<file> is not in the recipes folder`, `no recipe <file> in <folder>` | The file isn't in `config/recipes/`. | Pick the recipe again from the list. |
| `<file> can't run: step <n>: <problem>; ...` | The checks found errors (see [The checks](panels/recipes.md#the-checks)): a tag the machine doesn't know or a recipe can't set, a value outside its range or past a hard cap, a fault reset, a stop of the turbo pump ... The first three are given. | Open it with **EDIT**: the builder lists every problem. |
| `<file> is not valid JSON: <error>`, `<file>: a recipe must be a JSON object`, `a step must be a JSON object, not <type>` | The file can't be read as a recipe. | Open it with **EDIT** in the recipe builder, correct it and save. |
| `<file>: step <n>: <problem>`, for example `unknown step type '<type>'`, `duration_sec must be a number, not '<value>'`, `unknown condition '<c>'`, `unknown routine '<name>'` | A step the sequencer can't run as it is. | Correct the step in the recipe builder. |
| `<file>: step <n>: recipes may not reset faults (<tag>): press RESET FAULTS, or RESET & START for the turbo pump, yourself` | | Take the step out. |
| `<file>: fault policy entry <n>: <problem>` | A stop condition the sequencer can't evaluate (a bad fault bit, no tag, an unknown condition). | Correct or remove it on the builder's **Stop conditions** tab. |
| `<file> step <n>: <sub-recipe> isn't in the recipes folder`, `... there is no recipe <sub-recipe> in the recipes folder`, `... <sub-recipe> can't be read: <error>` | A sub-recipe is missing. | Correct the step, or put the recipe back. |
| `<file> step <n>: the recipes call each other in a circle (<a> > <b> > <a>), so it would never end`, `... sub-recipes nested more than 8 deep` | | Correct the recipes. |
| `<file> step <n>: <sub-recipe>: <problem>` | A sub-recipe has an error of its own. | Correct the sub-recipe. |
| `<file>: step number(s) <n> used more than once - open the recipe in the recipe builder and save it to renumber the steps` | | Do as it says. |
| `the sequencer is starting up - try again in a moment`, `recipe '<name>' is already running - stop it first` | See [Sequencer service](#sequencer-service). | |
| `the recipe is not running`, `the recipe is not paused` | **PAUSE** or **RESUME** with nothing to act on. | None needed. |
| `the recipe is paused - resume it first` | **ACKNOWLEDGE** while the recipe is paused. | Press **RESUME**. |
| `no prompt is waiting for an answer`, `that prompt has already been answered`, `bad prompt number <n>` | An acknowledgement that doesn't match the prompt now showing. | None needed. |
| `not yet: waiting until <condition> (<tag> = <value>)` (shown as `Not accepted - not yet: ...`), for example `waiting until the source gate valve is closed (ion_beam.source.chamber.stat_gv_closed = 0.0)` | The prompt's conditions don't hold yet. Only live values count: `<value>` reads `no live value` for a tag without one. | Make the conditions hold, then press **ACKNOWLEDGE** again. |
| `not yet: the step's conditions are not met` | The same, without details. | As above. |

### Why a recipe stopped

While a recipe runs the status line reads `<STATE> (<recipe>): <status>`; when it has ended, `FINISHED (<recipe>): <message>`. A recipe that failed also raises the alarm `recipe '<name>' failed: <message>`. Stopping or failing switches nothing off: every output stays as the completed steps left it.

| Message | What it means | What to do |
|---|---|---|
| `Recipe completed successfully.` | | |
| `Stopped by operator at step <n> - outputs left as they were.` | **STOP** was pressed. | Check the outputs. |
| `Stopped: the sequencer is shutting down at step <n> - outputs left as they were.` | `service_sequencer` was stopped or restarted (by **Service Manager**, the config editor, or the service manager after a hang). A recipe doesn't continue after a restart. | Check the outputs; start the recipe again if needed (it starts from step 1). |
| `Step <n> failed: <reason>` | A step failed. | See [Step failures](#step-failures). |
| `Step <n> failed: sub-recipe <file>: <error>` | (Sub-recipes are checked before the start: this would be a fault in the sequencer.) | Report it. |
| `Stopped at step <n>: fault <name> is active.` (`... (a stop condition of '<sub-recipe>').` for a sub-recipe's) | A PLC fault in the recipe's stop conditions is latched. The recipe ends as failed. | Deal with the fault: see [PLC faults](#plc-faults). |
| `Stopped at step <n>: <message> (<condition>).`, or the condition alone | A condition in the recipe's stop conditions holds, for example `the safety relay is off`. | Deal with the cause. |
| `Stopped at step <n>: the stop conditions can't be checked - no live value for <tags> for 3 s.` | A reading the stop conditions need had no live value for 3 s (its service is silent). | See [PLC COMMS shows OFFLINE](troubleshooting.md#plc-comms-shows-offline), or the service of the tag named. |
| `Stopped at step <n>: could not keep control of the <subsystem>: <reason>` | The recipe lost a control lease other than to **TAKE CONTROL**, for example `already controlled by mass scan`. | Stop the other sequence, then start the recipe again. |
| `Paused: the operator took control of the <subsystem>. Resume to carry on (the current step starts again), or stop.` | **TAKE CONTROL** was used. | **RESUME**: the recipe takes control again and runs the current step again from its start, sending its command again. Or **STOP**. |
| `Paused by operator at step <n>` | **PAUSE** was pressed. The step's timers are frozen. | **RESUME** or **STOP**. |
| `Step <n>: waiting for the operator` | A prompt is showing in the **Recipe action required** box. | Do what it asks, then **ACKNOWLEDGE**. |

### Step failures

The `<reason>` in `Step <n> failed: <reason>`. A missing live value shows as `None` in `(last: ...)`.

| Reason | Step | What it means | What to do |
|---|---|---|---|
| `<tag> = <value>: <result> - <reason>` | ACTION, VERIFIED_ACTION, routines | A command was refused, failed or got no answer. The reason is the service's. | See [Why a command was refused](#why-a-command-was-refused). |
| `no live value for <tag> for 10 s` | WAIT_TELEMETRY | The value being waited for has had no live value for 10 s: its service is silent, or it is unknown. | Check the service. |
| `timed out after <t> s waiting for <tag> <condition> <value> (last: <value>)` (with `± <tolerance>` for within) | WAIT_TELEMETRY | The condition didn't hold within the step's time limit. | Check why the value didn't get there. |
| `prerequisite <tag> has no live value`, `prerequisite not met: <tag> = <value> (needs <condition> <value>)` | VERIFIED_ACTION | The step's precondition didn't hold, so nothing was sent. | Make it hold, then start again. |
| `<tag> did not reach <condition> <value> within <t> s (last live value: <value>)` | VERIFIED_ACTION | The command was carried out, but the readback didn't get there within the step's time (5 s unless the step says otherwise, counted from when the command was sent). | Check the device. |
| `routine <name>: <error>`, `routine <name> failed` | ROUTINE_CALL | Settings the routine can't use (checked before the start, so a fault in the sequencer), or a routine that failed without a reason. | Report it. |
| `a parallel sub-step failed`, or the sub-steps' own reasons joined by `; ` | PARALLEL | | As for each reason. |
| `internal error in the <type> step: <error>` | any | | Report it. |
| `failed/timed out` | any | A failure without a reason. | Report it. |

### Routines

**safety_relay_init** brings up the safety relay from a recipe. Its prompts, in the **Recipe action required** box (where it says OK, press **ACKNOWLEDGE**):

- `The source pressure is <p> mbar: the gate valve should only open below <limit> mbar. Wait for it, then click OK.` (`not known` without a live value; only with the valve closed, and unless **Source pressure needed first** is 0)
- `Safety Relay requires Gate Valve to be OPEN. Click OK to attempt an automatic Open, or manually open it now.`
- `Gate Valve cannot be opened: Permissive is FALSE. Resolve vacuum/gauge fault, then click OK to retry.`
- `Gate Valve failed to open automatically. Please verify vacuum levels manually, open the valve, then click OK.` (after `gv_timeout_sec`, 15 s)
- `Gate Valve is not fully open. Please remedy this and click OK to re-evaluate.`
- `Safety Permissives Met. Turn the key to ENABLE, click OK, then immediately press START on the High Voltage Systems Control box.`

| Failure | What it means | What to do |
|---|---|---|
| `safety relay did not energise: physical PANEL START not pressed in time` | The relay didn't close within `timeout_sec` (6 s unless the step says otherwise) of the request; the routine withdraws the request. The PLC itself gives up after 5 s and latches `Safety_Relay_Refused`. | Press **RESET FAULTS**, then start the recipe again and press the panel start at once. See [The safety relay does not switch on](troubleshooting.md#the-safety-relay-does-not-switch-on). |

Once the relay is on, the routine waits `stabilization_sec` (15 s) before the step completes.

**hv_conditioning** ramps a setpoint in steps, holding while the pressure is high or after an arc.

| Failure | What it means |
|---|---|
| `no live readback <tag> to start the ramp from` | The readback it starts from has no live value. |
| `no live value for <tags> for 30 s` | The pressure or arc signal it watches had no live value for 30 s; it held the setpoint meanwhile. |
| `exceeded the maximum of <n> arcs` | More arcs than `max_arcs` (5 unless the step says otherwise). |
| `the supply is off (<tag> is 0): switch it on before conditioning it` | The supply's on status reads 0. |
| `the readback (<value>) didn't follow the setpoint (<setpoint>) within <tolerance>: is the supply on?` | At the end of a hold, the readback wasn't within the tolerance (one step unless the step says otherwise) of the setpoint. |

**psu_control** switches a supply on and sets it.

| Failure | What it means |
|---|---|
| `safety relay is not active (needed for <tag>)` | The relay was off when the step began. |
| `<tag> did not report enabled within 5 s of <tag>` | The supply didn't report itself on. |
| `<tag> did not reach <value> +/- <tolerance> within <t> s (last live value: <value>)` | The readback didn't reach the setpoint in time. |
| `<tag> = <value>: <result> - <reason>` | The supply refused a command, the setpoint among them (also without **Wait for the readback**). |

**mass_scan** runs a mass scan inside a recipe: see [Mass scan messages](#mass-scan-messages). It adds `the analyser magnet is off (or its state isn't known): switch it on first` and `the Faraday cup isn't in (or its position isn't known): insert it first` (it checks both before it scans), `mass scan settings: <error>` for settings the scan refuses, and `could not set the magnet on the peak (<I> A): <reason>` when it can't put the magnet on the strongest peak found.

## Mass scan messages

A refused start shows as `Status: start refused - <reason>` in the **Mass Scan** panel.

| Message | What it means | What to do |
|---|---|---|
| `start and stop currents are the same` | | Change one of them. |
| `the step must be more than 0 A` | | |
| `start_a <I> A is outside the magnet's range [<min>, <max>] A` (or `stop_a`) | Outside the magnet setpoint's registry limits. | |
| `Hard cap: start_a <I> A is above the 95 A cap - the magnet PSU's current cap. ...` (or `stop_a`) | Past the magnet's hard cap. | |
| `<n> points is more than the 100000 allowed - use a bigger step` | | Use a bigger step. |
| `the magnet tolerance must be more than 0 A`, `settle time and sample interval can't be negative`, `at least one sample per point is needed`, `NPLC <n> is outside [0.001, 100]` | | Correct the setting. |
| `no min/max limits for ion_beam.beamline.magnet.sp_requested_current in the tag registry` | | Ask an engineer to rebuild the registry. |
| `mass scan settings must be a JSON object`, `unknown mass scan setting(s): <names>`, `missing or bad mass scan setting: <error>`, `samples must be a whole number, not <value>`, `mass scan settings must be finite numbers` | Settings sent by a script or recipe that the scan can't use. | Correct them. |
| `mass scan is already running - stop it first`, `the sequencer is starting up - try again in a moment` | See [Sequencer service](#sequencer-service). | |

A scan that stops early ends with one of these on the status line. A failed scan also raises the alarm `mass scan failed: <message>`.

| Message | What it means | What to do |
|---|---|---|
| `SMU did not accept NPLC <n>: <reason>` | The SMU refused the integration time; nothing was scanned. | See the SMU's reason. |
| `magnet refused <I> A: <reason>` | A magnet setpoint was refused part-way. | See the magnet's reason. |
| `magnet or SMU telemetry is not live` | At a point, the magnet readback or the SMU's range flag still had no live value after 5 s. | Check the magnet and SMU services. |
| `magnet or Faraday-cup readings stopped updating` | No live magnet current, beam current or SMU range flag for 5 s while sampling. | As above. |
| `Stopped by operator` | **ABORT** was pressed. | |
| `Stopped: <subsystem> - <reason>` | The scan lost its control lease, for example `Stopped: magnet - control was taken by the operator`. | See the note below. |
| `Stopped: the sequencer is shutting down` | `service_sequencer` was stopped or restarted. | |
| `internal error: <error>` | | Report it. |
| `complete (<n> points)` | Finished. | |

While it runs, the status line shows `setting the SMU to NPLC <n>`, `scanning` (`scanning (pass <n>)` when looping), `back to the start for the next pass` and, at the end, `finishing: <message>` while the SMU's integration time is put back. A magnet that hasn't settled within the tolerance after 5 s is measured anyway.

After a scan, whatever ended it, the sequencer puts the SMU back to NPLC 1. If you took control of the SMU, it can't: the SMU stays at the scan's NPLC until the next scan ends.

The event `Mass scan pass <n> could not be saved: <error>` means **Auto-Export CSV on Complete** couldn't write that pass. A double-click on the plot that can't be turned into a current shows `Status: no jump - <error>`.

## Optimiser messages

A refused start shows as `Status: start refused - <reason>` in the **Optimiser** panel.

| Message | What it means | What to do |
|---|---|---|
| `X and Y are the same setpoint` | | Choose two different parameters. |
| `<tag> is not a setpoint the optimiser may sweep` | The registry doesn't mark the tag as a setpoint automation may drive. | Choose another parameter. |
| `no min/max limits for <tag> in the tag registry` | | Ask an engineer to rebuild the registry. |
| `X start and stop are the same` (or `Y`), `X start/stop must be finite numbers` | | Correct the range. |
| `X <value> is outside the allowed range [<min>, <max>] of <tag>` (or `Y`) | | |
| `Hard cap: X <tag> <value> <unit> is above the <cap> <unit> cap - ...` (or `Y`) | Past the parameter's hard cap. | |
| `X steps must be 2..500` (or `Y`), `dwell must be 0.05..60 s` | | |
| `no readback to optimise` | | |
| `no live value for <tag> (the optimiser puts it back afterwards) - is its service running?` | The optimiser reads each parameter's present value first, to put it back afterwards. | Check the parameter's service. |
| `optimiser settings must be a JSON object`, `unknown optimiser setting(s): <names>`, `missing or bad optimiser setting: <error>`, `x_steps must be a whole number, not <value>` | Settings from a script that the optimiser can't use. | Correct them. |

A double-click on the map during a sweep asks the optimiser to end the sweep at that point instead of putting the parameters back:

| Message | What it means |
|---|---|
| `stopping the sweep there` (OK) | It will end there. |
| `no sweep is running - set the values directly` | The sweep had already ended. |
| `(<x>, <y>) is outside the setpoints' allowed ranges`, `Hard cap: X <value> ...` | The point picked is out of range. |
| `goto needs {"x": number, "y": number}` | A malformed request: report it. |

A sweep that stops early ends with one of these. A failed sweep also raises the alarm `optimiser failed: <message>`.

| Message | What it means | What to do |
|---|---|---|
| `X setpoint refused: <reason>` (or `Y`) | A setpoint was refused part-way. | See the service's reason. |
| `readback <tag> is not live` | The beam readback had no live value at a point. A stale value would put a false flat area in the map, so the sweep stops. | Check the SMU service. |
| `Stopped by operator` | **ABORT** was pressed. | |
| `Stopped: going to X=<x>, Y=<y> instead of restoring` | You picked a point on the map. | |
| `Stopped: <subsystem> - <reason>` | The sweep lost its control lease, for example to **TAKE CONTROL**. | |
| `Stopped: the sequencer is shutting down` | | |
| `internal error: <error>` | | Report it. |
| `complete (<n> points)` | Finished. | |

While it runs the status line shows `2D raster sweep`, then `restoring the pre-scan values` or `going to X=<x>, Y=<y>`. If the optimiser can't put a parameter back (for example after you took control), `SystemLogs/service_sequencer.log` says `Optimiser could not set <tag> back: <reason>`.

## Alarms raised by the alarm service

`service_alarms` evaluates its rules twice a second (`config/alarm_config.json`). Priorities are critical (1), high (2), medium (3) and low (4); the horn sounds for priorities 1 to 3. A delay is how long the condition must hold before the alarm goes active. A value with no live data never changes an alarm: the loss of the data is its own alarm. See [Alarms](panels/alarms.md) for acknowledging and shelving.

| Alarm | Priority, delay | What it means | What to do |
|---|---|---|---|
| `PLC: <description>` | high for severity 1 bits, medium for severity 2; critical for `Safety_Relay_Tripped` and `PC_Watchdog_Timeout`; the `_Comms_Fail` bits after 10 s | One alarm per PLC fault bit, active while the bit is latched. The first PLC alarm since the last first-out reset is marked first-out (a star in the first column). | See [PLC faults](#plc-faults). |
| `No data from the PLC service - machine state unknown` | critical, 3 s | `service_plc` has stopped publishing. | Check `service_plc` in **Service Manager**. |
| `The PLC service has lost the PLC` | critical, 3 s | `service_plc` runs, but has no connection to the PLC. | See [PLC COMMS shows OFFLINE](troubleshooting.md#plc-comms-shows-offline). |
| `No data from the service manager` | high, 5 s | The service manager has stopped publishing. | Start it again (`python src/services/service_manager.py`). |
| `<service> is not running`, `<service> is crashed`, `<service> is hanging (no heartbeat)`, `<service> is waiting to be restarted` | high, 10 s | The manager reports the service OFFLINE, CRASHED, HANGING or BACKOFF. | See [A service shows BACKOFF](troubleshooting.md#a-service-shows-backoff). |
| `<service> was stopped by the operator` | low | STOPPED: listed, never sounded. | Start it again when wanted. |
| `<device>: communication lost` | medium, 5 s | A device's comms-fail flag. A gauge's alarm is listed but not sounded while its controller's alarm (`facilities graphix1` or `facilities graphix2`) is active. | Check the device and its link. |
| `<device>: pressure above its set point` | high | The gauge's relay is off. | Find why the pressure is up. |
| `<device>: pressure approaching its set point` | medium, 2 s | The pressure is above the relay's on point by up to 25 %. | Watch it. |
| `<device>: pressure rising rapidly` | high | Faster than `rapid_rise_thresh_mb_s`. | Look for the cause: a leak, or a burst of gas such as a new filament degassing. |
| `Gauge relay set points not confirmed against vac_gauges_config.json - see the event log (VG1/VG2 relays: gate valve held shut)` | high | The vacuum service couldn't confirm that every gauge relay holds the channel and set points in `config/vac_gauges_config.json`. For VG1 and VG2 it withholds the gate-valve permissive until they do. | Read the vacuum service's events in `SystemLogs/service_vac_gauge_controllers.log`: they name the relay and what it holds. Restarting the vacuum service writes the file's set points to the controller again. Do that with the source off: while the service restarts, the PLC loses the vacuum permissive and closes the gate valve, which opens the safety relay. |
| `<device>: arc (output collapsed) - latched until fault reset` | high | The Spellman service's arc detection: the output fell below 10 % of its setpoint, or the current rose above 500 µA, on 2 polls in a row (not judged below 0.5 kV). The PLC latches it too, as `Unit<n>_Arc_Exceeded`. | Find the cause, then press **RESET FAULTS**. |
| `<purpose> failed: <message>` | medium | The last recipe, mass scan or optimiser run failed. It clears when the next run starts. | See [Recipe messages](#recipe-messages), [Mass scan messages](#mass-scan-messages) or [Optimiser messages](#optimiser-messages). |
| `The alarm service is not running - alarms are NOT being monitored. Restart it from the Service Manager panel.` | critical; the HMI's own | No data from `service_alarms`. | Start `service_alarms` in **Service Manager**. |

`<device>` is the tag's place in words: `source vacuum gauge 1` to `endstation vacuum gauge 6`, `facilities graphix1`, `facilities graphix2`, `source turbo pump`, `source einzel`, `beamline magnet`, `beamline steerer magnet`, `beamline faraday smu`. `<service>` is a service's name, such as `service_plc`.

## PLC faults

The PLC latches each fault until a fault reset, in six 32-bit fault words. Each latched fault shows:

- in the **PLC faults** list of **System Health**, as `<NAME>: <description>`;
- as the alarm `PLC: <description>`;
- as FAULT events, `FAULT ACTIVATED: <description>` and `FAULT CLEARED: <description>`.

Acknowledging the alarm doesn't reset the fault. Only **RESET FAULTS** does. The words, bits and descriptions are defined in `config/fault_config.json`.

> [!NOTE]
> What the PLC does about each fault comes from a reading of the PLC program (an export of it), not from this repository. Check against the program in the PLC if it has changed. See [The PLC program](how-it-works/plc.md).

### What the PLC does about a fault

- **Words 0, 1, 2, 4 and 5** put the machine in state 99 (Fault) and light the amber beacon. The PLC also sets its Faraday cup request (`Cmd_Insert_FC`) off; which way the cup then moves hasn't been checked on the machine (see [The PLC program](how-it-works/plc.md)).
- **Word 3 (Spellman)** is latched only: it sets neither state 99 nor the amber beacon.
- A **hardware abort** (a sustained arc, `Filament_OverCurrent`, `Src_Body_Temp_High`, `Src_Coolant_Flow_Low`, `Cesium_Oven_OverTemp`) starts the PLC's shutdown at step 2, **SHUTDOWN SEQ: CESIUM COOLDOWN**: extraction off, the gate valve closed (which opens the safety relay), forced cooling. From there it goes to **FINAL KILL** once the caesium isn't hot and the filament is at 0 A, then to **LATCHED SAFE (RESET REQ)**.
- **RESET FAULTS** is accepted when the remote I/O sequence is at step 0, 3 or 99, the PC sequence is at step 0, 2, 3 or 99, and the PC watchdog isn't lost. It ends the shutdown sequences and clears all six words. A fault whose cause is still there latches again.

> [!WARNING]
> A fault reset is accepted during **SHUTDOWN SEQ: CESIUM COOLDOWN** and ends the cool-down there and then, with the caesium possibly still hot. Let the cool-down reach **FINAL KILL** or **LATCHED SAFE (RESET REQ)** before you press **RESET FAULTS**.

### Fault word 0 System

`ion_beam.faults.word_0_system`. Alarm priority high unless given.

| Fault | Description | When the PLC sets it, and what it does | What to do |
|---|---|---|---|
| `SAFETY_RELAY_REFUSED` (bit 0.0) | `Safety relay interlocks not met.` | The relay enable was on for 5 s and the relay didn't close: the panel start wasn't pressed in time, or the relay permissive is missing. The PLC drops the enable. | See [The safety relay does not switch on](troubleshooting.md#the-safety-relay-does-not-switch-on). Press **RESET FAULTS** before trying again. |
| `SAFETY_RELAY_TRIPPED` (bit 0.1) | `Hardware safety loop broken. HV disabled.` | The relay opened without the PC dropping it on purpose, or while the relay permissive was false (this includes the gate valve closing at shutdown step 2). With the relay open the PLC zeroes the requested setpoints. Alarm critical. | See [The safety relay dropped out](troubleshooting.md#the-safety-relay-dropped-out). |
| `AIR_PRESSURE_LOW` (bit 0.2) | `Pneumatic air supply below minimum operating threshold.` | The compressed-air switch opened (at once). The relay permissive is lost while it is open; the gate valve and Faraday cup need air to move. | Restore the compressed air, then press **RESET FAULTS**. |
| `SRC_COOLANT_FLOW_LOW` (bit 0.3) | `Source coolant flow below minimum.` | The source coolant flow switch was open for 2 s. The relay and source supply permissives drop at once with the switch; after 2 s the PLC switches extraction, target and filament off, switches auto-emission off and starts a hardware abort. | Restore the coolant flow. Let the shutdown run, then press **RESET FAULTS**. |
| `MAG_COOLANT_FLOW_LOW` (bit 0.4) | `Magnet coolant flow below minimum.` | The magnet coolant switch opened (at once). The relay permissive is lost while it is open. **Beamline Optics** shows **MAG COOLING: FAULT**. | Restore the magnet cooling, then press **RESET FAULTS**. |
| `GATEVALVE_TIMEOUT` (bit 1.0) | `Gate valve failed to actuate.` | The valve didn't reach the requested position within 5 s. **OPEN** and **CLOSE** read **FAULT LOCKOUT**. | Check the compressed air, the valve and its limit switches, then press **RESET FAULTS**. |
| `GATEVALVE_CONFLICT` (bit 1.1) | `Gate valve reads as both open and closed.` | Both limit switches are made. The valve shows **SENSOR CONFLICT** and the buttons **FAULT LOCKOUT**. | Check the limit switches, then press **RESET FAULTS**. |
| `PC_WATCHDOG_TIMEOUT` (bit 2.0) | `Python control software stopped sending 1Hz heartbeat.` | The watchdog the PLC service writes every 0.5 s didn't change for 3 s. The gate-valve open permissive is lost, so the PLC closes the gate valve, which opens the relay, and it starts its comms-loss shutdown (**GRACE PERIOD (75% EXTR)** for up to 60 s, then **CESIUM COOLDOWN**). Alarm critical. | See [PLC COMMS shows OFFLINE](troubleshooting.md#plc-comms-shows-offline). Once the PC is back, press **RESET FAULTS**, but not during **CESIUM COOLDOWN** (see the warning above). |
| `REMOTE_IO_LOST` (bit 2.1) | `Profinet connection to remote IO rack severed.` | The PLC lost the remote I/O on the source deck (counted only once the relay has been closed for 25 s). The target, filament and thermionic contactors drop at once. The PLC holds extraction at 75 % for up to 60 s, switches auto-emission off and cools a hot oven; if the link doesn't return it switches extraction off and closes the gate valve, which opens the relay. | Once the link is back, press **RESET FAULTS**. |

### Fault word 1 Pumps

`ion_beam.faults.word_1_pumps`. The PLC latches these from flags the PLC service writes from the turbo service's data.

| Fault | Description | When the PLC sets it | What to do |
|---|---|---|---|
| `SRC_TURBO_TRIP` (bit 0.0) | `Source turbopump stopped unexpectedly.` | The pump reports an error and isn't ready (`ion_beam.source.turbo_pump.stat_trip`). The turbo shows **TRIPPED**. | **RESET & START** in **Vacuum System**, then **RESET FAULTS**. |
| `SRC_TURBO_COMMS_FAIL` (bit 0.1) | `Source turbopump communication failure.` | The turbo service can't reach the pump, or the turbo service has been silent for 2 s. Alarm after 10 s. | Check `service_source_turbo` and the pump's bridge, then **RESET FAULTS**. |
| `SRC_TURBO_ERROR_ACTIVE` (bit 0.2) | `Source turbopump reports an error.` | The pump's status word shows an error. | Check the pump controller, then **RESET FAULTS**. |
| `SRC_TURBO_WARNING_ACTIVE` (bit 0.3) | `Source turbopump reports a warning.` | The pump's status word shows a warning. Severity 2: alarm medium. | As above. |
| `LDLK_TURBO_TRIP` (bit 1.0) | `Loadlock turbopump stopped unexpectedly.` | No service talks to the loadlock turbo, and the PLC service never writes its flags. | |
| `LDLK_TURBO_COMMS_FAIL` (bit 1.1) | `Loadlock turbopump communication failure.` | As above. Alarm after 10 s. | |
| `LDLK_TURBO_ERROR_ACTIVE` (bit 1.2) | `Loadlock turbopump reports an error.` | As above. | |
| `LDLK_TURBO_WARNING_ACTIVE` (bit 1.3) | `Loadlock turbopump reports a warning.` | As above. Severity 2. | |

### Fault word 2 Source

`ion_beam.faults.word_2_source`. The PLC detects these itself. Alarm priority high, or medium for the `_Fail` bits of the filament, extraction, target and thermionic (severity 2).

| Fault | Description | When the PLC sets it, and what it does | What to do |
|---|---|---|---|
| `SRC_BODY_TEMP_HIGH` (bit 0.0) | `Ion source body temperature exceeded safe operating limit.` | Source body above 40 °C for 200 ms, relay closed. Extraction, target and filament off; hardware abort. | Find why the source body is hot; let the shutdown run; **RESET FAULTS**. |
| `CESIUM_OVEN_OVERTEMP` (bit 0.1) | `Cesium oven temperature runaway detected.` | Oven above 200 °C for 200 ms. Relay permissive lost; heating locked out; cooling; hardware abort. | Let it cool; find why it overheated; **RESET FAULTS**. |
| `CESIUM_OVEN_HEATFAIL` (bit 0.2) | `Cesium oven did not heat up quick enough.` | Heater on with the error above 500 °C for 600 s. Heating locked out; oven mode 0, setpoint 20 °C. As programmed it can't occur, because the setpoint is limited to 200 °C. | **RESET FAULTS**. |
| `CESIUM_OVEN_COOLFAIL` (bit 0.3) | `Cesium oven did not cool down quick enough.` | Cooler on, oven above 30 °C, error below −5 °C for 40,000 s. As `HeatFail`, plus cooling. | Check the cooling air, then **RESET FAULTS**. |
| `FILAMENT_OVERCURRENT` (bit 0.4) | `Filament power supply current exceeded max limit.` | Filament above 38 A. Contactor off; hardware abort. | **RESET FAULTS** after the shutdown. |
| `FILAMENT_OVERVOLTAGE` (bit 0.5) | `Filament power supply voltage exceeded max limit.` | Filament above 10 V. | Check the filament, then **RESET FAULTS**. |
| `FILAMENT_FAIL` (bit 0.6) | `Filament failed to track setpoint.` | Readback more than 0.5 A from the setpoint for 1.5 s (10 s under auto-emission), after a 3 s grace. Contactor off. | Check the filament and its supply, then **RESET FAULTS**. |
| `EXTRACTION_OVERCURRENT` (bit 1.0) | `Extraction power supply current exceeded max limit.` | Above 10 mA. Extraction off. As programmed it can't occur: the readback's scale ends at 5 mA. | **RESET FAULTS**. |
| `EXTRACTION_OVERVOLTAGE` (bit 1.1) | `Extraction power supply voltage exceeded max limit.` | Above 30 kV. Extraction off. As programmed it can't occur: the readback's scale ends below it. | **RESET FAULTS**. |
| `EXTRACTION_FAIL` (bit 1.2) | `Extraction failed to track setpoint.` | Error above 0.6 kV for 1 s, not while arcing. Extraction off. | Check the supply and the load, then **RESET FAULTS**. |
| `EXTRACTION_SUSTAINED_ARC` (bit 1.3) | `Extraction power supply sustained arc.` | 3 arcs, the count falling by 1 every 10 s. Hardware abort. As programmed the extraction arc check can't trigger. | **RESET FAULTS** after the shutdown. |
| `TARGET_OVERCURRENT` (bit 2.0) | `Target power supply current exceeded max limit.` | Above 15 mA. Target off. | Check the source, then **RESET FAULTS**. |
| `TARGET_OVERVOLTAGE` (bit 2.1) | `Target power supply voltage exceeded max limit.` | Above 10 kV. Target off. | As above. |
| `TARGET_FAIL` (bit 2.2) | `Target failed to track setpoint.` | Error above 0.5 kV for 2 s. Contactor off. | Check the supply and the load, then **RESET FAULTS**. |
| `TARGET_SUSTAINED_ARC` (bit 2.3) | `Target power supply sustained arc.` | 3 arcs, the count falling by 1 every 10 s. Hardware abort. | Find the cause of the arcing; **RESET FAULTS** after the shutdown. |
| `THERMIONIC_OVERCURRENT` (bit 3.0) | `Thermionic power supply current exceeded max limit.` | Above 200 mA. Auto-emission off. | Check the emission, then **RESET FAULTS**. |
| `THERMIONIC_OVERVOLTAGE` (bit 3.1) | `Thermionic power supply voltage exceeded max limit.` | Above 2600 V. Auto-emission off. | As above. |
| `THERMIONIC_FAIL` (bit 3.2) | `Failed to produce thermionic current.` | Below 10 mA for 5 s while above 200 V with the filament above 36 A. Contactor off. | Check the filament and the thermionic supply, then **RESET FAULTS**. |
| `THERMIONIC_SUSTAINED_ARC` (bit 3.3) | `Thermionic power supply sustained arc.` | 3 arcs, the count falling by 1 every 10 s. Hardware abort. | Find the cause; **RESET FAULTS** after the shutdown. |

### Fault word 3 Spellman

`ion_beam.faults.word_3_spellman`. Latched only: no machine state 99, no amber beacon. Unit 1 is the source einzel (`plc_unit` 1 in `config/mpd_config.json`), the only Spellman supply configured. The PLC service writes, for each supply, its comms-fail, over-current, over-voltage, fail and arc flags; it has no flag for over-temperature or supply rail.

| Fault | Description | When it is set | What to do |
|---|---|---|---|
| `UNIT1_COMMS_FAIL` (bit 0.0) | `Spellman MPD Unit 1 communication failure.` | The supply didn't answer, or its bus isn't connected, or the Spellman service has been silent for 2 s. A supply silent because the relay is off doesn't count. Alarm after 10 s. | Check the supply and its bridge, then **RESET FAULTS**. |
| `UNIT1_OVERCURRENT` (bit 0.1) | `Spellman MPD Unit 1 current exceeded limit.` | The supply's status reports over-current. | See [The einzel current is high](troubleshooting.md#the-einzel-current-is-high), then **RESET FAULTS**. |
| `UNIT1_OVERVOLTAGE` (bit 0.2) | `Spellman MPD Unit 1 voltage exceeded limit.` | The supply's status reports over-voltage. | As above. |
| `UNIT1_FAIL` (bit 0.3) | `Spellman MPD Unit 1 failed to track setpoint.` | The supply's status reports over-temperature or a collapsed supply rail (the service's `stat_fail`), whatever the description says. | Check the supply, then **RESET FAULTS**. |
| `UNIT1_OVERTEMP` (bit 0.4) | `Spellman MPD Unit 1 over temperature fault.` | No flag from the PC feeds this bit. | |
| `UNIT1_SUPPLY_RAIL` (bit 0.5) | `Spellman MPD Unit 1 24V supply rail out of range.` | No flag from the PC feeds this bit. | |
| `UNIT1_ARC_EXCEEDED` (bit 0.6) | `Spellman MPD Unit 1 has exceeded arc limit.` | The Spellman service's arc detection latched (alarm `source einzel: arc (output collapsed) - latched until fault reset`). | Find the cause, then **RESET FAULTS**. |
| `UNIT2_COMMS_FAIL` (bit 0.7) | `Spellman MPD Unit 2 communication failure.` | As for Unit 1. | |
| `UNIT2_OVERCURRENT` (bit 1.0) | `Spellman MPD Unit 2 current exceeded limit.` | As for Unit 1. | |
| `UNIT2_OVERVOLTAGE` (bit 1.1) | `Spellman MPD Unit 2 voltage exceeded limit.` | As for Unit 1. | |
| `UNIT2_FAIL` (bit 1.2) | `Spellman MPD Unit 2 failed to track setpoint.` | As for Unit 1. | |
| `UNIT2_OVERTEMP` (bit 1.3) | `Spellman MPD Unit 2 over temperature fault.` | No flag from the PC. | |
| `UNIT2_SUPPLY_RAIL` (bit 1.4) | `Spellman MPD Unit 2 24V supply rail out of range.` | No flag from the PC. | |
| `UNIT2_ARC_EXCEEDED` (bit 1.5) | `Spellman MPD Unit 2 has exceeded arc limit.` | As for Unit 1. | |
| `UNIT3_COMMS_FAIL` (bit 1.6) | `Spellman MPD Unit 3 communication failure.` | As for Unit 1. | |
| `UNIT3_OVERCURRENT` (bit 1.7) | `Spellman MPD Unit 3 current exceeded limit.` | As for Unit 1. | |
| `UNIT3_OVERVOLTAGE` (bit 2.0) | `Spellman MPD Unit 3 voltage exceeded limit.` | As for Unit 1. | |
| `UNIT3_FAIL` (bit 2.1) | `Spellman MPD Unit 3 failed to track setpoint.` | As for Unit 1. | |
| `UNIT3_OVERTEMP` (bit 2.2) | `Spellman MPD Unit 3 over temperature fault.` | No flag from the PC. | |
| `UNIT3_SUPPLY_RAIL` (bit 2.3) | `Spellman MPD Unit 3 24V supply rail out of range.` | No flag from the PC. | |
| `UNIT3_ARC_EXCEEDED` (bit 2.4) | `Spellman MPD Unit 3 has exceeded arc limit.` | As for Unit 1. | |
| `UNIT4_COMMS_FAIL` (bit 2.5) | `Spellman MPD Unit 4 communication failure.` | As for Unit 1. | |
| `UNIT4_OVERCURRENT` (bit 2.6) | `Spellman MPD Unit 4 current exceeded limit.` | As for Unit 1. | |
| `UNIT4_OVERVOLTAGE` (bit 2.7) | `Spellman MPD Unit 4 voltage exceeded limit.` | As for Unit 1. | |
| `UNIT4_FAIL` (bit 3.0) | `Spellman MPD Unit 4 failed to track setpoint.` | As for Unit 1. | |
| `UNIT4_OVERTEMP` (bit 3.1) | `Spellman MPD Unit 4 over temperature fault.` | No flag from the PC. | |
| `UNIT4_SUPPLY_RAIL` (bit 3.2) | `Spellman MPD Unit 4 24V supply rail out of range.` | No flag from the PC. | |
| `UNIT4_ARC_EXCEEDED` (bit 3.3) | `Spellman MPD Unit 4 has exceeded arc limit.` | As for Unit 1. | |

> [!NOTE]
> Units 2 to 4 are the beamline einzel and the two neutral traps, which aren't installed yet: none of them is in `config/mpd_config.json`.

### Fault word 4 Magnet

`ion_beam.faults.word_4_magnet`. The PLC latches these from flags the PLC service writes from the analyser magnet service's data.

| Fault | Description | When it is set | What to do |
|---|---|---|---|
| `MAGNET_COMMS_FAIL` (bit 0.0) | `Magnet PSU communication failure.` | The magnet service can't reach the supply, or has been silent for 2 s. Alarm after 10 s. | Check the supply and `service_magnet_psu`, then **RESET FAULTS**. |
| `MAGNET_SHORT_CIRCUIT` (bit 0.1) | `Magnet PSU appears short-circuited.` | The coil's resistance (voltage over current) is below `short_circuit_threshold` in `config/magnet_config.json`. Judged only with the output on, above `min_current_for_calc` and 1.5 s after a setpoint change. | Check the coil and its cables, then **RESET FAULTS**. |
| `MAGNET_OPEN_CIRCUIT` (bit 0.2) | `Magnet PSU appears open-circuited.` | The resistance is above `open_circuit_threshold`. | As above. |
| `MAGNET_UNEXPECTED_RES` (bit 0.3) | `Magnet has unexpected resistance value.` | The resistance differs from `nominal_resistance` by more than `resistance_tolerance`. | As above. |
| `MAGNET_PSU_OVERTEMP` (bit 0.4) | `Magnet PSU Internal Overtemperature.` | The supply reported over-temperature. The service clears the supply's own alarm straight away; the PLC fault stays latched. | Check the supply's cooling, then **RESET FAULTS**. |
| `MAGNET_PSU_POWERFAIL` (bit 0.5) | `Magnet PSU AC Mains Power Fail.` | The supply reported a mains failure. As above. | Check its mains supply, then **RESET FAULTS**. |
| `MAGNET_PSU_OVP` (bit 0.6) | `Magnet PSU Hardware Overvoltage Trip.` | The supply's over-voltage protection tripped. As above. | Check the coil, then **RESET FAULTS**. |
| `MAGNET_PSU_OCP` (bit 0.7) | `Magnet PSU Hardware Overcurrent Trip.` | The supply's over-current protection tripped. As above. | As above. |
| `STR_MAGNET_COMMS_FAIL` (bit 1.0) | `Steerer Magnet PSU communication failure.` | The PLC service doesn't write the X-steerer's flags, so the bits of the steerer don't latch. | |
| `STR_MAGNET_SHORT_CIRCUIT` (bit 1.1) | `Steerer Magnet PSU appears short-circuited.` | As above. | |
| `STR_MAGNET_OPEN_CIRCUIT` (bit 1.2) | `Steerer Magnet PSU appears open-circuited.` | As above. | |
| `STR_MAGNET_UNEXPECTED_RES` (bit 1.3) | `Steerer Magnet has unexpected resistance value.` | As above. | |
| `STR_MAGNET_PSU_OVERTEMP` (bit 1.4) | `Steerer Magnet PSU Internal Overtemperature.` | As above. | |
| `STR_MAGNET_PSU_POWERFAIL` (bit 1.5) | `Steerer Magnet PSU AC Mains Power Fail.` | As above. | |
| `STR_MAGNET_PSU_OVP` (bit 1.6) | `Steerer Magnet PSU Hardware Overvoltage Trip.` | As above. | |
| `STR_MAGNET_PSU_OCP` (bit 1.7) | `Steerer Magnet PSU Hardware Overcurrent Trip.` | As above. | |

### Fault word 5 Gauges

`ion_beam.faults.word_5_gauges`. The PLC latches these from flags the PLC service writes from the vacuum service's data. The GRAPHIX comms faults and the VG1 and VG2 faults also hold the gate valve shut until **RESET FAULTS**, even after the vacuum has recovered.

| Fault | Description | When it is set | What to do |
|---|---|---|---|
| `GRAPHIX_1_COMMS_FAIL` (bit 0.0) | `Graphix Unit 1 communication failure.` | The vacuum service's link to the gauge controllers is down, or the vacuum service has been silent for 2 s. Alarm after 10 s. | Check the controllers, their bridge and `service_vac_gauge_controllers`, then **RESET FAULTS**. |
| `GRAPHIX_2_COMMS_FAIL` (bit 0.1) | `Graphix Unit 2 communication failure.` | As above. Alarm after 10 s. | As above. |
| `VG1_NOT_FOUND` (bit 0.2) | `VG1 not found by controller.` | The controller reports no gauge on VG1's channel (status 5). | Check the gauge head and cable, then **RESET FAULTS**. |
| `VG1_TYPE_MISMATCH` (bit 0.3) | `VG1 has unexpected gauge type.` | The controller reports a status other than OK or not found for VG1, including a gauge switched off (**SENSOR OFF**). | Check the gauge at the controller, then **RESET FAULTS**. |
| `VG1_ABOVE_SP_WARN` (bit 0.4) | `VG1 pressure above setpoint.` | VG1's relay is off: its pressure is above the relay's set point. | Find why the pressure is up; once it is down, **RESET FAULTS**. |
| `VG1_RAPID_RISE_WARN` (bit 0.5) | `VG1 rapid pressure rise.` | VG1's pressure rose faster than `rapid_rise_thresh_mb_s` over more than 0.5 s, above 1×10⁻⁶ mbar. | As above. |
| `VG2_NOT_FOUND` (bit 0.6) | `VG2 not found by controller.` | As for VG1. | As for VG1. |
| `VG2_TYPE_MISMATCH` (bit 0.7) | `VG2 has unexpected gauge type.` | As for VG1. | As for VG1. |
| `VG2_ABOVE_SP_WARN` (bit 1.0) | `VG2 pressure above setpoint.` | As for VG1. | As for VG1. |
| `VG2_RAPID_RISE_WARN` (bit 1.1) | `VG2 rapid pressure rise.` | As for VG1. | As for VG1. |
| `VG3_NOT_FOUND` (bit 1.2) | `VG3 not found by controller.` | As for VG1. | As for VG1. |
| `VG3_TYPE_MISMATCH` (bit 1.3) | `VG3 has unexpected gauge type.` | As for VG1. | As for VG1. |
| `VG3_ABOVE_SP_WARN` (bit 1.4) | `VG3 pressure above setpoint.` | As for VG1. | As for VG1. |
| `VG3_RAPID_RISE_WARN` (bit 1.5) | `VG3 rapid pressure rise.` | As for VG1. | As for VG1. |
| `VG4_NOT_FOUND` (bit 1.6) | `VG4 not found by controller.` | As for VG1. | As for VG1. |
| `VG4_TYPE_MISMATCH` (bit 1.7) | `VG4 has unexpected gauge type.` | As for VG1. | As for VG1. |
| `VG4_ABOVE_SP_WARN` (bit 2.0) | `VG4 pressure above setpoint.` | As for VG1. | As for VG1. |
| `VG4_RAPID_RISE_WARN` (bit 2.1) | `VG4 rapid pressure rise.` | The vacuum service never sets the rapid-rise flag for VG4. | |
| `VG5_NOT_FOUND` (bit 2.2) | `VG5 not found by controller.` | As for VG1. | As for VG1. |
| `VG5_TYPE_MISMATCH` (bit 2.3) | `VG5 has unexpected gauge type.` | As for VG1. | As for VG1. |
| `VG5_ABOVE_SP_WARN` (bit 2.4) | `VG5 pressure above setpoint.` | As for VG1. | As for VG1. |
| `VG5_RAPID_RISE_WARN` (bit 2.5) | `VG5 rapid pressure rise.` | As for VG1. | As for VG1. |
| `VG6_NOT_FOUND` (bit 2.6) | `VG1 not found by controller.` | VG6, not VG1: the description in `config/fault_config.json` names the wrong gauge. Otherwise as for VG1. | Check VG6, then **RESET FAULTS**. |
| `VG6_TYPE_MISMATCH` (bit 2.7) | `VG6 has unexpected gauge type.` | As for VG1. | As for VG1. |
| `VG6_ABOVE_SP_WARN` (bit 3.0) | `VG6 pressure above setpoint.` | As for VG1. | As for VG1. |
| `VG6_RAPID_RISE_WARN` (bit 3.1) | `VG6 rapid pressure rise.` | As for VG1. | As for VG1. |

## Events that need attention

The services also write events to the event log (`data/events.db`, and `SystemLogs/<service>.log`). These are the ones that need you to do something, or explain something you see. A message repeated often is written at most once a minute or so, ending ` (+<n> similar suppressed)`.

| Event | From | What it means | What to do |
|---|---|---|---|
| `PLC connection lost: <reason>` | `service_plc` | A read or write failed. The service publishes every PLC value as unknown, and connects again after 1, 2, 5 and then every 10 s. | See [PLC COMMS shows OFFLINE](troubleshooting.md#plc-comms-shows-offline). |
| `PLC connection to <address> failed: <error>`, `Connected to S7-1200 at <address>` | `service_plc` | A connection attempt failed; the connection is made. | As above. |
| `Gate valve closed by its interlock (vacuum permissive gone): its OPEN request is cleared, so it stays shut. Press OPEN again once the vacuum is OK.` | `service_plc` | The PLC closed the gate valve when the vacuum permissive went, and the valve has stayed shut for 3 s. | Press **OPEN** once the vacuum is good again. |
| `Vacuum service not sending data (restarting?): the PLC keeps its last judgment of the vacuum (gate valve permissive given; GRAPHIX and gauge flags as they were) for up to <n> s more, until the vacuum service judges it again` (or `still starting`, `withheld`) | `service_plc` | The vacuum service is restarting: the PLC is given its last judgment meanwhile, for at most 60 s after it. | Let it finish starting. See [When the vacuum service restarts](panels/vacuum.md#when-the-vacuum-service-restarts). |
| `Vacuum service judging the vacuum again: the PLC follows its readings (gate valve permissive given)` (or `withheld`) | `service_plc` | The restarted vacuum service has judged the vacuum, and the hold has ended. | |
| `No judgment of the vacuum from the vacuum service for 60 s: the PLC is now told the vacuum is unknown (gate valve permissive withdrawn)` | `service_plc` | The vacuum service didn't come back in time: an open gate valve closes and the safety relay drops. | Find why the vacuum service isn't running or starting: see [A service shows BACKOFF](troubleshooting.md#a-service-shows-backoff). |
| `Vacuum judged: the gate-valve permissive is given on this run's own readings` (or `withheld`) | `service_vac_gauge_controllers` | The vacuum service, after starting, has judged the vacuum for itself. | |
| `<device> at <address> not reachable: <error>` | device services | The service can't connect to its device. It keeps trying. | Check the device. |
| `Safety relay DE-ENERGISED (or its state unknown) - Spellman supplies unpowered, outputs reported disabled`, `No PLC telemetry - safety relay state unknown, treating as de-energised`, `Safety relay ENERGISED - Spellman supplies powered` | `service_spellman_mpd` | The relay as the Spellman service sees it. | |
| `ARC DETECTED on <supply>: output collapsed to <v> kV, below 10% of the <sp> kV setpoint - latched until fault reset` (or `output current <i> uA above 500 uA`) | `service_spellman_mpd` | The software arc detection latched. | Find the cause, then **RESET FAULTS**. |
| `Magnet output forced off: <why>` (`Steerer` for the steerer), `Magnet safety permissive restored (output stays off until enabled)` | magnet services | The relay went off and the output was forced off; the relay is back. | Switch the output on again when wanted. |
| `Magnet PSU didn't report its nominal ratings: until it does, only 0 A and output off are accepted and its V/I readbacks are unknown (<when it asks again>)` | magnet services | As the refusal of the same name. If it says the supply didn't answer, the ratings are asked for again only when the service reconnects. | Restart the service in **Service Manager**. |
| `Magnet PSU alarm: <alarms> (auto-clearing)` | magnet services | The supply reported OT (over-temperature), PF (power fail), OVP or OCP; the service clears it at the supply. | See [Fault word 4 Magnet](#fault-word-4-magnet). |
| `Degauss aborted - <reason>` | magnet services | The degauss sequence stopped part-way. | Check the reason; run it again. |
| `SMU bias forced to 0 V: safety relay off or no PLC telemetry`, `SMU reconnected - previous bias of <v> V NOT restored (safety relay not confirmed on); bias reset to 0 V, output off` | `service_smu` | The bias was taken to 0 V and forgotten. | Set the bias again when the relay is on. |
| `Node <node> relay <relay> (<gauge>): set points differ from vac_gauges_config.json: <differences>` (or `set points could not be read back`), ending ` - gate-valve permissive withheld until they match` for the relays of VG1 and VG2 | `service_vac_gauge_controllers` | A gauge relay holds other values than the configuration, each given as `<kind> <value> (should be <value>)`. For VG1 and VG2 the gate valve is held shut. | Restart the vacuum service to write the configuration again, or correct the controller. |
| `Gate-valve permissive withheld: it judges VG1 and VG2 by their relays, and <problems>` | `service_vac_gauge_controllers` | `config/vac_gauges_config.json` gives VG1 or VG2 no usable relay. | Ask an engineer to correct the file. |
| `Gauge controller node <node> doesn't answer: its names and relays aren't checked until the service reconnects (...)` | `service_vac_gauge_controllers` | One GRAPHIX controller was silent at start-up. | Check that controller. |
| `<service>: process exited with code <code> (details in SystemLogs/<service>.log). Restart attempt <n> in <t> s.` | service manager | The service crashed. | Read its log. See [A service shows BACKOFF](troubleshooting.md#a-service-shows-backoff). |
| `<service>: no heartbeat within <t> s of start. Restart attempt <n> in <t> s.`, `<service>: heartbeat stopped for more than <t> s (hung). Restart attempt <n> in <t> s.` | service manager | The service didn't start, or hung, and was killed. | As above. |
| `CRITICAL: Another service manager is already running. Aborting.` | service manager | A second manager for the same system was started. | Use the one already running. |
| `Network Timeout: <topic> silent for >1s. Forcing NaNs.` (WATCHDOG_TRIP), `Network Recovery: <topic> data stream restored.` (WATCHDOG_CLEAR) | `service_logger` | A service stopped publishing; its channels are logged as gaps until it is back. | See [Gaps in the logged data](troubleshooting.md#gaps-in-the-logged-data). |
| `[Logger I/O Error] Cannot write <chunk>: <error> (<n> GB free on the data drive) - <m> min of data waiting in RAM, tried again every 60 s; beyond 2 h of it the oldest is dropped` | `service_logger` | The disk won't take the logged data. | See [The disk is filling up](troubleshooting.md#the-disk-is-filling-up). |
| `Dropped unwritable chunk <chunk>: the disk hasn't taken data for over 2 h` | `service_logger` | Logged data was lost. | As above. |
| `The data drive is low on space: <n> GB free for <folder> - the logger can't save data once it is full` | `service_logger` | Less than 2 GB free. Repeated at most hourly. | As above. |
| `Logger is writing to disk again: <n> waiting chunk(s) saved`, `Recovered <n> logged rows from the crash journal` | `service_logger` | The waiting data has been saved; rows held in the crash journal after a crash were saved. | |
| `Readback out of tolerance (<rule>): readback <rb> vs setpoint <sp> (deviation <d>, allowed +/-<tol>, for <t> s)` (TOLERANCE) | `service_monitor` | A readback has been away from its setpoint for longer than its rule allows. Information, not a fault: the readout gets an amber outline. | Check the supply. The rules are in `config/readback_tolerance.json`. |
| `Tag registry not loaded - setpoints will be refused: <error>`, `WARNING: <file> not loaded (<error>). Operating blind.` | device services, `service_plc` | The tag registry couldn't be read. | Ask an engineer to rebuild it, then restart the services. |
| `<file> not loaded (<error>) - using built-in defaults` | device services | A configuration file couldn't be read. | Ask an engineer to correct it. |

## Config editor messages

The config editor opens from **Config** in the menu bar, with the engineering password. See [Configuration files and the config editor](engineering/configuration.md).

| Title and text | What it means | What to do |
|---|---|---|
| **Access Denied**: `Incorrect password.` | | |
| **Refused: Hard Cap**: `<file> was not changed: a value is over a hard cap (marked red - hover over it for the reason).` | A value is past a hard cap. No tick gets past it. | Change the value marked red. |
| **Not Applied**: `Check your inputs: <field>: '<text>' is not a number` | | Correct the field. |
| **Not Applied**: `<file> would have problems the services trip over:` and the problems | The checks (types, ranges, limits the right way round, and so on) failed. | Correct them. |
| **Not Applied**: `<file> was not changed: <file> was changed on disk since it was opened here (edited by hand, or a git pull) - open it again and make the change again, so that change isn't undone` | | Open the file again and make your change again. |
| **Not Applied**: `<file> was not changed: <error>` | It couldn't be written, or the tag registry couldn't be rebuilt (then ` - the previous file was put back`). | Report it. |
| **Nothing To Apply**: `No values were changed.` | | |
| **Applied**: `Saved <file>.` and what was restarted, for example `Nothing was restarted: every program, the service manager and this HMI included, reads this file when it starts. ...` or `<service> was not restarted: it is stopped (on purpose?). ...` | Saved, with what happens next. `Not committed to git: <reason>` means the change wasn't committed. | Restart what it says by hand. |
| **History**: `No earlier versions of <file> have been kept yet (one is kept every time it is applied here).`, `That version has problems the services would trip over:` | | |
| `This file can't be read, so it can't be edited here. The services can't read it either - use History... to put back a working version.` | above the tree | The file is broken. | Use **History...** to put back a working version. |
| `Problems in the file as it is now (the services are using it - fix them here, or put back a working version with History...):` | above the tree | | Fix them, or put back a working version. |
| `Large changes - check these are meant:` with the tick box `I have checked the large changes` | a number changes by more than a factor of 3 | | Check, then tick to confirm. |

## Data viewer messages

| Title and text | What it means | What to do |
|---|---|---|
| `Following live data`, `Browsing - click to follow live` | The button that says whether the plot follows the newest data. | Click it to follow live data again. |
| **Marker not saved**: `The marker "<text>" did not reach the event database (<path>), so it has been taken off the plot and would not be in exports. ...` | The marker wasn't in the event database a few seconds after it was added. | Check `service_events` is running for this system; see [The data viewer shows no live data](troubleshooting.md#the-data-viewer-shows-no-live-data). |
| **Export complete**, **Export incomplete**: `Saved to: <folder>` and a note | An export finished; incomplete when a log file couldn't be read (its metadata says `INCOMPLETE`). | See [Exporting data and figures](viewer/exporting.md). |
| **Export failed**: `<error>` | | Check the export folder. |
| **Too Many Channels**: `Selected: <n> Limit: 20` | At most 20 channels at a time. | Untick some channels. |
| **Error**: `Failed to save profile: <error>`, `Failed to delete profile from disk: <error>` | A workspace profile couldn't be written (`config/workspace_profiles.json`). | Check the file isn't read-only. |

## See also

- [Troubleshooting](troubleshooting.md)
- [Alarms and faults](procedures/alarms-and-faults.md)
- [How the controls behave](getting-started/controls-and-colours.md)
- [Command Log](panels/command-log.md)
- [The PLC program](how-it-works/plc.md)
- [Glossary](glossary.md)
