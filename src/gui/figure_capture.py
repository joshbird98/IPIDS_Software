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
import logging
import os
from contextlib import contextmanager
from datetime import datetime

import numpy as np
from PyQt6.QtCore import QMarginsF, QPoint, QRect, QSizeF, Qt, QTimer, QUrl
from PyQt6.QtGui import (QCursor, QDesktopServices, QImage, QKeySequence, QPageLayout, QPageSize,
                         QPaintEngine, QPainter, QPainterPath, QPdfWriter, QPen, QRegion, QShortcut,
                         QTransform)
from PyQt6.QtWidgets import (QAbstractScrollArea, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                             QDockWidget, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
                             QHBoxLayout, QLabel, QLineEdit, QMainWindow, QPushButton, QRubberBand,
                             QSpinBox, QTabWidget, QVBoxLayout, QWidget)

from src.gui import theme
from src.gui.style import set_role

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
DEFAULT_DIR = os.path.join(PROJECT_ROOT, "data", "exports", "figures")
SCREEN_DPI = 96.0           # Qt logical pixels per inch
MIN_SIDE_PX = 120           # ignore tiny widgets when offering what to capture
WINDOW_SIZES = [None, (1024, 640), (1280, 720), (1280, 800), (1440, 900), (1600, 900), (1920, 1080)]
PDF_DPI = 1200
LINE_PT = 0.75              # Plot lines at first: the printed width of a line 1 px wide on the screen
_last = {"line_pt": LINE_PT}   # (the dialog starts where the last capture left it)


# --------------------------------------------------------------------------- rendering
def _render_flags():
    return QWidget.RenderFlag.DrawWindowBackground | QWidget.RenderFlag.DrawChildren


def _paint(widget, painter, region):
    # Call QWidget.render explicitly: QGraphicsView (pyqtgraph plots) overrides render()
    # with a different signature (painter, target QRectF, source QRect, mode).
    QWidget.render(widget, painter, QPoint(0, 0), region, _render_flags())


class PlotLines:
    """The plot lines (pyqtgraph curves) in a widget, painted by this class until remove():

    * in the coordinates of the widget's own pixels, not the plot's. Qt's SVG writer keeps 6
      significant figures of whatever coordinates it is given, and the viewer's x is seconds from
      when it started, so a trace a day or more from there came out in whole-second steps;
    * with width_px, that many of the widget's pixels wide for each pixel of the line's own width
      (a 2 px trace stays twice a 1 px one), round-cornered, and the legend's line samples to
      match. The capture scales the widget's pixels to the page, so the window shows, while this is
      in place, the width the lines will print at.

    Only how those items paint is replaced, on each item alone: nothing in the plot changes - not
    its pens, data or ranges - so nothing pyqtgraph does to a curve (it re-applies a curve's own pen
    whenever the plot re-ranges, which it may do in the middle of a paint) can undo it.
    live: the window's own plots - repaint them whole while this is in place, so a line wider than
    pyqtgraph allows for leaves no trail when it moves."""

    def __init__(self, widget, width_px=None, live=False):
        import pyqtgraph as pg
        from pyqtgraph.graphicsItems.LegendItem import ItemSample
        self.width_px = width_px or None
        self.drawn = set()                    # the curves painted (ids)
        self._items, self._modes = [], []
        views = widget.findChildren(pg.GraphicsView)
        inside = widget                       # the widget itself, or the plot it is part of (the view's
        while inside is not None and not isinstance(inside, pg.GraphicsView):   # viewport, under the mouse)
            inside = inside.parentWidget()
        if inside is not None:
            views.insert(0, inside)
        self.views = views
        scenes = []
        for view in views:
            scene = view.scene()
            if scene is None or any(scene is s for s in scenes):
                continue
            scenes.append(scene)
            for item in scene.items():
                if isinstance(item, pg.PlotCurveItem):
                    item.paint = self._curve_paint(item)
                elif isinstance(item, ItemSample) and isinstance(getattr(item, "item", None),
                                                                 (pg.PlotDataItem, pg.PlotCurveItem)):
                    item.paint = self._sample_paint(item)       # (a line's sample: not a scatter's symbol)
                else:
                    continue
                self._items.append(item)
        if live:
            from PyQt6.QtWidgets import QGraphicsView
            for view in views:
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

    def pen(self, pen):
        """What a line pyqtgraph would draw with pen is drawn with (in the widget's pixels)."""
        if not self.width_px:
            return pen                        # as it is: a cosmetic pen, its width in the device's pixels
        wide = QPen(pen)
        wide.setCosmetic(False)
        wide.setWidthF(max(pen.widthF(), 1.0) * self.width_px)
        wide.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        if pen.style() == Qt.PenStyle.SolidLine:   # (round ends would lengthen a dash)
            wide.setCapStyle(Qt.PenCapStyle.RoundCap)
        return wide

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
            p.setPen(self.pen(pen))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
            p.restore()
            self.drawn.add(id(curve))
        return paint

    def _sample_paint(self, sample):
        def paint(p, *args):
            opts = sample.item.opts
            pen = opts.get("pen")
            if not self.width_px or not isinstance(pen, QPen):
                return type(sample).paint(sample, p, *args)
            wide = self.pen(pen)
            wide.setCapStyle(Qt.PenCapStyle.FlatCap)   # (the sample keeps its length)
            opts["pen"] = wide
            try:
                type(sample).paint(sample, p, *args)
            finally:
                opts["pen"] = pen
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
def plot_lines(widget, width_px=None):
    """PlotLines(widget, width_px) for the length of the with."""
    lines = PlotLines(widget, width_px)
    try:
        yield lines
    finally:
        lines.remove()


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
                  line_pt=None, stats=None):
    """Render `widget` (and its children) - or just `rect` of it - to vector PDF/SVG
    and a high-DPI PNG whose printed width is width_cm. line_pt: plot lines that wide on the
    page, in points, for each screen pixel of their width (None: as they are, which in the
    PDF and PNG is the device's own pixel). stats (a dict): gets "plot_lines", how many plot
    lines were drawn. Returns the files written."""
    os.makedirs(os.path.dirname(out_base) or ".", exist_ok=True)
    rect = rect if rect is not None else widget.rect()
    region = QRegion(rect)
    w, h = rect.width(), rect.height()
    height_cm = width_cm * h / w
    per_inch = w / (width_cm / 2.54)                     # the widget's pixels per inch on the page
    svg_dpi = int(round(per_inch))                       # SVG user units per inch: physical width = width_cm
    with plot_lines(widget, line_pt * per_inch / 72.0 if line_pt else None) as lines:
        written = _render(widget, out_base, rect, region, w, h, width_cm, height_cm, svg_dpi, formats, png_dpi)
    if stats is not None:
        stats["plot_lines"] = len(lines.drawn)
    return written


def _render(widget, out_base, rect, region, w, h, width_cm, height_cm, svg_dpi, formats, png_dpi):
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
        gen.setSize(rect.size())
        gen.setViewBox(QRect(0, 0, w, h))
        gen.setResolution(svg_dpi)
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
        f = QFormLayout()
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
        self.sp_width.setValue(16.0)
        f.addRow("Width on page", self.sp_width)

        self.cb_trim = QCheckBox("Trim empty margins (e.g. a stretched panel)")
        self.cb_trim.setChecked(True)
        f.addRow("", self.cb_trim)

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
        self._w_px = None
        self.cmb_target.currentIndexChanged.connect(self._update)
        self.cmb_size.currentIndexChanged.connect(self._apply_size)
        self.sp_width.valueChanged.connect(self._update)
        self.cb_trim.toggled.connect(self._update)
        self.sp_line.valueChanged.connect(self._preview_lines)
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
        # highlight the selection on the live window
        tl = wd.mapTo(self.window_, QPoint(0, 0)) if wd is not self.window_ else QPoint(0, 0)
        self.band.setGeometry(QRect(tl, wd.size()))
        self.band.show()
        # measured on the live (possibly re-laid-out) widget
        w_px = content_rect(wd).width() if self.cb_trim.isChecked() else wd.width()
        self._w_px = w_px
        self._preview_lines()
        font_pt = wd.font().pointSizeF() if wd.font().pointSizeF() > 0 else wd.font().pixelSize() * 0.75
        pt = font_pt * self.sp_width.value() / (w_px / SCREEN_DPI * 2.54)
        t = theme.tokens()
        col = t["ok"] if pt >= 7 else (t["warn"] if pt >= 5 else t["fault"])
        msg = f"<b style='color:{col}'>about {pt:.1f} pt</b> (body text is usually 10-12 pt, captions 8-10 pt)"
        if pt < 5:
            msg += " - too small: capture a smaller panel or a smaller window size"
        self.lbl_text.setText(msg)
        self.lbl_emoji.setText(
            "This area has emoji icons on buttons/labels. Qt can't embed colour emoji in PDF/SVG, so they "
            "will be missing there - the PNG (re-painted at the chosen dpi) shows them correctly."
            if has_emoji(wd) else "")
        if not self.ed_name.isModified():
            safe = "".join(c if c.isalnum() else "_" for c in _describe(wd)).strip("_")[:40]
            self.ed_name.setText(safe or "gui")

    def _preview_lines(self, *_):
        """The plot lines in what will be captured, on the window, as wide as they will print (the
        capture scales the window's pixels to the page, so that width in them is what prints)."""
        if self.lines is not None:
            self.lines.remove()
            self.lines = None
        line_pt = self.sp_line.value()
        if line_pt and self._w_px:
            per_inch = self._w_px / (self.sp_width.value() / 2.54)
            self.lines = PlotLines(self.target(), line_pt * per_inch / 72.0, live=True)

    def done(self, r):
        if self.lines is not None:   # the window's plot lines as they were
            self.lines.remove()
            self.lines = None
        self.band.hide()
        self.band.deleteLater()
        if r != QDialog.DialogCode.Accepted:
            self._restore()   # cancelled: put the window back
        super().done(r)

    def options(self):
        fm = tuple(f for f, cb in (("pdf", self.cb_pdf), ("svg", self.cb_svg), ("png", self.cb_png)) if cb.isChecked())
        _last["line_pt"] = self.sp_line.value()
        # the dialog has already applied the size live; capture() restores afterwards
        return {"widget": self.target(), "size": None, "restore": self._orig,
                "trim": self.cb_trim.isChecked(),
                "width_cm": self.sp_width.value(), "formats": fm, "dpi": self.sp_dpi.value(),
                "line_pt": self.sp_line.value() or None,
                "folder": self.ed_dir.text(), "name": self.ed_name.text() or "gui"}


def capture(window, opts, notify=True):
    """Run a capture with options from FigureCaptureDialog.options(). Returns files."""
    wd = opts["widget"]
    for rb in window.findChildren(QRubberBand):   # never capture a selection highlight
        rb.hide()
    orig = window.size()
    was_max = window.isMaximized()
    try:
        if opts["size"] is not None:
            if was_max:
                window.showNormal()
            window.resize(*opts["size"])
            for _ in range(3):          # let layouts and plots settle at the new size
                QApplication.processEvents()
        base = os.path.join(opts["folder"], f"{opts['name']}_{datetime.now():%Y%m%d_%H%M%S}")
        rect = content_rect(wd) if opts.get("trim", True) else None
        stats = {}
        files = render_widget(wd, base, opts["width_cm"], opts["formats"], opts["dpi"], rect=rect,
                              line_pt=opts.get("line_pt"), stats=stats)
    finally:
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
