"""The SINDIE icon: the beam bending through the analyser magnet onto its target, drawn rather than
loaded (so it is sharp at every size). The tray's copy carries a dot for the services: green, all
online; amber, some starting or restarting; red, the service manager isn't running.

src/utils/make_launcher_icon.py writes it as src/launcher/sindie.ico for the Windows shortcuts.
"""
from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

BACKGROUND = "#102033"
MAGNET = "#24466B"
BEAM = "#74C0FF"
TARGET = "#F4F8FC"
STATUS = {"ok": "#2E9E5E", "warn": "#E9A23B", "fault": "#D63D41"}
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def paint(p: QPainter, size: float, status: str = None):
    """The icon in a size x size square at the painter's origin (drawn on a 64-unit grid)."""
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size / 64.0, size / 64.0)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(BACKGROUND))
    p.drawRoundedRect(QRectF(2, 2, 60, 60), 13, 13)
    # the magnet: a quarter ring round the bend
    centre, inner, outer = QPointF(36, 32), 8.0, 19.0
    ring = QPainterPath()
    ring.arcMoveTo(QRectF(centre.x() - outer, centre.y() - outer, 2 * outer, 2 * outer), 90)
    ring.arcTo(QRectF(centre.x() - outer, centre.y() - outer, 2 * outer, 2 * outer), 90, 90)
    ring.arcTo(QRectF(centre.x() - inner, centre.y() - inner, 2 * inner, 2 * inner), 180, -90)
    ring.closeSubpath()
    p.setBrush(QColor(MAGNET))
    p.drawPath(ring)
    # the beam: up from the source, round the bend, on to the target
    r = (inner + outer) / 2.0
    beam = QPainterPath()
    beam.moveTo(centre.x() - r, 58)
    beam.lineTo(centre.x() - r, centre.y())
    beam.arcTo(QRectF(centre.x() - r, centre.y() - r, 2 * r, 2 * r), 180, -90)
    beam.lineTo(50, centre.y() - r)
    pen = QPen(QColor(BEAM), 5.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(beam)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(TARGET))
    p.drawEllipse(QPointF(51, centre.y() - r), 5.0, 5.0)
    if status in STATUS:
        p.setBrush(QColor(BACKGROUND))
        p.drawEllipse(QPointF(49, 49), 14.0, 14.0)
        p.setBrush(QColor(STATUS[status]))
        p.drawEllipse(QPointF(49, 49), 11.0, 11.0)
    p.restore()


def pixmap(size: int, status: str = None) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    paint(p, size, status)
    p.end()
    return pm


def app_icon(status: str = None) -> QIcon:
    icon = QIcon()
    for size in SIZES:
        icon.addPixmap(pixmap(size, status))
    return icon
