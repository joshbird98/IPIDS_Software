# SINDIE help

This is the help for the software of SINDIE, the Surrey Implanter for Negative-ions, Defect Implantation, and Enrichment, at the Surrey Ion Beam Centre. The software is the operator HMI, the data viewer, and the services behind them that talk to the PLC and the instruments.

The help covers:

- how to start the software;
- what each panel does;
- how to bring up and shut down a beam from the TESS (Thermionic Emission Sputter Source);
- what to do about alarms;
- how the system works underneath.

In the HMI, press **F1** with the mouse over a panel to open that panel's page. **Help > Help contents** opens this page.

> [!CAUTION]
> The software is not a safety system. People and the machine are protected by the safety relay and its hardwired interlocks, and by the interlocks in the PLC, whatever the HMI shows or sends. Read the [safety rules](safety.md) before operating SINDIE.

## Contents

- **Start here**
    - [Safety rules](safety.md)
    - [Starting the software](getting-started/starting-the-software.md)
    - [A tour of the HMI](getting-started/screen-tour.md)
    - [How the controls behave](getting-started/controls-and-colours.md)
- **Running the implanter**
    - [Bringing up a beam](procedures/beam-start-up.md)
    - [Shutting down](procedures/beam-shut-down.md)
    - [Alarms and faults](procedures/alarms-and-faults.md)
    - [Selecting a mass and tuning the beam](procedures/tuning-the-beam.md)
    - [Recording an experiment](procedures/recording-an-experiment.md)
- **HMI panels**
    - [Vacuum System](panels/vacuum.md)
    - [System Health](panels/system-health.md)
    - [TESS Ion Source Control](panels/ion-source.md)
    - [Beamline Optics](panels/beamline-optics.md)
    - [Faraday Cup & SMU](panels/faraday-cup-smu.md)
    - [Current Monitoring](panels/current-monitoring.md)
    - [Mass Scan](panels/mass-scan.md)
    - [Optimiser](panels/optimiser.md)
    - [Alarms](panels/alarms.md)
    - [Recipe Execution and the recipe builder](panels/recipes.md)
    - [Experiment Logging](panels/experiment-logging.md)
    - [Command Log](panels/command-log.md)
    - [Service Manager](panels/service-manager.md)
    - [The Automation bar](panels/automation-bar.md)
- **Data viewer**
    - [Using the data viewer](viewer/data-viewer.md)
    - [Exporting data and figures](viewer/exporting.md)
- **When something goes wrong**
    - [Troubleshooting](troubleshooting.md)
    - [Messages and what they mean](messages.md)
- **How it works**
    - [System overview](how-it-works/overview.md)
    - [The PLC program](how-it-works/plc.md)
    - [Services and messaging](how-it-works/services.md)
    - [Alarm handling](how-it-works/alarms.md)
    - [Data logging and storage](how-it-works/data-logging.md)
    - [The simulator](how-it-works/simulator.md)
- **Engineering**
    - [Configuration files and the config editor](engineering/configuration.md)
    - [Tests](engineering/testing.md)
    - [Writing help pages](engineering/writing-help-pages.md)
- [Glossary](glossary.md)
