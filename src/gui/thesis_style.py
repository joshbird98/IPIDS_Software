"""
The thesis figures' look, for the plots in an F12 capture (figure_capture.py: Style > Thesis figure).

The thesis' own figures share one matplotlib style (in the thesis repository, analysis/common/
thesis_style.py and analysis/source/style.py): Computer Modern, as the text; 9 pt type, 9.5 pt axis
titles, 8.5 pt numbers and legend; dark grey axes 0.8 pt wide with the ticks out and black text; a
light grey grid 0.6 pt wide behind the data; lines 1.5 pt; the colours #2a78d6, #eb6834, #1baf7a -
the first of the light theme's own. Figures are drawn 16 cm (6.3 in) wide and included at the text
width, 15 cm. ThesisStyle gives a window's pyqtgraph plots that look for a capture, sized for the
page (the capture scales the window's pixels to the page, so a size on the page is a size in them),
and puts everything back afterwards. What makes the figure the software's - its lanes, the legend's
box, the clock-time axis, the event markers - stays.

Fonts are given in pixels: a size in points is turned into pixels at the paint device's resolution,
which for Qt's SVG writer is not the screen's. The type is Latin Modern Roman (Computer Modern in
Unicode), from the computer if it has it, else from src/gui/assets/fonts. It has no superscript
figures, so an axis draws a number's power of ten itself (_draw_powers).
"""
import math
import os
import re

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QFont, QFontDatabase, QFontMetricsF, QPen, QTransform
from PyQt6.QtWidgets import QGraphicsItem

from src.gui import theme

STYLES = {"thesis": "Thesis figure (Computer Modern)"}
FONT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts", "lmroman10-regular.otf")
FALLBACK_FONTS = ("CMU Serif", "Times New Roman")
SVG_FONTS = "'Latin Modern Roman', 'LM Roman 10', 'CMU Serif', cmr10, serif"   # (as the thesis' SVG figures)
TEXT_PT, TITLE_PT, SMALL_PT = 9.0, 9.5, 8.5   # other text; axis titles; the numbers on the axes, the legend
AXIS_PT, TICK_LEN_PT, PAD_PT, GRID_PT, FRAME_PT = 0.8, 3.5, 3.5, 0.6, 0.5   # (matplotlib's ticks: 3.5 pt out)
LINE_PT = 1.5                         # the plot lines, for a line 1 px wide on the screen
WIDTH_CM = 16.0                       # drawn this wide, included at the text width (15 cm), as the others
INK, INK2, MUTED, GRID, PAPER = "#0B0B0B", "#52514E", "#898781", "#E1E0D9", "#FFFFFF"
_family = []


def font_family():
    """Latin Modern Roman: the computer's, or the one in src/gui/assets/fonts; else CMU Serif or
    Times New Roman."""
    if not _family:
        if "Latin Modern Roman" in QFontDatabase.families():
            _family.append("Latin Modern Roman")
        else:
            got = QFontDatabase.addApplicationFont(FONT_FILE) if os.path.exists(FONT_FILE) else -1
            names = QFontDatabase.applicationFontFamilies(got) if got >= 0 else []
            installed = set(QFontDatabase.families())
            _family.append(names[0] if names else next((f for f in FALLBACK_FONTS if f in installed),
                                                       FALLBACK_FONTS[-1]))
    return _family[0]


def plot_views(widget):
    """The pyqtgraph views painted when widget is: those in it, or the one it is part of (a view's
    viewport, which is what is under the mouse over a plot)."""
    import pyqtgraph as pg
    views = widget.findChildren(pg.GraphicsView)
    inside = widget
    while inside is not None and not isinstance(inside, pg.GraphicsView):
        inside = inside.parentWidget()
    if inside is not None:
        views.insert(0, inside)
    return views


def plot_items(widget):
    """Every item in the scenes of plot_views(widget), each scene once."""
    scenes, items = [], []
    for view in plot_views(widget):
        scene = view.scene()
        if scene is not None and not any(scene is s for s in scenes):
            scenes.append(scene)
            items += scene.items()
    return items


def axis_length(axis):
    """The axis' length in the view's pixels, as pyqtgraph measures it to choose its ticks."""
    b = axis.mapRectFromParent(axis.geometry())
    ends = {"left": (b.topRight() + QPointF(-1, -1), b.bottomRight() + QPointF(-1, 1)),
            "right": (b.topLeft() + QPointF(1, -1), b.bottomLeft() + QPointF(1, 1)),
            "top": (b.bottomLeft() + QPointF(-1, -1), b.bottomRight() + QPointF(1, -1)),
            "bottom": (b.topLeft() + QPointF(-1, 1), b.topRight() + QPointF(1, 1))}[axis.orientation]
    a, c = axis.mapToDevice(ends[0]), axis.mapToDevice(ends[1])
    return 0.0 if a is None or c is None else math.hypot(c.x() - a.x(), c.y() - a.y())


def major_ticks(axis):
    """Where the axis puts its major ticks (in its view's coordinates), inside its range."""
    lo, hi = sorted(axis.range)
    if axis._tickLevels is not None:
        values = [v for v, _s in axis._tickLevels[0]] if axis._tickLevels else []
    else:
        length = axis_length(axis)
        levels = axis.tickValues(axis.range[0], axis.range[1], length) if length > 0 else []
        values = list(levels[0][1]) if levels else []
    return [v for v in values if lo <= v <= hi]


_E = re.compile(r"^([+-]?\d+(?:\.\d+)?)e([+-]?)0*(\d+)$")


def tidy_number(text):
    """A tick label as the thesis writes numbers: a minus sign, not a hyphen, and 1.6e-07 as
    1.6×10^−7 (the axis draws what follows ^ as a superscript: _draw_powers), 1e-07 as 10^−7.
    Anything else (times, dates) as it is."""
    m = _E.match(text.strip())
    if m:
        mant, sign, exp = m.groups()
        power = "10^" + ("−" if sign == "-" else "") + exp
        mant = mant.replace("-", "−")
        if mant.lstrip("−") in ("1", "1.0"):
            return ("−" if mant.startswith("−") else "") + power
        return f"{mant}×{power}"
    return re.sub(r"^-(?=[\d.])", "−", text)


def _draw_powers(axis):
    """pyqtgraph's drawing of the axis, but with a tick label's power of ten raised and smaller."""
    def draw(p, axisSpec, tickSpecs, textSpecs):
        plain = [spec for spec in textSpecs if "^" not in spec[2]]
        type(axis).drawPicture(axis, p, axisSpec, tickSpecs, plain)
        powers = [spec for spec in textSpecs if "^" in spec[2]]
        if not powers:
            return
        font = QFont(axis.style["tickFont"] or p.font())
        small = QFont(font)
        if font.pixelSize() > 0:
            small.setPixelSize(max(1, round(font.pixelSize() * 0.7)))
        else:
            small.setPointSizeF(font.pointSizeF() * 0.7)
        fm, fs = QFontMetricsF(font), QFontMetricsF(small)
        p.setPen(axis.textPen())
        for rect, flags, text in powers:
            base, power = text.split("^", 1)
            f = int(getattr(flags, "value", flags))
            wide = fm.horizontalAdvance(base) + fs.horizontalAdvance(power)
            if f & Qt.AlignmentFlag.AlignRight.value:
                x = rect.right() - wide
            elif f & Qt.AlignmentFlag.AlignHCenter.value:
                x = rect.center().x() - wide / 2
            else:
                x = rect.left()
            if f & Qt.AlignmentFlag.AlignTop.value:
                y = rect.top() + fm.ascent()
            elif f & Qt.AlignmentFlag.AlignBottom.value:
                y = rect.bottom() - fm.descent()
            else:
                y = rect.center().y() + (fm.ascent() - fm.descent()) / 2
            p.setFont(font)
            p.drawText(QPointF(x, y), base)
            p.setFont(small)
            p.drawText(QPointF(x + fm.horizontalAdvance(base), y - 0.42 * fm.ascent()), power)
    return draw


class _GridLines(QGraphicsItem):
    """An axis' grid: lines across its plot at its major ticks, behind the data (pyqtgraph draws its
    grid as the axis' own ticks drawn long, over the data)."""

    def __init__(self, axis, host, pen):
        super().__init__(host)
        self.axis, self.host, self.pen = axis, host, pen
        self.setZValue(-1e5)                  # above the ViewBox's background (-1e6), below its data (0)

    def boundingRect(self):
        return QRectF(-1e7, -1e7, 2e7, 2e7)   # (the ViewBox clips it to the plot)

    def paint(self, p, opt, widget=None):
        view = self.axis.linkedView()
        if view is None or not self.axis.isVisible():
            return
        r = self.host.rect()
        along_x = self.axis.orientation in ("top", "bottom")
        to_px = p.worldTransform()
        p.save()
        p.setWorldTransform(QTransform())     # (in the window's pixels: see figure_capture.PlotLines)
        p.setRenderHint(p.RenderHint.Antialiasing, True)
        p.setPen(self.pen)
        for v in major_ticks(self.axis):
            at = self.mapFromScene(view.mapViewToScene(QPointF(v, v)))
            a, b = ((QPointF(at.x(), r.top()), QPointF(at.x(), r.bottom())) if along_x
                    else (QPointF(r.left(), at.y()), QPointF(r.right(), at.y())))
            p.drawLine(to_px.map(a), to_px.map(b))
        p.restore()


class ThesisStyle:
    """The plots in a widget in the thesis figures' look, at per_inch of the window's pixels to the
    inch on the page, until remove(). colour_for(curve or marker line) gives the colour to draw it in
    (figure_capture.PlotLines): a channel in the light theme's colour for its place in its lane (the
    thesis' colours; the dark theme's are its light ones made for a dark background), an event marker
    in its own, made to stand out on white."""

    def __init__(self, widget, per_inch, style="thesis"):
        import pyqtgraph as pg
        from pyqtgraph.graphicsItems.LegendItem import LegendItem
        self.per_inch, self.family = per_inch, font_family()
        self._undo = []
        light = theme.tokens("light")["series"]
        self._series = {}
        for mode in ("light", "dark"):
            for i, c in enumerate(theme.tokens(mode)["series"]):
                self._series.setdefault(QColor(c).rgb(), QColor(light[i % len(light)]))
        try:
            items = plot_items(widget)
            self._plots = [it for it in items if isinstance(it, pg.PlotItem)]
            for view in plot_views(widget):
                self._paper(view)
            for item in items:
                if isinstance(item, pg.AxisItem) and item.isVisible():
                    self._axis(item)
                elif isinstance(item, pg.ViewBox):
                    self._viewbox(item)
                elif isinstance(item, pg.TextItem):
                    self._text(item)
                elif isinstance(item, pg.PlotItem):
                    self._title(item)
                elif isinstance(item, LegendItem):
                    self._legend(item)
        except Exception:
            self.remove()
            raise

    def align(self):
        """The plots in line with one another, as the thesis figures' panels are: each side's axes as
        wide as the widest there (a plot with none on a side where another has one keeps the room,
        empty). Run once the axes have measured their text at its new size (they do as they paint)."""
        plots = [p for p in self._plots if p.isVisible()]
        if len(plots) < 2:
            return
        for col, side in ((0, "left"), (2, "right")):
            axes = [p.getAxis(side) if side in p.axes else None for p in plots]
            widest = max((ax.width() for ax in axes if ax is not None and ax.isVisible()), default=0.0)
            for p, ax in zip(plots, axes):
                if ax is not None and ax.isVisible():
                    fixed = ax.fixedWidth
                    ax.setWidth(int(math.ceil(widest)))
                    self._undo.append(lambda ax=ax, fixed=fixed: ax.setWidth(fixed))
                elif widest > 0:
                    low = p.layout.columnMinimumWidth(col)
                    p.layout.setColumnMinimumWidth(col, widest)
                    self._undo.append(lambda p=p, col=col, low=low: p.layout.setColumnMinimumWidth(col, low))

    def remove(self):
        while self._undo:
            try:
                self._undo.pop()()
            except RuntimeError:              # (an item that has gone meanwhile)
                pass

    # ----------------------------------------------------------------------- sizes
    def px(self, pt):
        """pt on the page, in the window's pixels."""
        return pt / 72.0 * self.per_inch

    def font(self, pt, base=None):
        f = QFont(base) if base is not None else QFont()
        f.setFamily(self.family)
        f.setStyleHint(QFont.StyleHint.Serif)
        f.setPixelSize(max(1, round(self.px(pt))))
        return f

    def pen(self, colour, pt):
        p = QPen(QColor(colour))
        p.setCosmetic(False)                  # (its width in the window's pixels, scaled with them)
        p.setWidthF(self.px(pt))
        p.setCapStyle(Qt.PenCapStyle.FlatCap)
        return p

    # ----------------------------------------------------------------------- colours
    def colour_for(self, item):
        import pyqtgraph as pg
        if isinstance(item, pg.InfiniteLine):
            pen = getattr(item, "currentPen", None)
            if not isinstance(pen, QPen):
                return None
            c = theme.readable(pen.color(), background=PAPER)
            c.setAlpha(pen.color().alpha())
            return c
        if isinstance(item, pg.PlotCurveItem):
            # (its PlotDataItem's pen, where it has one: that is the channel's, which the legend shows -
            # a curve that has had no data yet still has pyqtgraph's default pen of its own)
            owner = item.parentItem()
            pen = owner.opts.get("pen") if isinstance(owner, pg.PlotDataItem) else item.opts.get("pen")
            return self._series.get(pen.color().rgb()) if isinstance(pen, QPen) else None
        return None

    # ----------------------------------------------------------------------- the parts
    def _paper(self, view):
        background, brush = getattr(view, "_background", None), QBrush(view.backgroundBrush())
        view.setBackground(PAPER)

        def undo():
            view._background = background
            view.setBackgroundBrush(brush)
        self._undo.append(undo)

    def _viewbox(self, vb):
        background = vb.state.get("background")
        if background is not None:
            vb.setBackgroundColor(PAPER)
            self._undo.append(lambda: vb.setBackgroundColor(background))

    def _axis(self, ax):
        import pyqtgraph as pg
        pens = ax._pen, ax._textPen, ax._tickPen
        style = {k: (list(v) if isinstance(v, list) else v) for k, v in ax.style.items()}
        grid, label_font, label_style = ax.grid, QFont(ax.label.font()), dict(ax.labelStyle)
        fixed, room = (ax.fixedWidth, ax.fixedHeight), (ax.textWidth, ax.textHeight)
        own_strings, own_draw = ax.__dict__.get("tickStrings"), ax.__dict__.get("drawPicture")

        edge = self.pen(INK2, AXIS_PT)
        ax._pen, ax._tickPen, ax._textPen = edge, QPen(edge), QPen(QColor(INK))
        out = int(round(self.px(TICK_LEN_PT)))
        ax.style["tickLength"] = out if ax.style.get("showValues", True) else 0   # out; none where no numbers
        ax.style["tickTextOffset"] = [int(round(self.px(PAD_PT)))] * 2
        ax.style["tickFont"] = self.font(SMALL_PT)
        ax.style["tickAlpha"] = 1.0           # (pyqtgraph fades the finer ticks)
        ax.fixedWidth = ax.fixedHeight = None  # (room for the text at its new size)
        ax.labelStyle = {"color": INK, "font-family": self.family, "font-size": f"{round(self.px(TITLE_PT))}px"}
        ax.label.setFont(self.font(TITLE_PT))

        def tick_strings(values, scale, spacing):
            strings = (own_strings(values, scale, spacing) if own_strings
                       else type(ax).tickStrings(ax, values, scale, spacing))
            return [tidy_number(s) for s in strings]
        ax.tickStrings = tick_strings
        ax.drawPicture = _draw_powers(ax)
        lines = None
        if grid is not False:
            parent = ax.parentItem()
            host = parent.vb if isinstance(parent, pg.PlotItem) else ax.linkedView()
            if host is not None:
                lines = _GridLines(ax, host, self.pen(GRID, GRID_PT))
        ax.setGrid(False)
        ax._updateLabel()

        def undo():
            if lines is not None and lines.scene() is not None:
                lines.scene().removeItem(lines)
            ax._pen, ax._textPen, ax._tickPen = pens
            ax.style = style
            ax.fixedWidth, ax.fixedHeight = fixed
            ax.textWidth, ax.textHeight = room
            ax.labelStyle = label_style
            ax.label.setFont(label_font)
            for name, own in (("tickStrings", own_strings), ("drawPicture", own_draw)):
                if own is None:
                    ax.__dict__.pop(name, None)
                else:
                    setattr(ax, name, own)
            ax.setGrid(grid)
            ax._updateLabel()
        self._undo.append(undo)

    def _legend(self, legend):
        pen, brush = legend.opts["pen"], legend.opts["brush"]
        size, colour = legend.opts.get("labelTextSize"), legend.opts.get("labelTextColor")
        entries = [(sample, label, QFont(label.item.font()), dict(label.opts), sample.minimumSize(), sample.maximumSize())
                   for sample, label in legend.items]
        before = max((label.item.boundingRect().height() for _s, label in legend.items), default=0.0)
        legend.setPen(self.pen(MUTED, FRAME_PT))   # (the software's box, kept; the thesis' have none)
        legend.setBrush(QBrush(QColor(PAPER)))
        text_px = f"{round(self.px(SMALL_PT))}px"
        legend.opts["labelTextSize"], legend.opts["labelTextColor"] = text_px, QColor(INK)
        for sample, label, *_rest in entries:
            label.item.setFont(self.font(SMALL_PT))
            label.setText(label.text, color=INK, size=text_px)
        after = max((label.item.boundingRect().height() for _s, label in legend.items), default=0.0)
        k = after / before if before > 0 and after > 0 else 1.0
        for sample, *_rest in entries:        # the line samples grow with the text (as long, for its size)
            side = max(1, round(20 * k))
            sample.setFixedWidth(side)
            sample.setFixedHeight(side)
        legend.layout.activate()
        legend.updateSize()

        def undo():
            legend.setPen(pen)
            legend.setBrush(brush)
            legend.opts["labelTextSize"], legend.opts["labelTextColor"] = size, colour
            for sample, label, font, opts, low, high in entries:
                label.item.setFont(font)
                label.opts = opts
                label.setText(label.text)
                sample.setMinimumSize(low)
                sample.setMaximumSize(high)
            legend.layout.activate()
            legend.updateSize()
        self._undo.append(undo)

    def _text(self, item):
        font, colour, fill = QFont(item.textItem.font()), QColor(item.color), QBrush(item.fill)
        item.setFont(self.font(SMALL_PT, font))
        item.setColor(theme.readable(colour, background=PAPER, minimum=4.5))
        if fill.style() != Qt.BrushStyle.NoBrush:
            item.fill = QBrush(QColor(PAPER))

        def undo():
            item.setFont(font)
            item.setColor(colour)
            item.fill = fill
            item.update()
        self._undo.append(undo)

    def _title(self, plot):
        title = getattr(plot, "titleLabel", None)
        if title is None or not title.isVisible() or not getattr(title, "text", ""):
            return
        font, opts = QFont(title.item.font()), dict(title.opts)
        title.item.setFont(self.font(TITLE_PT))
        title.setText(title.text, color=INK, size=f"{round(self.px(TITLE_PT))}px")

        def undo():
            title.item.setFont(font)
            title.opts = opts
            title.setText(title.text)
        self._undo.append(undo)
