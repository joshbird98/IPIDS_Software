"""How the recipe builder draws a step's kind: an icon for each block, and a node for the list of
steps - the recipe drawn as a flow, a node per step on a line that runs from the first step to
the last, the steps of a "Do steps at the same time" step branching off it.

In the HMI's way (src/gui/theme.py: grey is normal, colour is news), most steps are neutral and
told apart by their icon and their node's shape; colour marks the two kinds that call for
attention when reading a recipe: where it hands over to the operator (blue: interaction), and the
procedures that run for minutes on their own (violet: a sequence). Red and amber stay for the
problems the checks find.

    category(block_key)       command, wait, operator, procedure, structure (or unknown)
    block_icon(key, size)     the block's icon in its category's colour (the palette, the form)
    paint_node(painter, ...)  a step's node in the list
"""
import os
import tempfile
import zlib

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPen

from src.gui import theme
from src.gui.control.dialogs.recipe_builder.blocks import BY_KEY

CATEGORIES = {"Set and switch": "command", "Wait": "wait", "The operator": "operator", "Procedures": "procedure",
              "Structure": "structure"}

# 16 x 16 line icons, drawn as the theme's own (src/gui/theme.py): {c} is the colour
_ICONS = {
    # a command: a value sent to the machine
    "set": '<path d="M2 8 H10.5 M7.5 4.8 L10.7 8 L7.5 11.2" fill="none" stroke="{c}" stroke-width="1.8" '
           'stroke-linecap="round" stroke-linejoin="round"/><path d="M13.5 3 V13" stroke="{c}" stroke-width="1.8" '
           'stroke-linecap="round"/>',
    # ... and its reading checked
    "set_check": '<path d="M1.8 6 H7.2 M5.2 3.6 L7.5 6 L5.2 8.4" fill="none" stroke="{c}" stroke-width="1.6" '
                 'stroke-linecap="round" stroke-linejoin="round"/><path d="M6.8 11.2 L9.3 13.6 L14.2 7.6" '
                 'fill="none" stroke="{c}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
    # a supply switched on and set
    "supply": '<path d="M8 2.2 V7.6" stroke="{c}" stroke-width="1.8" stroke-linecap="round"/>'
              '<path d="M4.6 4.4 A5.2 5.2 0 1 0 11.4 4.4" fill="none" stroke="{c}" stroke-width="1.8" '
              'stroke-linecap="round"/>',
    # a time
    "wait_time": '<circle cx="8" cy="9" r="5.1" fill="none" stroke="{c}" stroke-width="1.7"/>'
                 '<path d="M8 6.2 V9 L10 10.6 M6.4 1.9 H9.6" fill="none" stroke="{c}" stroke-width="1.7" '
                 'stroke-linecap="round" stroke-linejoin="round"/>',
    # a reading: a gauge's needle
    "wait_for": '<path d="M2.6 11.6 A5.6 5.6 0 1 1 13.4 11.6" fill="none" stroke="{c}" stroke-width="1.7" '
                'stroke-linecap="round"/><path d="M8 10.8 L11 6.4" stroke="{c}" stroke-width="1.8" '
                'stroke-linecap="round"/><circle cx="8" cy="10.8" r="1.3" fill="{c}"/>',
    # the operator
    "ask": '<circle cx="8" cy="5" r="2.6" fill="none" stroke="{c}" stroke-width="1.7"/>'
           '<path d="M3 13.8 C3.4 10.6 5.4 9.4 8 9.4 C10.6 9.4 12.6 10.6 13 13.8" fill="none" stroke="{c}" '
           'stroke-width="1.7" stroke-linecap="round"/>',
    # the safety relay: a shield
    "relay": '<path d="M8 1.8 L13.2 3.9 V7.9 C13.2 11 11 13.3 8 14.3 C5 13.3 2.8 11 2.8 7.9 V3.9 Z" fill="none" '
             'stroke="{c}" stroke-width="1.6" stroke-linejoin="round"/><path d="M5.6 8.2 L7.4 10 L10.6 6.4" '
             'fill="none" stroke="{c}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>',
    # a ramp up in steps
    "condition": '<path d="M2 13.5 H5 V10.5 H8 V7.5 H11 V4.5 H14" fill="none" stroke="{c}" stroke-width="1.7" '
                 'stroke-linecap="round" stroke-linejoin="round"/>',
    # a mass spectrum, the biggest peak marked
    "scan": '<path d="M1.6 13.6 H4 L5.4 10 L6.8 13.6 H7.6 L9.4 3.6 L11.2 13.6 H14.4" fill="none" stroke="{c}" '
            'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>'
            '<circle cx="9.4" cy="3.4" r="1.4" fill="{c}"/>',
    # another recipe, run from this one
    "sub": '<rect x="5.6" y="2.4" width="8.6" height="11.2" rx="1.6" fill="none" stroke="{c}" stroke-width="1.6"/>'
           '<path d="M8 6 H11.8 M8 8.6 H11.8 M8 11.2 H10.4" stroke="{c}" stroke-width="1.4" stroke-linecap="round"/>'
           '<path d="M1.4 8 H5.2 M3.4 6.2 L5.2 8 L3.4 9.8" fill="none" stroke="{c}" stroke-width="1.5" '
           'stroke-linecap="round" stroke-linejoin="round"/>',
    # steps at the same time: one line into two
    "parallel": '<path d="M1.8 8 H5.6 L9 4.2 H14.2 M5.6 8 L9 11.8 H14.2" fill="none" stroke="{c}" stroke-width="1.7" '
                'stroke-linecap="round" stroke-linejoin="round"/>',
    # a step type the builder doesn't know
    "unknown": '<path d="M5.8 6 C5.8 3.4 10.2 3.4 10.2 6 C10.2 7.8 8 8 8 9.8" fill="none" stroke="{c}" '
               'stroke-width="1.7" stroke-linecap="round"/><circle cx="8" cy="12.6" r="1.1" fill="{c}"/>',
}

_cache = {}


def category(key) -> str:
    block = BY_KEY.get(key) if key else None
    return CATEGORIES.get(block.group, "unknown") if block is not None else "unknown"


def colours(cat: str, mode: str = None) -> dict:
    """{fill, ring, ink, list}: a node's background, its outline and its icon's colour (the list's
    background: what a hollow node shows)."""
    t = theme.tokens(mode)
    if cat == "operator":
        return {"fill": t["info_bg"], "ring": t["info"], "ink": t["info"]}
    if cat == "procedure":
        return {"fill": t["seq_bg"], "ring": t["seq"], "ink": t["seq"]}
    if cat == "command":
        return {"fill": t["raised"], "ring": t["border_strong"], "ink": t["text"]}
    if cat == "wait":
        return {"fill": t["input"], "ring": t["text_faint"], "ink": t["text_muted"]}
    if cat == "structure":
        return {"fill": t["card_alt"], "ring": t["border_strong"], "ink": t["text_muted"]}
    return {"fill": t["input"], "ring": t["text_faint"], "ink": t["text_faint"]}


def icon(name: str, colour: str) -> QIcon:
    """One of the icons above as a QIcon in a colour ('#RRGGBB'), made once."""
    found = _cache.get((name, colour))
    if found is None:
        found = _cache[(name, colour)] = QIcon(_svg_file(name, colour))
    return found


def block_icon(key, mode: str = None) -> QIcon:
    """The block's icon in its category's colour (a step type the builder doesn't know: '?')."""
    name = key if key in _ICONS else "unknown"
    return icon(name, colours(category(key), mode)["ink"])


def _svg_file(name: str, colour: str) -> str:
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
           + _ICONS[name].format(c=colour) + "</svg>")
    folder = os.path.join(tempfile.gettempdir(), "sindie_theme_icons")
    path = os.path.join(folder, f"step_{name}_{zlib.crc32(svg.encode()):08x}.svg")
    if not os.path.exists(path):
        try:
            os.makedirs(folder, exist_ok=True)
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(svg)
            os.replace(tmp, path)
        except OSError:             # an icon missing is only cosmetic
            pass
    return path


def paint_node(painter: QPainter, centre: QPointF, size: float, key, faint: bool = False):
    """A step's node: a circle (a square with rounded corners for the structure blocks) in its
    category's colours, its block's icon inside. faint: a step that won't be in the recipe (the
    draft removes it, or adds it but it is left out)."""
    cat = category(key)
    c = colours(cat)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    if faint:
        painter.setOpacity(0.45)
    rect = QRectF(centre.x() - size / 2, centre.y() - size / 2, size, size)
    pen = QPen(QColor(c["ring"]), 1.4)
    if cat in ("wait", "unknown"):
        pen.setStyle(Qt.PenStyle.DashLine if cat == "unknown" else Qt.PenStyle.SolidLine)
    painter.setPen(pen)
    painter.setBrush(QColor(c["fill"]))
    if cat == "structure":
        painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 5, 5)
    else:
        painter.drawEllipse(rect.adjusted(1, 1, -1, -1))
    inner = size * 0.58
    icon_rect = QRectF(centre.x() - inner / 2, centre.y() - inner / 2, inner, inner).toAlignedRect()
    name = key if key in _ICONS else "unknown"
    icon(name, c["ink"]).paint(painter, icon_rect)
    painter.restore()
