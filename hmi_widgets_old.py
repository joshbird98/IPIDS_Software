# For the various GUI lights and icons that need to change
class Indicator:
    def __init__(self, name, QLabel, offImage, onImage, faultImage=None, defaultMode=INDICATOR_OFF):
        self.name = name
        self.label = QLabel
        self.offImage = offImage
        self.onImage = onImage
        self.faultImage = faultImage
        self.mode = None
        self.setMode(defaultMode)

    def setMode(self, newMode):
        if newMode != self.mode:
            if newMode is not None:
                if newMode == INDICATOR_ON:
                    self.mode = newMode
                    self.turnOn()
                    return True
                elif newMode == INDICATOR_OFF:
                    self.mode = newMode
                    self.turnOff()
                    return True
                elif (newMode >= INDICATOR_FAULT) & (self.faultImage is not None):
                    self.mode = newMode
                    self.turnFault()
                    return True
                else:
                    print(f"Indicator {self.name} tried to set as unimplemented mode")
                    return False
            elif self.faultImage is not None:
                self.turnFault()
            else:
                self.turnOff()
                return False
        return False

    def turnOn(self):
        self.label.setPixmap(self.onImage)

    def turnOff(self):
        self.label.setPixmap(self.offImage)

    def turnFault(self):
        self.label.setPixmap(self.faultImage)

# For the various GUI buttons which have a changing function / appearance
class Button:
    def __init__(self, name, QPushButton, function, offText, offColour, onText, onColour, disabledText=None, disabledColour=None, defaultMode=BUTTON_OFF):
        self.name = name
        self.button = QPushButton
        self.button.clicked.connect(function)
        self.offText = offText
        self.offColour = offColour
        self.onText = onText
        self.onColour = onColour
        self.disabledText = disabledText
        self.disabledColour = disabledColour
        self.mode = None
        self.setMode(defaultMode)

    def toggleMode(self):
        if self.mode == BUTTON_OFF:
            newMode = BUTTON_ON
        else:
            newMode = BUTTON_OFF
        self.setMode(newMode)

    def setMode(self, newMode):
        if newMode != self.mode:
            if newMode == BUTTON_ON:
                self.mode = newMode
                self.turnOn()
                return True
            elif newMode == BUTTON_OFF:
                self.mode = newMode
                self.turnOff()
                return True
            elif newMode == BUTTON_DISABLED and self.disabledText and self.disabledColour:
                self.mode = newMode
                self.turnDisabled()
                return True
            else:
                print(f"Indicator {self.name} tried to set as unimplemented mode: {newMode}")
                return False
        return False

    def turnOn(self):
        self.button.setText(self.onText)
        self.button.setStyleSheet(f"background-color: {self.onColour}; color: white; font-weight: bold;")

    def turnOff(self):
        self.button.setText(self.offText)
        self.button.setStyleSheet(f"background-color: {self.offColour}; color: white; font-weight: bold;")

    def turnDisabled(self):
        self.button.setText(self.disabledText)
        self.button.setStyleSheet(f"background-color: {self.disabledColour}; color: white; font-weight: bold;")

# For the labels that dynamically need to display readback values
class ReadbackLabel:
    def __init__(self, name, QLabel, units, defaultValue=False):
        self.name = name
        self.label = QLabel
        self.units = units
        self.value = None
        self.writeValue(defaultValue)

    def writeValue(self, newValue):
        if newValue != self.value:
            self.value = newValue
            if not self.value and (self.value != 0.0):
                self.label.setText("---")
            else:
                try:
                    if 1e-2 <= abs(self.value) <= 1e5:
                        self.label.setText(f"{self.value:.2f} {self.units}") # Decimal notation
                    else:
                        self.label.setText(f"{self.value:.2e} {self.units}") # Scientific notation
                except TypeError:
                    self.label.setText("---")
                    print(f"Incorrect type sent to {self.name} label.")
                    return False
            return True
        return False

# For the entry fields that then can write data to the PLC
class NumberEntry:
    def __init__(self, name, QLineEdit, hmi, messageHelper, parentKey, enablerTagKey, decimals=2, defaultValue=False, defaultEnabled = False):
        self.name = name
        self.numberEntry = QLineEdit
        self.hmi = hmi
        self.parentKey = parentKey
        self.messageHelper = messageHelper
        self.enablerTagKey = enablerTagKey
        self.decimals = decimals
        self.minValue = None
        self.maxValue = None
        self.validator = None
        self.updateMinMaxValues()
        if (self.minValue is not None) and (self.maxValue is not None):
            self.validator = QDoubleValidator(self.minValue, self.maxValue, int(self.decimals))  # min, max, 2 decimals
            self.validator.setNotation(QDoubleValidator.StandardNotation)
            self.numberEntry.setValidator(self.validator)
        self.numberEntry.returnPressed.connect(self.writeOutValueToPLC)
        self.enabled = None
        self.updateEnabled()
        self.writeValue(defaultValue)

    def writeValue(self, newValue):
        if not self.enabled:
            try:
                if (newValue or (newValue == 0.0)) and newValue is not None :
                    newValue = float(newValue)
                    self.numberEntry.setText(f"{newValue:.2f}")
                else:
                    self.numberEntry.setText("---")
            except ValueError:
                print(f"Error writing {newValue} to float when setting text for {self.name}")

    def writeOutValueToPLC(self):
        self.updateMinMaxValues()
        try:
            value = max(self.minValue, min(float(self.numberEntry.text()), self.maxValue)) # type: ignore

            # Update QLineEdit to show clamped value (2 decimals)
            self.writeValue(value)
            self.messageHelper.addToMessageBox(f"Accepted value: {value}", "INFO", True)
            if self.hmi.write_tag(self.parentKey, value):
                return True
            else:
                self.messageHelper.addToMessageBox("Write to setpoint failed", "WARN", True)
            return False

        except ValueError:
            # Reset if input was not numeric
            self.writeValue(False)
            self.messageHelper.addToMessageBox("Invalid input", "WARN", True)
            return False

    def updateEnabled(self):
        previousEnabled = self.enabled
        self.enabled = self.hmi.tags[self.enablerTagKey]["value"]

        self.numberEntry.setReadOnly(not self.enabled)
        if (self.enabled != previousEnabled) or (not self.enabled):
                self.writeValue(self.hmi.tags[self.parentKey]["value"]) # ensures internal PLC setpoint is flushed to entry field when HMI disabled or transitioning to enabled
        return self.enabled

    def updateMinMaxValues(self):
        self.minValue = self.hmi.tags[self.parentKey]["minValue"]
        self.maxValue = self.hmi.tags[self.parentKey]["maxValue"]
        if (self.minValue is not None) and (self.maxValue is not None):
            self.validator = QDoubleValidator(self.minValue, self.maxValue, int(self.decimals))  # min, max, 2 decimals
            self.validator.setNotation(QDoubleValidator.StandardNotation)
            self.numberEntry.setValidator(self.validator)

# ========== Main HMI Window ==========
