from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QTreeWidget, QTreeWidgetItem,
    QDialogButtonBox, QHBoxLayout, QPushButton, QMessageBox,
    QLabel, QComboBox
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor
import zlib
import os
import json

from utils.config_manager import delete_config


class ChannelSelectorDialog(QDialog):
    def __init__(self, available_tags, current_config, system_registry, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Channel Manager")
        self.resize(900, 700)

        self.tags = available_tags
        self.config = current_config.copy()
        self.registry = system_registry  # Store the master hardware registry
        self.item_map = {}

        layout = QVBoxLayout(self)

        # 1. Buttons
        btn_layout = QHBoxLayout()
        self.btn_all_fav = QPushButton("Select All Favourites")
        self.btn_none_fav = QPushButton("Deselect All Favourites")
        self.btn_deselect_all = QPushButton("Deselect All Channels")
        self.btn_reset = QPushButton("💥 Factory Reset Settings")
        self.btn_reset.setStyleSheet("background-color: #ffcccc; color: red; font-weight: bold;")

        self.btn_reset.clicked.connect(self._factory_reset)
        self.btn_all_fav.clicked.connect(self._select_all_favourites)
        self.btn_none_fav.clicked.connect(self._deselect_all_favourites)
        self.btn_deselect_all.clicked.connect(self._deselect_all_channels)

        btn_layout.addWidget(self.btn_all_fav)
        btn_layout.addWidget(self.btn_none_fav)
        btn_layout.addWidget(self.btn_deselect_all)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_reset)
        layout.addLayout(btn_layout)

        self.selected_label = QLabel("Active Traces: None")
        self.selected_label.setWordWrap(True)
        self.selected_label.setStyleSheet("font-weight: bold; color: #F44336; padding: 5px;")
        layout.addWidget(self.selected_label)

        # 2. Tree
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Hierarchy / Tag", "Plot Label", "Mult.", "Scale", "Fav", "Lane"])
        self.tree.setColumnWidth(0, 350)
        self.tree.setColumnWidth(1, 200)
        self.tree.setColumnWidth(2, 60)
        self.tree.setColumnWidth(3, 80)
        self.tree.setColumnWidth(4, 40)
        self.tree.setColumnWidth(5, 80)
        layout.addWidget(self.tree)

        self._populate_tree()

        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.itemClicked.connect(self._on_item_clicked)

        from PyQt6.QtWidgets import QAbstractItemView
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.itemDoubleClicked.connect(self._handle_double_click)

        # 3. Dialog Buttons
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._safe_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _deselect_all_channels(self):
        """Unchecks every single channel across the entire tree."""
        self.tree.blockSignals(True)
        for tag, items in self.item_map.items():
            if tag not in self.config: self.config[tag] = {}
            self.config[tag]['selected'] = False
            for item in items:
                item.setCheckState(0, Qt.CheckState.Unchecked)
        self.tree.blockSignals(False)
        self._update_selected_label()

    def _handle_double_click(self, item, column):
        """Intercepts double clicks so only specific columns can be edited."""
        tag = item.data(0, Qt.ItemDataRole.UserRole)
        # Only allow editing if it is a leaf node (has a tag) AND is Col 1 or 2
        if tag and column in [1, 2]:
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            self.tree.editItem(item, column)

    def _get_persistent_color(self, tag_name):
        palette = [
            '#1f77b4', '#aec7e8', '#ff7f0e', '#ffbb78', '#2ca02c', '#98df8a',
            '#d62728', '#ff9896', '#9467bd', '#c5b0d5', '#8c564b', '#c49c94',
            '#e377c2', '#f7b6d2', '#7f7f7f', '#c7c7c7', '#bcbd22', '#dbdb8d',
            '#17becf', '#9edae5'
        ]
        idx = zlib.adler32(tag_name.encode('utf-8')) % len(palette)
        return palette[idx]


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

    def _populate_tree(self):
        """Builds the UI tree directly from the Master Registry."""
        import os, json
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(current_dir)
        registry_path = os.path.join(project_root, "system_tags.json")

        try:
            with open(registry_path, "r", encoding="utf-8") as f:
                registry = json.load(f)
        except FileNotFoundError:
            print("[Viewer] Error: system_tags.json not found.")
            return

        self.tree.clear()
        self.item_map = {}
        folder_nodes = {}

        # Create the persistent Favorites Root at the very top
        self.fav_root = QTreeWidgetItem(self.tree)
        self.fav_root.setText(0, "⭐ Favorites")
        # Make the text bold so it stands out
        font = self.fav_root.font(0)
        font.setBold(True)
        self.fav_root.setFont(0, font)

        # 2. Create the All Channels Root
        all_root = QTreeWidgetItem(self.tree)
        all_root.setText(0, "📁 All Channels")

        for full_tag, metadata in registry.items():
            default_label = metadata.get("default_label", full_tag)
            description = metadata.get("description", "")
            default_mult = metadata.get("multiplier", 1.0)
            default_scale = metadata.get("default_scale", "linear")

            user_config = self.config.get(full_tag, {})
            display_label = user_config.get("label", default_label)
            is_selected = user_config.get("selected", False)
            user_mult = user_config.get("multiplier", default_mult)
            user_scale = user_config.get("scale", default_scale)
            is_fav = user_config.get("favorite", False)

            # 2. Find/Create Folder Path under all_root
            parts = full_tag.split('.')
            parent_node = all_root
            current_path = ""
            for part in parts[:-1]:
                current_path = f"{current_path}.{part}" if current_path else part
                if current_path not in folder_nodes:
                    node = QTreeWidgetItem(parent_node)
                    node.setText(0, part.replace('_', ' ').title())
                    folder_nodes[current_path] = node
                parent_node = folder_nodes[current_path]

            # 3. Create the row in the main tree
            self._create_tag_row(full_tag, parent_node, metadata, in_fav_folder=False)

            # 4. CRITICAL: If it's a favorite, create the row in the Fav root too!
            if is_fav:
                self._create_tag_row(full_tag, self.fav_root, metadata, in_fav_folder=True)

        self.tree.collapseAll()
        self.fav_root.setExpanded(True)  # Keep favorites visible
        self._update_selected_label()
        self.fav_root.sortChildren(0, Qt.SortOrder.AscendingOrder)

    def _create_tag_row(self, full_tag, parent_node, metadata, in_fav_folder=False):
        """Helper to create a row. Adapts text based on which folder it lives in."""
        user_cfg = self.config.get(full_tag, {})

        item = QTreeWidgetItem(parent_node)
        item.setData(0, Qt.ItemDataRole.UserRole, full_tag)
        # REMOVED ItemIsEditable here! The double-click interceptor handles it now.
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)

        is_selected = user_cfg.get('selected', False)
        item.setCheckState(0, Qt.CheckState.Checked if is_selected else Qt.CheckState.Unchecked)

        # --- Context-Aware Naming ---
        default_label = metadata.get('default_label', full_tag)
        description = metadata.get('description', "")  # Extract the description

        if in_fav_folder:
            # Favorites view: Show full name, hide redundant plot label
            item.setText(0, default_label)
            item.setText(1, "")
        else:
            # Hierarchy view: Show short name, show editable plot label
            short_name = full_tag.split('.')[-1].replace('_', ' ').title()
            item.setText(0, short_name)
            item.setText(1, user_cfg.get('label', default_label))

        if description:
            item.setToolTip(0, description)  # Hover over checkbox/short name
            item.setToolTip(1, description)  # Hover over plot label

        # Col 2: Multiplier
        item.setText(2, str(user_cfg.get('multiplier', metadata.get('multiplier', 1.0))))

        # Col 3: Scale Dropdown (With Dark-Mode CSS Fix)
        combo = QComboBox()
        combo.addItems(["Linear", "Log"])
        scale = user_cfg.get('scale', metadata.get('default_scale', 'linear'))
        combo.setCurrentText(scale.title())
        # Fixed Stylesheet to prevent invisible text
        combo.setStyleSheet("""
            QComboBox { background-color: white; color: black; border: 1px solid #ccc; padding: 1px; }
            QComboBox QAbstractItemView { background-color: white; color: black; selection-background-color: #e0e0e0; }
        """)
        combo.currentTextChanged.connect(lambda txt: self._on_scale_changed(item, full_tag, txt))
        self.tree.setItemWidget(item, 3, combo)

        # Col 4: Fav Star Button
        is_fav = user_cfg.get('favorite', False)
        btn_fav = QPushButton("⭐" if is_fav else "☆")
        color = "#FFD700" if is_fav else "#888888"
        btn_fav.setStyleSheet(f"font-size: 16px; color: {color}; border: none; background: transparent;")
        btn_fav.clicked.connect(lambda: self._on_fav_clicked(full_tag))
        self.tree.setItemWidget(item, 4, btn_fav)

        # Col 5: Lane Assignment Dropdown
        lane_combo = QComboBox()
        # Define default system lanes
        lane_combo.addItems(["Lane 1", "Lane 2", "Lane 3", "Lane 4"])

        # Load existing preference or default to Lane 1
        current_lane = user_cfg.get('lane', 'Lane 1')
        lane_combo.setCurrentText(current_lane)

        lane_combo.setStyleSheet("""
                    QComboBox { background-color: white; color: black; border: 1px solid #ccc; padding: 1px; }
                    QComboBox QAbstractItemView { background-color: white; color: black; selection-background-color: #e0e0e0; }
                """)
        lane_combo.currentTextChanged.connect(lambda txt: self._on_lane_changed(item, full_tag, txt))
        self.tree.setItemWidget(item, 5, lane_combo)

        if full_tag not in self.item_map:
            self.item_map[full_tag] = []
        self.item_map[full_tag].append(item)
        return item

    def _on_item_changed(self, item, column):
        """Handles standard text and checkbox edits."""
        self.tree.blockSignals(True)
        full_tag = item.data(0, Qt.ItemDataRole.UserRole)

        if full_tag and full_tag not in self.config:
            self.config[full_tag] = {}

        # Col 0: Checkbox Toggled
        if column == 0 and full_tag:
            is_checked = item.checkState(0) == Qt.CheckState.Checked
            self.config[full_tag]['selected'] = is_checked
            self._sync_items(full_tag, item, column_to_sync=0, check_state=item.checkState(0))
            self._update_selected_label()

        # Col 1: Label Edited
        elif column == 1 and full_tag:
            self.config[full_tag]['label'] = item.text(1).strip()
            self._sync_items(full_tag, item, column_to_sync=1, text_val=item.text(1))
            self._update_selected_label()

        # Col 2: Multiplier Edited
        elif column == 2 and full_tag:
            try:
                new_mult = float(item.text(2))
                self.config[full_tag]['multiplier'] = new_mult
                self._sync_items(full_tag, item, column_to_sync=2, text_val=str(new_mult))
            except ValueError:
                pass

        self.tree.blockSignals(False)

    def _on_lane_changed(self, source_item, full_tag, new_lane):
        """Saves the lane selection and syncs favorites."""
        if full_tag not in self.config: self.config[full_tag] = {}
        self.config[full_tag]['lane'] = new_lane

        if full_tag in self.item_map:
            for mapped_item in self.item_map[full_tag]:
                if mapped_item != source_item:
                    combo = self.tree.itemWidget(mapped_item, 5)
                    if combo and combo.currentText() != new_lane:
                        combo.blockSignals(True)
                        combo.setCurrentText(new_lane)
                        combo.blockSignals(False)

    # --- New Event Handlers for the Injected Widgets ---

    def _on_scale_changed(self, source_item, full_tag, new_scale):
        """Saves the dropdown selection to config and syncs favorites."""
        scale_lower = new_scale.lower()
        if full_tag not in self.config: self.config[full_tag] = {}
        self.config[full_tag]['scale'] = scale_lower

        if full_tag in self.item_map:
            for mapped_item in self.item_map[full_tag]:
                if mapped_item != source_item:
                    combo = self.tree.itemWidget(mapped_item, 3)
                    if combo and combo.currentText() != new_scale:
                        combo.blockSignals(True)
                        combo.setCurrentText(new_scale)
                        combo.blockSignals(False)

    def _on_fav_clicked(self, full_tag):
        """Toggles favorite state, updates UI, and sorts alphabetically."""
        # FIX: Block signals so creating a new row doesn't trigger the checkbox bug
        self.tree.blockSignals(True)

        try:
            if full_tag not in self.config: self.config[full_tag] = {}

            # 1. Toggle the data
            is_now_fav = not self.config[full_tag].get('favorite', False)
            self.config[full_tag]['favorite'] = is_now_fav

            # 2. Update all existing Star Buttons (visuals)
            new_text = "⭐" if is_now_fav else "☆"
            new_color = "#FFD700" if is_now_fav else "#888888"

            for item in self.item_map.get(full_tag, []):
                btn = self.tree.itemWidget(item, 4)
                if btn:
                    btn.setText(new_text)
                    btn.setStyleSheet(f"font-size: 16px; color: {new_color}; border: none; background: transparent;")

            # 3. Handle the Favorites Folder
            if is_now_fav:
                exists_in_fav = any(itm.parent() == self.fav_root for itm in self.item_map[full_tag])
                if not exists_in_fav:
                    metadata = self.registry.get(full_tag, {})
                    self._create_tag_row(full_tag, self.fav_root, metadata, in_fav_folder=True)
                    self.fav_root.setExpanded(True)

                    # ALPHABETICAL SORT: Sorts the folder by Column 0 (The Full Name)
                    self.fav_root.sortChildren(0, Qt.SortOrder.AscendingOrder)
            else:
                for item in self.item_map[full_tag][:]:
                    if item.parent() == self.fav_root:
                        self.item_map[full_tag].remove(item)
                        self.fav_root.removeChild(item)
        finally:
            # Re-enable signals after everything is built
            self.tree.blockSignals(False)

    def _sync_items(self, full_tag, source_item, column_to_sync, text_val=None, check_state=None):
        """Helper to sync changes between Main and Favorites folders."""
        if full_tag in self.item_map:
            for mapped_item in self.item_map[full_tag]:
                if mapped_item != source_item:
                    if text_val is not None:
                        mapped_item.setText(column_to_sync, text_val)
                    if check_state is not None:
                        mapped_item.setCheckState(column_to_sync, check_state)

    def _update_selected_label(self):
        """Scans the config and updates the live label at the top of the UI."""
        selected_names = []
        for tag, user_config in self.config.items():
            if user_config.get("selected", False):
                # Grab the custom label, or fallback to the tag name
                label = user_config.get("label", tag.split('.')[-1])
                selected_names.append(label)

        if selected_names:
            # Join them with a bullet point or comma
            text = " • ".join(selected_names)
            self.selected_label.setText(f"Active Traces: {text}")
            self.selected_label.setStyleSheet("font-weight: bold; color: #4CAF50; padding: 5px;")  # Green
        else:
            self.selected_label.setText("Active Traces: None")
            self.selected_label.setStyleSheet("font-weight: bold; color: #F44336; padding: 5px;")  # Red

    def _select_all_favourites(self):
        self.tree.blockSignals(True)
        for i in range(self.fav_root.childCount()):
            item = self.fav_root.child(i)
            tag = item.data(0, Qt.ItemDataRole.UserRole)

            if tag:
                # 1. Update visual state
                item.setCheckState(0, Qt.CheckState.Checked)

                # 2. Update master config state
                if tag not in self.config: self.config[tag] = {}
                self.config[tag]['selected'] = True

                # 3. Synchronize duplicates in the "All Channels" tree
                if tag in self.item_map:
                    for dup in self.item_map[tag]:
                        dup.setCheckState(0, Qt.CheckState.Checked)

        self.tree.blockSignals(False)
        self._update_selected_label()  # Refresh the green/red text at the top

    def _deselect_all_favourites(self):
        self.tree.blockSignals(True)
        print(self.fav_root.childCount())
        for i in range(self.fav_root.childCount()):
            item = self.fav_root.child(i)
            tag = item.data(0, Qt.ItemDataRole.UserRole)

            if tag:
                # 1. Update visual state
                item.setCheckState(0, Qt.CheckState.Unchecked)

                # 2. Update master config state
                if tag not in self.config: self.config[tag] = {}
                self.config[tag]['selected'] = False

                # 3. Synchronize duplicates
                if tag in self.item_map:
                    for dup in self.item_map[tag]:
                        dup.setCheckState(0, Qt.CheckState.Unchecked)

        self.tree.blockSignals(False)
        self._update_selected_label()  # Refresh the green/red text at the top

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

    def get_selection(self):
        final_config = {}
        for tag, items in self.item_map.items():
            if not items: continue
            item = items[0]

            is_selected = (item.checkState(0) == Qt.CheckState.Checked)
            is_fav = self.config.get(tag, {}).get('favorite', False)
            color = self.config.get(tag, {}).get('color') or self._get_persistent_color(tag)

            # Extract lane from the widget
            lane_widget = self.tree.itemWidget(item, 5)
            lane_val = lane_widget.currentText() if lane_widget else "Lane 1"

            final_config[tag] = {
                "dataSource": tag,
                "label": item.text(1),
                "multiplier": float(item.text(2)) if item.text(2) else 1.0,
                "scale": self.tree.itemWidget(item, 3).currentText().lower(),
                "favorite": is_fav,
                "selected": is_selected,
                "color": color,
                "lane": lane_val  # Inject into master config
            }
        return final_config