"""Saved GUI settings (window layouts, export choices), carried over from the IPIDS names.

SINDIE_SETTINGS_DIR keeps them in .ini files in that folder instead of the user's own settings
(the Windows registry on the lab PC). The tests set it: they open and close HMI windows, and a
window saves its dock layout when it closes - over the operator's - and starts from whatever
layout was saved last, so a hidden dock could make them fail.
"""
import os

from PyQt6.QtCore import QSettings

from src.core.env import getenv

_RENAMED = {
    ("SurreyIonBeam", "SINDIE_HMI"): ("SurreyIonBeam", "IPIDS_HMI"),
    ("SINDIE", "DataViewer"): ("IPIDS", "DataViewer"),
}


def gui_settings(organisation: str, application: str) -> QSettings:
    """QSettings under the SINDIE name; the first time, it starts from what was saved under the
    machine's old name (IPIDS), so dock layouts and export choices aren't lost by the rename."""
    folder = getenv("SETTINGS_DIR").strip()
    if folder:
        os.makedirs(folder, exist_ok=True)
        return QSettings(os.path.join(folder, f"{organisation}_{application}.ini"), QSettings.Format.IniFormat)
    st = QSettings(organisation, application)
    old = _RENAMED.get((organisation, application))
    if old and not st.allKeys() and not st.value("_migrated_from_ipids", False, type=bool):
        legacy = QSettings(*old)
        for key in legacy.allKeys():
            st.setValue(key, legacy.value(key))
        st.setValue("_migrated_from_ipids", True)
        st.sync()
    return st
