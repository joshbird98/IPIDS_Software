# The Automation bar

The Automation bar, across the top of the HMI above the panels, shows which sequence (a recipe, a mass scan or an optimiser sweep) holds which controls, and lets you take them back.

![The Automation bar while a mass scan holds the analyser magnet and the SMU](../images/automation-bar.png)

## What it shows

When no sequence holds anything, the bar reads **Automation: nothing holds any controls**.

Otherwise it has one entry per sequence: a violet label naming the sequence and what it holds, and a violet **TAKE CONTROL** button. For example:

- **mass scan holds: magnet, smu**: a mass scan holds the analyser magnet and the Faraday cup SMU;
- **optimiser holds: steering**: an optimiser sweep of the two electrostatic steering voltages, which belong to the same subsystem;
- **recipe 'Automated Mass Resolution' holds: diagnostics, magnet, smu**: a recipe, by its name, holds the Faraday cup drop-in, the magnet and the SMU.

The controls are named by the last part of their subsystem's name (`ion_beam.beamline.magnet` is **magnet**). Violet always means a sequence holds the control: the same violet marks a held supply on the source panel (see [How the controls behave](../getting-started/controls-and-colours.md)).

## Control leases

Recipes, mass scans and optimiser sweeps run in the sequencer service (`service_sequencer`), not in the HMI. Closing or restarting the HMI does not stop them; the panels only start, pause, stop and show them.

A sequence claims a control lease on each subsystem it drives, immediately before its first command to it:

- While the sequence holds the lease, the service that owns the subsystem takes commands for it only from that sequence. Your controls for it are greyed out, and their tooltip says why, for example **magnet is controlled by mass scan - stop it, or take control, first**.
- The sequence renews the lease every second, and a lease that is not renewed lapses after 5 s. A sequencer that stops or crashes gives its controls back within 5 s by itself.
- The sequence releases its leases when it ends: completed, stopped or failed.
- One recipe, one mass scan and one optimiser sweep can run at the same time, but never on the same subsystem: a sequence that needs a control another sequence holds stops or fails.
- The safety relay, the shutdown and fault reset controls and the gate valve are never leased. They stay available to you whatever is running.

Whatever a sequence sends, the services apply their limits and hard caps, and the PLC applies its own interlocks and limits.

## Taking control

**TAKE CONTROL** takes back every control that sequence holds:

1. Click **TAKE CONTROL**. It turns amber and reads **CONFIRM TAKE CONTROL?**.
2. Click it again within 4 s. Otherwise it goes back as it was and nothing happens.

The services then free those controls at once, and they are yours. The sequence notices at its next command or lease renewal, within about a second:

| Sequence | What happens |
|---|---|
| Recipe | It pauses: **Paused: the operator took control of the** ... The **RESUME** button on the [Recipe Execution](recipes.md) panel claims the controls again and repeats the step the recipe was on: its command is sent again, a wait starts from zero. |
| Mass scan | It stops, with **control was taken by the operator** in its status. It can't put the SMU back to 1 NPLC, because it no longer holds it: the beam-current readings stay at the scan's integration time until a scan next ends. The magnet stays at the last current the scan set. |
| Optimiser sweep | It stops. It can't put the two setpoints back: they stay at the last point swept. |

Each take is recorded in the event log (the operator took control of the subsystem from the sequence), and the commands appear in the [Command Log](command-log.md).

> [!TIP]
> To end a scan or a sweep and let it tidy up, use its own **ABORT** button instead: a mass scan then puts the SMU back to 1 NPLC, and the optimiser puts both setpoints back where they were. Take control when you need the controls at once.

## See also

- [How the controls behave](../getting-started/controls-and-colours.md)
- [Recipe Execution and the recipe builder](recipes.md)
- [Mass Scan](mass-scan.md)
- [Optimiser](optimiser.md)
- [Services and messaging](../how-it-works/services.md)
