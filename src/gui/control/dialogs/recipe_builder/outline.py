"""The recipe builder's list of steps: each step as a sentence (what it does), its note under it,
and a mark for its problems. Steps can be dragged: onto a "Do steps at the same time" step to go
inside it, between steps to move there. The list never moves anything itself: it says what was
asked (moved) and the builder changes the recipe and draws the list again.

While a draft is shown, the list shows its changes: + a step it adds, − one it removes (struck
through), ~ one it changes (with what it was under it). A step added but left out is drawn faint,
and a change left out shows the step as it stays, with the change under it; a click on a mark puts
the change back or leaves it out (change_clicked). A review's note on a step is drawn under it."""
from dataclasses import dataclass
from typing import Optional

from PyQt6.QtCore import QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QFontMetrics
from PyQt6.QtWidgets import (QAbstractItemView, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTreeWidget,
                             QTreeWidgetItem)

from src.gui import theme

(PATH, NUMBER, TEXT, NOTE, LEVEL, KIND, MARK, CHANGE, CHANGE_ID, KEPT, WAS, REVIEW,
 REVIEW_LEVEL) = (Qt.ItemDataRole.UserRole + i for i in range(13))
PAD = 6
GLYPH_W = 18
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


class _StepDelegate(QStyledItemDelegate):
    """Change mark, number, sentence (wrapped), note, was, review (muted) and a problem mark."""

    def _layout(self, option, index):
        fm = QFontMetrics(option.font)
        rect = option.rect.adjusted(PAD, PAD // 2 + 1, -PAD, -(PAD // 2 + 1))
        view = self.parent()
        glyph_w = GLYPH_W if view is not None and getattr(view, "changes_shown", False) else 0
        num_w = fm.horizontalAdvance("00.00") + 6
        mark = index.data(MARK) or ""
        mark_w = fm.horizontalAdvance(mark) + 18 if mark else 0
        text_w = max(60, rect.width() - glyph_w - num_w - mark_w)
        return fm, rect, glyph_w, num_w, mark_w, text_w

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
        fm, rect, glyph_w, num_w, mark_w, text_w = self._layout(option, index)
        flags = int(Qt.TextFlag.TextWordWrap)
        h = fm.boundingRect(QRect(0, 0, text_w, 10000), flags, index.data(TEXT) or "").height()
        for _kind, line in self._extra_lines(index):
            h += fm.boundingRect(QRect(0, 0, text_w, 10000), flags, line).height() + 2
        return QSize(width, max(h, fm.height()) + PAD + 2)

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
        fm, rect, glyph_w, num_w, mark_w, text_w = self._layout(option, index)
        text_colour = option.palette.highlightedText().color() if selected else theme.color("text")
        muted = option.palette.highlightedText().color() if selected else theme.color("text_muted")
        faint = option.palette.highlightedText().color() if selected else theme.color("text_faint")
        # a step that won't be in the recipe is drawn faint: one the draft removes, or adds but is left out
        # (a change left out shows the step as it stays, and the change under it)
        gone = (change == "remove" and kept) or (change == "add" and not kept)
        painter.save()
        font = QFont(option.font)
        flags = Qt.TextFlag.TextWordWrap
        x = rect.left()
        if glyph_w and change:
            bold = QFont(option.font)
            bold.setBold(True)
            painter.setFont(bold)
            painter.setPen(theme.color("info") if kept and not selected else
                           (text_colour if selected else faint))
            painter.drawText(QRect(x, rect.top(), glyph_w, fm.height()),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop), GLYPHS.get(change, ""))
        x += glyph_w
        painter.setFont(font)
        painter.setPen(muted)
        painter.drawText(QRect(x, rect.top(), num_w, fm.height()),
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop), index.data(NUMBER) or "")
        x += num_w
        text = index.data(TEXT) or ""
        struck = QFont(font)
        struck.setStrikeOut(change == "remove" and kept)
        painter.setFont(struck)
        painter.setPen(faint if gone else text_colour)
        box = fm.boundingRect(QRect(0, 0, text_w, 10000), int(flags), text)
        painter.drawText(QRect(x, rect.top(), text_w, box.height()), int(flags | Qt.AlignmentFlag.AlignLeft), text)
        painter.setFont(font)
        y = rect.top() + box.height() + 2
        for kind, line in self._extra_lines(index):
            if kind == "review":
                level = index.data(REVIEW_LEVEL) or ""
                painter.setPen(text_colour if selected else theme.color("warn" if level == "concern" else "info"))
            else:
                painter.setPen(faint if (gone or kind == "was") else muted)
            lbox = fm.boundingRect(QRect(0, 0, text_w, 10000), int(flags), line)
            painter.drawText(QRect(x, y, text_w, lbox.height()), int(flags | Qt.AlignmentFlag.AlignLeft), line)
            y += lbox.height() + 2
        mark = index.data(MARK) or ""
        if mark:
            level = index.data(LEVEL)
            colour = theme.color("fault" if level == "error" else "warn")
            fill = theme.color("fault_fill" if level == "error" else "warn_fill")
            mx = rect.right() - mark_w + 6
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            r = max(3, fm.height() // 5)
            painter.drawEllipse(mx, rect.top() + fm.height() // 2 - r, 2 * r, 2 * r)
            painter.setPen(text_colour if selected else colour)
            painter.drawText(QRect(mx + 2 * r + 4, rect.top(), mark_w, fm.height()),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop), mark)
        painter.restore()


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
        items = {}
        for r in rows:
            item = QTreeWidgetItem()
            for role, value in ((PATH, r.path), (NUMBER, r.number), (TEXT, r.text), (NOTE, r.note), (LEVEL, r.level),
                                (MARK, r.mark), (KIND, r.kind), (CHANGE, r.change), (CHANGE_ID, r.change_id),
                                (KEPT, r.kept), (WAS, r.was), (REVIEW, r.review), (REVIEW_LEVEL, r.review_level)):
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
