"""The symbols the machine diagrams are drawn with, in the diagram's units (the painter carries the
diagram's scale). The vacuum symbols are those of the thesis' vacuum drawing (arch_vacuum): a pump is
a circle with its inlet funnel, and a turbo (double ring), scroll (spiral) or ion pump (three
arrows) inside; a gauge is the box with a barred V; a valve two triangles tip to tip.

Each takes a tone (canvas.tone_colours): None draws the part dotted, as the software can't read it.
"""
import math

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QPainterPath, QPolygonF

from src.gui.control.diagrams.canvas import line_style


def soft(tone):
    """A big part's tone: 'on' is drawn light (as a running pump), so a large area isn't a slab of
    green; the small parts and the chips keep the strong green."""
    return "ok" if tone == "on" else tone


def _outline(v, tone, px: float = 2.0):
    fill, line, _ink = v.tone(tone)
    return fill, v.pen(line, px, line_style(tone))


def tubes(v, runs):
    """Vacuum tubes: runs of (path, width in units, bore colour or None). Each is a wall (border_strong)
    with a lighter bore; the walls are drawn first, so tubes that meet join without a seam."""
    p = v.p
    p.save()
    p.setBrush(Qt.BrushStyle.NoBrush)
    for layer in ("wall", "bore"):
        for path, width, bore in runs:
            pen = v.pen(v.c("border_strong") if layer == "wall" else bore or v.c("card_alt"))
            pen.setCosmetic(False)
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            pen.setWidthF(width if layer == "wall" else max(width - v.units(4.4), width * 0.45))
            p.setPen(pen)
            p.drawPath(path)
    p.restore()
    for path, width, _bore in runs:
        v.stroke(path, width)


def line(v, path: QPainterPath, px: float = 2.0, colour=None, style=Qt.PenStyle.SolidLine):
    """A pipe or a wire, px wide on the screen."""
    p = v.p
    p.save()
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(v.pen(colour or v.c("text_muted"), px, style))
    p.drawPath(path)
    p.restore()
    v.stroke(path, v.units(px + 2))


def _pump_body(v, c: QPointF, r: float, tone):
    p = v.p
    fill, pen = _outline(v, tone)
    p.setPen(pen)
    p.setBrush(fill)
    p.drawEllipse(c, r, r)
    for sx in (-1, 1):            # the inlet funnel
        p.drawLine(QPointF(c.x() + sx * 0.44 * r, c.y() - 0.86 * r), QPointF(c.x() + sx * 0.87 * r, c.y() + 0.53 * r))
    return pen


def turbo(v, c: QPointF, r: float, tone, spin: float = None):
    """A turbo pump. spin: its speed as a fraction of full (0..1) - an arc around it, in steps of a
    hundredth (a speed wavering by a hertz at full speed doesn't redraw the machine: canvas.py);
    None: not shown."""
    p = v.p
    p.save()
    _pump_body(v, c, r, tone)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(c, 0.41 * r, 0.41 * r)
    p.drawEllipse(c, 0.31 * r, 0.31 * r)
    hundredths = round(100 * min(1.0, spin)) if spin is not None else 0
    if hundredths > 0:
        _fill, line_c, _ink = v.tone(tone)
        arc = v.pen(line_c, 3.0)
        p.setPen(arc)
        rr = r + v.units(7)
        p.drawArc(QRectF(c.x() - rr, c.y() - rr, 2 * rr, 2 * rr), 90 * 16, -360 * 16 * hundredths // 100)
    p.restore()


def scroll(v, c: QPointF, r: float, tone):
    p = v.p
    p.save()
    _pump_body(v, c, r, tone)
    p.setBrush(Qt.BrushStyle.NoBrush)
    path = QPainterPath()
    turns, steps = 2.6, 90
    for i in range(steps + 1):
        a = i / steps * turns * 2 * math.pi
        rad = 0.45 * r * i / steps
        pt = QPointF(c.x() + rad * math.cos(a), c.y() + rad * math.sin(a))
        path.moveTo(pt) if i == 0 else path.lineTo(pt)
    p.drawPath(path)
    p.restore()


def ion_pump(v, c: QPointF, r: float, tone):
    p = v.p
    p.save()
    _pump_body(v, c, r, tone)
    p.setBrush(Qt.BrushStyle.NoBrush)
    for deg in (-90, 30, 150):
        a = math.radians(deg)
        tip = QPointF(c.x() + 0.42 * r * math.cos(a), c.y() + 0.42 * r * math.sin(a))
        p.drawLine(c, tip)
        for side in (-1, 1):
            b = a + math.pi + side * 0.5
            p.drawLine(tip, QPointF(tip.x() + 0.16 * r * math.cos(b), tip.y() + 0.16 * r * math.sin(b)))
    p.restore()


def gauge(v, c: QPointF, s: float, tone):
    """A vacuum gauge: the box with a barred V, s units across."""
    p = v.p
    p.save()
    fill, pen = _outline(v, tone)
    p.setPen(pen)
    p.setBrush(fill)
    p.drawRect(QRectF(c.x() - s / 2, c.y() - s / 2, s, s))
    _f, _l, ink = v.tone(tone)
    p.setPen(v.pen(ink if tone not in (None, "unknown", "off") else v.c("text_muted"), 1.8))
    top, bottom = c.y() - 0.27 * s, c.y() + 0.30 * s
    p.drawLine(QPointF(c.x() - 0.27 * s, top), QPointF(c.x(), bottom))
    p.drawLine(QPointF(c.x() + 0.27 * s, top), QPointF(c.x(), bottom))
    p.drawLine(QPointF(c.x() - 0.36 * s, c.y() - 0.05 * s), QPointF(c.x() + 0.36 * s, c.y() - 0.05 * s))
    p.restore()


def valve(v, c: QPointF, s: float, tone, vertical: bool = False, gate: bool = False, actuator: str = ""):
    """Two triangles tip to tip, s units long, along the line (vertical: the line runs up and down).
    gate: a gate valve, drawn heavier, with its gate across. actuator: 'pneumatic' - a half disc on
    the stem."""
    p = v.p
    p.save()
    fill, pen = _outline(v, tone, 2.4 if gate else 1.6)
    p.setPen(pen)
    p.setBrush(fill)
    h = s / 2
    w = s * (0.42 if gate else 0.36)
    if vertical:
        a = QPolygonF([QPointF(c.x() - w, c.y() - h), QPointF(c.x() + w, c.y() - h), c])
        b = QPolygonF([QPointF(c.x() - w, c.y() + h), QPointF(c.x() + w, c.y() + h), c])
    else:
        a = QPolygonF([QPointF(c.x() - h, c.y() - w), QPointF(c.x() - h, c.y() + w), c])
        b = QPolygonF([QPointF(c.x() + h, c.y() - w), QPointF(c.x() + h, c.y() + w), c])
    p.drawPolygon(a)
    p.drawPolygon(b)
    if gate:
        if vertical:
            p.drawLine(QPointF(c.x() - w * 1.15, c.y()), QPointF(c.x() + w * 1.15, c.y()))
        else:
            p.drawLine(QPointF(c.x(), c.y() - w * 1.15), QPointF(c.x(), c.y() + w * 1.15))
    if actuator == "pneumatic":
        stem_end = QPointF(c.x() + w * 1.6, c.y()) if vertical else QPointF(c.x(), c.y() - w * 1.6)
        p.drawLine(c, stem_end)
        rr = 0.22 * s
        p.setBrush(fill)
        if vertical:
            p.drawPie(QRectF(stem_end.x() - rr, stem_end.y() - rr, 2 * rr, 2 * rr), 90 * 16, -180 * 16)
        else:
            p.drawPie(QRectF(stem_end.x() - rr, stem_end.y() - rr, 2 * rr, 2 * rr), 0, 180 * 16)
    p.restore()


def three_way(v, c: QPointF, s: float, tone):
    """A three-way valve: triangles from the left, the right and below, tips at the centre."""
    p = v.p
    p.save()
    fill, pen = _outline(v, tone, 1.6)
    p.setPen(pen)
    p.setBrush(fill)
    h, w = s / 2, s * 0.36
    for tri in ([QPointF(c.x() - h, c.y() - w), QPointF(c.x() - h, c.y() + w), c],
                [QPointF(c.x() + h, c.y() - w), QPointF(c.x() + h, c.y() + w), c],
                [QPointF(c.x() - w, c.y() + h), QPointF(c.x() + w, c.y() + h), c]):
        p.drawPolygon(QPolygonF(tri))
    p.restore()


def regulator(v, c: QPointF, s: float, tone):
    """A pressure regulator: a valve with a diaphragm on top."""
    valve(v, c, s, tone)
    p = v.p
    p.save()
    _fill, pen = _outline(v, tone, 1.6)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawLine(c, QPointF(c.x(), c.y() - 0.55 * s))
    p.drawArc(QRectF(c.x() - 0.3 * s, c.y() - 0.85 * s, 0.6 * s, 0.6 * s), 0, 180 * 16)
    p.drawLine(QPointF(c.x() - 0.3 * s, c.y() - 0.55 * s), QPointF(c.x() + 0.3 * s, c.y() - 0.55 * s))
    p.restore()


def cylinder(v, rect: QRectF, tone):
    """A gas cylinder."""
    p = v.p
    p.save()
    fill, pen = _outline(v, tone, 1.8)
    p.setPen(pen)
    p.setBrush(fill)
    neck = rect.width() * 0.32
    p.drawRoundedRect(rect, rect.width() * 0.42, rect.width() * 0.42)
    p.drawRect(QRectF(rect.center().x() - neck / 2, rect.top() - neck * 0.9, neck, neck * 0.9))
    p.restore()


def chamber(v, rect: QRectF, tone=None, radius: float = 10.0, label_colour=None):
    """A vessel or a box drawn plainly (card fill, soft border)."""
    p = v.p
    p.save()
    p.setPen(v.pen(v.c("border_strong"), 1.6))
    p.setBrush(v.c("card"))
    p.drawRoundedRect(rect, radius, radius)
    p.restore()


def cross_mark(v, c: QPointF, r: float, colour):
    """An X: a condition holding a chain off."""
    p = v.p
    p.save()
    p.setPen(v.pen(colour, 2.4))
    p.drawLine(QPointF(c.x() - r, c.y() - r), QPointF(c.x() + r, c.y() + r))
    p.drawLine(QPointF(c.x() - r, c.y() + r), QPointF(c.x() + r, c.y() - r))
    p.restore()


def tick_mark(v, c: QPointF, r: float, colour):
    p = v.p
    p.save()
    p.setPen(v.pen(colour, 2.2))
    path = QPainterPath(QPointF(c.x() - r, c.y()))
    path.lineTo(QPointF(c.x() - r * 0.3, c.y() + r * 0.7))
    path.lineTo(QPointF(c.x() + r, c.y() - r * 0.7))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(path)
    p.restore()


def swatch(v, c: QPointF, s: float, tone):
    """A tone, for the key: a small rounded square in its fill and line (dashed, dotted as drawn)."""
    p = v.p
    p.save()
    fill, pen = _outline(v, tone, 1.6)
    p.setPen(pen)
    p.setBrush(fill)
    r = s * 0.42
    p.drawRoundedRect(QRectF(c.x() - r, c.y() - r * 0.8, 2 * r, 1.6 * r), s * 0.12, s * 0.12)
    p.restore()


# --- Beam optics: drawn across a beam that runs up and down (vertical) or across ---------------------
def _pt(c: QPointF, vertical: bool, along: float, across: float) -> QPointF:
    return QPointF(c.x() + across, c.y() + along) if vertical else QPointF(c.x() + along, c.y() + across)


def _box(c: QPointF, vertical: bool, a0: float, a1: float, b0: float, b1: float) -> QRectF:
    """A box a0..a1 along the beam and b0..b1 across it, from c."""
    return QRectF(_pt(c, vertical, a0, b0), _pt(c, vertical, a1, b1)).normalized()


def _draw_boxes(v, boxes, tone, px=1.6):
    p = v.p
    p.save()
    fill, pen = _outline(v, tone, px)
    p.setPen(pen)
    p.setBrush(fill)
    for box in boxes:
        p.drawRect(box)
    p.restore()


def einzel(v, c: QPointF, vertical: bool, tone, half_gap: float = 24, depth: float = 26):
    """An Einzel lens: its three cylinders' walls either side of the beam. The outer two are earthed
    (grey); the middle one, on the supply, takes its tone."""
    lengths, gap = (16, 44, 16), 9
    a = -(sum(lengths) + gap * 2) / 2
    for i, length in enumerate(lengths):
        boxes = [_box(c, vertical, a, a + length, half_gap, half_gap + depth),
                 _box(c, vertical, a, a + length, -half_gap - depth, -half_gap)]
        _draw_boxes(v, boxes, tone if i == 1 else ("off" if tone is not None else None))
        a += length + gap


def deflector(v, c: QPointF, vertical: bool, tone, half_gap: float = 22, length: float = 58, thick: float = 8):
    """Electrostatic plates either side of the beam (the Y steerer, the neutral trap)."""
    _draw_boxes(v, [_box(c, vertical, -length / 2, length / 2, half_gap, half_gap + thick),
                    _box(c, vertical, -length / 2, length / 2, -half_gap - thick, -half_gap)], tone)


def coils(v, c: QPointF, vertical: bool, tone, half_gap: float = 24, length: float = 40, depth: float = 24):
    """A magnetic steerer: a coil either side of the beam, its windings drawn across."""
    boxes = [_box(c, vertical, -length / 2, length / 2, half_gap, half_gap + depth),
             _box(c, vertical, -length / 2, length / 2, -half_gap - depth, -half_gap)]
    _draw_boxes(v, boxes, tone)
    p = v.p
    p.save()
    _fill, pen = _outline(v, tone, 1.0)
    p.setPen(pen)
    for side in (1, -1):
        b0, b1 = (half_gap, half_gap + depth) if side > 0 else (-half_gap - depth, -half_gap)
        for k in range(1, 5):
            a = -length / 2 + k * length / 5
            p.drawLine(_pt(c, vertical, a, b0), _pt(c, vertical, a, b1))
    p.restore()


def jaws(v, c: QPointF, vertical: bool, tone, wall: float = 40, opening: float = 9, thick: float = 12):
    """A pair of slit jaws, closing in from the tube's walls."""
    _draw_boxes(v, [_box(c, vertical, -thick / 2, thick / 2, opening, wall),
                    _box(c, vertical, -thick / 2, thick / 2, -wall, -opening)], tone)


def aperture(v, c: QPointF, vertical: bool, tone, wall: float = 36, hole: float = 13, thick: float = 6):
    """A plate across the beam with a hole in it."""
    _draw_boxes(v, [_box(c, vertical, -thick / 2, thick / 2, hole, wall),
                    _box(c, vertical, -thick / 2, thick / 2, -wall, -hole)], tone, 1.4)


def drop_in_cup(v, c: QPointF, tone, inserted, travel: float = 62, size: float = 30):
    """A Faraday cup dropped into a beam that runs left to right from above: on the beam when
    inserted, raised clear of it when not; None (not known): drawn half way. Its mouth faces the
    beam."""
    lift = 0.0 if inserted is True else travel if inserted is False else travel / 2
    cy = c.y() - lift
    p = v.p
    p.save()
    fill, pen = _outline(v, tone, 1.8)
    p.setPen(pen)
    p.setBrush(fill)
    s = size
    path = QPainterPath(QPointF(c.x() - s * 0.5, cy - s * 0.5))         # the cup: open to the left
    path.lineTo(QPointF(c.x() + s * 0.5, cy - s * 0.5))
    path.lineTo(QPointF(c.x() + s * 0.5, cy + s * 0.5))
    path.lineTo(QPointF(c.x() - s * 0.5, cy + s * 0.5))
    path.lineTo(QPointF(c.x() - s * 0.5, cy + s * 0.3))
    path.lineTo(QPointF(c.x() + s * 0.3, cy + s * 0.3))
    path.lineTo(QPointF(c.x() + s * 0.3, cy - s * 0.3))
    path.lineTo(QPointF(c.x() - s * 0.5, cy - s * 0.3))
    path.closeSubpath()
    p.drawPath(path)
    top = c.y() - travel - s * 1.2                                        # the stem, to the actuator
    p.drawLine(QPointF(c.x(), cy - s * 0.5), QPointF(c.x(), top))
    p.drawRect(QRectF(c.x() - s * 0.42, top - s * 0.6, s * 0.84, s * 0.6))
    p.restore()


def magnet_sector(v, centre: QPointF, r_in: float, r_out: float, start: float, span: float, tone) -> QPainterPath:
    """A sector magnet's poles: the band r_in..r_out around centre, from start through span degrees
    (Qt's angles: counter-clockwise from three o'clock)."""
    path = QPainterPath()
    outer = QRectF(centre.x() - r_out, centre.y() - r_out, 2 * r_out, 2 * r_out)
    inner = QRectF(centre.x() - r_in, centre.y() - r_in, 2 * r_in, 2 * r_in)
    path.arcMoveTo(outer, start)
    path.arcTo(outer, start, span)
    path.arcTo(inner, start + span, -span)
    path.closeSubpath()
    p = v.p
    p.save()
    fill, pen = _outline(v, tone, 1.8)
    p.setPen(pen)
    p.setBrush(fill)
    p.drawPath(path)
    p.restore()
    return path


def source_body(v, rect: QRectF, tone):
    """The ion source: a body with its extraction cones below it (the beam leaves downwards)."""
    p = v.p
    p.save()
    fill, pen = _outline(v, tone, 2.0)
    p.setPen(pen)
    p.setBrush(fill)
    p.drawRoundedRect(rect, 10, 10)
    cx, y = rect.center().x(), rect.bottom()
    for sx in (-1, 1):                  # the biased and the earthed cones, either side of the gap
        p.drawPolygon(QPolygonF([QPointF(cx + sx * 12, y + 6), QPointF(cx + sx * 46, y + 6),
                                 QPointF(cx + sx * 46, y + 12), QPointF(cx + sx * 16, y + 12)]))
        p.drawPolygon(QPolygonF([QPointF(cx + sx * 15, y + 26), QPointF(cx + sx * 46, y + 22),
                                 QPointF(cx + sx * 46, y + 30), QPointF(cx + sx * 18, y + 32)]))
    p.restore()


def beam(v, path: QPainterPath, width: float, colour, style=Qt.PenStyle.SolidLine):
    """The beam's path: a band width units across."""
    p = v.p
    p.save()
    pen = v.pen(colour, 1.0, style, Qt.PenCapStyle.FlatCap)
    pen.setCosmetic(False)
    pen.setWidthF(width)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(path)
    p.restore()


# --- Switches and wiring (the safety chain) ------------------------------------------------------------
def contact(v, c: QPointF, closed, tone, vertical: bool = False, actuator: str = "", half: float = 18):
    """A switch: two terminals and a blade between them, closed (made) or open, drawn along a wire
    that runs across (or up and down: vertical). actuator marks what works it: 'temperature',
    'flow', 'pressure', 'door', 'eso', 'key', 'button', 'remote' (a dashed stem: a fibre or a relay)."""
    p = v.p
    p.save()
    fill, pen = _outline(v, tone, 2.0)
    _f, line_c, _ink = v.tone(tone)
    p.setPen(pen)
    a, b = _pt(c, vertical, -half, 0), _pt(c, vertical, half, 0)
    lift = 0 if closed else -half * 0.75                 # the blade's free end, raised when open
    tip = _pt(c, vertical, half * 0.9, lift)
    p.setBrush(QColor(line_c))
    r = v.units(3.2)
    p.drawEllipse(a, r, r)
    p.drawEllipse(b, r, r)
    p.drawLine(a, tip)
    p.setBrush(Qt.BrushStyle.NoBrush)
    mid = _pt(c, vertical, 0, lift / 2 - 2)
    top = _pt(c, vertical, 0, -half * 1.15)
    thin = v.pen(line_c, 1.4, line_style(tone))
    if actuator == "remote":
        p.setPen(v.pen(line_c, 1.2, Qt.PenStyle.DashLine))
        p.drawLine(mid, top)
    elif actuator in ("temperature", "flow", "pressure", "eso", "button", "key"):
        p.setPen(thin)
        p.drawLine(mid, top)
        s = half * 0.5
        if actuator == "temperature":            # a bimetal step
            path = QPainterPath(_pt(c, vertical, -s, -half * 1.15))
            for da, db in ((-s, -half * 1.45), (0, -half * 1.45), (0, -half * 1.15), (s, -half * 1.15),
                           (s, -half * 1.45)):
                path.lineTo(_pt(c, vertical, da, db))
            p.drawPath(path)
        elif actuator == "flow":                 # a flag
            p.drawPolygon(QPolygonF([top, _pt(c, vertical, 0, -half * 1.75), _pt(c, vertical, s * 1.2, -half * 1.75)]))
        elif actuator == "pressure":             # a diaphragm
            rr = s * 1.1
            p.drawArc(QRectF(top.x() - rr, top.y() - rr, 2 * rr, 2 * rr), 0, 180 * 16)
            p.drawLine(QPointF(top.x() - rr, top.y()), QPointF(top.x() + rr, top.y()))
        elif actuator == "eso":                  # a mushroom head
            path = QPainterPath(_pt(c, vertical, -s * 1.3, -half * 1.15))
            path.cubicTo(_pt(c, vertical, -s * 1.3, -half * 1.75), _pt(c, vertical, s * 1.3, -half * 1.75),
                         _pt(c, vertical, s * 1.3, -half * 1.15))
            path.closeSubpath()
            p.setBrush(fill)
            p.drawPath(path)
        elif actuator == "button":               # a push button
            p.drawLine(_pt(c, vertical, -s, -half * 1.15), _pt(c, vertical, s, -half * 1.15))
        elif actuator == "key":                  # a key's bow
            rr = s * 0.55
            k = _pt(c, vertical, -s * 0.9, -half * 1.15)
            p.drawEllipse(k, rr, rr)
            p.drawLine(_pt(c, vertical, -s * 0.9 + rr, -half * 1.15), top)
    elif actuator == "door":                     # a door switch's roller, beside the wire
        p.setPen(thin)
        s = half * 0.45
        o = _pt(c, vertical, 0, -half * 0.55)
        p.drawPolygon(QPolygonF([_pt(c, vertical, -s, -half * 0.55 - s), _pt(c, vertical, s, -half * 0.55 - s), o]))
    p.restore()


def wire(v, points, state: str = "dead", px: float = 2.2):
    """A wire through points: 'live' (made: green), 'hv' (switching the high voltage: amber), 'dead'
    (grey), 'unknown' (dashed), 'hidden' (dotted)."""
    colour = {"live": v.tone("ok")[1], "hv": v.c("warn_fill")}.get(state, v.c("border_strong"))
    style = {"unknown": Qt.PenStyle.DashLine, "hidden": Qt.PenStyle.DotLine}.get(state, Qt.PenStyle.SolidLine)
    if state in ("unknown", "hidden"):
        colour = v.c("text_faint")
    line(v, path_points(points), px if state in ("live", "hv") else px * 0.85, colour, style)


def path_points(points) -> QPainterPath:
    path = QPainterPath(QPointF(*points[0]))
    for pt in points[1:]:
        path.lineTo(QPointF(*pt))
    return path


def coil(v, rect: QRectF, tone):
    """A relay or contactor coil: a box with its diagonal."""
    p = v.p
    p.save()
    fill, pen = _outline(v, tone, 1.8)
    p.setPen(pen)
    p.setBrush(fill)
    p.drawRect(rect)
    p.drawLine(rect.bottomLeft(), rect.topRight())
    p.restore()


def rack(v, rect: QRectF, dotted: bool = False):
    """A cabinet or enclosure: a plain frame."""
    p = v.p
    p.save()
    p.setPen(v.pen(v.c("border_strong"), 1.6, Qt.PenStyle.DotLine if dotted else Qt.PenStyle.SolidLine))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(rect, 6, 6)
    p.restore()
