"""Engineering config editor: edit a config/*.json file as a tree, then apply it and restart what
reads it.

A change is saved as this machine's lab settings (config/lab/<file>, only what differs from the
repository's file; src/core/lab_config.py): the repository's file changes only with software
updates, which keep the lab settings. Nothing is committed to git.

Safeguards (the checking and the backups are in src/core/config_store.py):
  * on load, the file is checked and any problem is shown above the tree,
  * on apply, the edits are checked (types, ranges, limits the right way round...), the changes
    are listed for confirmation with an optional reason, and a number that changes by more than
    a factor of 3 needs a tick to confirm it was meant (95.0 typed as 950),
  * a value over a hard cap (src/core/hard_caps.py: VG relays, Spellman, magnet, SMU) is refused
    outright - no tick gets past it - and its field is marked red with the reason as tooltip,
  * nothing is written if the file changed since it was opened (a hand edit or a software update
    would otherwise be silently undone),
  * the file as it was is kept in config/backups/ ('History...' puts one back), and the lab
    settings of files that feed system_tags.json are put back if the rebuild fails,
  * the change is recorded in the event log,
  * 'Repository values' takes this machine's lab settings for the file away (backed up first).
Keys can't be renamed or added here (the services look things up by name). Numbers keep full
precision; nulls and empty lists are kept.
"""
import html
import json
import logging
import math

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QInputDialog, QLabel,
                             QLineEdit, QListWidget, QMessageBox, QPushButton, QTreeWidget, QTreeWidgetItem,
                             QVBoxLayout)

from src.core import config_store, hard_caps, lab_config, paths
from src.gui import theme
from src.gui.style import set_prop, set_role, set_tone
from src.core.config_store import REGISTRY_INPUTS, ConfigError

log = logging.getLogger("hmi.config_editor")

WHOLE_SYSTEM = "WHOLE SYSTEM (by hand)"
ALL_SERVICES = "ALL SERVICES"
HMI = "HMI_GUI"
BUILDER = "RECIPE BUILDER (nothing to restart)"
FAULT_READERS = "service_plc + service_alarms"
SERVICE_GROUPS = {FAULT_READERS: ("service_plc", "service_alarms")}
CONFIG_TO_SERVICE = {
    "alarm_config.json": "service_alarms",
    # read by the PLC and alarm services, and by the HMI's fault list (every service's event helper
    # loads it too, but only the PLC service logs faults) - so not every service is restarted
    "fault_config.json": FAULT_READERS,
    # every program - the manager and the HMI too - reads its ports when it starts: restarting
    # the services alone would leave them talking past the manager (which then restarts them...)
    "network_config.json": WHOLE_SYSTEM,
    "logger_config.json": "service_logger",
    "magnet_config.json": "service_magnet_psu",
    "steerer_magnet_config.json": "service_steerer_magnet_psu",
    "smu_config.json": "service_smu",
    "readback_tolerance.json": "service_monitor",
    "mpd_config.json": "service_spellman_mpd",
    "vac_gauges_config.json": "service_vac_gauge_controllers",
    "plc_config.json": "service_plc",
    "turbo_guard.json": "service_source_turbo",       # (a trip it has made stays, in data/turbo_guard_state.json)
    # drafting's settings: read each time a recipe builder window opens
    "recipe_ai.json": BUILDER,
}
HMI_TOO = {"fault_config.json"}          # the HMI reads these at start-up as well
SERVICE_CHOICES = ["service_events", "service_logger", "service_data_compactor", "service_plc", "service_monitor",
                   "service_alarms", "service_vac_gauge_controllers", "service_source_turbo", "service_magnet_psu",
                   "service_steerer_magnet_psu", "service_spellman_mpd", "service_smu", "service_sequencer"]


CAP_TONE = "fault"          # a field past a hard cap: red outline (theme), the reason in its tooltip


def format_float(value: float) -> str:
    """Shortest text that reads back as exactly this float (the old '%g' kept 6 digits, so saving
    a file rounded every value with more)."""
    return repr(float(value))


def parse_number(text: str):
    """'5' -> 5, '5.5' / '2e-4' -> float; ValueError otherwise (also for nan / inf)."""
    try:
        return int(text)
    except ValueError:
        value = float(text)
    if not math.isfinite(value):
        raise ValueError("not a finite number")
    return value


class ConfirmChangesDialog(QDialog):
    def __init__(self, title: str, intro: str, changes: list, parent=None, large: list = ()):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(660, 420)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(intro))
        self.list = QListWidget()
        self.list.addItems(changes)
        layout.addWidget(self.list)
        self.large = None
        if large:
            warn = QLabel("<b>Large changes - check these are meant:</b><br>"
                          + "<br>".join(html.escape(x) for x in large))
            set_role(warn, "note")
            set_prop(warn, "level", "warn")
            warn.setWordWrap(True)
            layout.addWidget(warn)
            self.large = QCheckBox("I have checked the large changes")
            layout.addWidget(self.large)
        layout.addWidget(QLabel("Reason (kept in the change history):"))
        self.reason = QLineEdit()
        self.reason.setPlaceholderText("e.g. new PSU fitted, limits from the datasheet")
        layout.addWidget(self.reason)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        if self.large is not None:
            ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
            ok.setEnabled(False)
            self.large.toggled.connect(ok.setEnabled)


class ConfigEditorDialog(QDialog):
    def __init__(self, cmd_thread, parent=None, config_dir=None, services=None, events=None, rebuild=None):
        """services: callable -> {service name: state} as the manager reports them (for 'ALL
        SERVICES'); events: EventHelper for the change record; rebuild: callable -> error text or None."""
        super().__init__(parent)
        self.setWindowTitle("System Configuration Editor")
        self.resize(760, 760)
        self.cmd_thread = cmd_thread
        self.config_dir = config_dir or paths.CONFIG_DIR
        self.services = services
        self.events = events
        self.rebuild = rebuild or config_store.rebuild_registry
        self.original = None            # the file as loaded (None: nothing loaded / unreadable)
        self._leaves = []               # (tree item, path tuple, original value)

        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("<b>Target Config:</b>"))
        self.cb_files = QComboBox()
        self.cb_files.addItems(config_store.config_files(self.config_dir))
        self.cb_files.currentTextChanged.connect(self._load_file)
        top.addWidget(self.cb_files)
        lbl_restart = QLabel("Restart")
        set_role(lbl_restart, "caption")
        top.addWidget(lbl_restart)
        self.cb_service = QComboBox()
        self.cb_service.addItems([ALL_SERVICES] + SERVICE_CHOICES)
        self.cb_service.currentTextChanged.connect(self._update_button_text)
        top.addWidget(self.cb_service)
        layout.addLayout(top)

        self.lbl_problems = QLabel()
        self.lbl_problems.setWordWrap(True)
        self.lbl_problems.setTextFormat(Qt.TextFormat.PlainText)
        set_role(self.lbl_problems, "note")
        set_prop(self.lbl_problems, "level", "fault")
        self.lbl_problems.hide()
        layout.addWidget(self.lbl_problems)

        self.lbl_lab = QLabel()            # what this machine changes in the repository's file
        self.lbl_lab.setWordWrap(True)
        self.lbl_lab.setTextFormat(Qt.TextFormat.PlainText)
        set_role(self.lbl_lab, "caption")
        layout.addWidget(self.lbl_lab)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Configuration Key", "Value"])
        self.tree.setColumnWidth(0, 300)
        self.tree.setAlternatingRowColors(True)
        layout.addWidget(self.tree)

        bottom = QHBoxLayout()
        self.btn_history = QPushButton("History...")
        self.btn_history.setToolTip("Put back a previous version of this file (every save keeps one).")
        self.btn_history.clicked.connect(self._show_history)
        bottom.addWidget(self.btn_history)
        self.btn_repo = QPushButton("Repository values...")
        self.btn_repo.setToolTip("Take this machine's lab settings for this file away: the file as the repository has "
                                 "it (backed up first, like any change).")
        self.btn_repo.clicked.connect(self._use_repository_values)
        bottom.addWidget(self.btn_repo)
        self.btn_save = QPushButton("APPLY CHANGES && RESTART SERVICE")
        set_tone(self.btn_save, "primary")
        self.btn_save.setMinimumHeight(34)
        self.btn_save.clicked.connect(self._save_and_restart)
        bottom.addWidget(self.btn_save, stretch=1)
        layout.addLayout(bottom)

        if self.cb_files.count() > 0:
            self._load_file(self.cb_files.currentText())

    # ------------------------------------------------------------------ loading
    def _update_button_text(self, svc_name=None):
        svc_name = svc_name or self.cb_service.currentText()
        if svc_name == ALL_SERVICES:
            self.btn_save.setText("APPLY CHANGES && RESTART THE RUNNING SERVICES")
        elif svc_name in (HMI, WHOLE_SYSTEM):
            self.btn_save.setText("APPLY CHANGES (THEN RESTART BY HAND)")
        elif svc_name == BUILDER:
            self.btn_save.setText("APPLY CHANGES (USED THE NEXT TIME THE RECIPE BUILDER OPENS)")
        else:
            self.btn_save.setText(f"APPLY CHANGES && RESTART {svc_name.upper()}")

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
        target_svc = CONFIG_TO_SERVICE.get(filename, ALL_SERVICES)
        if self.cb_service.findText(target_svc) == -1:
            self.cb_service.addItem(target_svc)
        self.cb_service.setCurrentText(target_svc)
        self._update_button_text(target_svc)

        self.tree.clear()
        self.original, self._leaves = None, []
        self._show_lab(filename)
        try:
            data, problems = config_store.load(filename, self.config_dir)
        except ConfigError as e:
            repository = config_store.repository_problem(filename, self.config_dir)
            if repository:
                self._show_problems([repository], "This file can't be read, so it can't be edited here. The "
                                                  "services can't read it either:")
            else:
                self._show_problems([str(e)], "This machine's lab settings for the file can't be read, so it can't "
                                              "be edited here. The services can't read it either - use History... "
                                              "to put back a working version.")
            self.btn_save.setEnabled(False)
            return
        self.original = data
        self._populate_tree(self.tree.invisibleRootItem(), data, ())
        self.tree.expandAll()
        self._mark_caps(filename, data)
        self.btn_save.setEnabled(True)
        self._show_problems(problems, "Problems in the file as it is now (the services are using it - fix them "
                                      "here, or put back a working version with History...):")

    def _show_lab(self, filename):
        """Which values this machine sets itself (its lab settings), or that it has none."""
        try:
            keys = lab_config.changed_keys(config_store.lab_settings(filename, self.config_dir))
        except ConfigError as e:
            keys, problem = [], str(e)
        else:
            problem = ""
        self.btn_repo.setEnabled(bool(keys))
        if problem:
            text = f"Lab settings can't be read: {problem}"
        elif not keys:
            text = "No lab settings: the file as the repository has it."
        else:
            shown = ", ".join(keys[:6]) + (f" and {len(keys) - 6} more" if len(keys) > 6 else "")
            text = (f"Lab settings ({lab_config.LAB_DIRNAME}/{filename}): this machine sets {len(keys)} value(s) itself, "
                    f"kept through software updates: {shown}.")
        self.lbl_lab.setText(text)

    def _populate_tree(self, parent_node, data, path):
        items = data.items() if isinstance(data, dict) else enumerate(data)
        for key, value in items:
            item = QTreeWidgetItem(parent_node)
            item.setText(0, str(key) if isinstance(data, dict) else f"[{key}]")
            if isinstance(value, (dict, list)) and value:
                self._populate_tree(item, value, path + (key,))
            elif isinstance(value, (dict, list)):
                item.setText(1, "(empty)")
                item.setForeground(1, theme.color("text_faint"))
            else:
                self._add_edit_widget(item, key, value, path + (key,))

    def _add_edit_widget(self, item, key, value, path):
        if isinstance(value, bool):
            w = QCheckBox()
            w.setChecked(value)
        elif isinstance(value, float):
            w = QLineEdit(format_float(value))
        elif isinstance(value, int):
            w = QLineEdit(str(value))
        elif value is None:
            w = QLineEdit("null")
            w.setToolTip("null in the file: leave as null, or type a number or text")
        else:
            w = QLineEdit(str(value))
        if key == "bus_id":
            w.setEnabled(False)
            w.setToolTip("Bus ID cannot be changed as it maps to physical hardware and network configs.")
        w.setProperty("plain_tip", w.toolTip())
        self.tree.setItemWidget(item, 1, w)
        self._leaves.append((item, path, value))

    def _mark_caps(self, filename, data):
        """Fields past a hard cap in red, with the reason as their tooltip (cleared once they're
        within it again); the first one is scrolled into view. Returns how many there are."""
        breaches = {v.path: v.message for v in hard_caps.violations(filename, data)}
        first = None
        for item, path, _ in self._leaves:
            w = self.tree.itemWidget(item, 1)
            if path in breaches:
                set_tone(w, CAP_TONE)
                w.setToolTip(breaches[path])
                first = first or item
            elif w.property("tone") == CAP_TONE:
                set_tone(w, None)
                w.setToolTip(w.property("plain_tip") or "")
        if first is not None:
            self.tree.scrollToItem(first)
        return len(breaches)

    # ------------------------------------------------------------------ edits
    @staticmethod
    def _label(path):
        return "".join(f"[{p}]" if isinstance(p, int) else (f".{p}" if i else str(p)) for i, p in enumerate(path))

    def _value_of(self, widget, original, path):
        if isinstance(widget, QCheckBox):
            return widget.isChecked()
        text = widget.text().strip() if isinstance(widget, QLineEdit) else ""
        if isinstance(original, bool):
            return original
        if isinstance(original, (int, float)):
            try:
                value = parse_number(text)
            except ValueError:
                raise ValueError(f"{self._label(path)}: '{text}' is not a number") from None
            if isinstance(original, float):
                return float(value)
            return int(value) if isinstance(value, float) and value.is_integer() else value   # '5.0' -> 5
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
        return config_store.apply_edits(self.original, leaves)

    # ------------------------------------------------------------------ applying
    def _service_states(self):
        """{service: state} as the service manager reports it ({} if it isn't reporting)."""
        if not callable(self.services):
            return {}
        try:
            reported = self.services()
            states = dict(reported) if isinstance(reported, dict) else {n: "" for n in reported}
        except (TypeError, ValueError, AttributeError) as e:
            log.warning("Service list not available: %s", e)
            return {}
        return {n: str(state).upper() for n, state in states.items() if isinstance(n, str)}

    def _restart(self, filename, target):
        """Send the restarts; returns what to tell the operator. A service stopped on purpose is
        left stopped (the manager's restart would start it again)."""
        if target == WHOLE_SYSTEM:
            return ("Nothing was restarted: every program, the service manager and this HMI included, reads "
                    "this file when it starts. Restart the service manager and the HMI at a safe moment; until "
                    "then everything keeps running with the old settings.")
        if target == HMI:
            return "Restart the operator HMI for the change to take effect."
        if target == BUILDER:
            return "Nothing was restarted: the recipe builder reads this file each time it opens."
        states = self._service_states()
        hmi = " Restart the operator HMI too (it reads this file at start-up)." if filename in HMI_TOO else ""
        if target == ALL_SERVICES:
            if not states:
                return ("Nothing was restarted: the service manager isn't reporting which services run. "
                        "Restart them from the services panel (or restart the manager).")
            group = list(states)
        else:
            group = list(SERVICE_GROUPS.get(target, (target,)))
        names = [n for n in group if states.get(n) != "STOPPED"]
        stopped = [n for n in group if states.get(n) == "STOPPED"]
        if not names:
            if len(group) == 1:
                return (f"{target} was not restarted: it is stopped (on purpose?). It reads the new settings when "
                        "it is started again from the services panel." + hmi)
            which = "every service is" if target == ALL_SERVICES else f"{' and '.join(stopped)} are"
            return (f"Nothing was restarted: {which} stopped. They read the new settings when they are started "
                    "again from the services panel." + hmi)
        for svc in names:
            self.cmd_thread.send_command("manager", "manager_cmd", {"command": "restart", "service": svc})
        told = f"Restart requested for {', '.join(names)}."
        if stopped:
            told += f" Left stopped: {', '.join(stopped)} (they read the new settings when started)."
        return told + hmi

    def _record(self, filename, changes, reason, result, what="changed"):
        log.warning("Config %s %s: %s (reason: %s; backup %s; lab settings %s)", filename, what, "; ".join(changes),
                    reason or "-", result.backup, result.path)
        if self.events is not None:
            text = f"Config {filename} {what}: " + "; ".join(changes[:4]) + (" ..." if len(changes) > 4 else "")
            self.events.log_general(text + (f" ({reason})" if reason else ""), severity="WARNING",
                                    event_type="CONFIG_CHANGE",
                                    metadata={"file": filename, "changes": changes, "reason": reason,
                                              "backup": result.backup, "lab_file": result.path,
                                              "lab_settings": lab_config.changed_keys(result.lab)})

    def _apply(self, filename, changes, title, intro, what, write, large=()):
        """Confirm, then write(reason, after_write) -> SaveResult; record and restart. True if applied."""
        target = self.cb_service.currentText()
        dlg = ConfirmChangesDialog(title, intro, changes, self, large=large)
        if not dlg.exec():
            return False
        reason = dlg.reason.text().strip()
        after = self.rebuild if filename in REGISTRY_INPUTS else None
        result = write(reason, after)
        if not result.saved:
            detail = "\n".join(result.problems) if result.problems else result.error
            QMessageBox.critical(self, "Not Applied", f"{filename} was not changed:\n\n{detail}")
            return False
        self._record(filename, changes, reason, result, what)
        keys = lab_config.changed_keys(result.lab)
        lines = [f"Saved {filename}" + (" and rebuilt system_tags.json." if after else "."),
                 (f"This machine now sets {len(keys)} value(s) of it itself, as lab settings "
                  f"({lab_config.LAB_DIRNAME}/{filename}): software updates keep them." if keys else
                  "It is now as the repository has it: no lab settings."),
                 f"Previous version kept: {result.backup}" if result.backup else "",
                 self._restart(filename, target)]
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
        if self._mark_caps(filename, data):
            capped = [p for p in problems if p.startswith("Hard cap:")]
            others = [p for p in problems if not p.startswith("Hard cap:")]
            QMessageBox.warning(self, "Refused: Hard Cap",
                                f"{filename} was not changed: a value is over a hard cap (marked red - hover "
                                "over it for the reason).\n\n• " + "\n• ".join(capped)
                                + ("\n\nAlso:\n• " + "\n• ".join(others) if others else ""))
            return
        if problems:
            QMessageBox.warning(self, "Not Applied", f"{filename} would have problems the services trip over:"
                                                     "\n\n• " + "\n• ".join(problems))
            return
        changes = config_store.diff(self.original, data)
        if not changes:
            QMessageBox.information(self, "Nothing To Apply", "No values were changed.")
            return
        original = self.original

        def write(reason, after):
            return config_store.save(filename, data, original=original, expected=original, reason=reason,
                                     config_dir=self.config_dir, after_write=after)
        if self._apply(filename, changes, "Apply Changes", f"<b>{filename}</b>: {len(changes)} change(s). Apply them?",
                       "changed", write, large=config_store.large_changes(original, data)):
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
        labels = []
        for p in found:                     # two saves within a second: tell them apart
            label = config_store.backup_time(p)
            labels.append(label if label not in labels else f"{label} ({sum(x.startswith(label) for x in labels) + 1})")
        choice, ok = QInputDialog.getItem(self, "Put Back a Previous Version",
                                          f"{filename} as it was before the change saved at:", labels, 0, False)
        if not ok:
            return
        path = found[labels.index(choice)]
        try:
            data = config_store.read_path(path)
        except ConfigError as e:
            QMessageBox.critical(self, "History", f"Could not read {path}:\n{e}")
            return
        problems = config_store.check(filename, data)
        if problems:
            QMessageBox.warning(self, "History", "That version has problems the services would trip over:\n\n• "
                                + "\n• ".join(problems))
            return
        current = self.original

        def write(reason, after):
            return config_store.restore(filename, path, expected=current,
                                        reason=reason or f"put back the version from before {choice}",
                                        config_dir=self.config_dir, after_write=after)
        changes = config_store.diff(current if current is not None else {}, data) or ["(identical to the current file)"]
        large = config_store.large_changes(current, data) if current is not None else []
        if self._apply(filename, changes, "Put Back a Previous Version",
                       f"<b>{filename}</b>: put back the version from before {choice}?", "restored", write,
                       large=large):
            self._load_file(filename)

    def _use_repository_values(self):
        """This machine's lab settings for the file taken away: the repository's file again."""
        filename = self.cb_files.currentText()
        if not filename or self.original is None:
            return
        try:
            data = config_store.repository_value(filename, self.config_dir)
        except ConfigError as e:
            QMessageBox.critical(self, "Repository Values", f"Could not read the repository's {filename}:\n{e}")
            return
        if data is None:
            QMessageBox.information(self, "Repository Values", f"The repository has no {filename}: it is this "
                                                               "machine's own file.")
            return
        problems = config_store.check(filename, data)
        if problems:
            QMessageBox.warning(self, "Repository Values", "The repository's file has problems the services would "
                                "trip over:\n\n• " + "\n• ".join(problems))
            return
        current = self.original

        def write(reason, after):
            return config_store.save(filename, data, expected=current, reason=reason or "the repository's values",
                                     config_dir=self.config_dir, after_write=after)
        changes = config_store.diff(current, data) or ["(identical to the current file)"]
        if self._apply(filename, changes, "Use the Repository's Values",
                       f"<b>{filename}</b>: take this machine's lab settings away, so it is as the repository has "
                       "it?", "set to the repository's values", write,
                       large=config_store.large_changes(current, data)):
            self._load_file(filename)
