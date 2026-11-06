"""The TESS Source Diagram: the source as the thesis' schematic of it draws it (tess_schematic) - the
water-cooled cathode with the target at its end, the ioniser heated by electron bombardment from the
tungsten filament behind it, the caesium oven feeding vapour into the body, and the extraction
electrode the negative ions leave through, on to the source Einzel lens.

Each electrode is drawn in its supply's colour on the TESS Ion Source panel (green on, grey off, blue
driven automatically, violet held by a sequence, amber and red what needs attention) with that
supply's readback beside it; its faceplate has the panel's setpoint box and buttons (mirror.py), so
switching a supply on takes the same two clicks. The caesium ions and the electrons are drawn while
the supplies that make them are on: a picture of how the source works, not a measurement.
"""
import math

from PyQt6.QtCore import QPointF, QRectF, QSize, QSizeF, Qt
from PyQt6.QtGui import QColor, QPainterPath

from src.core import interlocks as il
from src.gui.control.diagrams import symbols as sym
from src.gui.control.diagrams.canvas import Part
from src.gui.control.diagrams.faceplate import Faceplate
from src.gui.control.diagrams.panel import DiagramPanel
from src.gui.control.diagrams.plates import readback, supply_chip, supply_rows, supply_tone

L, R, HC = Qt.AlignmentFlag.AlignLeft, Qt.AlignmentFlag.AlignRight, Qt.AlignmentFlag.AlignHCenter
T, B, VC = Qt.AlignmentFlag.AlignTop, Qt.AlignmentFlag.AlignBottom, Qt.AlignmentFlag.AlignVCenter

AXIS = 380.0                                    # the beam's axis
BODY = QRectF(40, 36, 935, 634)                 # the source body, in vacuum
CATHODE = QRectF(70, AXIS - 36, 400, 72)
CHANNEL = QRectF(92, AXIS - 13, 350, 26)        # its cooling water
TARGET = QRectF(470, AXIS - 42, 34, 84)
FACE = QPointF(TARGET.right(), AXIS)            # the target's face, where the ions come from
IONISER_R, FILAMENT_R, ARC_DEG = 165.0, 240.0, 56.0
OVEN = QRectF(110, 715, 230, 105)
NOZZLE = QPointF(430, AXIS + 70)
EXTRACTION_X, GAP = 1055.0, 38.0
EINZEL_X = 1360.0
END_X = 1720.0

PANEL_LINK = "TESS Ion Source panel →"


def _arc_point(r: float, deg: float) -> QPointF:
    a = math.radians(deg)
    return QPointF(FACE.x() + r * math.cos(a), AXIS + r * math.sin(a))


def _arc_band(r: float, half_width: float) -> QPainterPath:
    """The band r ± half_width around the target's face, ARC_DEG either side of the axis."""
    path = QPainterPath()
    outer = QRectF(FACE.x() - r - half_width, AXIS - r - half_width, 2 * (r + half_width), 2 * (r + half_width))
    inner = QRectF(FACE.x() - r + half_width, AXIS - r + half_width, 2 * (r - half_width), 2 * (r - half_width))
    path.arcMoveTo(outer, ARC_DEG)
    path.arcTo(outer, ARC_DEG, -2 * ARC_DEG)
    path.arcTo(inner, -ARC_DEG, 2 * ARC_DEG)
    path.closeSubpath()
    return path


class TessSourceDiagram(DiagramPanel):
    ORIGIN = QPointF(0, 0)
    SIZE = QSizeF(1760, 880)
    DESIGN_SCALE = 0.55
    READABLE = QSize(900, 520)

    def __init__(self, panels: dict, show_panel=None):
        super().__init__(panels, show_panel)
        self.src = panels["source"]
        self.interlocks = panels.get("interlocks")
        self._parts = self._make_parts()
        self._box = {part.key: part.box for part in self._parts}
        self.coolant = None
        self.energy = None

    @property
    def watched(self) -> list:
        return [w for w in (self.src, self.interlocks) if w is not None]

    # --- what is where --------------------------------------------------------------------------
    def _make_parts(self):
        ioniser, filament = _arc_band(IONISER_R, 11), _arc_band(FILAMENT_R, 16)
        return [
            Part("target", CATHODE.united(TARGET), "The cathode and its target: the Target supply"),
            Part("coolant", CHANNEL, "The cathode's cooling water (the source coolant loop)"),
            Part("ioniser", ioniser.boundingRect(), "The ioniser, heated by electron bombardment: the Thermionic "
                                                    "supply", shape=ioniser),
            Part("filament", filament.boundingRect(), "The tungsten filament: the Filament supply", shape=filament),
            Part("oven", OVEN, "The caesium oven"),
            Part("extraction", QRectF(EXTRACTION_X - 18, AXIS - GAP - 150, 36, 2 * GAP + 300),
                 "The extraction electrode: the Extraction supply holds the source body below it"),
            Part("einzel", QRectF(EINZEL_X - 50, AXIS - 54, 100, 108), "The source Einzel lens"),
        ]

    def parts(self):
        return self._parts

    # --- the telemetry, as the panels show it -------------------------------------------------
    def read(self, data: dict):
        ov = getattr(self.interlocks, "overview", None)
        g = ov.group("relay") if ov is not None else None
        self.coolant = next((c for c in g.conditions if c.label == "Source coolant flow"), None) if g else None
        self.energy = None
        tgt, ext = data.get("ion_beam.source.target.rb_voltage"), data.get("ion_beam.source.extraction.rb_voltage")
        if tgt is not None and ext is not None:
            self.energy = tgt + ext

    def _on(self, name: str) -> bool:
        return supply_tone(self.src.psu_controls[name]) in ("on", "info", "seq")

    # --- drawing ------------------------------------------------------------------------------
    def paint(self, v):
        ctrl = self.src.psu_controls
        p = v.p
        # The source body, in vacuum, and the beam leaving it
        p.save()
        p.setPen(v.pen(v.c("border_strong"), 1.6, Qt.PenStyle.DashLine))
        fill = QColor(v.c("card_alt"))
        fill.setAlpha(120)
        p.setBrush(fill)
        p.drawRoundedRect(BODY, 18, 18)
        p.restore()
        frame = QPainterPath()
        frame.addRoundedRect(BODY, 18, 18)
        v.stroke(frame, v.units(3))
        self._beam(v)
        # The oven and its vapour line into the body
        oven_tone = supply_tone(ctrl["Cesium"])
        sym.line(v, sym.path_points([(OVEN.center().x(), OVEN.top()), (OVEN.center().x(), NOZZLE.y() + 120),
                                     (NOZZLE.x(), NOZZLE.y() + 120), (NOZZLE.x(), NOZZLE.y())]), 5.0,
                 v.c("border_strong"))
        p.save()
        f, line_c, _ink = v.tone(sym.soft(oven_tone))
        p.setPen(v.pen(line_c, 1.8, sym.line_style(oven_tone)))
        p.setBrush(f)
        p.drawRoundedRect(OVEN, 10, 10)
        heater = QColor(v.tone(oven_tone)[1] if oven_tone in ("on", "ok") else v.c("border"))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(heater)
        p.drawRoundedRect(OVEN.adjusted(14, OVEN.height() - 26, -14, -12), 4, 4)
        p.restore()
        v.ring("oven", OVEN)
        # The cathode, its cooling water, the target
        target_tone = supply_tone(ctrl["Target"])
        p.save()
        f, line_c, _ink = v.tone(sym.soft(target_tone))
        p.setPen(v.pen(line_c, 1.8, sym.line_style(target_tone)))
        p.setBrush(f)
        p.drawRect(CATHODE)
        cool = self.coolant
        cool_tone = self._cond_tone(cool)
        f2, l2, _i2 = v.tone(cool_tone)
        p.setPen(v.pen(l2, 1.2, sym.line_style(cool_tone)))
        p.setBrush(f2)
        p.drawRoundedRect(CHANNEL, 6, 6)
        p.setPen(v.pen(l2, 1.4, Qt.PenStyle.DashLine))
        p.drawLine(QPointF(CHANNEL.left() + 16, AXIS), QPointF(CHANNEL.right() - 24, AXIS))
        p.drawLine(QPointF(CHANNEL.right() - 36, AXIS - 7), QPointF(CHANNEL.right() - 24, AXIS))
        p.drawLine(QPointF(CHANNEL.right() - 36, AXIS + 7), QPointF(CHANNEL.right() - 24, AXIS))
        p.setPen(v.pen(v.c("text"), 1.6))
        p.setBrush(v.c("text_muted"))
        p.drawRect(TARGET)
        p.restore()
        v.ring("target", CATHODE.united(TARGET))
        v.ring("coolant", CHANNEL)
        # The caesium ions and the electrons, while their supplies are on
        if self._on("Thermionic") and self._on("Target"):
            self._rays(v, IONISER_R - 12, 0, v.tone("ok")[1], Qt.PenStyle.SolidLine, 9)
        if self._on("Filament") and self._on("Thermionic"):
            self._rays(v, FILAMENT_R - 14, IONISER_R + 14, v.tone("info")[1], Qt.PenStyle.DashLine, 7)
        # The ioniser and the filament
        for key, r, w, name in (("ioniser", IONISER_R, 11, "Thermionic"), ("filament", FILAMENT_R, 16, "Filament")):
            tone = supply_tone(ctrl[name])
            p.save()
            f, line_c, _ink = v.tone(sym.soft(tone) if key == "ioniser" else tone)
            p.setPen(v.pen(line_c, 1.8, sym.line_style(tone)))
            p.setBrush(f)
            if key == "ioniser":
                p.drawPath(_arc_band(r, w))
            else:
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(v.pen(line_c, 2.2, sym.line_style(tone)))
                p.drawPath(self._zigzag(r, w))
            p.restore()
            v.ring(key, self._box[key], radius=14)
        # The extraction electrode, and the Einzel lens downstream
        ext_tone = supply_tone(ctrl["Extraction"])
        p.save()
        f, line_c, _ink = v.tone(sym.soft(ext_tone))
        p.setPen(v.pen(line_c, 1.8, sym.line_style(ext_tone)))
        p.setBrush(f)
        for top, bottom in ((AXIS - GAP - 150, AXIS - GAP), (AXIS + GAP, AXIS + GAP + 150)):
            p.drawRect(QRectF(EXTRACTION_X - 14, top, 28, bottom - top))
        p.restore()
        v.ring("extraction", self._box["extraction"])
        sym.einzel(v, QPointF(EINZEL_X, AXIS), False, supply_tone(ctrl["Source Einzel"]), half_gap=24, depth=26)
        v.ring("einzel", self._box["einzel"])
        self._labels(v)
        v.legend(self.ORIGIN.x() + self.SIZE.width() - 8, self.ORIGIN.y() + self.SIZE.height() - 8, [[
            (lambda vv, c, s, t=tone: sym.swatch(vv, c, s, t), text) for tone, text in (
                ("on", "On"), ("off", "Off"), ("info", "Automatic"), ("seq", "Held by a sequence"),
                ("warn", "Warning, arc quench"), ("fault", "Fault, locked"), ("unknown", "No live reading"))]],
                 R | B)

    def _cond_tone(self, c):
        if c is None or c.state == il.UNKNOWN:
            return "unknown"
        return "ok" if c.state == il.OK else "warn"

    def _rays(self, v, r_from, r_to, colour, style, n):
        """Lines from one arc in to another (or to the target's face, r_to 0)."""
        p = v.p
        p.save()
        c = QColor(colour)
        c.setAlpha(150)
        p.setPen(v.pen(c, 1.2, style))
        for i in range(n):
            deg = -ARC_DEG * 0.85 + i * (2 * ARC_DEG * 0.85) / (n - 1)
            a = _arc_point(r_from, deg)
            b = _arc_point(r_to, deg) if r_to else QPointF(FACE.x() + 2, AXIS + (i - (n - 1) / 2) * 6)
            p.drawLine(a, b)
        p.restore()

    @staticmethod
    def _zigzag(r: float, w: float) -> QPainterPath:
        """The filament: a wire zig-zagging along its arc."""
        path = QPainterPath()
        steps = 26
        for i in range(steps + 1):
            deg = -ARC_DEG + i * 2 * ARC_DEG / steps
            pt = _arc_point(r + (w * 0.7 if i % 2 else -w * 0.7), deg)
            path.moveTo(pt) if i == 0 else path.lineTo(pt)
        return path

    def _beam(self, v):
        """The negative ions, from the target's face out through the extraction gap: drawn while the
        extraction and target supplies are on."""
        if not (self._on("Extraction") and self._on("Target")):
            return
        p = v.p
        p.save()
        c = QColor(v.tone("ok")[1])
        c.setAlpha(70)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        path = QPainterPath(QPointF(FACE.x(), AXIS - 6))
        path.lineTo(QPointF(EXTRACTION_X, AXIS - GAP * 0.7))
        path.lineTo(QPointF(EINZEL_X, AXIS - 14))
        path.lineTo(QPointF(END_X - 30, AXIS - 8))
        path.lineTo(QPointF(END_X - 30, AXIS + 8))
        path.lineTo(QPointF(EINZEL_X, AXIS + 14))
        path.lineTo(QPointF(EXTRACTION_X, AXIS + GAP * 0.7))
        path.lineTo(QPointF(FACE.x(), AXIS + 6))
        path.closeSubpath()
        p.drawPath(path)
        p.setPen(v.pen(v.tone("ok")[1], 1.6))
        p.drawLine(QPointF(FACE.x() + 4, AXIS), QPointF(END_X - 20, AXIS))
        p.drawLine(QPointF(END_X - 34, AXIS - 8), QPointF(END_X - 20, AXIS))
        p.drawLine(QPointF(END_X - 34, AXIS + 8), QPointF(END_X - 20, AXIS))
        p.restore()

    def _supply_block(self, v, x, y, align, title, name, extra_rows=(), stack=False):
        """A supply's name, readback and chip (beside the readback, or under it: stack)."""
        ctrl = self.src.psu_controls[name]
        text, tone = supply_chip(ctrl)
        value, chip = ("value", readback(ctrl)), ("chip", text, tone)
        rows = [[("name-b", title)]] + ([[value], [chip]] if stack else [[value, chip]])
        if "lbl_rb2" in ctrl:
            rows.append([("note", ctrl["lbl_rb2"].text())])
        v.block(x, y, rows + list(extra_rows), align)

    def _labels(self, v):
        ctrl = self.src.psu_controls
        v.block(BODY.left() + 22, BODY.top() + 18, [[("name-b", "Source body"), ("note", "in vacuum")],
                                                     [("note", "body"), ("value", self.src.lbl_body.text())]], L | T)
        self._supply_block(v, CATHODE.center().x() + 20, CATHODE.top() - 14, HC | B, "Target (cathode)", "Target")
        cool = self.coolant
        chip = "" if cool is not None and cool.state == il.OK else (cool.detail.upper() if cool is not None
                                                                     else "NOT KNOWN")
        v.block(CHANNEL.center().x(), CATHODE.bottom() + 12, [[("note", "Cooling water")],
                                                              [("chip", chip, self._cond_tone(cool))]], HC | T)
        auto = ctrl["Thermionic"].get("btn_auto")
        auto_row = [("chip", "AUTO EMISSION" if auto is not None and auto.isChecked() else "", "info")]
        top = _arc_point(IONISER_R, -ARC_DEG)
        self._supply_block(v, top.x() - 110, top.y() - 22, HC | B, "Ioniser (emission)", "Thermionic", [auto_row],
                           stack=True)
        bottom = _arc_point(FILAMENT_R, ARC_DEG)
        self._supply_block(v, bottom.x() + 72, bottom.y() + 20, L | VC, "W filament", "Filament", stack=True)
        cs = ctrl["Cesium"]
        text, tone = supply_chip(cs)
        cool_btn = cs.get("btn_cool")
        forced = cool_btn.text() if cool_btn is not None and cool_btn.isChecked() else ""
        v.block(OVEN.right() + 24, OVEN.center().y(), [
            [("name-b", "Caesium oven")], [("value", readback(cs)), ("chip", text, tone)],
            [("note", cs["lbl_rb2"].text())], [("chip", forced, cool_btn.property("tone") if cool_btn else None)]], L | VC)
        self._supply_block(v, EXTRACTION_X - 14, AXIS - GAP - 170, L | B, "Extraction", "Extraction")
        self._supply_block(v, EINZEL_X, AXIS - 76, HC | B, "Source Einzel lens", "Source Einzel", stack=True)
        energy = f"{self.energy:.1f} keV" if self.energy is not None else "--- keV"
        v.block(END_X - 100, AXIS - 26, [[("name-b", "Negative-ion beam")], [("value-b", energy)],
                                         [("note", "to the analysing magnet")]], HC | B)

    # --- faceplates ---------------------------------------------------------------------------
    def _supply_plate(self, key, title, subtitle, name, note=""):
        fp = Faceplate(self, key, title, subtitle)
        supply_rows(fp, self.src.psu_controls[name])
        if note:
            fp.note(note)
        fp.link(PANEL_LINK, lambda: self.show_panel("source"))
        return fp

    def faceplate_for(self, key: str):
        if key == "target":
            return self._supply_plate(key, "Target (cathode)", "The caesium ions sputter it; it sits below the body",
                                      "Target")
        if key == "ioniser":
            return self._supply_plate(key, "Ioniser: electron emission", "The Thermionic supply", "Thermionic",
                                      "With AUTO EMISSION on, the PLC sets the filament current to hold this "
                                      "emission.")
        if key == "filament":
            return self._supply_plate(key, "Tungsten filament", "Its electrons heat the ioniser", "Filament")
        if key == "oven":
            return self._supply_plate(key, "Caesium oven", "Feeds caesium vapour to the ioniser", "Cesium")
        if key == "extraction":
            return self._supply_plate(key, "Extraction", "The source body, below the earthed extraction electrode",
                                      "Extraction")
        if key == "einzel":
            return self._supply_plate(key, "Source Einzel lens", "Spellman supply", "Source Einzel")
        if key == "coolant":
            fp = Faceplate(self, key, "Cooling water", "The source coolant loop, through the cathode")
            chip = fp.chip("", "unknown")
            fp.row("Flow", chip)
            detail = fp.note("")

            def show(c=chip, d=detail):
                from src.gui.style import set_tone
                cond = self.coolant
                c.setText("OK" if cond is not None and cond.state == il.OK else
                          "HOLDING" if cond is not None and cond.state == il.HOLDING else "NOT KNOWN")
                set_tone(c, self._cond_tone(cond))
                d.setText(cond.detail if cond is not None else "No data from the PLC.")
            fp.every_refresh(show)
            fp.link("Interlocks panel →", lambda: self.show_panel("interlocks"))
            return fp
        return None
