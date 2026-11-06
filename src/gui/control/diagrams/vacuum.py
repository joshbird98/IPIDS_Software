"""The Vacuum Diagram: the vacuum system laid out as in the thesis' drawing of it (arch_vacuum) - the
source column with the target loadlock and the argon line at the top, the 90 degree magnet, the
beamline and the angled end-station with its sample loadlock - with every pump, gate valve and gauge.

What the software reads is live: the six gauges, the source turbo pump, the source gate valve (GV2)
and the loadlock turbo's faults. Their colours, values and faceplates are the Vacuum System panel's
(the faceplates' buttons are its buttons: mirror.py). The other pumps, the gate valves GV1, GV3 and
GV4, the hand valves and the argon line are drawn dotted: the software can't see them, so the
diagram doesn't pretend to know their state. A tube's bore turns amber or red where its gauge warns
or has tripped.

The source turbo can only be started here, as on the panel: it is stopped by hand, at the pump.
"""
from PyQt6.QtCore import QPointF, QRectF, QSize, QSizeF, Qt
from PyQt6.QtGui import QPainterPath

from src.gui.control.diagrams import symbols as sym
from src.gui.control.diagrams.canvas import Part, path_through
from src.gui.control.diagrams.faceplate import Faceplate
from src.gui.control.diagrams.mirror import MirrorButton, MirrorLabel
from src.gui.control.diagrams.panel import DiagramPanel
from src.gui.control.diagrams.plates import gv2_plate, not_read_plate

L, R, HC = Qt.AlignmentFlag.AlignLeft, Qt.AlignmentFlag.AlignRight, Qt.AlignmentFlag.AlignHCenter
T, B, VC = Qt.AlignmentFlag.AlignTop, Qt.AlignmentFlag.AlignBottom, Qt.AlignmentFlag.AlignVCenter

COLUMN_X, TOP_Y, BEAM_Y = 215.0, 62.0, 1115.0
ARC_C, ARC_R = QPointF(605, 725), 390.0
TUBE_W = 18.0
GAUGE_S, PUMP_R = 70.0, 50.0

# Gauges: centre of the box, the line to the vessel, where the readout goes (x, y, align), and
# whether its chip (shown when it isn't healthy) goes beside the name or under the pressure
GAUGES = {
    1: dict(name="VG1  Source", at=(372, 195), line=[(372, 240), (372, 230)], label=(338, 153, L | B),
            chip="beside"),             # (from the box's left edge: GV1 is close on its left, the room is right)
    2: dict(name="VG2  Beamline pre-magnet", at=(90, 545), line=[(125, 545), (COLUMN_X, 545)],
            label=(90, 588, HC | T), chip="under"),
    3: dict(name="VG3  Beamline", at=(868, 1210), line=[(868, BEAM_Y), (868, 1175)], label=(868, 1253, HC | T),
            chip="beside"),
    4: dict(name="VG4  End-station 1", at=(1215, 1210), line=[(1215, BEAM_Y), (1215, 1175)],
            label=(1215, 1253, HC | T), chip="beside"),
    5: dict(name="VG5  Sample loadlock", at=(1180, 950), line=[(1215, 950), (1281, 950)], label=(1137, 950, R | VC),
            chip="beside"),
    6: dict(name="VG6  End-station 2", at=(1445, 975), line=[(1445, 1010), (1445, 1072)], label=(1445, 932, HC | B),
            chip="under"),
}
LOCATIONS = {1: "source", 2: "beamline", 3: "beamline", 4: "endstation", 5: "loadlock", 6: "endstation"}

# Pumps: kind, centre, L/s, and whether (and how) the software reads it
PUMPS = {
    "turbo_source": dict(kind="turbo", at=(372, 312), size="350 L/s", name="Source turbo pump", live="source"),
    "turbo_column": dict(kind="turbo", at=(320, 660), size="350 L/s", name="Column turbo pump"),
    "turbo_beamline": dict(kind="turbo", at=(800, 1022), size="350 L/s", name="Beamline turbo pump"),
    "turbo_endstation": dict(kind="turbo", at=(1540, 1065), size="700 L/s", name="End-station turbo pump"),
    "turbo_loadlock": dict(kind="turbo", at=(1380, 775), size="150 L/s", name="Sample loadlock turbo pump",
                           live="loadlock"),
    "ion_pump": dict(kind="ion", at=(1408, 1215), size="75 L/s", name="End-station ion pump"),
    "scroll_source": dict(kind="scroll", at=(840, 415), size="corrosion-resistant", name="Source backing pump"),
    "scroll_beamline": dict(kind="scroll", at=(484, 905), size="", name="Column and beamline backing pump"),
    "scroll_endstation": dict(kind="scroll", at=(1632, 1215), size="", name="End-station and loadlock backing pump"),
}
# Where the unread pumps' sizes go (x, y, align)
PUMP_LABELS = {"turbo_column": (320, 600, HC | B), "turbo_beamline": (738, 1022, R | VC),
               "turbo_endstation": (1540, 1125, HC | T), "ion_pump": (1468, 1215, L | VC)}
GATE_VALVES = {
    "GV1": dict(at=(COLUMN_X, 108), vertical=True, name="GV1  Target loadlock", label=(178, 108, R | VC)),
    "GV2": dict(at=(COLUMN_X, 322), vertical=True, name="GV2  Source gate valve", live=True,
                label=(176, 322, R | VC)),
    "GV3": dict(at=(1120, BEAM_Y), vertical=False, name="GV3  End-station", label=(1120, 1152, HC | T)),
    "GV4": dict(at=(1301, 1030), vertical=True, name="GV4  Sample loadlock", label=(1340, 1030, L | VC)),
}
HAND_VALVES = [((262, 65), False), ((785, 65), False), ((440, 415), False), ((418, 660), False),
               ((800, 930), True), ((1632, 1120), True), ((1525, 775), False)]
ARGON = QRectF(865, 90, 52, 190)        # the cylinder

TURBO_TIP = "The source turbo pump. It is started from here or its panel, and stopped only by hand, at the pump."


class VacuumDiagram(DiagramPanel):
    ORIGIN = QPointF(-70, 0)
    SIZE = QSizeF(1790, 1340)
    DESIGN_SCALE = 0.55
    MINIMUM = QSize(860, 660)

    def __init__(self, panels: dict, show_panel=None):
        super().__init__(panels, show_panel)
        self.vac = panels["vacuum"]
        self.gauge_state = {n: ("--- mbar", "unknown", "OFFLINE", True) for n in GAUGES}
        self.turbo = ("unknown", "OFFLINE", None)
        self.guard = ("", None)
        self.loadlock = ("unknown", "")
        self.gv2 = ("unknown", "UNKNOWN")
        self._parts = self._make_parts()

    # --- what is where --------------------------------------------------------------------------
    def _make_parts(self):
        parts = []
        for n, g in GAUGES.items():
            x, y = g["at"]
            parts.append(Part(f"VG{n}", QRectF(x - GAUGE_S / 2, y - GAUGE_S / 2, GAUGE_S, GAUGE_S), g["name"]))
        for key, pump in PUMPS.items():
            x, y = pump["at"]
            r = self._radius(pump)
            tip = pump["name"] + (f" ({pump['size']})" if pump["size"] else "")
            if not pump.get("live"):
                tip += ". Not read by the software."
            parts.append(Part(key, QRectF(x - r, y - r, 2 * r, 2 * r), tip, round=True))
        for key, gv in GATE_VALVES.items():
            x, y = gv["at"]
            parts.append(Part(key, QRectF(x - 32, y - 32, 64, 64),
                              gv["name"] + ("" if gv.get("live") else ". Not read by the software.")))
        parts.append(Part("argon", ARGON.adjusted(-8, -28, 8, 0), "Argon, 200 bar, to vent the target loadlock. "
                                                                  "Not read by the software."))
        return parts

    @staticmethod
    def _radius(pump):
        return PUMP_R + (5 if pump["kind"] == "scroll" else 0)

    def parts(self):
        return self._parts

    # --- the telemetry, as the Vacuum System panel shows it ---------------------------------
    def read(self, data: dict):
        vac = self.vac
        for n in GAUGES:
            g = vac.gauges[n]
            stale = g["val"].property("ink") == "muted"
            self.gauge_state[n] = (g["val"].text(), g["status"].property("tone") or "unknown",
                                   g["status"].text(), stale)
        speed_pct = data.get("ion_beam.source.turbo_pump.rb_speed_pct")
        speed_hz = data.get("ion_beam.source.turbo_pump.rb_speed_hz")
        spin = (speed_pct / 100.0 if speed_pct is not None else
                speed_hz / 1000.0 if speed_hz is not None else None)
        self.turbo = (vac.lbl_turbo_status.property("tone") or "unknown", vac.lbl_turbo_status.text(),
                      spin if vac.lbl_turbo_speed.property("ink") != "muted" else None)
        guard = vac.lbl_turbo_guard
        self.guard = ("", None) if guard.isHidden() else (guard.text().split(":")[0], guard.property("tone"))
        self.gv2 = (vac.lbl_gv_status.property("tone") or "unknown", vac.lbl_gv_status.text())
        self.loadlock = self._loadlock_state(data)

    @staticmethod
    def _loadlock_state(data: dict):
        """(tone, chip) for the loadlock turbo, from the faults its controller reports to the PLC. Grey
        when it reports none: the software can't tell whether it is running, so it isn't drawn green."""
        base = "ion_beam.loadlock.turbo_pump"
        flags = {k: data.get(f"{base}.stat_{k}") for k in ("comms_fail", "error", "trip", "warning")}
        if not data.get("system.connected") or any(v is None for v in flags.values()):
            return "unknown", "NO DATA"
        if flags["comms_fail"]:
            return "unknown", "NOT ANSWERING"
        if flags["trip"] or flags["error"]:
            return "fault", "TRIPPED" if flags["trip"] else "ERROR"
        if flags["warning"]:
            return "warn", "WARNING"
        return "off", "NO FAULT"

    # --- drawing ------------------------------------------------------------------------------
    def _bore(self, v, gauge_n):
        """The bore of a tube near a gauge: tinted when that gauge warns or has tripped."""
        _text, tone, _chip, stale = self.gauge_state[gauge_n]
        if stale:
            return None
        if tone == "fault":
            return v.c("fault_bg")
        if tone == "warn":
            return v.c("warn_bg")
        return None

    def paint(self, v):
        # Pumping lines and the argon line, under the vessels
        pipes = [
            [(COLUMN_X, 240), (372, 240), (372, 262)],                  # source: VG1 and its turbo
            [(372, 362), (372, 415), (785, 415)],                       # the source turbo's backing
            [(COLUMN_X, TOP_Y + 3), (891, TOP_Y + 3), (891, 70)],       # argon to the target loadlock
            [(730, TOP_Y + 3), (730, 415)],                             # argon to the backing line
            [(COLUMN_X, 660), (270, 660)], [(370, 660), (484, 660), (484, 850)],
            [(484, 818), (800, 818), (800, 972)], [(800, 1072), (800, BEAM_Y)],
            [(1408, 1077), (1408, 1165)],                               # ion pump
            [(1590, 1065), (1632, 1065), (1632, 1160)],                 # the end-station turbo's backing
            [(1272, 775), (1330, 775)], [(1430, 775), (1632, 775), (1632, 1065)],
        ] + [g["line"] for g in GAUGES.values()]
        for pts in pipes:
            sym.line(v, path_through(pts), 2.2, v.c("border_strong"))

        # The vessels: column, magnet chamber, beamline, end-station, sample loadlock
        column = path_through([(COLUMN_X, TOP_Y), (COLUMN_X, 425)])
        column_low = path_through([(COLUMN_X, 425), (COLUMN_X, ARC_C.y())])
        arc = QPainterPath(QPointF(COLUMN_X, ARC_C.y()))
        arc.arcTo(QRectF(ARC_C.x() - ARC_R, ARC_C.y() - ARC_R, 2 * ARC_R, 2 * ARC_R), 180, 90)
        beamline = path_through([(ARC_C.x(), BEAM_Y), (1088, BEAM_Y)])
        station = path_through([(1152, BEAM_Y), (1240, BEAM_Y), (1395, 1079), (1490, 1066)])
        loadlock = path_through([(1312, 1088), (1300, 1040), (1283, 962), (1272, 915), (1272, 760)])
        sym.tubes(v, [(column, TUBE_W, self._bore(v, 1)), (column_low, TUBE_W, self._bore(v, 2)),
                      (arc, TUBE_W * 0.8, None), (beamline, TUBE_W, self._bore(v, 3)),
                      (station, TUBE_W * 1.5, self._bore(v, 4)), (loadlock, TUBE_W * 0.9, self._bore(v, 5))])

        # Hand valves, the argon line's valves and cylinder (never read)
        for (x, y), vertical in HAND_VALVES:
            sym.valve(v, QPointF(x, y), 26, None, vertical=vertical)
        sym.three_way(v, QPointF(730, TOP_Y + 3), 28, None)
        sym.valve(v, QPointF(730, 150), 26, None, vertical=True, actuator="pneumatic")
        sym.regulator(v, QPointF(840, TOP_Y + 3), 26, None)
        sym.cylinder(v, ARGON, None)
        v.ring("argon", ARGON.adjusted(-8, -28, 8, 0))

        # Gate valves
        for key, gv in GATE_VALVES.items():
            tone = self.gv2[0] if gv.get("live") else None
            c = QPointF(*gv["at"])
            sym.valve(v, c, 64, tone, vertical=gv["vertical"], gate=True)
            v.ring(key, QRectF(c.x() - 32, c.y() - 32, 64, 64))

        # Pumps
        for key, pump in PUMPS.items():
            c = QPointF(*pump["at"])
            r = self._radius(pump)
            if pump.get("live") == "source":
                tone, spin = self.turbo[0], self.turbo[2]
            elif pump.get("live") == "loadlock":
                tone, spin = self.loadlock[0], None
            else:
                tone, spin = None, None
            if pump["kind"] == "turbo":
                sym.turbo(v, c, r, tone, spin)
            elif pump["kind"] == "scroll":
                sym.scroll(v, c, r, tone)
            else:
                sym.ion_pump(v, c, r, tone)
            v.ring(key, QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r), radius=r)

        # Gauges
        for n, g in GAUGES.items():
            c = QPointF(*g["at"])
            _text, tone, _chip, stale = self.gauge_state[n]
            sym.gauge(v, c, GAUGE_S, "unknown" if stale else tone)
            v.ring(f"VG{n}", QRectF(c.x() - GAUGE_S / 2, c.y() - GAUGE_S / 2, GAUGE_S, GAUGE_S))

        self._labels(v)
        self._legend(v)

    def _labels(self, v):
        # Gauges: the name and pressure; a chip only when it isn't healthy
        for n, g in GAUGES.items():
            text, tone, chip, stale = self.gauge_state[n]
            x, y, align = g["label"]
            chip_item = ("chip", chip if tone != "ok" else "", tone)
            value = ("value-b", text, v.c("text_faint") if stale else None)
            name = ("name-b", f"VG{n}")
            rows = [[name, chip_item], [value]] if g["chip"] == "beside" else [[name], [value], [chip_item]]
            v.block(x, y, rows, align)
        # The pumps the software reads
        tone, chip, _spin = self.turbo
        speed = self.vac.lbl_turbo_speed.text()
        stale = self.vac.lbl_turbo_speed.property("ink") == "muted"
        guard, guard_tone = self.guard
        v.block(445, 312, [[("name-b", "Source turbo")], [("value-b", speed, v.c("text_faint") if stale else None)],
                           [("chip", chip, tone)], [("chip", guard, guard_tone)], [("note", "350 L/s")]], L | VC)
        tone, chip = self.loadlock
        v.block(1380, 715, [[("name-b", "Loadlock turbo")], [("note", "150 L/s")], [("chip", chip, tone)]], HC | B)
        # The others: their size, as on the thesis' drawing
        for key, (x, y, align) in PUMP_LABELS.items():
            v.text(x, y, PUMPS[key]["size"], "note", align)
        v.text(840, 478, "Corrosion-resistant", "note", HC | T)
        # Gate valves
        for key, gv in GATE_VALVES.items():
            x, y, align = gv["label"]
            if gv.get("live"):
                tone, chip = self.gv2
                v.block(x, y, [[("name-b", key)], [("chip", chip, tone)]], align)
            else:
                v.text(x, y, key, "name", align)
        # Vessels
        v.text(COLUMN_X, 28, "Target loadlock", "name-b", HC | VC)
        v.text(178, 205, "TESS\nion source", "name-b", R | VC)
        v.text(420, 1192, "90° magnet chamber\nlow conductance, about 6.8 L/s", "note", HC | T)
        v.text(960, 1078, "Beamline", "name-b", HC | B)
        v.text(1180, 1078, "End-station", "name-b", HC | B)
        v.text(1255, 790, "Sample loadlock", "name-b", R | VC)
        v.text(ARGON.center().x(), ARGON.bottom() + 20, "Argon, 200 bar", "note", HC | T)

    def _legend(self, v):
        def kind(draw):
            return lambda vv, c, s: draw(vv, c, s)

        kinds = [(kind(lambda vv, c, s: sym.turbo(vv, c, s * 0.5, "off")), "Turbo pump"),
                 (kind(lambda vv, c, s: sym.scroll(vv, c, s * 0.5, "off")), "Scroll pump"),
                 (kind(lambda vv, c, s: sym.ion_pump(vv, c, s * 0.5, "off")), "Ion pump"),
                 (kind(lambda vv, c, s: sym.gauge(vv, c, s * 0.85, "off")), "Vacuum gauge"),
                 (kind(lambda vv, c, s: sym.valve(vv, c, s, "off", gate=True)), "Gate valve"),
                 (kind(lambda vv, c, s: sym.valve(vv, c, s * 0.8, "off")), "Hand valve")]
        states = [(lambda vv, c, s, t=tone: sym.swatch(vv, c, s, t), text) for tone, text in (
            ("ok", "On, open, healthy"), ("off", "Off, closed"), ("info", "Starting, slowing"),
            ("warn", "Warning"), ("fault", "Fault, tripped"), ("unknown", "No live reading"),
            (None, "Not read by the software"))]
        v.legend(self.ORIGIN.x() + self.SIZE.width() - 8, 14, [kinds, states])

    # --- faceplates ---------------------------------------------------------------------------
    def faceplate_for(self, key: str):
        vac = self.vac
        if key.startswith("VG"):
            n = int(key[2:])
            g = vac.gauges[n]
            fp = Faceplate(self, key, GAUGES[n]["name"].replace("  ", ": "),
                           f"Vacuum gauge, in the {LOCATIONS[n]}")
            fp.row("Pressure", MirrorLabel(g["val"], role="readout"))
            fp.row("State", MirrorLabel(g["status"]))
            fp.note("The gauges' set points and trips are in vac_gauges_config.json (Config).")
            fp.link("Vacuum System panel →", lambda: self.show_panel("vacuum"))
            return fp
        if key == "turbo_source":
            fp = Faceplate(self, key, "Source turbo pump", "350 L/s, on the source chamber")
            fp.row("Speed", MirrorLabel(vac.lbl_turbo_speed))
            fp.row("Current", MirrorLabel(vac.lbl_turbo_current))
            fp.row("Bearing, converter", MirrorLabel(vac.lbl_turbo_temps))
            fp.wide(MirrorLabel(vac.lbl_turbo_status))
            fp.wide(MirrorLabel(vac.lbl_turbo_guard))
            fp.wide(MirrorButton(vac.btn_turbo_start))
            fp.note("It is stopped only by hand, at the pump, by trained staff: never from the software.")
            fp.link("Vacuum System panel →", lambda: self.show_panel("vacuum"))
            return fp
        if key == "GV2":
            return gv2_plate(self, key, vac)
        if key == "turbo_loadlock":
            fp = Faceplate(self, key, "Sample loadlock turbo pump", "150 L/s")
            chip = fp.chip()
            fp.wide(chip)

            def show(c=chip):
                from src.gui.style import set_tone
                tone, text = self.loadlock
                c.setText(text)
                set_tone(c, tone)
            fp.every_refresh(show)
            fp.note("The software reads only its controller's fault outputs (through the PLC): not its speed, "
                    "and not whether it is running.")
            return fp
        names = {**{k: (p["name"], p["size"]) for k, p in PUMPS.items()},
                 **{k: (g["name"].replace("  ", ": "), "") for k, g in GATE_VALVES.items()},
                 "argon": ("Argon line", "200 bar cylinder, to vent the target loadlock")}
        if key in names:
            name, sub = names[key]
            what = "valve" if key.startswith("GV") else "line" if key == "argon" else "pump"
            return not_read_plate(self, key, name, sub, what)
        return None
