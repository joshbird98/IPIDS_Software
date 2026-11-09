"""The operator interface's screenshots for Josh's thesis (joshbird98/PhD-Thesis), from the simulator.

    python src/utils/make_thesis_screenshots.py THESIS_FOLDER              about ten minutes
    python src/utils/make_thesis_screenshots.py THESIS_FOLDER --theme dark
    python src/utils/make_thesis_screenshots.py THESIS_FOLDER --only hmi_health,hmi_alarms
    python src/utils/make_thesis_screenshots.py THESIS_FOLDER --out SOMEWHERE   (THESIS_FOLDER's layout, elsewhere)

As make_help_screenshots.py, it starts the simulated services on their own ports and in a temporary
data folder, with a copy of the recipes folder (so it is safe on the lab PC next to the running
system, and leaves nothing behind), opens the HMI and the data viewer offscreen, and works the
simulated machine as an operator would:

  1. with the gate valve open and the safety relay still off, two instruments' links drop out for a
     while, and ENABLE SAFETY RELAY is refused with a door open in the relay's hardwired loop, then
     pressed again: the System Health panel counting down, and the Alarms panel (put right after);
  2. the safety relay on, then extraction, target, einzel, caesium oven, filament and thermionic
     with auto-emission, the steering on, the magnet on Si-28 and the beam on the Faraday cup;
  3. a mass scan;
  4. a recipe for an implant, which holds the Faraday cup, the magnet and the source einzel lens
     while the beam settles.

It saves

  parts/platform/figures/hmi_overview.png       the operator interface during the run
  parts/platform/figures/hmi_beam_path_diagram.png   Beamline Optics shown as its diagram
  parts/appendices/figures/hmi_*.png            its panels (the four with List | Diagram both ways)
                                                and dialogs
  parts/appendices/figures/viewer_*.png         the telemetry viewer

as the thesis' figure guidelines (notes/figure_guidelines.md) ask: PNG, as screenshots are, at
about 2700 px for the text width (160 mm), PX_PER_MM. WIDTH_MM below is the width each is included
at, which the .tex files must match. Each is re-painted at that resolution, not scaled.

The text is set larger than the screen's normal size (TEXT_SCALE, as View > Text size > Larger),
and each panel laid out narrow enough on the (offscreen) screen that its text prints at about
TARGET_PT (the thesis' smallest text is 6.5 pt): a panel that needs more room than that is laid out
at the width it needs, and prints smaller; so do the windows of SCREEN_PX. The diagrams are laid
out at the smallest size their labels fit in at that text size, which prints their labels at about
6 pt. The whole window (hmi_overview) is at the normal text size, for its layout: its panels are in
the pictures of their own. Plot lines are drawn as thick, for the size of the picture, as they are
on the screen. The light theme prints best on a white page; --theme dark for the other. The pictures
show the export folder as the lab PC has it (LAB_PROJECT), wherever this runs.
"""
import argparse
import contextlib
import os
import shutil
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
from src.utils.make_help_screenshots import Shooter, run_offscreen   # noqa: E402

PORT_OFFSET = 35000
SCREEN = {"synchronousWindowSystemEvents": False, "windowFrameMargins": False,
          "screens": [{"name": "Desk", "x": 0, "y": 0, "width": 1920, "height": 1200,
                       "logicalDpi": 96, "logicalBaseDpi": 96, "dpr": 1}]}
PX_PER_MM = 2700 / 160.0            # the thesis' guideline: ~2700 px across the 160 mm text width
TEXT_SCALE = 1.3                    # the interface's text, as View > Text size > Larger ...
TARGET_PT = 7.0                     # ... and printed at about this size (layout_px)
MM_PER_PT = 25.4 / 72.0
OVERVIEW_TEXT_SCALE = 1.0           # the whole window: at the screen's normal size, for its layout
WIDTH_MM = {                        # the width each picture is included at in the thesis, mm
    "hmi_overview": 160, "hmi_beam_path_diagram": 160,
    "hmi_health": 110, "hmi_alarms": 160, "hmi_source": 160, "hmi_vacuum": 120, "hmi_optics": 130,
    "hmi_interlocks": 160, "hmi_config_editor": 160, "hmi_mass_scan": 160,
    "hmi_vacuum_diagram": 160, "hmi_safety_chain_diagram": 160, "hmi_source_diagram": 160,
    "viewer_overview": 160, "viewer_export": 110,
    "hmi_recipe_run": 140, "hmi_recipe_builder": 160,
}
PLATFORM = {"hmi_overview", "hmi_beam_path_diagram"}       # (Chapter 4's; the rest are Appendix B's)
SCREEN_PX = {                       # windows, laid out at (width, height) px whatever their text prints at
    "hmi_overview": (1600, 1100), "hmi_mass_scan": (None, 760), "hmi_interlocks": (1200, None),
    "hmi_recipe_builder": (1380, 960), "viewer_overview": (1300, 1200),
}
OVERVIEW_COLUMNS = [640, 960]       # (the left and right columns' widths, px)
OVERVIEW_LEFT = [470, 360, 250]     # (Vacuum System, System Health, Alarms: heights, px)
OVERVIEW_RIGHT = [580, 460]         # (the source panel, Beamline Optics)
# The panels shown as their diagram: picture -> (the panel's dock, the part whose faceplate is open,
# and whether that card is moved down clear of the labels above the part: beside the source turbo, it
# would hide VG1's pressure)
DIAGRAMS = {
    "hmi_vacuum_diagram": ("vac_dock", "turbo_source", True),
    "hmi_beam_path_diagram": ("optics_dock", None, False),
    "hmi_safety_chain_diagram": ("interlocks_dock", None, False),
    "hmi_source_diagram": ("src_dock", None, False),
}
LAB_PROJECT = r"C:\Users\100kV\PycharmProjects\IPIDS_Software"
RECIPE_FILE = "silicon_implant.json"
SLITS_MM = 1.5                      # each jaw: the 3 mm slits of the enrichment implant
STEERING = {"ion_beam.beamline.steering.sp_requested_y_volts": 15.0,
            "ion_beam.beamline.steerer_magnet.sp_requested_current": 0.5}


@contextlib.contextmanager
def rows_tall(view, spare=8):
    """A list or table (a QAbstractItemView) as tall as its rows, while the picture is taken."""
    model = view.model()
    if hasattr(view, "rowHeight"):                  # a table: its rows as they are drawn
        rows = sum(view.rowHeight(r) for r in range(model.rowCount()))
    else:
        rows = sum(view.sizeHintForRow(r) for r in range(model.rowCount()))
    header = getattr(view, "horizontalHeader", None)
    if header is not None and header().isVisible():
        rows += header().height()
    least, most = view.minimumHeight(), view.maximumHeight()
    view.setMinimumHeight(0)
    view.setFixedHeight(rows + 2 * view.frameWidth() + spare)
    try:
        yield view
    finally:
        view.setMinimumHeight(least)
        view.setMaximumHeight(most)


def implant_recipe(magnet_a):
    """The recipe of the pictures: an implant at the magnet current found for Si-28. It holds the
    Faraday cup, the magnet and the source einzel lens from its first steps, and is pictured in its
    30-minute wait, before the operator's go-ahead takes the cup out."""
    cup, mag, einzel = "ion_beam.beamline.diagnostics", "ion_beam.beamline.magnet", "ion_beam.source.einzel"
    step = [
        {"type": "VERIFIED_ACTION", "target": f"{cup}.cmd_insert_fc", "value": True,
         "verify_target": f"{cup}.stat_fc_inserted", "verify_condition": "==", "verify_value": 1,
         "timeout_sec": 15.0, "comment": "Faraday cup in"},
        {"type": "VERIFIED_ACTION", "target": f"{mag}.sp_requested_current", "value": round(magnet_a, 2),
         "verify_target": f"{mag}.rb_current", "verify_condition": ">=", "verify_value": round(magnet_a - 0.1, 2),
         "timeout_sec": 30.0, "comment": "Analyser magnet on the Si-28 peak"},
        {"type": "VERIFIED_ACTION", "target": f"{einzel}.sp_requested_voltage", "value": 10.4,
         "verify_target": f"{einzel}.rb_voltage", "verify_condition": ">=", "verify_value": 10.3,
         "timeout_sec": 20.0, "comment": "Source einzel lens to 10.4 kV"},
        {"type": "WAIT_TELEMETRY", "target": "ion_beam.beamline.faraday.smu.rb_current", "condition": ">",
         "value": 5e-8, "timeout_sec": 30.0, "comment": "Beam on the Faraday cup"},
        {"type": "WAIT_TIME", "duration_sec": 1800.0, "comment": "The beam settles"},
        {"type": "USER_PROMPT", "prompt_text": "Note the beam current on the Faraday cup, then click OK to take "
                                               "the cup out and start the implant.",
         "verify_conditions": [], "comment": "The operator's go-ahead"},
        {"type": "VERIFIED_ACTION", "target": f"{cup}.cmd_insert_fc", "value": False,
         "verify_target": f"{cup}.stat_fc_inserted", "verify_condition": "==", "verify_value": 0,
         "timeout_sec": 15.0, "comment": "Faraday cup out: the implant starts"},
        {"type": "WAIT_TIME", "duration_sec": 6 * 3600.0, "comment": "The implant"},
        {"type": "VERIFIED_ACTION", "target": f"{cup}.cmd_insert_fc", "value": True,
         "verify_target": f"{cup}.stat_fc_inserted", "verify_condition": "==", "verify_value": 1,
         "timeout_sec": 15.0, "comment": "Faraday cup in: the implant ends"},
    ]
    for n, s in enumerate(step, 1):
        s["step_id"] = n
    return {"recipe_name": "Silicon implant", "version": "1.0",
            "description": "Holds the Faraday cup, the magnet and the source einzel lens while the Si-28 beam "
                           "settles, then implants for 6 h once the operator says so.",
            "fault_policy": [{"type": "BIT_FAULT", "registry_tag": "ion_beam.faults.word_0_system", "bit_str": "0.1",
                              "name": "Safety_Relay_Tripped", "action": "ABORT"}],
            "steps": step}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("thesis", help="the thesis repository (its parts/ folder gets the pictures)")
    ap.add_argument("--theme", default="light", choices=("light", "dark"))
    ap.add_argument("--only", default="", help="comma-separated picture names (of WIDTH_MM): just these")
    ap.add_argument("--out", default="", help="save in this folder's parts/... instead of the thesis' own "
                                              "(made as needed): to look at them before they replace the thesis'")
    ap.add_argument("--text-scale", type=float, default=TEXT_SCALE, help="the panels' text size (1 = normal)")
    ap.add_argument("--port-offset", type=int, default=PORT_OFFSET)
    args = ap.parse_args()
    for folder in (os.path.join(os.path.abspath(args.thesis), "parts", f, "figures") for f in ("platform", "appendices")):
        if not os.path.isdir(folder):
            sys.exit(f"No {folder}: is {args.thesis} the thesis repository?")
    root = os.path.abspath(args.out or args.thesis)
    platform = os.path.join(root, "parts", "platform", "figures")
    appendix = os.path.join(root, "parts", "appendices", "figures")
    for folder in (platform, appendix):
        os.makedirs(folder, exist_ok=True)
    only = [n.strip() for n in args.only.split(",") if n.strip()]
    unknown = [n for n in only if n not in WIDTH_MM]
    if unknown:
        sys.exit(f"Not a picture: {', '.join(unknown)} (known: {', '.join(WIDTH_MM)})")
    run_offscreen(args.port_offset, SCREEN,
                  lambda: ThesisShooter(platform, appendix, args.theme, only, args.text_scale).run(),
                  prefix="sindie_thesis_shots_", own_recipes=True)


class ThesisShooter(Shooter):
    def __init__(self, platform, appendix, mode, only=(), text_scale=TEXT_SCALE):
        super().__init__(appendix, mode)
        self.platform = platform
        self.only = set(only)
        self.mode, self.text_scale = mode, text_scale
        self.set_text_scale(text_scale)

    def want(self, *names):
        return not self.only or any(n in self.only for n in names)

    def set_text_scale(self, scale):
        from src.gui import theme
        theme.apply_appearance(self.app, self.mode, scale, "standard", persist=False)
        self.app.processEvents()

    def em_px(self) -> int:
        from PyQt6.QtGui import QFontInfo
        return QFontInfo(self.app.font()).pixelSize()

    def layout_px(self, name, least=0):
        """How wide a picture's panel or window is laid out on the screen, px: so that its text prints
        at TARGET_PT, or wider if its contents need `least`."""
        if name in SCREEN_PX and SCREEN_PX[name][0]:
            return SCREEN_PX[name][0]
        return max(least, round(WIDTH_MM[name] * self.em_px() / (TARGET_PT * MM_PER_PT)))

    # --- pictures at print resolution ----------------------------------------------------------
    def render(self, widget, name, rect=None, folder=None):
        """widget (or rect of it) re-painted at PX_PER_MM over its width in the thesis, as a PNG."""
        from PyQt6.QtCore import QRect
        from PyQt6.QtGui import QFontInfo
        from src.gui.figure_capture import render_widget
        if not self.want(name):
            return
        width_mm = WIDTH_MM[name]
        rect = rect if rect is not None else QRect(0, 0, widget.width(), widget.height())
        page_pt = width_mm / 25.4 * 72.0
        base = os.path.join(folder or (self.platform if name in PLATFORM else self.out), name)
        render_widget(widget, base, width_cm=width_mm / 10.0, formats=("png",), png_dpi=PX_PER_MM * 25.4,
                      rect=rect, line_pt=page_pt / rect.width())
        text_pt = QFontInfo(self.app.font()).pixelSize() * page_pt / rect.width()
        self._optimise(base + ".png", f"{width_mm} mm wide, {width_mm * rect.height() / rect.width():.0f} mm tall, "
                                      f"text at about {text_pt:.1f} pt")
        self.saved.append(name)

    def save(self, image, name):
        """(The help pictures the shared steps take on the way - the Automation bar during the mass
        scan - aren't the thesis'.)"""

    @staticmethod
    def _optimise(path, note):
        """A smaller PNG: 256 colours (a screen of panels has few, and the text's smoothing loses nothing
        that shows) and compressed - a third of the size of Qt's file, which keeps the thesis' PDF small
        (the 13 pictures: 1.7 MB instead of 5.1)."""
        try:
            from PIL import Image
            with Image.open(path) as im:
                image = im.convert("RGB")
            image = image.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
            image.save(path, optimize=True)
            print(f"  {os.path.basename(path)}  {image.width} x {image.height}  "
                  f"{os.path.getsize(path) // 1024} kB, {note}")
        except Exception as e:                      # (Pillow missing: Qt's file is fine)
            print(f"  {os.path.basename(path)} (not optimised: {e}), {note}")

    def panel(self, dock, name, width=None, height=None):
        """A panel pulled out on its own at width (layout_px if None: at least as wide as its contents
        need) x height (or as tall as it needs), without its title bar: its contents, as they show in
        the panel. A panel that can also be shown as a diagram, as its list, under the switch."""
        from PyQt6.QtCore import QRect
        if not self.want(name):
            return
        hmi = self.hmi
        views = hmi._views.get(dock)
        if views is not None:
            views.show_view("list")
        scroll = views.page() if views is not None else dock.widget()
        content = scroll.widget()
        if width is None:
            frame = 2 + (scroll.verticalScrollBar().sizeHint().width() if height else 0)
            width = self.layout_px(name, least=content.minimumSizeHint().width() + frame)
        dock.setFloating(True)
        dock.resize(width, height or 900)
        dock.show()
        dock.raise_()
        self.unfocus()
        widget = hmi._panel_widgets.get(dock)
        if widget is not None:
            widget.update_telemetry(hmi.master_telemetry_cache)
        self.spin(0.8)
        if height is None and content is not None and content.layout() is not None:
            content.layout().activate()
            need = content.layout().totalHeightForWidth(content.width()) \
                if content.layout().hasHeightForWidth() else content.sizeHint().height()
            dock.resize(width, need + (dock.height() - scroll.height()) + 4)
            self.spin(0.6)
        self.unfocus(dock)
        target = dock.widget()
        self.render(target, name, rect=QRect(0, 0, target.width(), target.height()))

    def diagram(self, name):
        """A panel shown as its diagram (List | Diagram), pulled out on its own: as wide as the
        drawing's labels need at this text size (the smallest it is drawn in without scrolling) and
        as tall as the drawing then is, under the switch, with a faceplate open if DIAGRAMS says."""
        from PyQt6.QtCore import QRect
        if not self.want(name):
            return
        hmi = self.hmi
        dock_name, part, clear_above = DIAGRAMS[name]
        dock = getattr(hmi, dock_name)
        views, diagram = hmi._views[dock], hmi._diagram_of[dock]
        views.show_view("diagram")
        dock.setFloating(True)
        dock.show()
        dock.raise_()
        self.spin(0.3)
        diagram.canvas.invalidate()
        floor = diagram.canvas.minimumSizeHint()
        page = views.page("diagram")
        chrome_w, chrome_h = dock.width() - page.viewport().width(), dock.height() - page.viewport().height()
        room = 1.04 if part else 1.0        # (a little more, beside a card: room for it between its part and the key)
        dock.resize(round(floor.width() * room) + chrome_w + 2, round(floor.height() * room) + chrome_h + 2)
        self.unfocus()
        for w in diagram.watched + [diagram]:       # (up to date at once: its panels first)
            w.update_telemetry(hmi.master_telemetry_cache)
        self.spin(1.0)
        if part:
            diagram.open_faceplate(part)
            self.spin(0.8)
            if clear_above:
                self.card_below_labels(diagram, part)
        self.unfocus(dock)
        target = dock.widget()
        bars = page.verticalScrollBar().isVisible() or page.horizontalScrollBar().isVisible()
        self.render(target, name, rect=QRect(0, 0, target.width(), target.height()))
        if bars:
            print(f"  ({name}: the panel scrolls - drawn too small for its labels?)")
        if part:
            diagram.open_faceplate("")
        views.show_view("list")

    def window_with(self, dock, name, width, height):
        """The main window with just this panel docked in it, under the menus and the Automation bar."""
        if not self.want(name):
            return
        hmi = self.hmi
        hmi.showNormal()
        for d in hmi._docks:
            d.setFloating(False)
            d.setVisible(d is dock)
        dock.raise_()
        hmi.resize(width, height)
        self.unfocus()
        self.spin(1.5)
        self.render(hmi, name)
        self.relayout()

    def relayout(self):
        hmi = self.hmi
        hmi._reset_layout()
        for d in hmi._docks:
            d.show()
        hmi._reset_layout()
        self.spin(1.0)

    def type_into(self, box, value):
        """Type a value into a setpoint box and press Enter, as an operator would (its panel on show)."""
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        box.setFocus()
        box.selectAll()
        QTest.keyClicks(box, f"{value:g}")
        QTest.keyClick(box, Qt.Key.Key_Return)
        self.spin(0.3)

    def alarms(self):
        """The rows of the Alarms panel: [first-out, priority, state, time, alarm, count]."""
        t = self.hmi.alarm_widget.table
        return [[t.item(r, c).text() if t.item(r, c) else "" for c in range(t.columnCount())]
                for r in range(t.rowCount())]

    def alarm_rows(self, *words):
        return [r for r, row in enumerate(self.alarms()) if any(w in row[4].lower() for w in words)]

    # --- the run --------------------------------------------------------------------------------
    def run(self):
        from src.gui.control.control_main import ControlMainWindow
        self.hmi = hmi = ControlMainWindow()
        hmi.show()
        hmi.resize(*SCREEN_PX["hmi_overview"])
        self.view = view = hmi.master_telemetry_cache
        print("Bringing the simulated machine up ...")
        if not self.wait(lambda: view.get("system.connected") is True and
                         all(s == "ONLINE" for s in (view.get("manager.services") or {"x": 0}).values()),
                         90, "every service ONLINE"):
            print("  carrying on anyway")
        self.open_viewer()                  # (its live buffer then holds the whole run)
        self.set_slits()
        self.t_start = time.time()
        self.send("ion_beam.source.turbo_pump.cmd_enable", True)
        self.send("ion_beam.source.chamber.cmd_open_gv", True)
        self.wait(lambda: view.get("ion_beam.source.chamber.stat_gv_open") == 1.0, 30, "gate valve open")
        if self.want("hmi_health", "hmi_alarms"):
            self.relay_refused()
            if self.only and self.only <= {"hmi_health", "hmi_alarms"}:
                return self.finish()
        if view.get("ion_beam.facilities.safety_relay_active") != 1.0:
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
        hmi.event_helper.log_phase("Source start-up")
        self.wait(lambda: (view.get("ion_beam.source.thermionic.rb_power") or 0) > 210
                  and (view.get("ion_beam.source.cesium.rb_temp") or 0) > 55, 90, "source running")
        self.send("ion_beam.source.thermionic.cmd_auto_emission", True)     # the PLC holds the filament
        for tag, value in STEERING.items():
            self.send(tag, value)
        self.spin(1.0)
        for tag in ("ion_beam.beamline.steering.cmd_enable", "ion_beam.beamline.steerer_magnet.cmd_enable"):
            self.send(tag, True)
        self.put_mass_on_the_cup(28.0)
        hmi.event_helper.log_phase("Silicon implant run 4")
        self.spin(1.0)
        hmi.event_helper.log_user_marker(time.time(), "Beam tuned on Si-28", "#FFFFFF")
        if self.want("hmi_mass_scan", "viewer_overview", "viewer_export"):
            self.run_mass_scan()
            self.panel(hmi.mass_scan_dock, "hmi_mass_scan", None, SCREEN_PX["hmi_mass_scan"][1])
            self.relayout()
            self.put_mass_on_the_cup(28.0)
        self.spin(3.0)
        self.panel(hmi.vac_dock, "hmi_vacuum")
        self.panel(hmi.optics_dock, "hmi_optics")
        self.relayout()
        self.start_recipe()
        self.spin(6.0)

        self.shoot_overview()
        for name in DIAGRAMS:
            self.diagram(name)
        self.relayout()
        self.panel(hmi.src_dock, "hmi_source")
        self.panel(hmi.interlocks_dock, "hmi_interlocks")
        self.relayout()
        self.window_with(hmi.recipe_dock, "hmi_recipe_run", self.layout_px("hmi_recipe_run"), 600)
        self.shoot_thesis_viewer()
        self.shoot_thesis_dialogs()
        self.finish()

    def finish(self):
        self.hmi.close()
        if getattr(self, "viewer", None) is not None:
            self.viewer.close()
            self.cache.stop()
        print(f"Saved {len(self.saved)} pictures: {', '.join(self.saved)}")

    def set_slits(self):
        """The slit widths, typed in as the operator sets them by hand (each goes in the event log)."""
        opt = self.hmi.optics_widget
        self.hmi.optics_dock.raise_()
        self.spin(0.5)
        for box in (opt.sp_obj_l, opt.sp_obj_r, opt.sp_img_l, opt.sp_img_r):
            self.wait(box.isEnabled, 10, "the slit boxes")
            self.type_into(box, SLITS_MM)
        self.unfocus()
        self.hmi.src_dock.raise_()

    def relay_refused(self):
        """Faults on purpose, with the gate valve open and the relay still off. The X steerer's and the
        SMU's links drop out (simulated): their alarms come; the steerer's link comes back, its alarm
        returned to normal but not acknowledged; the SMU's is acknowledged and stays. Then a door
        open in the relay's hardwired loop: ENABLE SAFETY RELAY is refused after the 5 s the PLC
        waits for START, and the refusal latched; pressed again, the System Health panel counts
        down, and both panels are taken. Then it is all put right: links back, RESET FAULTS, ACK
        ALL, the first-out reset."""
        from PyQt6.QtCore import QItemSelectionModel
        from src import sim
        hmi = self.hmi
        health, alarms = hmi.health_widget, hmi.alarm_widget
        hmi.alarm_dock.raise_()
        try:
            sim.set_faults({"steerer_disconnect", "smu_disconnect"})
            self.wait(lambda: self.alarm_rows("steer") and self.alarm_rows("smu"), 40,
                      "the steerer's and the SMU's alarms")
            self.spin(12.0)                         # (the PLC's comms faults: 10 s)
            sim.set_faults({"smu_disconnect"})
            self.wait(lambda: any("CLEARED" in self.alarms()[r][2] for r in self.alarm_rows("steer")), 30,
                      "the steerer's alarm returned to normal")
            table, sel = alarms.table, alarms.table.selectionModel()
            sel.clearSelection()
            for r in self.alarm_rows("smu"):
                sel.select(table.model().index(r, 0), QItemSelectionModel.SelectionFlag.Select
                           | QItemSelectionModel.SelectionFlag.Rows)
            self.spin(0.3)
            if alarms.btn_ack.isEnabled():
                alarms.btn_ack.click()
            self.spin(2.0)
            table.clearSelection()

            sim.set_faults({"smu_disconnect", "safety_loop_open"})
            self.wait(health.btn_enable_safety.isEnabled, 10, "ENABLE SAFETY RELAY")
            health.btn_enable_safety.click()
            self.wait(lambda: "REFUSED" in health.lbl_safety_state.text(), 12, "the relay refused")
            self.spin(3.0)
            self.wait(health.btn_enable_safety.isEnabled, 10, "ENABLE SAFETY RELAY again")
            health.btn_enable_safety.click()
            self.wait(lambda: "PRESS PANEL START" in health.btn_enable_safety.text(), 5, "the countdown")
            self.spin(1.6)                          # (a second or two of it gone)
            # The fault list and the alarm table as tall as their rows need, not the window
            with rows_tall(health.active_faults_list):
                self.panel(hmi.health_dock, "hmi_health")
            with rows_tall(alarms.table):
                self.panel(hmi.alarm_dock, "hmi_alarms")
        finally:
            # The request withdrawn before the door is shut again: else the relay would close on it
            self.send("ion_beam.system.cmd_enable_safety", False)
            self.wait(lambda: not self.view.get("ion_beam.system.safety_timer_elapsed_ms"), 10, "the countdown over")
            sim.set_faults(set())
        self.relayout()
        self.spin(8.0)                              # (the links back)
        health.btn_reset_faults.click()
        self.spin(3.0)
        alarms.btn_ack_all.click()
        alarms.btn_first_out.click()
        self.wait(lambda: not self.alarms(), 40, "the alarm list empty")

    def put_mass_on_the_cup(self, amu):
        super().put_mass_on_the_cup(amu)
        self.unfocus()

    def start_recipe(self):
        import json
        w = self.hmi.recipe_widget
        recipe = implant_recipe(self.view.get("ion_beam.beamline.magnet.sp_requested_current") or 19.83)
        with open(os.path.join(w.config_dir, RECIPE_FILE), "w", encoding="utf-8") as f:
            json.dump(recipe, f, indent=2)
        self.load_recipe(RECIPE_FILE)
        self.wait(w.btn_start.isEnabled, 10, "recipe START")
        w.btn_start.click()
        self.wait(lambda: self.hmi.automation_bar.groups, 20, "the recipe's lease in the Automation bar")
        self.wait(lambda: "step 5" in (self.view.get("sequencer.recipe.status") or "").lower(), 60,
                  "the recipe at its wait")

    def open_viewer(self):
        super().open_viewer()
        v = self.viewer
        lanes = {"ion_beam.source.vacuum_gauge_1.rb_pressure": ("Lane 1", "log", "Source pressure (VG1)"),
                 "ion_beam.beamline.vacuum_gauge_2.rb_pressure": ("Lane 1", "log", "Beamline pressure (VG2)"),
                 "ion_beam.source.extraction.rb_voltage": ("Lane 2", "linear", "Extraction"),
                 "ion_beam.source.target.rb_voltage": ("Lane 2", "linear", "Target (cathode)"),
                 "ion_beam.source.einzel.rb_voltage": ("Lane 2", "linear", "Source einzel"),
                 "ion_beam.source.cesium.rb_temp": ("Lane 3", "linear", "Caesium oven"),
                 "ion_beam.beamline.faraday.smu.rb_current": ("Lane 4", "linear", "Faraday cup current")}
        v.plot_config = {t: {"selected": True, "lane": lane, "scale": scale, "label": label}
                         for t, (lane, scale, label) in lanes.items()}
        v.cache.register_client_tags(v.client_id, list(v.plot_config))
        v.edit_export_path.setText(os.path.join(LAB_PROJECT, "data", "exports", "SINDIE_Snapshot")
                                   .replace("/", "\\"))
        v.request_ui_refresh()

    # --- the pictures -----------------------------------------------------------------------------
    def shoot_overview(self):
        """The whole window, at the normal text size: Vacuum System, shown as its diagram, over System
        Health over Alarms on the left; the source panel over Beamline Optics, shown as its diagram
        (the beam path), on the right; the Automation bar showing the recipe's lease at the top."""
        from PyQt6.QtCore import Qt
        if not self.want("hmi_overview"):
            return
        hmi = self.hmi
        self.set_text_scale(OVERVIEW_TEXT_SCALE)
        hmi.showNormal()
        hmi.resize(*SCREEN_PX["hmi_overview"])
        for dock in hmi._docks:
            dock.setFloating(False)
            hmi.removeDockWidget(dock)
        L, R, V = Qt.DockWidgetArea.LeftDockWidgetArea, Qt.DockWidgetArea.RightDockWidgetArea, Qt.Orientation.Vertical
        hmi.addDockWidget(L, hmi.vac_dock)
        hmi.addDockWidget(L, hmi.health_dock)
        hmi.splitDockWidget(hmi.vac_dock, hmi.health_dock, V)
        hmi.addDockWidget(L, hmi.alarm_dock)
        hmi.splitDockWidget(hmi.health_dock, hmi.alarm_dock, V)
        hmi.tabifyDockWidget(hmi.health_dock, hmi.interlocks_dock)
        previous = hmi.alarm_dock
        for dock in (hmi.recipe_dock, hmi.log_dock, hmi.cmd_log_dock, hmi.service_dock):
            hmi.tabifyDockWidget(previous, dock)
            previous = dock
        hmi.addDockWidget(R, hmi.src_dock)
        hmi.addDockWidget(R, hmi.optics_dock)
        hmi.splitDockWidget(hmi.src_dock, hmi.optics_dock, V)
        previous = hmi.optics_dock
        for dock in (hmi.smu_dock, hmi.mass_scan_dock, hmi.optimiser_dock, hmi.current_mon_dock):
            hmi.tabifyDockWidget(previous, dock)
            previous = dock
        for dock in hmi._docks:
            dock.show()
        for dock in (hmi.health_dock, hmi.alarm_dock, hmi.src_dock, hmi.optics_dock):
            dock.raise_()
        hmi._views[hmi.vac_dock].show_view("diagram")
        hmi._views[hmi.optics_dock].show_view("diagram")
        self.spin(0.5)
        hmi.resizeDocks([hmi.vac_dock, hmi.src_dock], OVERVIEW_COLUMNS, Qt.Orientation.Horizontal)
        hmi.resizeDocks([hmi.vac_dock, hmi.health_dock, hmi.alarm_dock], OVERVIEW_LEFT, V)
        hmi.resizeDocks([hmi.src_dock, hmi.optics_dock], OVERVIEW_RIGHT, V)
        self.unfocus()
        for d in hmi.diagrams:
            for w in d.watched + [d]:
                w.update_telemetry(hmi.master_telemetry_cache)
        self.spin(2.0)
        self.render(hmi, "hmi_overview")
        for views in hmi._views.values():
            views.show_view("list")
        self.set_text_scale(self.text_scale)
        self.relayout()

    def shoot_thesis_viewer(self):
        """The viewer over the whole run: from just before the gate valve opened."""
        v = getattr(self, "viewer", None)
        if v is None or not self.want("viewer_overview"):
            return
        v.showNormal()
        self.spin(0.5)
        v.setGeometry(0, 0, *SCREEN_PX["viewer_overview"])
        self.spin(1.0)
        v._set_timespan((time.time() - self.t_start + 5.0) / 60.0)     # (the start-up's own events left out)
        self.spin(4.0)
        self.unfocus(v)
        self.render(v, "viewer_overview")

    def shoot_thesis_dialogs(self):
        hmi, v = self.hmi, getattr(self, "viewer", None)
        if v is not None and self.want("viewer_export"):
            try:
                from src.core import export_data
                from src.gui.data_viewer.export_dialog import ExportDialog
                t1 = time.time()
                path = v.edit_export_path.text()
                folder, name = path.rsplit("\\", 1) if "\\" in path else os.path.split(path)
                dlg = ExportDialog(v, t1 - 600, t1, len(v.plot_config), export_data.logger_tick_seconds(v.PROJECT_ROOT),
                                   12000, folder, name)
                dlg.resize(self.layout_px("viewer_export", least=dlg.minimumSizeHint().width()),
                           dlg.sizeHint().height())
                dlg.show()
                self.spin(1.0)
                dlg.lbl_out.setText(dlg.lbl_out.text().replace("/", "\\"))     # (as on the lab PC)
                self.unfocus(dlg)
                self.render(dlg, "viewer_export")
                dlg.close()
            except Exception as e:
                print("  (export dialog not taken:", e, ")")
        if self.want("hmi_config_editor"):
            try:
                from src.gui.control.dialogs.config_editor_dialog import ConfigEditorDialog
                from PyQt6.QtWidgets import QLineEdit
                dlg = ConfigEditorDialog(hmi.cmd_thread, hmi, events=hmi.event_helper,
                                         services=lambda: dict(self.view.get("manager.services") or {}))
                dlg.cb_files.setCurrentText("magnet_config.json")
                dlg.resize(self.layout_px("hmi_config_editor", least=dlg.minimumSizeHint().width()), 820)
                dlg.show()
                self.spin(1.0)
                for edit in dlg.tree.findChildren(QLineEdit):
                    edit.setCursorPosition(0)
                self.unfocus(dlg)
                self.render(dlg, "hmi_config_editor")
                dlg.close()
            except Exception as e:
                print("  (config editor not taken:", e, ")")
        if self.want("hmi_recipe_builder"):
            from src.gui.control.dialogs.recipe_builder import dialog as builder
            real_availability = builder.availability
            # As on a PC with a Gemini key (the lab's): the drafting box ready. Nothing is sent to a model.
            builder.availability = lambda settings=None: (True, "")
            try:
                from src.core import paths
                templates = os.path.join(paths.CONFIG_DIR, "recipe_templates")
                w = hmi.recipe_widget
                dlg = builder.RecipeBuilderDialog(w, w.config_dir)
                dlg.new_from_template(os.path.join(templates, "start_up.json"))
                dlg.resize(*SCREEN_PX["hmi_recipe_builder"])
                dlg.show()
                self.spin(1.0)
                dlg.outline.select_path((3,), quiet=False)
                dlg.tabs.setCurrentWidget(dlg.txt_recipe.parentWidget())
                self.spin(0.5)
                self.unfocus(dlg)
                self.render(dlg, "hmi_recipe_builder")
                dlg._closing_ok = True
                dlg.close()
            except Exception as e:
                print("  (recipe builder not taken:", e, ")")
            finally:
                builder.availability = real_availability


if __name__ == "__main__":
    main()
