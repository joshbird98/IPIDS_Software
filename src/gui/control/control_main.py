import os
import sys
import time
from PyQt6.QtWidgets import QMainWindow, QDockWidget, QMessageBox, QInputDialog, QLineEdit
from PyQt6.QtCore import Qt, QTimer, QUrl, QSettings
from PyQt6.QtGui import QAction
from PyQt6.QtMultimedia import QSoundEffect

from qfluentwidgets import FluentIcon as FIF, Theme
from src.core.theme import is_dark_mode

from src.core.network_map import (
    ZMQ_PORT_PLC_PUB, ZMQ_PORT_VACUUM_PUB,
    ZMQ_PORT_SRC_TURBO_PUB, ZMQ_PORT_MANAGER_PUB,
    ZMQ_PORT_SPELLMAN_PUB, ZMQ_PORT_MAGNET_PUB, ZMQ_PORT_SMU_PUB
)
from src.core.event_helper import EventHelper
from src.core.recipe_engine import RecipeWorker

# --- Core/Thread Imports ---
from src.core.network_threads import ZMQTelemetryThread, ZMQCommandThread
from src.core.fault_registry import FaultRegistry

# --- Extracted Widget Imports ---
from src.gui.control.widgets.recipe_widget import RecipeExecutionWidget
from src.gui.control.widgets.vacuum_widget import VacuumControlWidget
from src.gui.control.widgets.logger_widget import ExperimentLoggerWidget
from src.gui.control.widgets.source_widget import IonSourceWidget
from src.gui.control.widgets.optics_widget import BeamlineOpticsWidget
from src.gui.control.widgets.system_health_widget import SystemHealthWidget
from src.gui.control.widgets.services_widget import ServicesWidget
from src.gui.control.dialogs.config_editor_dialog import ConfigEditorDialog
from src.gui.control.widgets.faraday_smu_widget import FaradaySMUWidget
from src.gui.control.widgets.optimiser_widget import ParameterOptimizerWidget


class AudioManager:
    def __init__(self):
        self.sounds = {}
        self.base_dir = os.path.dirname(os.path.abspath(__file__))
        self.sounds_dir = os.path.join(self.base_dir, "assets", "sounds")

        self._load_sound("warning", "warning.wav", volume=0.7)
        self._load_sound("fault", "fault.wav", volume=1.0)
        self._load_sound("critical", "critical_comms_loss.wav", volume=1.0)
        self._load_sound("trip", "relay_trip.wav", volume=0.8)

    def _load_sound(self, name: str, filename: str, volume: float):
        filepath = os.path.join(self.sounds_dir, filename)
        if not os.path.exists(filepath):
            print(f"[Audio] Missing asset: {filepath}")
            return
        effect = QSoundEffect()
        effect.setSource(QUrl.fromLocalFile(filepath))
        effect.setVolume(volume)
        self.sounds[name] = effect

    def play(self, name: str):
        if name in self.sounds:
            self.sounds[name].play()


class ControlMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("IPIDS Operator HMI")
        self.setObjectName("ControlMainWindow")
        self.resize(1600, 900)
        self.setDockOptions(QMainWindow.DockOption.AllowNestedDocks | QMainWindow.DockOption.AllowTabbedDocks)

        self.settings = QSettings("SurreyIonBeam", "IPIDS_HMI")
        self.audio = AudioManager()

        self.memory_active_faults = set()
        self.memory_vg_warnings = {i: False for i in range(1, 7)}
        self.memory_vg_trips = {i: False for i in range(1, 7)}
        self.memory_comms_lost = False

        current_dir = os.path.dirname(os.path.abspath(__file__))
        fault_map_path = os.path.abspath(os.path.join(current_dir, "../../../config/fault_config.json"))
        self.fault_engine = FaultRegistry(fault_map_path)

        self.cmd_thread = ZMQCommandThread()
        self.cmd_thread.start()

        self.event_helper = EventHelper("hmi_control")
        self.master_telemetry_cache = {}

        self.recipe_worker = RecipeWorker(self.cmd_thread, lambda: self.master_telemetry_cache.copy())

        telemetry_ports = [
            ZMQ_PORT_PLC_PUB, ZMQ_PORT_VACUUM_PUB,
            ZMQ_PORT_SRC_TURBO_PUB, ZMQ_PORT_MANAGER_PUB,
            ZMQ_PORT_SPELLMAN_PUB, ZMQ_PORT_MAGNET_PUB,
            ZMQ_PORT_SMU_PUB
        ]
        self.telemetry_thread = ZMQTelemetryThread(telemetry_ports)
        self.telemetry_thread.data_received.connect(self._route_telemetry)
        self.telemetry_thread.start()

        self.subsystems = {}

        self._init_docks()
        self._setup_menus()

        if self.settings.value("geometry"):
            self.restoreGeometry(self.settings.value("geometry"))
        if self.settings.value("windowState"):
            self.restoreState(self.settings.value("windowState"))

        self.last_seen = {"plc": 0.0, "vacuum": 0.0, "src_turbo": 0.0, "magnet": 0.0}

        self.watchdog_timer = QTimer(self)
        self.watchdog_timer.timeout.connect(self._ui_update_loop)
        self.watchdog_timer.start(100)

    def _setup_menus(self):
        menubar = self.menuBar()

        # Explicitly cast the icon theme based on the config file
        icon_theme = Theme.DARK if is_dark_mode else Theme.LIGHT

        # 1. VIEW MENU
        view_menu = menubar.addMenu("View")
        view_menu.setIcon(FIF.LAYOUT.icon(icon_theme))

        view_menu.addAction(self.vac_dock.toggleViewAction())
        view_menu.addAction(self.src_dock.toggleViewAction())
        view_menu.addAction(self.optics_dock.toggleViewAction())
        view_menu.addAction(self.smu_dock.toggleViewAction())
        view_menu.addAction(self.optimiser_dock.toggleViewAction())
        view_menu.addAction(self.log_dock.toggleViewAction())
        view_menu.addAction(self.recipe_dock.toggleViewAction())
        view_menu.addAction(self.health_dock.toggleViewAction())
        view_menu.addAction(self.service_dock.toggleViewAction())

        view_menu.addSeparator()

        reset_action = QAction(FIF.SYNC.icon(icon_theme), "Reset Default Layout", self)
        reset_action.triggered.connect(self._reset_layout)
        view_menu.addAction(reset_action)

        # 2. CONFIG EDITOR
        settings_action = QAction(FIF.SETTING.icon(icon_theme), "Config", self)
        settings_action.triggered.connect(self._show_config_editor)
        menubar.addAction(settings_action)

        # 3. HELP DOCUMENTATION
        help_action = QAction(FIF.HELP.icon(icon_theme), "Help", self)
        help_action.triggered.connect(self._open_help_docs)
        menubar.addAction(help_action)

    def _reset_layout(self):
        reply = QMessageBox.question(
            self, 'Reset Layout',
            'Are you sure you want to reset the UI layout? The application will close and you must restart it.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.settings.clear()
            self.close()

    def _show_config_editor(self):
        pwd, ok = QInputDialog.getText(self, "Authentication Required", "Enter Engineering Password:",
                                       QLineEdit.EchoMode.Password)
        if ok and pwd == "admin":
            editor = ConfigEditorDialog(self.cmd_thread, self)
            editor.exec()
        elif ok:
            QMessageBox.warning(self, "Access Denied", "Incorrect password.")

    def _open_help_docs(self):
        QMessageBox.information(self, "Documentation",
                                "IPIDS Documentation Library coming soon...\n\nFuture implementation will include navigation links and offline markdown viewing.")

    def _init_docks(self):
        # 1. Left Area Docks
        self.vac_dock = QDockWidget("Vacuum System", self)
        self.vac_dock.setObjectName("VacuumDock")
        self.vac_widget = VacuumControlWidget(self.cmd_thread, self.fault_engine)
        self.vac_dock.setWidget(self.vac_widget)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.vac_dock)
        self.subsystems["vacuum"] = self.vac_widget

        self.log_dock = QDockWidget("Experiment Logging", self)
        self.log_dock.setObjectName("LogDock")
        self.log_widget = ExperimentLoggerWidget(self.event_helper)
        self.log_dock.setWidget(self.log_widget)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.log_dock)
        self.subsystems["logging"] = self.log_widget

        self.recipe_dock = QDockWidget("Recipe Execution", self)
        self.recipe_dock.setObjectName("RecipeDock")
        self.recipe_widget = RecipeExecutionWidget(self.recipe_worker, self.master_telemetry_cache)
        self.recipe_dock.setWidget(self.recipe_widget)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.recipe_dock)
        self.subsystems["recipe"] = self.recipe_widget

        self.optimiser_dock = QDockWidget("Optimiser", self)
        self.optimiser_dock.setObjectName("OptimiserDock")
        self.optimiser_widget = ParameterOptimizerWidget(self.cmd_thread, lambda tag: self.master_telemetry_cache.get(tag, 0.0))
        self.optimiser_dock.setWidget(self.optimiser_widget)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.optimiser_dock)
        self.subsystems["optimiser"] = self.optimiser_widget

        self.tabifyDockWidget(self.vac_dock, self.log_dock)
        self.tabifyDockWidget(self.log_dock, self.recipe_dock)
        self.tabifyDockWidget(self.recipe_dock, self.optimiser_dock)
        self.vac_dock.raise_()

        # 2. Right Area Docks
        self.src_dock = QDockWidget("TESS Ion Source Control", self)
        self.src_dock.setObjectName("SourceDock")
        self.src_widget = IonSourceWidget(self.cmd_thread, self.fault_engine)
        self.src_dock.setWidget(self.src_widget)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.src_dock)
        self.subsystems["source"] = self.src_widget

        self.optics_dock = QDockWidget("Beamline Optics", self)
        self.optics_dock.setObjectName("OpticsDock")
        self.optics_widget = BeamlineOpticsWidget(self.cmd_thread, self.event_helper)
        self.optics_dock.setWidget(self.optics_widget)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.optics_dock)
        self.subsystems["optics"] = self.optics_widget

        self.smu_dock = QDockWidget("Faraday Cup && SMU", self)
        self.smu_dock.setObjectName("FaradaySmuDock")
        self.smu_widget = FaradaySMUWidget(self.cmd_thread)
        self.smu_dock.setWidget(self.smu_widget)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.smu_dock)
        self.subsystems["faraday_smu"] = self.smu_widget

        self.tabifyDockWidget(self.src_dock, self.optics_dock)
        self.src_dock.raise_()

        # 3. Bottom Area Dock
        self.health_dock = QDockWidget("System Health", self)
        self.health_dock.setObjectName("HealthDock")
        self.health_widget = SystemHealthWidget(self.cmd_thread, self.fault_engine)
        self.health_dock.setWidget(self.health_widget)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.health_dock)
        self.subsystems["health"] = self.health_widget


        self.service_dock = QDockWidget("Service Manager", self)
        self.service_dock.setObjectName("ServicesDock")
        self.service_widget = ServicesWidget(self.cmd_thread)
        self.service_dock.setWidget(self.service_widget)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.service_dock)
        self.subsystems["services"] = self.service_widget

    def _route_telemetry(self, fresh_data: dict):
        now = time.time()
        if "system.connected" in fresh_data:
            self.last_seen["plc"] = now
            fresh_data["system.connected"] = True
        if "ion_beam.source.turbo_pump.rb_speed_hz" in fresh_data:
            self.last_seen["src_turbo"] = now
            fresh_data["pump.connected"] = True
        if any("vacuum_gauge" in key for key in fresh_data.keys()):
            self.last_seen["vacuum"] = now
            fresh_data["vacuum.connected"] = True
        if "ion_beam.beamline.magnet.rb_voltage" in fresh_data:
            self.last_seen["magnet"] = now
            fresh_data["magnet.connected"] = True
        if "ion_beam.beamline.faraday.smu.stat_comms_fail" in fresh_data:
            self.last_seen["smu"] = time.time()
            fresh_data["smu.connected"] = True

        self.master_telemetry_cache.update(fresh_data)

    def _ui_update_loop(self):
        now = time.time()

        if now - self.last_seen["plc"] > 2.5 and self.master_telemetry_cache.get("system.connected", True):
            self.master_telemetry_cache["system.connected"] = False
        if now - self.last_seen["src_turbo"] > 2.5 and self.master_telemetry_cache.get("pump.connected", True):
            self.master_telemetry_cache["pump.connected"] = False
        if now - self.last_seen["vacuum"] > 2.5 and self.master_telemetry_cache.get("vacuum.connected", True):
            self.master_telemetry_cache["vacuum.connected"] = False
        if now - self.last_seen["magnet"] > 2.5 and self.master_telemetry_cache.get("magnet.connected", True):
            self.master_telemetry_cache["magnet.connected"] = False
        if now - self.last_seen.get("smu", 0) > 2.5 and self.master_telemetry_cache.get("smu.connected", True):
            self.master_telemetry_cache["smu.connected"] = False

        self.vac_widget.update_telemetry(self.master_telemetry_cache)
        self.src_widget.update_telemetry(self.master_telemetry_cache)
        self.optics_widget.update_telemetry(self.master_telemetry_cache)
        self.health_widget.update_telemetry(self.master_telemetry_cache)
        self.service_widget.update_telemetry(self.master_telemetry_cache)
        self.log_widget.update_telemetry(self.master_telemetry_cache)
        self.smu_widget.update_telemetry(self.master_telemetry_cache)
        self.optimiser_widget.update_telemetry(self.master_telemetry_cache)

        self._evaluate_audio_triggers()

    def _evaluate_audio_triggers(self):
        data = self.master_telemetry_cache
        comms_lost = not bool(data.get("system.connected", False)) or bool(
            data.get("ion_beam.system.pc_plc_comms_lost", False))

        if comms_lost and not self.memory_comms_lost:
            self.audio.play("critical")
        self.memory_comms_lost = comms_lost

        if comms_lost:
            return

        gauge_locations = {1: "source", 2: "beamline", 3: "beamline", 4: "beamline", 5: "endstation", 6: "loadlock"}
        for i in range(1, 7):
            loc = gauge_locations[i]
            approaching = bool(data.get(f"ion_beam.{loc}.vacuum_gauge_{i}.stat_approaching_sp", False))
            tripped = bool(data.get(f"ion_beam.{loc}.vacuum_gauge_{i}.stat_above_sp", False))

            if approaching and not self.memory_vg_warnings[i]:
                self.audio.play("warning")
            self.memory_vg_warnings[i] = approaching

            if tripped and not self.memory_vg_trips[i]:
                self.audio.play("trip")
            self.memory_vg_trips[i] = tripped

        current_active_faults = set()
        word_mapping = {
            "ion_beam.faults.word_0_system": "UDT_Fault_Word_0_System",
            "ion_beam.faults.word_1_vacuum": "UDT_Fault_Word_1_Vacuum",
            "ion_beam.faults.word_2_source": "UDT_Fault_Word_2_Source",
        }

        for tag, udt_key in word_mapping.items():
            raw_word = data.get(tag)
            if raw_word is not None:
                faults = self.fault_engine.get_active_faults(udt_key, int(raw_word))
                for f in faults:
                    current_active_faults.add(f["name"])

        new_faults = current_active_faults - self.memory_active_faults
        if new_faults:
            self.audio.play("fault")
        self.memory_active_faults = current_active_faults

    def closeEvent(self, event):
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("windowState", self.saveState())
        self.telemetry_thread.stop()
        self.cmd_thread.stop()
        super().closeEvent(event)