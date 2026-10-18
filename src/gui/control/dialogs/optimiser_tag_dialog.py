"""Pick a setpoint for an optimiser axis: registry tags the automation may drive, with limits."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QTreeWidget, QTreeWidgetItem, QVBoxLayout


class OptimizerTagSelectorDialog(QDialog):
    def __init__(self, registry: dict, axis_label: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Select {axis_label}-Axis Sweep Parameter")
        self.resize(500, 500)
        self.registry = registry
        self.selected_tag = None
        self.selected_meta = None

        layout = QVBoxLayout(self)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["ISA-95 Hierarchy / Parameter", "Unit"])
        self.tree.setColumnWidth(0, 350)
        layout.addWidget(self.tree)

        self.tree.itemDoubleClicked.connect(self._on_double_click)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept_selection)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._populate_tree()

    def _populate_tree(self):
        folder_nodes = {}
        for full_tag, meta in self.registry.items():
            if not meta.get("auto_controllable") or meta.get("min_val") is None or meta.get("max_val") is None:
                continue

            parts = full_tag.split('.')
            parent_node = self.tree.invisibleRootItem()
            current_path = ""

            for part in parts[:-1]:
                current_path = f"{current_path}.{part}" if current_path else part
                if current_path not in folder_nodes:
                    node = QTreeWidgetItem(parent_node)
                    node.setText(0, part.replace('_', ' ').title())
                    folder_nodes[current_path] = node
                parent_node = folder_nodes[current_path]

            item = QTreeWidgetItem(parent_node)
            item.setText(0, meta.get("default_label", parts[-1]))
            item.setText(1, meta.get("unit", ""))
            item.setData(0, Qt.ItemDataRole.UserRole, {"tag": full_tag, "meta": meta})

        self.tree.collapseAll()
        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setExpanded(True)

    def _on_double_click(self, item, column):
        self.accept_selection()

    def accept_selection(self):
        items = self.tree.selectedItems()
        if items and items[0].data(0, Qt.ItemDataRole.UserRole):
            data = items[0].data(0, Qt.ItemDataRole.UserRole)
            self.selected_tag = data["tag"]
            self.selected_meta = data["meta"]
            self.accept()
        else:
            self.reject()
