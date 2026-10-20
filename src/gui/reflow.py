"""A grid of cards that takes as many columns as fit its width: three gauges side by side in a
wide panel, one above another in a narrow one. The panels can then be docked in a narrow column
without their contents being cut off or pushing the panel wider.

    grid = ReflowGrid(max_columns=3)
    for card in cards:
        grid.add(card)
    layout.addWidget(grid)

The column width is the widest card's minimum width (checked again when a card's contents
change, e.g. a button's text gets longer); the grid re-arranges when it is resized. Qt's own
flow-layout example does the same for rows of differently sized items.
"""
from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QGridLayout, QSizePolicy, QWidget


class ReflowGrid(QWidget):
    def __init__(self, max_columns: int = 3, min_column_width: int = 0, spacing: int = 8, parent=None):
        super().__init__(parent)
        self._items = []
        self._max = max(1, max_columns)
        self._floor = min_column_width
        self._min_col = min_column_width
        self._columns = 0
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(spacing)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

    def add(self, widget):
        self._items.append(widget)
        widget.ensurePolished()
        self._refit(force=True)

    def columns(self) -> int:
        return self._columns

    def _widest(self) -> int:
        # (a card's own minimum width counts too: the size hint leaves it out)
        return max([self._floor] + [max(w.minimumSizeHint().width(), w.minimumWidth()) for w in self._items])

    def _fit(self, width: int) -> int:
        """Columns for this width: as many as fit, then evened out (four buttons that don't fit in
        a row go two by two, not three and one)."""
        gap = self._grid.horizontalSpacing()
        n = len(self._items) or 1
        fit = max(1, min(self._max, n, (width + gap) // max(1, self._min_col + gap)))
        rows = -(-n // fit)
        return -(-n // rows)

    def _refit(self, force: bool = False):
        widest = self._widest()
        if widest != self._min_col:
            self._min_col = widest
            force = True
        # Never narrower than one card; the layout must not hold it wider than that, or the
        # panel could not be made narrow enough to fold into fewer columns
        if self.minimumWidth() != self._min_col:
            self.setMinimumWidth(self._min_col)
        n = self._fit(self.width() if self.isVisible() else 1 << 20)
        if force or n != self._columns:
            self._arrange(n)

    def _arrange(self, columns: int):
        self._columns = columns
        for w in self._items:
            self._grid.removeWidget(w)
        for c in range(self._max):
            self._grid.setColumnStretch(c, 1 if c < columns else 0)
        for i, w in enumerate(self._items):
            self._grid.addWidget(w, i // columns, i % columns)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        n = self._fit(event.size().width())
        if n != self._columns:
            self._arrange(n)

    def event(self, event):
        # A card's size hint changed (new text, a label shown): its minimum width may have too
        if event.type() == QEvent.Type.LayoutRequest and self._items:
            self._refit()
        return super().event(event)
