"""The Beam Path Diagram: the optics along the beam as the thesis' drawing lays them out (arch_general) -
down the source column from TESS through the source Einzel lens, GV2, the steerers, the object slits
and the aperture ladder, round the 90 degree magnet (or straight on, to the straight-through cup),
and along the beamline through the mass-resolving slits, the implant stage, the drop-in Faraday cup,
the beamline Einzel lens, GV3 and the neutral trap to the end-station.

Each element shows what its panel shows (Beamline Optics, TESS Ion Source, Vacuum System, Faraday Cup
& SMU, Current Monitoring) and its faceplate has that panel's controls (mirror.py). The beam's path
is worked out, not measured: it starts when the extraction supply is on, stops at GV2 unless it is
open, goes round the magnet when the magnet is on (straight on when it is off), and stops at the
drop-in cup when it is in. GV3 isn't read, so past it the path is drawn dotted.
"""
from PyQt6.QtCore import QPointF, QRectF, QSize, QSizeF, Qt
from PyQt6.QtGui import QColor, QPainterPath

from src.core.mass_calibration import mass_for_current
from src.gui.control.diagrams import symbols as sym
from src.gui.control.diagrams.canvas import Part, path_through
from src.gui.control.diagrams.faceplate import Faceplate
from src.gui.control.diagrams.mirror import MirrorButton, MirrorLabel, MirrorSetpoint
from src.gui.control.diagrams.panel import DiagramPanel
from src.gui.control.diagrams.plates import (gv2_plate, not_read_plate, plain_tone, readback, supply_chip,
                                             supply_rows, supply_tone)

L, R, HC = Qt.AlignmentFlag.AlignLeft, Qt.AlignmentFlag.AlignRight, Qt.AlignmentFlag.AlignHCenter
T, B, VC = Qt.AlignmentFlag.AlignTop, Qt.AlignmentFlag.AlignBottom, Qt.AlignmentFlag.AlignVCenter

CX = 300.0                              # the source column
SOURCE = QRectF(CX - 60, 40, 120, 105)
EINZEL_Y, GV2_Y, Y_STEER_Y, X_STEER_Y, OBJ_Y, LADDER_Y = 235.0, 330.0, 405.0, 468.0, 548.0, 618.0
MAG_Y, BEND_R = 760.0, 260.0            # the magnet's entrance, and its radius
MAG_C = QPointF(CX + BEND_R, MAG_Y)
BY = MAG_Y + BEND_R                     # the beamline
STRAIGHT_END = 1172.0                   # the straight-through cup
IMG_X, STAGE_X, FC_X, EINZEL_BL_X, GV3_X, TRAP_X = 760.0, 930.0, 1110.0, 1290.0, 1440.0, 1570.0
STATION = [(1615.0, BY), (1800.0, BY - 43)]
TUBE = 34.0
NAME_DX = 80.0                          # names left of the column, values right of it
NAME_DY, VALUE_DY = 60.0, 62.0          # names under the beamline, values over it

TONES = {"on": "on", "off": "off", "unknown": "unknown"}


def _cup_state(btn) -> object:
    """True (in the beam), False (out of it) or None, from the Faraday panel's button."""
    text = btn.text()
    if text.endswith("INSERTED"):
        return True
    if text.endswith("RETRACTED"):
        return False
    return None


class BeamPathDiagram(DiagramPanel):
    ORIGIN = QPointF(-40, 0)
    SIZE = QSizeF(1950, 1235)
    DESIGN_SCALE = 0.55
    READABLE = QSize(900, 600)

    def __init__(self, panels: dict, show_panel=None):
        super().__init__(panels, show_panel)
        self.src = panels["source"]
        self.optics = panels["optics"]
        self.vac = panels["vacuum"]
        self.smu = panels["faraday"]
        self.currents = panels["currents"]
        self.state = {}
        self._parts = self._make_parts()
        self._box = {part.key: part.box for part in self._parts}

    # --- what is where --------------------------------------------------------------------------
    def _make_parts(self):
        def across(x, half=55, up=55, down=55):
            return QRectF(x - half, BY - up, 2 * half, up + down)

        def column(y, half=60, h=60):
            return QRectF(CX - half, y - h / 2, 2 * half, h)

        magnet = QPainterPath()
        outer = QRectF(MAG_C.x() - BEND_R - 45, MAG_C.y() - BEND_R - 45, 2 * BEND_R + 90, 2 * BEND_R + 90)
        inner = QRectF(MAG_C.x() - BEND_R + 45, MAG_C.y() - BEND_R + 45, 2 * BEND_R - 90, 2 * BEND_R - 90)
        magnet.arcMoveTo(outer, 180)
        magnet.arcTo(outer, 180, 90)
        magnet.arcTo(inner, 270, -90)
        magnet.closeSubpath()
        return [
            Part("tess", SOURCE.adjusted(0, 0, 0, 34), "The TESS ion source: its extraction and target supplies"),
            Part("einzel_src", column(EINZEL_Y, h=94), "Source Einzel lens"),
            Part("GV2", column(GV2_Y, half=34, h=64), "GV2: the source gate valve"),
            Part("steerers", QRectF(CX - 56, Y_STEER_Y - 34, 112, X_STEER_Y - Y_STEER_Y + 60),
                 "The steerers: Y electrostatic plates, X magnetic"),
            Part("object_slits", column(OBJ_Y, h=26), "Object slits: the jaws' currents"),
            Part("ladder", column(LADDER_Y, half=44, h=20), "Aperture ladder. Not read by the software."),
            Part("magnet", magnet.boundingRect(), "The 90 degree analysing magnet", shape=magnet),
            Part("straight", QRectF(CX - 40, STRAIGHT_END - 12, 80, 26), "Straight-through cup: the current that "
                                                                           "goes straight on past the magnet"),
            Part("image_slits", across(IMG_X, half=12, up=42, down=42), "Mass-resolving slits: the jaws' currents"),
            Part("stage", across(STAGE_X, half=28, up=40, down=40), "The implant stage's aperture"),
            Part("fc", QRectF(FC_X - 24, BY - 120, 48, 140), "The drop-in Faraday cup"),
            Part("einzel_bl", across(EINZEL_BL_X, half=46, up=52, down=52), "Beamline Einzel lens"),
            Part("GV3", across(GV3_X, half=32, up=32, down=32), "GV3: the end-station gate valve. "
                                                                 "Not read by the software."),
            Part("trap", across(TRAP_X, half=32, up=34, down=34), "The neutral trap"),
            Part("station", QRectF(1745, BY - 112, 120, 102), "The end-station. Not read by the software."),
        ]

    def parts(self):
        return self._parts

    # --- the telemetry, as the panels show it -------------------------------------------------
    def read(self, data: dict):
        src, opt, vac, smu = self.src.psu_controls, self.optics, self.vac, self.smu
        mon = self.currents.ui_elements
        st = {
            "extraction": src["Extraction"], "target": src["Target"], "einzel_src": src["Source Einzel"],
            "gv2": (plain_tone(vac.lbl_gv_status), vac.lbl_gv_status.text()),
            "steer": TONES.get(plain_tone(opt.btn_steer_en), plain_tone(opt.btn_steer_en)),
            "magnet": TONES.get(plain_tone(opt.btn_mag_en), plain_tone(opt.btn_mag_en)),
            "cup": _cup_state(smu.btn_fc), "cup_tone": plain_tone(smu.btn_fc),
            "obj": (mon[0]["val"], mon[1]["val"]), "straight": mon[2]["val"], "img": (mon[3]["val"], mon[4]["val"]),
            "aperture": mon[5]["val"],
        }
        st["source_on"] = plain_tone(src["Extraction"]["btn_enable"]) == "on"
        st["gv2_open"] = vac.lbl_gv_status.text() == "OPEN"
        st["energy"] = self._energy(data)
        st["mass"] = self._mass(data, st["energy"])
        self.state = st

    @staticmethod
    def _energy(data: dict):
        """The beam's energy in keV (target + extraction readbacks), as the optics panel takes it."""
        tgt, ext = data.get("ion_beam.source.target.rb_voltage"), data.get("ion_beam.source.extraction.rb_voltage")
        return None if tgt is None or ext is None else tgt + ext

    def _mass(self, data: dict, energy_kv):
        """The mass the magnet passes now, from the Mass Scan panel's calibration (as CALCULATE & SET
        AMPS uses it, the other way round); None when not known."""
        amps = data.get("ion_beam.beamline.magnet.rb_current")
        cal = self.optics.mass_calibration() if callable(getattr(self.optics, "mass_calibration", None)) else None
        if amps is None or not energy_kv or energy_kv <= 0 or not cal or cal.get("k", 0) <= 0:
            return None
        return mass_for_current(amps, energy_kv * 1000.0, cal["k"], cal["i_off"])

    # --- drawing ------------------------------------------------------------------------------
    def _bend(self) -> QPainterPath:
        path = QPainterPath(QPointF(CX, MAG_Y))
        path.arcTo(QRectF(MAG_C.x() - BEND_R, MAG_C.y() - BEND_R, 2 * BEND_R, 2 * BEND_R), 180, 90)
        return path

    def paint(self, v):
        st = self.state
        if not st:
            return
        sym.magnet_sector(v, MAG_C, BEND_R - 45, BEND_R + 45, 180, 90, sym.soft(st["magnet"]))
        v.ring("magnet", self._box['magnet'], radius=12)
        bend = self._bend()
        bend.lineTo(QPointF(STATION[0][0], BY))
        sym.tubes(v, [(path_through([(CX, SOURCE.bottom() + 30), (CX, MAG_Y)]), TUBE, None),
                      (path_through([(CX, MAG_Y), (CX, STRAIGHT_END)]), TUBE * 0.6, None),
                      (bend, TUBE, None),
                      (path_through([STATION[0], STATION[1]]), TUBE * 1.4, None)])
        self._beam(v)
        self._elements(v)
        self._labels(v)
        v.legend(self.ORIGIN.x() + self.SIZE.width() - 8, 14, [[
            (lambda vv, c, s, t=tone: sym.swatch(vv, c, s, t), text) for tone, text in (
                ("on", "On"), ("off", "Off"), ("info", "In the beam, automatic"), ("seq", "Held by a sequence"),
                ("warn", "Warning"), ("fault", "Fault"), ("unknown", "No live reading"),
                (None, "Not read by the software"))] + [
            (lambda vv, c, s: sym.beam(vv, path_through([(c.x() - s * 0.5, c.y()), (c.x() + s * 0.5, c.y())]),
                                       s * 0.3, self._beam_colour(vv)), "Beam path (worked out)")]])

    @staticmethod
    def _beam_colour(v):
        c = QColor(v.tone("ok")[1])
        c.setAlpha(150)
        return c

    def route(self) -> list:
        """Where the beam's path goes now: stretches in order, each 'column' (from the source to GV2),
        'column-on' (GV2 to the magnet), 'straight' (on to the straight-through cup), 'bend-to-cup'
        (round the magnet to the inserted cup), 'bend' (round the magnet to GV3) or 'station' (on
        past GV3: not known, dotted). 'bend' is dotted too when the cup's position isn't known."""
        st = self.state
        if not st or not st["source_on"]:
            return []
        if not st["gv2_open"]:
            return ["column"]
        out = ["column", "column-on"]
        if st["magnet"] == "off":
            return out + ["straight"]
        if st["magnet"] != "on":
            return out                                  # not known which way it goes
        if st["cup"] is True:
            return out + ["bend-to-cup"]
        return out + ["bend" if st["cup"] is False else "bend-unknown", "station"]

    def _beam(self, v):
        """The beam's path, as far as the supplies, valves, magnet and cup let it go."""
        colour, width = self._beam_colour(v), 11.0
        solid, dotted = Qt.PenStyle.SolidLine, Qt.PenStyle.DotLine
        for stretch in self.route():
            if stretch == "column":
                end = GV2_Y - 14 if not self.state["gv2_open"] else GV2_Y
                sym.beam(v, path_through([(CX, SOURCE.bottom() + 34), (CX, end)]), width, colour)
            elif stretch == "column-on":
                sym.beam(v, path_through([(CX, GV2_Y), (CX, MAG_Y)]), width, colour)
            elif stretch == "straight":
                sym.beam(v, path_through([(CX, MAG_Y), (CX, STRAIGHT_END - 10)]), width, colour)
            elif stretch.startswith("bend"):
                bend = self._bend()
                bend.lineTo(QPointF(FC_X - 16 if stretch == "bend-to-cup" else GV3_X, BY))
                sym.beam(v, bend, width, colour, dotted if stretch == "bend-unknown" else solid)
            elif stretch == "station":
                sym.beam(v, path_through([(GV3_X, BY), STATION[0], (STATION[1][0] - 40, STATION[1][1] + 9)]),
                         width, colour, dotted)

    def _elements(self, v):
        st = self.state
        sym.source_body(v, SOURCE, sym.soft(supply_tone(st["extraction"])))
        v.ring("tess", self._box['tess'])
        sym.einzel(v, QPointF(CX, EINZEL_Y), True, supply_tone(st["einzel_src"]))
        v.ring("einzel_src", self._box['einzel_src'])
        sym.valve(v, QPointF(CX, GV2_Y), 62, st["gv2"][0], vertical=True, gate=True)
        v.ring("GV2", self._box['GV2'])
        sym.deflector(v, QPointF(CX, Y_STEER_Y), True, st["steer"])
        sym.coils(v, QPointF(CX, X_STEER_Y), True, st["steer"])
        v.ring("steerers", self._box['steerers'])
        sym.jaws(v, QPointF(CX, OBJ_Y), True, "off", wall=TUBE * 0.5 + 18)
        v.ring("object_slits", self._box['object_slits'])
        sym.aperture(v, QPointF(CX, LADDER_Y), True, None, wall=TUBE * 0.5 + 10)
        v.ring("ladder", self._box['ladder'])
        sym.aperture(v, QPointF(CX, STRAIGHT_END), True, "off", wall=TUBE * 0.4, hole=0.01, thick=10)
        v.ring("straight", self._box['straight'])
        sym.jaws(v, QPointF(IMG_X, BY), False, "off", wall=TUBE * 0.5 + 18)
        v.ring("image_slits", self._box['image_slits'])
        sym.aperture(v, QPointF(STAGE_X, BY), False, "off", wall=TUBE * 0.5 + 12)
        v.ring("stage", self._box['stage'])
        sym.drop_in_cup(v, QPointF(FC_X, BY), "info" if st["cup"] is True else
                        "off" if st["cup"] is False else st["cup_tone"], st["cup"])
        v.ring("fc", self._box['fc'])
        sym.einzel(v, QPointF(EINZEL_BL_X, BY), False, supply_tone(self.optics.psu_controls["Beamline Einzel"]))
        v.ring("einzel_bl", self._box['einzel_bl'])
        sym.valve(v, QPointF(GV3_X, BY), 62, None, gate=True)
        v.ring("GV3", self._box['GV3'])
        sym.deflector(v, QPointF(TRAP_X, BY), False, self._trap_tone(), half_gap=24, length=52)
        v.ring("trap", self._box['trap'])
        p = v.p                                          # the end-station's chamber, at the end of its arm
        p.save()
        p.setPen(v.pen(v.c("text_faint"), 1.8, Qt.PenStyle.DotLine))
        p.setBrush(v.c("card"))
        p.drawRoundedRect(self._box['station'].adjusted(8, 8, -8, -8), 12, 12)
        p.restore()
        v.ring("station", self._box['station'])

    def _trap_tone(self):
        tones = {supply_tone(self.optics.psu_controls[n]) for n in ("Neutral Trap (Pos)", "Neutral Trap (Neg)")}
        if len(tones) == 1:
            return tones.pop()
        for worse in ("fault", "warn", "unknown", "seq", "info", "on"):
            if worse in tones:
                return worse
        return "off"

    @staticmethod
    def _ink(v, label):
        ink = label.property("ink")
        return {"faint": v.c("text_faint"), "muted": v.c("text_faint"), "warn": v.c("warn"),
                "fault": v.c("fault")}.get(ink)

    def _value(self, v, label, kind="value"):
        return (kind, label.text(), self._ink(v, label))

    def _labels(self, v):
        st, opt = self.state, self.optics
        name_x, value_x = CX - NAME_DX, CX + NAME_DX
        # The source column: names on the left, what they read on the right
        v.block(name_x, SOURCE.center().y(), [[("name-b", "TESS ion")], [("name-b", "source")]], R | VC)
        energy = f"{st['energy']:.1f} keV" if st["energy"] is not None else "--- keV"
        v.block(value_x, SOURCE.center().y(), [
            [("note", "Extraction"), ("value", readback(st["extraction"]))],
            [("note", "Target"), ("value", readback(st["target"]))],
            [("note", "Beam"), ("value-b", energy)]], L | VC)
        v.block(name_x, EINZEL_Y, [[("name-b", "Source Einzel")], [("name-b", "lens")]], R | VC)
        text, tone = supply_chip(st["einzel_src"])
        v.block(value_x, EINZEL_Y, [[("value", readback(st["einzel_src"])), ("chip", text, tone)]], L | VC)
        v.text(name_x, GV2_Y, "GV2", "name-b", R | VC)
        v.chip(value_x, GV2_Y, st["gv2"][1], st["gv2"][0], L | VC)
        mid = (Y_STEER_Y + X_STEER_Y) / 2
        v.block(name_x, mid, [[("name-b", "Steerers")], [("note", "Y electric, X magnetic")]], R | VC)
        y_rb, x_rb = opt.lbl_y_rb.text().replace("RB: ", ""), opt.lbl_x_rb.text().replace("RB: ", "")
        steer_chip = {"on": "ON", "off": "OFF", "unknown": "NO DATA"}.get(st["steer"], opt.btn_steer_en.text())
        v.block(value_x, mid, [[("note", "Y"), ("value", y_rb)], [("note", "X"), ("value", x_rb.split(" | ")[0])],
                               [("chip", steer_chip, st["steer"])]], L | VC)
        v.text(name_x, OBJ_Y, "Object slits", "name-b", R | VC)
        left, right = st["obj"]
        v.block(value_x, OBJ_Y, [[("note", "L"), self._value(v, left)], [("note", "R"), self._value(v, right)]], L | VC)
        v.text(name_x, LADDER_Y, "Aperture ladder", "name", R | VC)
        v.block(name_x, STRAIGHT_END, [[("name-b", "Straight-")], [("name-b", "through")]], R | VC)
        v.block(value_x - 30, STRAIGHT_END, [[self._value(v, st["straight"])]], L | VC)
        # The magnet, inside its bend
        amps = opt.lbl_mag_rb.text().replace("RB: ", "").split(" | ")[0]
        mass = f"≈ {st['mass']:.1f} amu" if st["mass"] is not None else ""
        mag_chip = {"on": "ON", "off": "OFF", "unknown": "NO DATA"}.get(st["magnet"], opt.btn_mag_en.text())
        cooling = opt.lbl_mag_cooling
        cool_chip = "" if plain_tone(cooling) in ("ok", "on") else cooling.text().replace("MAG COOLING: ", "COOLING ")
        v.block(MAG_C.x() - 45, MAG_C.y() + 40, [[("name-b", "90° magnet")], [("value-b", amps)], [("note", mass)],
                                                 [("chip", mag_chip, st["magnet"])],
                                                 [("chip", cool_chip, plain_tone(cooling))]], HC | VC)
        # The beamline: names under it, what they read over it
        under, over = BY + NAME_DY, BY - VALUE_DY
        v.block(IMG_X, under, [[("name-b", "Mass-resolving")], [("name-b", "slits")]], HC | T)
        left, right = st["img"]
        v.block(IMG_X, over, [[("note", "L"), self._value(v, left)], [("note", "R"), self._value(v, right)]], HC | B)
        v.block(STAGE_X, under, [[("name-b", "Implant")], [("name-b", "stage")]], HC | T)
        v.block(STAGE_X, over, [[("note", "aperture")], [self._value(v, st["aperture"])]], HC | B)
        v.block(FC_X, under, [[("name-b", "Faraday")], [("name-b", "cup")]], HC | T)
        cup_text = self.smu.btn_fc.text().replace("FARADAY CUP: ", "")
        cup_tone = "info" if st["cup"] is True else "off" if st["cup"] is False else st["cup_tone"]
        v.block(FC_X, BY - 128, [[self._value(v, self.smu.lbl_digital_current)], [("chip", cup_text, cup_tone)]],
                HC | B)
        bl = opt.psu_controls["Beamline Einzel"]
        v.block(EINZEL_BL_X, under, [[("name-b", "Beamline")], [("name-b", "Einzel lens")]], HC | T)
        text, tone = supply_chip(bl)
        v.block(EINZEL_BL_X, over, [[("value", readback(bl).split(" | ")[0])], [("chip", text, tone)]], HC | B)
        v.text(GV3_X, under, "GV3", "name", HC | T)
        v.block(TRAP_X, under, [[("name-b", "Neutral")], [("name-b", "trap")]], HC | T)
        pos, neg = opt.psu_controls["Neutral Trap (Pos)"], opt.psu_controls["Neutral Trap (Neg)"]
        text, tone = supply_chip(pos) if supply_tone(pos) == supply_tone(neg) else ("MIXED", self._trap_tone())
        v.block(TRAP_X, over, [[("note", "+"), ("value", readback(pos).split(" | ")[0])],
                               [("note", "−"), ("value", readback(neg).split(" | ")[0])],
                               [("chip", text, tone)]], HC | B)
        v.text(self._box['station'].center().x(), BY + 8, "End-station", "name-b", HC | T)

    # --- faceplates ---------------------------------------------------------------------------
    def _link(self, fp, name, text):
        fp.link(text, lambda: self.show_panel(name))

    def _slits_plate(self, key, title, left, right, box_l, box_r, where):
        fp = Faceplate(self, key, title, where)
        fp.row("Left jaw", MirrorLabel(left["val"], role="readout"), MirrorLabel(left["status"]))
        fp.row("Right jaw", MirrorLabel(right["val"], role="readout"), MirrorLabel(right["status"]))
        fp.row("Width set (L, R)", MirrorSetpoint(box_l), MirrorSetpoint(box_r))
        fp.note("The jaws are set by hand at the machine: enter what you set, for the log.")
        self._link(fp, "optics", "Beamline Optics panel →")
        return fp

    def faceplate_for(self, key: str):
        src, opt, mon = self.src.psu_controls, self.optics, self.currents.ui_elements
        if key == "tess":
            fp = Faceplate(self, key, "TESS ion source", "The beam's energy is the target and extraction voltages")
            fp.row("Extraction", MirrorLabel(src["Extraction"]["lbl_rb"], role="readout"),
                   MirrorLabel(src["Extraction"]["lbl_rb2"]))
            fp.row("Target", MirrorLabel(src["Target"]["lbl_rb"], role="readout"),
                   MirrorLabel(src["Target"]["lbl_rb2"]))
            fp.note("The source's supplies, oven and filament are on the TESS Source Diagram and the TESS Ion Source "
                    "panel.")
            self._link(fp, "source_diagram", "TESS Source Diagram →")
            self._link(fp, "source", "TESS Ion Source panel →")
            return fp
        if key == "einzel_src":
            fp = Faceplate(self, key, "Source Einzel lens", "Three cylinders; the middle one on a Spellman supply")
            supply_rows(fp, src["Source Einzel"])
            self._link(fp, "source", "TESS Ion Source panel →")
            return fp
        if key == "GV2":
            return gv2_plate(self, key, self.vac)
        if key == "steerers":
            fp = Faceplate(self, key, "Steerers", "Y: electrostatic plates. X: a magnet")
            fp.row("Y readback", MirrorLabel(opt.lbl_y_rb, role="readout"))
            fp.row("Y setpoint", MirrorSetpoint(opt.sp_y_steer))
            fp.row("X readback", MirrorLabel(opt.lbl_x_rb, role="readout"))
            fp.row("X setpoint", MirrorSetpoint(opt.sp_x_steer))
            fp.wide(MirrorButton(opt.btn_steer_en))
            fp.note("The button switches both steerers, with two clicks to switch them on.")
            self._link(fp, "optics", "Beamline Optics panel →")
            return fp
        if key == "object_slits":
            return self._slits_plate(key, "Object slits", mon[0], mon[1], opt.sp_obj_l, opt.sp_obj_r,
                                     "Molybdenum jaws, isolated: their currents read by the ADAM module")
        if key == "magnet":
            fp = Faceplate(self, key, "90° analysing magnet", "Radius 0.75 m")
            fp.wide(MirrorLabel(opt.lbl_mag_cooling))
            fp.row("Readback", MirrorLabel(opt.lbl_mag_rb, role="readout"))
            fp.row("Setpoint", MirrorSetpoint(opt.sp_mag_current))
            mass = fp.chip("", "off")
            fp.row("Passing now", mass)

            def show_mass(lbl=mass):
                m = self.state.get("mass")
                lbl.setText(f"≈ {m:.1f} amu" if m is not None else "not known")
            fp.every_refresh(show_mass)
            fp.wide(MirrorButton(opt.btn_mag_en), MirrorButton(opt.btn_mag_deg))
            fp.note("The mass is worked out from the magnet's current, the beam's energy and the Mass Scan panel's "
                    "calibration. To set a mass, use the Beamline Optics panel.")
            self._link(fp, "optics", "Beamline Optics panel →")
            return fp
        if key == "straight":
            fp = Faceplate(self, key, "Straight-through cup", "What goes straight on past the magnet")
            fp.row("Current", MirrorLabel(mon[2]["val"], role="readout"), MirrorLabel(mon[2]["status"]))
            self._link(fp, "currents", "Current Monitoring panel →")
            return fp
        if key == "image_slits":
            return self._slits_plate(key, "Mass-resolving slits", mon[3], mon[4], opt.sp_img_l, opt.sp_img_r,
                                     "Copper-beryllium jaws with V notches, isolated")
        if key == "stage":
            fp = Faceplate(self, key, "Implant stage", "Temporary: an aperture, the sample and a cup behind it")
            fp.row("Aperture current", MirrorLabel(mon[5]["val"], role="readout"), MirrorLabel(mon[5]["status"]))
            fp.note("The 9 mm aperture's current, read by the ADAM module. The sample's actuator and bias aren't "
                    "read by the software.")
            self._link(fp, "currents", "Current Monitoring panel →")
            return fp
        if key == "fc":
            smu = self.smu
            fp = Faceplate(self, key, "Faraday cup", "Pneumatic, dropped into the beam; read by the SMU")
            fp.wide(MirrorButton(smu.btn_fc))
            fp.row("Current", MirrorLabel(smu.lbl_digital_current, role="readout"))
            fp.wide(MirrorLabel(smu.lbl_bias_status))
            fp.row("Bias", MirrorSetpoint(smu.sp_bias), MirrorLabel(smu.lbl_bias_rb))
            fp.wide(MirrorButton(smu.btn_enable_bias))
            self._link(fp, "faraday", "Faraday Cup & SMU panel →")
            return fp
        if key == "einzel_bl":
            fp = Faceplate(self, key, "Beamline Einzel lens", "Spellman supply")
            supply_rows(fp, opt.psu_controls["Beamline Einzel"])
            fp.note("The thesis' implant stage was fitted in its place for the enrichment implant.")
            self._link(fp, "optics", "Beamline Optics panel →")
            return fp
        if key == "trap":
            fp = Faceplate(self, key, "Neutral trap", "Deflector plates into the angled end-station")
            supply_rows(fp, opt.psu_controls["Neutral Trap (Pos)"], "Positive plate")
            supply_rows(fp, opt.psu_controls["Neutral Trap (Neg)"], "Negative plate")
            self._link(fp, "optics", "Beamline Optics panel →")
            return fp
        if key == "ladder":
            return not_read_plate(self, key, "Aperture ladder", "Holes of 3, 6.5 and 10 mm, moved by hand", "ladder")
        if key == "GV3":
            return not_read_plate(self, key, "GV3: end-station gate valve", "", "valve",
                                  "Past it, the beam's path is drawn dotted.")
        if key == "station":
            return not_read_plate(self, key, "End-station", "Manual XY stage and the sample loadlock", "chamber")
        return None
