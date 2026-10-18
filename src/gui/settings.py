"""Saved GUI settings (window layouts, export choices), carried over from the IPIDS names."""
from PyQt6.QtCore import QSettings

_RENAMED = {
    ("SurreyIonBeam", "SINDIE_HMI"): ("SurreyIonBeam", "IPIDS_HMI"),
    ("SINDIE", "DataViewer"): ("IPIDS", "DataViewer"),
}


def gui_settings(organisation: str, application: str) -> QSettings:
    """QSettings under the SINDIE name; the first time, it starts from what was saved under the
    machine's old name (IPIDS), so dock layouts and export choices aren't lost by the rename."""
    st = QSettings(organisation, application)
    old = _RENAMED.get((organisation, application))
    if old and not st.allKeys() and not st.value("_migrated_from_ipids", False, type=bool):
        legacy = QSettings(*old)
        for key in legacy.allKeys():
            st.setValue(key, legacy.value(key))
        st.setValue("_migrated_from_ipids", True)
        st.sync()
    return st
