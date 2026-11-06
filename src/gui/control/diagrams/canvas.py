"""The drawing surface of the machine diagrams: a picture of the machine in its own units, scaled to
fit the panel, with parts that can be pointed at (a tooltip) and clicked (the panel opens the part's
faceplate).

The machine is drawn in the diagram's units (about a pixel of the thesis' drawings each), scaled
and centred in the panel. Lines are drawn at a fixed width on the screen whatever the scale; text
is drawn at a readable size too, a little smaller or larger with the panel, so a label never
becomes too small to read nor crowds what it labels. layout_problems() finds the labels that would
overlap each other, a part, a pipe or the edge at a given panel size (the tests check each diagram
with it).
"""
import math
from dataclasses import dataclass

from PyQt6.QtCore import QPointF, QRectF, QSize, QSizeF, Qt, pyqtSignal
from PyQt6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath, QPainterPathStroker,
                         QPen, QTransform)
from PyQt6.QtWidgets import QSizePolicy, QToolTip, QWidget

from src.gui import theme

MARGIN_PX = 10
HIT_PX = 6              # a part can be clicked this far outside its outline
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
            stroker = QPainterPathStroker()
            stroker.setWidth(2 * pad)
            return stroker.createStroke(self.shape).contains(at)
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
    (strokes) and boxes (obstacles) labels must keep clear of, for layout_problems()."""

    def __init__(self, painter: QPainter, transform: QTransform, scale: float, hover: str, selected: str,
                 text_scale: float, legend_open: bool = True):
        self.p = painter
        self.t = theme.tokens()
        self.transform = transform
        self.scale = scale
        self.hover = hover
        self.selected = selected
        self.text_scale = text_scale
        self.legend_open = legend_open
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
        """A line drawn px wide on the screen, whatever the scale."""
        pen = QPen(QColor(colour), px, style, cap, Qt.PenJoinStyle.RoundJoin)
        pen.setCosmetic(True)
        return pen

    def to_px(self, x: float, y: float) -> QPointF:
        return self.transform.map(QPointF(x, y))

    def to_units(self, point: QPointF) -> QPointF:
        inverse, _ok = self.transform.inverted()
        return inverse.map(point)

    def units(self, px: float) -> float:
        """A length on the screen, in the drawing's units."""
        return px / self.scale

    def emphasis(self, key: str):
        """'selected', 'hover' or None for a part."""
        if key and key == self.selected:
            return "selected"
        if key and key == self.hover:
            return "hover"
        return None

    def ring(self, key: str, box: QRectF, radius: float = 6.0):
        """The outline of a part pointed at or selected (blue: interaction)."""
        how = self.emphasis(key)
        if how is None:
            return
        p = self.p
        p.save()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(self.pen(self.c("accent"), 2.5 if how == "selected" else 1.5,
                          Qt.PenStyle.SolidLine if how == "selected" else Qt.PenStyle.DashLine))
        pad = 6 / self.scale
        p.drawRoundedRect(box.adjusted(-pad, -pad, pad, pad), radius, radius)
        p.restore()

    def stroke(self, path: QPainterPath, width_units: float):
        """A pipe labels must keep clear of (symbols.line and symbols.tube say so themselves)."""
        self.strokes.append((path, width_units))

    # --- text at screen size ------------------------------------------------------------------
    def measure(self, kind: str, text: str) -> QSizeF:
        """The size of a text (several lines stacked) or, kind 'chip', a chip, on the screen."""
        if kind == "chip":
            fm = QFontMetricsF(self.fonts["chip"])
            return QSizeF(fm.horizontalAdvance(text) + 12 * self.text_scale, fm.height() + 5 * self.text_scale)
        fm = QFontMetricsF(self.fonts[kind])
        lines = text.split("\n")
        return QSizeF(max(fm.horizontalAdvance(s) for s in lines), fm.height() * len(lines))

    def _draw(self, rect: QRectF, kind: str, text: str, extra=None, h_flag=A.AlignLeft, check=True):
        """A text or chip in its screen rect. extra: a chip's tone, or a text's colour."""
        p = self.p
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
        if check:
            self.placed.append((QRectF(rect), text))

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
        fm = QFontMetricsF(self.fonts["name"])
        row_h, sym_w, pad, col_gap = fm.height() * 1.5, fm.height() * 1.5, 9 * ts, 16 * ts
        head = QFontMetricsF(self.fonts["name-b"]).height() + 6 * ts
        arrow = "▾" if self.legend_open else "▸"
        title_w = QFontMetricsF(self.fonts["name-b"]).horizontalAdvance(f"{title}  {arrow}")
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
        p.setFont(self.fonts["name-b"])
        p.setPen(self.c("text_muted"))
        p.drawText(QRectF(box.left() + pad, box.top() + 3 * ts, w - 2 * pad, head), int(A.AlignLeft | A.AlignVCenter),
                   f"{title}  {arrow}")
        p.restore()
        if self.legend_open:
            left = box.left() + pad
            for rows, col_w in zip(columns, col_ws):
                for i, (draw, text) in enumerate(rows):
                    cy = box.top() + head + 3 * ts + row_h * (i + 0.5)
                    centre = self.to_units(QPointF(left + sym_w / 2, cy))
                    draw(self, centre, self.units(fm.height() * 1.25))
                    self._draw(QRectF(left + sym_w + 6 * ts, cy - fm.height() / 2, col_w, fm.height()), "name", text,
                               check=False)
                left += col_w + col_gap
        self.obstacles.append((QRectF(box), "the key"))
        return box


class DiagramCanvas(QWidget):
    """Draws a diagram (diagram.drawing_size, diagram.paint(view), diagram.parts()) and reports the part
    clicked (its key; '' for empty space)."""
    clicked = pyqtSignal(str)

    def __init__(self, diagram, design_scale: float, minimum: QSize, parent=None):
        """diagram.drawing_origin, diagram.drawing_size: the drawing's top left corner and size, in
        its units.
        design_scale: the scale (screen px per unit) the labels were placed for; the text is drawn
        at its normal size there. minimum: the smallest the drawing can be shown."""
        super().__init__(parent)
        self.diagram = diagram
        self.design_scale = design_scale
        self.hover = ""
        self.selected = ""
        self.legend_open = True
        self.legend_box = QRectF()      # where the key was last drawn, on the screen
        self._pressed = None
        self.setMouseTracking(True)
        self.setMinimumSize(minimum)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)     # (clicks here never take the keyboard: see control_main)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)

    def sizeHint(self):
        size = self.diagram.drawing_size
        return QSize(int(size.width() * self.design_scale) + 2 * MARGIN_PX,
                     int(size.height() * self.design_scale) + 2 * MARGIN_PX)

    # --- geometry -------------------------------------------------------------------------------
    def scale(self) -> float:
        size = self.diagram.drawing_size
        return max(0.05, min((self.width() - 2 * MARGIN_PX) / size.width(),
                             (self.height() - 2 * MARGIN_PX) / size.height()))

    def transform(self) -> QTransform:
        s = self.scale()
        size, origin = self.diagram.drawing_size, self.diagram.drawing_origin
        ox = (self.width() - size.width() * s) / 2 - origin.x() * s
        oy = (self.height() - size.height() * s) / 2 - origin.y() * s
        return QTransform(s, 0, 0, s, ox, oy)

    def text_scale(self) -> float:
        return max(0.85, min(1.2, (self.scale() / self.design_scale) ** 0.5))

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
    def paint_on(self, p: QPainter) -> View:
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.fillRect(self.rect(), theme.color("panel"))
        transform = self.transform()
        p.setTransform(transform)
        view = View(p, transform, self.scale(), self.hover, self.selected, self.text_scale(), self.legend_open)
        self.diagram.paint(view)
        self.legend_box = next((box for box, what in view.obstacles if what == "the key"), QRectF())
        return view

    def paintEvent(self, event):
        p = QPainter(self)
        self.paint_on(p)
        p.end()

    # --- the mouse --------------------------------------------------------------------------
    def mouseMoveEvent(self, event):
        part = self.part_at(event.position())
        key = part.key if part is not None else ""
        if key != self.hover:
            self.hover = key
            self.setCursor(Qt.CursorShape.PointingHandCursor if part is not None and part.clickable
                           else Qt.CursorShape.ArrowCursor)
            self.update()
            if part is not None and part.tip:
                QToolTip.showText(event.globalPosition().toPoint(), part.tip, self)
            else:
                QToolTip.hideText()

    def leaveEvent(self, event):
        if self.hover:
            self.hover = ""
            self.update()
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
