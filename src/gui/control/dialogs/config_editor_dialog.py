"""Engineering config editor: edit a config/*.json file as a tree, then apply it and restart the
service that reads it.

Safeguards (the checking, backups and git commit are in src/core/config_store.py):
  * on load, the file is checked and any problem is shown above the tree,
  * on apply, the edits are checked (types, ranges, limits the right way round...) and the
    changes are listed for confirmation with an optional reason,
  * the previous file is kept in config/backups/ ('History...' puts one back), the new one is
    written whole, and files that feed system_tags.json are rolled back if the rebuild fails,
  * the change is committed to git and recorded in the event log.
Values keep their types and nothing the tree can't show (null, empty lists) is lost on save.
"""
import json
import logging

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QInputDialog, QLabel,
                             QLineEdit, QListWidget, QMessageBox, QPushButton, QSpinBox, QTreeWidget,
                             QTreeWidgetItem, QVBoxLayout)

from src.core import config_store, paths
from src.core.config_store import REGISTRY_INPUTS, ConfigError

log = logging.getLogger("hmi.config_editor")

CONFIG_TO_SERVICE = {
    "alarm_config.json": "service_alarms",
    "fault_config.json": "ALL SERVICES",
    "network_config.json": "ALL SERVICES",
    "logger_config.json": "service_logger",
    "magnet_config.json": "service_magnet_psu",
    "steerer_magnet_config.json": "service_steerer_magnet_psu",
    "smu_config.json": "service_smu",
    "readback_tolerance.json": "service_monitor",
    "mpd_config.json": "service_spellman_mpd",
    "vac_gauges_config.json": "service_vac_gauge_controllers",
    "plc_config.json": "service_plc",
    "theme_config.json": "HMI_GUI",
}
# Used for 'ALL SERVICES' when the service manager isn't reporting its list
KNOWN_SERVICES = ["service_events", "service_logger", "service_data_compactor", "service_plc", "service_monitor",
                  "service_alarms", "service_vac_gauge_controllers", "service_source_turbo", "service_magnet_psu",
                  "service_steerer_magnet_psu", "service_spellman_mpd", "service_smu", "service_sequencer"]
INT_MIN, INT_MAX = -2147483648, 2147483647


class SpinBox(QSpinBox):
    """Ignores the mouse wheel unless it has focus: scrolling down the tree must not change ports
    or counts it happens to pass over."""

    def __init__(self):
        super().__init__()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


def format_float(value: float) -> str:
    """Shortest text that reads back as exactly this float (the old '%g' kept 6 digits, so saving
    a file rounded every value with more)."""
    return repr(float(value))


class ConfirmChangesDialog(QDialog):
    def __init__(self, title: str, intro: str, changes: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(640, 380)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(intro))
        self.list = QListWidget()
        self.list.addItems(changes)
        layout.addWidget(self.list)
        layout.addWidget(QLabel("Reason (kept in the change history):"))
        self.reason = QLineEdit()
        self.reason.setPlaceholderText("e.g. new PSU fitted, limits from the datasheet")
        layout.addWidget(self.reason)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)


class ConfigEditorDialog(QDialog):
    def __init__(self, cmd_thread, parent=None, config_dir=None, services=None, events=None, rebuild=None,
                 commit=True):
        """services: callable -> names of the services the manager runs (for 'ALL SERVICES');
        events: EventHelper for the change record; rebuild: callable -> error text or None."""
        super().__init__(parent)
        self.setWindowTitle("System Configuration Editor")
        self.resize(760, 760)
        self.cmd_thread = cmd_thread
        self.config_dir = config_dir or paths.CONFIG_DIR
        self.services = services
        self.events = events
        self.rebuild = rebuild or config_store.rebuild_registry
        self.commit = commit
        self.original = None            # the file as loaded (None: nothing loaded / unreadable)
        self._leaves = []               # (tree item, path tuple, original value)
        self._renamable = []            # (tree item, path of its dict, original key)

        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("<b>Target Config:</b>"))
        self.cb_files = QComboBox()
        self.cb_files.addItems(config_store.config_files(self.config_dir))
        self.cb_files.currentTextChanged.connect(self._load_file)
        top.addWidget(self.cb_files)
        top.addWidget(QLabel("<b>Target Service:</b>"))
        self.cb_service = QComboBox()
        self.cb_service.addItems(["ALL SERVICES"] + KNOWN_SERVICES)
        self.cb_service.currentTextChanged.connect(self._update_button_text)
        top.addWidget(self.cb_service)
        layout.addLayout(top)

        self.lbl_problems = QLabel()
        self.lbl_problems.setWordWrap(True)
        self.lbl_problems.setTextFormat(Qt.TextFormat.PlainText)
        self.lbl_problems.setStyleSheet("background-color: #FFEBEE; color: #B71C1C; border: 1px solid #E57373; "
                                        "border-radius: 4px; padding: 6px;")
        self.lbl_problems.hide()
        layout.addWidget(self.lbl_problems)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Configuration Key", "Value"])
        self.tree.setColumnWidth(0, 300)
        self.tree.setAlternatingRowColors(True)
        layout.addWidget(self.tree)

        bottom = QHBoxLayout()
        self.btn_history = QPushButton("🕘 History...")
        self.btn_history.setToolTip("Put back a previous version of this file (every save keeps one).")
        self.btn_history.clicked.connect(self._show_history)
        bottom.addWidget(self.btn_history)
        self.btn_save = QPushButton("💾 APPLY CHANGES && RESTART SERVICE")
        self.btn_save.setStyleSheet("background-color: #2196F3; color: white; font-weight: bold; padding: 10px; "
                                    "font-size: 11pt;")
        self.btn_save.clicked.connect(self._save_and_restart)
        bottom.addWidget(self.btn_save, stretch=1)
        layout.addLayout(bottom)

        if self.cb_files.count() > 0:
            self._load_file(self.cb_files.currentText())

    # ------------------------------------------------------------------ loading
    def _update_button_text(self, svc_name=None):
        svc_name = svc_name or self.cb_service.currentText()
        if svc_name == "ALL SERVICES":
            self.btn_save.setText("💾 APPLY CHANGES && RESTART ALL SERVICES")
        elif svc_name == "HMI_GUI":
            self.btn_save.setText("💾 APPLY CHANGES (RESTART THE HMI AFTERWARDS)")
        else:
            self.btn_save.setText(f"💾 APPLY CHANGES && RESTART {svc_name.upper()}")

    def _show_problems(self, problems, heading):
        if not problems:
            self.lbl_problems.hide()
            return
        shown = problems[:10]
        more = f"\n...and {len(problems) - 10} more" if len(problems) > 10 else ""
        self.lbl_problems.setText(heading + "\n• " + "\n• ".join(shown) + more)
        self.lbl_problems.show()

    def _load_file(self, filename):
        if not filename:
            return
        target_svc = CONFIG_TO_SERVICE.get(filename, "ALL SERVICES")
        if self.cb_service.findText(target_svc) == -1:
            self.cb_service.addItem(target_svc)
        self.cb_service.setCurrentText(target_svc)
        self._update_button_text(target_svc)

        self.tree.clear()
        self.original, self._leaves, self._renamable = None, [], []
        try:
            data, problems = config_store.load(filename, self.config_dir)
        except ConfigError as e:
            self._show_problems([str(e)], "This file can't be read, so it can't be edited here. The services "
                                          "can't read it either - use History... to put back a working version.")
            self.btn_save.setEnabled(False)
            return
        self.original = data
        self._populate_tree(self.tree.invisibleRootItem(), data, ())
        self.tree.expandAll()
        self.btn_save.setEnabled(True)
        self._show_problems(problems, "Problems in the file as it is now (the services are using it - fix them "
                                      "here, or put back a working version with History...):")

    def _populate_tree(self, parent_node, data, path):
        if isinstance(data, dict):
            renamable = bool(path) and path[-1] == "devices"
            for key, value in data.items():
                item = QTreeWidgetItem(parent_node)
                item.setText(0, str(key))
                if renamable:
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
                    item.setBackground(0, QColor("#E3F2FD"))
                    item.setToolTip(0, "Double click to rename this device key.")
                    self._renamable.append((item, path, key))
                self._populate_child(item, key, value, path + (key,))
        elif isinstance(data, list):
            for i, value in enumerate(data):
                item = QTreeWidgetItem(parent_node)
                item.setText(0, f"[{i}]")
                self._populate_child(item, f"[{i}]", value, path + (i,))

    def _populate_child(self, item, key, value, path):
        if isinstance(value, (dict, list)) and value:
            self._populate_tree(item, value, path)
        elif isinstance(value, (dict, list)):
            item.setText(1, "(empty)")
            item.setForeground(1, QColor("#9E9E9E"))
        else:
            self._add_edit_widget(item, key, value, path)

    def _add_edit_widget(self, item, key, value, path):
        if isinstance(value, bool):
            w = QCheckBox()
            w.setChecked(value)
        elif isinstance(value, int) and INT_MIN <= value <= INT_MAX:
            w = SpinBox()
            w.setRange(INT_MIN, INT_MAX)
            w.setValue(value)
        elif isinstance(value, float):
            w = QLineEdit(format_float(value))
        elif value is None:
            w = QLineEdit("null")
            w.setToolTip("null in the file: leave as null, or type a number or text")
        else:
            w = QLineEdit(str(value))
        if key == "bus_id":
            w.setEnabled(False)
            w.setToolTip("Bus ID cannot be changed as it maps to physical hardware and network configs.")
        self.tree.setItemWidget(item, 1, w)
        self._leaves.append((item, path, value))

    # ------------------------------------------------------------------ edits
    @staticmethod
    def _label(path):
        return "".join(f"[{p}]" if isinstance(p, int) else (f".{p}" if i else str(p)) for i, p in enumerate(path))

    def _value_of(self, widget, original, path):
        if isinstance(widget, QCheckBox):
            return widget.isChecked()
        if isinstance(widget, QSpinBox):
            return widget.value()
        text = widget.text().strip() if isinstance(widget, QLineEdit) else ""
        if isinstance(original, bool):
            return original
        if isinstance(original, int):
            try:
                return int(text)
            except ValueError:
                raise ValueError(f"{self._label(path)}: '{text}' is not a whole number") from None
        if isinstance(original, float):
            try:
                value = float(text)
            except ValueError:
                raise ValueError(f"{self._label(path)}: '{text}' is not a number") from None
            if value != value or value in (float("inf"), float("-inf")):
                raise ValueError(f"{self._label(path)}: must be a finite number")
            return value
        if original is None:
            if text in ("", "null"):
                return None
            try:
                return json.loads(text)
            except ValueError:
                return text
        return widget.text() if isinstance(widget, QLineEdit) else original

    def edited_data(self):
        """The file as edited (raises ValueError with the field and the problem)."""
        leaves = {path: self._value_of(self.tree.itemWidget(item, 1), orig, path)
                  for item, path, orig in self._leaves}
        renames = {}
        for item, dict_path, old_key in self._renamable:
            new_key = item.text(0).strip()
            if not new_key:
                raise ValueError(f"{self._label(dict_path)}: a device name is empty")
            if new_key != old_key:
                renames.setdefault(dict_path, {})[old_key] = new_key
        return config_store.apply_edits(self.original, leaves, renames)

    # ------------------------------------------------------------------ applying
    def _service_list(self):
        names = []
        if callable(self.services):
            try:
                names = [n for n in self.services() if isinstance(n, str)]
            except (TypeError, ValueError, AttributeError) as e:
                log.warning("Service list not available: %s", e)
        return names or list(KNOWN_SERVICES)

    def _restart(self, target):
        if target == "HMI_GUI":
            return "Restart the operator HMI for the change to take effect."
        names = self._service_list() if target == "ALL SERVICES" else [target]
        for svc in names:
            self.cmd_thread.send_command("manager", "manager_cmd", {"command": "restart", "service": svc})
        return f"Restart requested for {'all services' if target == 'ALL SERVICES' else target}."

    def _record(self, filename, changes, reason, result, what="changed"):
        log.warning("Config %s %s: %s (reason: %s; backup %s; git %s)", filename, what, "; ".join(changes),
                    reason or "-", result.backup, result.git)
        if self.events is not None:
            text = f"Config {filename} {what}: " + "; ".join(changes[:4]) + (" ..." if len(changes) > 4 else "")
            self.events.log_general(text + (f" ({reason})" if reason else ""), severity="WARNING",
                                    event_type="CONFIG_CHANGE",
                                    metadata={"file": filename, "changes": changes, "reason": reason,
                                              "backup": result.backup, "git": result.git})

    def _apply(self, filename, data, original, changes, title, intro, what):
        """Confirm, save (or restore), record, restart. True if it was applied."""
        target = self.cb_service.currentText()
        dlg = ConfirmChangesDialog(title, intro, changes, self)
        if not dlg.exec():
            return False
        reason = dlg.reason.text().strip()
        after = self.rebuild if filename in REGISTRY_INPUTS else None
        result = config_store.save(filename, data, original=original, reason=reason, config_dir=self.config_dir,
                                   after_write=after, commit=self.commit)
        if not result.saved:
            detail = "\n".join(result.problems) if result.problems else result.error
            QMessageBox.critical(self, "Not Applied", f"{filename} was not changed:\n\n{detail}")
            return False
        self._record(filename, changes, reason, result, what)
        lines = [f"Saved {filename}" + (" and rebuilt system_tags.json." if after else "."),
                 f"Previous version kept: {result.backup}" if result.backup else "",
                 f"Committed to git: {result.git}" if result.committed else f"Not committed to git: {result.git}",
                 self._restart(target)]
        QMessageBox.information(self, "Applied", "\n".join(line for line in lines if line))
        return True

    def _save_and_restart(self):
        filename = self.cb_files.currentText()
        if not filename or self.original is None:
            return
        try:
            data = self.edited_data()
        except ValueError as e:
            QMessageBox.warning(self, "Not Applied", f"Check your inputs:\n\n{e}")
            return
        problems = config_store.check(filename, data, self.original)
        if problems:
            QMessageBox.warning(self, "Not Applied", f"{filename} would have problems the services trip over:"
                                                     "\n\n• " + "\n• ".join(problems))
            return
        changes = config_store.renamed_diff(self.original, data)
        if not changes:
            QMessageBox.information(self, "Nothing To Apply", "No values were changed.")
            return
        if self._apply(filename, data, self.original, changes, "Apply Changes",
                       f"<b>{filename}</b>: {len(changes)} change(s). Apply them?", "changed"):
            self.accept()

    def _show_history(self):
        filename = self.cb_files.currentText()
        if not filename:
            return
        found = config_store.backups(filename, self.config_dir)
        if not found:
            QMessageBox.information(self, "History", f"No earlier versions of {filename} have been kept yet "
                                                     "(one is kept every time it is applied here).")
            return
        labels = [config_store.backup_time(p) for p in found]
        choice, ok = QInputDialog.getItem(self, "Put Back a Previous Version",
                                          f"{filename} as it was before the change saved at:", labels, 0, False)
        if not ok:
            return
        path = found[labels.index(choice)]
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            QMessageBox.critical(self, "History", f"Could not read {path}:\n{e}")
            return
        problems = config_store.check(filename, data)
        if problems:
            QMessageBox.warning(self, "History", "That version has problems the services would trip over:\n\n• "
                                + "\n• ".join(problems))
            return
        current = self.original if self.original is not None else {}
        changes = config_store.renamed_diff(current, data) or ["(identical to the current file)"]
        if self._apply(filename, data, None, changes, "Put Back a Previous Version",
                       f"<b>{filename}</b>: put back the version from before {choice}?", "restored"):
            self._load_file(filename)
