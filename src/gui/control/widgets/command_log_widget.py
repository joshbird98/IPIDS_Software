"""Recent commands and what each service said about them (newest first).

Every command the HMI, a recipe, the optimiser or a scan sends is answered by
the service: ok / accepted / rejected (with the reason) / failed. Timeouts and
'service not running' are added by the HMI itself. The full history is in
events.db (event type COMMAND) and each service's log file.
"""
import time

from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (QCheckBox, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget,
                             QTableWidgetItem, QVBoxLayout, QWidget)

from src.core.commands import SUCCESS

MAX_ROWS = 500
COLUMNS = ["Time", "Origin", "Service", "Tag", "Value", "Result", "Reason"]
_COLOURS = {"rejected": "#C62828", "failed": "#C62828", "timeout": "#EF6C00", "unreachable": "#EF6C00"}


class CommandLogWidget(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        bar = QHBoxLayout()
        self.lbl_summary = QLabel("No commands yet")
        self.chk_problems = QCheckBox("Problems only")
        self.chk_problems.toggled.connect(self._apply_filter)
        btn_clear = QPushButton("Clear")
        btn_clear.clicked.connect(self._clear)
        bar.addWidget(self.lbl_summary, stretch=1)
        bar.addWidget(self.chk_problems)
        bar.addWidget(btn_clear)
        layout.addLayout(bar)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        hdr = self.table.horizontalHeader()
        for i in range(len(COLUMNS) - 1):
            hdr.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(len(COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)

        self.total = 0
        self.problems = 0

    def add_result(self, result: dict):
        status = result.get("status", "?")
        ok = status in SUCCESS
        self.total += 1
        self.problems += 0 if ok else 1

        tag = result.get("tag", "")
        value = result.get("value")
        if isinstance(value, dict):  # manager commands carry {"command": ..., "service": ...}
            value = " ".join(str(v) for v in value.values())
        cells = [time.strftime("%H:%M:%S"), result.get("origin", ""), result.get("subsystem", "") or "",
                 tag, repr(value) if not isinstance(value, str) else value, status.upper(),
                 result.get("reason", "") or ""]

        self.table.insertRow(0)
        for col, text in enumerate(cells):
            item = QTableWidgetItem(str(text))
            if col == 3:
                item.setToolTip(tag)
            if col == 6:
                item.setToolTip(str(text))
            if not ok:
                item.setForeground(QColor(_COLOURS.get(status, "#C62828")))
            self.table.setItem(0, col, item)
        self.table.setRowHidden(0, self.chk_problems.isChecked() and ok)
        if self.table.rowCount() > MAX_ROWS:
            self.table.removeRow(self.table.rowCount() - 1)
        self.lbl_summary.setText(f"{self.total} command(s) this session, {self.problems} not executed")

    def _apply_filter(self, problems_only: bool):
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 5)
            ok = item is not None and item.text().lower() in SUCCESS
            self.table.setRowHidden(row, problems_only and ok)

    def _clear(self):
        self.table.setRowCount(0)
        self.total = self.problems = 0
        self.lbl_summary.setText("No commands yet")

    def update_telemetry(self, data: dict):
        pass
