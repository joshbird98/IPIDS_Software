"""Recipe fault policy: which PLC faults stop the recipe (BIT_FAULT entries)."""
import json
import logging

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QComboBox, QDialog, QFormLayout, QHBoxLayout, QPushButton, QScrollArea, QVBoxLayout,
                             QWidget)

from src.core.paths import config_path

log = logging.getLogger("hmi.fault_policy")


class FaultPolicyDialog(QDialog):
    def __init__(self, policy_list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Global Fault Policy")
        self.resize(800, 400)
        self.policy = policy_list
        self.available_faults = self._load_fault_config()

        layout = QVBoxLayout(self)
        btn_add = QPushButton("➕ Add Specific Fault Abort")
        btn_add.clicked.connect(self._add_fault)
        layout.addWidget(btn_add)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.container = QWidget()
        self.form = QFormLayout(self.container)
        scroll.setWidget(self.container)
        layout.addWidget(scroll)

        btn_ok = QPushButton("Done")
        btn_ok.clicked.connect(self.accept)
        layout.addWidget(btn_ok)

        self._redraw()

    @staticmethod
    def _load_fault_config(path: str = None) -> list:
        """fault_config.json as a flat list for the dropdown ([] if it can't be read - logged)."""
        path = path or config_path("fault_config.json")
        try:
            with open(path, 'r') as f:
                data = json.load(f)
            faults = [{"registry_tag": udt_data.get("registry_tag", ""), "bit_str": bit_str,
                       "name": info.get("name", "Unknown"), "desc": info.get("description", ""),
                       "display": f"[{info.get('name')}] {info.get('description')}"}
                      for udt_data in data.values() for bit_str, info in udt_data.get("bits", {}).items()]
        except (OSError, ValueError, AttributeError) as e:
            log.error("Fault list %s not readable - the fault policy can't offer any faults: %s", path, e)
            return []
        return sorted(faults, key=lambda x: x["name"])

    def _add_fault(self):
        # We now store a specialized 'BIT_FAULT' dict
        self.policy.append({
            "type": "BIT_FAULT",
            "registry_tag": "",
            "bit_str": "",
            "name": "",
            "action": "ABORT"
        })
        self._redraw()

    def _del_fault(self, idx):
        del self.policy[idx]
        self._redraw()

    def _redraw(self):
        while self.form.count():
            item = self.form.takeAt(0)
            if item.widget(): item.widget().deleteLater()

        for i, fault in enumerate(self.policy):
            w = QWidget()
            l = QHBoxLayout(w)
            l.setContentsMargins(0, 0, 0, 0)

            cb_faults = QComboBox()
            # Populate dropdown
            for af in self.available_faults:
                cb_faults.addItem(af["display"], userData=af)

            # Set current selection (a fault no longer in fault_config.json is shown as it is, not
            # replaced on screen by the first fault in the list while the policy still holds it)
            current_name = fault.get("name")
            if current_name:
                idx = cb_faults.findText(f"[{current_name}]", Qt.MatchFlag.MatchContains)
                if idx < 0:
                    cb_faults.insertItem(0, f"[{current_name}] not in fault_config.json - trips on "
                                            f"{fault.get('registry_tag') or '?'} bit {fault.get('bit_str') or '?'}")
                    idx = 0
                cb_faults.setCurrentIndex(idx)

            def _on_fault_changed(index, f=fault, cb=cb_faults):
                data = cb.itemData(index)
                if data:
                    f["registry_tag"] = data["registry_tag"]
                    f["bit_str"] = data["bit_str"]
                    f["name"] = data["name"]

            cb_faults.currentIndexChanged.connect(_on_fault_changed)
            # Trigger once to set initial blank value
            if not current_name and cb_faults.count() > 0:
                _on_fault_changed(0)

            btn_del = QPushButton("❌")
            btn_del.clicked.connect(lambda _, index=i: self._del_fault(index))

            l.addWidget(cb_faults, stretch=1)
            l.addWidget(btn_del)
            self.form.addRow(f"Trip {i + 1}:", w)
