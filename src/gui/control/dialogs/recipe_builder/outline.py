"""The recipe builder's list of steps: each step as a sentence (what it does), its note under it,
and a mark for its problems. Steps can be dragged: onto a "Do steps at the same time" step to go
inside it, between steps to move there. The list never moves anything itself: it says what was
asked (moved) and the builder changes the recipe and draws the list again."""
from PyQt6.QtCore import QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QFontMetrics
from PyQt6.QtWidgets import (QAbstractItemView, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTreeWidget,
                             QTreeWidgetItem)

from src.gui import theme

PATH, NUMBER, TEXT, NOTE, LEVEL, KIND, MARK = (Qt.ItemDataRole.UserRole + i for i in range(7))
PAD = 6


class _StepDelegate(QStyledItemDelegate):
    """Number, sentence (wrapped), note (muted) and a problem mark on the right."""

    def _layout(self, option, index):
        fm = QFontMetrics(option.font)
        rect = option.rect.adjusted(PAD, PAD // 2 + 1, -PAD, -(PAD // 2 + 1))
        num_w = fm.horizontalAdvance("00.00") + 6
        mark = index.data(MARK) or ""
        mark_w = fm.horizontalAdvance(mark) + 18 if mark else 0
        text_w = max(60, rect.width() - num_w - mark_w)
        return fm, rect, num_w, mark_w, text_w

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
        fm, rect, num_w, mark_w, text_w = self._layout(option, index)
        flags = int(Qt.TextFlag.TextWordWrap)
        h = fm.boundingRect(QRect(0, 0, text_w, 10000), flags, index.data(TEXT) or "").height()
        note = index.data(NOTE) or ""
        if note:
            h += fm.boundingRect(QRect(0, 0, text_w, 10000), flags, note).height() + 2
        return QSize(width, max(h, fm.height()) + PAD + 2)

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        style = opt.widget.style() if opt.widget is not None else None
        if style is not None:
            style.drawPrimitive(QStyle.PrimitiveElement.PE_PanelItemViewItem, opt, painter, opt.widget)
        fm, rect, num_w, mark_w, text_w = self._layout(option, index)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        text_colour = option.palette.highlightedText().color() if selected else theme.color("text")
        muted = option.palette.highlightedText().color() if selected else theme.color("text_muted")
        painter.save()
        painter.setFont(option.font)
        flags = Qt.TextFlag.TextWordWrap
        painter.setPen(muted)
        painter.drawText(QRect(rect.left(), rect.top(), num_w, fm.height()),
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop), index.data(NUMBER) or "")
        x = rect.left() + num_w
        painter.setPen(text_colour)
        text = index.data(TEXT) or ""
        box = fm.boundingRect(QRect(0, 0, text_w, 10000), int(flags), text)
        painter.drawText(QRect(x, rect.top(), text_w, box.height()), int(flags | Qt.AlignmentFlag.AlignLeft), text)
        note = index.data(NOTE) or ""
        if note:
            painter.setPen(muted)
            painter.drawText(QRect(x, rect.top() + box.height() + 2, text_w, rect.height()),
                             int(flags | Qt.AlignmentFlag.AlignLeft), note)
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
    go there)."""
    moved = pyqtSignal(tuple, tuple, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("RecipeOutline")
        self.setHeaderHidden(True)
        self.setColumnCount(1)
        self.setUniformRowHeights(False)
        self.setWordWrap(True)
        self.setItemDelegate(_StepDelegate(self))
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDropIndicatorShown(True)
        self.setExpandsOnDoubleClick(False)
        self.setRootIsDecorated(False)            # (everything is shown expanded: no arrows to take room)
        self.setItemsExpandable(False)
        self.setIndentation(18)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        theme.theme_changed().connect(self.viewport().update)

    # ------------------------------------------------------------------ items
    def fill(self, rows):
        """rows: [(path, number, text, note, level, mark, kind)] in order; children after their
        parent, with paths one longer."""
        self.blockSignals(True)
        self.clear()
        items = {}
        for path, number_text, text, note, level, mark, kind in rows:
            item = QTreeWidgetItem()
            for role, value in ((PATH, path), (NUMBER, number_text), (TEXT, text), (NOTE, note), (LEVEL, level),
                                (MARK, mark), (KIND, kind)):
                item.setData(0, role, value)
            tip = text + (f"\nNote: {note}" if note else "")
            item.setToolTip(0, tip)
            flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled
            if kind == "PARALLEL":
                flags |= Qt.ItemFlag.ItemIsDropEnabled
            item.setFlags(flags)
            parent = items.get(path[:-1])
            if parent is None:
                self.addTopLevelItem(item)
            else:
                parent.addChild(item)
            items[path] = item
        self.expandAll()
        self.blockSignals(False)
        self._items = items

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
