import os
import json
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QTreeWidget, QTreeWidgetItem,
    QDialogButtonBox, QMessageBox
)
from PyQt6.QtCore import Qt


class TagSelectorDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ISA-95 Tag Selector")
        self.resize(500, 600)

        layout = QVBoxLayout(self)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["ISA-95 Hierarchy / Tag", "Description"])
        self.tree.setColumnWidth(0, 300)
        layout.addWidget(self.tree)

        self.tree.itemDoubleClicked.connect(self._on_double_click)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.selected_tag = None
        self._populate_tree()

    def _populate_tree(self):
        registry_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../config/system_tags.json'))
        try:
            with open(registry_path, "r", encoding="utf-8") as f:
                registry = json.load(f)
        except FileNotFoundError:
            QMessageBox.warning(self, "Error", f"Could not find registry at {registry_path}")
            return

        folder_nodes = {}
        for full_tag, metadata in registry.items():
            parts = full_tag.split('.')
            parent_node = self.tree.invisibleRootItem()
            current_path = ""

            # Build folders
            for part in parts[:-1]:
                current_path = f"{current_path}.{part}" if current_path else part
                if current_path not in folder_nodes:
                    node = QTreeWidgetItem(parent_node)
                    node.setText(0, part.replace('_', ' ').title())
                    node.setIcon(0, self.style().standardIcon(self.style().StandardPixmap.SP_DirIcon))
                    folder_nodes[current_path] = node
                parent_node = folder_nodes[current_path]

            # Build leaf node
            item = QTreeWidgetItem(parent_node)
            item.setText(0, parts[-1])
            item.setText(1, metadata.get("description", ""))
            item.setData(0, Qt.ItemDataRole.UserRole, full_tag)

        self.tree.sortItems(0, Qt.SortOrder.AscendingOrder)

    def _on_double_click(self, item, column):
        tag = item.data(0, Qt.ItemDataRole.UserRole)
        if tag:  # It's a leaf node
            self.selected_tag = tag
            self.accept()

    def get_selected_tag(self):
        if self.selected_tag:
            return self.selected_tag

        # Fallback for single click + OK button
        items = self.tree.selectedItems()
        if items:
            return items[0].data(0, Qt.ItemDataRole.UserRole)
        return None