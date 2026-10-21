"""The recipe builder's list of steps, drawn as a flow: a node for each step on a line from the
first step to the last (its icon says what kind of step it is: src/gui/control/dialogs/
recipe_builder/look.py), the steps of a "Do steps at the same time" step branching off it; beside
each node the step's number, the step as a sentence, its note, and a mark for its problems. Steps
can be dragged: onto a "Do steps at the same time" step to go inside it, between steps to move
there. The list never moves anything itself: it says what was asked (moved) and the builder
changes the recipe and draws the list again.

While a draft is shown, the list shows its changes: + a step it adds, − one it removes (struck
through), ~ one it changes (with what it was under it). A step added but left out is drawn faint,
and a change left out shows the step as it stays, with the change under it; a click on a mark puts
the change back or leaves it out (change_clicked). A review's note on a step is drawn under it."""
from dataclasses import dataclass
from typing import Optional

from PyQt6.QtCore import QPointF, QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QFontMetrics, QPainter, QPen
from PyQt6.QtWidgets import (QAbstractItemView, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTreeWidget,
                             QTreeWidgetItem)

from src.gui import theme
from src.gui.control.dialogs.recipe_builder import look

(PATH, NUMBER, TEXT, NOTE, LEVEL, KIND, MARK, CHANGE, CHANGE_ID, KEPT, WAS, REVIEW,
 REVIEW_LEVEL, BLOCK) = (Qt.ItemDataRole.UserRole + i for i in range(14))
PAD = 6
GLYPH_W = 18
NODE, NODE_SM = 24, 20                  # a step's node; one inside "Do steps at the same time"
NODE_COL, NODE_COL_SM = 34, 28          # the column it sits in
ELBOW = 10                              # a branch's length left of a child step's row
RAIL_W = 2.0
GLYPHS = {"add": "+", "remove": "−", "change": "~"}
CHANGE_TIPS = {"add": "added by the draft", "remove": "removed by the draft", "change": "changed by the draft"}


@dataclass
class OutlineRow:
    path: tuple
    number: str
    text: str
    note: str = ""
    level: str = ""                 # "error", "warning" or ""
    mark: str = ""                  # "1 error, 2 warnings"
    kind: str = ""                  # the step type (PARALLEL steps take drops)
    change: str = ""                # "add", "remove", "change" (a draft's), or ""
    change_id: Optional[int] = None
    kept: bool = True               # the change is ticked
    was: str = ""                   # a changed step: what it was ("was: ..."), or the change left out
    review: str = ""                # a review's note on the step
    review_level: str = ""          # "concern", "suggestion", "question"
    block: str = ""                 # the block it is (blocks.py: its icon); "" for a type the builder doesn't know


class _Geometry:
    """Where a row's parts go."""

    def __init__(self, option, index, changes_shown):
        self.fm = QFontMetrics(option.font)
        self.rect = option.rect.adjusted(PAD, PAD // 2 + 1, -PAD, -(PAD // 2 + 1))
        self.child = index.parent().isValid()
        self.glyph_w = GLYPH_W if changes_shown and not self.child else 0
        self.node_w, self.node = (NODE_COL_SM, NODE_SM) if self.child else (NODE_COL, NODE)
        self.num_w = self.fm.horizontalAdvance("00.00") + 6
        mark = index.data(MARK) or ""
        self.mark_w = self.fm.horizontalAdvance(mark) + 18 if mark else 0
        self.text_w = max(60, self.rect.width() - self.glyph_w - self.node_w - self.num_w - self.mark_w)
        self.lead = max(0, (self.node - self.fm.height()) // 2)     # the first line level with the node's centre
        self.node_x = self.rect.left() + self.glyph_w + self.node_w / 2
        self.node_y = self.rect.top() + self.node / 2


class _StepDelegate(QStyledItemDelegate):
    """Change mark, the flow's line and the step's node, number, sentence (wrapped), note, was,
    review (muted) and a problem mark."""

    def _geometry(self, option, index):
        view = self.parent()
        return _Geometry(option, index, view is not None and getattr(view, "changes_shown", False))

    @staticmethod
    def _extra_lines(index):
        lines = []
        note = index.data(NOTE) or ""
        if note:
            lines.append(("note", note))
        was = index.data(WAS) or ""
        if was:
            lines.append(("was", was))
        review = index.data(REVIEW) or ""
        if review:
            lines.append(("review", f"Review: {review}"))
        return lines

    def sizeHint(self, option, index):
        view = self.parent()
        width = option.rect.width()
        if width <= 0:                            # (the view doesn't always say: its own width, then)
            width = view.viewport().width() if view is not None else 400
        depth, parent = 0, index.parent()
        while parent.isValid():
            depth, parent = depth + 1, parent.parent()
        width = max(120, width - depth * (view.indentation() if view is not None else 18))
        option = QStyleOptionViewItem(option)
        option.rect = QRect(0, 0, width, 0)
        g = self._geometry(option, index)
        flags = int(Qt.TextFlag.TextWordWrap)
        h = g.lead + g.fm.boundingRect(QRect(0, 0, g.text_w, 10000), flags, index.data(TEXT) or "").height()
        for _kind, line in self._extra_lines(index):
            h += g.fm.boundingRect(QRect(0, 0, g.text_w, 10000), flags, line).height() + 2
        return QSize(width, max(h, g.node) + PAD + 4)

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        style = opt.widget.style() if opt.widget is not None else None
        change, kept = index.data(CHANGE) or "", index.data(KEPT) is not False
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if change and kept and change != "remove" and not selected:
            painter.fillRect(option.rect, theme.color("info_bg"))
        if style is not None:
            style.drawPrimitive(QStyle.PrimitiveElement.PE_PanelItemViewItem, opt, painter, opt.widget)
        g = self._geometry(option, index)
        fm, rect = g.fm, g.rect
        text_colour = option.palette.highlightedText().color() if selected else theme.color("text")
        muted = option.palette.highlightedText().color() if selected else theme.color("text_muted")
        faint = option.palette.highlightedText().color() if selected else theme.color("text_faint")
        # a step that won't be in the recipe is drawn faint: one the draft removes, or adds but is left out
        # (a change left out shows the step as it stays, and the change under it)
        gone = (change == "remove" and kept) or (change == "add" and not kept)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont(option.font)
        flags = Qt.TextFlag.TextWordWrap
        x = rect.left()
        if g.glyph_w and change:
            bold = QFont(option.font)
            bold.setBold(True)
            painter.setFont(bold)
            painter.setPen(theme.color("info") if kept and not selected else
                           (text_colour if selected else faint))
            painter.drawText(QRect(x, rect.top() + g.lead, g.glyph_w, fm.height()),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop), GLYPHS.get(change, ""))
        self._paint_flow(painter, option, index, g, gone)
        x += g.glyph_w + g.node_w
        painter.setFont(font)
        painter.setPen(muted)
        painter.drawText(QRect(x, rect.top() + g.lead, g.num_w, fm.height()),
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop), index.data(NUMBER) or "")
        x += g.num_w
        text = index.data(TEXT) or ""
        struck = QFont(font)
        struck.setStrikeOut(change == "remove" and kept)
        painter.setFont(struck)
        painter.setPen(faint if gone else text_colour)
        top = rect.top() + g.lead
        box = fm.boundingRect(QRect(0, 0, g.text_w, 10000), int(flags), text)
        painter.drawText(QRect(x, top, g.text_w, box.height()), int(flags | Qt.AlignmentFlag.AlignLeft), text)
        painter.setFont(font)
        y = top + box.height() + 2
        for kind, line in self._extra_lines(index):
            if kind == "review":
                level = index.data(REVIEW_LEVEL) or ""
                painter.setPen(text_colour if selected else theme.color("warn" if level == "concern" else "info"))
            else:
                painter.setPen(faint if (gone or kind == "was") else muted)
            lbox = fm.boundingRect(QRect(0, 0, g.text_w, 10000), int(flags), line)
            painter.drawText(QRect(x, y, g.text_w, lbox.height()), int(flags | Qt.AlignmentFlag.AlignLeft), line)
            y += lbox.height() + 2
        mark = index.data(MARK) or ""
        if mark:
            level = index.data(LEVEL)
            colour = theme.color("fault" if level == "error" else "warn")
            fill = theme.color("fault_fill" if level == "error" else "warn_fill")
            mx = rect.right() - g.mark_w + 6
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            r = max(3, fm.height() // 5)
            painter.drawEllipse(mx, top + fm.height() // 2 - r, 2 * r, 2 * r)
            painter.setPen(text_colour if selected else colour)
            painter.drawText(QRect(mx + 2 * r + 4, top, g.mark_w, fm.height()),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop), mark)
        painter.restore()

    @staticmethod
    def _paint_flow(painter, option, index, g, gone):
        """The line through the steps and this step's node: a step's line runs on to the next step,
        and down the branches of its steps at the same time (Outline.drawBranches draws those)."""
        model = index.model()
        pen = QPen(theme.color("border_strong"), RAIL_W)
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        painter.setPen(pen)
        cx, cy, r = g.node_x, g.node_y, g.node / 2
        if g.child:                         # the rest of the branch, from the edge of the row to the node
            painter.drawLine(QPointF(option.rect.left(), cy), QPointF(cx - r, cy))
        else:
            if index.row() > 0:
                painter.drawLine(QPointF(cx, option.rect.top()), QPointF(cx, cy - r))
            if index.row() < model.rowCount(index.parent()) - 1 or model.rowCount(index) > 0:
                painter.drawLine(QPointF(cx, cy + r), QPointF(cx, option.rect.bottom() + 1))
        look.paint_node(painter, QPointF(cx, cy), g.node, index.data(BLOCK) or "", faint=gone)


class Outline(QTreeWidget):
    """moved(from path, to parent path, index): a step was dragged (the builder decides if it may
    go there). change_clicked(change id): the mark of a draft's change was clicked."""
    moved = pyqtSignal(tuple, tuple, int)
    change_clicked = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("RecipeOutline")
        self.setHeaderHidden(True)
        self.setColumnCount(1)
        self.setUniformRowHeights(False)
        self.setWordWrap(True)
        self.setItemDelegate(_StepDelegate(self))
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.set_draggable(True)
        self.setExpandsOnDoubleClick(False)
        self.setRootIsDecorated(False)            # (everything is shown expanded: no arrows to take room)
        self.setItemsExpandable(False)
        self.setIndentation(18)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.changes_shown = False
        self.empty_text = ""
        theme.theme_changed().connect(self.viewport().update)

    def set_draggable(self, on: bool):
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove if on else
                             QAbstractItemView.DragDropMode.NoDragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDropIndicatorShown(on)

    # ------------------------------------------------------------------ items
    def fill(self, rows):
        """rows: [OutlineRow] in order; children after their parent, with paths one longer."""
        self.blockSignals(True)
        self.clear()
        self.changes_shown = any(r.change for r in rows)
        # (a branch starts at the line through the steps, then runs right to the step's own node)
        self.setIndentation(int(PAD + (GLYPH_W if self.changes_shown else 0) + NODE_COL / 2 + ELBOW))
        items = {}
        for r in rows:
            item = QTreeWidgetItem()
            for role, value in ((PATH, r.path), (NUMBER, r.number), (TEXT, r.text), (NOTE, r.note), (LEVEL, r.level),
                                (MARK, r.mark), (KIND, r.kind), (CHANGE, r.change), (CHANGE_ID, r.change_id),
                                (KEPT, r.kept), (WAS, r.was), (REVIEW, r.review), (REVIEW_LEVEL, r.review_level),
                                (BLOCK, r.block)):
                item.setData(0, role, value)
            tip = r.text + (f"\nNote: {r.note}" if r.note else "")
            if r.was:
                tip += f"\n{r.was[:1].upper()}{r.was[1:]}"
            if r.change:
                tip += (f"\n\n{CHANGE_TIPS[r.change].capitalize()}"
                        + (" - click the mark on the left to leave it out." if r.kept else
                           " - left out: click the mark on the left to put it back."))
            if r.review:
                tip += f"\n\nReview ({r.review_level}): {r.review}"
            item.setToolTip(0, tip)
            flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled
            if r.kind == "PARALLEL":
                flags |= Qt.ItemFlag.ItemIsDropEnabled
            item.setFlags(flags)
            parent = items.get(r.path[:-1])
            if parent is None:
                self.addTopLevelItem(item)
            else:
                parent.addChild(item)
            items[r.path] = item
        self.expandAll()
        self.blockSignals(False)
        self._items = items
        self.viewport().update()

    def item_at_path(self, path):
        return getattr(self, "_items", {}).get(tuple(path)) if path is not None else None

    def select_path(self, path, quiet: bool = True):
        item = self.item_at_path(path)
        if quiet:
            self.blockSignals(True)
        if item is None:
            self.clearSelection()
            self.setCurrentItem(None)
        else:
            self.setCurrentItem(item)
            item.setSelected(True)
            self.scrollToItem(item)
        if quiet:
            self.blockSignals(False)

    def selected_path(self):
        items = self.selectedItems()
        return tuple(items[0].data(0, PATH)) if items else None

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.scheduleDelayedItemsLayout()         # the sentences wrap to the new width

    def drawBranches(self, painter, rect, index):
        """Left of a step inside "Do steps at the same time": the line through the steps, going on
        down past it, and a branch off it to the step (its delegate draws the rest of the branch)."""
        parent = index.parent()
        if not parent.isValid():
            return
        model = self.model()
        spine = self.visualRect(parent).left() + PAD + (GLYPH_W if self.changes_shown else 0) + NODE_COL / 2
        node_y = rect.top() + PAD // 2 + 1 + NODE_SM / 2
        last = index.row() == model.rowCount(parent) - 1
        more = parent.row() < model.rowCount(parent.parent()) - 1     # (a step after the branches)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(theme.color("border_strong"), RAIL_W)
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        painter.setPen(pen)
        painter.drawLine(QPointF(spine, rect.top()), QPointF(spine, rect.bottom() + 1 if (more or not last) else node_y))
        painter.drawLine(QPointF(spine, node_y), QPointF(rect.right() + 1, node_y))
        painter.restore()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.topLevelItemCount() == 0 and self.empty_text:
            from PyQt6.QtGui import QPainter
            painter = QPainter(self.viewport())
            painter.setPen(theme.color("text_faint"))
            area = self.viewport().rect().adjusted(24, 24, -24, -24)
            painter.drawText(area, int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop
                                       | Qt.TextFlag.TextWordWrap), self.empty_text)
            painter.end()

    # ------------------------------------------------------------------ a draft's marks
    def mousePressEvent(self, event):
        if self.changes_shown and event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            item = self.itemAt(pos)
            if item is not None and item.data(0, CHANGE_ID) is not None:
                rect = self.visualItemRect(item)
                if rect.left() + PAD - 2 <= pos.x() <= rect.left() + PAD + GLYPH_W:
                    self.change_clicked.emit(int(item.data(0, CHANGE_ID)))
                    event.accept()
                    return
        super().mousePressEvent(event)

    # ------------------------------------------------------------------ dragging
    def dropEvent(self, event):
        source = self.selected_path()
        target = self.itemAt(event.position().toPoint())
        where = self.dropIndicatorPosition()
        event.setDropAction(Qt.DropAction.IgnoreAction)
        event.accept()                            # the builder moves the step, then draws the list again
        if source is None:
            return
        Indicator = QAbstractItemView.DropIndicatorPosition
        if target is None or where == Indicator.OnViewport:
            parent, index = (), self.topLevelItemCount()
        else:
            path = tuple(target.data(0, PATH))
            if where == Indicator.OnItem:
                if target.data(0, KIND) == "PARALLEL":
                    parent, index = path, target.childCount()
                else:
                    parent, index = path[:-1], path[-1] + 1
            elif where == Indicator.AboveItem:
                parent, index = path[:-1], path[-1]
            else:
                parent, index = path[:-1], path[-1] + 1
        self.moved.emit(source, parent, index)
