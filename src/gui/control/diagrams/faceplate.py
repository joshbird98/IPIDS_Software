"""A faceplate: the card that opens over a diagram when a part is clicked, with what that part shows
and the controls it has (mirrors of its panel's: mirror.py). It sits beside the part, inside the
panel; the cross, a click on empty space, or another part closes it. In a panel too short for all
of it, its contents scroll between its title and its link to the panel. Its shadow is drawn by the
canvas under it (DiagramCanvas._shadow): a blur effect on the card would be worked out again on
every refresh of its readouts.
"""
from PyQt6.QtCore import QPoint, QRect, QRectF, Qt, pyqtSignal
from PyQt6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy,
                             QToolButton, QVBoxLayout, QWidget)

from src.gui.style import set_role, set_tone

GAP_PX = 14
WIDTH_PX = 330
LEAST_CONTENTS_PX = 64      # of its contents shown, however short the panel


class Faceplate(QFrame):
    closed = pyqtSignal()

    def __init__(self, parent: QWidget, key: str, title: str, subtitle: str = ""):
        super().__init__(parent)
        self.key = key
        self.setObjectName("DiagramFaceplate")
        set_role(self, "card")
        self.setAutoFillBackground(True)
        self.setFixedWidth(WIDTH_PX)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum)
        self._mirrors = []
        self._updaters = []
        self._placed = None         # (anchor, bounds, the contents' width and height) when last placed

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 8, 8, 12)
        outer.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(6)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.lbl_title = QLabel(title)
        set_role(self.lbl_title, "title")
        titles.addWidget(self.lbl_title)
        if subtitle:
            sub = QLabel(subtitle)
            set_role(sub, "caption")
            sub.setWordWrap(True)
            titles.addWidget(sub)
        head.addLayout(titles, 1)
        close = QToolButton()
        close.setText("✕")
        close.setToolTip("Close (or click an empty part of the diagram)")
        close.setAutoRaise(True)
        close.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        close.clicked.connect(self.close_plate)
        head.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(head)
        # The contents (rows, notes): scrolled when the panel is too short for them (place())
        body = QWidget()
        self._body = QVBoxLayout(body)
        self._body.setContentsMargins(0, 0, 0, 0)
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(10)
        self.grid.setVerticalSpacing(6)
        self.grid.setColumnStretch(1, 1)
        self._body.addLayout(self.grid)
        self.scroll = QScrollArea()
        set_role(self.scroll, "pane-body")          # (transparent: the card shows through)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.scroll.setWidget(body)
        outer.addWidget(self.scroll)
        self._outer = outer

    # --- contents ---------------------------------------------------------------------------
    def _track(self, widget):
        if hasattr(widget, "sync"):
            self._mirrors.append(widget)
        return widget

    def row(self, caption: str, *widgets):
        """A caption and what goes with it (a readout, a setpoint box, a chip)."""
        r = self.grid.rowCount()
        cap = QLabel(caption)
        set_role(cap, "caption")
        self.grid.addWidget(cap, r, 0, Qt.AlignmentFlag.AlignVCenter)
        if len(widgets) == 1:
            self.grid.addWidget(self._track(widgets[0]), r, 1)
        else:
            box = QHBoxLayout()
            box.setSpacing(6)
            for w in widgets:
                box.addWidget(self._track(w))
            self.grid.addLayout(box, r, 1)

    def wide(self, *widgets):
        """Widgets across the whole card (chips, buttons side by side)."""
        r = self.grid.rowCount()
        box = QHBoxLayout()
        box.setSpacing(6)
        for w in widgets:
            box.addWidget(self._track(w))
        self.grid.addLayout(box, r, 0, 1, 2)

    def heading(self, text: str) -> QLabel:
        """A small heading across the card, for a second thing on it (the neutral trap's two supplies)."""
        lbl = QLabel(text)
        set_role(lbl, "title")
        self.grid.addWidget(lbl, self.grid.rowCount(), 0, 1, 2)
        return lbl

    def note(self, text: str, role: str = "hint") -> QLabel:
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        set_role(lbl, role)
        self.grid.addWidget(lbl, self.grid.rowCount(), 0, 1, 2)
        return lbl

    def chip(self, text: str = "", tone: str = "off") -> QLabel:
        lbl = QLabel(text)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        set_tone(lbl, tone)
        return lbl

    def link(self, text: str, callback):
        """A quiet button to the full panel."""
        btn = QPushButton(text)
        btn.setFlat(True)
        set_role(btn, "link")
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.clicked.connect(lambda *_: callback())
        self._outer.addWidget(btn, 0, Qt.AlignmentFlag.AlignRight)
        return btn

    def every_refresh(self, fn):
        """fn() on every refresh, for what isn't a mirror (a value worked out by the diagram)."""
        self._updaters.append(fn)
        fn()

    # --- life ---------------------------------------------------------------------------------
    def sync(self):
        for m in self._mirrors:
            m.sync()
        for fn in self._updaters:
            fn()
        if self._placed is not None:
            anchor, bounds, width, height = self._placed
            if self._contents_height(width) != height:      # (a row shown or hidden, a note longer)
                self.place(anchor, bounds)

    def close_plate(self):
        self.hide()
        self.closed.emit()
        self.deleteLater()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close_plate()
            return
        super().keyPressEvent(event)

    def _contents_height(self, width: int) -> int:
        body = self._body
        return body.totalHeightForWidth(width) if body.hasHeightForWidth() else body.sizeHint().height()

    def place(self, anchor: QRectF, bounds: QRect):
        """Beside anchor (the part, in the parent's coordinates), inside bounds: all of it, or as much
        of its contents as fit, scrolled."""
        w = WIDTH_PX
        self.ensurePolished()
        lay = self.layout()
        frame, margins = self.contentsMargins(), lay.contentsMargins()
        width = w - frame.left() - frame.right() - margins.left() - margins.right()
        # (its wrapped notes are as tall as they are at this width: sizeHint alone cuts the last line)
        contents = self._contents_height(width)
        self.scroll.setFixedHeight(contents)
        lay.activate()
        h = max(lay.totalHeightForWidth(w) if lay.hasHeightForWidth() else 0, self.sizeHint().height())
        room = bounds.height() - 8
        if h > room:
            shown = min(contents, max(LEAST_CONTENTS_PX, contents - (h - room)))
            self.scroll.setFixedHeight(shown)
            h -= contents - shown
        self._placed = (QRectF(anchor), QRect(bounds), width, contents)
        self.resize(w, h)
        a = anchor.toAlignedRect()
        if a.right() + GAP_PX + w <= bounds.right():
            x = a.right() + GAP_PX
        elif a.left() - GAP_PX - w >= bounds.left():
            x = a.left() - GAP_PX - w
        else:
            x = bounds.right() - w - 4
        y = a.center().y() - h // 2
        y = max(bounds.top() + 4, min(y, bounds.bottom() - h - 4))
        x = max(bounds.left() + 4, x)
        self.move(QPoint(x, y))
        self.raise_()
