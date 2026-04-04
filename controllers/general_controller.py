#clearFaultsButton - QPushButton

from hmi_widget_classes import *


class GeneralController:
    def __init__(self, main_window_ui, plc):
        self.ui = main_window_ui
        self.plc = plc
        self.prefix = "system.general."
        self.binders = []
        self.bind_widgets()

        self.plc.tags["virtual.plcStatus"] = {
            "value": None,
            "offset": None,  # It has no PLC offset
            "type": "INT"
        }


    def update_virtual_tags(self):
        self.plc.tags["virtual.plcStatus"]["value"] = self.plc.connected + (2 * self.plc.tags["system.general.errorPLC"]["value"])
        #self.plc.gui_messages.append("INFO:Virtual tags updated")


    def bind_widgets(self):
        # PLC Connected Symbol
        self.binders.append(StatusIndicatorBinder(
            self.ui.plcConnectedSymbol, self.plc, "virtual.plcStatus",
            image_map={0: "plc_disconnected", 1: "plc_connected", 2: "plc_disconnected", 3: "plc_disconnected"}
        ))

        # Message List
        self.binders.append(MessageLogBinder(self.ui.messageList, self.plc))

        # Fault List
        self.binders.append(FaultDisplayBinder(self.ui.faultList, self.plc))

        # Fault Status LED
        self.binders.append(StatusIndicatorBinder(
            self.ui.faultStatusLED, self.plc, "system.general.systemFault",
            image_map={0: "green_light", 1: "red_light"}
        ))

        # Door Status LED
        self.binders.append(StatusIndicatorBinder(
            self.ui.doorStatusLED, self.plc, "system.ionSource.general.doorStatus",
            image_map={0: "red_light", 1: "green_light"}
        ))

        # Coolant Status LED
        self.binders.append(StatusIndicatorBinder(
            self.ui.coolantStatusLED, self.plc, "system.general.coolantStatus",
            image_map={0: "red_light", 1: "green_light"}
        ))

        # Source Temperature Status LED
        self.binders.append(StatusIndicatorBinder(
            self.ui.sourceBodyTempStatusLED, self.plc, "system.ionSource.general.bodyTempOkay",
            image_map={0: "red_light", 1: "green_light"}
        ))

        # Source Vacuum Status LED
        self.binders.append(StatusIndicatorBinder(
            self.ui.sourceVacuumStatusLED, self.plc, "system.vacuumSystem.gauges.source.status",
            image_map={0: "red_light", 1: "green_light"}
        ))

        # Source Temperature Readback
        self.binders.append(ReadbackLabelBinder(
            self.ui.ionSourceBodyTemperatureReadbackLabel, self.plc, "system.ionSource.general.bodyTempC",
            "C", "{:.1f}"
        ))

        # Source Vacuum Readback
        self.binders.append(ReadbackLabelBinder(
            self.ui.ionSourceVacuumReadbackLabel, self.plc, "system.vacuumSystem.gauges.source.readback_mB",
            "mB", "{:.2e}"
        ))

        # Clear Faults Button
        self.binders.append(SmartButtonBinder(
            self.ui.clearFaultsButton, self.plc, "system.general.requestClearFaults",
            momentary=True
        ))



        # 2. Plotting Logic (See note below)
        # Plots are usually too complex for a generic "Binder".
        # You normally handle them explicitly here.
        # self.ui.my_plot_widget is the widget in your UI file

    def update_plots(self):
        # Called by the main loop, specifically for plots
        # data = self.plc.tags["my_array"]["value"]
        # self.ui.my_plot_widget.plot(data)
        pass

    def get_binders(self):
        return self.binders