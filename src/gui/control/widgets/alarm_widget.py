"""Alarms panel: the alarm service's list (src/services/service_alarms.py), most urgent first.

Unacknowledged alarms flash and sound (the most urgent one's sound, looped) until acknowledged;
SILENCE stops the sound without acknowledging (a new alarm sounds again). Shelving takes an
alarm out of annunciation for a while, with a reason. If the alarm service stops, the panel says
so and sounds a critical alarm of its own: nothing is watching the alarms then.

Alarms are selected by clicking their rows (Ctrl+click / Shift+click for several). The selection
belongs to the alarms, not the rows: it stays while the list is redrawn and re-sorted.
"""
import time

from PyQt6.QtCore import QItemSelection, QItemSelectionModel, Qt, QTimer
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QHeaderView,
                             QLabel, QLineEdit, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
                             QWidget)

from src.alarms.model import ACKED, PRIORITY_NAMES, RTN_UNACK, UNACK
from src.gui.control.alarm_horn import AlarmHorn

OFFLINE_ID = "hmi.alarm_service_offline"
COLUMNS = ["", "Priority", "State", "Time", "Alarm", "Count"]
SELECT_TIP = "Click an alarm in the list to select it (Ctrl+click or Shift+click for several)."
PRIORITY_COLOURS = {1: ("#B71C1C", "white"), 2: ("#E65100", "white"), 3: ("#F9A825", "black"), 4: ("#1565C0", "white")}
FLASH_COLOURS = {1: "#EF9A9A", 2: "#FFCC80", 3: "#FFF59D", 4: "#90CAF9"}
QUIET_COLOUR = ("#9E9E9E", "black")
STATE_TEXT = {UNACK: "UNACKNOWLEDGED", ACKED: "ACKNOWLEDGED", RTN_UNACK: "CLEARED, UNACKNOWLEDGED", "NORMAL": "NORMAL"}
BUTTON = "font-weight: bold; border-radius: 4px; padding: 6px; color: white; background-color: {};"


def hhmmss(ts) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts)) if ts else ""


class ShelveDialog(QDialog):
    def __init__(self, message: str, max_minutes: int = 480, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Shelve alarm")
        form = QFormLayout(self)
        form.addRow(QLabel(f"<b>{message}</b>"))
        self.minutes = QSpinBox()
        self.minutes.setRange(1, max_minutes)
        self.minutes.setValue(30)
        self.minutes.setSuffix(" min")
        self.reason = QLineEdit()
        self.reason.setPlaceholderText("why (required) - e.g. gauge being replaced")
        form.addRow("Shelve for:", self.minutes)
        form.addRow("Reason:", self.reason)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.reason.textChanged.connect(lambda t: self.buttons.button(QDialogButtonBox.StandardButton.Ok)
                                        .setEnabled(bool(t.strip())))
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        form.addRow(self.buttons)


class AlarmPanel(QWidget):
    def __init__(self, cmd_thread, horn: AlarmHorn = None):
        super().__init__()
        self.cmd_thread = cmd_thread
        self.horn = horn if horn is not None else AlarmHorn()
        self.alarms = []
        self.online = None
        self._signature = None
        self._flash_on = False
        self._init_ui()
        self.flash_timer = QTimer(self)
        self.flash_timer.timeout.connect(self._flash)
        self.flash_timer.start(500)

    # ------------------------------------------------------------------ UI
    def _init_ui(self):
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.lbl_summary = QLabel("Alarms: waiting for the alarm service...")
        self.lbl_summary.setStyleSheet("font-size: 12pt; font-weight: bold; padding: 4px;")
        self.btn_silence = QPushButton("🔇 SILENCE")
        self.btn_silence.setStyleSheet(BUTTON.format("#455A64"))
        self.btn_silence.setToolTip("Stop the sound without acknowledging (a new alarm sounds again).")
        self.btn_silence.clicked.connect(self.silence)
        self.btn_ack_all = QPushButton("✔ ACK ALL")
        self.btn_ack_all.setStyleSheet(BUTTON.format("#2E7D32"))
        self.btn_ack_all.clicked.connect(self.ack_all)
        top.addWidget(self.lbl_summary, stretch=1)
        top.addWidget(self.btn_silence)
        top.addWidget(self.btn_ack_all)
        layout.addLayout(top)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setToolTip(SELECT_TIP)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        header = self.table.horizontalHeader()
        for col in range(len(COLUMNS)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)

        buttons = QHBoxLayout()
        self.btn_ack = QPushButton("✔ Acknowledge selected")
        self.btn_ack.clicked.connect(self.ack_selected)
        self.btn_shelve = QPushButton("💤 Shelve...")
        self.btn_shelve.setToolTip("Take the selected alarm out of annunciation for a while (needs a reason).")
        self.btn_shelve.clicked.connect(self.shelve_selected)
        self.btn_unshelve = QPushButton("Unshelve")
        self.btn_unshelve.clicked.connect(self.unshelve_selected)
        self.btn_first_out = QPushButton("Reset first-out")
        self.btn_first_out.setToolTip("⭐ marks the first PLC trip of a cascade. Reset it once the cause is understood.")
        self.btn_first_out.clicked.connect(lambda: self._send("alarms.cmd_reset_first_out", True))
        for b in (self.btn_ack, self.btn_shelve, self.btn_unshelve, self.btn_first_out):
            buttons.addWidget(b)
        buttons.addStretch()
        layout.addLayout(buttons)
        self._update_buttons()

    # ------------------------------------------------------------------ commands
    def _send(self, tag, value):
        return self.cmd_thread.send_command("alarms", tag, value, origin="hmi")

    def selected_ids(self) -> list:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.table.item(r, 4).data(Qt.ItemDataRole.UserRole) for r in rows
                if self.table.item(r, 4) is not None]

    def _selected_alarms(self) -> list:
        ids = set(self.selected_ids()) - {OFFLINE_ID}
        return [a for a in self.alarms if a["id"] in ids]

    def _update_buttons(self):
        """What the buttons would act on; with nothing selected they say how to select."""
        chosen = self._selected_alarms()
        to_ack = [a for a in chosen if a["state"] in (UNACK, RTN_UNACK)]
        shelved = [a for a in chosen if a.get("shelved_until")]
        self.btn_ack.setEnabled(bool(to_ack))
        self.btn_ack.setToolTip(
            f"Acknowledge the {len(to_ack)} selected unacknowledged alarm{'s' if len(to_ack) != 1 else ''}. "
            "An acknowledged alarm leaves the list once its condition has cleared." if to_ack else
            ("The selected alarms are already acknowledged: they leave the list when their condition clears."
             if chosen else SELECT_TIP + " Then acknowledge them here - or use ACK ALL."))
        self.btn_shelve.setEnabled(len(chosen) == 1)
        self.btn_shelve.setToolTip("Take the selected alarm out of annunciation for a while (needs a reason)."
                                   if len(chosen) == 1 else "Select one alarm to shelve. " + SELECT_TIP)
        self.btn_unshelve.setEnabled(bool(shelved))
        self.btn_unshelve.setToolTip("Put the selected shelved alarms back into annunciation." if shelved
                                     else "Select a shelved alarm (💤) to unshelve it.")

    def _restore_selection(self, ids: set):
        """Select the rows of these alarms (all of them, wherever they now are in the list)."""
        model = self.table.model()
        selection = QItemSelection()
        for row, alarm in enumerate(self.alarms):
            if alarm["id"] in ids:
                selection.select(model.index(row, 0), model.index(row, len(COLUMNS) - 1))
        self.table.selectionModel().select(selection, QItemSelectionModel.SelectionFlag.ClearAndSelect)

    def ack_selected(self):
        ids = [i for i in self.selected_ids() if i != OFFLINE_ID]
        if ids:
            self._send("alarms.cmd_ack", {"ids": ids})

    def ack_all(self):
        if self.online:
            self._send("alarms.cmd_ack", {"all": True})

    def silence(self):
        self.horn.silence(self._sounding())

    def shelve_selected(self):
        ids = [i for i in self.selected_ids() if i != OFFLINE_ID]
        if len(ids) != 1:
            return
        alarm = next((a for a in self.alarms if a["id"] == ids[0]), None)
        dialog = ShelveDialog(alarm["message"] if alarm else ids[0], parent=self)
        if dialog.exec():
            self._send("alarms.cmd_shelve", {"id": ids[0], "minutes": dialog.minutes.value(),
                                             "reason": dialog.reason.text().strip()})

    def unshelve_selected(self):
        for alarm_id in self.selected_ids():
            if alarm_id != OFFLINE_ID:
                self._send("alarms.cmd_unshelve", {"id": alarm_id})

    # ------------------------------------------------------------------ telemetry
    def _sounding(self) -> frozenset:
        return frozenset(a["id"] for a in self.alarms if a["state"] == UNACK and not a.get("shelved_until")
                         and not a.get("suppressed"))

    def update_telemetry(self, data: dict):
        listed = data.get("alarms.list")
        self.online = listed is not None
        if not self.online:
            # Nothing is watching the alarms: say so, and sound it (the HMI's own alarm)
            self.alarms = [{"id": OFFLINE_ID, "priority": 1, "state": UNACK, "since": None, "count": 1,
                            "message": "The alarm service is not running - alarms are NOT being monitored. "
                                       "Restart it from the Service Manager panel."}]
            self._render()
            self.horn.update(1, self._sounding())
            self.lbl_summary.setText("⚠ ALARM SERVICE NOT RUNNING")
            self.lbl_summary.setStyleSheet("font-size: 12pt; font-weight: bold; padding: 4px; color: white; "
                                           "background-color: #B71C1C;")
            return
        self.alarms = list(listed)
        self._render()
        counts = data.get("alarms.counts") or {}
        self.horn.update(data.get("alarms.horn"), self._sounding())
        unack, active = counts.get("unack", 0), counts.get("active", 0)
        if unack:
            top = min(a["priority"] for a in self.alarms if a["state"] in (UNACK, RTN_UNACK))
            bg, fg = PRIORITY_COLOURS.get(top, QUIET_COLOUR)
            text = f"🔔 {unack} alarm{'s' if unack != 1 else ''} to acknowledge"
        elif active:
            bg, fg = "#FFF8E1", "black"
            text = f"{active} active alarm{'s' if active != 1 else ''} (acknowledged)"
        else:
            bg, fg = "#E8F5E9", "#1B5E20"
            text = "No active alarms"
        if counts.get("shelved"):
            text += f"   💤 {counts['shelved']} shelved"
        if self.horn.silenced and unack:
            text += "   🔇 silenced"
        self.lbl_summary.setText(text)
        self.lbl_summary.setStyleSheet(f"font-size: 12pt; font-weight: bold; padding: 4px; color: {fg}; "
                                       f"background-color: {bg};")

    def _render(self):
        signature = tuple((a["id"], a["state"], a.get("shelved_until"), a.get("suppressed"), a.get("first_out"),
                           a.get("count"), a.get("message")) for a in self.alarms)
        if signature == self._signature:
            return
        self._signature = signature
        selected = set(self.selected_ids())       # (read from the rows as they were drawn)
        self.table.blockSignals(True)
        try:
            self._fill(selected)
        finally:
            self.table.blockSignals(False)
        self._update_buttons()
        self._paint()

    def _fill(self, selected: set):
        self.table.setRowCount(len(self.alarms))
        for row, alarm in enumerate(self.alarms):
            marks = ("⭐" if alarm.get("first_out") else "") + ("💤" if alarm.get("shelved_until") else "") + \
                    ("⤵" if alarm.get("suppressed") else "")
            message = alarm["message"]
            if alarm.get("shelved_until"):
                message += f"   (shelved until {hhmmss(alarm['shelved_until'])}: {alarm.get('shelve_reason', '')})"
            if alarm.get("suppressed"):
                message += "   (suppressed: explained by another alarm)"
            when = alarm.get("cleared") if alarm["state"] == RTN_UNACK else alarm.get("since")
            cells = [marks, PRIORITY_NAMES.get(alarm["priority"], str(alarm["priority"])).upper(),
                     STATE_TEXT.get(alarm["state"], alarm["state"]), hhmmss(when), message, str(alarm.get("count", ""))]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col == 4:
                    item.setData(Qt.ItemDataRole.UserRole, alarm["id"])
                    item.setToolTip(f"{alarm['id']}\n{message}\n\n{SELECT_TIP}")
                self.table.setItem(row, col, item)
        # Re-selected by alarm, all at once: selectRow() per row followed the keyboard's modifiers -
        # only the last alarm stayed selected, or (Ctrl held) the selection was toggled off
        self._restore_selection(selected)

    def _flash(self):
        self._flash_on = not self._flash_on
        self._paint()

    def _paint(self):
        for row, alarm in enumerate(self.alarms):
            quiet = alarm.get("shelved_until") or alarm.get("suppressed")
            bg, fg = QUIET_COLOUR if quiet else PRIORITY_COLOURS.get(alarm["priority"], QUIET_COLOUR)
            if alarm["state"] == UNACK and not quiet and self._flash_on:
                bg, fg = FLASH_COLOURS.get(alarm["priority"], bg), "black"
            elif alarm["state"] == ACKED and not quiet:
                fg = "black"
                bg = FLASH_COLOURS.get(alarm["priority"], bg)       # steady, paler: acknowledged
            font = QFont()
            font.setBold(alarm["state"] in (UNACK, RTN_UNACK))
            font.setItalic(alarm["state"] == RTN_UNACK)
            for col in range(len(COLUMNS)):
                item = self.table.item(row, col)
                if item is not None:
                    item.setBackground(QColor(bg))
                    item.setForeground(QColor(fg))
                    item.setFont(font)
