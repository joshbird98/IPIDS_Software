"""The Safety Chain Diagram: what the safety relay's closing depends on, laid out as the thesis' drawing
of it (arch_safety). The PLC reads the coolant, air and gate valve switches where they are on the
machine, and gives its enable over the fibre once they all hold and you have asked (ENABLE SAFETY
RELAY, from the PC). The enable is one contact in the relay's hardwired loop, with the vacuum
controller's relay, the high-voltage enclosure's two doors and its ESO, and the operator desk's key,
START and ESO. With the loop made, the relay closes and its contactor switches the mains to every
high-voltage supply.

Every state is the Interlocks panel's (src/core/interlocks.py: its 'Safety relay' chain). The doors,
the ESOs, the key and START are wired in series where no software can see them: they are drawn
dotted, and drawn made only when the relay is closed (so all of them must be). The relay's faceplate
has the System Health panel's own buttons (mirror.py).
"""
from PyQt6.QtCore import QPointF, QRectF, QSize, QSizeF, Qt
from PyQt6.QtGui import QColor, QPainterPath
from PyQt6.QtWidgets import QLabel

from src.core import interlocks as il
from src.gui.control.diagrams import symbols as sym
from src.gui.control.diagrams.canvas import Part
from src.gui.control.diagrams.faceplate import Faceplate
from src.gui.control.diagrams.mirror import MirrorButton, MirrorLabel
from src.gui.control.diagrams.panel import DiagramPanel
from src.gui.control.diagrams.plates import gv2_plate
from src.gui.style import set_role, set_tone

L, R, HC = Qt.AlignmentFlag.AlignLeft, Qt.AlignmentFlag.AlignRight, Qt.AlignmentFlag.AlignHCenter
T, B, VC = Qt.AlignmentFlag.AlignTop, Qt.AlignmentFlag.AlignBottom, Qt.AlignmentFlag.AlignVCenter

# The places (in the drawing's units, after arch_safety)
ENCLOSURE = QRectF(170, 20, 345, 225)
QUIET = QRectF(747, 120, 380, 233)
PLC_BOX = QRectF(762, 172, 176, 132)
PC_BOX = QRectF(966, 172, 146, 132)
NOISY = QRectF(748, 417, 380, 300)
VAC_BOX = QRectF(905, 434, 210, 112)
RELAY_BOX = QRectF(840, 568, 200, 104)
DESK = QRectF(1218, 410, 270, 100)
CABINET = QRectF(724, 840, 450, 140)
COLUMN_X, BUS_X, LOOP_Y = 290.0, 655.0, 465.0
MAG_C, MAG_R = QPointF(590, 800), 300.0

# The switches: centre, along a vertical wire?, what works it, the chain's condition for it
SWITCHES = {
    "door_1": dict(at=(197, 130), vertical=True, actuator="door", name="Access door"),
    "door_2": dict(at=(197, 190), vertical=True, actuator="door", name="Access door"),
    "eso_hv": dict(at=(350, 75), vertical=False, actuator="eso", name="ESO, high-voltage enclosure"),
    "fibre": dict(at=(870, LOOP_Y), vertical=False, actuator="remote", name="The PLC's enable (fibre receiver)"),
    "vacuum": dict(at=(1005, LOOP_Y), vertical=False, actuator="remote", name="Vacuum controller relay"),
    "key": dict(at=(1272, LOOP_Y), vertical=False, actuator="key", name="Key switch"),
    "start": dict(at=(1352, LOOP_Y), vertical=False, actuator="button", name="START"),
    "eso_desk": dict(at=(1430, LOOP_Y), vertical=False, actuator="eso", name="ESO, operator desk"),
    "src_temp": dict(at=(190, 460), vertical=False, actuator="temperature", name="Source coolant temperature"),
    "src_flow": dict(at=(400, 460), vertical=False, actuator="flow", name="Source coolant flow"),
    "air": dict(at=(470, 770), vertical=False, actuator="pressure", name="Compressed air pressure"),
    "mag_temp": dict(at=(400, 1170), vertical=False, actuator="temperature", name="Magnet coolant temperature"),
    "mag_flow": dict(at=(570, 1170), vertical=False, actuator="flow", name="Magnet coolant flow"),
}
HIDDEN_KEYS = ("door_1", "door_2", "eso_hv", "key", "start", "eso_desk")
# Which of the chain's conditions each read part shows
CONDITION_OF = {"src_temp": "Source coolant flow", "src_flow": "Source coolant flow", "air": "Compressed air",
                "mag_temp": "Magnet coolant", "mag_flow": "Magnet coolant", "vacuum": "Gauge controller relay (vacuum)",
                "gv2": "Gate valve open"}
PLC_CONDITIONS = ("Compressed air", "Source coolant flow", "Magnet coolant", "Gate valve open",
                  "No relay kill (shutdown sequence)", "No caesium over-temperature")
STATE_CHIP = {il.OK: "OK", il.HOLDING: "HOLDING", il.UNKNOWN: "NOT KNOWN", il.HIDDEN: "UNSEEN", il.SUSPECT: "SUSPECT",
              il.ON: "ON", il.OFF: "OFF", il.INFO: ""}

HIDDEN_NOTE = ("In the relay's hardwired loop, in series with the other doors, ESOs, the key and START: no software "
               "sees them, only whether the relay closes. All of them are made while the relay is closed.")


class SafetyChainDiagram(DiagramPanel):
    ORIGIN = QPointF(-60, 0)
    SIZE = QSizeF(1600, 1265)
    DESIGN_SCALE = 0.55
    MINIMUM = QSize(980, 760)

    def __init__(self, panels: dict, show_panel=None):
        super().__init__(panels, show_panel)
        self.health = panels["health"]
        self.interlocks = panels["interlocks"]
        self.vac = panels["vacuum"]
        self.group = None
        self._parts = self._make_parts()
        self._box = {part.key: part.box for part in self._parts}

    @property
    def watched(self) -> list:
        return [self.interlocks, self.health, self.vac]

    # --- what is where --------------------------------------------------------------------------
    def _make_parts(self):
        parts = []
        for key, sw in SWITCHES.items():
            x, y = sw["at"]
            box = QRectF(x - 22, y - 30, 44, 42) if not sw["vertical"] else QRectF(x - 24, y - 22, 36, 44)
            tip = sw["name"] + (". Not seen by the software." if key in HIDDEN_KEYS else "")
            parts.append(Part(key, box, tip))
        parts += [
            Part("gv2", QRectF(COLUMN_X - 30, 300, 60, 60), "GV2: the source gate valve (the PLC needs it open)"),
            Part("plc", PLC_BOX, "The PLC: its enable to the relay, and what it waits for", holds_text=True),
            Part("pc", PC_BOX, "The PC: your ENABLE SAFETY RELAY", holds_text=True),
            Part("relay", RELAY_BOX, "The dual-channel safety relay", holds_text=True),
            Part("contactor", QRectF(CABINET.left() + 20, CABINET.top() + 10, 270, 110),
                 "The contactor: the relay switches the mains to every high-voltage supply"),
        ]
        return parts

    def parts(self):
        return self._parts

    # --- the telemetry, as the Interlocks panel works it out --------------------------------------
    def read(self, data: dict):
        ov = self.interlocks.overview
        self.group = ov.group("relay") if ov is not None else None

    def cond(self, label: str):
        g = self.group
        return next((c for c in g.conditions if c.label == label), None) if g is not None else None

    def _hidden(self):
        g = self.group
        return next((c for c in g.conditions if c.label == il.LOOP_HIDDEN), None) if g is not None else None

    def _emphasis(self, c) -> str:
        """As the Interlocks panel marks a condition holding its chain: red for a latched fault, amber
        when the relay was asked for (or is in trouble), plain otherwise."""
        if c.kind == il.FAULT:
            return "fault"
        level = self.group.level if self.group is not None else il.LEVEL_NORMAL
        return "warn" if level in (il.LEVEL_ATTENTION, il.LEVEL_FAULT) else "off"

    def tone_of(self, c):
        """A condition's tone on the drawing."""
        if c is None:
            return "unknown"
        if c.state == il.OK:
            return "ok"
        if c.state == il.HOLDING:
            return self._emphasis(c)
        if c.state == il.SUSPECT:
            return "warn"
        if c.state == il.HIDDEN:
            return None
        if c.state == il.ON:
            return "info" if c.kind == il.REQUEST else "on"
        if c.state in (il.OFF, il.INFO):
            return "off"
        return "unknown"

    @staticmethod
    def made(c):
        """Whether a switch is drawn made: True, False, or None (not known)."""
        if c is None or c.state in (il.UNKNOWN, il.HIDDEN, il.SUSPECT):
            return None
        return c.state in (il.OK, il.ON)

    def enable_given(self):
        """Whether the PLC gives its enable: every one of its conditions holds and you have asked. (With
        the relay closed it must be: the enable is in the relay's loop.)"""
        if self.relay_closed():
            return True
        conds = [self.cond(label) for label in PLC_CONDITIONS] + [self.cond("ENABLE SAFETY RELAY")]
        if any(c is not None and c.state in (il.HOLDING, il.OFF) for c in conds):
            return False
        if any(c is None or c.state == il.UNKNOWN for c in conds):
            return None
        return True

    def relay_closed(self):
        c = self.cond("Safety relay")
        return None if c is None or c.state == il.UNKNOWN else c.state == il.ON

    # --- drawing ------------------------------------------------------------------------------
    def paint(self, v):
        self._machine(v)
        self._frames(v)
        self._wires(v)
        self._switches(v)
        self._boxes(v)
        self._labels(v)
        v.legend(self.ORIGIN.x() + self.SIZE.width() - 8, 14, [[
            (lambda vv, c, s: sym.contact(vv, c, True, "ok", half=s * 0.5), "Made (holds)"),
            (lambda vv, c, s: sym.contact(vv, c, False, "warn", half=s * 0.5), "Open: holding the relay off"),
            (lambda vv, c, s: sym.contact(vv, c, None, "unknown", half=s * 0.5), "Not known now"),
            (lambda vv, c, s: sym.contact(vv, c, None, None, half=s * 0.5), "Not seen by the software"),
            (lambda vv, c, s: sym.wire(vv, [(c.x() - s * 0.6, c.y()), (c.x() + s * 0.6, c.y())], "live"),
             "Made, through to here"),
            (lambda vv, c, s: sym.wire(vv, [(c.x() - s * 0.6, c.y()), (c.x() + s * 0.6, c.y())], "hv"),
             "High voltage available")]], R | T)

    def _machine(self, v):
        """The machine, faintly, so the switches are where they are on it."""
        p = v.p
        p.save()
        p.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath(QPointF(COLUMN_X, ENCLOSURE.bottom()))
        path.lineTo(QPointF(COLUMN_X, MAG_C.y()))
        path.arcTo(QRectF(MAG_C.x() - MAG_R, MAG_C.y() - MAG_R, 2 * MAG_R, 2 * MAG_R), 180, 90)
        path.lineTo(QPointF(1300, MAG_C.y() + MAG_R))
        path.lineTo(QPointF(1470, MAG_C.y() + MAG_R - 45))
        wall = v.pen(v.c("border"), 1.0)
        wall.setCosmetic(False)
        wall.setWidthF(30)
        p.setPen(wall)
        p.drawPath(path)
        inner = v.pen(v.c("panel"), 1.0)
        inner.setCosmetic(False)
        inner.setWidthF(30 - 2.4 / v.scale)
        p.setPen(inner)
        p.drawPath(path)
        p.restore()

    def _frames(self, v):
        p = v.p
        p.save()
        fill = QColor(v.c("card_alt"))
        fill.setAlpha(150)
        p.setBrush(fill)
        p.setPen(v.pen(v.c("border_strong"), 1.6))
        p.drawRoundedRect(ENCLOSURE, 6, 6)
        p.restore()
        for frame in (QUIET, NOISY, DESK, CABINET):
            sym.rack(v, frame)

    def _wire_state(self, made):
        return "live" if made is True else "dead" if made is False else "unknown"

    def _wires(self, v):
        closed = self.relay_closed()
        loop = "live" if closed else "dead" if closed is False else "unknown"
        sw = {k: s["at"] for k, s in SWITCHES.items()}
        h = 18
        # The relay's hardwired loop: from one channel round to the other
        segments = [
            [(873, RELAY_BOX.top()), (873, 520), (517, 520), (517, 293), (197, 293), (197, sw["door_2"][1] + h)],
            [(197, sw["door_2"][1] - h), (197, sw["door_1"][1] + h)],
            [(197, sw["door_1"][1] - h), (197, 75), (sw["eso_hv"][0] - h, 75)],
            [(sw["eso_hv"][0] + h, 75), (582, 75), (582, LOOP_Y), (sw["fibre"][0] - h, LOOP_Y)],
            [(sw["fibre"][0] + h, LOOP_Y), (sw["vacuum"][0] - h, LOOP_Y)],
            [(sw["vacuum"][0] + h, LOOP_Y), (sw["key"][0] - h, LOOP_Y)],
            [(sw["key"][0] + h, LOOP_Y), (sw["start"][0] - h, LOOP_Y)],
            [(sw["start"][0] + h, LOOP_Y), (sw["eso_desk"][0] - h, LOOP_Y)],
            [(sw["eso_desk"][0] + h, LOOP_Y), (1505, LOOP_Y), (1505, 556), (990, 556), (990, RELAY_BOX.top())],
        ]
        for seg in segments:
            sym.wire(v, seg, loop, 2.6)
        # The PLC's inputs, along their bus
        inputs = {
            "gv2": [(COLUMN_X + 32, 330), (BUS_X, 330)],
            "src_temp": [(sw["src_temp"][0] + h, 460), (240, 460), (240, 640), (BUS_X, 640)],
            "src_flow": [(sw["src_flow"][0] + h, 460), (500, 460), (500, 603), (BUS_X, 603)],
            "air": [(sw["air"][0] + h, 770), (BUS_X, 770)],
            "mag_temp": [(sw["mag_temp"][0] + h, 1170), (450, 1170), (450, 1228), (BUS_X, 1228)],
            "mag_flow": [(sw["mag_flow"][0] + h, 1170), (BUS_X, 1170)],
        }
        for key, pts in inputs.items():
            sym.wire(v, pts, self._wire_state(self.made(self.cond(CONDITION_OF[key]))), 1.8)
        sym.wire(v, [(BUS_X, 1228), (BUS_X, 238), (PLC_BOX.left(), 238)], "dead", 2.0)
        # The PLC's enable, down the fibre to its contact in the loop; the relay's output to the contactor
        given = self.enable_given()
        sym.wire(v, [(870, PLC_BOX.bottom()), (870, LOOP_Y - 24)], self._wire_state(given), 2.0)
        sym.wire(v, [(PLC_BOX.right(), 238), (PC_BOX.left(), 238)], "dead", 1.6)
        hv = "hv" if closed else "dead" if closed is False else "unknown"
        sym.wire(v, [(790, 694), (912, 694)], "dead", 2.0)
        sym.wire(v, [(948, 694), (1080, 694), (1080, 872), (CABINET.left() + 196, 872)], hv, 2.2)
        sym.wire(v, [(CABINET.left() + 30, 935), (CABINET.left() + 140, 935)], "dead", 2.0)
        sym.wire(v, [(CABINET.left() + 184, 935), (CABINET.left() + 300, 935)], hv, 2.4)

    def _switches(self, v):
        closed = self.relay_closed()
        hidden = self._hidden()
        for key, sw in SWITCHES.items():
            c = QPointF(*sw["at"])
            if key in HIDDEN_KEYS:
                made = True if closed else None
                tone = "ok" if closed else self.tone_of(hidden) if hidden is not None else "unknown"
            elif key == "fibre":
                made = self.enable_given()
                tone = "ok" if made else "off" if made is False else "unknown"
            else:
                cnd = self.cond(CONDITION_OF[key])
                made, tone = self.made(cnd), self.tone_of(cnd)
            sym.contact(v, c, made, tone, vertical=sw["vertical"], actuator=sw["actuator"])
            v.ring(key, self._box[key])
        # The relay's output contact and the contactor's, made when the relay is closed
        out_tone = "warn" if closed else "off" if closed is False else "unknown"
        sym.contact(v, QPointF(930, 694), bool(closed) if closed is not None else None, out_tone)
        sym.contact(v, QPointF(CABINET.left() + 162, 935), bool(closed) if closed is not None else None, out_tone,
                    actuator="remote")
        gv = self.cond("Gate valve open")
        sym.valve(v, QPointF(COLUMN_X, 330), 52, self.tone_of(gv) if gv is not None else "unknown", vertical=True,
                  gate=True)
        v.ring("gv2", self._box["gv2"])

    def _boxes(self, v):
        closed = self.relay_closed()
        relay_tone = "warn" if closed else "off" if closed is False else "unknown"
        given = self.enable_given()
        held = next((c for c in (self.cond(label) for label in PLC_CONDITIONS) if c is not None
                     and c.state == il.HOLDING), None)
        plc_tone = "ok" if given else (self.tone_of(held) if held is not None else "off") if given is False else "unknown"
        request = self.cond("ENABLE SAFETY RELAY")
        for key, rect, tone in (("plc", PLC_BOX, plc_tone), ("pc", PC_BOX, self.tone_of(request)),
                                ("relay", RELAY_BOX, relay_tone)):
            p = v.p
            p.save()
            fill, line_c, _ink = v.tone(sym.soft(tone))
            p.setPen(v.pen(line_c, 1.8, sym.line_style(tone)))
            p.setBrush(fill)
            p.drawRoundedRect(rect, 8, 8)
            p.restore()
            v.ring(key, rect)
        sym.coil(v, QRectF(CABINET.left() + 196, 858, 60, 28), relay_tone)
        v.ring("contactor", self._box["contactor"])

    def _chip_for(self, c):
        if c is None:
            return "NO DATA", "unknown"
        return STATE_CHIP.get(c.state, ""), self.tone_of(c)

    def _labels(self, v):
        closed = self.relay_closed()
        # The high-voltage enclosure
        v.text(ENCLOSURE.center().x() + 8, ENCLOSURE.bottom() - 10, "High-voltage deck\nenclosure", "name-b", HC | B)
        v.text(160, 160, "Access\ndoors", "name", R | VC)
        v.text(350, 34, "ESO", "name", HC | VC)
        # The PLC's inputs
        v.text(COLUMN_X - 40, 330, "GV2", "name-b", R | VC)
        v.text(295, 410, "Source coolant loop", "name-b", HC | B)
        v.text(190, 484, "T < 35 °C", "note", HC | T)
        v.text(400, 484, "Flow > 2 L/min", "note", HC | T)
        v.text(470, 728, "Air compressor", "name-b", HC | B)
        v.text(470, 794, "> 2 bar", "note", HC | T)
        v.text(485, 1128, "Magnet coolant loop", "name-b", HC | B)
        v.text(400, 1194, "T < 35 °C", "note", HC | T)
        v.text(570, 1194, "Flow > 2 L/min", "note", HC | T)
        held = [(k, self.cond(CONDITION_OF[k])) for k in ("src_flow", "air", "mag_flow")]
        for key, cnd in held:
            if cnd is not None and cnd.state not in (il.OK,):
                x, y = SWITCHES[key]["at"]
                text, tone = self._chip_for(cnd)
                v.chip(x, y + 52, text, tone, HC | T)
        # The quiet rack: the PLC and the PC
        v.text(QUIET.center().x(), QUIET.top() - 8, "Quiet electronics rack", "name", HC | B)
        given = self.enable_given()
        held_by = next((c for c in (self.cond(label) for label in PLC_CONDITIONS) if c is not None
                        and c.state == il.HOLDING), None)
        if given:
            plc_chip = ("ENABLE GIVEN", "on")
        elif held_by is not None:
            plc_chip = ("HELD", self.tone_of(held_by))
        elif given is False:
            plc_chip = ("NO ENABLE", "off")
        else:
            plc_chip = ("NOT KNOWN", "unknown")
        v.block(PLC_BOX.center().x(), PLC_BOX.center().y(), [[("name-b", "PLC")], [("chip", *plc_chip)]], HC | VC)
        request = self.cond("ENABLE SAFETY RELAY")
        if request is None:
            pc_chip = ("NO DATA", "unknown")
        elif request.state == il.ON:
            pc_chip = ("PRESS START" if "START" in request.detail else "ASKED", "info")
        else:
            pc_chip = ("NOT ASKED" if request.state == il.OFF else STATE_CHIP.get(request.state, ""),
                       self.tone_of(request))
        v.block(PC_BOX.center().x(), PC_BOX.center().y(), [[("name-b", "PC")], [("note", "your enable")],
                                                           [("chip", *pc_chip)]], HC | VC)
        v.text(895, 372, "Fibre", "note", L | VC)
        # The noisy rack: the enable's contact, the vacuum relay, the safety relay
        v.text(NOISY.center().x(), NOISY.bottom() + 8, "Noisy electronics rack", "name", HC | T)
        v.text(870, LOOP_Y + 22, "Enable", "note", HC | T)
        vac = self.cond("Gauge controller relay (vacuum)")
        text, tone = self._chip_for(vac)
        v.block(1010, LOOP_Y + 24, [[("note", "Vacuum controller")], [
            ("note", "< 10⁻⁶ mbar"), ("chip", text if vac is None or vac.state != il.OK else "", tone)]], HC | T)
        state = self.health.lbl_safety_state
        v.block(RELAY_BOX.center().x(), RELAY_BOX.center().y(), [
            [("name-b", "Dual-channel")], [("name-b", "safety relay")],
            [("chip", state.text().replace("SAFETY: ", ""), "warn" if closed else
              "fault" if state.property("tone") == "fault" else "off" if closed is False else "unknown")]], HC | VC)
        v.text(782, 694, "24 V", "note", R | VC)
        # The operator desk
        v.text(DESK.center().x(), DESK.top() - 8, "Operator desk", "name", HC | B)
        for key, text in (("key", "Key"), ("start", "START"), ("eso_desk", "ESO")):
            x, y = SWITCHES[key]["at"]
            v.text(x, y + 22, text, "note", HC | T)
        # The power cabinet
        v.text(CABINET.center().x(), CABINET.bottom() + 8, "Power cabinet B", "name", HC | T)
        v.text(CABINET.left() + 22, 935, "L1", "note", R | VC)
        v.block(CABINET.left() + 310, 935, [[("name-b", "Every high-voltage supply")],
                                            [("note", "switched mains")]], L | VC)

    # --- faceplates ---------------------------------------------------------------------------
    def _conditions_plate(self, fp: Faceplate, labels):
        """A row for each of these conditions: its state, and what it rests on, kept up to date."""
        rows = []
        for label in labels:
            chip = fp.chip("", "unknown")
            detail = QLabel()
            detail.setWordWrap(True)
            set_role(detail, "hint")
            fp.row(label.replace(" (shutdown sequence)", ""), chip)
            fp.grid.addWidget(detail, fp.grid.rowCount(), 0, 1, 2)
            rows.append((label, chip, detail))

        def show():
            for label, chip, detail in rows:
                c = self.cond(label) if label != il.LOOP_HIDDEN else self._hidden()
                text, tone = self._chip_for(c)
                chip.setText(text or "-")
                set_tone(chip, tone if tone is not None else "unknown")
                detail.setText(c.detail if c is not None else "No data from the PLC.")
        fp.every_refresh(show)

    def faceplate_for(self, key: str):
        h = self.health
        if key == "relay":
            fp = Faceplate(self, key, "Safety relay", "Dual-channel: closes on START once its loop is made")
            fp.wide(MirrorLabel(h.lbl_safety_state))
            fp.wide(MirrorLabel(h.lbl_shutdown_state))
            fp.wide(MirrorButton(h.btn_enable_safety))
            fp.wide(MirrorButton(h.btn_reset_faults))
            fp.wide(MirrorButton(h.btn_shutdown), MirrorButton(h.btn_cancel_shutdown))
            fp.note("After ENABLE SAFETY RELAY, press START on the operator desk before the countdown ends.")
            fp.link("System Health panel →", lambda: self.show_panel("health"))
            fp.link("Interlocks panel →", lambda: self.show_panel("interlocks"))
            return fp
        if key == "plc":
            fp = Faceplate(self, key, "The PLC's enable", "Given over the fibre when all of these hold and you ask")
            self._conditions_plate(fp, PLC_CONDITIONS + ("ENABLE SAFETY RELAY",))
            fp.wide(MirrorLabel(h.lbl_comms))
            fp.link("Interlocks panel →", lambda: self.show_panel("interlocks"))
            return fp
        if key == "pc":
            fp = Faceplate(self, key, "ENABLE SAFETY RELAY", "Your request to the PLC")
            self._conditions_plate(fp, ("ENABLE SAFETY RELAY",))
            fp.wide(MirrorButton(h.btn_enable_safety))
            fp.note("Then press START on the operator desk before the countdown ends.")
            fp.link("System Health panel →", lambda: self.show_panel("health"))
            return fp
        if key == "gv2":
            return gv2_plate(self, key, self.vac)
        if key in ("src_temp", "src_flow", "air", "mag_temp", "mag_flow", "vacuum"):
            label = CONDITION_OF[key]
            fp = Faceplate(self, key, SWITCHES[key]["name"],
                           "In the relay's loop, and read by the PC" if key == "vacuum" else "Read by the PLC")
            self._conditions_plate(fp, (label,))
            if key in ("src_temp", "src_flow", "mag_temp", "mag_flow"):
                fp.note("The temperature and flow switches are wired in series to one PLC input, so the software "
                        "can't tell which of them is open.")
            if key == "vacuum":
                fp.link("Vacuum System panel →", lambda: self.show_panel("vacuum"))
            fp.link("Interlocks panel →", lambda: self.show_panel("interlocks"))
            return fp
        if key == "fibre":
            fp = Faceplate(self, key, "The PLC's enable", "Its contact in the relay's loop, worked by the fibre")
            self._conditions_plate(fp, PLC_CONDITIONS + ("ENABLE SAFETY RELAY",))
            return fp
        if key in HIDDEN_KEYS:
            fp = Faceplate(self, key, SWITCHES[key]["name"], "Not seen by the software")
            fp.note(HIDDEN_NOTE)
            self._conditions_plate(fp, (il.LOOP_HIDDEN,))
            return fp
        if key == "contactor":
            fp = Faceplate(self, key, "Contactor, power cabinet B", "Worked by the safety relay")
            fp.note("It switches the mains (L1) to every high-voltage supply: they have no power while the relay is "
                    "open.")
            fp.wide(MirrorLabel(h.lbl_safety_state))
            return fp
        return None
