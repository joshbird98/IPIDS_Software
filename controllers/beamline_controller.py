from hmi_widget_classes import *

class BeamlineController:
    def __init__(self, main_window_ui, plc):
        self.ui = main_window_ui
        self.plc = plc
        self.binders = []

        self.bind_widgets()

    def bind_widgets(self):

        # Magnet Sweep start button
        self.binders.append(SmartButtonBinder(
            self.ui.startMagnetSweep,
            self.plc,
            write_tag="system.beamline.massScan.requestStart",
            read_tag="system.beamline.massScan.feedback.sweepActive",
            colors={'on': 'grey', 'off': 'green', 'text': 'black'},
            text_on="---",    # When running (True), button says "STOP"
            text_off="GO!"   # When stopped (False), button says "START"
            )
        )

        # Magnet Sweep stop button
        self.binders.append(SmartButtonBinder(
            self.ui.stopMagnetSweep,
            self.plc,
            write_tag="system.beamline.massScan.requestStop",
            read_tag="system.beamline.massScan.feedback.sweepActive",
            colors={'on': 'red', 'off': 'grey', 'text': 'black'},
            text_on="STOP!",    # When running (True), button says "STOP"
            text_off="---",   # When stopped (False), button says "START"
            invert=True
            )
        )

        # Magnet Current Readback label
        self.binders.append(ReadbackLabelBinder(
            self.ui.magnetCurrentReadbackLabel, self.plc, "system.beamline.magnet.readbackA",
            format_str="{:.3f}",
            unit="A"
        ))

        # Magnet lock label
        self.binders.append(StatusIndicatorBinder(
            self.ui.magnetLockLabel, self.plc, "system.beamline.magnet.hmiSetpointAEnabled",
            image_map={0: "padlock_icon", 1: "blank"}
        ))

        # Magnet status label
        self.binders.append(StatusIndicatorBinder(
            self.ui.magnetStatusLED, self.plc, "system.beamline.magnet.status",
            image_map={0: "dim_green_light", 1: "green_light", 2:"red_light"}
        ))

        # Magnet status label
        self.binders.append(StatusIndicatorBinder(
            self.ui.magnetStaticIndicatorLED, self.plc, "system.beamline.magnet.static_active",
            image_map={0: "dim_green_light", 1: "green_light", 2: "red_light"}
        ))

        # Mass-Scan status label
        self.binders.append(StatusIndicatorBinder(
            self.ui.magnetSweepIndicatorLED, self.plc, "system.beamline.massScan.feedback.status",
            image_map={0: "dim_green_light", 1: "green_light", 2: "green_light", 99: "red_light"}
        ))

        # Remaining Sweep Time Minutes readback label
        self.binders.append(ReadbackLabelBinder(
            self.ui.magnetSweepRemainingTime_min, self.plc, "system.beamline.massScan.feedback.remainingTime_m",
            unit="",
            format_str="{:02d}"
        ))
        # Remaining Sweep Time Seconds readback label
        self.binders.append(ReadbackLabelBinder(
            self.ui.magnetSweepRemainingTime_sec, self.plc, "system.beamline.massScan.feedback.remainingTime_s",
            unit="",
            format_str="{:02d}"
        ))

        # Magnet current static setpoint entry field
        self.binders.append(NumberEntryBinder(
            self.ui.magnetCurrentSetpointEntry, self.plc,
            write_tag="system.beamline.magnet.hmiSetpointAEntry",
            enable_tag="system.beamline.magnet.hmiSetpointAEnabled",
            min_tag="system.beamline.magnet.hmiSetpointAEntryMin",
            max_tag="system.beamline.magnet.hmiSetpointAEntryMax",
            positive_only=True  # Cannot be negative (>= 0)
        ))


        # Magnet current sweep low setpoint entry field
        self.binders.append(NumberEntryBinder(
            self.ui.magnetCurrentLowSweepSetpointEntry, self.plc,
            write_tag="system.beamline.massScan.settings.startCurrent",
            min_tag="system.beamline.magnet.hmiSetpointAEntryMin",
            max_tag="system.beamline.magnet.hmiSetpointAEntryMax",
            positive_only=True  # Cannot be negative (>= 0)
        ))

        # Magnet current sweep high setpoint entry field
        self.binders.append(NumberEntryBinder(
            self.ui.magnetCurrentHighSweepSetpointEntry, self.plc,
            write_tag="system.beamline.massScan.settings.stopCurrent",
            min_tag="system.beamline.magnet.hmiSetpointAEntryMin",
            max_tag="system.beamline.magnet.hmiSetpointAEntryMax",
            positive_only=True  # Cannot be negative (>= 0)
        ))

        # Magnet current sweep step count setpoint entry field
        self.binders.append(NumberEntryBinder(
            self.ui.magnetCurrentSweepStepCountSetpointEntry, self.plc,
            write_tag="system.beamline.massScan.settings.totalSteps",
            is_int=True,  # Must be an Integer
            positive_only=True  # Cannot be negative (>= 0)
        ))

        # Magnet current sweep step duration setpoint entry field
        self.binders.append(NumberEntryBinder(
            self.ui.magnetCurrentSweepStepDurationSetpointEntry, self.plc,
            write_tag="system.beamline.massScan.settings.stepTime",
            positive_only=True  # Cannot be negative (>= 0)
        ))


    def get_binders(self):
        return self.binders