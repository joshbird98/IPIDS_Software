"""Take the screenshots for the help pages (docs/images/) from the simulator, with no windows on
the screen.

    python src/utils/make_help_screenshots.py              about four minutes
    python src/utils/make_help_screenshots.py --theme light --out somewhere/else

It starts the simulated services on their own ports and in a temporary data folder (so it is safe
on the lab PC next to the running system), opens the HMI and the data viewer offscreen on a
1536 x 864 screen, and brings the simulated machine up as an operator would: gate valve, safety
relay, extraction, target, einzel, caesium oven, filament and thermionic, the magnet on Si-28 and
the beam on the Faraday cup. It runs a mass scan, then saves each panel, the whole HMI and the data
viewer (both with numbered markers that the screen-tour pages explain), and some dialogs.

Re-run it after changing what a panel looks like, then check the pages that show it.
"""
import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PORT_OFFSET = 34000
SCREEN = {"synchronousWindowSystemEvents": False, "windowFrameMargins": False,
          "screens": [{"name": "Laptop", "x": 0, "y": 0, "width": 1536, "height": 864,
                       "logicalDpi": 96, "logicalBaseDpi": 96, "dpr": 1}]}

# The size each panel is saved at (floating, as if pulled out of the window): wide enough for its
# contents without scrolling. object name -> (file name, width, height)
PANELS = {
    "VacuumDock": ("panel-vacuum", 680, 450),
    "HealthDock": ("panel-system-health", 680, 470),
    "SourceDock": ("panel-ion-source", 780, 480),
    "OpticsDock": ("panel-beamline-optics", 780, 700),
    "FaradaySmuDock": ("panel-faraday-cup-smu", 700, 560),
    "CurrentMonDock": ("panel-current-monitoring", 800, 330),
    "MassScanDock": ("panel-mass-scan", 1180, 600),
    "OptimiserDock": ("panel-optimiser", 800, 640),
    "AlarmDock": ("panel-alarms", 820, 330),
    "RecipeDock": ("panel-recipe-execution", 700, 460),
    "LogDock": ("panel-experiment-logging", 700, 330),
    "CommandLogDock": ("panel-command-log", 900, 340),
    "ServicesDock": ("panel-service-manager", 600, 560),
}
MARKER_FILL = "#D6336C"         # the numbered markers on the overview pictures


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "images"), help="folder for the pictures")
    ap.add_argument("--theme", default="dark", choices=("dark", "light"))
    ap.add_argument("--port-offset", type=int, default=PORT_OFFSET)
    args = ap.parse_args()

    work = tempfile.mkdtemp(prefix="sindie_help_shots_")
    screen_file = os.path.join(work, "screen.json")
    with open(screen_file, "w") as f:
        import json
        json.dump(SCREEN, f)
    os.environ.update(SINDIE_SIMULATE="1", SINDIE_PORT_OFFSET=str(args.port_offset),
                      SINDIE_DATA_DIR=os.path.join(work, "data"), SINDIE_SETTINGS_DIR=os.path.join(work, "settings"),
                      QT_QPA_PLATFORM="offscreen")
    # (nothing from src is imported yet: paths.py reads SINDIE_REGISTRY once, at import, and the
    # registry is only built by run_simulation.py below)
    manager_lock_port = 65432 - (args.port_offset % 60000)          # as src/core/network_map.py
    probe = socket.socket()
    try:
        probe.bind(("localhost", manager_lock_port))
    except OSError:
        sys.exit(f"Port offset {args.port_offset} is in use (another run of this script?). Use --port-offset.")
    finally:
        probe.close()
    print(f"Simulated services starting (data in {work}) ...")
    services = subprocess.Popen(
        [sys.executable, "run_simulation.py", "--no-hmi", "--no-viewer", "--port-offset", str(args.port_offset)],
        cwd=ROOT, stdin=subprocess.PIPE, stdout=open(os.path.join(work, "services.log"), "w"),
        stderr=subprocess.STDOUT, text=True, env=dict(os.environ, PYTHONPATH=ROOT))
    try:
        registry = os.path.join(work, "data", "system_tags.json")
        end = time.time() + 60
        while not os.path.exists(registry) and time.time() < end and services.poll() is None:
            time.sleep(0.5)
        os.environ["SINDIE_REGISTRY"] = registry
        os.environ["QT_QPA_PLATFORM"] = "offscreen:configfile=" + screen_file
        sys.path.insert(0, ROOT)
        Shooter(args.out, args.theme).run()
        code = 0
    except Exception:
        import traceback
        traceback.print_exc()
        code = 1
    finally:
        try:
            services.stdin.write("q\n")
            services.stdin.flush()
            services.wait(90)
        except (OSError, subprocess.TimeoutExpired):
            services.kill()
        shutil.rmtree(work, ignore_errors=True)
    # Leave without Python's clean-up: Qt's teardown at exit can crash once the windows have gone,
    # and the windows' saved settings would put the temporary folder back
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


class Shooter:
    def __init__(self, out, mode):
        from PyQt6.QtWidgets import QApplication
        self.app = QApplication(sys.argv)
        from src.gui import theme
        theme.install(self.app, mode=mode, follow=False)
        self.out = out
        os.makedirs(out, exist_ok=True)
        self.saved = []

    # --- helpers --------------------------------------------------------------------------------
    def spin(self, seconds):
        from PyQt6.QtCore import QEventLoop, QTimer
        loop = QEventLoop()
        QTimer.singleShot(int(seconds * 1000), loop.quit)
        loop.exec()

    def wait(self, test, timeout, what):
        end = time.time() + timeout
        while time.time() < end:
            self.spin(0.1)
            try:
                if test():
                    return True
            except Exception:
                pass
        print(f"  (gave up waiting for {what})")
        return False

    def send(self, tag, value):
        from src.core.commands import route_for_tag
        self.hmi.cmd_thread.send_command(route_for_tag(tag), tag, value)

    def unfocus(self, within=None):
        """No focus ring or text cursor on the pictures: a panel pulled out into a window of its
        own gets back that window's last focused box when it is shown, so clear that too."""
        from PyQt6.QtWidgets import QAbstractButton, QAbstractSpinBox, QApplication, QComboBox, QLineEdit
        focused = QApplication.focusWidget()
        if focused is not None:
            focused.clearFocus()
        if within is not None:
            for kind in (QAbstractSpinBox, QLineEdit, QComboBox, QAbstractButton):
                for w in within.findChildren(kind):
                    if w.hasFocus() or w is within.focusWidget():
                        w.clearFocus()
            self.app.processEvents()

    def save(self, image, name):
        path = os.path.join(self.out, name + ".png")
        image.save(path, "PNG", 0)
        self.saved.append(name)
        print(f"  {name}.png  {image.width()} x {image.height()}")

    # --- the run --------------------------------------------------------------------------------
    def run(self):
        from src.gui.control.control_main import ControlMainWindow
        self.hmi = hmi = ControlMainWindow()
        hmi.show()
        hmi.resize(1536, 864)
        self.view = view = hmi.master_telemetry_cache
        print("Bringing the simulated machine up ...")
        if not self.wait(lambda: view.get("system.connected") is True and
                         all(s == "ONLINE" for s in (view.get("manager.services") or {"x": 0}).values()),
                         90, "every service ONLINE"):
            print("  carrying on anyway")
        self.send("ion_beam.source.turbo_pump.cmd_enable", True)      # (runs up in ~40 s, as on the machine)
        self.send("ion_beam.source.chamber.cmd_open_gv", True)
        self.wait(lambda: view.get("ion_beam.source.chamber.stat_gv_open") == 1.0, 30, "gate valve open")
        self.wait(hmi.health_widget.btn_enable_safety.isEnabled, 10, "ENABLE SAFETY RELAY")
        hmi.health_widget.btn_enable_safety.click()     # (the simulated PLC "presses" the panel's START)
        self.wait(lambda: view.get("ion_beam.facilities.safety_relay_active") == 1.0, 20, "safety relay on")
        for tag, value in (("ion_beam.source.extraction.sp_requested_voltage", 15.0),
                           ("ion_beam.source.target.sp_requested_voltage", 5.0)):
            self.send(tag, value)
        self.spin(1.0)
        for tag in ("ion_beam.source.extraction.cmd_enable", "ion_beam.source.target.cmd_enable"):
            self.send(tag, True)
        self.wait(lambda: abs((view.get("ion_beam.source.target.rb_voltage") or 0) - 5.0) < 0.1, 20, "target at 5 kV")

        self.shoot_arming()

        for tag, value in (("ion_beam.source.einzel.sp_requested_voltage", 10.0),
                           ("ion_beam.source.cesium.sp_requested_temp", 150.0),
                           ("ion_beam.source.filament.sp_requested_current", 28.8),
                           ("ion_beam.source.thermionic.sp_requested_current", 125.0)):
            self.send(tag, value)
        self.spin(1.0)
        for tag, value in (("ion_beam.source.einzel.cmd_enable", True), ("ion_beam.source.cesium.cmd_ctrl_mode", 1),
                           ("ion_beam.source.filament.cmd_enable", True),
                           ("ion_beam.source.thermionic.cmd_enable", True),
                           ("ion_beam.beamline.magnet.cmd_enable", True)):
            self.send(tag, value)
        self.wait(lambda: (view.get("ion_beam.source.thermionic.rb_power") or 0) > 210
                  and (view.get("ion_beam.source.cesium.rb_temp") or 0) > 55, 90, "source running")
        self.put_mass_on_the_cup(28.0)
        hmi.event_helper.log_phase("Silicon implant run 4")
        self.spin(1.0)
        hmi.event_helper.log_user_marker(time.time(), "Beam tuned on Si-28", "#FFFFFF")

        self.open_viewer()
        self.load_recipe("test_sequence.json")
        self.run_mass_scan()
        self.put_mass_on_the_cup(28.0)
        self.spin(8.0)

        self.shoot_overview()
        self.shoot_viewer()
        self.shoot_panels()
        self.shoot_dialogs()
        self.shoot_alarm()
        hmi.close()
        self.viewer.close()
        self.cache.stop()
        print(f"Saved {len(self.saved)} pictures in {self.out}")

    def put_mass_on_the_cup(self, amu):
        opt = self.hmi.optics_widget
        self.hmi.optics_dock.raise_()
        self.wait(opt.btn_calc_mass.isEnabled, 10, "CALCULATE & SET AMPS")
        opt.sp_mass.setValue(amu)
        opt.btn_calc_mass.click()
        self.wait(lambda: (self.view.get("ion_beam.beamline.faraday.smu.rb_current") or 0) > 50e-9, 30,
                  f"mass {amu:g} beam on the cup")
        self.hmi.src_dock.raise_()

    def load_recipe(self, name):
        w = self.hmi.recipe_widget
        w.select_recipe(name)
        w.btn_load.click()
        self.spin(0.5)

    def run_mass_scan(self):
        from PyQt6.QtCore import QRect
        from PyQt6.QtWidgets import QWidget
        hmi, view = self.hmi, self.view
        w = hmi.mass_scan_widget
        # 11.5 to 29 A at 20 keV: C, O, OH, C2, the three silicon isotopes and Si2 (the simulated
        # sputter source's peaks); a few minutes
        for box, value in ((w.sp_start, 11.5), (w.sp_stop, 29.0), (w.sp_step, 0.05), (w.sp_settle, 0.05),
                           (w.sp_samples, 2), (w.sp_interval, 0.02), (w.sp_nplc, 1.0)):
            box.setValue(value)
        w.chk_loop.setChecked(False)
        w.chk_export.setChecked(False)
        hmi.mass_scan_dock.raise_()
        if not self.wait(w.btn_start.isEnabled, 15, "mass scan START"):
            return
        w.btn_start.click()
        if self.wait(lambda: view.get("sequencer.mass_scan.state") == "running", 15, "scan running"):
            self.spin(3.0)
            bar = hmi.automation_bar
            right = max(w.geometry().right() for w in bar.findChildren(QWidget) if w.isVisible()
                        and w.parentWidget() is not None)
            self.save(bar.grab(QRect(0, 0, min(bar.width(), right + 16), bar.height())), "automation-bar")
        print("  mass scan running (a minute or two) ...")
        if not self.wait(lambda: view.get("sequencer.mass_scan.state") == "complete", 600, "scan complete"):
            w.btn_stop.click()
            self.wait(lambda: view.get("sequencer.mass_scan.state") != "running", 20, "scan stopped")
        self.spin(2.0)

    # --- pictures -------------------------------------------------------------------------------
    def shoot_arming(self):
        src = self.hmi.src_widget
        self.hmi.src_dock.raise_()
        self.spin(0.5)
        button = src.psu_controls["Source Einzel"]["btn_enable"]
        self.wait(button.isEnabled, 10, "einzel ENABLE")
        button.click()                                   # first click only: it waits for the second
        self.spin(0.4)
        panel = self.hmi.src_dock
        image = panel.grab().toImage()
        top = button.mapTo(panel, button.rect().topLeft()).y()
        from PyQt6.QtCore import QRect
        crop = QRect(0, max(0, top - 52), image.width(), min(image.height(), button.height() + 104))
        self.save(image.copy(crop), "arming")
        self.spin(5.0)                                   # let it lapse

    def shoot_overview(self):
        hmi = self.hmi
        hmi.showNormal()
        hmi._reset_layout()
        hmi.resize(1536, 864)
        self.unfocus()
        self.spin(2.0)
        image = hmi.grab().toImage()
        marks = []
        bar = hmi.menuBar()
        help_rect = bar.actionGeometry(hmi.help_menu.menuAction())
        marks.append(self._at(bar, help_rect.right() + 18, help_rect.center().y(), hmi))
        auto = hmi.automation_bar
        marks.append(self._at(auto, auto.lbl_idle.geometry().right() + 20, auto.height() // 2, hmi))
        for dock in (hmi.vac_dock, hmi.health_dock):
            text_w = dock.fontMetrics().horizontalAdvance(dock.windowTitle())
            marks.append(self._at(dock, text_w + 36, 13, hmi))
        for title in ("TESS Ion Source Control", "Alarms"):
            tabs = self._tab_bar_with(title)
            if tabs is not None:
                r = tabs.tabRect(tabs.count() - 1)
                marks.append(self._at(tabs, r.right() + 20, r.center().y(), hmi))
        dock = hmi.vac_dock
        marks.append(self._at(dock, dock.width() - 58, 13, hmi))
        self.save(self._mark(image, marks), "hmi-overview")

    def _tab_bar_with(self, title):
        from PyQt6.QtWidgets import QTabBar
        for tabs in self.hmi.findChildren(QTabBar):
            if tabs.isVisible() and any(tabs.tabText(i).replace("&&", "&") == title for i in range(tabs.count())):
                return tabs
        return None

    @staticmethod
    def _at(widget, x, y, window):
        from PyQt6.QtCore import QPoint
        return widget.mapTo(window, QPoint(int(x), int(y)))

    @staticmethod
    def _mark(image, points):
        """Numbered circles at the points (1, 2, ...)."""
        from PyQt6.QtCore import QRectF, Qt
        from PyQt6.QtGui import QColor, QFont, QPainter, QPen
        p = QPainter(image)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont(p.font())
        font.setBold(True)
        font.setPixelSize(13)
        p.setFont(font)
        for n, pt in enumerate(points, 1):
            r = QRectF(pt.x() - 12, pt.y() - 12, 24, 24)
            p.setPen(QPen(QColor("#FFFFFF"), 2))
            p.setBrush(QColor(MARKER_FILL))
            p.drawEllipse(r)
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, str(n))
        p.end()
        return image

    def open_viewer(self):
        from src.core import paths
        from src.core.data_cache import DualPipelineCache
        from src.gui.data_viewer.viewer_main import DataViewerApp
        self.cache = DualPipelineCache(paths.DATA_DIR)
        self.cache.start()
        v = self.viewer = DataViewerApp(self.cache, window_title="SINDIE Data Viewer")
        v.show()
        v.resize(1536, 864)
        lanes = {"ion_beam.source.vacuum_gauge_1.rb_pressure": ("Lane 1", "log"),
                 "ion_beam.beamline.vacuum_gauge_2.rb_pressure": ("Lane 1", "log"),
                 "ion_beam.source.extraction.rb_voltage": ("Lane 2", "linear"),
                 "ion_beam.source.target.rb_voltage": ("Lane 2", "linear"),
                 "ion_beam.source.einzel.rb_voltage": ("Lane 2", "linear"),
                 "ion_beam.beamline.faraday.smu.rb_current": ("Lane 3", "linear")}
        v.plot_config = {t: {"selected": True, "lane": lane, "scale": scale,
                             "label": t.split(".")[-2] + " " + t.split(".")[-1]}
                         for t, (lane, scale) in lanes.items()}
        v.cache.register_client_tags(v.client_id, list(v.plot_config))
        for name, cb in v.filter_checkboxes.items():
            cb.setChecked(name in ("User Markers", "Faults: Activated", "Faults: Cleared", "Phase Changes"))
        v.request_ui_refresh()

    def shoot_viewer(self):
        from PyQt6.QtWidgets import QGroupBox
        v = self.viewer
        v.showNormal()
        self.spin(0.5)
        v.setGeometry(0, 0, 1536, 864)
        self.spin(3.0)
        image = v.grab().toImage()
        marks = []
        for box in v.findChildren(QGroupBox):
            if box.title() in ("Time", "Channels", "Values", "Export", "Event markers") and box.isVisible():
                marks.append((box.title(), self._at(box, box.width() - 22, 10, v)))
        order = ["Time", "Channels", "Values", "Export", "Event markers"]
        points = [pt for _t, pt in sorted(marks, key=lambda m: order.index(m[0]))]
        plots = v.layout_widget
        points.append(self._at(plots, plots.width() // 2, 24, v))
        self.save(self._mark(image, points), "viewer-overview")

    def shoot_panels(self):
        hmi = self.hmi
        for dock in hmi._docks:
            name, width, height = PANELS.get(dock.objectName(), (None, 0, 0))
            if not name:
                continue
            dock.setFloating(True)
            dock.resize(width, height)
            dock.show()
            dock.raise_()
            self.unfocus()
            widget = hmi._panel_widgets.get(dock)
            if widget is not None:                  # (a panel that was behind a tab: up to date first)
                widget.update_telemetry(hmi.master_telemetry_cache)
            self.spin(0.8)
            # as tall as the contents need at that width (up to the size above), no empty space below
            content = dock.widget().widget() if hasattr(dock.widget(), "widget") else None
            if content is not None and content.layout() is not None:
                content.layout().activate()
                need = content.layout().totalHeightForWidth(content.width()) \
                    if content.layout().hasHeightForWidth() else content.sizeHint().height()
                dock.resize(width, max(160, min(height, need + (dock.height() - dock.widget().height()) + 6)))
                self.spin(0.5)
            self.unfocus(dock)
            self.save(dock.grab(), name)
        hmi._reset_layout()
        self.spin(1.0)

    def shoot_dialogs(self):
        hmi, v = self.hmi, self.viewer
        try:
            from src.core import paths
            from src.gui.control.dialogs.recipe_builder import RecipeBuilderDialog
            w = hmi.recipe_widget
            dlg = RecipeBuilderDialog(w, w.config_dir)
            dlg.new_from_template(os.path.join(paths.CONFIG_DIR, "recipe_templates", "mass_scan.json"))
            dlg.resize(1320, 820)
            dlg.show()
            self.spin(1.0)
            dlg.outline.select_path((0,), quiet=False)
            self.spin(0.5)
            self.save(dlg.grab(), "dialog-recipe-builder")
            dlg._closing_ok = True              # (a template not saved: close without asking)
            dlg.close()
        except Exception as e:
            print("  (recipe builder not taken:", e, ")")
        try:
            from src.gui.data_viewer.channel_selector import ChannelSelectorDialog
            dlg = ChannelSelectorDialog({k: None for k in v.system_registry}, v.plot_config, v.system_registry, v)
            dlg.resize(980, 640)
            dlg.show()
            self.spin(1.0)
            self.save(dlg.grab(), "viewer-channel-selector")
            dlg.close()
        except Exception as e:
            print("  (channel selector not taken:", e, ")")
        try:
            from src.core import export_data
            from src.gui.data_viewer.export_dialog import ExportDialog
            t1 = time.time()
            path = v.edit_export_path.text()
            dlg = ExportDialog(v, t1 - 600, t1, len(v.plot_config), export_data.logger_tick_seconds(v.PROJECT_ROOT),
                               12000, os.path.dirname(path), os.path.basename(path))
            dlg.show()
            self.spin(1.0)
            self.save(dlg.grab(), "viewer-export-dialog")
            dlg.close()
        except Exception as e:
            print("  (export dialog not taken:", e, ")")
        try:
            overlay = v.event_overlay
            key = next((k for k, it in overlay.items.items()
                        if any(ev.get("type") == "USER_MARKER" for _x, _k, ev in it["members"])), None)
            if key is None and overlay.items:
                key = next(iter(overlay.items))
            if key is not None:
                overlay.show_details(key)
                self.spin(0.8)
                self.save(overlay.details.grab(), "viewer-event-details")
                overlay.details.close()
        except Exception as e:
            print("  (event details not taken:", e, ")")
        try:
            from src.gui.control.dialogs.config_editor_dialog import ConfigEditorDialog
            dlg = ConfigEditorDialog(hmi.cmd_thread, hmi, events=hmi.event_helper,
                                     services=lambda: dict(self.view.get("manager.services") or {}))
            dlg.resize(1100, 700)
            dlg.show()
            self.spin(1.0)
            self.save(dlg.grab(), "dialog-config-editor")
            dlg.close()
        except Exception as e:
            print("  (config editor not taken:", e, ")")

    def shoot_alarm(self):
        """The Alarms panel with an alarm raised: the turbo pump's controller made unreachable."""
        from src import sim
        hmi = self.hmi
        sim.set_faults({"turbo_disconnect"})
        try:
            self.wait(lambda: any("turbo" in (hmi.alarm_widget.table.item(r, c).text() if
                                              hmi.alarm_widget.table.item(r, c) else "").lower()
                                  for r in range(hmi.alarm_widget.table.rowCount())
                                  for c in range(hmi.alarm_widget.table.columnCount())),
                      40, "the turbo alarm")
            self.spin(2.0)
            dock = hmi.alarm_dock
            name, width, height = PANELS["AlarmDock"]
            dock.setFloating(True)
            dock.resize(width, height)
            dock.raise_()
            self.spin(1.0)
            self.save(dock.grab(), name)
        finally:
            sim.set_faults(set())


if __name__ == "__main__":
    main()
