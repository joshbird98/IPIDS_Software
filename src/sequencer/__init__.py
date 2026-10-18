"""The sequencer: automation that runs in its own service (src/services/service_sequencer.py)
instead of inside the HMI, so a recipe or a long scan carries on when the HMI is closed or
restarted, and every sequence holds a control lease on what it drives (src/core/leases.py).

    protocol.py     command and telemetry names shared with the HMI
    port.py         how a job sends commands: lease claimed per subsystem, renewed, released
    jobs.py         the Job base class (states, stop, lease loss) and GeneratorJob
    recipe_job.py   recipes (the step classes are in src/core/recipe_engine.py)
    mass_scan.py    the precision mass scan (was the HMI's PrecisionScanWorker)
    optimiser.py    the 2D optimiser sweep (was the HMI's Optimizer2DWorker)
    points.py       scan points: kept by the job, followed by the HMI from telemetry
"""
