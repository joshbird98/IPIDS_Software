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
* No window title bar/frame, no mouse cursor.

Use: press F12 with the mouse over the part you want (works in any viewer or
HMI window), choose what to capture and the width on the page, Capture.
Files go to data/exports/figures/<name>_<timestamp>.pdf/.svg/.png.
"""
import logging
import os
from datetime import datetime

from PyQt6.QtCore import QMarginsF, QPoint, QRect, QSizeF, Qt, QTimer, QUrl
from PyQt6.QtGui import (QCursor, QDesktopServices, QImage, QKeySequence, QPageLayout, QPageSize,
                         QPainter, QPdfWriter, QRegion, QShortcut)
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                             QDockWidget, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
                             QHBoxLayout, QLabel, QLineEdit, QMainWindow, QPushButton, QRubberBand,
                             QSpinBox, QTabWidget, QVBoxLayout, QWidget)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
DEFAULT_DIR = os.path.join(PROJECT_ROOT, "data", "exports", "figures")
SCREEN_DPI = 96.0           # Qt logical pixels per inch
MIN_SIDE_PX = 120           # ignore tiny widgets when offering what to capture
WINDOW_SIZES = [None, (1024, 640), (1280, 720), (1280, 800), (1440, 900), (1600, 900), (1920, 1080)]


# --------------------------------------------------------------------------- rendering
def _render_flags():
    return QWidget.RenderFlag.DrawWindowBackground | QWidget.RenderFlag.DrawChildren


def _paint(widget, painter, region):
    # Call QWidget.render explicitly: QGraphicsView (pyqtgraph plots) overrides render()
    # with a different signature (painter, target QRectF, source QRect, mode).
    QWidget.render(widget, painter, QPoint(0, 0), region, _render_flags())


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
    import numpy as np
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


def render_widget(widget, out_base, width_cm=16.0, formats=("pdf", "svg", "png"), png_dpi=600, rect=None):
    """Render `widget` (and its children) - or just `rect` of it - to vector PDF/SVG
    and a high-DPI PNG whose printed width is width_cm. Returns the files written."""
    os.makedirs(os.path.dirname(out_base) or ".", exist_ok=True)
    rect = rect if rect is not None else widget.rect()
    region = QRegion(rect)
    w, h = rect.width(), rect.height()
    height_cm = width_cm * h / w
    written = []

    if "pdf" in formats:
        path = f"{out_base}.pdf"
        pdf = QPdfWriter(path)
        pdf.setResolution(1200)
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
        gen.setResolution(int(round(w / (width_cm / 2.54))))   # physical width = width_cm
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
        self.lbl_size.setStyleSheet("color: #E65100;")
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

        self.lbl_text = QLabel()
        self.lbl_text.setWordWrap(True)
        f.addRow("UI text prints at", self.lbl_text)
        self.lbl_emoji = QLabel()
        self.lbl_emoji.setWordWrap(True)
        self.lbl_emoji.setStyleSheet("color: #E65100;")
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
        tip.setStyleSheet("color: #666;")
        lay.addWidget(tip)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.btn_go = bb.addButton("Capture", QDialogButtonBox.ButtonRole.AcceptRole)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        self.cmb_target.currentIndexChanged.connect(self._update)
        self.cmb_size.currentIndexChanged.connect(self._apply_size)
        self.sp_width.valueChanged.connect(self._update)
        self.cb_trim.toggled.connect(self._update)
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
        font_pt = wd.font().pointSizeF() if wd.font().pointSizeF() > 0 else wd.font().pixelSize() * 0.75
        pt = font_pt * self.sp_width.value() / (w_px / SCREEN_DPI * 2.54)
        col = "#2E7D32" if pt >= 7 else ("#E65100" if pt >= 5 else "#C62828")
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

    def done(self, r):
        self.band.hide()
        self.band.deleteLater()
        if r != QDialog.DialogCode.Accepted:
            self._restore()   # cancelled: put the window back
        super().done(r)

    def options(self):
        fm = tuple(f for f, cb in (("pdf", self.cb_pdf), ("svg", self.cb_svg), ("png", self.cb_png)) if cb.isChecked())
        # the dialog has already applied the size live; capture() restores afterwards
        return {"widget": self.target(), "size": None, "restore": self._orig,
                "trim": self.cb_trim.isChecked(),
                "width_cm": self.sp_width.value(), "formats": fm, "dpi": self.sp_dpi.value(),
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
        files = render_widget(wd, base, opts["width_cm"], opts["formats"], opts["dpi"], rect=rect)
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
    logging.getLogger("figure_capture").info("Figure saved: %s", ", ".join(files))
    if notify and files:
        from PyQt6.QtWidgets import QMessageBox
        box = QMessageBox(QMessageBox.Icon.Information, "GUI figure saved", "\n".join(files), parent=window)
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
