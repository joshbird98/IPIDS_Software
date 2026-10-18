"""Workspace profiles: named sets of channels and lanes, shared by every viewer window.

They live in config/workspace_profiles.json (per machine, not in git - like plot_settings.json).
They used to be written to src/gui/config/, a folder that only existed by accident; profiles
found there are copied across once, never overwriting one with the same name.

Every change is a read-modify-write of the file, so several open windows don't overwrite each
other's profiles.
"""
import copy
import json
import logging
import os

from PyQt6.QtWidgets import QInputDialog, QMenu, QMessageBox

from src.core import paths

PROFILES_FILE = paths.config_path("workspace_profiles.json")
LEGACY_PROFILES_FILE = os.path.join(paths.PROJECT_ROOT, "src", "gui", "config", "workspace_profiles.json")

log = logging.getLogger("viewer.profiles")


def read_profiles(path: str) -> dict:
    """The profiles saved in `path` ({} if there is no file; a broken file is logged, not fatal)."""
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.error("Workspace profiles %s not readable: %s", path, e)
        return {}
    return data if isinstance(data, dict) else {}


def write_profiles(path: str, profiles: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(profiles, f, indent=4)
    os.replace(tmp, path)


def migrate_legacy_profiles(legacy: str = LEGACY_PROFILES_FILE, target: str = PROFILES_FILE) -> list:
    """Copy profiles from the old location that the new file doesn't have. Returns their names."""
    old = read_profiles(legacy)
    if not old:
        return []
    current = read_profiles(target)
    new_names = [name for name in old if name not in current]
    if new_names:
        current.update({name: old[name] for name in new_names})
        try:
            write_profiles(target, current)
        except OSError as e:
            log.error("Could not copy the old workspace profiles to %s: %s", target, e)
            return []
        log.warning("Workspace profiles %s copied from %s to %s - check them, then delete the old file",
                    ", ".join(new_names), legacy, target)
    return new_names


class ProfilesMixin:
    """Mixed into DataViewerApp (viewer_main.py). Needs self.profiles_file, self.plot_config,
    self.combo_profiles and self.cache."""

    def _load_all_profiles(self):
        if self.profiles_file == PROFILES_FILE:
            migrate_legacy_profiles()
        profiles = {"Default": {}}
        profiles.update(read_profiles(self.profiles_file))
        return profiles

    def _save_current_profile(self):
        """Saves the current lane and channel configuration as a reusable profile."""
        name, ok = QInputDialog.getText(self, "Save Profile", "Enter profile name:")
        if not ok or not name.strip():
            return
        name = name.strip()
        self.profiles[name] = copy.deepcopy(self.plot_config)
        if self.combo_profiles.findText(name) == -1:
            self.combo_profiles.addItem(name)
        self.combo_profiles.setCurrentText(name)
        # Read-modify-write: keep what other windows saved meanwhile
        disk_profiles = read_profiles(self.profiles_file)
        disk_profiles[name] = copy.deepcopy(self.plot_config)
        try:
            write_profiles(self.profiles_file, disk_profiles)
        except OSError as e:
            QMessageBox.critical(self, "Error", f"Failed to save profile:\n{e}")
            return
        self.profiles = {"Default": {}, **disk_profiles}
        QMessageBox.information(self, "Success", f"Profile '{name}' saved successfully.")

    def _apply_profile(self):
        name = self.combo_profiles.currentText()
        if name in self.profiles:
            self.plot_config = copy.deepcopy(self.profiles[name])
            self.cache.register_client_tags(self.client_id, list(self.plot_config.keys()))
            self.request_ui_refresh()

    def _show_profile_context_menu(self, pos):
        """Right-click menu for an item inside the profile dropdown."""
        index = self.combo_profiles.view().indexAt(pos)
        if not index.isValid():
            return
        profile_name = self.combo_profiles.itemText(index.row())
        if not profile_name or profile_name == "Default":       # the Default profile stays
            return
        menu = QMenu(self)
        delete_action = menu.addAction(f"Delete '{profile_name}'")
        action = menu.exec(self.combo_profiles.view().viewport().mapToGlobal(pos))
        if action == delete_action:
            self._delete_profile(profile_name)

    def _delete_profile(self, profile_name):
        """Remove a profile from the file (for every window) and from this window's list."""
        reply = QMessageBox.question(
            self, 'Delete Profile',
            f"Are you sure you want to delete '{profile_name}'?\nThis will affect all open windows.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        disk_profiles = read_profiles(self.profiles_file)
        if profile_name in disk_profiles:
            del disk_profiles[profile_name]
            try:
                write_profiles(self.profiles_file, disk_profiles)
            except OSError as e:
                QMessageBox.critical(self, "Error", f"Failed to delete profile from disk:\n{e}")
                return
        self.profiles = {"Default": {}, **disk_profiles}
        idx = self.combo_profiles.findText(profile_name)
        if idx != -1:
            self.combo_profiles.removeItem(idx)
        self.combo_profiles.setCurrentText("Default")
        self._apply_profile()
