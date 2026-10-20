# Starting the software

How to start and stop the SINDIE software on the lab PC, what each part is for, and how to run the simulator instead when you want to practise or try something out without the machine.

## The three parts

| Part | What it does | How to start it |
|---|---|---|
| The services | Talk to the PLC and the instruments, keep the alarms, run recipes and scans, log the data. Thirteen programs, started and watched over by the service manager. | `python src/services/service_manager.py` |
| The HMI | The operator screen: shows what the services report and sends your commands to them. | `python src/hmi.py` |
| The data viewer | Plots the logged data, live or from any time in the past; exports data and figures. | `python src/gui/data_viewer/viewer_main.py` |

Run each command in its own command window, from the SINDIE software folder.

The services do the work and hold the machine's state. The HMI and the data viewer only show it and pass on your commands, so you can close and reopen either of them at any time: nothing on the machine changes, and a recipe or scan that is running carries on.

## Starting on the lab PC

1. Start the service manager. It starts every service, and restarts any that stops or stops answering. Give it about 15 seconds to bring them all up.
2. Start the HMI. On the **Service Manager** panel, every service should show **ONLINE**; on the **System Health** panel, the PLC connection should show OK.
3. Start the data viewer if you want to watch trends while you work.

When the HMI opens it sends nothing to the machine. It shows the values the services publish, and every setpoint box shows the live setpoint. A value shows as dashes (`---`) until it has been received, and again whenever its service stops reporting it, so a dash never hides a stale number.

> [!NOTE]
> The first time the HMI opens after an update it may show its default arrangement of panels instead of the one you saved: arrangements saved by an older version are not restored. Arrange the panels again and save the arrangement under **View > Layouts > Save layout as...**.

## Stopping

- **The HMI and the data viewer:** close their windows. The machine and the services carry on.
- **The services:** press Ctrl+C in the service manager's command window. It stops every service.

> [!WARNING]
> Shut the source down before you stop the services. The PC writes a watchdog value to the PLC every half second. If it stops changing for 3 seconds, the PLC treats the PC as lost. It latches a watchdog fault, which takes away the gate valve's open permissive, so the PLC closes the gate valve and the safety relay drops, even with the source hot. It also starts its own shutdown sequence. See [Shutting down](../procedures/beam-shut-down.md) and [The PLC program](../how-it-works/plc.md).

Restarting one service from the **Service Manager** panel leaves the others running. Restarting the PLC service pauses the watchdog for a few seconds, which can be enough to set this off. The watchdog fault then stays latched, and the gate valve won't open, until **RESET FAULTS** is pressed.

## The simulator

The simulator runs the whole system against simulated devices, so you can learn the HMI, try a recipe or check a change without the machine:

```
python run_simulation.py
```

It starts the services, the HMI and the data viewer. The HMI's title bar ends with **[SIMULATION - fake devices]**. The simulator uses its own network ports and keeps its data and logs in `data_sim/`, so it can run on the lab PC beside the real system without touching it.

The command window it was started from shows a menu for making things go wrong: the PLC lost, a safety relay trip, an einzel arc, the analyser magnet 10 % low, a gauge controller unreachable and more. Type a number to switch a fault on or off, `c` to clear them all and `q` to stop everything.

The simulated machine starts as the real one does after a power-up: gate valve shut, safety relay off. Open the gate valve and switch the safety relay on before the supplies accept commands. How closely the simulator follows the real source is described in [The simulator](../how-it-works/simulator.md).

Each program also takes `--sim` on its own, for example `python src/hmi.py --sim`. It then uses the simulator's devices, ports and data, so it never talks to the real machine.

## Where things are kept

| What | Where |
|---|---|
| Logged telemetry | `data/parquet_logs/` (today's, in 15-minute files) and `data/daily_logs/` (one file per day) |
| Events: alarms, faults, commands, markers | `data/events.db` |
| Each program's log file | `SystemLogs/` |
| Configuration | `config/` (see [Configuration files](../engineering/configuration.md)) |
| Recipes | `config/recipes/` |

**Help > About SINDIE** shows which version of the software is running and the folders it is using. Quote those details when reporting a problem.

## See also

- [A tour of the HMI](screen-tour.md)
- [Safety rules](../safety.md)
- [Service Manager](../panels/service-manager.md)
- [Troubleshooting](../troubleshooting.md)
