"""The drawing surface of the machine diagrams: a picture of the machine in its own units, scaled to
fit the panel, with parts that can be pointed at (a tooltip) and clicked (the panel opens the part's
faceplate).

The machine is drawn in the diagram's units (about a pixel of the thesis' drawings each), scaled
and centred in the panel. Lines are drawn at a fixed width on the screen; text at a readable size,
a little smaller or larger with the panel. Below the diagram's READABLE size (where the text is at
its smallest readable size) the whole drawing shrinks together, text and lines too, as a picture
zoomed out: it always fits its panel, and what is clear at READABLE stays clear smaller.
layout_problems() finds the labels that would overlap each other, a part, a pipe or the edge at a
given panel size (the tests check each diagram with it).

Repainting is kept cheap, as the panels refresh ten times a second: the machine (everything but the
text and the rings round the part pointed at or selected) is recorded each refresh and drawn to a
cached picture only when it changed - a valve opening, a tone - and only the text and rings that
changed are redrawn over it.
"""
import math
from dataclasses import dataclass

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, QSize, QSizeF, Qt, pyqtSignal
from PyQt6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath, QPainterPathStroker,
                         QPen, QPicture, QPixmap, QRegion, QTransform)
from PyQt6.QtWidgets import QSizePolicy, QToolTip, QWidget

from src.gui import theme

MARGIN_PX = 10
HIT_PX = 6              # a part can be clicked this far outside its outline
SMALLEST = QSize(240, 160)      # the panel scrolls only below this
ROW_GAP_PX = 2          # between the rows of a block of text
ITEM_GAP_PX = 6         # between a name and its chip

A = Qt.AlignmentFlag
H_FLAGS = A.AlignLeft | A.AlignRight | A.AlignHCenter
V_FLAGS = A.AlignTop | A.AlignBottom | A.AlignVCenter


@dataclass
class Part:
    """Something on a diagram that can be pointed at and clicked: its key, its outline in the
    diagram's units, and its tooltip. clickable: it has a faceplate. round: it is the circle in the
    box (a pump). shape: its outline when a box would take in too much (the magnet's arc).
    holds_text: its labels are written inside it (the PLC's box), so the layout check only minds
    text that spills out of it."""
    key: str
    box: QRectF
    tip: str = ""
    clickable: bool = True
    round: bool = False
    shape: QPainterPath = None
    holds_text: bool = False

    def hit(self, at: QPointF, pad: float) -> bool:
        if self.shape is not None:
            if self.shape.contains(at):
                return True
            made = getattr(self, "_edge", None)         # (made once for each scale: pointing is often)
            if made is None or made[0] != pad:
                stroker = QPainterPathStroker()
                stroker.setWidth(2 * pad)
                made = self._edge = (pad, stroker.createStroke(self.shape))
            return made[1].contains(at)
        if self.round:
            c = self.box.center()
            return math.hypot(at.x() - c.x(), at.y() - c.y()) <= self.box.width() / 2 + pad
        return self.box.adjusted(-pad, -pad, pad, pad).contains(at)


# Each tone's colours on the drawing - (fill, line, text) - matching the panels' chips and buttons
# (theme.build_stylesheet): grey is normal, green an output that is on, blue automatic or moving,
# violet held by a sequence, amber and red what needs attention.
def tone_colours(t: dict, tone) -> tuple:
    q = theme.qcolor
    if tone == "on":
        return q(t["ok_fill"]), q(t["ok"]), q(t["ok_fill_text"])
    if tone == "ok":
        return q(t["ok_bg"]), q(t["ok"]), q(t["ok"])
    if tone in ("warn", "armed"):
        return q(t["warn_fill"]), q(t["warn_fill"]), q(t["warn_fill_text"])
    if tone == "fault":
        return q(t["fault_fill"]), q(t["fault_fill"]), q(t["fault_fill_text"])
    if tone == "info":
        return q(t["info_bg"]), q(t["info"]), q(t["info"])
    if tone == "seq":
        return q(t["seq_bg"]), q(t["seq"]), q(t["seq"])
    if tone == "off":
        return q(t["off_bg"]), q(t["border_strong"]), q(t["off_text"])
    if tone == "primary":
        return q(t["accent"]), q(t["accent"]), q(t["on_accent"])
    if tone == "unknown":
        return QColor(0, 0, 0, 0), q(t["text_faint"]), q(t["text_faint"])
    # None: drawn only (not read by the software)
    return q(t["card"]), q(t["text_faint"]), q(t["text_muted"])


def line_style(tone) -> Qt.PenStyle:
    """Dashed: not known now (no live value); dotted: never read by the software."""
    if tone == "unknown":
        return Qt.PenStyle.DashLine
    if tone is None:
        return Qt.PenStyle.DotLine
    return Qt.PenStyle.SolidLine


class View:
    """What a diagram draws with: the theme's colours, the scale, and text placed at points of the
    drawing but drawn at screen size. It keeps where each label went (placed), and the pipes
    (strokes) and boxes (obstacles) labels must keep clear of, for layout_problems().

    zoom: how far below its READABLE size the drawing is shown (1 at or above it): lines and the
    gaps drawn in screen pixels shrink with it. text_ops: a list to put the text in, to draw later
    over the cached machine (DiagramCanvas), with the rings round the parts pointed at and selected
    (rings); None draws them straight away."""

    def __init__(self, painter: QPainter, transform: QTransform, scale: float, hover: str, selected: str,
                 text_scale: float, legend_open: bool = True, zoom: float = 1.0, text_ops: list = None):
        self.p = painter
        self.t = theme.tokens()
        self.transform = transform
        self.scale = scale
        self.hover = hover
        self.selected = selected
        self.text_scale = text_scale
        self.legend_open = legend_open
        self.zoom = zoom
        self.text_ops = text_ops
        self.rings = []         # (screen rect, corner radius px, 'hover' or 'selected')
        self.placed = []        # (screen rect, text)
        self.strokes = []       # (path in units, width in units)
        self.obstacles = []     # (screen rect, what)
        ui, mono = theme.fonts()["ui"], theme.fonts()["mono"]
        self.fonts = {
            "name": self._font(ui, 9.0), "name-b": self._font(ui, 9.0, bold=True),
            "value": self._font(mono, 9.5), "value-b": self._font(mono, 10.0, bold=True),
            "note": self._font(ui, 8.0), "chip": self._font(ui, 7.5, bold=True),
            "title": self._font(ui, 10.5, bold=True), "big": self._font(mono, 13.0, bold=True),
        }
        self._metrics = {}

    def _font(self, family: str, points: float, bold: bool = False) -> QFont:
        f = QFont(family)
        f.setPointSizeF(points * self.text_scale)
        if bold:
            f.setWeight(QFont.Weight.DemiBold)
        return f

    # --- colours ----------------------------------------------------------------------------
    def c(self, name: str) -> QColor:
        return theme.qcolor(self.t[name])

    def tone(self, tone) -> tuple:
        return tone_colours(self.t, tone)

    def pen(self, colour, px: float = 1.5, style=Qt.PenStyle.SolidLine, cap=Qt.PenCapStyle.RoundCap) -> QPen:
        """A line drawn px wide on the screen whatever the scale (thinner when zoomed out)."""
        pen = QPen(QColor(colour), px * max(self.zoom, 0.5), style, cap, Qt.PenJoinStyle.RoundJoin)
        pen.setCosmetic(True)
        return pen

    def to_px(self, x: float, y: float) -> QPointF:
        return self.transform.map(QPointF(x, y))

    def to_units(self, point: QPointF) -> QPointF:
        inverse, _ok = self.transform.inverted()
        return inverse.map(point)

    def units(self, px: float) -> float:
        """A gap or a width drawn px screen pixels wide (shrinking when zoomed out), in the drawing's
        units."""
        return px * self.zoom / self.scale

    def screen_units(self, px: float) -> float:
        """A length measured on the screen as it is (text's), in the drawing's units."""
        return px / self.scale

    def emphasis(self, key: str):
        """'selected', 'hover' or None for a part."""
        if key and key == self.selected:
            return "selected"
        if key and key == self.hover:
            return "hover"
        return None

    def ring(self, key: str, box: QRectF, radius: float = 6.0):
        """The outline of a part pointed at or selected (blue: interaction), over the machine: box and
        radius in the drawing's units."""
        how = self.emphasis(key)
        if how is None:
            return
        pad = self.units(6)
        ring = (self.transform.mapRect(box.adjusted(-pad, -pad, pad, pad)), radius * self.scale, how)
        self.rings.append(ring)
        if self.text_ops is None:
            self.draw_ring(self.p, *ring)

    def draw_ring(self, p: QPainter, rect: QRectF, radius_px: float, how: str):
        p.save()
        p.setTransform(QTransform())
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(self.pen(self.c("accent"), 2.5 if how == "selected" else 1.5,
                          Qt.PenStyle.SolidLine if how == "selected" else Qt.PenStyle.DashLine))
        p.drawRoundedRect(rect, radius_px, radius_px)
        p.restore()

    def stroke(self, path: QPainterPath, width_units: float):
        """A pipe labels must keep clear of (symbols.line and symbols.tube say so themselves)."""
        self.strokes.append((path, width_units))

    # --- text at screen size ------------------------------------------------------------------
    def metrics(self, kind: str) -> QFontMetricsF:
        fm = self._metrics.get(kind)
        if fm is None:
            fm = self._metrics[kind] = QFontMetricsF(self.fonts[kind])
        return fm

    def measure(self, kind: str, text: str) -> QSizeF:
        """The size of a text (several lines stacked) or, kind 'chip', a chip, on the screen."""
        if kind == "chip":
            fm = self.metrics("chip")
            return QSizeF(fm.horizontalAdvance(text) + 12 * self.text_scale, fm.height() + 5 * self.text_scale)
        fm = self.metrics(kind)
        lines = text.split("\n")
        return QSizeF(max(fm.horizontalAdvance(s) for s in lines), fm.height() * len(lines))

    def _draw(self, rect: QRectF, kind: str, text: str, extra=None, h_flag=A.AlignLeft, check=True):
        """A text or chip in its screen rect (recorded, or drawn now). extra: a chip's tone, or a
        text's colour."""
        if check:
            self.placed.append((QRectF(rect), text))
        if self.text_ops is not None:
            self.text_ops.append((QRectF(rect), kind, text, extra, h_flag))
        else:
            self.draw_text(self.p, QRectF(rect), kind, text, extra, h_flag)

    def draw_text(self, p: QPainter, rect: QRectF, kind: str, text: str, extra=None, h_flag=A.AlignLeft):
        p.save()
        p.setTransform(QTransform())
        if kind == "chip":
            fill, line, ink = self.tone(extra)
            if extra in ("ok", "info", "seq"):
                line = QColor(line)
                line.setAlpha(115)
            p.setPen(self.pen(line, 1.0, Qt.PenStyle.DashLine if extra == "unknown" else Qt.PenStyle.SolidLine))
            p.setBrush(fill)
            p.drawRoundedRect(rect, 3.5, 3.5)
            p.setFont(self.fonts["chip"])
            p.setPen(ink)
            p.drawText(rect, int(A.AlignCenter), text)
        else:
            colour = extra if extra is not None else self.c(
                {"name": "text_muted", "note": "text_faint"}.get(kind, "text"))
            p.setFont(self.fonts[kind])
            p.setPen(QColor(colour))
            p.drawText(rect, int(h_flag | A.AlignVCenter), text)
        p.restore()

    def _place(self, x: float, y: float, size: QSizeF, align) -> QRectF:
        at = self.to_px(x, y)
        w, h = size.width(), size.height()
        left = at.x() - (w if align & A.AlignRight else w / 2 if align & A.AlignHCenter else 0)
        top = at.y() - (h if align & A.AlignBottom else 0 if align & A.AlignTop else h / 2)
        return QRectF(left, top, w, h)

    @staticmethod
    def _h(align):
        return (A.AlignRight if align & A.AlignRight else A.AlignHCenter if align & A.AlignHCenter
                else A.AlignLeft)

    def text(self, x: float, y: float, text: str, kind: str = "name", align=None, colour=None,
             check: bool = True) -> QRectF:
        """Text at the drawing's point (x, y), placed by align (Qt flags: AlignLeft - starting at the
        point; AlignRight - ending at it; AlignHCenter; AlignTop - its top at the point; AlignBottom;
        AlignVCenter). Several lines are stacked. Returns where it went, on the screen."""
        if not text:
            return QRectF()
        align = align if align is not None else A.AlignLeft | A.AlignVCenter
        rect = self._place(x, y, self.measure(kind, text), align)
        self._draw(rect, kind, text, colour, self._h(align), check)
        return rect

    def chip(self, x: float, y: float, text: str, tone, align=None, check: bool = True) -> QRectF:
        """A status chip, as on the panels, at the drawing's point (placed as text())."""
        if not text:
            return QRectF()
        align = align if align is not None else A.AlignLeft | A.AlignVCenter
        rect = self._place(x, y, self.measure("chip", text), align)
        self._draw(rect, "chip", text, tone, check=check)
        return rect

    def block(self, x: float, y: float, rows, align) -> QRectF:
        """Rows of items stacked into one block, placed at (x, y) by align (as text()). A row is a
        list of (kind, text[, tone or colour]) side by side: kind 'chip' is a chip (its tone), any
        other a text (its colour). Items with no text, and rows with none, are left out."""
        rows = [[item for item in row if item[1]] for row in rows]
        rows = [row for row in rows if row]
        if not rows:
            return QRectF()
        gap, row_gap = ITEM_GAP_PX * self.text_scale, ROW_GAP_PX * self.text_scale
        sizes = [[self.measure(item[0], item[1]) for item in row] for row in rows]
        widths = [sum(s.width() for s in row) + gap * (len(row) - 1) for row in sizes]
        heights = [max(s.height() for s in row) for row in sizes]
        block = self._place(x, y, QSizeF(max(widths), sum(heights) + row_gap * (len(rows) - 1)), align)
        h_flag = self._h(align)
        top = block.top()
        for row, row_sizes, width, height in zip(rows, sizes, widths, heights):
            left = (block.right() - width if h_flag == A.AlignRight else
                    block.center().x() - width / 2 if h_flag == A.AlignHCenter else block.left())
            for (kind, text, *extra), size in zip(row, row_sizes):
                rect = QRectF(left, top + (height - size.height()) / 2, size.width(), size.height())
                self._draw(rect, kind, text, extra[0] if extra else None)
                left += size.width() + gap
            top += height + row_gap
        return block

    # --- the key ------------------------------------------------------------------------------
    def legend(self, x: float, y: float, columns, align=A.AlignRight | A.AlignTop, title: str = "Key") -> QRectF:
        """The key, at screen size, placed at the drawing's point (x, y) by align: columns of rows
        of (draw(view, centre, size), text), where draw draws the symbol at a centre and size in the
        drawing's units. Folded (legend_open False), only its title shows. Returns its box on the
        screen, which is the diagram's 'legend' part (a click folds or unfolds it)."""
        ts = self.text_scale
        fm = self.metrics("name")
        row_h, sym_w, pad, col_gap = fm.height() * 1.5, fm.height() * 1.5, 9 * ts, 16 * ts
        head = self.metrics("name-b").height() + 6 * ts
        arrow = "▾" if self.legend_open else "▸"
        title_w = self.metrics("name-b").horizontalAdvance(f"{title}  {arrow}")
        col_ws = [max(fm.horizontalAdvance(text) for _draw, text in rows) + sym_w + 6 * ts for rows in columns]
        if self.legend_open:
            w = max(title_w, sum(col_ws) + col_gap * (len(columns) - 1)) + 2 * pad
            h = head + row_h * max(len(rows) for rows in columns) + pad * 1.5
        else:
            w, h = title_w + 2 * pad, head + pad * 0.6
        box = self._place(x, y, QSizeF(w, h), align)
        p = self.p
        p.save()
        p.setTransform(QTransform())
        p.setPen(self.pen(self.c("border"), 1.0))
        bg = QColor(self.c("card"))
        bg.setAlpha(235)
        p.setBrush(QBrush(bg))
        p.drawRoundedRect(box, 7, 7)
        p.restore()
        self._draw(QRectF(box.left() + pad, box.top() + 3 * ts, w - 2 * pad, head), "name-b", f"{title}  {arrow}",
                   self.c("text_muted"), check=False)
        if self.legend_open:
            left = box.left() + pad
            for rows, col_w in zip(columns, col_ws):
                for i, (draw, text) in enumerate(rows):
                    cy = box.top() + head + 3 * ts + row_h * (i + 0.5)
                    centre = self.to_units(QPointF(left + sym_w / 2, cy))
                    draw(self, centre, self.screen_units(fm.height() * 1.25))
                    self._draw(QRectF(left + sym_w + 6 * ts, cy - fm.height() / 2, col_w, fm.height()), "name", text,
                               check=False)
                left += col_w + col_gap
        self.obstacles.append((QRectF(box), "the key"))
        return box


def _picture_bytes(picture: QPicture) -> bytes:
    """All of a recorded picture, to compare (QPicture.data() stops at the first zero byte)."""
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    picture.save(buf)
    buf.close()
    return bytes(data)


def _text_key(op) -> tuple:
    """A text (View.text_ops) as something to compare: where it is, what it says and how it looks."""
    rect, kind, text, extra, h_flag = op
    if isinstance(extra, QColor):
        extra = ("colour", extra.rgba())
    return (rect.x(), rect.y(), rect.width(), rect.height(), kind, text, extra, h_flag)


def _ring_key(ring) -> tuple:
    rect, radius, how = ring
    return ("ring", rect.x(), rect.y(), rect.width(), rect.height(), radius, how)


def _around(rect: QRectF):
    return rect.adjusted(-3, -3, 3, 3).toAlignedRect()     # (with its line's width, antialiased)


class DiagramCanvas(QWidget):
    """Draws a diagram (diagram.drawing_size, diagram.paint(view), diagram.parts()) and reports the part
    clicked (its key; '' for empty space)."""
    clicked = pyqtSignal(str)

    def __init__(self, diagram, design_scale: float, readable: QSize, parent=None):
        """diagram.drawing_origin, diagram.drawing_size: the drawing's top left corner and size, in
        its units. design_scale: the scale (screen px per unit) the labels were placed for; the text
        is drawn at its normal size there. readable: the smallest panel the text is drawn at its
        smallest readable size in; smaller, the whole drawing shrinks together."""
        super().__init__(parent)
        self.diagram = diagram
        self.design_scale = design_scale
        self.readable = readable
        self.hover = ""
        self.selected = ""
        self.legend_open = True
        self.legend_box = QRectF()      # where the key was last drawn, on the screen
        self._pressed = None
        # A drawing is (state, picture, view, signature): the state it was recorded in (size, pixel
        # ratio, what is pointed at...), the machine recorded, the texts and rings drawn over it
        # (view.text_ops, view.rings), and all as something to compare (signature: the machine's
        # bytes, the texts' and rings' keys).
        self._pending = None            # recorded by refresh(), to paint
        self._last = None               # last painted (a repaint with nothing new draws it again)
        self._painted = None            # the signature on the screen
        self._layer = None              # the machine, drawn: ((w, h, ratio), machine bytes, pixmap)
        self.setMouseTracking(True)
        self.setMinimumSize(SMALLEST)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)     # (clicks here never take the keyboard: see control_main)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)

    def sizeHint(self):
        return QSize(self.readable)

    def minimumSizeHint(self):
        return QSize(SMALLEST)

    # --- geometry -------------------------------------------------------------------------------
    def _scale_for(self, width: float, height: float) -> float:
        size = self.diagram.drawing_size
        return max(0.02, min((width - 2 * MARGIN_PX) / size.width(), (height - 2 * MARGIN_PX) / size.height()))

    def scale(self) -> float:
        return self._scale_for(self.width(), self.height())

    def zoom(self) -> float:
        """1 at or above the readable size; below it, how far the whole drawing is shrunk."""
        return min(1.0, self.scale() / self._scale_for(self.readable.width(), self.readable.height()))

    def transform(self) -> QTransform:
        s = self.scale()
        size, origin = self.diagram.drawing_size, self.diagram.drawing_origin
        ox = (self.width() - size.width() * s) / 2 - origin.x() * s
        oy = (self.height() - size.height() * s) / 2 - origin.y() * s
        return QTransform(s, 0, 0, s, ox, oy)

    def text_scale(self) -> float:
        """The text's size: a little smaller or larger with the panel down to the readable size,
        then shrinking with the drawing."""
        knee = self._scale_for(self.readable.width(), self.readable.height())
        base = max(0.85, min(1.2, (max(self.scale(), knee) / self.design_scale) ** 0.5))
        return base * self.zoom()

    def to_units(self, pos) -> QPointF:
        inverse, ok = self.transform().inverted()
        return inverse.map(QPointF(pos))

    def part_at(self, pos) -> Part:
        if self.legend_box.contains(QPointF(pos)):
            return Part("legend", QRectF(), "Fold or unfold the key")
        at = self.to_units(pos)
        pad = HIT_PX / self.scale()
        hits = [part for part in self.diagram.parts() if part.hit(at, pad)]
        if not hits:
            return None
        return min(hits, key=lambda part: part.box.width() * part.box.height())     # the smallest: on top

    def box_on_screen(self, key: str) -> QRectF:
        part = next((p for p in self.diagram.parts() if p.key == key), None)
        return self.transform().mapRect(part.box) if part else QRectF()

    # --- painting ---------------------------------------------------------------------------
    def _view(self, p: QPainter, text_ops) -> View:
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        transform = self.transform()
        p.setTransform(transform)
        return View(p, transform, self.scale(), self.hover, self.selected, self.text_scale(), self.legend_open,
                    self.zoom(), text_ops)

    def paint_on(self, p: QPainter) -> View:
        """The whole drawing, straight on to p (an image, for the layout check)."""
        p.fillRect(self.rect(), theme.color("panel"))
        view = self._view(p, None)
        self.diagram.paint(view)
        self.legend_box = next((box for box, what in view.obstacles if what == "the key"), QRectF())
        return view

    def _state(self):
        return (self.width(), self.height(), self.devicePixelRatioF(), self.hover, self.selected, self.legend_open)

    def _record(self):
        """The drawing as it is now: the machine recorded (not drawn), and the text listed."""
        picture = QPicture()
        p = QPainter(picture)
        view = self._view(p, [])
        self.diagram.paint(view)
        p.end()
        overlay = tuple(_text_key(op) for op in view.text_ops) + tuple(_ring_key(ring) for ring in view.rings)
        return (self._state(), picture, view, (_picture_bytes(picture), overlay))

    @staticmethod
    def _changed(before, after):
        """Where the drawing 'after' looks different from 'before' on the screen: a region, or None
        for all of it (the machine changed)."""
        if before is None or before[0] != after[0]:
            return None
        region = QRegion()
        for key in set(before[1]) ^ set(after[1]):
            region = region.united(_around(QRectF(*key[1:5]) if key[0] == "ring" else QRectF(*key[:4])))
        return region

    def refresh(self):
        """The telemetry was refreshed (or the part pointed at changed): repaint what changed in the
        drawing, if anything did - all of it if the machine did, or else only the texts and rings
        that changed."""
        drawing = self._record()
        changed = self._changed(self._painted, drawing[3])
        if changed is not None and changed.isEmpty():
            self._pending = None
            self._last = drawing            # (what is on the screen, in the state now)
            return
        self._pending = drawing
        if changed is None:
            self.update()
        else:
            self.update(changed)

    def invalidate(self):
        """Draw it all again (the theme changed)."""
        self._pending = self._last = self._painted = self._layer = None
        self.update()

    def paintEvent(self, event):
        state = self._state()
        pending, self._pending = self._pending, None
        if pending is not None and pending[0] == state:
            drawing = pending
        elif pending is None and self._last is not None and self._last[0] == state:
            drawing = self._last            # (uncovered, say: nothing new to draw)
        else:
            drawing = self._record()
        _state, picture, view, signature = drawing
        key = state[:3]
        if self._layer is None or self._layer[0] != key or self._layer[1] != signature[0]:
            dpr = state[2]
            pixmap = QPixmap(max(1, round(self.width() * dpr)), max(1, round(self.height() * dpr)))
            pixmap.setDevicePixelRatio(dpr)
            pixmap.fill(theme.color("panel"))
            lp = QPainter(pixmap)
            lp.drawPicture(0, 0, picture)
            lp.end()
            self._layer = (key, signature[0], pixmap)
        region = event.region()
        p = QPainter(self)
        p.drawPixmap(0, 0, self._layer[2])
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        for ring in view.rings:
            if region.intersects(_around(ring[0])):
                view.draw_ring(p, *ring)
        for op in view.text_ops:
            if region.intersects(_around(op[0])):
                view.draw_text(p, *op)
        self._shadow(p)
        p.end()
        # What changed since the screen was last painted, outside what was painted now (a part
        # uncovered just as the telemetry changed): that too, next.
        missed = self._changed(self._painted, signature)
        missed = (QRegion(self.rect()) if missed is None else missed).subtracted(region)
        self._painted = signature
        self._last = drawing
        self.legend_box = next((box for box, what in view.obstacles if what == "the key"), QRectF())
        if not missed.isEmpty():
            self.update(missed)

    def _shadow(self, p: QPainter):
        """A soft shadow under the open faceplate (drawn here: a blur on the card itself would be
        drawn again on every refresh)."""
        plate = getattr(self.diagram, "faceplate", None)
        if plate is None or plate.isHidden():
            return
        rect = QRectF(plate.geometry()).translated(-self.x(), -self.y())
        p.save()
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(1, 9):
            p.setBrush(QColor(0, 0, 0, 7))
            p.drawRoundedRect(rect.adjusted(-i * 0.8, -i * 0.5, i * 0.8, i * 1.1), 8 + i, 8 + i)
        p.restore()

    def resizeEvent(self, event):
        self._pending = None
        super().resizeEvent(event)

    # --- the mouse --------------------------------------------------------------------------
    def mouseMoveEvent(self, event):
        part = self.part_at(event.position())
        key = part.key if part is not None else ""
        if key != self.hover:
            self.hover = key
            self.setCursor(Qt.CursorShape.PointingHandCursor if part is not None and part.clickable
                           else Qt.CursorShape.ArrowCursor)
            self.refresh()
            if part is not None and part.tip:
                QToolTip.showText(event.globalPosition().toPoint(), part.tip, self)
            else:
                QToolTip.hideText()

    def leaveEvent(self, event):
        if self.hover:
            self.hover = ""
            self.refresh()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            part = self.part_at(event.position())
            self._pressed = part.key if part is not None and part.clickable else ""
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self._pressed is None:
            return
        part = self.part_at(event.position())
        key = part.key if part is not None and part.clickable else ""
        pressed, self._pressed = self._pressed, None
        if key == "legend" and pressed == "legend":
            self.legend_open = not self.legend_open
            self.update()
        elif key == pressed:
            self.clicked.emit(key)
        event.accept()


def path_through(points) -> QPainterPath:
    path = QPainterPath(QPointF(*points[0]))
    for pt in points[1:]:
        path.lineTo(QPointF(*pt))
    return path


def _circle_hits(rect: QRectF, centre: QPointF, r: float) -> bool:
    nx = min(max(centre.x(), rect.left()), rect.right())
    ny = min(max(centre.y(), rect.top()), rect.bottom())
    return math.hypot(nx - centre.x(), ny - centre.y()) < r


def layout_problems(canvas: DiagramCanvas, size: QSize, slack_px: float = 1.0) -> list:
    """What would be hard to read with the drawing at this size: a label over another, over a part,
    a pipe or the key, or off the edge. (Drawn into an image: nothing is shown.)"""
    canvas.resize(size)
    image = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
    p = QPainter(image)
    view = canvas.paint_on(p)
    p.end()
    transform = canvas.transform()
    inverse, _ok = transform.inverted()
    outlines = []
    for path, width in view.strokes:
        stroker = QPainterPathStroker()
        stroker.setWidth(width)
        outlines.append(stroker.createStroke(path))
    bounds = QRectF(0, 0, size.width(), size.height())
    labels = [(rect.adjusted(slack_px, slack_px, -slack_px, -slack_px), text) for rect, text in view.placed]
    problems = []
    for i, (rect, text) in enumerate(labels):
        name = text.replace("\n", " ")
        if not bounds.contains(rect):
            problems.append(f"{name!r} runs off the edge")
        for other, other_text in labels[i + 1:]:
            if rect.intersects(other):
                problems.append(f"{name!r} overlaps {other_text.replace(chr(10), ' ')!r}")
        in_units = inverse.mapRect(rect)
        for part in canvas.diagram.parts():
            box = transform.mapRect(part.box)
            if part.holds_text:
                if rect.intersects(box) and not box.contains(rect):
                    problems.append(f"{name!r} spills out of {part.key}")
                continue
            if part.shape is not None:
                hit = part.shape.intersects(in_units)
            elif part.round:
                hit = _circle_hits(rect, box.center(), box.width() / 2)
            else:
                hit = rect.intersects(box.adjusted(slack_px, slack_px, -slack_px, -slack_px))
            if hit:
                problems.append(f"{name!r} overlaps {part.key}")
        if any(outline.intersects(in_units) for outline in outlines):
            problems.append(f"{name!r} crosses a line")
        for box, what in view.obstacles:
            if rect.intersects(box):
                problems.append(f"{name!r} is under {what}")
    return problems
