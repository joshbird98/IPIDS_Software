from hmi_widget_classes import *

class IonSourceController:
    def __init__(self, main_window_ui, plc):
        self.ui = main_window_ui
        self.plc = plc
        self.binders = []

        self.bind_widgets()

        self.plc.tags["virtual.targetForm"] = {
            "value": None,
            "offset": None,  # It has no PLC offset
            "type": "STRING"
        }
        self.plc.tags["virtual.targetMaterial"] = {
            "value": None,
            "offset": None,  # It has no PLC offset
            "type": "STRING"
        }

    def bind_widgets(self):

        # Condition source button
        self.binders.append(SmartButtonBinder(
            self.ui.conditionSourceButton, self.plc, "system.ionSource.general.requestCondition",
            momentary=True
        ))
        # Start source button
        self.binders.append(SmartButtonBinder(
            self.ui.startSourceButton, self.plc, "system.ionSource.general.requestStart",
            momentary=True
        ))
        # Stop source button
        self.binders.append(SmartButtonBinder(
            self.ui.stopSourceButton, self.plc, "system.ionSource.general.requestStop",
            momentary=True
        ))
        # Pause source button
        self.binders.append(SmartButtonBinder(
            self.ui.pauseSourceButton, self.plc, "system.ionSource.general.requestPause",
            momentary=True
        ))
        # Resume source button
        self.binders.append(SmartButtonBinder(
            self.ui.resumeSourceButton, self.plc, "system.ionSource.general.requestResume",
            momentary=True
        ))

        # Source mode label
        self.binders.append(ReadbackLabelBinder(
            self.ui.sourceModeLabel, self.plc, "system.ionSource.general.modeCurrent",
            value_map = {0:"Powered Off", 1:"Off", 2:"Conditioning", 3:"Running", 4:"Pausing", 5:"Paused"}
        ))
        # Ion voltage label
        self.binders.append(ReadbackLabelBinder(
            self.ui.ionVoltageCalculatedReadbackLabel, self.plc, "system.ionSource.general.beamVoltage",
            unit="V",
            format_str="{:.0f}"
        ))

        # Ioniser lock label
        self.binders.append(StatusIndicatorBinder(
            self.ui.ioniserLockLabel, self.plc, "system.ionSource.ioniser.hmiSetpointWEnabled",
            image_map={0: "padlock_icon", 1: "blank"}
        ))
        # Target lock label
        self.binders.append(StatusIndicatorBinder(
            self.ui.targetLockLabel, self.plc, "system.ionSource.target.hmiSetpointVEnabled",
            image_map={0: "padlock_icon", 1: "blank"}
        ))
        # Extraction lock label
        self.binders.append(StatusIndicatorBinder(
            self.ui.extractionLockLabel, self.plc, "system.ionSource.extraction.hmiSetpointVEnabled",
            image_map={0: "padlock_icon", 1: "blank"}
        ))
        # Cesium lock label
        self.binders.append(StatusIndicatorBinder(
            self.ui.cesiumLockLabel, self.plc, "system.ionSource.cesium.hmiSetpointCEnabled",
            image_map={0: "padlock_icon", 1: "blank"}
        ))

        # Ioniser status label
        self.binders.append(StatusIndicatorBinder(
            self.ui.ioniserStatusLED, self.plc, "system.ionSource.ioniser.status",
            image_map={0: "dim_green_light", 1: "green_light", 2:"red_light"}
        ))
        # Target status label
        self.binders.append(StatusIndicatorBinder(
            self.ui.targetStatusLED, self.plc, "system.ionSource.target.status",
            image_map={0: "dim_green_light", 1: "green_light", 2: "red_light"}
        ))
        # Extraction status label
        self.binders.append(StatusIndicatorBinder(
            self.ui.extractionStatusLED, self.plc, "system.ionSource.extraction.status",
            image_map={0: "dim_green_light", 1: "green_light", 2: "red_light"}
        ))
        # Cesium status label
        self.binders.append(StatusIndicatorBinder(
            self.ui.cesiumStatusLED, self.plc, "system.ionSource.cesium.status",
            image_map={0: "dim_green_light", 1: "green_light", 2: "red_light", 3: "red_light"}
        ))

        # Cesium coolant on LED
        self.binders.append(StatusIndicatorBinder(
            self.ui.cesiumCoolantOnLED, self.plc, "system.ionSource.cesium.coolantOn",
            image_map={0: "dim_blue_light", 1: "blue_light"}
        ))
        # Cesium heater on LED
        self.binders.append(StatusIndicatorBinder(
            self.ui.cesiumHeaterOnLED, self.plc, "system.ionSource.cesium.heaterOn",
            image_map={0: "dim_orange_light", 1: "orange_light"}
        ))

        # Ioniser power readback label
        self.binders.append(ReadbackLabelBinder(
            self.ui.ioniserPowerReadbackLabel, self.plc, "system.ionSource.ioniser.readbackW",
            unit="W"
        ))
        # Ioniser power ratio label
        self.binders.append(ReadbackLabelBinder(
            self.ui.ioniserPowerRatioLabel, self.plc, "system.ionSource.ioniser.powerRatio"
        ))

        # Filament voltage readback label
        self.binders.append(ReadbackLabelBinder(
            self.ui.filamentVoltageReadbackLabel, self.plc, "system.ionSource.ioniser.filament.readbackV",
            unit="V"
        ))
        # Filament current readback label
        self.binders.append(ReadbackLabelBinder(
            self.ui.filamentCurrentReadbackLabel, self.plc, "system.ionSource.ioniser.filament.readbackA",
            unit="A"
        ))
        # Filament power readback label
        self.binders.append(ReadbackLabelBinder(
            self.ui.filamentPowerReadbackLabel, self.plc, "system.ionSource.ioniser.filament.readbackW",
            unit="W"
        ))

        # Thermionic voltage readback label
        self.binders.append(ReadbackLabelBinder(
            self.ui.thermionicVoltageReadbackLabel, self.plc, "system.ionSource.ioniser.thermionic.readbackV",
            unit="V"
        ))
        # Thermionic current readback label
        self.binders.append(ReadbackLabelBinder(
            self.ui.thermionicCurrentReadbackLabel, self.plc, "system.ionSource.ioniser.thermionic.readbackA",
            unit="A"
        ))
        # Thermionic power readback label
        self.binders.append(ReadbackLabelBinder(
            self.ui.thermionicPowerReadbackLabel, self.plc, "system.ionSource.ioniser.thermionic.readbackW",
            unit="W"
        ))


        # Target voltage readback label
        self.binders.append(ReadbackLabelBinder(
            self.ui.targetVoltageReadbackLabel, self.plc, "system.ionSource.target.readbackV",
            unit="V"
        ))
        # Target current readback label
        self.binders.append(ReadbackLabelBinder(
            self.ui.targetCurrentReadbackLabel, self.plc, "system.ionSource.target.readbackA",
            unit="A"
        ))
        # Target power readback label
        self.binders.append(ReadbackLabelBinder(
            self.ui.targetPowerReadbackLabel, self.plc, "system.ionSource.target.readbackW",
            unit="W"
        ))

        # Extraction voltage readback label
        self.binders.append(ReadbackLabelBinder(
            self.ui.extractionVoltageReadbackLabel, self.plc, "system.ionSource.extraction.readbackV",
            unit="V"
        ))
        # Extraction current readback label
        self.binders.append(ReadbackLabelBinder(
            self.ui.extractionCurrentReadbackLabel, self.plc, "system.ionSource.extraction.readbackA",
            unit="A"
        ))
        # Extraction power readback label
        self.binders.append(ReadbackLabelBinder(
            self.ui.extractionPowerReadbackLabel, self.plc, "system.ionSource.extraction.readbackW",
            unit="W"
        ))

        # Cesium temperature readback label
        self.binders.append(ReadbackLabelBinder(
            self.ui.cesiumTemperatureReadbackLabel, self.plc, "system.ionSource.cesium.readbackC",
            unit="C"
        ))

        # Ioniser power setpoint entry field
        self.binders.append(NumberEntryBinder(
            self.ui.ioniserPowerSetpointEntry, self.plc,
            write_tag="system.ionSource.ioniser.hmiSetpointWEntry",
            enable_tag="system.ionSource.ioniser.hmiSetpointWEnabled",
            min_tag="system.ionSource.ioniser.hmiSetpointWEntryMin",
            max_tag="system.ionSource.ioniser.hmiSetpointWEntryMax"
        ))
        # Target voltage setpoint entry field
        self.binders.append(NumberEntryBinder(
            self.ui.targetVoltageSetpointEntry, self.plc,
            write_tag="system.ionSource.target.hmiSetpointVEntry",
            enable_tag="system.ionSource.target.hmiSetpointVEnabled",
            min_tag="system.ionSource.target.hmiSetpointVEntryMin",
            max_tag="system.ionSource.target.hmiSetpointVEntryMax"
        ))
        # Extraction voltage setpoint entry field
        self.binders.append(NumberEntryBinder(
            self.ui.extractionVoltageSetpointEntry, self.plc,
            write_tag="system.ionSource.extraction.hmiSetpointVEntry",
            enable_tag="system.ionSource.extraction.hmiSetpointVEnabled",
            min_tag="system.ionSource.extraction.hmiSetpointVEntryMin",
            max_tag="system.ionSource.extraction.hmiSetpointVEntryMax"
        ))
        # Cesium temperature setpoint entry field
        self.binders.append(NumberEntryBinder(
            self.ui.cesiumTemperatureSetpointEntry, self.plc,
            write_tag="system.ionSource.cesium.hmiSetpointCEntry",
            enable_tag="system.ionSource.cesium.hmiSetpointCEnabled",
            min_tag="system.ionSource.cesium.hmiSetpointCEntryMin",
            max_tag="system.ionSource.cesium.hmiSetpointCEntryMax"
        ))

        # Target form entry field
        self.binders.append(TextEntryBinder(
            self.ui.targetFormEntry, self.plc,
            write_tag="virtual.targetForm"
        ))
        # Target form entry field
        self.binders.append(TextEntryBinder(
            self.ui.targetMaterialEntry, self.plc,
            write_tag="virtual.targetMaterial"
        ))

    def get_binders(self):
        return self.binders