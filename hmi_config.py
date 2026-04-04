# hmi_config.py
"""
Global Static Configuration Constants
No imports of UI (PyQt) or Hardware (Snap7) libraries are permitted here.
"""
import os

# --- PLC / HARDWARE CONFIGURATION ---
PLC_IP_ADDRESS = "192.168.0.10"
PLC_RACK = 0
PLC_SLOT = 1

HMI_DB_NUM = 1
MESSAGES_DB_NUM = 3
MESSAGES_DB_LENGTH = 200

# --- FILE PATHS ---
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
HMI_DB_DOCUMENT_ADDRESS = os.path.join(PROJECT_ROOT, "utils", "hmiDB.db")
DEFAULT_PLOT_DIRECTORY = os.path.join(PROJECT_ROOT, "PlotData")
DEFAULT_LOG_DIRECTORY = os.path.join(PROJECT_ROOT, "LogData")
UI_LAYOUT_FILE = os.path.join(PROJECT_ROOT, "utils", "hmi_layout.ui")

# --- SYSTEM TIMING ---
LOG_INTERVAL = 5        # Background logging interval (Seconds)
PLOT_REFRESH_HZ = 10    # GUI plot refresh rate
GUI_REFRESH_HZ = 50     # GUI general refresh rate
HMI_REFRESH_TIMER = 50  # Legacy timer reference

# --- PHYSICAL HARDWARE LIMITS ---
IONISER_W_MAX = 300
TARGET_V_MAX = 10000
EXTRACTION_V_MAX = 20000
CESIUM_C_MAX = 150

# --- GUI CONSTANTS ---
NIGHT_MODE = True
PLOT_SAVE_SCALING = 1

# Tabs
TAB_OVERVIEW = 0
TAB_ION_SOURCE = 1
TAB_VACUUM = 2
TAB_BEAMLINE = 3
TAB_ENDSTATION = 4

# Indicators
INDICATOR_OFF = 0
INDICATOR_ON = 1
INDICATOR_FAULT = 2

# Buttons
BUTTON_OFF = 0
BUTTON_ON = 1
BUTTON_DISABLED = 2