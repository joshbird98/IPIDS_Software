# Starting the software

How to start and stop the SINDIE software on the lab PC, with the launcher or by hand, what each part is for, and how to run the simulator instead when you want to practise or try something out without the machine.

## The parts

| Part | What it does | How to start it |
|---|---|---|
| The services | Talk to the PLC and the instruments, keep the alarms, run recipes and scans, log the data. Thirteen programs, started and watched over by the service manager. | The launcher (**Start services**), or `python src/services/service_manager.py` |
| The HMI | The operator screen: shows what the services report and sends your commands to them. | The launcher or the **SINDIE HMI** shortcut, or `python src/hmi.py` |
| The data viewer | Plots the logged data, live or from any time in the past; exports data and figures. | The launcher or the **SINDIE Data Viewer** shortcut, or `python src/gui/data_viewer/viewer_main.py` |
| The launcher | Starts the others without PyCharm, and keeps the services running in the background with an icon in the Windows tray. | The **SINDIE** shortcut, or `pythonw main_launcher.py` |

The services do the work and hold the machine's state. The HMI and the data viewer only show it and pass on your commands, so you can close and reopen either of them at any time: nothing on the machine changes, and a recipe or scan that is running carries on.

## The launcher

![The launcher, here started for the simulator: the services' state, the programs and the Windows settings](../images/launcher.png)

| Control | What it does |
|---|---|
| **Services** chip | The services as the service manager reports them: **13 OF 13 ONLINE** (green), fewer (amber, with which are waiting below it), **STARTING**, **NOT ANSWERING** (the service manager runs but hasn't reported for a while), **STOPPING...**, **NOT RUNNING** (red) or **STOPPED** (stopped on purpose with **Stop services...**, with when and by whom). |
| **Start services** | Starts the service manager in the background, with no command window to close by mistake. It starts every service, as before. It runs on its own, not as part of the launcher, so closing the launcher (or stopping it in PyCharm) leaves it running. Greyed out while it runs. |
| **Stop services...** | Stops every service and the service manager, after you have read what that does and typed `STOP` (see [Stopping](#stopping)). |
| **Keep the services running** | Starts the service manager whenever it isn't running: at once when ticked, and again if it stops, after 5 s, then 30 s, then every 2 minutes. A service manager it has just started gets 30 s to come up before it is started again. Services stopped on purpose stay stopped: see [Stopping](#stopping). |
| **Operator HMI**, **Data viewer** | Start the program, or bring it to the front if it is already open: each runs once. |
| **Help** | This help. |
| **Start the simulator** | `run_simulation.py` in a window of its own (see [The simulator](#the-simulator)). Windows only. |
| **Start with Windows** | Logging on to Windows starts the launcher in the tray, and it starts the services (it ticks **Keep the services running**). Only the real system's launcher offers it, not the simulator's. |
| **Put shortcuts on the desktop** | **SINDIE** (the launcher), **SINDIE HMI** and **SINDIE Data Viewer**. |
| **Python** | The Python that the launcher, the programs it starts and its shortcuts use. |

Closing the launcher's window leaves it in the Windows tray. Its icon carries a dot for the services: green, all online; amber, some starting or restarting; red, the service manager isn't running. Click the icon to open the window; right-click it for the programs, the services' state, **Stop services...** and **Close the launcher**. Closing the launcher never stops the services: they run on their own.

The HMI and the data viewer open each other: **View > Open the data viewer** in the HMI, **View > Open the operator HMI** in the data viewer. Each program runs once per system and Windows user: starting it again, from a shortcut or the launcher, brings the open one to the front. If the open one doesn't answer (it is busy, or hung), you are told so rather than a second copy being opened: wait a moment and try again, or close it (with Task Manager if need be). The simulator's programs are kept apart from the real system's, so both can be open at once.

### Setting it up on the lab PC

Once:

1. Start the launcher with the Python that PyCharm uses for the project: in PyCharm, run `main_launcher.py`; or, in a command window in the SINDIE folder, run the `pythonw.exe` beside that Python with `main_launcher.py`. Nothing needs installing: the launcher, and everything it starts, use that Python, which already has every library SINDIE needs. Its path is at the bottom of the launcher.
2. Press **Put shortcuts on the desktop**.
3. Tick **Start with Windows**.

From then on PyCharm isn't needed to run SINDIE: the services start when Windows does, and the desktop shortcuts open the HMI and the data viewer.

> [!NOTE]
> The shortcuts point at that Python. If the project moves to another Python (a new virtual environment), start the launcher with the new one, press **Put shortcuts on the desktop** again, and untick and tick **Start with Windows**.

## Starting on the lab PC

1. Check the services are running: the launcher's tray icon has a green dot. If not, press **Start services** in the launcher. Give the service manager about 15 seconds to bring them all up.
2. Start the HMI. On the **Service Manager** panel, every service should show **ONLINE**; on the **System Health** panel, the PLC connection should show OK.
3. Start the data viewer if you want to watch trends while you work.

When the HMI opens it sends nothing to the machine. It shows the values the services publish, and every setpoint box shows the live setpoint. A value shows as dashes (`---`) until it has been received, and again whenever its service stops reporting it, so a dash never hides a stale number.

Without the launcher, run each command in the table above in its own command window, from the SINDIE software folder, with the project's Python.

> [!NOTE]
> The first time the HMI opens after an update it may show its default arrangement of panels instead of the one you saved: arrangements saved by an older version are not restored. Arrange the panels again and save the arrangement under **View > Layouts > Save layout as...**.

## Stopping

- **The HMI and the data viewer:** close their windows. The machine and the services carry on.
- **The services:** **Stop services...** in the launcher or its tray menu. It says what stopping does (and whether the safety relay is on or the source hot), and goes ahead only once you have typed `STOP`. It stops every service, the most recently started first and the event log last, then the service manager. A service manager started by hand in a command window stops with Ctrl+C there.

Services stopped with **Stop services...** stay stopped until someone presses **Start services**, even with **Keep the services running** ticked, and even after the launcher or Windows restarts: the stop is recorded for the system (in `data/launcher_state_0.json`), and the launcher shows **STOPPED** with when and by whom. A service manager started some other way meanwhile clears it.

> [!WARNING]
> Shut the source down before you stop the services. The PC writes a watchdog value to the PLC every half second. If it stops changing for 3 seconds, the PLC treats the PC as lost. It latches a watchdog fault, which takes away the gate valve's open permissive, so the PLC closes the gate valve and the safety relay drops, even with the source hot. It also starts its own shutdown sequence. See [Shutting down](../procedures/beam-shut-down.md) and [The PLC program](../how-it-works/plc.md).

Restarting one service from the **Service Manager** panel leaves the others running. Restarting the PLC service pauses the watchdog for a few seconds, which can be enough to set this off. The watchdog fault then stays latched, and the gate valve won't open, until **RESET FAULTS** is pressed.

If the service manager doesn't answer **Stop services...**, the launcher offers to end it and the services it started at once, without tidying up. The next start clears anything left over.

## The simulator

The simulator runs the whole system against simulated devices, so you can learn the HMI, try a recipe or check a change without the machine:

```
python run_simulation.py
```

It starts the services, the HMI and the data viewer (the launcher's **Start the simulator** runs it in a window of its own). The HMI's title bar ends with **[SIMULATION - fake devices]**. The simulator uses its own network ports and keeps its data and logs in `data_sim/`, so it can run on the lab PC beside the real system without touching it.

The command window it was started from shows a menu for making things go wrong: the PLC lost, a safety relay trip, an einzel arc, the analyser magnet 10 % low, a gauge controller unreachable and more. Type a number to switch a fault on or off, `c` to clear them all and `q` to stop everything.

Closing the HMI and the data viewer doesn't stop the simulator: its services carry on until `q` is typed in that window. Started again meanwhile (the launcher's **Start the simulator** too), it says the simulator is already running and asks: `o` opens its HMI and data viewer again, with the fault menu, `r` restarts it, `s` stops it and `q` leaves it running.

The simulated machine starts as the real one does after a power-up: gate valve shut, safety relay off. Open the gate valve and switch the safety relay on before the supplies accept commands. How closely the simulator follows the real source is described in [The simulator](../how-it-works/simulator.md).

Each program also takes `--sim` on its own, for example `python src/hmi.py --sim`. It then uses the simulator's devices, ports and data, so it never talks to the real machine. `main_launcher.py --sim` is a launcher for the simulator's system: its buttons start the simulator's services, HMI and data viewer.

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
