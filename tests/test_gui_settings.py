"""Saved window layouts: the tests keep theirs out of the user's settings (src/gui/settings.py)."""
import os


def test_the_tests_layouts_stay_out_of_the_users_settings(tmp_path, monkeypatch):
    from src.gui.settings import gui_settings
    assert gui_settings("SurreyIonBeam", "SINDIE_HMI").fileName().startswith(os.environ["SINDIE_SETTINGS_DIR"])
    monkeypatch.setenv("SINDIE_SETTINGS_DIR", str(tmp_path / "layouts"))
    st = gui_settings("SurreyIonBeam", "SINDIE_HMI")
    st.setValue("windowState", b"x")
    st.sync()
    assert (tmp_path / "layouts" / "SurreyIonBeam_SINDIE_HMI.ini").exists()
    assert gui_settings("SurreyIonBeam", "SINDIE_HMI").value("windowState") == b"x"
