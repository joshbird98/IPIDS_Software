"""The faceplates more than one diagram opens, and what a diagram reads off a panel's supply.

A supply (a row of the source or optics panel: its psu_controls entry) is drawn in the tone its panel
shows: the mode badge's when one shows (violet held by a sequence, blue automatic, amber arc quench,
red locked), otherwise its output button's (green on, grey off, dashed not known).
"""
from src.gui.control.diagrams.faceplate import Faceplate
from src.gui.control.diagrams.mirror import MirrorButton, MirrorLabel, MirrorSetpoint

NOT_READ = ("Not read by the software: it can't see this {what}, so the diagram doesn't show its state. "
            "Check it at the machine.")
_BADGE_TONES = ("seq", "info", "warn", "fault", "on")


def plain_tone(widget, unknown: str = "unknown") -> str:
    """A panel widget's tone as the diagram draws it: no tone (the panel's normal look) is grey."""
    tone = widget.property("tone")
    return "off" if tone in (None, "") else tone


def supply_tone(ctrl: dict) -> str:
    badge = ctrl.get("lbl_badge")
    if badge is not None and not badge.isHidden() and badge.property("tone") in _BADGE_TONES:
        return badge.property("tone")
    return plain_tone(ctrl["btn_enable"])


def supply_chip(ctrl: dict) -> tuple:
    """(text, tone) for a supply's chip on a diagram: its badge, or ON / OFF / NO DATA."""
    badge = ctrl.get("lbl_badge")
    if badge is not None and not badge.isHidden() and badge.text():
        return badge.text(), badge.property("tone") or "info"
    tone = plain_tone(ctrl["btn_enable"])
    if tone == "on":
        return "ON", "on"
    if tone == "off":
        return "OFF", "off"
    if tone == "armed":
        return "CONFIRM?", "armed"
    if tone == "unknown":
        return "NO DATA", "unknown"
    return ctrl["btn_enable"].text(), tone


def readback(ctrl: dict) -> str:
    """A supply's main readback as its panel shows it, without 'RB: '."""
    text = ctrl["lbl_rb"].text()
    return text[4:] if text.startswith("RB: ") else text


def supply_rows(fp: Faceplate, ctrl: dict, caption: str = ""):
    """A supply's readback, setpoint, mode and output button, mirrored from its panel."""
    if caption:
        fp.heading(caption)
    lines = [MirrorLabel(ctrl["lbl_rb"], role="readout")]
    if "lbl_rb2" in ctrl:
        lines.append(MirrorLabel(ctrl["lbl_rb2"]))
    fp.row("Readback", *lines)
    fp.row("Setpoint", MirrorSetpoint(ctrl["sp_box"]))
    if "lbl_badge" in ctrl:
        fp.wide(MirrorLabel(ctrl["lbl_badge"]))
    buttons = [MirrorButton(ctrl["btn_enable"])]
    for extra in ("btn_auto", "btn_cool"):
        if extra in ctrl:
            buttons.append(MirrorButton(ctrl[extra]))
    fp.wide(*buttons)


def supply_plate(panel, key: str, ctrl: dict, title: str, subtitle: str, link_to: str, link_text: str) -> Faceplate:
    fp = Faceplate(panel, key, title, subtitle)
    supply_rows(fp, ctrl)
    fp.note("Switching it on takes two clicks, as on its panel.")
    fp.link(link_text, lambda: panel.show_panel(link_to))
    return fp


def gv2_plate(panel, key: str, vac) -> Faceplate:
    fp = Faceplate(panel, key, "GV2: source gate valve", "Between the source and the beamline")
    fp.wide(MirrorLabel(vac.lbl_gv_status))
    fp.wide(MirrorLabel(vac.lbl_gv_fault))
    fp.wide(MirrorLabel(vac.lbl_gv_hold))
    fp.wide(MirrorButton(vac.btn_gv_open), MirrorButton(vac.btn_gv_close))
    fp.note("The PLC opens it only with VG1 and VG2 read, healthy and below their set points, and closes it by "
            "hand only with the source cold. Hover over a button for why it can't be pressed.")
    fp.link("Vacuum System panel →", lambda: panel.show_panel("vacuum"))
    return fp


def not_read_plate(panel, key: str, title: str, subtitle: str, what: str, more: str = "") -> Faceplate:
    fp = Faceplate(panel, key, title, subtitle)
    fp.note(NOT_READ.format(what=what) + (f" {more}" if more else ""))
    return fp
