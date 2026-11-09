import json
import logging
import os
import time
from PyQt6.QtWidgets import (QAbstractSpinBox, QApplication, QMainWindow, QDockWidget, QMessageBox, QInputDialog,
                             QLineEdit, QPlainTextEdit, QScrollArea, QFrame, QTabWidget, QTextEdit)
from PyQt6.QtCore import Qt, QTimer, QPoint, QByteArray
from PyQt6.QtGui import QAction, QGuiApplication

from qfluentwidgets import InfoBar, InfoBarPosition
from src.gui import theme
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
from src.gui.control.setpoint_box import SetpointBox

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
from src.gui.control.widgets.interlocks_widget import InterlocksWidget
from src.gui.control.widgets.services_widget import ServicesWidget
from src.gui.control.dialogs.config_editor_dialog import ConfigEditorDialog
from src.gui.control import wheel_guard
from src.gui.control.widgets.faraday_smu_widget import FaradaySMUWidget
from src.gui.control.widgets.optimiser_widget import ParameterOptimizerWidget
from src.gui.control.widgets.current_monitor_widget import AdamMonitorWidget
from src.gui.control.widgets.mass_scan import PrecisionMassScannerWidget
from src.gui.control.widgets.command_log_widget import CommandLogWidget
from src.gui.control.widgets.automation_bar import AutomationBar
from src.gui.control.widgets.alarm_widget import AlarmPanel
from src.gui.control.diagrams.vacuum import VacuumDiagram
from src.gui.control.diagrams.beamline import BeamPathDiagram
from src.gui.control.diagrams.safety import SafetyChainDiagram
from src.gui.control.diagrams.source import TessSourceDiagram
from src.gui.control.panel_views import DIAGRAM, LIST, VIEWS, DEFAULT_VIEW, PanelViews
from src.gui.help import pages as help_pages


def _takes_typing(widget) -> bool:
    """A box that takes what is typed (a setpoint box, a line edit, a text box), and isn't read-only."""
    return (isinstance(widget, (QLineEdit, QAbstractSpinBox, QTextEdit, QPlainTextEdit))
            and not widget.isReadOnly())


log = logging.getLogger("hmi")

# Command outcomes worth interrupting the operator for
_PROBLEM_TITLES = {"rejected": "Command refused", "failed": "Command failed",
                   "timeout": "No reply to command", "unreachable": "Service not running"}


class ControlMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SINDIE Operator HMI" + ("  [SIMULATION - fake devices]" if simulation_enabled() else ""))
        self.setObjectName("ControlMainWindow")
        # Panels can be tabbed together, nested side by side, and dragged as a whole tab group
        # (drag a tab group's title bar; drag one tab to take that panel out of the group)
        self.setDockOptions(QMainWindow.DockOption.AllowNestedDocks | QMainWindow.DockOption.AllowTabbedDocks
                            | QMainWindow.DockOption.AnimatedDocks | QMainWindow.DockOption.GroupedDragging)
        self.setTabPosition(Qt.DockWidgetArea.AllDockWidgetAreas, QTabWidget.TabPosition.North)

        self.settings = gui_settings("SurreyIonBeam", "SINDIE_HMI")
        # The mouse wheel must not change setpoint boxes (they send on focus-out): wheel_guard.py
        wheel_guard.install(QApplication.instance())

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
        self._restore_layout()

        self.watchdog_timer = QTimer(self)
        self.watchdog_timer.timeout.connect(self._ui_update_loop)
        self.watchdog_timer.start(100)

        # No control holds the keyboard focus when the HMI opens. Qt would give it to the first one
        # in the window - the gate valve's OPEN button, or a setpoint box - where a stray Space or
        # Enter would act on it. (The HMI sends nothing until the operator does something: the
        # services hold the machine's state, and the panels show it from their telemetry.) The window
        # keeps its NoFocus policy: clicking an empty part of it still leaves the focus where it is.
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        # ... nor after the focused control is locked or its panel goes away (see _keep_focus_here)
        self._focus_choice = None
        self._lost_keys_shown = 0.0             # (keyPressEvent: typing that went into no control)
        QApplication.instance().focusChanged.connect(self._keep_focus_here)
        for dock in self.findChildren(QDockWidget):
            dock.visibilityChanged.connect(lambda visible, d=dock: self._panel_shown(d) if visible
                                           else self._panel_gone(d))
        # Setpoint entries that were not sent, and why (setpoint_box.py)
        for box in self.findChildren(SetpointBox):
            box.notice.connect(self._show_notice)

    def _keep_focus_here(self, old, new):
        """Qt hands the keyboard to the next control when the focused one is locked (a relay trip
        locks a whole panel) or hidden: the operator's next keys - Space, digits and Enter - would
        then act on a control they never chose. Park it on the window instead. With the window in
        the background Qt moves it without a word, and the keyboard lands there when the operator
        comes back: then too.

        Never away from a control the operator has just clicked, though. Coming back to the HMI by
        clicking a setpoint box, after the control they had used last was locked or hidden while the
        HMI was in the background, used to park the keyboard on the window: the box showed the text
        they selected, and the value they typed and their Enter went nowhere, with no message
        (Josh, October 2026: the filament's "29")."""
        try:
            if new is None or new is self or not self.isAncestorOf(new):
                return
            pressed = wheel_guard.last_press_at(QApplication.instance())
            if pressed is not None and new.rect().contains(new.mapFromGlobal(pressed)):
                self._focus_choice = new        # the operator's choice: they just clicked it
                return
            if old is not None and self.isAncestorOf(old):
                if not old.isEnabled() or not old.isVisible():
                    self._park_focus()
                    return
            else:                       # back into the window
                chosen = self._focus_choice
                if chosen is not None and chosen is not new and (not chosen.isEnabled() or not chosen.isVisible()):
                    self._park_focus()
                    return
            self._focus_choice = new
        except RuntimeError:            # (a widget already deleted)
            self._focus_choice = None

    def _park_focus(self):
        self._focus_choice = None
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    def keyPressEvent(self, event):
        """A value typed, or Enter, that reaches the window went into no control: the keyboard was
        parked here (see _keep_focus_here), or the control holding it takes no text. Say so, rather
        than nothing happening.

        Not when the keyboard is with a box that takes typing: Qt passes Enter on to the window from
        a setpoint box (any spin box or line edit) once the box has used it - sent the value, added
        the marker - so that a dialog's default button can answer it too. That Enter went somewhere:
        saying "Not sent" after every value sent was wrong (Josh, 30 October 2026)."""
        text = event.text()
        typed = (text and (text.isdigit() or text in ".,-")) or event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        if typed and not _takes_typing(QApplication.focusWidget()):
            now = time.monotonic()
            if now - self._lost_keys_shown > 5.0:
                self._lost_keys_shown = now
                self._show_notice("What you typed went into no setpoint box: none had the keyboard. Click in "
                                  "the box, then type the value and press Enter.")
        super().keyPressEvent(event)

    def _panel_gone(self, dock, why: str = "its panel was closed or switched away before you pressed Enter"):
        """A dock closed or switched away (Qt keeps a switched-away tab's widgets, and their focus,
        off-screen), or one view of a panel switched to the other (then `dock` is that view's page):
        an unsent entry in it is dropped, and the keyboard comes back to the window."""
        for box in dock.findChildren(SetpointBox):
            if box.editing:
                box.drop_edit(why)
        focused = QApplication.focusWidget()
        if focused is not None and dock.isAncestorOf(focused):
            self._park_focus()

    def _panel_shown(self, dock):
        """A panel came into view (opened, its tab chosen, or switched to its other view): bring
        what it shows up to date at once - hidden panels aren't refreshed (see _ui_update_loop)."""
        widget = self.shown_in(dock)
        if not self.master_telemetry_cache:
            return              # no telemetry yet: nothing to bring it up to (the next refresh does)
        if widget is not None and widget not in self._always_refresh:
            # (a diagram: the panels it draws from first, as they weren't refreshed while hidden)
            for w in [p for p in getattr(widget, "watched", []) if p not in self._always_refresh] + [widget]:
                try:
                    w.update_telemetry(self.master_telemetry_cache)
                except Exception:
                    log.exception("%s.update_telemetry failed", type(w).__name__)

    def _open_program(self, program: str):
        """The data viewer (or another SINDIE program) to the front, or started (src/launcher/instance.py)."""
        from src.launcher.instance import open_or_raise
        problem = open_or_raise(program)
        if problem:
            QMessageBox.warning(self, "SINDIE", problem)

    def _show_notice(self, message: str):
        taken = " was sent, but " in message          # (sent, and the setpoint in use didn't change)
        log.info("Setpoint %s: %s", "not taken" if taken else "not sent", message)
        try:
            InfoBar.warning("Not taken" if taken else "Not sent", message, orient=Qt.Orientation.Horizontal,
                            isClosable=True, position=InfoBarPosition.TOP_RIGHT, duration=8000, parent=self)
        except Exception:  # a cosmetic pop-up must never take the HMI down
            log.exception("Could not show the setpoint notice")

    def _setup_menus(self):
        menubar = self.menuBar()

        # 1. VIEW MENU: panels, layouts, theme
        self.view_menu = view_menu = menubar.addMenu("View")
        panels = view_menu.addMenu("Panels")
        for dock in sorted(self._docks, key=lambda d: d.windowTitle().replace("&&", "&")):
            panels.addAction(dock.toggleViewAction())

        self.layouts_menu = view_menu.addMenu("Layouts")
        self._rebuild_layouts_menu()
        reset_action = QAction("Reset layout", self)
        reset_action.setToolTip("Put every panel back where it starts, sized for this screen")
        reset_action.triggered.connect(self._reset_layout)
        view_menu.addAction(reset_action)
        self._reset_action = reset_action

        view_menu.addSeparator()
        theme.add_theme_menu(view_menu, self)
        view_menu.addSeparator()
        self.open_viewer_action = view_menu.addAction("Open the data viewer")
        self.open_viewer_action.setToolTip("Brings the data viewer to the front, or starts it")
        self.open_viewer_action.triggered.connect(lambda: self._open_program("viewer"))

        # Thesis/report figures of the GUI itself: vector PDF/SVG + high-DPI PNG (also F12)
        from src.gui.figure_capture import add_capture_action, install_figure_capture
        view_menu.addSeparator()
        add_capture_action(view_menu, self)
        install_figure_capture(self)

        # 2. CONFIG EDITOR
        self._settings_action = QAction("Config", self)
        self._settings_action.triggered.connect(self._show_config_editor)
        menubar.addAction(self._settings_action)

        # 3. HELP: the pages in docs/; F1 opens the page of the panel under the mouse pointer
        from src.gui.help import add_help_menu, pages as help_pages
        self.help_menu = add_help_menu(self, menubar, default=help_pages.HOME, entries=[
            ("Safety rules", help_pages.SAFETY),
            ("Bringing up a beam", help_pages.START_UP),
            ("Shutting down", "procedures/beam-shut-down.md"),
            ("Alarms and faults", "procedures/alarms-and-faults.md"),
            (None, None),
            ("Troubleshooting", "troubleshooting.md"),
            ("Messages and what they mean", "messages.md"),
        ])


    # --- Layout: the panels' arrangement and the window's place on the screen ------------------
    # Saved arrangements from before this version (every panel open, the window too big for a
    # laptop screen) are not restored: the default below is used once instead.
    LAYOUT_VERSION = 2

    def _arrange_default(self):
        """Four panels in view - Vacuum over System Health on the left; the source tab group over
        the Alarms tab group on the right - and every other panel a tab behind them."""
        for dock in self._docks:
            dock.setFloating(False)
            self.removeDockWidget(dock)
        L, R = Qt.DockWidgetArea.LeftDockWidgetArea, Qt.DockWidgetArea.RightDockWidgetArea
        self.addDockWidget(L, self.vac_dock)
        self.addDockWidget(L, self.health_dock)
        self.splitDockWidget(self.vac_dock, self.health_dock, Qt.Orientation.Vertical)
        self.tabifyDockWidget(self.health_dock, self.interlocks_dock)
        self.addDockWidget(R, self.src_dock)
        self.addDockWidget(R, self.alarm_dock)
        self.splitDockWidget(self.src_dock, self.alarm_dock, Qt.Orientation.Vertical)
        previous = self.src_dock
        for dock in (self.optics_dock, self.smu_dock, self.mass_scan_dock, self.optimiser_dock,
                     self.current_mon_dock):
            self.tabifyDockWidget(previous, dock)
            previous = dock
        previous = self.alarm_dock
        for dock in (self.recipe_dock, self.log_dock, self.cmd_log_dock, self.service_dock):
            self.tabifyDockWidget(previous, dock)
            previous = dock
        for dock in self._docks:
            dock.show()
        self.vac_dock.raise_()
        self.health_dock.raise_()
        self.src_dock.raise_()
        self.alarm_dock.raise_()
        QTimer.singleShot(0, self._size_default_docks)

    def _size_default_docks(self):
        w, h = max(self.width(), 800), max(self.height(), 500)
        self.resizeDocks([self.vac_dock, self.src_dock], [int(w * 0.4), int(w * 0.6)], Qt.Orientation.Horizontal)
        self.resizeDocks([self.vac_dock, self.health_dock], [int(h * 0.52), int(h * 0.48)], Qt.Orientation.Vertical)
        self.resizeDocks([self.src_dock, self.alarm_dock], [int(h * 0.62), int(h * 0.38)], Qt.Orientation.Vertical)

    def _restore_layout(self):
        geometry = self.settings.value("geometry")
        if isinstance(geometry, QByteArray) and not geometry.isEmpty():
            self.restoreGeometry(geometry)
        else:
            self._fit_to_screen(force=True)
        state = self.settings.value("windowState")
        if not (isinstance(state, QByteArray) and not state.isEmpty()
                and self.restoreState(state, self.LAYOUT_VERSION)):
            self._arrange_default()
        else:
            self._place_new_panels()
        self._fit_to_screen()

    def _place_new_panels(self):
        """A panel added since the layout was saved (the saved layout doesn't place it): a tab behind
        System Health, where it can be found, rather than nowhere. (The diagrams' own panels of
        before November 2026, in a layout saved then, are left out: each is now a view of its
        panel.)"""
        for dock in self._docks:
            if self.dockWidgetArea(dock) == Qt.DockWidgetArea.NoDockWidgetArea and not dock.isFloating():
                anchor = self.health_dock if dock is not self.health_dock else self.vac_dock
                if self.dockWidgetArea(anchor) == Qt.DockWidgetArea.NoDockWidgetArea:
                    self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
                else:
                    self.tabifyDockWidget(anchor, dock)
                    dock.show()                 # (a tab that is there, not one hidden from the start)
                    anchor.raise_()

    def _fit_to_screen(self, force: bool = False):
        """Keep the window on a screen that exists and no bigger than it (a layout saved on a big
        monitor, or a monitor since unplugged). force: size it for this screen afresh."""
        if self.isMaximized() and not force:
            return
        frame = self.frameGeometry()
        screen = QGuiApplication.screenAt(frame.center()) or self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return
        avail = screen.availableGeometry()
        fits = (avail.contains(frame.center()) and frame.width() <= avail.width()
                and frame.height() <= avail.height())
        if fits and not force:
            return
        w = min(int(avail.width() * 0.94), 1680)
        h = min(int(avail.height() * 0.92), 1050)
        self.resize(w, h)
        self.move(avail.center() - QPoint(w // 2, h // 2))

    def _reset_layout(self):
        self._fit_to_screen(force=True)
        self._arrange_default()

    def _saved_layouts(self) -> list:
        self.settings.beginGroup("layouts")
        names = sorted(self.settings.childKeys(), key=str.lower)
        self.settings.endGroup()
        return names

    def _rebuild_layouts_menu(self):
        menu = self.layouts_menu
        menu.clear()
        save = menu.addAction("Save layout as...")
        save.triggered.connect(self._save_layout_as)
        names = self._saved_layouts()
        if names:
            menu.addSeparator()
            for name in names:
                act = menu.addAction(name)
                act.triggered.connect(lambda _=False, n=name: self._load_layout(n))
            delete = menu.addMenu("Delete layout")
            for name in names:
                act = delete.addAction(name)
                act.triggered.connect(lambda _=False, n=name: self._delete_layout(n))

    def _save_layout_as(self):
        name, ok = QInputDialog.getText(self, "Save layout", "Name for this arrangement of panels:")
        name = (name or "").strip().replace("/", "-").replace("\\", "-")
        if not ok or not name:
            return
        self.settings.setValue(f"layouts/{name}", self.saveState(self.LAYOUT_VERSION))
        self.settings.setValue(f"layout_views/{name}", json.dumps(self.views()))
        self._rebuild_layouts_menu()

    def _load_layout(self, name: str):
        state = self.settings.value(f"layouts/{name}")
        if not (isinstance(state, QByteArray) and self.restoreState(state, self.LAYOUT_VERSION)):
            QMessageBox.warning(self, "Layout", f"The layout '{name}' could not be restored "
                                                f"(saved by an older version?).")
        else:
            self._place_new_panels()
            self._show_views(self.settings.value(f"layout_views/{name}"))   # (none in a layout saved before them)
        self._fit_to_screen()

    def _delete_layout(self, name: str):
        self.settings.remove(f"layouts/{name}")
        self.settings.remove(f"layout_views/{name}")
        self._rebuild_layouts_menu()

    def _show_config_editor(self):
        pwd, ok = QInputDialog.getText(self, "Authentication Required", "Enter Engineering Password:",
                                       QLineEdit.EchoMode.Password)
        if ok and pwd == "admin":
            editor = ConfigEditorDialog(
                self.cmd_thread, self, events=self.event_helper,
                services=lambda: dict(self.master_telemetry_cache.get("manager.services") or {}))
            editor.exec()
        elif ok:
            QMessageBox.warning(self, "Access Denied", "Incorrect password.")

    def _scroll(self, widget):
        """widget in a scroll area: a panel's contents scroll when the panel is made smaller than
        they are, so a panel fits wherever it is dropped and the window fits a laptop screen."""
        scroll = QScrollArea()
        scroll.setObjectName("PanelScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        # A scroll area takes the keyboard focus on a click (StrongFocus): a click on an empty part
        # of a panel would move it there - and a typed setpoint not yet entered counts as "moved on
        # to another control" and is sent. Empty space must leave the focus where it is.
        scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        scroll.setWidget(widget)
        return scroll

    def _panel(self, title: str, object_name: str, widget, diagram=None):
        """A dockable panel. With a diagram, the panel can be shown either way, as its list or as
        the diagram, with a List | Diagram switch at its top (panel_views.py): one dock, and one
        entry in View > Panels."""
        dock = QDockWidget(title, self)
        dock.setObjectName(object_name)
        if diagram is None:
            dock.setWidget(self._scroll(widget))
        else:
            diagram_page = self._scroll(diagram)
            diagram_page.setProperty("help_page", help_pages.DIAGRAMS)       # (F1 there: the diagrams' page)
            views = PanelViews(self._scroll(widget), diagram_page)
            views.show_view(self._saved_view(object_name))
            views.switched.connect(lambda view, d=dock: self._view_switched(d, view))
            dock.setWidget(views)
            self._views[dock] = views
            self._diagram_of[dock] = diagram
        self._docks.append(dock)
        self._panel_widgets[dock] = widget
        return dock

    def _init_docks(self):
        self._docks = []
        self._panel_widgets = {}
        self._views = {}            # dock -> PanelViews, for a panel that can also be shown as a diagram
        self._diagram_of = {}       # ... and its diagram

        # The panels
        self.vac_widget = VacuumControlWidget(self.cmd_thread, self.fault_engine)
        self.subsystems["vacuum"] = self.vac_widget
        self.log_widget = ExperimentLoggerWidget(self.event_helper)
        self.subsystems["logging"] = self.log_widget
        self.recipe_widget = RecipeExecutionWidget(self.cmd_thread)
        self.subsystems["recipe"] = self.recipe_widget
        self.optimiser_widget = ParameterOptimizerWidget(self.cmd_thread, self.event_helper)
        self.subsystems["optimiser"] = self.optimiser_widget
        self.src_widget = IonSourceWidget(self.cmd_thread, self.fault_engine)
        self.subsystems["source"] = self.src_widget
        self.optics_widget = BeamlineOpticsWidget(self.cmd_thread, self.event_helper)
        self.subsystems["optics"] = self.optics_widget
        self.smu_widget = FaradaySMUWidget(self.cmd_thread)
        self.subsystems["faraday_smu"] = self.smu_widget
        self.current_mon_widget = AdamMonitorWidget(self.cmd_thread)
        self.subsystems["current_mon"] = self.current_mon_widget
        self.mass_scan_widget = PrecisionMassScannerWidget(self.cmd_thread, self.event_helper)
        # "Calculate & set amps" in the optics panel uses the mass scan's calibration
        self.optics_widget.mass_calibration = self.mass_scan_widget.current_calibration
        self.subsystems["mass_scan"] = self.mass_scan_widget
        self.health_widget = SystemHealthWidget(self.cmd_thread, self.fault_engine)
        self.subsystems["health"] = self.health_widget
        # Every interlock and what is holding it now (read only)
        self.interlocks_widget = InterlocksWidget(self.fault_engine)
        self.subsystems["interlocks"] = self.interlocks_widget
        self.service_widget = ServicesWidget(self.cmd_thread)
        self.subsystems["services"] = self.service_widget
        # Alarms (service_alarms): list, acknowledge / shelve, and the horn
        self.alarm_widget = AlarmPanel(self.cmd_thread)
        self.subsystems["alarms"] = self.alarm_widget
        self.cmd_log_widget = CommandLogWidget()

        # The machine diagrams (a test, November 2026): the machine drawn as it is laid out, live, from
        # the panels above - a part's faceplate has its panel's own controls (src/gui/control/diagrams).
        # Each is the other view of the panel it draws most from (List | Diagram, panel_views.py).
        self.vac_diagram = VacuumDiagram({"vacuum": self.vac_widget}, self.show_panel)
        self.beam_diagram = BeamPathDiagram(
            {"source": self.src_widget, "optics": self.optics_widget, "vacuum": self.vac_widget,
             "faraday": self.smu_widget, "currents": self.current_mon_widget}, self.show_panel)
        self.safety_diagram = SafetyChainDiagram(
            {"health": self.health_widget, "interlocks": self.interlocks_widget, "vacuum": self.vac_widget},
            self.show_panel)
        self.source_diagram = TessSourceDiagram({"source": self.src_widget, "interlocks": self.interlocks_widget},
                                                self.show_panel)
        self.diagrams = [self.vac_diagram, self.beam_diagram, self.safety_diagram, self.source_diagram]
        for diagram in self.diagrams:
            diagram.notice.connect(self._show_notice)       # (a faceplate's setpoint entry not sent)

        # Their docks
        self.vac_dock = self._panel("Vacuum System", "VacuumDock", self.vac_widget, self.vac_diagram)
        self.log_dock = self._panel("Experiment Logging", "LogDock", self.log_widget)
        self.recipe_dock = self._panel("Recipe Execution", "RecipeDock", self.recipe_widget)
        self.optimiser_dock = self._panel("Optimiser", "OptimiserDock", self.optimiser_widget)
        self.src_dock = self._panel("TESS Ion Source Control", "SourceDock", self.src_widget, self.source_diagram)
        self.optics_dock = self._panel("Beamline Optics", "OpticsDock", self.optics_widget, self.beam_diagram)
        self.smu_dock = self._panel("Faraday Cup && SMU", "FaradaySmuDock", self.smu_widget)
        self.current_mon_dock = self._panel("Current Monitoring", "CurrentMonDock", self.current_mon_widget)
        self.mass_scan_dock = self._panel("Mass Scan", "MassScanDock", self.mass_scan_widget)
        self.health_dock = self._panel("System Health", "HealthDock", self.health_widget)
        self.interlocks_dock = self._panel("Interlocks", "InterlocksDock", self.interlocks_widget,
                                           self.safety_diagram)
        self.service_dock = self._panel("Service Manager", "ServicesDock", self.service_widget)
        self.alarm_dock = self._panel("Alarms", "AlarmDock", self.alarm_widget)
        self.cmd_log_dock = self._panel("Command Log", "CommandLogDock", self.cmd_log_widget)

        # Refreshed even while hidden: the alarm horn, the automation bar, and the panels that
        # follow a running sequence (operator prompts, scan points fetched as they come)
        self._always_refresh = {self.automation_bar, self.alarm_widget, self.recipe_widget,
                                self.mass_scan_widget, self.optimiser_widget}
        self._refreshed = [self.vac_widget, self.src_widget, self.optics_widget, self.health_widget,
                           self.interlocks_widget, self.service_widget, self.log_widget, self.smu_widget, self.current_mon_widget,
                           self.optimiser_widget, self.mass_scan_widget, self.recipe_widget,
                           self.automation_bar, self.alarm_widget] + self.diagrams     # (after their panels)
        self._dock_of = {w: d for d, w in self._panel_widgets.items()}
        self._dock_of.update({diagram: d for d, diagram in self._diagram_of.items()})

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

        minimised = self.isMinimized()
        # The panels a diagram in view draws from are kept up to date, their own lists hidden or not
        drawn_from = set() if minimised else {w for d in self.diagrams if self.in_view(d) for w in d.watched}
        for widget in self._refreshed:
            if widget not in self._always_refresh and widget not in drawn_from:
                # A closed panel, one behind another tab, or the view of a panel not shown, isn't
                # redrawn 10 times a second for nobody: it catches up the moment it is shown
                # (_panel_shown)
                if minimised or not self.in_view(widget):
                    continue
            try:
                widget.update_telemetry(self.master_telemetry_cache)
            except Exception:  # one widget's bug must not freeze every other panel
                log.exception("%s.update_telemetry failed", type(widget).__name__)


    def show_panel(self, name: str):
        """A panel to the front, opened if it was closed (a diagram's faceplate links to its panel):
        as its list, or as its diagram for "<name>_diagram" ("source_diagram")."""
        name, view = (name[:-len("_diagram")], DIAGRAM) if name.endswith("_diagram") else (name, LIST)
        dock = {"vacuum": self.vac_dock, "source": self.src_dock, "optics": self.optics_dock,
                "faraday": self.smu_dock, "currents": self.current_mon_dock, "health": self.health_dock,
                "interlocks": self.interlocks_dock}.get(name)
        if dock is not None:
            if dock in self._views:
                self._views[dock].show_view(view)
            dock.show()
            dock.raise_()

    # --- A panel's two views: List | Diagram (panel_views.py) ---------------------------------------
    def _saved_view(self, object_name: str) -> str:
        view = self.settings.value(f"views/{object_name}", DEFAULT_VIEW)
        return view if view in VIEWS else DEFAULT_VIEW

    def views(self) -> dict:
        """{dock name: the view it shows} for the panels with two."""
        return {dock.objectName(): v.view() for dock, v in self._views.items()}

    def _show_views(self, saved):
        """The views of a named layout ({dock name: view}, as JSON)."""
        try:
            wanted = json.loads(saved) if isinstance(saved, str) else {}
        except ValueError:
            wanted = {}
        for dock, v in self._views.items():
            if isinstance(wanted, dict) and wanted.get(dock.objectName()) in VIEWS:
                v.show_view(wanted[dock.objectName()])

    def _view_switched(self, dock, view: str):
        """The operator chose a panel's other view: remembered for the next start; the view switched
        away from lets go of an unsent entry and the keyboard; the one shown is brought up to date."""
        self.settings.setValue(f"views/{dock.objectName()}", view)
        other = LIST if view == DIAGRAM else DIAGRAM
        self._panel_gone(self._views[dock].page(other),
                         f"its panel was switched to its {'diagram' if view == DIAGRAM else 'list'} "
                         f"before you pressed Enter")
        if self.on_screen(dock):
            self._panel_shown(dock)

    def shown_in(self, dock):
        """What a dock shows: its panel's widget, or the diagram when that view is chosen."""
        views = self._views.get(dock)
        if views is not None and views.view() == DIAGRAM:
            return self._diagram_of[dock]
        return self._panel_widgets.get(dock)

    def in_view(self, widget) -> bool:
        """Whether a panel's widget, or a diagram, can be seen: its dock on screen and showing it."""
        dock = self._dock_of.get(widget)
        if dock is None:
            return True
        return self.on_screen(dock) and self.shown_in(dock) is widget

    @staticmethod
    def on_screen(dock) -> bool:
        """Whether a panel can be seen: open, and not behind another panel's tab. (Qt leaves a tab
        that isn't chosen 'visible', just with nothing of it showing.)"""
        return dock.isVisible() and not dock.visibleRegion().isEmpty()

    def closeEvent(self, event):
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("windowState", self.saveState(self.LAYOUT_VERSION))
        self.telemetry_thread.stop()
        self.cmd_thread.stop()
        super().closeEvent(event)