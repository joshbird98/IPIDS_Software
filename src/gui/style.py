"""Setting how a widget looks without giving it colours of its own (see src/gui/theme.py).

    set_tone(w, "fault")      what state it shows: ok, on, warn, fault, off, info, seq, armed,
                              unknown, primary, danger - or None for plain
    set_role(w, "readout")    what kind of thing it is: readout, readout-sm, readout-lg, caption,
                              hint, title, heading, error, warning, success, card, callout, rule
    set_ink(w, "fault")       text colour only (a readout that has gone stale or bad): ok, warn,
                              fault, info, muted, faint - or None for normal
    set_prop(w, name, value)  any other styling property (e.g. tolerance="out")

The application stylesheet draws each tone and role in the current theme, so a theme switch
restyles them all. Setting the value it already has does nothing: the panels refresh 10 times a
second, and re-styling a widget (what setStyleSheet did on every refresh) is costly.
"""
TONES = ("ok", "on", "warn", "fault", "off", "info", "seq", "armed", "unknown", "primary", "danger")


def set_prop(widget, name: str, value) -> bool:
    """Set a styling property and re-style the widget, only if it changed. Returns True if it did."""
    value = "" if value is None else str(value)
    current = widget.property(name)
    if (current or "") == value:
        return False
    widget.setProperty(name, value)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()
    return True


def set_tone(widget, tone) -> bool:
    return set_prop(widget, "tone", tone)


def set_ink(widget, ink) -> bool:
    return set_prop(widget, "ink", ink)


def tone(widget) -> str:
    return widget.property("tone") or ""


def set_role(widget, role) -> bool:
    return set_prop(widget, "role", role)


def reserve_width(widget, sample: str, extra: int = 10):
    """Minimum width for the longest text a label will show, measured in its own (styled) font,
    so a value changing length doesn't make the panel's layout jump."""
    widget.ensurePolished()
    widget.setMinimumWidth(widget.fontMetrics().horizontalAdvance(sample) + extra)


def swatch(colour, size: int = 14):
    """A small rounded square of a colour (a QIcon), outlined so white and yellow show on any
    background: marker colours, a channel's line colour."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QColor(0, 0, 0, 90))
    p.setBrush(QColor(colour))
    p.drawRoundedRect(1, 1, size - 2, size - 2, 3, 3)
    p.end()
    return QIcon(pm)


def chip(text: str = "", tone: str = "off", role: str = None):
    """A status chip: a centred label with a tone."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QLabel
    lbl = QLabel(text)
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    set_tone(lbl, tone)
    if role:
        set_role(lbl, role)
    return lbl
