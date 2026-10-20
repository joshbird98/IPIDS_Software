# Recording an experiment

Everything SINDIE measures is logged all the time, without anything to start. This page is about making that record easy to use afterwards: naming each part of an experiment, noting what you did and saw, and getting the data out.

## What is recorded without you doing anything

- **Telemetry:** every readback, setpoint and status from the PLC, the vacuum gauges, the turbo pump, the Spellman supplies, the magnets and the SMU, 20 times a second, whether or not the HMI is open.
- **Events:** every command sent (by you, a recipe, a scan or the optimiser) with the answer it got, every alarm and PLC fault, readbacks going out of tolerance, configuration changes, and the phases and markers you add.
- **Mass scans:** with **Auto-Export CSV on Complete** ticked on the **Mass Scan** panel, the sequencer saves each finished pass, even if the HMI is closed.

See [Data logging and storage](../how-it-works/data-logging.md) for where it all goes.

## During the experiment

1. **Name the phase.** On the **Experiment Logging** panel, type a name for what you are about to do in **Experiment phase**, for example `Silicon_Implant_Run_04`, and press **SET NEW PHASE**. The data viewer can draw a line at that moment with the name (tick **Phase Changes** under its **Event markers**). Set a new phase whenever the experiment moves on: a new sample, a new species, a new dose.
2. **Note what you do and see.** Type it in **Add a log marker or observation**, choose a **Marker colour** if you want to group similar notes, and press **ADD MARKER**. Markers appear on the data viewer's plots at the time you added them. Good things to note: a sample loaded or moved, the beam tuned, an unexpected reading, anything you changed by hand at the machine.
3. You can also add a marker in the data viewer, by double-clicking a plot at the time you want it.

The panel lists this session's phases and markers.

## Afterwards

1. Open the data viewer and go to the time of the experiment: see [Using the data viewer](../viewer/data-viewer.md).
2. Check the record: the phases and markers, the beam current, the pressures.
3. Export the data you need, with its metadata, a summary and a ready-made figure: see [Exporting data and figures](../viewer/exporting.md). The events written with an export are the kinds ticked under the data viewer's **Event markers**, so tick **Phase Changes** as well as **User Markers** before exporting.

> [!TIP]
> Name phases the same way every time (sample, species, run number). The names are what you will search for months later, in the data viewer and in `data/events.db`.

## See also

- [Experiment Logging](../panels/experiment-logging.md)
- [Using the data viewer](../viewer/data-viewer.md)
- [Exporting data and figures](../viewer/exporting.md)
- [Data logging and storage](../how-it-works/data-logging.md)
