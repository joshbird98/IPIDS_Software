"""
Publication-quality captures of the GUI itself (viewer and HMI).

Why a normal screenshot looks wrong in a thesis
-----------------------------------------------
It is mostly SCALE, not resolution. A 1600 px window is ~42 cm wide at 96 dpi;
shrunk to a 16 cm A4 text width, its 9 pt UI font prints at ~3.4 pt - far
below the ~8-10 pt of captions and axis labels around it. Upscaling a
screenshot can't fix that, and a raster grab also blurs/aliases when the PDF
is zoomed or printed.

What this does instead
----------------------
* Re-PAINTS the widget (QWidget.render) straight into a QPdfWriter / SVG
  generator, so buttons, text, borders and pyqtgraph plots come out as VECTORS
  (sharp at any zoom, text selectable). PNG is re-painted at the target DPI
  (not upscaled), so it is crisp too.
* Lets you capture ONE PANEL (the widget under the mouse, or any of its
  parents, or a dock panel) instead of the whole window - the main way to get
  legible text on the page - and optionally re-lays-out the window at a
  chosen size for the capture.
* Shows the printed size the UI text will have, so you can pick a framing
  that keeps it >= ~6-7 pt.
* In the thesis style (thesis_style.py), writes the figure's words as a figure
  has them - plain legend names, the units on the axis titles, "Time of day" -
  in a Figure text box where they can be changed; what is changed there is
  remembered (data/figure_text.json) for the next capture of those channels.
* Draws the plot lines at a chosen width on the page (Plot lines, in pt for a
  line 1 px wide on the screen), shown on the window while the dialog is open:
  pyqtgraph's pens are cosmetic, their width in the paint device's own pixels -
  1/1200 inch in the PDF - so as they are, the traces print a fraction of a hair
  wide. The lines are drawn in the window's pixels rather than the plot's own
  coordinates, which Qt's SVG writer rounds to 6 significant figures (whole
  seconds, for a trace a day from where the viewer's time axis starts).
* No window title bar/frame, no mouse cursor.

Use: press F12 with the mouse over the part you want (works in any viewer or
HMI window), choose what to capture and the width on the page, Capture.
Files go to data/exports/figures/<name>_<timestamp>.pdf/.svg/.png.
"""
import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime

import numpy as np
from PyQt6.QtCore import QMarginsF, QPoint, QPointF, QRect, QSize, QSizeF, Qt, QTimer, QUrl
from PyQt6.QtGui import (QColor, QCursor, QDesktopServices, QImage, QKeySequence, QPageLayout, QPageSize,
                         QPaintEngine, QPainter, QPainterPath, QPdfWriter, QPen, QRegion, QShortcut,
                         QTransform)
from PyQt6.QtWidgets import (QAbstractScrollArea, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                             QDockWidget, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QGroupBox,
                             QHBoxLayout, QLabel, QLineEdit, QMainWindow, QPushButton, QRubberBand,
                             QScrollArea, QSpinBox, QTabWidget, QVBoxLayout, QWidget)

from src.core import paths
from src.gui import theme
from src.gui.style import set_role
from src.gui.thesis_style import (SMALL_PT, STYLES, SVG_FONTS, TEXT_WIDTH_CM, TITLE_PT, ThesisStyle, figure_texts,
                                  plot_items, plot_views)
from src.gui.thesis_style import LINE_PT as THESIS_LINE_PT
from src.gui.thesis_style import WIDTH_CM
from src.gui.thesis_style import AXIS_PT as MARKER_PT   # (event markers in a thesis figure: as fine as its axes)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
DEFAULT_DIR = os.path.join(PROJECT_ROOT, "data", "exports", "figures")
TEXTS_FILE = os.path.join(paths.DATA_DIR, "figure_text.json")   # thesis figure texts changed in the dialog
SCREEN_DPI = 96.0           # Qt logical pixels per inch
MIN_SIDE_PX = 120           # ignore tiny widgets when offering what to capture
WINDOW_SIZES = [None, (1024, 640), (1280, 720), (1280, 800), (1440, 900), (1600, 900), (1920, 1080)]
PDF_DPI = 1200
LINE_PT = 0.75              # Plot lines at first: the printed width of a line 1 px wide on the screen
TEXT_BOX_MAX_PX = 260       # Figure text: taller than this, its fields scroll
_last = {"line_pt": LINE_PT, "style": None}   # (the dialog starts where the last capture left it)


# --------------------------------------------------------------------------- rendering
def _render_flags():
    return QWidget.RenderFlag.DrawWindowBackground | QWidget.RenderFlag.DrawChildren


def _paint(widget, painter, region):
    # Call QWidget.render explicitly: QGraphicsView (pyqtgraph plots) overrides render()
    # with a different signature (painter, target QRectF, source QRect, mode).
    QWidget.render(widget, painter, QPoint(0, 0), region, _render_flags())


class PlotLines:
    """The plot lines in a widget - pyqtgraph's curves, and its straight lines across a plot
    (InfiniteLine: the viewer's event markers) - painted by this class until remove():

    * in the coordinates of the widget's own pixels, not the plot's. Qt's SVG writer keeps 6
      significant figures of whatever coordinates it is given, and the viewer's x is seconds from
      when it started, so a trace a day or more from there came out in whole-second steps;
    * with width_px, that many of the widget's pixels wide for each pixel of the line's own width
      (a 2 px trace stays twice a 1 px one), round-cornered, and the legend's line samples to
      match. The capture scales the widget's pixels to the page, so the window shows, while this is
      in place, the width the lines will print at;
    * with colours (a function of the curve or line, as thesis_style.ThesisStyle.colour_for), in
      the colour it gives for it, if any; with marker_px, the straight lines that wide (per pixel of
      their own width) rather than width_px.

    Only how those items paint is replaced, on each item alone: nothing in the plot changes - not
    its pens, data or ranges - so nothing pyqtgraph does to a curve (it re-applies a curve's own pen
    whenever the plot re-ranges, which it may do in the middle of a paint) can undo it.
    live: the window's own plots - repaint them whole while this is in place, so a line wider than
    pyqtgraph allows for leaves no trail when it moves."""

    def __init__(self, widget, width_px=None, live=False, colours=None, marker_px=None):
        import pyqtgraph as pg
        from pyqtgraph.graphicsItems.LegendItem import ItemSample
        self.width_px, self.colours, self.marker_px = width_px or None, colours, marker_px or None
        self.drawn = set()                    # the curves painted (ids)
        self._items, self._modes = [], []
        self.views = plot_views(widget)
        for item in plot_items(widget):
            if isinstance(item, pg.PlotCurveItem):
                item.paint = self._curve_paint(item)
            elif isinstance(item, pg.InfiniteLine):
                item.paint = self._line_paint(item)
            elif isinstance(item, ItemSample) and isinstance(getattr(item, "item", None),
                                                             (pg.PlotDataItem, pg.PlotCurveItem)):
                item.paint = self._sample_paint(item)       # (a line's sample: not a scatter's symbol)
            else:
                continue
            self._items.append(item)
        if live:
            from PyQt6.QtWidgets import QGraphicsView
            for view in self.views:
                self._modes.append((view, view.viewportUpdateMode()))
                view.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
                view.viewport().update()

    def remove(self):
        for item in self._items:
            item.__dict__.pop("paint", None)
        self._items = []
        for view, mode in self._modes:
            try:
                view.setViewportUpdateMode(mode)
                view.viewport().update()
            except RuntimeError:              # (the plot has gone)
                pass
        self._modes = []

    def pen(self, pen, item=None, width_px=None):
        """What a line pyqtgraph would draw with pen (for item) is drawn with (in the widget's pixels).
        width_px: rather than self.width_px."""
        colour = self.colours(item) if self.colours is not None and item is not None else None
        width_px = width_px or self.width_px
        if not width_px and colour is None:
            return pen                        # as it is: a cosmetic pen, its width in the device's pixels
        out = QPen(pen)
        if colour is not None:
            colour = QColor(colour)
            colour.setAlpha(pen.color().alpha())
            out.setColor(colour)
        if width_px:
            out.setCosmetic(False)
            out.setWidthF(max(pen.widthF(), 1.0) * width_px)
            out.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            if pen.style() == Qt.PenStyle.SolidLine:   # (round ends would lengthen a dash)
                out.setCapStyle(Qt.PenCapStyle.RoundCap)
        return out

    def _curve_paint(self, curve):
        def paint(p, opt, widget=None):
            o = curve.opts
            pen, brush = o.get("pen"), o.get("brush")
            fill = o.get("fillLevel") is not None and brush is not None and brush.style() != Qt.BrushStyle.NoBrush
            if fill and o.get("fillOutline"):          # the line is the fill's edge: pyqtgraph's own paint
                return type(curve).paint(curve, p, opt, widget)
            shadow = o.get("shadowPen")
            if fill or (shadow is not None and shadow.style() != Qt.PenStyle.NoPen):
                o["pen"] = None                        # the fill under the line, the shadow behind it:
                try:                                   # pyqtgraph's own paint, without the line
                    type(curve).paint(curve, p, opt, widget)
                finally:
                    o["pen"] = pen
            if pen is None or pen.style() == Qt.PenStyle.NoPen or curve.xData is None or len(curve.xData) == 0:
                return
            to_px = p.worldTransform()
            export = getattr(curve, "_exportOpts", False)
            aa = export.get("antialias", True) if export else o.get("antialias")
            p.save()
            p.setWorldTransform(QTransform())
            if p.hasClipping() and p.paintEngine().type() == QPaintEngine.Type.SVG:
                path = clip_path(curve.getPath(), to_px, p.clipBoundingRect())   # (Qt's SVG writer
            else:                                                                 # leaves out clips)
                path = to_px.map(curve.getPath())
            p.setRenderHint(QPainter.RenderHint.Antialiasing, bool(aa or self.width_px))
            if o.get("compositionMode") is not None:
                p.setCompositionMode(o["compositionMode"])
            p.setPen(self.pen(pen, curve))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
            p.restore()
            self.drawn.add(id(curve))
        return paint

    def _line_paint(self, line):
        def paint(p, *args):
            pen = getattr(line, "currentPen", None)
            if not isinstance(pen, QPen) or getattr(line, "markers", None):   # (arrow heads: pyqtgraph's own)
                return type(line).paint(line, p, *args)
            left, right = line._endPoints
            to_px = p.worldTransform()
            p.save()
            p.setWorldTransform(QTransform())
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            out = self.pen(pen, line, self.marker_px)
            out.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
            p.setPen(out)
            p.drawLine(to_px.map(QPointF(left, 0)), to_px.map(QPointF(right, 0)))
            p.restore()
        return paint

    def _sample_paint(self, sample):
        def paint(p, *args):
            item = sample.item
            o = item.opts
            pen = o.get("pen")
            if not isinstance(pen, QPen) or (not self.width_px and self.colours is None):
                return type(sample).paint(sample, p, *args)
            line = self.pen(pen, getattr(item, "curve", item))
            line.setCapStyle(Qt.PenCapStyle.FlatCap)   # (the sample keeps its length)
            if (pen.style() == Qt.PenStyle.NoPen or not item.isVisible() or o.get("symbol") is not None
                    or o.get("fillLevel") is not None):   # more than a line: pyqtgraph's own, with this pen
                o["pen"] = line
                try:
                    type(sample).paint(sample, p, *args)
                finally:
                    o["pen"] = pen
                return
            y = sample.height() * 0.55                 # (pyqtgraph's: 11 px down a 20 px sample)
            to_px = p.worldTransform()
            p.save()
            p.setWorldTransform(QTransform())
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.setPen(line)
            p.drawLine(to_px.map(QPointF(0, y)), to_px.map(QPointF(sample.width(), y)))
            p.restore()
        return paint


def clip_lines(xy, left, top, right, bottom):
    """The parts of the polyline xy ((n, 2)) inside the rectangle (Liang-Barsky, each segment cut
    where it crosses the edge), as one array of points with a row of NaN between the pieces."""
    a, d = xy[:-1], np.diff(xy, axis=0)
    t0, t1 = np.zeros(len(d)), np.ones(len(d))
    keep = np.ones(len(d), bool)
    with np.errstate(divide="ignore", invalid="ignore"):
        for p, q in ((-d[:, 0], a[:, 0] - left), (d[:, 0], right - a[:, 0]),
                     (-d[:, 1], a[:, 1] - top), (d[:, 1], bottom - a[:, 1])):
            keep &= (p != 0) | (q >= 0)       # (along the edge: wholly in or wholly out)
            r = q / p
            t0 = np.where(p < 0, np.maximum(t0, r), t0)
            t1 = np.where(p > 0, np.minimum(t1, r), t1)
    i = np.nonzero(keep & (t0 <= t1))[0]
    if not i.size:
        return np.empty((0, 2))
    start, end = a[i] + t0[i, None] * d[i], a[i] + t1[i, None] * d[i]
    joined = np.zeros(i.size, bool)           # a segment carrying straight on from the one before
    joined[1:] = (i[1:] == i[:-1] + 1) & (t1[i[:-1]] >= 1) & (t0[i[1:]] <= 0)
    rows = np.where(joined, 1, 3)             # its end; or NaN, its start, its end
    at = np.cumsum(rows) - 1
    out = np.full((at[-1] + 1, 2), np.nan)
    out[at] = end
    out[at[~joined] - 1] = start[~joined]
    return out


def clip_path(path, transform, rect):
    """path (straight lines only, as pyqtgraph's curves are) mapped by transform, with what falls
    outside rect cut away."""
    import pyqtgraph.functions as fn
    out = QPainterPath()
    for poly in path.toSubpathPolygons(transform):
        xy = clip_lines(np.array(fn.ndarray_from_qpolygonf(poly), dtype=float),
                        rect.left(), rect.top(), rect.right(), rect.bottom())
        for piece in np.split(xy, np.nonzero(np.isnan(xy[:, 0]))[0]):
            piece = piece[~np.isnan(piece[:, 0])]
            if len(piece) > 1:
                part = fn.create_qpolygonf(len(piece))
                fn.ndarray_from_qpolygonf(part)[:] = piece
                out.addPolygon(part)
    return out


@contextmanager
def plot_lines(widget, width_px=None, colours=None, marker_px=None):
    """PlotLines(widget, width_px, colours=colours, marker_px=marker_px) for the length of the with."""
    lines = PlotLines(widget, width_px, colours=colours, marker_px=marker_px)
    try:
        yield lines
    finally:
        lines.remove()


@contextmanager
def background_painted(widget):
    """The graphics views in widget (pyqtgraph's plots) paint their backgrounds afresh for the length of
    the with. They keep them as a picture of the whole plot area (QGraphicsView's CacheBackground,
    pyqtgraph's choice) and paint that, so the PDF had an image in it - a blank white JPEG under the
    traces, which are lines - and the SVG a PNG; painted afresh, the background is a filled rectangle."""
    from PyQt6.QtWidgets import QGraphicsView
    views = widget.findChildren(QGraphicsView)
    inside = widget
    while inside is not None and not isinstance(inside, QGraphicsView):
        inside = inside.parentWidget()
    if inside is not None and inside not in views:
        views.insert(0, inside)
    kept = []
    for view in views:
        kept.append((view, view.cacheMode()))
        view.setCacheMode(QGraphicsView.CacheModeFlag.CacheNone)
        view.resetCachedContent()
    try:
        yield
    finally:
        for view, mode in kept:
            try:
                view.setCacheMode(mode)
                view.resetCachedContent()
            except RuntimeError:              # (the view has gone)
                pass


def print_text_pt(widget, width_cm, width_px=None):
    """Size the widget's (application) font will have on paper at width_cm."""
    font_pt = widget.font().pointSizeF()
    if font_pt <= 0:   # pixel-sized font
        font_pt = widget.font().pixelSize() * 72.0 / SCREEN_DPI
    on_screen_cm = (width_px or widget.width()) / SCREEN_DPI * 2.54
    return font_pt * width_cm / on_screen_cm if on_screen_cm else 0.0


def content_rect(widget, pad_px=8, tol=10):
    """Bounding box (widget coords) of everything that differs from the background
    colour, plus padding - trims the empty space a stretched dock/panel leaves."""
    pm = widget.grab()
    dpr = pm.devicePixelRatio() or 1.0
    img = pm.toImage().convertToFormat(QImage.Format.Format_RGB32)
    w, h = img.width(), img.height()
    if w < 4 or h < 4:
        return widget.rect()
    ptr = img.constBits()
    ptr.setsize(img.sizeInBytes())
    a = np.frombuffer(ptr, np.uint8).reshape(h, img.bytesPerLine() // 4, 4)[:, :w, :3].astype(np.int16)
    corners = np.array([a[-2, -2], a[-2, 1], a[1, -2]])
    bg = np.median(corners, axis=0)
    diff = (np.abs(a - bg).max(axis=2) > tol)
    rows, cols = np.nonzero(diff.any(axis=1))[0], np.nonzero(diff.any(axis=0))[0]
    if rows.size == 0 or cols.size == 0:
        return widget.rect()
    x0, x1 = cols[0] / dpr, (cols[-1] + 1) / dpr
    y0, y1 = rows[0] / dpr, (rows[-1] + 1) / dpr
    r = QRect(int(max(0, x0 - pad_px)), int(max(0, y0 - pad_px)), 0, 0)
    r.setRight(int(min(widget.width(), x1 + pad_px)) - 1)
    r.setBottom(int(min(widget.height(), y1 + pad_px)) - 1)
    return r


def render_widget(widget, out_base, width_cm=16.0, formats=("pdf", "svg", "png"), png_dpi=600, rect=None,
                  line_pt=None, stats=None, colours=None, marker_pt=None):
    """Render `widget` (and its children) - or just `rect` of it - to vector PDF/SVG
    and a high-DPI PNG whose printed width is width_cm. line_pt: plot lines that wide on the
    page, in points, for each screen pixel of their width (None: as they are, which in the
    PDF and PNG is the device's own pixel). colours: PlotLines' (ThesisStyle.colour_for); marker_pt:
    straight lines (event markers) that wide rather than line_pt.
    stats (a dict): gets "plot_lines", how many plot lines were drawn. Returns the files written."""
    os.makedirs(os.path.dirname(out_base) or ".", exist_ok=True)
    rect = rect if rect is not None else widget.rect()
    region = QRegion(rect)
    w, h = rect.width(), rect.height()
    height_cm = width_cm * h / w
    per_inch = w / (width_cm / 2.54)                     # the widget's pixels per inch on the page
    to_px = per_inch / 72.0
    with plot_lines(widget, line_pt * to_px if line_pt else None, colours,
                    marker_pt * to_px if marker_pt else None) as lines, background_painted(widget):
        written = _render(widget, out_base, rect, region, w, h, width_cm, height_cm, formats, png_dpi)
    if stats is not None:
        stats["plot_lines"] = len(lines.drawn)
    return written


def _render(widget, out_base, rect, region, w, h, width_cm, height_cm, formats, png_dpi):
    written = []

    if "pdf" in formats:
        path = f"{out_base}.pdf"
        pdf = QPdfWriter(path)
        pdf.setResolution(PDF_DPI)
        pdf.setCreator("SINDIE figure capture")
        pdf.setPageLayout(QPageLayout(QPageSize(QSizeF(width_cm * 10, height_cm * 10), QPageSize.Unit.Millimeter),
                                      QPageLayout.Orientation.Portrait, QMarginsF(0, 0, 0, 0)))
        p = QPainter(pdf)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        s = pdf.width() / w
        p.scale(s, s)
        _paint(widget, p, region)
        p.end()
        written.append(path)

    if "svg" in formats:
        from PyQt6.QtSvg import QSvgGenerator
        path = f"{out_base}.svg"
        gen = QSvgGenerator()
        gen.setFileName(path)
        # The window's pixels are the SVG's units (viewBox); its size on the page comes from width
        # and height. The resolution is the screen's: Qt's SVG writer turns a font's size in points
        # into units at it, and at the page's (the window's pixels per inch on the page, 254 for
        # 600 px at 6 cm) the window's 9 pt text came out 2.6 times too big.
        dpi = widget.logicalDpiX() or SCREEN_DPI
        gen.setResolution(dpi)
        gen.setSize(QSize(max(1, round(width_cm / 2.54 * dpi)), max(1, round(height_cm / 2.54 * dpi))))
        gen.setViewBox(QRect(0, 0, w, h))
        gen.setTitle(widget.windowTitle() or type(widget).__name__)
        p = QPainter(gen)
        _paint(widget, p, region)
        p.end()
        written.append(path)

    if "png" in formats:
        path = f"{out_base}.png"
        k = png_dpi * (width_cm / 2.54) / w          # device pixels per logical pixel
        img = QImage(max(1, round(w * k)), max(1, round(h * k)), QImage.Format.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(k)                   # widgets re-paint at this resolution
        img.fill(Qt.GlobalColor.transparent)
        img.setDotsPerMeterX(round(png_dpi / 0.0254))
        img.setDotsPerMeterY(round(png_dpi / 0.0254))
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        _paint(widget, p, region)
        p.end()
        img.save(path)
        written.append(path)
    return written


def has_ui_text(widget):
    """True if what is captured has text besides its plots' - labels, buttons, fields, tables - which a
    thesis style leaves as on screen."""
    from PyQt6.QtWidgets import (QAbstractButton, QAbstractItemView, QAbstractSpinBox, QGraphicsView,
                                 QTabBar)
    kinds = (QLabel, QAbstractButton, QLineEdit, QComboBox, QAbstractSpinBox, QGroupBox, QTabBar, QAbstractItemView)
    for wd in [widget] + widget.findChildren(QWidget):
        if not wd.isVisible() or not isinstance(wd, kinds):
            continue
        inside = wd.parentWidget()
        while inside is not None and inside is not widget and not isinstance(inside, QGraphicsView):
            inside = inside.parentWidget()
        if not isinstance(inside, QGraphicsView):
            return True
    return False


def has_emoji(widget):
    """True if any visible text in the widget uses emoji/pictographs. Qt's PDF/SVG
    engines cannot embed colour-emoji fonts, so those glyphs drop out of vector
    output (the PNG keeps them)."""
    import unicodedata

    def emoji(t):
        return any(ord(c) >= 0x2190 and unicodedata.category(c) == "So" for c in t)
    for wd in [widget] + widget.findChildren(QWidget):
        if not wd.isVisible():
            continue
        for attr in ("text", "title", "windowTitle", "placeholderText"):
            fn = getattr(wd, attr, None)
            try:
                if callable(fn) and isinstance(fn(), str) and emoji(fn()):
                    return True
            except TypeError:
                continue
    return False


# --------------------------------------------------------------------------- target discovery
def _describe(widget):
    if isinstance(widget, QDockWidget):
        name = widget.windowTitle()
    elif isinstance(widget, QGroupBox):
        name = widget.title()
    elif isinstance(widget, QMainWindow):
        name = f"Whole window ({widget.windowTitle() or type(widget).__name__})"
    else:
        name = widget.windowTitle() if widget.isWindow() else ""
        if not name:
            par = widget.parentWidget()
            if isinstance(par, QDockWidget):
                name = par.windowTitle() + " (contents)"
            elif par is not None and isinstance(par.parentWidget(), QTabWidget):
                tabs = par.parentWidget()
                i = tabs.indexOf(widget)
                name = f"Tab: {tabs.tabText(i)}" if i >= 0 else ""
        if not name:
            name = widget.objectName() or type(widget).__name__
    return name.strip() or type(widget).__name__


def capture_targets(window, hovered=None):
    """Candidate widgets: the hovered widget and its parents (innermost first),
    then every visible dock panel, then the whole window."""
    out, seen = [], set()

    def add(wd):
        if wd is None or id(wd) in seen or not wd.isVisible():
            return
        if min(wd.width(), wd.height()) < MIN_SIDE_PX and not wd.isWindow():
            return
        area = wd.parentWidget()
        if isinstance(area, QAbstractScrollArea) and area.viewport() is wd:
            return     # a plot's (scroll area's) inside, under the mouse: the plot itself comes next
        seen.add(id(wd))
        out.append(wd)

    wd = hovered
    while wd is not None and wd is not window:
        add(wd)
        wd = wd.parentWidget()
    for dock in window.findChildren(QDockWidget):
        add(dock)
    # windows without docks: offer the main areas (e.g. the viewer's sidebar and plots)
    central = window.centralWidget() if isinstance(window, QMainWindow) else None
    if central is not None:
        for child in central.findChildren(QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly):
            add(child)
        add(central)
    add(window)
    return out


# --------------------------------------------------------------------------- figure texts, remembered
def load_texts(path=None):
    """The thesis figure texts changed in the dialog before ({FigureText.key: text}); {} if none."""
    try:
        with open(path or TEXTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): str(v) for k, v in data.get("texts", {}).items()}
    except (OSError, ValueError, AttributeError, TypeError):
        return {}


def save_texts(texts, path=None):
    path = path or TEXTS_FILE
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"texts": texts}, f, indent=1, ensure_ascii=False, sort_keys=True)
        os.replace(path + ".tmp", path)
    except OSError as e:
        logging.getLogger("figure_capture").warning("Figure texts not remembered (%s): %s", path, e)


def _elide(text, n=44):
    return text if len(text) <= n else text[:n - 1] + "…"


# --------------------------------------------------------------------------- dialog
class FigureCaptureDialog(QDialog):
    def __init__(self, window, hovered=None):
        super().__init__(window)
        self.setWindowTitle("Capture GUI figure")
        self.setMinimumWidth(560)
        self.window_ = window
        self.targets = capture_targets(window, hovered)
        self._orig = (window.size(), window.isMaximized())
        self.band = QRubberBand(QRubberBand.Shape.Rectangle, window)

        lay = QVBoxLayout(self)
        f = self._form = QFormLayout()
        self.cmb_target = QComboBox()
        for wd in self.targets:
            self.cmb_target.addItem(f"{_describe(wd)}  -  {wd.width()} x {wd.height()} px")
        f.addRow("Capture", self.cmb_target)

        self.cmb_size = QComboBox()
        self.cmb_size.addItem(f"As on screen ({window.width()} x {window.height()})")
        for s_ in WINDOW_SIZES[1:]:
            self.cmb_size.addItem(f"Re-lay out window at {s_[0]} x {s_[1]} (live preview)")
        f.addRow("Window size", self.cmb_size)
        self.lbl_size = QLabel()
        self.lbl_size.setWordWrap(True)
        set_role(self.lbl_size, "warning")
        f.addRow("", self.lbl_size)

        self.sp_width = QDoubleSpinBox()
        self.sp_width.setRange(2, 40)
        self.sp_width.setDecimals(1)
        self.sp_width.setSuffix(" cm")
        self.sp_width.setValue(WIDTH_CM)
        self.sp_width.setToolTip("The width the figure is drawn at. 16 cm, as the thesis' other figures are "
                                 "drawn, to be included at the text width (15 cm): its text then prints a "
                                 "little smaller than the sizes shown here, as theirs does.")
        f.addRow("Width on page", self.sp_width)

        self.cb_trim = QCheckBox("Trim empty margins (e.g. a stretched panel)")
        self.cb_trim.setChecked(True)
        f.addRow("", self.cb_trim)

        self.cmb_style = QComboBox()
        self.cmb_style.addItem("As on screen", None)
        for key, text in STYLES.items():
            self.cmb_style.addItem(text, key)
        self.cmb_style.setCurrentIndex(max(0, self.cmb_style.findData(_last["style"])))
        self.cmb_style.setToolTip(
            "Thesis figure: the plots drawn in the style of the thesis' own figures - Computer Modern, "
            "9.5 pt axis titles with 8.5 pt numbers and legend, dark grey axes with the ticks out, a light "
            "grey grid behind the data, white, the light theme's colours (the thesis') - at their sizes "
            "on the page. The lanes, the legend's box, the clock-time axis and the event markers stay the "
            "software's. Only the plots change: the rest of what is captured is drawn as on screen. The "
            "window shows it while this dialog is open.")
        f.addRow("Style", self.cmb_style)

        self.sp_line = QDoubleSpinBox()
        self.sp_line.setRange(0.0, 5.0)
        self.sp_line.setDecimals(2)
        self.sp_line.setSingleStep(0.25)
        self.sp_line.setSuffix(" pt")
        self.sp_line.setSpecialValueText("Unchanged (hairline)")
        self.sp_line.setValue(_last["line_pt"])
        self.sp_line.setToolTip("How wide the plot lines print, for a line 1 px wide on the screen (a 2 px line "
                                "prints twice as wide). While this dialog is open, the window shows them as wide "
                                "as they will print. About 0.5 pt to 1 pt reads well on A4. At the bottom of the "
                                "range, the lines are left as they are: a hairline in the PDF and PNG.")
        f.addRow("Plot lines", self.sp_line)

        # the thesis figure's words: its axis titles and legend names (thesis_style.figure_texts)
        self.box_text = QGroupBox("Figure text")
        self.box_text.setToolTip(
            "The thesis figure's axis titles and legend names, as a figure has them: the channels named plainly, "
            "their units on the axis titles (mbar, not the software's mB), the time axis 'Time of day'. Change "
            "any of them here: the window shows them. What you change is remembered for the next capture of "
            "the same channels.")
        self._text_scroll = QScrollArea()     # (many channels: the fields scroll, the dialog stays on the screen)
        self._text_scroll.setWidgetResizable(True)
        self._text_scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        self.form_text = QFormLayout(inner)
        self.form_text.setContentsMargins(0, 0, 0, 0)
        self._text_scroll.setWidget(inner)
        QVBoxLayout(self.box_text).addWidget(self._text_scroll)
        self.box_text.hide()
        f.addRow(self.box_text)
        self._remembered = load_texts()       # changed in an earlier capture ({key: text})
        self._edited = {}                     # changed in this dialog
        self._rows, self._rows_for = {}, None  # key -> (FigureText, QLineEdit); for (target, style)
        self._text_timer = QTimer(self)
        self._text_timer.setSingleShot(True)
        self._text_timer.setInterval(300)
        self._text_timer.timeout.connect(self._preview)

        self.lbl_thesis = QLabel()
        self.lbl_thesis.setWordWrap(True)
        self.lbl_thesis.setToolTip("The thesis' figures are included at the text width, 15 cm "
                                   "(\\includegraphics[width=\\textwidth]): a figure drawn wider is shrunk, "
                                   "and its text with it.")
        f.addRow("In the thesis", self.lbl_thesis)

        self.lbl_text = QLabel()
        self.lbl_text.setWordWrap(True)
        f.addRow("UI text prints at", self.lbl_text)
        self.lbl_emoji = QLabel()
        self.lbl_emoji.setWordWrap(True)
        set_role(self.lbl_emoji, "warning")
        f.addRow("", self.lbl_emoji)

        h = QHBoxLayout()
        self.cb_pdf, self.cb_svg, self.cb_png = QCheckBox("PDF (vector)"), QCheckBox("SVG (vector)"), QCheckBox("PNG")
        for cb in (self.cb_pdf, self.cb_svg, self.cb_png):
            cb.setChecked(True)
            h.addWidget(cb)
        self.sp_dpi = QSpinBox()
        self.sp_dpi.setRange(150, 1200)
        self.sp_dpi.setSingleStep(150)
        self.sp_dpi.setValue(600)
        self.sp_dpi.setSuffix(" dpi")
        h.addWidget(self.sp_dpi)
        h.addStretch()
        f.addRow("Formats", h)

        self.ed_dir = QLineEdit(DEFAULT_DIR)
        b = QPushButton("...")
        b.setFixedWidth(30)
        b.clicked.connect(self._browse)
        h2 = QHBoxLayout()
        h2.addWidget(self.ed_dir)
        h2.addWidget(b)
        f.addRow("Folder", h2)
        self.ed_name = QLineEdit()
        f.addRow("Name", self.ed_name)
        lay.addLayout(f)

        tip = QLabel("Tip: for legible text on A4, capture single panels rather than the whole window, "
                     "or let the window re-lay out at a smaller size. Aim for UI text of at least ~6-7 pt.")
        tip.setWordWrap(True)
        set_role(tip, "caption")
        lay.addWidget(tip)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.btn_go = bb.addButton("Capture", QDialogButtonBox.ButtonRole.AcceptRole)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        self.lines = None            # the plot lines in the window, as wide as they will print
        self.look = None             # the plots in the window in the Style chosen
        self._w_px = None
        self.cmb_target.currentIndexChanged.connect(self._update)
        self.cmb_size.currentIndexChanged.connect(self._apply_size)
        self.sp_width.valueChanged.connect(self._update)
        self.cb_trim.toggled.connect(self._update)
        self.cmb_style.currentIndexChanged.connect(self._style_changed)
        self.sp_line.valueChanged.connect(self._preview)
        self._style_key = self.cmb_style.currentData()
        self._update()

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Figure folder", self.ed_dir.text())
        if d:
            self.ed_dir.setText(d)

    def target(self):
        return self.targets[self.cmb_target.currentIndex()]

    def _apply_size(self, *_):
        """Re-lay out the real window now, so what you see is what gets captured."""
        size = WINDOW_SIZES[self.cmb_size.currentIndex()]
        win = self.window_
        if size is None:
            self._restore()
        else:
            if win.isMaximized():
                win.showNormal()
            win.resize(*size)
        for _ in range(3):
            QApplication.processEvents()
        if size is not None and (win.width() > size[0] + 2 or win.height() > size[1] + 2):
            self.lbl_size.setText(f"The window's minimum size is {win.width()} x {win.height()}, so it "
                                  "can't shrink further - capture a single panel for larger text.")
        else:
            self.lbl_size.setText("")
        for i, wd in enumerate(self.targets):   # sizes changed with the layout
            self.cmb_target.setItemText(i, f"{_describe(wd)}  -  {wd.width()} x {wd.height()} px")
        self._update()

    def _restore(self):
        size, was_max = self._orig
        win = self.window_
        win.resize(size)
        if was_max and not win.isMaximized():
            win.showMaximized()

    def _update(self, *_):
        wd = self.target()
        self._build_text_rows()
        # highlight the selection on the live window
        tl = wd.mapTo(self.window_, QPoint(0, 0)) if wd is not self.window_ else QPoint(0, 0)
        self.band.setGeometry(QRect(tl, wd.size()))
        self.band.show()
        # measured on the live (possibly re-laid-out) widget
        w_px = content_rect(wd).width() if self.cb_trim.isChecked() else wd.width()
        self._w_px = w_px
        self._preview()
        font_pt = wd.font().pointSizeF() if wd.font().pointSizeF() > 0 else wd.font().pixelSize() * 0.75
        pt = font_pt * self.sp_width.value() / (w_px / SCREEN_DPI * 2.54)
        t = theme.tokens()
        col = t["ok"] if pt >= 7 else (t["warn"] if pt >= 5 else t["fault"])
        msg = f"<b style='color:{col}'>about {pt:.1f} pt</b> (body text is usually 10-12 pt, captions 8-10 pt)"
        if pt < 5:
            msg += " - too small: capture a smaller panel or a smaller window size"
        if self.cmb_style.currentData():
            msg += f"; the plots' text {TITLE_PT:g} pt and {SMALL_PT:g} pt"
        self.lbl_text.setText(msg)
        thesis = bool(self.cmb_style.currentData())
        self._form.setRowVisible(self.lbl_thesis, thesis)
        self._form.setRowVisible(self.lbl_text, not thesis or has_ui_text(wd))   # (plots only: In the thesis)
        if thesis:
            k = TEXT_WIDTH_CM / self.sp_width.value()
            small, others = SMALL_PT * k, SMALL_PT * TEXT_WIDTH_CM / WIDTH_CM
            col = t["ok"] if small >= 7.5 else (t["warn"] if small >= 6.5 else t["fault"])
            text = (f"<b style='color:{col}'>axis titles {TITLE_PT * k:.1f} pt, numbers and legend {small:.1f} pt"
                    f"</b>, included at the text width ({TEXT_WIDTH_CM:g} cm)")
            if small < 7.5:
                text += (f" - smaller than in the thesis' other figures ({others:.1f} pt): draw it "
                         f"{WIDTH_CM:g} cm wide, as they are")
            elif abs(self.sp_width.value() - WIDTH_CM) < 0.05:
                text += ", as in the thesis' other figures"
            self.lbl_thesis.setText(text)
        self.lbl_emoji.setText(
            "This area has emoji icons on buttons/labels. Qt can't embed colour emoji in PDF/SVG, so they "
            "will be missing there - the PNG (re-painted at the chosen dpi) shows them correctly."
            if has_emoji(wd) else "")
        if not self.ed_name.isModified():
            safe = "".join(c if c.isalnum() else "_" for c in _describe(wd)).strip("_")[:40]
            self.ed_name.setText(safe or "gui")

    def _style_changed(self, *_):
        key = self.cmb_style.currentData()
        if key and not self._style_key:      # into a thesis style: its line width too (can be changed)
            self.sp_line.blockSignals(True)
            self.sp_line.setValue(THESIS_LINE_PT)
            self.sp_line.blockSignals(False)
        self._style_key = key
        self._update()                       # (the plots' text changes size, and with it the layout)

    def _build_text_rows(self):
        """Figure text: a field for each of the thesis figure's texts in what is captured
        (thesis_style.figure_texts), holding what was chosen for it before - in this dialog, or in an
        earlier capture - or else the plain text worked out for it. Built again for another target."""
        key = self.cmb_style.currentData()
        if self._rows_for == (id(self.target()), key):
            return
        self._rows_for = (id(self.target()), key)
        while self.form_text.rowCount():
            self.form_text.removeRow(0)
        self._rows = {}
        if not key:
            self.box_text.hide()
            return
        self._unpreview()                     # (the names as the window has them)
        for spec in figure_texts(self.target()):
            if spec.key in self._rows:        # (one field for every time axis)
                continue
            ed = QLineEdit(self._edited.get(spec.key, self._remembered.get(spec.key, spec.default)))
            ed.setPlaceholderText(spec.default if spec.kind == "legend" else "No title")
            ed.setToolTip(f"Worked out: {spec.default or '(no title)'}" if spec.kind == "axis" else
                          f"The legend's name for {spec.what}. Worked out: {spec.default}; left empty, that.")
            ed.setProperty("figure_text", spec.key)
            ed.textEdited.connect(self._text_edited)   # (a method, not a lambda: that would keep the dialog)
            name = QLabel(_elide(spec.what))
            name.setToolTip(spec.what)
            self.form_text.addRow(name, ed)
            self._rows[spec.key] = (spec, ed)
        if self._rows:
            b = QPushButton("Worked-out texts")
            b.setToolTip("Every text here back to the one worked out from the channels' names and units.")
            b.clicked.connect(self._reset_texts)
            h = QHBoxLayout()
            h.addStretch()
            h.addWidget(b)
            self.form_text.addRow(h)
        inner = self._text_scroll.widget()
        for wd in [inner] + inner.findChildren(QWidget):
            wd.ensurePolished()               # (their size in the theme's style, before the dialog is shown)
        self.form_text.invalidate()
        self.form_text.activate()
        self._text_scroll.setFixedHeight(min(self.form_text.sizeHint().height() + 2, TEXT_BOX_MAX_PX))
        self.box_text.setVisible(bool(self._rows))
        if self.isVisible():
            QTimer.singleShot(0, self._fit_height)    # (once the layout has taken the box in)

    def _text_edited(self, text):
        key = self.sender().property("figure_text") if self.sender() is not None else None
        if key:
            self._edited[key] = text
        self._text_timer.start()              # (the window shows it once the typing pauses)

    def _reset_texts(self):
        for k, (spec, ed) in self._rows.items():
            ed.setText(spec.default)
            self._edited[k] = spec.default
        self._preview()

    def texts(self):
        """The figure texts chosen: those changed in an earlier capture, and the fields here."""
        return {**self._remembered, **{k: ed.text() for k, (_spec, ed) in self._rows.items()}}

    def _remember_texts(self):
        """The fields that differ from the worked-out text, kept for the next capture (data/figure_text.json)."""
        kept = dict(self._remembered)
        for k, (spec, ed) in self._rows.items():
            text = ed.text()
            if text == spec.default or (spec.kind == "legend" and not text.strip()):
                kept.pop(k, None)
            else:
                kept[k] = text
        if kept != self._remembered:
            save_texts(kept)
            self._remembered = kept

    def _unpreview(self):
        for part in (self.lines, self.look):  # the window's plots as they were
            if part is not None:
                part.remove()
        self.lines = self.look = None

    def _preview(self, *_):
        """What will be captured, on the window: the plots in the Style chosen, and their lines as
        wide as they will print (the capture scales the window's pixels to the page, so a size on
        the page is a size in them)."""
        self._unpreview()
        if not self._w_px:
            return
        per_inch = self._w_px / (self.sp_width.value() / 2.54)
        key = self.cmb_style.currentData()
        if key:
            self.look = look = ThesisStyle(self.target(), per_inch, key, texts=self.texts())
            QTimer.singleShot(250, lambda: look.align() if self.look is look else None)   # (once laid out)
        line_pt = self.sp_line.value()
        if line_pt or self.look is not None:
            self.lines = PlotLines(self.target(), line_pt * per_inch / 72.0 if line_pt else None, live=True,
                                   colours=self.look.colour_for if self.look is not None else None,
                                   marker_px=MARKER_PT * per_inch / 72.0 if self.look is not None else None)

    def _fit_height(self):
        """Tall enough for everything at its width: Qt sizes a dialog of wrapping labels by its layout's
        least height, which leaves the Figure text box short of its fields."""
        lay = self.layout()
        need = lay.heightForWidth(self.width()) if lay.hasHeightForWidth() else lay.sizeHint().height()
        if self.height() < need:
            self.resize(self.width(), need)

    def showEvent(self, ev):
        super().showEvent(ev)
        self._fit_height()

    def done(self, r):
        self._unpreview()
        self.band.hide()
        self.band.deleteLater()
        if r != QDialog.DialogCode.Accepted:
            self._restore()   # cancelled: put the window back
        super().done(r)

    def options(self):
        fm = tuple(f for f, cb in (("pdf", self.cb_pdf), ("svg", self.cb_svg), ("png", self.cb_png)) if cb.isChecked())
        _last["line_pt"] = self.sp_line.value()
        _last["style"] = self.cmb_style.currentData()
        if self.cmb_style.currentData():
            self._remember_texts()
        # the dialog has already applied the size live; capture() restores afterwards
        return {"widget": self.target(), "size": None, "restore": self._orig,
                "trim": self.cb_trim.isChecked(),
                "width_cm": self.sp_width.value(), "formats": fm, "dpi": self.sp_dpi.value(),
                "line_pt": self.sp_line.value() or None, "style": self.cmb_style.currentData(),
                "texts": self.texts(), "folder": self.ed_dir.text(), "name": self.ed_name.text() or "gui"}


def _settle(widget):
    """Let the window catch up with a change of style: the plots' axes measure their text as they
    paint and then ask for the room it needs."""
    for _ in range(2):
        widget.grab()
        for _ in range(3):
            QApplication.processEvents()


def capture(window, opts, notify=True):
    """Run a capture with options from FigureCaptureDialog.options(). Returns files."""
    wd = opts["widget"]
    for rb in window.findChildren(QRubberBand):   # never capture a selection highlight
        rb.hide()
    orig = window.size()
    was_max = window.isMaximized()
    look = None
    try:
        if opts["size"] is not None:
            if was_max:
                window.showNormal()
            window.resize(*opts["size"])
            for _ in range(3):          # let layouts and plots settle at the new size
                QApplication.processEvents()
        base = os.path.join(opts["folder"], f"{opts['name']}_{datetime.now():%Y%m%d_%H%M%S}")
        trim = opts.get("trim", True)
        rect = content_rect(wd) if trim else None
        if opts.get("style"):
            # The plots' text at its size on the page changes the layout, and with it what the trim
            # keeps, which sets the scale to the page: once more if that moved the scale.
            per_inch = (rect or wd.rect()).width() / (opts["width_cm"] / 2.54)
            for _ in range(2):
                if look is not None:
                    look.remove()
                look = ThesisStyle(wd, per_inch, opts["style"], texts=opts.get("texts"))
                _settle(wd)
                look.align()
                _settle(wd)
                rect = content_rect(wd) if trim else None
                now = (rect or wd.rect()).width() / (opts["width_cm"] / 2.54)
                if abs(now / per_inch - 1) < 0.01:
                    break
                per_inch = now
        stats = {}
        files = render_widget(wd, base, opts["width_cm"], opts["formats"], opts["dpi"], rect=rect,
                              line_pt=opts.get("line_pt"), stats=stats,
                              colours=look.colour_for if look is not None else None,
                              marker_pt=MARKER_PT if look is not None else None)
        if look is not None:                # the SVG names other forms of the type, as the thesis' do
            for path in (f for f in files if f.endswith(".svg")):
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text.replace(f'font-family="{look.family}"', f'font-family="{SVG_FONTS}"'))
    finally:
        if look is not None:
            look.remove()
        if opts["size"] is not None:
            window.resize(orig)
            if was_max:
                window.showMaximized()
        elif opts.get("restore") is not None:      # size was applied live by the dialog
            size, was_max0 = opts["restore"]
            window.resize(size)
            if was_max0 and not window.isMaximized():
                window.showMaximized()
    n, line_pt = stats.get("plot_lines", 0), opts.get("line_pt")
    note = ("" if not line_pt else f"Plot lines: {n} drawn {line_pt:g} pt wide." if n else
            "Plot lines: there are none in the captured area, so the width changed nothing.")
    logging.getLogger("figure_capture").info("Figure saved: %s%s", ", ".join(files), f" ({note})" if note else "")
    if notify and files:
        from PyQt6.QtWidgets import QMessageBox
        box = QMessageBox(QMessageBox.Icon.Information, "GUI figure saved", "\n".join(files), parent=window)
        if note:
            box.setInformativeText(note)
        btn = box.addButton("Open folder", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Close)
        btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(files[0]))))
        box.setModal(False)
        box.show()
    return files


def open_capture_dialog(window):
    hovered = QApplication.widgetAt(QCursor.pos())
    if hovered is not None and hovered.window() is not window:
        hovered = None
    dlg = FigureCaptureDialog(window, hovered)
    if dlg.exec():
        o = dlg.options()
        # let the dialog disappear before re-painting the window
        QTimer.singleShot(50, lambda: capture(window, o))


def install_figure_capture(window, key="F12"):
    """Add the capture hotkey to a top-level window (viewer or HMI)."""
    sc = QShortcut(QKeySequence(key), window)
    sc.setContext(Qt.ShortcutContext.WindowShortcut)
    sc.activated.connect(lambda: open_capture_dialog(window))
    window._figure_capture_shortcut = sc
    return sc


def add_capture_action(menu, window, key="F12"):
    """Add 'Capture GUI figure...' to a menu (the hotkey also works)."""
    from PyQt6.QtGui import QAction
    act = QAction(f"Capture GUI figure...\t{key}", window)
    act.triggered.connect(lambda: open_capture_dialog(window))
    menu.addAction(act)
    return act
