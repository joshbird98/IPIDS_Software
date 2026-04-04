from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QTreeWidget, QTreeWidgetItem,
                             QDialogButtonBox, QHBoxLayout, QPushButton, QMessageBox)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QBrush
import re
import random

# Import config manager
from utils.config_manager import delete_config


class ChannelSelectorDialog(QDialog):
    def __init__(self, available_tags, current_config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Channel Manager")
        self.resize(900, 700)

        self.tags = available_tags
        self.config = current_config.copy()

        # Map: tag -> [item_in_fav, item_in_all]
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

        # Signals
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.itemClicked.connect(self._on_item_clicked)

        # 3. Dialog Buttons
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
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

        # Helper to set visuals
        def setup_cols(item):
            item.setText(0, name)
            item.setFlags(item.flags() | Qt.ItemIsEditable | Qt.ItemIsUserCheckable)
            item.setCheckState(0, Qt.Checked if cfg.get('selected') else Qt.Unchecked)
            item.setData(0, Qt.UserRole, full_tag)

            item.setText(1, cfg.get('label', name))
            item.setText(2, str(cfg.get('multiplier', 1.0)))
            item.setText(3, cfg.get('scale', 'linear'))

            is_fav = cfg.get('favorite', False)
            item.setText(4, "★" if is_fav else "☆")
            item.setForeground(4, QBrush(QColor("orange" if is_fav else "gray")))

            self.item_map[full_tag].append(item)

        # Always create in All Tags
        main_item = QTreeWidgetItem(parent)
        setup_cols(main_item)

        # Create in Favorites if needed
        if cfg.get('favorite'):
            fav_item = QTreeWidgetItem(self.fav_root)
            setup_cols(fav_item)

    def _generate_smart_label(self, tag_name):
        """
        Smartly generates a label like 'Filament (W)' from '...filament.readbackW'.
        """
        # 1. Detect Unit Suffix
        unit = ""
        # Check specific suffixes first (longer matches first)
        if tag_name.endswith("_mB"):
            unit = "mB"
        elif tag_name.endswith("readbackW"):
            unit = "W"  # Special case for readbackW
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

        # 2. Identify the "Core Name"
        # Split tag by dots to find parents
        parts = tag_name.split('.')

        # Default: Use the last part (e.g. 'pressure')
        core_name = parts[-1]

        # IMPROVEMENT: If last part is generic 'readback', use the Parent
        # e.g. "filament.readbackW" -> use "filament"
        if "readback" in core_name.lower() and len(parts) > 1:
            core_name = parts[-2]

        # 3. Clean up the Name
        # Remove suffixes like _mB, readback, etc if they are still attached
        core_name = core_name.replace("readback", "").replace("_mB", "")

        # Snake_case to Title Case (ion_source -> Ion Source)
        core_name = core_name.replace("_", " ")

        # CamelCase to Title Case (ionSource -> Ion Source)
        core_name = re.sub(r"(\w)([A-Z])", r"\1 \2", core_name)

        # Capitalize first letters
        core_name = core_name.title().strip()

        # 4. Return Final Format
        if unit:
            return f"{core_name} ({unit})"
        else:
            return core_name

    # --- DYNAMIC FAVORITES LOGIC (NEW) ---

    def _on_item_clicked(self, item, column):
        """Handles Star Clicking with Live Updates."""
        if column == 4:  # Star Column
            tag = item.data(0, Qt.UserRole)
            if not tag: return

            # 1. Determine New State (Toggle)
            txt = item.text(4)
            is_now_fav = (txt == "☆")

            # 2. Visually Update ALL instances immediately
            # (So both the Favorites list and Main list update their stars)
            if tag in self.item_map:
                for dup in self.item_map[tag]:
                    dup.setText(4, "★" if is_now_fav else "☆")
                    dup.setForeground(4, QBrush(QColor("orange" if is_now_fav else "gray")))

            # 3. Modify the Tree Structure Live
            if is_now_fav:
                self._add_to_favorites_view(tag, item)
            else:
                self._remove_from_favorites_view(tag)

    def _add_to_favorites_view(self, tag, source_item):
        """Creates a clone in the Favorites folder if one doesn't exist."""
        # Check if already there
        for item in self.item_map.get(tag, []):
            if item.parent() is self.fav_root:
                return  # Already exists

        # Create Clone
        new_item = QTreeWidgetItem(self.fav_root)
        new_item.setFlags(source_item.flags())

        # Copy Columns
        for c in range(5):
            new_item.setText(c, source_item.text(c))
            new_item.setForeground(c, source_item.foreground(c))

        # Copy Data & State
        new_item.setData(0, Qt.UserRole, tag)
        new_item.setCheckState(0, source_item.checkState(0))

        # Register in map so sync works
        self.item_map[tag].append(new_item)
        self.fav_root.setExpanded(True)  # Optional: Auto-expand to show user

    def _remove_from_favorites_view(self, tag):
        """Finds the duplicate in Favorites and deletes it."""
        items = self.item_map.get(tag, [])

        # Find the one that lives in Favorites
        target = None
        for item in items:
            if item.parent() is self.fav_root:
                target = item
                break

        if target:
            # 1. Remove from Map
            items.remove(target)
            # 2. Remove from UI
            self.fav_root.removeChild(target)

    # --- SYNC LOGIC (Unchanged) ---
    def _on_item_changed(self, item, column):
        tag = item.data(0, Qt.UserRole)
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

    # --- ACTIONS (Fixed for "Select All" Bug) ---
    def _select_all_favorites(self):
        self.tree.blockSignals(True)
        # Iterate direct children only to prevent selecting "All Tags"
        for i in range(self.fav_root.childCount()):
            item = self.fav_root.child(i)
            item.setCheckState(0, Qt.Checked)
            # Sync
            tag = item.data(0, Qt.UserRole)
            if tag in self.item_map:
                for dup in self.item_map[tag]:
                    dup.setCheckState(0, Qt.Checked)
        self.tree.blockSignals(False)

    def _deselect_all_favorites(self):
        self.tree.blockSignals(True)
        for i in range(self.fav_root.childCount()):
            item = self.fav_root.child(i)
            item.setCheckState(0, Qt.Unchecked)
            # Sync
            tag = item.data(0, Qt.UserRole)
            if tag in self.item_map:
                for dup in self.item_map[tag]:
                    dup.setCheckState(0, Qt.Unchecked)
        self.tree.blockSignals(False)

    def _factory_reset(self):
        ret = QMessageBox.question(self, "Reset", "Wipe all settings?", QMessageBox.Yes | QMessageBox.No)
        if ret == QMessageBox.Yes:
            delete_config()
            self.config = {}
            self._populate_tree()

    def _safe_accept(self):
        count = 0
        # Use item_map to count unique selected tags
        for tag, items in self.item_map.items():
            if items and items[0].checkState(0) == Qt.Checked:
                count += 1

        if count > 20:
            QMessageBox.critical(self, "Too Many Channels", f"Selected: {count}\nLimit: 20")
            return
        self.accept()

    def get_selection(self):
        final_config = {}
        for tag, items in self.item_map.items():
            if not items: continue
            item = items[0]  # Source of truth

            is_selected = (item.checkState(0) == Qt.Checked)
            is_fav = (item.text(4) == "★")

            old_color = self.config.get(tag, {}).get('color', None)
            if not old_color:
                old_color = QColor.fromHsv(random.randint(0, 359), 200, 255).name()

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