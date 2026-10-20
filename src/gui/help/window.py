"""The help window: the pages in docs/, with their contents, a search, and back and forward.

    show_help(page, anchor)       open the window (or bring it to the front) at a page
    context_help(window, default) F1: the page of the panel under the mouse pointer
    add_help_menu(...)            the Help menu of the HMI and of the data viewer

The window has no parent, so it can sit beside the HMI or on another screen, but it never keeps
a program running after its main window has closed. Pages are converted to HTML when opened (in
the current theme, again after a switch), and screenshots are scaled to fit the text column.
"""
import html
import logging
import os
import re

from PyQt6 import sip
from PyQt6.QtCore import QMarginsF, QSize, Qt, QTimer, QUrl
from PyQt6.QtGui import (QAction, QCursor, QFont, QImage, QImageReader, QKeySequence, QPageLayout, QPageSize,
                         QTextCharFormat, QTextCursor, QTextDocument)
from PyQt6.QtWidgets import (QApplication, QDockWidget, QFileDialog, QFrame, QLabel, QLineEdit, QListWidget,
                             QListWidgetItem, QMainWindow, QMenu, QMessageBox, QSplitter, QStackedWidget,
                             QTextBrowser, QTextEdit, QToolBar, QToolButton, QTreeWidget, QTreeWidgetItem,
                             QVBoxLayout, QWidget)

from src.gui import theme
from src.gui.help import pages, render
from src.gui.settings import gui_settings
from src.gui.style import set_role

log = logging.getLogger("help")

_HTML = QTextDocument.ResourceType.HtmlResource
_IMAGE = QTextDocument.ResourceType.ImageResource
TEXT_SIZES = (9.0, 10.0, 10.5, 11.5, 12.5, 14.0, 16.0)
DEFAULT_TEXT_SIZE = 10.5
PRINT_IMAGE_WIDTH = 640          # px at 96 dpi: an A4 page's text width, less a little


class HelpBrowser(QTextBrowser):
    """One help page. Markdown pages are converted to HTML here; images are scaled to the
    column. The text is kept to a readable column, centred in a wide window."""
    MAX_COLUMN = 900
    MIN_SIDE = 28

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("HelpPage")
        self.setOpenExternalLinks(True)           # web links open in the web browser
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.base_pt = DEFAULT_TEXT_SIZE
        self.highlight_terms = []
        self._cache = {}
        self._column = None
        self._reflow = QTimer(self)
        self._reflow.setSingleShot(True)
        self._reflow.setInterval(150)
        self._reflow.timeout.connect(self._rescale_images)

    # --- loading ------------------------------------------------------------------------------
    def doSetSource(self, name, rtype=QTextDocument.ResourceType.UnknownResource):
        if name.isLocalFile() and name.toLocalFile().lower().endswith(".md") and render.available():
            rtype = _HTML                      # (otherwise Qt renders the Markdown itself)
        super().doSetSource(name, rtype)
        self._fit_column(force=True)
        self.show_highlights()

    def loadResource(self, rtype, url):
        try:
            if url.isLocalFile():
                path = url.toLocalFile()
                if rtype == _HTML.value and path.lower().endswith(".md") and render.available():
                    return self._page_html(path)
                if rtype == _IMAGE.value:
                    image = self._image(path)
                    if image is not None:
                        return image
        except Exception:        # a broken page must not take the program down
            log.exception("Help: could not load %s", url.toString())
            if rtype == _HTML.value:
                return f"<p>This page could not be shown: {html.escape(url.toLocalFile())}</p>"
        return super().loadResource(rtype, url)

    def _page_html(self, path: str) -> str:
        try:
            stamp = os.path.getmtime(path)
        except OSError:
            stamp = 0
        key = (path, stamp, theme.current_mode(), self.base_pt)
        page = self._cache.get(key)
        if page is None:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
            fonts = theme.fonts()
            page = render.to_html(text, theme.tokens(), fonts["ui"], fonts["mono"], self.base_pt)
            self._cache[key] = page
        return page

    def _image(self, path: str):
        image = QImage(path)
        if image.isNull():
            return None
        width = self.column_width() - 4
        if 0 < width < image.width():
            dpr = max(1.0, self.devicePixelRatioF())
            image = image.scaledToWidth(int(width * dpr), Qt.TransformationMode.SmoothTransformation)
            image.setDevicePixelRatio(dpr)
        return image

    # --- the text column ------------------------------------------------------------------------
    def _side_margin(self) -> int:
        return max(self.MIN_SIDE, (self.viewport().width() - self.MAX_COLUMN) // 2)

    def column_width(self) -> int:
        return max(100, self.viewport().width() - 2 * self._side_margin())

    def _fit_column(self, force: bool = False):
        side = self._side_margin()
        root = self.document().rootFrame()
        fmt = root.frameFormat()
        if force or fmt.leftMargin() != side:
            fmt.setLeftMargin(side)
            fmt.setRightMargin(side)
            fmt.setTopMargin(18)
            fmt.setBottomMargin(48)
            root.setFrameFormat(fmt)
        width = self.column_width()
        if self._column is not None and width != self._column:
            self._reflow.start()                 # screenshots: scaled again for the new width
        self._column = width

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_column()

    def _rescale_images(self):
        if self.source().isEmpty():
            return
        bar = self.verticalScrollBar()
        fraction = bar.value() / max(1, bar.maximum())
        self.reload()
        QTimer.singleShot(0, lambda: bar.setValue(int(round(fraction * bar.maximum()))))

    # --- look -----------------------------------------------------------------------------------
    def restyle(self):
        t = theme.tokens()
        self.setStyleSheet(f"QTextBrowser#HelpPage {{ background: {t['card']}; border: none; }}")
        self._cache.clear()
        if not self.source().isEmpty():
            self._rescale_images()               # (the same: reload where we are)

    def set_text_size(self, points: float):
        self.base_pt = points
        if not self.source().isEmpty():
            self._rescale_images()

    def show_highlights(self):
        """Mark every occurrence of the search words on the page."""
        marks = []
        if self.highlight_terms:
            t = theme.tokens()
            fmt = QTextCharFormat()
            fmt.setBackground(theme.qcolor(t["warn_fill"]))
            fmt.setForeground(theme.qcolor(t["warn_fill_text"]))
            doc = self.document()
            for term in self.highlight_terms:
                cursor = QTextCursor(doc)
                while len(marks) < 400:
                    cursor = doc.find(term, cursor)
                    if cursor.isNull():
                        break
                    mark = QTextEdit.ExtraSelection()
                    mark.cursor = cursor
                    mark.format = fmt
                    marks.append(mark)
        self.setExtraSelections(marks)


class HelpWindow(QMainWindow):
    def __init__(self):
        super().__init__(None)
        self.setObjectName("HelpWindow")
        self.setWindowTitle("SINDIE Help")
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.settings = gui_settings("SurreyIonBeam", "SINDIE_Help")
        self.browser = HelpBrowser(self)
        self.current = None
        self._index = None
        self._titles = {}
        self._tree_items = {}
        self._build()
        self._fill_tree()
        theme.theme_changed().connect(self._restyle)
        self._restyle()
        self._restore()

    # --- building -------------------------------------------------------------------------------
    def _build(self):
        bar = QToolBar("Help", self)
        bar.setObjectName("HelpToolBar")
        bar.setMovable(False)
        bar.setIconSize(QSize(16, 16))
        self.addToolBar(bar)
        self.act_back = QAction("Back", self)
        self.act_back.setShortcuts([QKeySequence(QKeySequence.StandardKey.Back), QKeySequence("Backspace")])
        self.act_back.setToolTip("Back to the previous page (Alt+Left)")
        self.act_back.triggered.connect(self.browser.backward)
        self.act_back.setEnabled(False)
        self.act_forward = QAction("Forward", self)
        self.act_forward.setShortcut(QKeySequence(QKeySequence.StandardKey.Forward))
        self.act_forward.setToolTip("Forward (Alt+Right)")
        self.act_forward.triggered.connect(self.browser.forward)
        self.act_forward.setEnabled(False)
        self.act_home = QAction("Contents", self)
        self.act_home.setShortcut(QKeySequence("Alt+Home"))
        self.act_home.setToolTip("The home page and its contents (Alt+Home)")
        self.act_home.triggered.connect(lambda: self.open_page(pages.HOME))
        for act in (self.act_back, self.act_forward, self.act_home):
            bar.addAction(act)
        self.crumb = QLabel()
        self.crumb.setObjectName("HelpCrumb")
        set_role(self.crumb, "hint")
        self.crumb.setContentsMargins(10, 0, 10, 0)
        bar.addWidget(self.crumb)
        spacer = QWidget()
        spacer.setSizePolicy(spacer.sizePolicy().Policy.Expanding, spacer.sizePolicy().Policy.Preferred)
        bar.addWidget(spacer)

        for label, keys, std, step, tip in (
                ("A−", "Ctrl+-", QKeySequence.StandardKey.ZoomOut, -1, "Smaller text (Ctrl+-)"),
                ("A+", "Ctrl+=", QKeySequence.StandardKey.ZoomIn, +1, "Larger text (Ctrl+=)")):
            act = QAction(label, self)
            act.setShortcuts([QKeySequence(keys)] + [k for k in QKeySequence.keyBindings(std)
                                                     if k != QKeySequence(keys)])
            act.setToolTip(tip)
            act.triggered.connect(lambda _=False, s=step: self._step_text_size(s))
            bar.addAction(act)
        normal = QAction(self)
        normal.setShortcut(QKeySequence("Ctrl+0"))
        normal.triggered.connect(lambda: self._step_text_size(0))
        self.addAction(normal)

        print_button = self.print_button = QToolButton()
        print_button.setObjectName("HelpMenuButton")
        print_button.setText("Print or save")
        print_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        print_menu = QMenu(print_button)
        act = print_menu.addAction("Print this page...")
        act.setShortcut(QKeySequence(QKeySequence.StandardKey.Print))
        act.triggered.connect(self.print_page)
        self.addAction(act)
        print_menu.addAction("Save this page as PDF...").triggered.connect(lambda: self.save_pdf(all_pages=False))
        print_menu.addAction("Save all of the help as PDF...").triggered.connect(lambda: self.save_pdf(all_pages=True))
        print_button.setMenu(print_menu)
        bar.addWidget(print_button)

        left = QWidget()
        left.setObjectName("HelpSide")
        lay = QVBoxLayout(left)
        lay.setContentsMargins(8, 8, 4, 8)
        lay.setSpacing(6)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search the help")
        self.search.setClearButtonEnabled(True)
        self._search_icon = self.search.addAction(theme.icon("search"), QLineEdit.ActionPosition.LeadingPosition)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self._run_search)
        self.search.textChanged.connect(lambda _t: self._search_timer.start())
        self.search.returnPressed.connect(self._open_first_result)
        find = QAction(self)
        find.setShortcut(QKeySequence(QKeySequence.StandardKey.Find))
        find.triggered.connect(lambda: (self.search.setFocus(), self.search.selectAll()))
        self.addAction(find)
        lay.addWidget(self.search)

        self.stack = QStackedWidget()
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(14)
        self.tree.setUniformRowHeights(True)
        self.tree.itemClicked.connect(self._tree_clicked)
        self.tree.itemActivated.connect(self._tree_clicked)
        self.stack.addWidget(self.tree)
        results = QWidget()
        rlay = QVBoxLayout(results)
        rlay.setContentsMargins(0, 0, 0, 0)
        rlay.setSpacing(4)
        self.results_label = QLabel()
        set_role(self.results_label, "hint")
        self.results = QListWidget()
        self.results.setWordWrap(True)
        self.results.itemClicked.connect(self._result_clicked)
        self.results.itemActivated.connect(self._result_clicked)
        rlay.addWidget(self.results_label)
        rlay.addWidget(self.results)
        self.stack.addWidget(results)
        lay.addWidget(self.stack)

        self.split = QSplitter(Qt.Orientation.Horizontal)
        self.split.addWidget(left)
        self.split.addWidget(self.browser)
        self.split.setStretchFactor(0, 0)
        self.split.setStretchFactor(1, 1)
        self.split.setChildrenCollapsible(False)
        left.setMinimumWidth(180)
        self.setCentralWidget(self.split)

        self.browser.sourceChanged.connect(self._page_changed)
        self.browser.backwardAvailable.connect(self.act_back.setEnabled)
        self.browser.forwardAvailable.connect(self.act_forward.setEnabled)

    def _fill_tree(self):
        self.tree.clear()
        self._tree_items = {}
        home = QTreeWidgetItem(["Home"])
        home.setData(0, Qt.ItemDataRole.UserRole, pages.HOME)
        self.tree.addTopLevelItem(home)
        self._tree_items[pages.HOME] = home

        def add(entry, parent):
            item = QTreeWidgetItem([entry.title])
            item.setToolTip(0, entry.title)
            if entry.page:
                item.setData(0, Qt.ItemDataRole.UserRole, entry.page)
                self._tree_items.setdefault(entry.page, item)
            else:
                font = item.font(0)
                font.setBold(True)
                item.setFont(0, font)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            if parent is None:
                self.tree.addTopLevelItem(item)
            else:
                parent.addChild(item)
            for child in entry.children:
                add(child, item)

        try:
            for root in pages.contents():
                add(root, None)
        except OSError:
            log.exception("Help: the contents could not be read")
        self.tree.expandAll()

    # --- pages ----------------------------------------------------------------------------------
    def open_page(self, page: str = None, anchor: str = None, highlight=None):
        page = page or pages.HOME
        path = pages.path_of(page)
        if not os.path.exists(path):
            log.warning("Help page %s not found", page)
            path = pages.path_of(pages.HOME)
        self.browser.highlight_terms = list(highlight or [])
        url = QUrl.fromLocalFile(path)
        if anchor:
            url.setFragment(anchor)
        if url == self.browser.source():
            self.browser.show_highlights()
            if anchor:
                self.browser.scrollToAnchor(anchor)
        else:
            self.browser.setSource(url)

    def title_of(self, page: str) -> str:
        if page not in self._titles:
            try:
                self._titles[page] = render.title_of(pages.read(page), page)
            except OSError:
                self._titles[page] = page
        return self._titles[page]

    def _page_changed(self, url: QUrl):
        if not url.isLocalFile():
            return
        page = pages.relative(url.toLocalFile())
        self.current = page
        item = self._tree_items.get(page)
        self.tree.blockSignals(True)
        if item is not None:
            self.tree.setCurrentItem(item)
            self.tree.scrollToItem(item)
        else:
            self.tree.clearSelection()
        self.tree.blockSignals(False)
        title = self.title_of(page)
        group = pages.group_of(page)
        self.crumb.setText(f"{group}  ›  {title}" if group else title)
        self.setWindowTitle(f"{title} - SINDIE Help")

    def _tree_clicked(self, item, _column=0):
        page = item.data(0, Qt.ItemDataRole.UserRole)
        if page:
            self.open_page(page)
        else:
            item.setExpanded(not item.isExpanded())

    # --- search ---------------------------------------------------------------------------------
    def _build_index(self):
        index = []
        for page in pages.all_pages():
            try:
                text = pages.read(page)
            except OSError:
                continue
            title = render.title_of(text, page)
            self._titles[page] = title
            for heading, anchor, body in render.sections(text):
                index.append((page, title, heading, anchor, body, title.lower(), heading.lower(), body.lower()))
        self._index = index

    def search_hits(self, query: str, limit: int = 60):
        """[(page, title, heading, anchor, snippet html)] for every section holding all the words."""
        if self._index is None:
            self._build_index()
        terms = [t for t in query.lower().split() if t]
        if not terms:
            return []
        hits = []
        for page, title, heading, anchor, body, ltitle, lhead, lbody in self._index:
            if all(t in ltitle or t in lhead or t in lbody for t in terms):
                score = sum(6 * ltitle.count(t) + 4 * lhead.count(t) + min(lbody.count(t), 8) for t in terms)
                hits.append((score, page, title, heading, anchor, body))
        hits.sort(key=lambda h: (-h[0], h[2], h[3]))
        return [(page, title, heading, anchor, _snippet(body, terms)) for _s, page, title, heading, anchor, body
                in hits[:limit]]

    def _run_search(self):
        query = self.search.text().strip()
        if not query:
            self.stack.setCurrentIndex(0)
            if self.browser.highlight_terms:
                self.browser.highlight_terms = []
                self.browser.show_highlights()
            return
        self.stack.setCurrentIndex(1)
        self.results.clear()
        hits = self.search_hits(query)
        self.results_label.setText(f"{len(hits)} result{'s' if len(hits) != 1 else ''}"
                                   + (" (the first 60)" if len(hits) >= 60 else "") if hits
                                   else "Nothing found. Try fewer or shorter words.")
        muted = theme.tokens()["text_muted"]
        for page, title, heading, anchor, snippet in hits:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, (page, anchor, query.lower().split()))
            where = html.escape(heading) if heading and heading != title else ""
            label = QLabel(f"<b>{html.escape(title)}</b>"
                           + (f" <span style='color:{muted}'>› {where}</span>" if where else "")
                           + (f"<br><span style='color:{muted}'>{snippet}</span>" if snippet else ""))
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.RichText)
            label.setContentsMargins(6, 5, 6, 5)
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self.results.addItem(item)
            self.results.setItemWidget(item, label)
            item.setSizeHint(QSize(100, label.sizeHint().height() + 4))
        self.results.setCurrentRow(-1)

    def _result_clicked(self, item):
        page, anchor, terms = item.data(Qt.ItemDataRole.UserRole)
        self.open_page(page, anchor or None, highlight=terms)

    def _open_first_result(self):
        self._search_timer.stop()
        self._run_search()
        if self.results.count():
            self._result_clicked(self.results.item(0))

    # --- printing -------------------------------------------------------------------------------
    def print_page(self):
        from PyQt6.QtPrintSupport import QPrintDialog, QPrinter
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        if QPrintDialog(printer, self).exec():
            print_document([self.current or pages.HOME]).print(printer)

    def save_pdf(self, all_pages: bool = False, path: str = None):
        chosen = pages.contents_pages() if all_pages else [self.current or pages.HOME]
        if path is None:
            name = "SINDIE help.pdf" if all_pages else f"SINDIE help - {self.title_of(chosen[0])}.pdf"
            folder = self.settings.value("pdf_folder", os.path.expanduser("~"))
            path, _ = QFileDialog.getSaveFileName(self, "Save as PDF", os.path.join(folder, name), "PDF (*.pdf)")
            if not path:
                return None
            self.settings.setValue("pdf_folder", os.path.dirname(path))
        try:
            write_pdf(chosen, path)
        except Exception as e:
            log.exception("Help: PDF not written")
            QMessageBox.warning(self, "Save as PDF", f"The PDF could not be written:\n{e}")
            return None
        return path

    # --- look and settings ------------------------------------------------------------------------
    def _restyle(self, _mode=None):
        self.act_back.setIcon(theme.icon("back", "text"))
        self.act_forward.setIcon(theme.icon("forward", "text"))
        self.act_home.setIcon(theme.icon("home", "text"))
        self.search.removeAction(self._search_icon)
        self._search_icon = self.search.addAction(theme.icon("search"), QLineEdit.ActionPosition.LeadingPosition)
        self.print_button.setStyleSheet(
            "QToolButton#HelpMenuButton { padding-right: 18px; }"
            f"QToolButton#HelpMenuButton::menu-indicator {{ image: url({theme.icon_file('down')}); "
            "subcontrol-origin: padding; subcontrol-position: center right; right: 4px; width: 10px; height: 10px; }")
        self.browser.restyle()
        if self.stack.currentIndex() == 1:
            self._run_search()                   # (the results' colours)

    def _step_text_size(self, step: int):
        sizes = TEXT_SIZES
        if step == 0:
            size = DEFAULT_TEXT_SIZE
        else:
            nearest = min(range(len(sizes)), key=lambda i: abs(sizes[i] - self.browser.base_pt))
            size = sizes[max(0, min(len(sizes) - 1, nearest + step))]
        self.browser.set_text_size(size)
        self.settings.setValue("text_size", size)

    def _restore(self):
        geometry = self.settings.value("geometry")
        if geometry is not None and not geometry.isEmpty():
            self.restoreGeometry(geometry)
        else:
            screen = QApplication.primaryScreen()
            avail = screen.availableGeometry() if screen else None
            w = min(1180, int(avail.width() * 0.8)) if avail else 1100
            h = min(860, int(avail.height() * 0.85)) if avail else 780
            self.resize(w, h)
        split = self.settings.value("splitter")
        if split is not None and not split.isEmpty():
            self.split.restoreState(split)
        else:
            self.split.setSizes([280, max(400, self.width() - 280)])
        try:
            size = float(self.settings.value("text_size", DEFAULT_TEXT_SIZE))
        except (TypeError, ValueError):
            size = DEFAULT_TEXT_SIZE
        self.browser.base_pt = size if TEXT_SIZES[0] <= size <= TEXT_SIZES[-1] else DEFAULT_TEXT_SIZE

    def closeEvent(self, event):
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("splitter", self.split.saveState())
        super().closeEvent(event)


def _snippet(body: str, terms, width: int = 70) -> str:
    low = body.lower()
    at = min((low.find(t) for t in terms if low.find(t) >= 0), default=-1)
    if at < 0:
        return html.escape(body[:2 * width] + ("..." if len(body) > 2 * width else ""))
    start = max(0, at - width)
    end = min(len(body), at + width)
    text = ("..." if start else "") + body[start:end] + ("..." if end < len(body) else "")
    out = html.escape(text)
    for t in sorted(set(terms), key=len, reverse=True):
        out = re.sub(re.escape(html.escape(t)), lambda m: f"<b>{m.group(0)}</b>", out, flags=re.IGNORECASE)
    return out


# --- Printing and PDF ----------------------------------------------------------------------------
_IMG_SRC = re.compile(r'<img([^>]*?)src="([^"]+)"([^>]*)>')


def print_document(page_list) -> QTextDocument:
    """The pages as one printable document, in the light theme, each page starting on a new
    sheet, screenshots at most a page wide."""
    tokens = theme.tokens("light")
    fonts = theme.fonts()
    parts = []
    for i, page in enumerate(page_list):
        try:
            body = render.body_html(pages.read(page), tokens) if render.available() \
                else f"<pre>{html.escape(pages.read(page))}</pre>"
        except OSError:
            continue
        base = os.path.dirname(pages.path_of(page))
        body = _IMG_SRC.sub(lambda m: _print_image(m, base), body)
        if i:
            body = body.replace("<h1", '<h1 style="page-break-before: always;"', 1)
        parts.append(body)
    doc = QTextDocument()
    doc.setDefaultFont(QFont(fonts["ui"], 10))
    doc.setHtml(render.document("".join(parts), tokens, fonts["ui"], fonts["mono"], 10.0))
    return doc


def _print_image(m, base: str) -> str:
    src = m.group(2)
    path = src if os.path.isabs(src) else os.path.normpath(os.path.join(base, src))
    size = QImageReader(path).size()
    width = min(size.width(), PRINT_IMAGE_WIDTH) if size.isValid() else PRINT_IMAGE_WIDTH
    attrs = re.sub(r'\s(width|height)="[^"]*"', "", m.group(1) + m.group(3))
    return f'<img{attrs} src="{QUrl.fromLocalFile(path).toString()}" width="{width}">'


def write_pdf(page_list, path: str):
    from PyQt6.QtPrintSupport import QPrinter
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(path)
    printer.setPageLayout(QPageLayout(QPageSize(QPageSize.PageSizeId.A4), QPageLayout.Orientation.Portrait,
                                      QMarginsF(15, 15, 15, 15), QPageLayout.Unit.Millimeter))
    printer.setDocName(os.path.splitext(os.path.basename(path))[0])
    print_document(page_list).print(printer)


# --- Opening the window ----------------------------------------------------------------------------
_window = {"w": None}


def help_window() -> HelpWindow:
    w = _window["w"]
    if w is None or sip.isdeleted(w):
        w = HelpWindow()
        _window["w"] = w
    return w


def show_help(page: str = None, anchor: str = None) -> HelpWindow:
    """Open the help at a page (relative to docs/, e.g. 'panels/vacuum.md') and bring it to the
    front."""
    w = help_window()
    w.open_page(page or pages.HOME, anchor)
    if w.isMinimized():
        w.showNormal()
    w.show()
    w.raise_()
    w.activateWindow()
    return w


def context_page(window, default: str = pages.HOME, widget=None) -> str:
    """The page for `widget`, else what is under the mouse pointer in `window`, else what has
    the keyboard focus: the page of the panel it is in, or `default`."""
    if widget is None:
        widget = QApplication.widgetAt(QCursor.pos())
        if widget is None or widget.window() is not window:
            widget = QApplication.focusWidget()
    while widget is not None:
        page = pages.PANEL_PAGES.get(widget.objectName())
        if page and (isinstance(widget, QDockWidget) or widget.objectName() == "AutomationBar"):
            return page
        widget = widget.parentWidget()
    return default


def context_help(window, default: str = pages.HOME) -> HelpWindow:
    return show_help(context_page(window, default))


def add_help_menu(window, menubar, default: str = pages.HOME, entries=None, label: str = None):
    """The Help menu: its first item opens `default` (named `label`); `entries` are further
    (label, page) items; About at the end. F1, pressed anywhere in the window, opens the page of
    the panel under the mouse pointer (else `default`). It is the window's own shortcut, not the
    menu item's: chosen from the menu, the pointer is on the menu, not on a panel."""
    from src.gui.help.about import show_about
    menu = menubar.addMenu("Help")
    act = QAction((label or ("Help contents" if default == pages.HOME else "Help")) + "\tF1", window)
    act.triggered.connect(lambda: show_help(default))
    menu.addAction(act)
    f1 = QAction(window)
    f1.setObjectName("HelpF1")
    f1.setShortcut(QKeySequence("F1"))
    f1.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
    f1.triggered.connect(lambda: context_help(window, default))
    window.addAction(f1)
    window._help_f1 = f1
    if default != pages.HOME:
        menu.addAction("Help contents").triggered.connect(lambda: show_help(pages.HOME))
    for text, page in entries or ():
        if text is None:
            menu.addSeparator()
            continue
        menu.addAction(text).triggered.connect(lambda _=False, p=page: show_help(p))
    menu.addSeparator()
    menu.addAction("About SINDIE").triggered.connect(lambda: show_about(window))
    window._help_menu = menu
    return menu
