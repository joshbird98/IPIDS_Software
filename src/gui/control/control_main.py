import logging
import os
import time
from PyQt6.QtWidgets import QMainWindow, QDockWidget, QMessageBox, QInputDialog, QLineEdit
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction

from qfluentwidgets import FluentIcon as FIF, Theme, InfoBar, InfoBarPosition
from src.core.theme import is_dark_mode
from src.gui.settings import gui_settings

from src.core.network_map import (
    ZMQ_PORT_PLC_PUB, ZMQ_PORT_VACUUM_PUB,
    ZMQ_PORT_SRC_TURBO_PUB, ZMQ_PORT_MANAGER_PUB,
    ZMQ_PORT_SPELLMAN_PUB, ZMQ_PORT_MAGNET_PUB, ZMQ_PORT_SMU_PUB,
    ZMQ_PORT_ADAM_PUB, ZMQ_PORT_STEERER_MAGNET_PUB, ZMQ_PORT_MONITOR_PUB, ZMQ_PORT_SEQUENCER_PUB,
    ZMQ_PORT_ALARMS_PUB
)
from src.core.commands import SUCCESS, describe
from src.core.event_helper import EventHelper
from src.core.paths import simulation_enabled
from src.core.telemetry_cache import TelemetryCache
from src.gui.control.sequencer_link import FETCH_ORIGIN

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
from src.gui.control.widgets.current_monitor_widget import AdamMonitorWidget
from src.gui.control.widgets.mass_scan import PrecisionMassScannerWidget
from src.gui.control.widgets.command_log_widget import CommandLogWidget
from src.gui.control.widgets.automation_bar import AutomationBar
from src.gui.control.widgets.alarm_widget import AlarmPanel

log = logging.getLogger("hmi")

# Command outcomes worth interrupting the operator for
_PROBLEM_TITLES = {"rejected": "Command refused", "failed": "Command failed",
                   "timeout": "No reply to command", "unreachable": "Service not running"}


class ControlMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SINDIE Operator HMI" + ("  [SIMULATION - fake devices]" if simulation_enabled() else ""))
        self.setObjectName("ControlMainWindow")
        self.resize(1600, 900)
        self.setDockOptions(QMainWindow.DockOption.AllowNestedDocks | QMainWindow.DockOption.AllowTabbedDocks)

        self.settings = gui_settings("SurreyIonBeam", "SINDIE_HMI")

        current_dir = os.path.dirname(os.path.abspath(__file__))
        fault_map_path = os.path.abspath(os.path.join(current_dir, "../../../config/fault_config.json"))
        self.fault_engine = FaultRegistry(fault_map_path)

        self.cmd_thread = ZMQCommandThread()
        self.cmd_thread.command_result.connect(self._on_command_result)
        self.cmd_thread.start()
        self._last_problem_shown = {}

        self.event_helper = EventHelper("hmi_control")
        # Every value with the service it came from and when: stale values are dropped from
        # what the widgets see (shown as '---'), and automation only ever reads fresh ones.
        self.telemetry = TelemetryCache()
        # The fresh view handed to the widgets; refreshed in place 10x a second
        self.master_telemetry_cache = {}

        # Recipes, mass scans and the optimiser run in the sequencer service; their panels are
        # clients of it (src/gui/control/sequencer_link.py)
        telemetry_ports = [
            ZMQ_PORT_PLC_PUB, ZMQ_PORT_VACUUM_PUB,
            ZMQ_PORT_SRC_TURBO_PUB, ZMQ_PORT_MANAGER_PUB,
            ZMQ_PORT_SPELLMAN_PUB, ZMQ_PORT_MAGNET_PUB,
            ZMQ_PORT_SMU_PUB, ZMQ_PORT_ADAM_PUB, ZMQ_PORT_STEERER_MAGNET_PUB,
            ZMQ_PORT_MONITOR_PUB, ZMQ_PORT_SEQUENCER_PUB, ZMQ_PORT_ALARMS_PUB
        ]
        self.telemetry_thread = ZMQTelemetryThread(telemetry_ports)
        self.telemetry_thread.data_received.connect(self._route_telemetry)
        self.telemetry_thread.start()

        self.subsystems = {}

        # Which sequence holds which controls, with Take control buttons
        self.automation_bar = AutomationBar(self.cmd_thread, self)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.automation_bar)

        self._init_docks()
        self._setup_menus()

        if self.settings.value("geometry"):
            self.restoreGeometry(self.settings.value("geometry"))
        if self.settings.value("windowState"):
            self.restoreState(self.settings.value("windowState"))

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
        view_menu.addAction(self.current_mon_dock.toggleViewAction())
        view_menu.addAction(self.mass_scan_dock.toggleViewAction())
        view_menu.addAction(self.cmd_log_dock.toggleViewAction())
        view_menu.addAction(self.alarm_dock.toggleViewAction())

        view_menu.addSeparator()

        reset_action = QAction(FIF.SYNC.icon(icon_theme), "Reset Default Layout", self)
        reset_action.triggered.connect(self._reset_layout)
        view_menu.addAction(reset_action)

        # Thesis/report figures of the GUI itself: vector PDF/SVG + high-DPI PNG (also F12)
        from src.gui.figure_capture import add_capture_action, install_figure_capture
        view_menu.addSeparator()
        add_capture_action(view_menu, self)
        install_figure_capture(self)

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
                                "SINDIE Documentation Library coming soon...\n\nOr more likely 10 years time.")

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
        self.recipe_widget = RecipeExecutionWidget(self.cmd_thread)
        self.recipe_dock.setWidget(self.recipe_widget)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.recipe_dock)
        self.subsystems["recipe"] = self.recipe_widget

        self.optimiser_dock = QDockWidget("Optimiser", self)
        self.optimiser_dock.setObjectName("OptimiserDock")
        self.optimiser_widget = ParameterOptimizerWidget(self.cmd_thread, self.event_helper)
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

        self.current_mon_dock = QDockWidget("Current Monitoring", self)
        self.current_mon_dock.setObjectName("CurrentMonDock")
        self.current_mon_widget = AdamMonitorWidget(self.cmd_thread)
        self.current_mon_dock.setWidget(self.current_mon_widget)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.current_mon_dock)
        self.subsystems["current_mon"] = self.current_mon_widget

        self.mass_scan_dock = QDockWidget("Mass Scan", self)
        self.mass_scan_dock.setObjectName("MassScanDock")
        self.mass_scan_widget = PrecisionMassScannerWidget(self.cmd_thread, self.event_helper)
        # "Calculate & set amps" in the optics panel uses the mass scan's calibration
        self.optics_widget.mass_calibration = self.mass_scan_widget.current_calibration
        self.mass_scan_dock.setWidget(self.mass_scan_widget)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.mass_scan_dock)
        self.subsystems["mass_scan"] = self.mass_scan_widget

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

        # Alarms (service_alarms): list, acknowledge / shelve, and the horn
        self.alarm_dock = QDockWidget("Alarms", self)
        self.alarm_dock.setObjectName("AlarmDock")
        self.alarm_widget = AlarmPanel(self.cmd_thread)
        self.alarm_dock.setWidget(self.alarm_widget)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.alarm_dock)
        self.splitDockWidget(self.health_dock, self.alarm_dock, Qt.Orientation.Horizontal)
        self.subsystems["alarms"] = self.alarm_widget

        self.cmd_log_dock = QDockWidget("Command Log", self)
        self.cmd_log_dock.setObjectName("CommandLogDock")
        self.cmd_log_widget = CommandLogWidget()
        self.cmd_log_dock.setWidget(self.cmd_log_widget)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.cmd_log_dock)
        self.tabifyDockWidget(self.service_dock, self.cmd_log_dock)
        self.service_dock.raise_()

    def _route_telemetry(self, topic: str, fresh_data: dict):
        self.telemetry.ingest(topic, fresh_data)

    def _on_command_result(self, result: dict):
        """Every command outcome lands in the Command Log; problems also pop up briefly."""
        if result.get("origin") == FETCH_ORIGIN:
            return              # panels fetching scan points from the sequencer: not operator actions
        self.cmd_log_widget.add_result(result)
        status = result.get("status")
        if status in SUCCESS:
            return
        # Don't bury the operator: one pop-up per (command, outcome) every few seconds
        key = (result.get("subsystem"), result.get("tag"), status)
        now = time.time()
        if now - self._last_problem_shown.get(key, 0.0) < 5.0:
            return
        self._last_problem_shown[key] = now
        title = _PROBLEM_TITLES.get(status, "Command problem")
        origin = result.get("origin", "")
        content = describe(result) + (f"  (from {origin})" if origin and origin != "hmi" else "")
        try:
            InfoBar.warning(title, content, orient=Qt.Orientation.Horizontal, isClosable=True,
                            position=InfoBarPosition.TOP_RIGHT, duration=7000, parent=self) \
                if status in ("timeout", "unreachable") else \
                InfoBar.error(title, content, orient=Qt.Orientation.Horizontal, isClosable=True,
                              position=InfoBarPosition.TOP_RIGHT, duration=7000, parent=self)
        except Exception:  # a cosmetic pop-up must never take the HMI down
            log.exception("Could not show command result pop-up")

    def _ui_update_loop(self):
        # Fresh values only: anything from a silent service is left out, so widgets show '---'
        view = self.telemetry.view()
        self.master_telemetry_cache.clear()
        self.master_telemetry_cache.update(view)

        for widget in (self.vac_widget, self.src_widget, self.optics_widget, self.health_widget,
                       self.service_widget, self.log_widget, self.smu_widget, self.current_mon_widget,
                       self.optimiser_widget, self.mass_scan_widget, self.recipe_widget, self.automation_bar,
                       self.alarm_widget):
            try:
                widget.update_telemetry(self.master_telemetry_cache)
            except Exception:  # one widget's bug must not freeze every other panel
                log.exception("%s.update_telemetry failed", type(widget).__name__)


    def closeEvent(self, event):
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("windowState", self.saveState())
        self.telemetry_thread.stop()
        self.cmd_thread.stop()
        super().closeEvent(event)