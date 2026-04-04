from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QTreeWidget, QTreeWidgetItem,
                             QDialogButtonBox, QHBoxLayout, QPushButton, QMessageBox)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QBrush
import re
import random

from utils.config_manager import delete_config


class ChannelSelectorDialog(QDialog):
    def __init__(self, available_tags, current_config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Channel Manager")
        self.resize(900, 700)

        self.tags = available_tags
        self.config = current_config.copy()
        self.item_map = {}

        layout = QVBoxLayout(self)

        # 1. Buttons
        btn_layout = QHBoxLayout()
        self.btn_all_fav = QPushButton("Select All Favorites")
        self.btn_none_fav = QPushButton("Deselect All Favorites")
        self.btn_reset = QPushButton("💥 Factory Reset Settings")
        self.btn_reset.setStyleSheet("background-color: #ffcccc; color: red; font-weight: bold;")

        self.btn_reset.clicked.connect(self._factory_reset)
        self.btn_all_fav.clicked.connect(self._select_all_favorites)
        self.btn_none_fav.clicked.connect(self._deselect_all_favorites)

        btn_layout.addWidget(self.btn_all_fav)
        btn_layout.addWidget(self.btn_none_fav)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_reset)
        layout.addLayout(btn_layout)

        # 2. Tree
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Hierarchy / Tag", "Plot Label", "Mult.", "Scale", "Fav"])
        self.tree.setColumnWidth(0, 350)
        self.tree.setColumnWidth(1, 200)
        self.tree.setColumnWidth(2, 60)
        self.tree.setColumnWidth(3, 80)
        self.tree.setColumnWidth(4, 40)
        layout.addWidget(self.tree)

        self._populate_tree()

        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.itemClicked.connect(self._on_item_clicked)

        # 3. Dialog Buttons
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._safe_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _populate_tree(self):
        self.tree.clear()
        self.item_map = {}

        self.fav_root = QTreeWidgetItem(self.tree)
        self.fav_root.setText(0, "Favorites")
        self.fav_root.setExpanded(True)
        self.fav_root.setForeground(0, QBrush(QColor("#FFD700")))

        self.all_root = QTreeWidgetItem(self.tree)
        self.all_root.setText(0, "All System Tags")
        self.all_root.setExpanded(False)

        structure = {}
        for tag in sorted(self.tags.keys()):
            parts = tag.split('.')
            node = structure
            for part in parts:
                node = node.setdefault(part, {})

        def add_node(parent_item, data, full_path=""):
            for key, val in data.items():
                path = f"{full_path}.{key}" if full_path else key
                if not val:
                    self._create_leaf_item(parent_item, key, path)
                else:
                    folder = QTreeWidgetItem(parent_item)
                    folder.setText(0, key)
                    add_node(folder, val, path)

        self.tree.blockSignals(True)
        add_node(self.all_root, structure)
        self.tree.blockSignals(False)

    def _create_leaf_item(self, parent, name, full_tag):
        cfg = self.config.get(full_tag, {})
        if not cfg:
            is_log = "vacuum" in full_tag.lower() or "pressure" in full_tag.lower()
            label = self._generate_smart_label(full_tag)
            cfg = {'label': label, 'multiplier': 1.0, 'scale': 'log' if is_log else 'linear', 'favorite': False,
                   'selected': False}

        if full_tag not in self.item_map: self.item_map[full_tag] = []

        def setup_cols(item):
            item.setText(0, name)
            # PyQt6 Enum Updates
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Checked if cfg.get('selected') else Qt.CheckState.Unchecked)
            item.setData(0, Qt.ItemDataRole.UserRole, full_tag)

            item.setText(1, cfg.get('label', name))
            item.setText(2, str(cfg.get('multiplier', 1.0)))
            item.setText(3, cfg.get('scale', 'linear'))

            is_fav = cfg.get('favorite', False)
            item.setText(4, "★" if is_fav else "☆")
            item.setForeground(4, QBrush(QColor("orange" if is_fav else "gray")))

            self.item_map[full_tag].append(item)

        main_item = QTreeWidgetItem(parent)
        setup_cols(main_item)

        if cfg.get('favorite'):
            fav_item = QTreeWidgetItem(self.fav_root)
            setup_cols(fav_item)

    def _generate_smart_label(self, tag_name):
        unit = ""
        if tag_name.endswith("_mB"):
            unit = "mB"
        elif tag_name.endswith("readbackW"):
            unit = "W"
        elif tag_name.endswith("readbackV"):
            unit = "V"
        elif tag_name.endswith("readbackA"):
            unit = "A"
        elif tag_name.endswith("readbackC"):
            unit = "°C"
        elif tag_name.endswith("C"):
            unit = "°C"
        elif tag_name.endswith("W"):
            unit = "W"
        elif tag_name.endswith("V"):
            unit = "V"
        elif tag_name.endswith("A"):
            unit = "A"

        parts = tag_name.split('.')
        core_name = parts[-1]
        if "readback" in core_name.lower() and len(parts) > 1:
            core_name = parts[-2]

        core_name = core_name.replace("readback", "").replace("_mB", "").replace("_", " ")
        core_name = re.sub(r"(\w)([A-Z])", r"\1 \2", core_name).title().strip()

        return f"{core_name} ({unit})" if unit else core_name

    def _on_item_clicked(self, item, column):
        if column == 4:
            tag = item.data(0, Qt.ItemDataRole.UserRole)
            if not tag: return
            is_now_fav = (item.text(4) == "☆")

            if tag in self.item_map:
                for dup in self.item_map[tag]:
                    dup.setText(4, "★" if is_now_fav else "☆")
                    dup.setForeground(4, QBrush(QColor("orange" if is_now_fav else "gray")))

            if is_now_fav:
                self._add_to_favorites_view(tag, item)
            else:
                self._remove_from_favorites_view(tag)

    def _add_to_favorites_view(self, tag, source_item):
        for item in self.item_map.get(tag, []):
            if item.parent() is self.fav_root: return
        new_item = QTreeWidgetItem(self.fav_root)
        new_item.setFlags(source_item.flags())
        for c in range(5):
            new_item.setText(c, source_item.text(c))
            new_item.setForeground(c, source_item.foreground(c))
        new_item.setData(0, Qt.ItemDataRole.UserRole, tag)
        new_item.setCheckState(0, source_item.checkState(0))
        self.item_map[tag].append(new_item)
        self.fav_root.setExpanded(True)

    def _remove_from_favorites_view(self, tag):
        items = self.item_map.get(tag, [])
        target = next((item for item in items if item.parent() is self.fav_root), None)
        if target:
            items.remove(target)
            self.fav_root.removeChild(target)

    def _on_item_changed(self, item, column):
        tag = item.data(0, Qt.ItemDataRole.UserRole)
        if not tag or tag not in self.item_map: return
        self.tree.blockSignals(True)
        for dup in self.item_map[tag]:
            if dup is item: continue
            if column == 0:
                dup.setCheckState(0, item.checkState(0))
            elif column == 1:
                dup.setText(1, item.text(1))
            elif column == 2:
                dup.setText(2, item.text(2))
        self.tree.blockSignals(False)

    def _select_all_favorites(self):
        self.tree.blockSignals(True)
        for i in range(self.fav_root.childCount()):
            item = self.fav_root.child(i)
            item.setCheckState(0, Qt.CheckState.Checked)
            tag = item.data(0, Qt.ItemDataRole.UserRole)
            if tag in self.item_map:
                for dup in self.item_map[tag]: dup.setCheckState(0, Qt.CheckState.Checked)
        self.tree.blockSignals(False)

    def _deselect_all_favorites(self):
        self.tree.blockSignals(True)
        for i in range(self.fav_root.childCount()):
            item = self.fav_root.child(i)
            item.setCheckState(0, Qt.CheckState.Unchecked)
            tag = item.data(0, Qt.ItemDataRole.UserRole)
            if tag in self.item_map:
                for dup in self.item_map[tag]: dup.setCheckState(0, Qt.CheckState.Unchecked)
        self.tree.blockSignals(False)

    def _factory_reset(self):
        ret = QMessageBox.question(self, "Reset", "Wipe all settings?",
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ret == QMessageBox.StandardButton.Yes:
            delete_config()
            self.config = {}
            self._populate_tree()

    def _safe_accept(self):
        count = sum(1 for items in self.item_map.values() if items and items[0].checkState(0) == Qt.CheckState.Checked)
        if count > 20:
            QMessageBox.critical(self, "Too Many Channels", f"Selected: {count}\nLimit: 20")
            return
        self.accept()

    def _get_persistent_color(self, tag_name):
        import zlib
        palette = [
            '#1f77b4', '#aec7e8', '#ff7f0e', '#ffbb78', '#2ca02c', '#98df8a',
            '#d62728', '#ff9896', '#9467bd', '#c5b0d5', '#8c564b', '#c49c94',
            '#e377c2', '#f7b6d2', '#7f7f7f', '#c7c7c7', '#bcbd22', '#dbdb8d',
            '#17becf', '#9edae5'
        ]
        idx = zlib.adler32(tag_name.encode('utf-8')) % len(palette)
        return palette[idx]

    def get_selection(self):
        final_config = {}
        for tag, items in self.item_map.items():
            if not items: continue
            item = items[0]
            is_selected = (item.checkState(0) == Qt.CheckState.Checked)
            is_fav = (item.text(4) == "★")

            # --- FIXED COLOR ASSIGNMENT ---
            old_color = self.config.get(tag, {}).get('color', None)
            if not old_color:
                old_color = self._get_persistent_color(tag)

            final_config[tag] = {
                "dataSource": tag,
                "label": item.text(1),
                "multiplier": float(item.text(2)) if item.text(2) else 1.0,
                "scale": item.text(3),
                "favorite": is_fav,
                "selected": is_selected,
                "color": old_color
            }
        return final_config