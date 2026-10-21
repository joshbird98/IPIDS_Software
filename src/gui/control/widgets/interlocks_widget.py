"""The Interlocks panel: every interlock between the operator and the machine, and what is holding
each one now (src/core/interlocks.py works it out from the live telemetry; this draws it).

Each card is one permissive, drawn as a chain: the conditions in series on a rail, the output at the
bottom, like a rung of ladder logic read top to bottom. The rail is solid as far as the conditions
hold and dashed after the first that doesn't, so the eye goes straight to what is holding it. A
condition that holds is quiet grey; one that holds the output off is marked with a cross - amber
when the output has been asked for, red for a latched fault, plain otherwise (the machine simply
isn't on) - with the first such row tinted. Dashed rings are values that aren't known; a ring of
dots is the part of the safety relay's loop no software can see.
"""
import time

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout,
                             QWidget)

from src.core import interlocks as il
from src.gui import theme
from src.gui.reflow import ReflowGrid
from src.gui.settings import gui_settings
from src.gui.style import chip, set_prop, set_role, set_tone

GUTTER = 26             # the node column
NODE = 13               # a condition's node (diameter)
PAD = 4                 # above and below each condition's text: room for the rail between nodes
CARD_MIN_W = 380        # one column of cards in a docked panel, two or three in a wide window
CONFIG_CHECK_S = 5.0    # vac_gauges_config.json re-read when it changes (the loop's gauge relay)

LEVEL_TONES = {il.LEVEL_ON: "on", il.LEVEL_ATTENTION: "warn", il.LEVEL_FAULT: "fault", il.LEVEL_NORMAL: "off",
               il.LEVEL_AUTO: "info", il.LEVEL_UNKNOWN: "unknown"}
GROUP_TITLES = {"relay": "Safety relay", "gv_open": "Gate valve", "source_cold": "Source cold",
                "supplies": "Source supplies", "cesium": "Caesium oven", "reset": "Fault reset",
                "hmi": "Control from the HMI"}


def _emphasis(cond: il.Condition, level: str) -> str:
    """How a condition that holds its chain off is marked: 'fault' (red) for a latched PLC fault,
    'attention' (amber) when its group's output was asked for or is in trouble, else 'plain'."""
    if cond.kind == il.FAULT:
        return "fault"
    return "attention" if level in (il.LEVEL_ATTENTION, il.LEVEL_FAULT) else "plain"


def node_style(cond: il.Condition, emphasis: str, t: dict) -> dict:
    """{shape, ring, fill, mark, mark_colour, dash, size} for a condition's node."""
    kind, state = cond.kind, cond.state
    if kind == il.OUTPUT:
        shape, size = "square", NODE + 2
    elif kind == il.REQUEST:
        shape, size = "diamond", NODE + 1
    elif kind == il.PLC:
        shape, size = "circle", 8
    elif kind == il.LEASE:
        return dict(shape="circle", size=9, ring=t["seq"], fill=t["seq"], mark="", mark_colour="", dash=None)
    else:
        shape, size = "circle", NODE
    out = dict(shape=shape, size=size, ring=t["border_strong"], fill=t["card"], mark="", mark_colour=t["text_muted"],
               dash=None)
    if state == il.OK:
        out.update(mark="check")
    elif state == il.HOLDING:
        if emphasis == "fault":
            out.update(ring=t["fault_fill"], fill=t["fault_fill"], mark="cross", mark_colour=t["fault_fill_text"])
        elif emphasis == "attention":
            out.update(ring=t["warn_fill"], fill=t["warn_fill"], mark="cross", mark_colour=t["warn_fill_text"])
        else:
            out.update(ring=t["text"], mark="cross", mark_colour=t["text"])
    elif state == il.UNKNOWN:
        out.update(ring=t["text_faint"], fill=t["card"], mark="?", mark_colour=t["text_faint"], dash=Qt.PenStyle.DashLine)
    elif state == il.HIDDEN:
        out.update(ring=t["text_faint"], mark="dots", mark_colour=t["text_faint"], dash=Qt.PenStyle.DotLine)
    elif state == il.SUSPECT:
        out.update(ring=t["warn"], mark="?", mark_colour=t["warn"], dash=Qt.PenStyle.DashLine)
    elif state == il.ON:
        if kind == il.REQUEST:
            out.update(ring=t["info_fill"], fill=t["info_fill"], mark="", mark_colour=t["info_fill_text"])
        else:
            out.update(ring=t["ok_fill"], fill=t["ok_fill"], mark="check", mark_colour=t["ok_fill_text"])
    elif state == il.OFF:
        out.update(ring=t["info"] if kind == il.REQUEST else t["border_strong"],
                   fill=t["card"] if kind == il.REQUEST else t["off_bg"])
    else:       # INFO
        out.update(ring=t["text_muted"], fill=t["card"])
    return out


def paint_node(p: QPainter, centre: QPointF, style: dict):
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(style["ring"]), 1.5)
    if style["dash"] is not None:
        pen.setStyle(style["dash"])
    p.setPen(pen)
    p.setBrush(QColor(style["fill"]))
    s = style["size"]
    r = QRectF(centre.x() - s / 2, centre.y() - s / 2, s, s)
    if style["shape"] == "square":
        p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 3, 3)
    elif style["shape"] == "diamond":
        path = QPainterPath()
        path.moveTo(centre.x(), r.top())
        path.lineTo(r.right(), centre.y())
        path.lineTo(centre.x(), r.bottom())
        path.lineTo(r.left(), centre.y())
        path.closeSubpath()
        p.drawPath(path)
    else:
        p.drawEllipse(r.adjusted(0.5, 0.5, -0.5, -0.5))
    mark, ink = style["mark"], QColor(style["mark_colour"] or style["ring"])
    k = s * 0.24
    if mark == "check":
        p.setPen(QPen(ink, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        path = QPainterPath()
        path.moveTo(centre.x() - k * 1.1, centre.y() + 0.1)
        path.lineTo(centre.x() - k * 0.25, centre.y() + k * 0.9)
        path.lineTo(centre.x() + k * 1.2, centre.y() - k * 0.9)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
    elif mark == "cross":
        p.setPen(QPen(ink, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(centre.x() - k, centre.y() - k), QPointF(centre.x() + k, centre.y() + k))
        p.drawLine(QPointF(centre.x() + k, centre.y() - k), QPointF(centre.x() - k, centre.y() + k))
    elif mark == "dots":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(ink)
        for dx in (-k * 1.2, 0.0, k * 1.2):
            p.drawEllipse(QPointF(centre.x() + dx, centre.y()), 1.0, 1.0)
    elif mark == "?":
        p.setPen(ink)
        f = p.font()
        f.setPixelSize(max(8, int(s * 0.7)))
        f.setBold(True)
        p.setFont(f)
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, "?")
    p.restore()


def legend_icon(cond: il.Condition, emphasis: str = "plain", mode: str = None) -> QPixmap:
    """A node on its own, for the panel's key."""
    t = theme.tokens(mode)
    pm = QPixmap(20, 20)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    paint_node(p, QPointF(10, 10), node_style(cond, emphasis, t))
    p.end()
    return pm


def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class _Row(QWidget):
    """One condition (or a section heading): its node and the rail in the gutter, its text beside."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setTextFormat(Qt.TextFormat.RichText)
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.text.setContentsMargins(0, PAD, 4, PAD)
        self.text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(GUTTER, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.text)
        self.cond = None
        self.style_ = None
        self.rail_above = self.rail_below = None     # None: no rail; True: live (solid); False: dashed
        self.tint = None

    def node_y(self) -> float:
        return PAD + self.text.fontMetrics().height() / 2.0

    def paintEvent(self, event):
        p = QPainter(self)
        t = theme.tokens()
        if self.tint:
            p.fillRect(self.rect(), QColor(self.tint))
        x, y = GUTTER / 2.0, self.node_y()
        r = (self.style_["size"] / 2.0 + 1.5) if self.style_ else 0.0
        for live, y0, y1 in ((self.rail_above, 0.0, y - r), (self.rail_below, y + r, float(self.height()))):
            if live is None or y1 <= y0:
                continue
            pen = QPen(QColor(t["text_muted"] if live else t["text_faint"]), 2.0 if live else 1.3)
            if not live:
                pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawLine(QPointF(x, y0), QPointF(x, y1))
        if self.style_ is not None:
            paint_node(p, QPointF(x, y), self.style_)
        p.end()


class ChainView(QWidget):
    """A group's conditions: a rail through the ones in series (up to and including the output),
    the rest as a list after it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 2, 0, 2)
        self.lay.setSpacing(0)
        self.rows = []
        self._shape = None
        self._html = []

    def show_group(self, g: il.Group, links):
        shape = tuple((c.label, c.kind, c.section) for c in g.conditions)
        if shape != self._shape:
            self._rebuild(g, links)
        t = theme.tokens()
        out_at = next((i for i, c in enumerate(g.conditions) if c.kind == il.OUTPUT), len(g.conditions) - 1)
        first = g.first
        live = True
        k = 0
        prev_section = None
        for i, c in enumerate(g.conditions):
            on_rail = g.chain and i <= out_at
            if c.section and c.section != prev_section:
                head = self.rows[k]
                k += 1
                head.rail_above = head.rail_below = live if on_rail and i > 0 else None
                head.style_ = None
                self._set_html(head, f'<span style="color:{t["text_faint"]}; font-weight:600">{_esc(c.section)}</span>')
                head.update()
            prev_section = c.section
            row = self.rows[k]
            k += 1
            emphasis = _emphasis(c, g.level)
            row.cond = c
            row.style_ = node_style(c, emphasis, t)
            row.rail_above = live if on_rail and i > 0 else None
            if c.gates:
                live = live and c.state in (il.OK, il.ON)
            row.rail_below = live if on_rail and i < out_at else None
            row.tint = None
            if i == first:
                row.tint = (t["fault_bg"] if emphasis == "fault" else t["warn_bg"] if emphasis == "attention"
                            else t["card_alt"])
            self._set_html(row, self._row_html(c, emphasis, t, links))
            tip = f"Read from {c.tag}: plot it in the data viewer to see its history." if c.tag else ""
            if row.text.toolTip() != tip:
                row.text.setToolTip(tip)
            row.update()

    def _rebuild(self, g: il.Group, links):
        for row in self.rows:
            self.lay.removeWidget(row)
            row.hide()              # (deleted later: hidden now, so it isn't painted over the new rows)
            row.deleteLater()
        self.rows = []
        prev_section = None
        for c in g.conditions:
            if c.section and c.section != prev_section:
                self.rows.append(_Row(self))
            prev_section = c.section
            self.rows.append(_Row(self))
        for row in self.rows:
            row.text.linkActivated.connect(links)
            self.lay.addWidget(row)
        self._shape = tuple((c.label, c.kind, c.section) for c in g.conditions)

    @staticmethod
    def _set_html(row: _Row, html: str):
        if row.text.text() != html:
            row.text.setText(html)

    @staticmethod
    def _row_html(c: il.Condition, emphasis: str, t: dict, links) -> str:
        label, detail = _esc(c.label), _esc(c.detail)
        if c.state == il.HOLDING:
            ink = t["fault"] if emphasis == "fault" else t["warn"] if emphasis == "attention" else t["text"]
            head = f'<span style="color:{t["text"]}; font-weight:600">{label}</span>'
            tail = f'<span style="color:{ink}">{detail}</span>'
        elif c.state in (il.UNKNOWN, il.HIDDEN):
            head = f'<span style="color:{t["text_muted"]}">{label}</span>'
            tail = f'<span style="color:{t["text_faint"]}">{detail}</span>'
        elif c.state == il.SUSPECT:
            head = f'<span style="color:{t["text"]}; font-weight:600">{label}</span>'
            tail = f'<span style="color:{t["warn"]}">{detail}</span>'
        elif c.kind == il.OUTPUT:
            head = f'<span style="color:{t["text"]}; font-weight:600">{label}</span>'
            tail = f'<span style="color:{t["text_muted"]}">{detail}</span>'
        elif c.kind in (il.REQUEST, il.LEASE):
            head = f'<span style="color:{t["text"]}">{label}</span>'
            tail = f'<span style="color:{t["info"] if c.kind == il.REQUEST else t["seq"]}">{detail}</span>'
        elif c.kind == il.PLC:
            head = f'<span style="color:{t["text_muted"]}">{label}</span>'
            tail = f'<span style="color:{t["text_muted"]}">{detail}</span>'
        else:
            head = f'<span style="color:{t["text_muted"]}">{label}</span>'
            tail = f'<span style="color:{t["text_faint"]}">{detail}</span>'
        see = ""
        if c.see and c.state != il.OK and c.see in GROUP_TITLES:
            see = (f' <a href="group:{c.see}" style="color:{t["accent"]}; text-decoration:none">'
                   f'(see {_esc(GROUP_TITLES[c.see])})</a>')
        return f"{head}&nbsp;&nbsp;{tail}{see}" if detail else f"{head}{see}"


class GroupCard(QFrame):
    """One permissive: heading (title, status chip), what holds it, notes, then its chain."""

    def __init__(self, key: str, title: str, links, collapsed=False, on_toggle=None, parent=None):
        super().__init__(parent)
        self.key = key
        set_role(self, "pane")
        self.setObjectName(f"Interlock_{key}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(6)
        self.btn = QPushButton("▾")
        set_role(self.btn, "flat")
        self.btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn.setFixedWidth(18)
        self.btn.setToolTip("Show or hide the conditions")
        self.btn.clicked.connect(self.toggle)
        self.title = QLabel(title)
        set_role(self.title, "heading")
        self.status = chip("", "unknown")
        head.addWidget(self.btn)
        head.addWidget(self.title)
        head.addStretch(1)
        head.addWidget(self.status)
        lay.addLayout(head)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)
        self.notes = []
        self.notes_box = QVBoxLayout()
        self.notes_box.setSpacing(4)
        lay.addLayout(self.notes_box)
        self.body = QWidget()
        body = QVBoxLayout(self.body)
        body.setContentsMargins(0, 2, 0, 0)
        body.setSpacing(2)
        self.intro = QLabel()
        self.intro.setWordWrap(True)
        set_role(self.intro, "hint")
        body.addWidget(self.intro)
        self.chain = ChainView()
        body.addWidget(self.chain)
        lay.addWidget(self.body)
        self._links = links
        self._on_toggle = on_toggle
        self.set_collapsed(collapsed)

    def set_collapsed(self, collapsed: bool):
        self.body.setVisible(not collapsed)
        self.btn.setText("▸" if collapsed else "▾")

    def collapsed(self) -> bool:
        return self.body.isHidden()

    def toggle(self):
        self.set_collapsed(not self.collapsed())
        if self._on_toggle:
            self._on_toggle()

    def mousePressEvent(self, event):
        # A click on the heading folds the card (the rest of it is for reading and links)
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() < self.summary.y():
            self.toggle()
            return
        super().mousePressEvent(event)

    def show_group(self, g: il.Group):
        if self.title.text() != g.title:
            self.title.setText(g.title)
        if self.status.text() != g.status:
            self.status.setText(g.status)
        set_tone(self.status, LEVEL_TONES.get(g.level, "off"))
        if self.summary.text() != g.summary:
            self.summary.setText(g.summary)
        notes = list(g.notes)
        if g.disagree:
            notes.append((g.disagree, "warn"))
        while len(self.notes) < len(notes):
            lbl = QLabel()
            lbl.setWordWrap(True)
            set_role(lbl, "note")
            self.notes_box.addWidget(lbl)
            self.notes.append(lbl)
        for i, lbl in enumerate(self.notes):
            if i < len(notes):
                text, level = notes[i]
                if lbl.text() != text:
                    lbl.setText(text)
                set_prop(lbl, "level", level)
                lbl.setVisible(True)
            else:
                lbl.setVisible(False)
        if self.intro.text() != g.intro:
            self.intro.setText(g.intro)
        self.chain.show_group(g, self._links)


class _Columns(QWidget):
    """The cards in one, two or three columns as the width allows, each column a stack of cards at
    their own heights (a grid would stretch a short card to the height of its row's tallest). Which
    card goes in which column is fixed for each number of columns, so cards don't move about as
    their contents change."""
    ORDER = {1: [list(GROUP_TITLES)],
             2: [["relay", "supplies", "hmi"], ["gv_open", "source_cold", "cesium", "reset"]],
             3: [["relay", "hmi"], ["gv_open", "source_cold", "reset"], ["supplies", "cesium"]]}

    def __init__(self, cards: dict, gap: int = 8, parent=None):
        super().__init__(parent)
        self.cards, self.gap = cards, gap
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(gap)
        self.columns = []
        for _ in range(3):
            col = QWidget()
            lay = QVBoxLayout(col)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(gap)
            lay.addStretch(1)
            row.addWidget(col, 1)
            self.columns.append(col)
        self.n = 0
        self.arrange(1)
        self.setMinimumWidth(CARD_MIN_W)

    def fit(self, width: int) -> int:
        return max(1, min(3, (width + self.gap) // (CARD_MIN_W + self.gap)))

    def arrange(self, n: int):
        if n == self.n:
            return
        self.n = n
        for i, keys in enumerate(self.ORDER[n]):
            lay = self.columns[i].layout()
            for j, key in enumerate(keys):
                lay.insertWidget(j, self.cards[key])
        for i, col in enumerate(self.columns):
            col.setVisible(i < n)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.arrange(self.fit(event.size().width()))


class InterlocksWidget(QWidget):
    """The panel. update_telemetry(view) 10 times a second while it is in view."""

    def __init__(self, fault_engine=None, parent=None, clock=None, wall_clock=None):
        super().__init__(parent)
        self._wall_clock = wall_clock or time.time      # (how old the gauge relays' readings are)
        self.setObjectName("PanelBody")
        self.fault_text = il.fault_descriptions(getattr(fault_engine, "fault_structures", {}) or {})
        self.loop = il.LoopConfig.load()
        self._loop_mtime = il.config_mtime()
        self._loop_checked = 0.0
        self._clock = clock or time.monotonic
        self.disagreements = il.Disagreements(clock=self._clock)
        self.settings = gui_settings("SurreyIonBeam", "SINDIE_HMI")
        folded = self.settings.value("interlocks/collapsed", [], type=list) or []
        self.overview = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.machine_chip = chip("WAITING FOR DATA", "unknown")
        self.machine_text = QLabel("")
        self.machine_text.setWordWrap(True)
        set_role(self.machine_text, "caption")
        top.addWidget(self.machine_chip, 0, Qt.AlignmentFlag.AlignTop)
        top.addWidget(self.machine_text, 1)
        lay.addLayout(top)
        self.key = self._make_key()
        lay.addWidget(self.key)

        self.cards = {key: GroupCard(key, title, self._link, collapsed=key in folded, on_toggle=self._save_folded)
                      for key, title in GROUP_TITLES.items()}
        self.columns = _Columns(self.cards)
        lay.addWidget(self.columns)
        lay.addStretch(1)
        theme.theme_changed().connect(self._restyle)

    # --- the key -------------------------------------------------------------------------------
    KEY = [(il.Condition("", il.OK), "plain", "holds"),
           (il.Condition("", il.HOLDING), "attention", "holding it off"),
           (il.Condition("", il.UNKNOWN), "plain", "not known"),
           (il.Condition("", il.HIDDEN), "plain", "can't be seen"),
           (il.Condition("", il.ON, kind=il.REQUEST), "plain", "your request"),
           (il.Condition("", il.ON, kind=il.OUTPUT), "plain", "output on")]

    def _make_key(self) -> QWidget:
        """What the nodes mean, in as many columns as fit (it mustn't make the panel wider)."""
        box = ReflowGrid(max_columns=len(self.KEY), spacing=4)
        self._key_icons = []
        for cond, emphasis, text in self.KEY:
            item = QWidget()
            lay = QHBoxLayout(item)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(3)
            icon = QLabel()
            icon.setPixmap(legend_icon(cond, emphasis))
            self._key_icons.append((icon, cond, emphasis))
            label = QLabel(text)
            set_role(label, "hint")
            lay.addWidget(icon)
            lay.addWidget(label)
            lay.addStretch(1)
            box.add(item)
        return box

    def _restyle(self, *_):
        for icon, cond, emphasis in self._key_icons:
            icon.setPixmap(legend_icon(cond, emphasis))
        if self.overview is not None:
            for g in self.overview.groups:
                card = self.cards.get(g.key)
                if card is not None:
                    card.show_group(g)

    # --- links and folding -----------------------------------------------------------------------
    def _link(self, href: str):
        if href.startswith("group:"):
            card = self.cards.get(href[len("group:"):])
            if card is None:
                return
            card.set_collapsed(False)
            area = self._scroll_area()
            if area is not None:
                QTimer.singleShot(0, lambda: area.ensureWidgetVisible(card, 0, 8))

    def _scroll_area(self):
        w = self.parentWidget()
        while w is not None and not isinstance(w, QScrollArea):
            w = w.parentWidget()
        return w

    def _save_folded(self):
        self.settings.setValue("interlocks/collapsed", [k for k, c in self.cards.items() if c.collapsed()])

    # --- telemetry -------------------------------------------------------------------------------
    def _reload_loop(self):
        now = self._clock()
        if now - self._loop_checked < CONFIG_CHECK_S:
            return
        self._loop_checked = now
        mtime = il.config_mtime()
        if mtime != self._loop_mtime:
            self._loop_mtime = mtime
            self.loop = il.LoopConfig.load()

    def update_telemetry(self, data: dict):
        self._reload_loop()
        ov = self.disagreements.filter(il.evaluate(data, self.loop, self.fault_text, now=self._wall_clock()))
        self.overview = ov
        if self.machine_chip.text() != ov.machine_name:
            self.machine_chip.setText(ov.machine_name)
        set_tone(self.machine_chip, LEVEL_TONES.get(ov.machine_level, "off"))
        if self.machine_text.text() != ov.machine:
            self.machine_text.setText(ov.machine)
        for g in ov.groups:
            card = self.cards.get(g.key)
            if card is not None:
                card.show_group(g)
