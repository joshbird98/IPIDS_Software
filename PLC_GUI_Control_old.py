
# TO FIX

# Fix weird text positioning and colour palette (proper dark/light mode)

# Fix button behaviour - strange stuff with all buttons and 'requests'

import csv

from PyQt5.QtGui import QImage, QPainter
from PyQt5 import QtWidgets, QtCore, QtGui

from utils.plc_control import *

# ========== Helpers ==========
class TimeAxisItem(pg.AxisItem):
    """Custom axis for displaying time in HH:MM:SS format"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def tickStrings(self, values, scale, spacing):
        """Convert timestamp values to time strings"""
        strings = []
        for v in values:
            # Convert timestamp to datetime (UTC)
            dt_utc = datetime.utcfromtimestamp(v)
            # UK is UTC+0, so no adjustment needed
            strings.append(dt_utc.strftime('%H:%M:%S'))
        return strings

class DailyJSONLogger:
    def __init__(self, log_dir=defaultLogDirectory):
        self.log_dir = log_dir
        os.makedirs(self.log_dir, exist_ok=True)
        self.current_date = datetime.now().strftime("%Y-%m-%d")
        self.filepath = os.path.join(self.log_dir, f"hmi_data_{self.current_date}.jsonl")
        self.file = open(self.filepath, "a", encoding="utf-8")

    def log(self, data: dict):
        today = datetime.now().strftime("%Y-%m-%d")
        # If date rolled over, start a new file
        if today != self.current_date:
            self.file.close()
            self.current_date = today
            self.filepath = os.path.join(self.log_dir, f"hmi_data_{self.current_date}.jsonl")
            self.file = open(self.filepath, "a", encoding="utf-8")

        # Add timestamp to the record
        record = {
            "timestamp": datetime.now().isoformat(),
            **data
        }
        self.file.write(json.dumps(record) + "\n")
        self.file.flush()

    def close(self):
        if not self.file.closed:
            self.file.close()

# Class for handling the scrollable text field showing info, warning and fault messages
class MessageHelper:
    """Wrap a QTextEdit to add coloured, timestamped message entries, limited to max_messages."""
    def __init__(self, text_editAll, ipids, max_messages=100):
        self.text_editAll = text_editAll
        self.max_messages = max_messages
        self._messagesAll = []
        self.plc = ipids

    def addToMessageBox(self, message, severity="info", force=False):
        ts = datetime.now().strftime("%H:%M:%S")
        color = {"INFO": "white", "WARN": "blue", "FAULT": "red"}.get(severity, "white")
        line = f'<span style="color:{color}">[{ts}] {message}</span>'
        self._messagesAll.append(line)
        if len(self._messagesAll) > self.max_messages:
            self._messagesAll = self._messagesAll[-self.max_messages:]
        if force:
            self.text_editAll.setHtml("<br>".join(self._messagesAll))
            self.text_editAll.moveCursor(self.text_editAll.textCursor().End)

    def updateMessages(self):
        newMessages = self.plc.hmi.tags["system.general.newMessages"]["value"]
        if (newMessages != "") and (newMessages is not None) :
            try:
                for part in newMessages.split(" & "):
                    if (part != '') and (part is not None):
                        if ':' in part:
                            severity, text = part.split(":", 1)  # split into two pieces only
                            self.addToMessageBox(text, severity)
            except ValueError as e:
                print(f"Value error: {e}")
                pass
            self.text_editAll.setHtml("<br>".join(self._messagesAll))
            self.text_editAll.moveCursor(self.text_editAll.textCursor().End)

            # Clear old messages from the class and the HMI
            self.plc.hmi.tags["system.general.newMessages"]["value"] = ''
            if not self.plc.hmi.write_tag("system.general.newMessages", ''):
                self.addToMessageBox("Write to clear messages request failed", "WARN", True)


class HMIApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.showMaximized()

        # Set up day/night mode for plot window
        self.nightMode = NIGHT_MODE
        uic.loadUi(UI_LAYOUT_FILE, self)  # Load your existing .ui file

        self.plc = PLC_Interface

        # --- THREAD SETUP ---
        # 1. Create the Thread and the Worker
        self.thread = QThread()
        self.worker = PLCWorker(self.plc)
        # 2. Move the Worker to the Thread
        self.worker.moveToThread(self.thread)
        # 3. Connect Signals
        # When thread starts, start the worker loop
        self.thread.started.connect(self.worker.run)
        # When worker says "Data Updated", update the GUI widgets
        self.worker.data_updated.connect(self.update_gui_view)
        # 4. Start the Thread
        self.thread.start()

        self.logger = DailyJSONLogger()
        self.messageHelper = MessageHelper(self.messageList, self.plc)
        self.previousFaultCode = 0

        # --------- Set up stuff for data logging and plotting ----------
        # Initialize variables
        self.data = {}
        self.timestamps = []
        self.all_timestamps = []  # Keep all historical timestamps
        self.all_data = {}  # Keep all historical data
        self.active_channels = set()
        self.channels = recording_channels
        # Initialize all data arrays
        for channel in self.channels.keys():
            self.data[channel] = []
            self.all_data[channel] = []

        # Setup the channel selection button
        self.setup_channel_selection_button()

        # Setup the plot
        self.setup_plot()

        # Connect signals
        self.plotTimeSpanSlider.valueChanged.connect(self.update_time_span)

        # Initialize time span
        self.time_span = 10  # seconds (default)
        self.update_time_span_label()

        # Setup data update timer
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_data)  # type: ignore
        self.timer.start(int(1000 / PLOT_REFRESH_HZ))  # Update every second
        # SIMPLE MEMORY MANAGEMENT: Keep last 30 minutes of data
        self.max_data_points = 30 * 60 * PLOT_REFRESH_HZ

        # -------- Load pixmaps --------
        self.pixBlueLight = QPixmap(":/icons/images/blue_light.png")
        self.pixDimBlueLight = QPixmap(":/icons/images/dim_blue_light.png")
        self.pixOrangeLight = QPixmap(":/icons/images/orange_light.png")
        self.pixDimOrangeLight = QPixmap(":/icons/images/dim_orange_light.png")
        self.pixGreenLight = QPixmap(":/icons/images/green_light.png")
        self.pixDimGreenLight = QPixmap(":/icons/images/dim_green_light.png")
        self.pixRedLight = QPixmap(":/icons/images/red_light.png")
        self.pixDimRedLight = QPixmap(":/icons/images/dim_red_light.png")
        self.pixPadlock = QPixmap(":/icons/images/padlock_icon.png")
        self.pixPlcDisconnected = QPixmap(":/icons/images/plc_disconnected.png")
        self.pixPlcConnected = QPixmap(":/icons/images/plc_connected.png")
        self.pixPlcError = QPixmap(":/icons/images/plc_error.png")
        self.pixBlank = QPixmap(":/icons/images/blank.png")

        # -------- Set up buttons --------
        self.stopStartSourceButtonGUI = Button("stopStartSourceButton", self.stopStartSourceButton, self.stopStartSource,"STOP", "rgb(255,0,0)", "START", "rgb(0, 255, 0")
        self.pauseResumeSourceButtonGUI = Button("pauseResumeSourceButton", self.pauseResumeSourceButton, self.pauseResumeSource,"RESUME", "rgb(255,0,0)", "PAUSE", "rgb(0, 255, 0", "...", "rgb(0,0,0)")
        self.folderLineEdit.setText(defaultPlotDirectory)
        self.folderButton.clicked.connect(self.pickFigureDirectory)
        self.savePlotButton.clicked.connect(self.savePlotAndData)
        self.clearFaultsButton.clicked.connect(self.clearFaults)

        # -------- Set up edit fields ----------
        self.plc.hmi.tags["system.ionSource.ioniser.setpointW"]["widget"] = NumberEntry(name = "ioniserPowerSetpointEntry",
                                                                                        QLineEdit = self.ioniserPowerSetpointEntry,
                                                                                        hmi = self.plc.hmi,
                                                                                        messageHelper = self.messageHelper,
                                                                                        parentKey = "system.ionSource.ioniser.setpointW",
                                                                                        enablerTagKey = "system.ionSource.ioniser.hmiControlEnabled")
        self.plc.hmi.tags["system.ionSource.target.setpointV"]["widget"] = NumberEntry(name = "targetVoltageSetpointEntry",
                                                                                        QLineEdit = self.targetVoltageSetpointEntry,
                                                                                        hmi = self.plc.hmi,
                                                                                        messageHelper = self.messageHelper,
                                                                                        parentKey = "system.ionSource.target.setpointV",
                                                                                        enablerTagKey = "system.ionSource.target.hmiControlEnabled")
        self.plc.hmi.tags["system.ionSource.extraction.setpointV"]["widget"] = NumberEntry(name = "extractionVoltageSetpointEntry",
                                                                                        QLineEdit = self.extractionVoltageSetpointEntry,
                                                                                        hmi = self.plc.hmi,
                                                                                        messageHelper = self.messageHelper,
                                                                                        parentKey = "system.ionSource.extraction.setpointV",
                                                                                        enablerTagKey = "system.ionSource.extraction.hmiControlEnabled")
        self.plc.hmi.tags["system.ionSource.cesium.setpointC"]["widget"] = NumberEntry(name = "cesiumTemperatureSetpointEntry",
                                                                                        QLineEdit = self.cesiumTemperatureSetpointEntry,
                                                                                        hmi = self.plc.hmi,
                                                                                        messageHelper = self.messageHelper,
                                                                                        parentKey = "system.ionSource.cesium.setpointC",
                                                                                        enablerTagKey = "system.ionSource.cesium.hmiControlEnabled")

        # ---------- Set up Indicators ----------
        self.plc.hmi.tags["system.ionSource.ioniser.status"]["widget"] = Indicator(name = "ioniserStatusLED",
                                                                                     QLabel = self.ioniserStatusLED,
                                                                                     offImage = self.pixDimGreenLight,
                                                                                     onImage = self.pixGreenLight,
                                                                                     faultImage = self.pixRedLight)

        self.plc.hmi.tags["system.ionSource.target.status"]["widget"] = Indicator(name = "targetStatusLED",
                                                                                     QLabel = self.targetStatusLED,
                                                                                     offImage = self.pixDimGreenLight,
                                                                                     onImage = self.pixGreenLight,
                                                                                     faultImage = self.pixRedLight)

        self.plc.hmi.tags["system.ionSource.extraction.status"]["widget"] = Indicator(name = "extractionStatusLED",
                                                                                     QLabel = self.extractionStatusLED,
                                                                                     offImage = self.pixDimGreenLight,
                                                                                     onImage = self.pixGreenLight,
                                                                                     faultImage = self.pixRedLight)

        self.plc.hmi.tags["system.ionSource.cesium.status"]["widget"] = Indicator(name = "cesiumStatusLED",
                                                                                     QLabel = self.cesiumStatusLED,
                                                                                     offImage = self.pixDimGreenLight,
                                                                                     onImage = self.pixGreenLight,
                                                                                     faultImage = self.pixRedLight)

        self.plc.hmi.tags["system.ionSource.cesium.heaterOn"]["widget"] = Indicator(name = "cesiumHeaterOnLED",
                                                                                     QLabel = self.cesiumHeaterOnLED,
                                                                                     offImage = self.pixDimOrangeLight,
                                                                                     onImage = self.pixOrangeLight)

        self.plc.hmi.tags["system.ionSource.cesium.coolantOn"]["widget"] = Indicator(name = "cesiumCoolantOnLED",
                                                                                     QLabel = self.cesiumCoolantOnLED,
                                                                                     offImage = self.pixDimBlueLight,
                                                                                     onImage = self.pixBlueLight)

        self.plc.hmi.tags["system.general.doorStatus"]["widget"] =        Indicator(name = "doorStatusLED",
                                                                                     QLabel = self.doorStatusLED,
                                                                                     offImage = self.pixRedLight,
                                                                                     onImage = self.pixGreenLight)

        self.plc.hmi.tags["system.general.coolantStatus"]["widget"] =     Indicator(name = "coolantStatusLED",
                                                                                     QLabel = self.coolantStatusLED,
                                                                                     offImage = self.pixRedLight,
                                                                                     onImage = self.pixGreenLight)

        self.plc.hmi.tags["system.general.coolantStatus"]["widget"] =     Indicator(name = "coolantStatusLED",
                                                                                     QLabel = self.coolantStatusLED,
                                                                                     offImage = self.pixRedLight,
                                                                                     onImage = self.pixGreenLight)

        self.plc.hmi.tags["system.ionSource.general.bodyTempStatus"]["widget"] = Indicator(name = "sourceBodyTempStatusLED",
                                                                                     QLabel = self.sourceBodyTempStatusLED,
                                                                                     offImage = self.pixRedLight,
                                                                                     onImage = self.pixGreenLight)

        self.plc.hmi.tags["system.vacuumSystem.source.status"]["widget"] = Indicator(name = "sourceVacuumStatusLED",
                                                                                     QLabel = self.sourceVacuumStatusLED,
                                                                                     offImage = self.pixRedLight,
                                                                                     onImage = self.pixGreenLight)

        self.plc.hmi.tags["system.general.systemFault"]["widget"] =       Indicator(name = "faultStatusLED",
                                                                                     QLabel = self.faultStatusLED,
                                                                                     offImage = self.pixGreenLight,
                                                                                     onImage = self.pixRedLight)

        self.plc.hmi.tags["system.ionSource.ioniser.hmiControlEnabled"]["widget"] = Indicator(name = "ioniserLockLabel",
                                                                                     QLabel = self.ioniserLockLabel,
                                                                                     offImage = self.pixPadlock,
                                                                                     onImage = self.pixBlank)

        self.plc.hmi.tags["system.ionSource.target.hmiControlEnabled"]["widget"] = Indicator(name = "targetLockLabel",
                                                                                     QLabel = self.targetLockLabel,
                                                                                     offImage = self.pixPadlock,
                                                                                     onImage = self.pixBlank)

        self.plc.hmi.tags["system.ionSource.extraction.hmiControlEnabled"]["widget"] = Indicator(name = "extractionLockLabel",
                                                                                     QLabel = self.extractionLockLabel,
                                                                                     offImage = self.pixPadlock,
                                                                                     onImage = self.pixBlank)

        self.plc.hmi.tags["system.ionSource.cesium.hmiControlEnabled"]["widget"] = Indicator(name = "cesiumLockLabel",
                                                                                     QLabel = self.cesiumLockLabel,
                                                                                     offImage = self.pixPadlock,
                                                                                     onImage = self.pixBlank)

        self.plcConnectedIndicator =                                        Indicator(name = "plcConnected",
                                                                                      QLabel = self.plcConnectedSymbol,
                                                                                      offImage = self.pixPlcDisconnected,
                                                                                      onImage = self.pixPlcConnected,
                                                                                      faultImage = self.pixPlcDisconnected)

        # ---------- Set up Number Labels ----------

        self.plc.hmi.tags["system.ionSource.ioniser.filamentV"]["widget"] = ReadbackLabel(name = "filamentVoltageReadbackLabel",
                                                                                            QLabel = self.filamentVoltageReadbackLabel,
                                                                                            units = "V")
        self.plc.hmi.tags["system.ionSource.ioniser.filamentA"]["widget"] = ReadbackLabel(name = "filamentCurrentReadbackLabel",
                                                                                            QLabel = self.filamentCurrentReadbackLabel,
                                                                                            units = "A")
        self.plc.hmi.tags["system.ionSource.ioniser.filamentW"]["widget"] = ReadbackLabel(name = "filamentPowerReadbackLabel",
                                                                                            QLabel = self.filamentPowerReadbackLabel,
                                                                                            units = "W")

        self.plc.hmi.tags["system.ionSource.ioniser.thermionicV"]["widget"] = ReadbackLabel(name = "thermionicVoltageReadbackLabel",
                                                                                            QLabel = self.thermionicVoltageReadbackLabel,
                                                                                            units = "V")
        self.plc.hmi.tags["system.ionSource.ioniser.thermionicA"]["widget"] = ReadbackLabel(name = "thermionicCurrentReadbackLabel",
                                                                                            QLabel = self.thermionicCurrentReadbackLabel,
                                                                                            units = "A")
        self.plc.hmi.tags["system.ionSource.ioniser.thermionicW"]["widget"] = ReadbackLabel(name = "thermionicPowerReadbackLabel",
                                                                                            QLabel = self.thermionicPowerReadbackLabel,
                                                                                            units = "W")

        self.plc.hmi.tags["system.ionSource.ioniser.readbackW"]["widget"] = ReadbackLabel(name = "ioniserPowerReadbackLabel",
                                                                                            QLabel = self.ioniserPowerReadbackLabel,
                                                                                            units = "W")

        self.plc.hmi.tags["system.ionSource.ioniser.powerRatio"]["widget"] = ReadbackLabel(name = "ioniserPowerRatioLabel",
                                                                                            QLabel = self.ioniserPowerRatioLabel,
                                                                                            units = "")

        self.plc.hmi.tags["system.ionSource.target.readbackV"]["widget"] = ReadbackLabel(name = "targetVoltageReadbackLabel",
                                                                                            QLabel = self.targetVoltageReadbackLabel,
                                                                                            units = "V")
        self.plc.hmi.tags["system.ionSource.target.readbackA"]["widget"] = ReadbackLabel(name = "targetCurrentReadbackLabel",
                                                                                            QLabel = self.targetCurrentReadbackLabel,
                                                                                            units = "A")
        self.plc.hmi.tags["system.ionSource.target.readbackW"]["widget"] = ReadbackLabel(name = "targetPowerReadbackLabel",
                                                                                            QLabel = self.targetPowerReadbackLabel,
                                                                                            units = "W")

        self.plc.hmi.tags["system.ionSource.extraction.readbackV"]["widget"] = ReadbackLabel(name = "extractionVoltageReadbackLabel",
                                                                                            QLabel = self.extractionVoltageReadbackLabel,
                                                                                            units = "V")
        self.plc.hmi.tags["system.ionSource.extraction.readbackA"]["widget"] = ReadbackLabel(name = "extractionCurrentReadbackLabel",
                                                                                            QLabel = self.extractionCurrentReadbackLabel,
                                                                                            units = "A")
        self.plc.hmi.tags["system.ionSource.extraction.readbackW"]["widget"] = ReadbackLabel(name = "extractionPowerReadbackLabel",
                                                                                            QLabel = self.extractionPowerReadbackLabel,
                                                                                            units = "W")

        self.plc.hmi.tags["system.ionSource.general.ionVoltage"]["widget"] = ReadbackLabel(name = "ionVoltageCalculatedReadbackLabel",
                                                                                            QLabel = self.ionVoltageCalculatedReadbackLabel,
                                                                                            units = "V")

        self.plc.hmi.tags["system.ionSource.cesium.readbackC"]["widget"] = ReadbackLabel(name = "cesiumTemperatureReadbackLabel",
                                                                                            QLabel = self.cesiumTemperatureReadbackLabel,
                                                                                            units = "C")

        self.plc.hmi.tags["system.ionSource.general.bodyTempC"]["widget"] = ReadbackLabel(name = "ionSourceBodyTemperatureReadbackLabel",
                                                                                            QLabel = self.ionSourceBodyTemperatureReadbackLabel,
                                                                                            units = "C")

        self.plc.hmi.tags["system.vacuumSystem.source.readback"]["widget"] = ReadbackLabel(name = "ionSourceVacuumReadbackLabel",
                                                                                            QLabel = self.ionSourceVacuumReadbackLabel,
                                                                                            units = "mB")


        # -------- Set up tabs ---------
        self.currentTab = self.tabMachineSections.currentIndex()

        # -------- Time span slider --------
        self.plotTimeSpanSlider.setValue(0)

        # -------- Timer / Demo Data --------
        self.refreshGUITimer = QTimer(self)
        self.refreshGUITimer.timeout.connect(self.updateGUI) # type: ignore
        self.refreshGUITimer.start(int(1000 / GUI_REFRESH_HZ))  # update every 1000 ms

        self.refreshHMITimer = QTimer(self)
        self.refreshHMITimer.timeout.connect(self.updateHMI) # type: ignore
        self.refreshHMITimer.start(int(1000/ HMI_REFRESH_TIMER))  # update every 900 ms

        self.refreshLogTimer = QTimer(self)
        self.refreshLogTimer.timeout.connect(self.updateLog) # type: ignore
        self.refreshLogTimer.start(int(1000 / LOG_REFRESH_TIMER))  # update every 900 ms

    # -------- Update Values ---------
    # Gets the latest values from the PLCs hmiDB via Snap7
    def updateHMI(self):
        self.plc.readAll()

    def updateGUI(self):

        for name, meta in self.plc.hmi.tags.items():
            if meta["widget"] is not None:
                value = meta["value"]
                if isinstance(meta["widget"], Indicator):
                    meta["widget"].setMode(value)

                elif isinstance(meta["widget"], ReadbackLabel):
                    meta["widget"].writeValue(value)

                elif isinstance(meta["widget"], NumberEntry) and meta["writable"]:
                    meta["widget"].writeValue(str(value))
                    meta["widget"].updateEnabled()
                    meta["widget"].updateMinMaxValues()

        self.updateFaultList()
        self.messageHelper.updateMessages()
        self.plcConnectedIndicator.setMode(self.plc.hmi.connected + self.plc.hmi.tags["system.general.errorPLC"]["value"])

        # Tab specific stuff
        self.currentTab = self.tabMachineSections.currentIndex()

        # Updating button and status label appearance
        if self.currentTab == TAB_ION_SOURCE:
            self.updateSourceTabButtons()

    def updateSourceTabButtons(self):
        mode = self.plc.hmi.tags["system.ionSource.general.modeCurrent"]["value"]
        if mode == POWERED_OFF:
            self.stopStartSourceButtonGUI.setMode(BUTTON_DISABLED)
            self.pauseResumeSourceButtonGUI.setMode(BUTTON_DISABLED)
            self.sourceModeLabel.setText("POWERED OFF")
        elif mode == OFF:
            self.stopStartSourceButtonGUI.setMode(BUTTON_ON)
            self.pauseResumeSourceButtonGUI.setMode(BUTTON_DISABLED)
            self.sourceModeLabel.setText("OFF")
        elif mode == CONDITIONING:
            self.stopStartSourceButtonGUI.setMode(BUTTON_OFF)
            self.pauseResumeSourceButtonGUI.setMode(BUTTON_DISABLED)
            if self.plc.hmi.tags["system.ionSource.general.conditioningPaused"]["value"]:
                self.sourceModeLabel.setText("CONDITIONING - WAITING")
            else:
                self.sourceModeLabel.setText("CONDITIONING - RAMPING")
        elif mode == RUNNING:
            self.stopStartSourceButtonGUI.setMode(BUTTON_OFF)
            self.pauseResumeSourceButtonGUI.setMode(BUTTON_ON)
            self.sourceModeLabel.setText("RUNNING")
        elif mode == PAUSING:
            self.stopStartSourceButtonGUI.setMode(BUTTON_OFF)
            self.pauseResumeSourceButtonGUI.setMode(BUTTON_OFF)
            self.sourceModeLabel.setText("PAUSING")
        elif mode == PAUSED:
            self.stopStartSourceButtonGUI.setMode(BUTTON_OFF)
            self.pauseResumeSourceButtonGUI.setMode(BUTTON_OFF)
            self.sourceModeLabel.setText("PAUSED")
        elif mode == TEST:
            self.stopStartSourceButtonGUI.setMode(BUTTON_OFF)
            self.pauseResumeSourceButtonGUI.setMode(BUTTON_OFF)
            self.sourceModeLabel.setText("TEST")

    # Looks at fault code, and if different to before, writes out new fault messages into fault box, in red
    def updateFaultList(self):
        # check if different to before
        newFaultCode = self.plc.hmi.tags["system.general.faultCode"]["value"]
        if newFaultCode is not None:
            if newFaultCode != self.previousFaultCode:
                faultMessages = []
                for bit in range(32):
                    if newFaultCode & (1 << bit):
                        message = error_messages_dict.get(bit)
                        print(f"New fault: {message}")
                        if message:
                            faultMessages.append(f'<span style="color:red">{"> "}{message}</span>')
                self.faultList.setHtml("<br>".join(faultMessages))
            self.previousFaultCode = newFaultCode


    # Handles function of stop / start / condition buttons
    def stopStartSource(self):
        sourceMode = self.plc.hmi.tags["system.ionSource.general.modeCurrent"]["value"]
        startRequestStatus = self.plc.hmi.tags["system.ionSource.general.startRequest"]["value"]
        stopRequestStatus = self.plc.hmi.tags["system.ionSource.general.stopRequest"]["value"]

        if ((sourceMode == OFF) or (sourceMode == POWERED_OFF)) and (startRequestStatus == False) and (self.stopStartSourceButtonGUI.mode == BUTTON_ON): # Perform start request
            if self.plc.hmi.write_tag("system.ionSource.general.conditionRequest", self.conditionSourceTickbox.isChecked()):
                if self.plc.hmi.write_tag("system.ionSource.general.startRequest", True):
                    self.stopStartSourceButtonGUI.setMode(BUTTON_OFF)
                else:
                    self.messageHelper.addToMessageBox("Write to start request failed", "WARN", True)
            else:
                self.messageHelper.addToMessageBox("Write to condition request failed", "WARN", True)

        if (sourceMode != OFF) and (sourceMode != POWERED_OFF) and (stopRequestStatus == False) and (self.stopStartSourceButtonGUI.mode == BUTTON_OFF):
            if self.plc.hmi.write_tag("system.ionSource.general.stopRequest", True):
                self.stopStartSourceButtonGUI.setMode(BUTTON_ON)
            else:
                self.messageHelper.addToMessageBox("Write to stop request failed", "WARN", True)

    def pauseResumeSource(self):
        mode = self.plc.hmi.tags["system.ionSource.general.modeCurrent"]["value"]
        pauseRequestStatus = self.plc.hmi.tags["system.ionSource.general.pauseRequest"]["value"]
        resumeRequestStatus = self.plc.hmi.tags["system.ionSource.general.resumeRequest"]["value"]

        if (mode == RUNNING) and (pauseRequestStatus == False) and (self.pauseResumeSourceButtonGUI.mode == BUTTON_ON): # Perform start request
            if self.plc.hmi.write_tag("system.ionSource.general.pauseRequest", True):
                self.pauseResumeSourceButtonGUI.setMode(BUTTON_OFF)
            else:
                self.messageHelper.addToMessageBox("Write to pause request failed", "WARN", True)

        if (mode == PAUSED) and (resumeRequestStatus == False) and (self.pauseResumeSourceButtonGUI.mode == BUTTON_OFF):
            if self.plc.hmi.write_tag("system.ionSource.general.resumeRequest", True):
                self.stopStartSourceButtonGUI.setMode(BUTTON_ON)
            else:
                self.messageHelper.addToMessageBox("Write to resume request failed", "WARN", True)

    # Handles clear faults request button
    def clearFaults(self):
        self.plc.hmi.read_all_plc()
        if not self.plc.hmi.tags["system.general.clearFaultsRequest"]["value"]:
            if self.plc.hmi.write_tag("system.general.clearFaultsRequest", True): # check if write is successful
                    self.messageHelper.addToMessageBox("Clear fault request sent.", "INFO", True)
                    return True
            self.messageHelper.addToMessageBox("Write to clear faults request failed", "WARN", True)
        return False

    # Opens a file explorer and allows the user to choose a folder for saving plots to
    def pickFigureDirectory(self):
        path = QFileDialog.getExistingDirectory(self, "Select a folder", "")
        if path:
            self.folderLineEdit.setText(path)

    # Saves the plot image and the raw data to the requested location
    def savePlotAndData(self):
        """Save current plot as PNG and raw data as CSV using QLineEdit fields."""
        folder = self.folderLineEdit.text().strip()
        name = self.filenameEdit.text().strip()

        if not folder or not name:
            self.messageHelper.addToMessageBox("Folder or filename is empty!", "WARN", True)
            return

        # Ensure folder exists
        os.makedirs(folder, exist_ok=True)

        # Full paths
        png_path = os.path.join(folder, f"{time.strftime('%Y%m%d_%H%M%S')}_{name}.png")
        csv_path = os.path.join(folder, f"{time.strftime('%Y%m%d_%H%M%S')}_{name}.csv")

        # --- Save PNG image ---
        try:
            # Capture the entire graphFrame (which contains the plot widget)
            graph_frame = self.graphFrame
            size = graph_frame.size()

            # Create high-resolution image
            scale_factor = PLOT_SAVE_SCALING
            img_width = int(size.width() * scale_factor)
            img_height = int(size.height() * scale_factor)

            img = QImage(img_width, img_height, QImage.Format_ARGB32)
            img.fill(Qt.white)

            # Render the graphFrame
            painter = QPainter(img)
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setRenderHint(QPainter.TextAntialiasing, True)
            painter.scale(scale_factor, scale_factor)

            graph_frame.render(painter)

            painter.end()

            img.save(png_path)
            print(f"High-resolution plot exported successfully to {png_path}")
            self.messageHelper.addToMessageBox(f"High-resolution plot exported successfully to {png_path}", "INFO", True)
        except Exception as e:
            print(f"Error exporting plot: {e}")
            self.messageHelper.addToMessageBox(f"Error exporting plot: {e}", "WARN", True)

        # --- Save csv data ---
        try:
            with open(csv_path, 'w', newline='') as csvfile:
                writer = csv.writer(csvfile)

                # Write header row with channel names and units
                headers = ['Timestamp', 'Unix Time']
                for channel in self.active_channels:
                    headers.append(f"{channel} ({self.channels[channel]['unit']})")
                writer.writerow(headers)

                # Get the currently visible time range
                current_time = datetime.now()
                cutoff_time = current_time - timedelta(seconds=self.time_span)

                # Find data points within the visible time range
                visible_indices = []
                for i, timestamp in enumerate(self.all_timestamps):
                    if timestamp >= cutoff_time:
                        visible_indices.append(i)

                # Write only visible data for active channels
                for i in visible_indices:
                    if i < len(self.all_timestamps):
                        timestamp = self.all_timestamps[i]
                        row = [
                            timestamp.strftime('%Y-%m-%d %H:%M:%S'),
                            (timestamp - datetime(1970, 1, 1)).total_seconds()
                        ]

                        for channel in self.active_channels:
                            if i < len(self.all_data[channel]):
                                value = self.all_data[channel][i]
                                # Format numbers appropriately
                                if isinstance(value, float) and abs(value) < 0.001 and value != 0:
                                    row.append(f"{value:.6e}")
                                else:
                                    row.append(str(value))
                            else:
                                row.append('')

                        writer.writerow(row)

            self.messageHelper.addToMessageBox(f"Data exported successfully to {csv_path}", "INFO", True)

        except Exception as e:
            self.messageHelper.addToMessageBox(f"Error exporting data: {e}", "WARN", True)

    def updateLog(self):
        try:
            hmi_values = {}
            for name, meta in self.plc.hmi.tags.items():
                hmi_values[name] = meta["value"]
            self.logger.log(hmi_values)
        except AttributeError as e:
            print(f"Missing attribute, logger: {e}")

    def setup_plot(self):

        if self.nightMode:
            text_color = pg.mkColor('white')
        else:
            text_color = pg.mkColor('black')

        # Create a layout for the graph frame
        layout = QVBoxLayout()
        self.graphFrame.setLayout(layout)

        # Create the plot widget with custom time axis
        self.plot_widget = pg.PlotWidget(axisItems={'bottom': TimeAxisItem(orientation='bottom')})
        layout.addWidget(self.plot_widget)

        # Customize the plot
        if self.nightMode:
            self.plot_widget.setBackground('black')
        else:
            self.plot_widget.setBackground('white')

        self.plot_widget.showGrid(x=True, y=True, alpha=0.2)
        self.plot_widget.setLabel('left', 'Linear Scale')
        self.plot_widget.setLabel('bottom', 'Time')

        # Disable mouse scrolling and panning
        self.plot_widget.setMouseEnabled(x=False, y=False)
        self.plot_widget.setMenuEnabled(False)

        # Create a separate viewbox for the right axis (log scale)
        self.right_vb = ViewBox()
        self.plot_widget.scene().addItem(self.right_vb)

        # Add right axis and link it to the right viewbox
        self.plot_widget.showAxis('right')
        self.plot_widget.getAxis('right').linkToView(self.right_vb)
        self.right_vb.setXLink(self.plot_widget.getPlotItem())

        # Set up the right axis for log scale
        self.plot_widget.getAxis('right').setLabel('Log Scale', color='#2ca02c')
        self.plot_widget.getAxis('right').setLogMode(True)

        # Update views when resized
        self.update_views()
        self.plot_widget.getPlotItem().vb.sigResized.connect(self.update_views)

        # Create plot items for each channel
        self.plots = {}

        for channel, config in self.channels.items():
            if config['scale'] == 'log':
                # For log scale, use the right viewbox
                plot = pg.PlotDataItem(pen=pg.mkPen(color=config['color'], width=2))
                self.right_vb.addItem(plot)
            else:
                # For linear scale, use the left axis
                plot = pg.PlotDataItem(pen=pg.mkPen(color=config['color'], width=2))
                self.plot_widget.addItem(plot)

            self.plots[channel] = plot

        # Add legend with better positioning and background
        self.legend = pg.LegendItem(offset=(70, 10))  # Moved further right
        self.legend.setParentItem(self.plot_widget.getPlotItem())

        if self.nightMode:
            # Add a proper background to the legend (non-transparent black)
            self.legend.setBrush(pg.mkBrush(0, 0, 0))  # Solid black
            self.legend.setPen(pg.mkPen('white', width=1))  # White border
            self.legend.setLabelTextColor(pg.mkColor('white'))
        else:
            # Add a proper background to the legend (non-transparent white)
            self.legend.setBrush(pg.mkBrush(255, 255, 255))  # Solid white
            self.legend.setPen(pg.mkPen('k', width=1))  # Black border
            self.legend.setLabelTextColor(pg.mkColor('black'))

        # Ensure the legend appears on top of everything else
        self.legend.setZValue(1000)  # High z-value to put it on top

        # Manually position the legend by setting its position
        # This positions it in the top right corner with some margin
        plot_width = self.graphFrame.width()
        self.legend.setPos(plot_width - 150, 10)  # Adjust these values as needed

        # Set axis label colors
        self.plot_widget.getAxis('left').setLabel(text='Linear Scale', color=text_color)
        self.plot_widget.getAxis('bottom').setLabel(text='Time', color=text_color)
        self.plot_widget.getAxis('right').setLabel(text='Log Scale', color=text_color)

        # Set tick text colors
        self.plot_widget.getAxis('left').setPen(text_color)
        self.plot_widget.getAxis('left').setTextPen(text_color)
        self.plot_widget.getAxis('bottom').setPen(text_color)
        self.plot_widget.getAxis('bottom').setTextPen(text_color)
        self.plot_widget.getAxis('right').setPen(text_color)
        self.plot_widget.getAxis('right').setTextPen(text_color)

        # Remove the label from the bottom axis entirely
        self.plot_widget.setLabel('bottom', '')

    def update_views(self):
        # Update the right viewbox geometry to match the main plot
        self.right_vb.setGeometry(self.plot_widget.getPlotItem().vb.sceneBoundingRect())
        self.right_vb.linkedViewChanged(self.plot_widget.getPlotItem().vb, self.right_vb.XAxis)

    def update_data(self):
        # Get current time
        current_time = datetime.now()
        self.all_timestamps.append(current_time)

        # Generate new data points for each channel
        for channel, config in self.channels.items():
            if self.plc.hmi.connected:
                try:
                    # Parse the data source path and get the value
                    value = self.plc.hmi.tags[config["dataSource"]]["value"] * config["multiplier"]
                    self.all_data[channel].append(value)
                except Exception as e:
                    print(f"Error getting data for {channel}: {e}")
                    self.messageHelper.addToMessageBox(f"Error getting data for {channel}: {e}", "WARN", True)
                    # Fallback to dummy data or handle error
                    self.all_data[channel].append(-1.0)
            else:
                self.all_data[channel].append(-1.0)

        # Prune old data
        if len(self.all_timestamps) > self.max_data_points:
            # Remove oldest data
            excess = len(self.all_timestamps) - self.max_data_points
            self.all_timestamps = self.all_timestamps[excess:]
            for channel in self.channels.keys():
                if len(self.all_data[channel]) > excess:
                    self.all_data[channel] = self.all_data[channel][excess:]

        # Update the plot
        self.update_plot()

    def update_plot(self):
        # Get the current time and calculate the cutoff time
        current_time = datetime.now()
        cutoff_time = current_time - timedelta(seconds=self.time_span)

        # Filter timestamps and data to only show the requested time span
        visible_timestamps = []
        visible_data = {channel: [] for channel in self.channels.keys()}

        for i, timestamp in enumerate(self.all_timestamps):
            if timestamp >= cutoff_time:
                visible_timestamps.append(timestamp)
                for channel in self.channels.keys():
                    if i < len(self.all_data[channel]):
                        visible_data[channel].append(self.all_data[channel][i])

        # Convert timestamps to seconds since epoch for plotting
        times = [(t - datetime(1970, 1, 1)).total_seconds() for t in visible_timestamps]

        # Set x-axis range
        x_min = (cutoff_time - datetime(1970, 1, 1)).total_seconds()
        x_max = (current_time - datetime(1970, 1, 1)).total_seconds()
        self.plot_widget.setXRange(x_min, x_max)

        # Clear the legend
        self.legend.clear()

        # Track which channels are using which axis
        right_axis_channels = []
        left_axis_channels = []

        # Update plots for all channels
        for channel, plot in self.plots.items():
            if channel in self.active_channels and visible_data[channel]:
                plot.show()

                # Update the data
                plot.setData(times, visible_data[channel])

                # Add to legend
                self.legend.addItem(plot, channel)

                # Track which axis this channel uses
                if self.channels[channel]['scale'] == 'log':
                    right_axis_channels.append(channel)
                else:
                    left_axis_channels.append(channel)
            else:
                plot.hide()

        # Configure left axis for linear channels
        if left_axis_channels:
            self.plot_widget.getAxis('left').setLabel('Linear Scale', units='')
            self.plot_widget.getAxis('left').setLogMode(False)
            self.plot_widget.getAxis('left').setStyle(showValues=True)

            # Set appropriate Y range for left axis
            if left_axis_channels:
                min_val = float('inf')
                max_val = float('-inf')
                for channel in left_axis_channels:
                    if visible_data[channel]:
                        channel_min = min(visible_data[channel])
                        channel_max = max(visible_data[channel])
                        min_val = min(min_val, channel_min)
                        max_val = max(max_val, channel_max)

                # Add some padding
                if min_val != float('inf') and max_val != float('-inf'):
                    padding = (max_val - min_val) * 0.1
                    self.plot_widget.setYRange(min_val - padding, max_val + padding)
        else:
            # If no linear channels, hide the left axis
            self.plot_widget.getAxis('left').setLabel('')
            self.plot_widget.getAxis('left').setStyle(showValues=False)

        # Configure right axis for log channels
        if right_axis_channels:
            self.plot_widget.getAxis('right').setLabel('Log Scale', units='')
            self.plot_widget.getAxis('right').setLogMode(True)
            self.plot_widget.getAxis('right').setStyle(showValues=True)

            # Set appropriate Y range for right axis
            if right_axis_channels:
                min_val = float('inf')
                max_val = float('-inf')
                for channel in right_axis_channels:
                    if visible_data[channel]:
                        # For log scale, we need to ensure all values are positive
                        positive_values = [v for v in visible_data[channel] if v > 0]
                        if positive_values:
                            channel_min = min(positive_values)
                            channel_max = max(positive_values)
                            min_val = min(min_val, channel_min)
                            max_val = max(max_val, channel_max)

                # Add some padding for log scale
                if min_val > 0 and max_val > 0 and min_val != float('inf') and max_val != float('-inf'):

                    # Set the range
                    self.right_vb.setYRange(min_val * 0.9, max_val * 1.1)

                    # Convert to log space for calculations
                    log_min = np.log10(min_val)
                    log_max = np.log10(max_val)

                    # Calculate optimal log ticks (always exactly 5 well-spaced ticks)
                    log_range = log_max - log_min
                    tick_step = max(1, round(log_range / 4))  # Ensure we have about 5 ticks

                    # Generate ticks
                    ticks = []
                    start_exp = int(np.floor(log_min))
                    end_exp = int(np.ceil(log_max))

                    # Always show exactly 5 ticks, evenly spaced in log space
                    tick_exponents = np.linspace(log_min, log_max, 5)
                    for exp in tick_exponents:
                        value = 10 ** exp
                        # Format the label based on the value magnitude
                        if value < 0.001:
                            label = f"{value:.1e}"
                        else:
                            label = f"{value:.3g}"
                        ticks.append((value, label))

                    self.plot_widget.getAxis('right').setTicks([ticks])
        else:
            # If no log channels, hide the right axis
            self.plot_widget.getAxis('right').setLabel('')
            self.plot_widget.getAxis('right').setStyle(showValues=False)
            self.plot_widget.getAxis('right').setLogMode(False)

    def update_time_span(self):
        # Map slider position (0-1000) to time span (10s to 1800s/30m)
        # Using exponential mapping for more precision at lower ranges
        slider_value = self.plotTimeSpanSlider.value()

        # Exponential mapping: 10 * (180/10)^(x/1000)
        # This gives more precision at the lower end
        self.time_span = 10 * (180) ** (slider_value / 1000)

        # Clamp to 10s - 1800s (30 minutes)
        self.time_span = max(10, min(1800, self.time_span))

        self.update_time_span_label()

        # Update the plot with the new time span
        self.update_plot()

    def update_time_span_label(self):
        # Format the time span as either seconds or minutes:seconds
        if self.time_span < 60:
            self.plotTimeSpanLabel.setText(f"{int(self.time_span)}s")
        else:
            minutes = int(self.time_span // 60)
            seconds = int(self.time_span % 60)
            self.plotTimeSpanLabel.setText(f"{minutes}m{seconds:02d}s")

    def setup_channel_selection_button(self):
        """Setup channel selection using the push button with a QMenu"""
        # Create a menu for channel selection
        self.channel_menu = QtWidgets.QMenu(self)
        self.channel_actions = {}

        # Add select all action
        select_all_action = QtWidgets.QAction("Select All", self)
        select_all_action.triggered.connect(self.select_all_channels)
        self.channel_menu.addAction(select_all_action)

        # Add deselect all action
        deselect_all_action = QtWidgets.QAction("Deselect All", self)
        deselect_all_action.triggered.connect(self.deselect_all_channels)
        self.channel_menu.addAction(deselect_all_action)

        # Add separator
        self.channel_menu.addSeparator()

        # Add checkable actions for each channel
        for channel in self.channels.keys():
            action = QtWidgets.QAction(channel, self)
            action.setCheckable(True)
            action.setChecked(False)
            action.triggered.connect(self.update_channel_selection)
            self.channel_actions[channel] = action
            self.channel_menu.addAction(action)

        # Connect the button to show the menu
        self.plotChannelsSelectionButton.setMenu(self.channel_menu)

        # Set initial button text
        self.plotChannelsSelectionButton.setText("Select Channels")

        # Make the button show the menu on click
        self.plotChannelsSelectionButton.clicked.connect(self.show_channel_menu)

    def select_all_channels(self):
        """Select all channels"""
        for action in self.channel_actions.values():
            action.setChecked(True)
        self.update_channel_selection()

    def deselect_all_channels(self):
        """Deselect all channels"""
        for action in self.channel_actions.values():
            action.setChecked(False)
        self.update_channel_selection()

    def show_channel_menu(self):
        """Show the channel selection menu when button is clicked"""
        # Show the menu below the button
        self.channel_menu.exec_(self.plotChannelsSelectionButton.mapToGlobal(
            QtCore.QPoint(0, self.plotChannelsSelectionButton.height())))

    def update_channel_selection(self):
        """Update channel selection based on menu actions"""
        self.active_channels.clear()

        # Get all checked channels
        for channel, action in self.channel_actions.items():
            if action.isChecked():
                self.active_channels.add(channel)

        # Update the plot
        self.update_plot()


# ========== Run the App ==========
if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = HMIApp()
    win.show()
    sys.exit(app.exec_())
