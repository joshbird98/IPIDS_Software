"""The look of both SINDIE programs (operator HMI and data viewer): one set of colours per theme,
dark (the default) and light, switched while the programs run.

Principle (from high-performance HMI practice, ISA-101): grey is normal, colour is news. Normal
states are drawn in calm neutral tones; amber and red are kept for what needs attention, so an
alarm stands out instead of competing with a screen full of green. Green marks an output that is
switched ON (an energised supply, a running pump), blue marks automatic modes and interaction
(focus, selection, the main action), violet marks a sequence holding a control.

    install(app)          at start-up: Fusion style, fonts, the saved theme, and following theme
                          changes made in the other program (appearance.json, checked every second)
    apply_theme(app, m)   switch now ('dark' / 'light'); every window restyles in place
    tokens() / color(n)   the current colours, for code that paints (dials, plots, table rows)
    style_plot(p)         pyqtgraph plots: colours now, and again after every switch
    theme_changed         Qt signal (mode) for anything else that needs to redraw

Widgets don't carry colours of their own: they set a "tone" or "role" property (src/gui/style.py)
and the application stylesheet built here draws it.
"""
import json
import logging
import os
import tempfile
import weakref
import zlib

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PyQt6.QtWidgets import QApplication

log = logging.getLogger("theme")

MODES = ("dark", "light")
DEFAULT_MODE = "dark"

# --- Colours -----------------------------------------------------------------------------------
# window: behind the panels (shows in the gaps); panel: a panel's own background; card: group
# boxes and cards on a panel; input: entry fields and lists; raised: buttons.
PALETTES = {
    "dark": {
        "window": "#13171B", "panel": "#1B2026", "card": "#21272E", "card_alt": "#272E36",
        "input": "#161A1F", "raised": "#2A313A", "raised_hover": "#333B45", "raised_press": "#222830",
        "border": "#353D47", "border_soft": "#2B323A", "border_strong": "#4A5462",
        "text": "#E2E6EA", "text_muted": "#9AA3AD", "text_faint": "#6A737D",
        "accent": "#4C9AEB", "accent_hover": "#66AAF0", "accent_press": "#3A86D6",
        "accent_soft": "#1F3A57", "on_accent": "#FFFFFF",
        "ok": "#62BA86", "ok_bg": "#1A3024", "ok_fill": "#2E8A57", "ok_fill_text": "#FFFFFF",
        "warn": "#ECAA47", "warn_bg": "#3A2C14", "warn_fill": "#E9A23B", "warn_fill_text": "#1C1305",
        "fault": "#F27474", "fault_bg": "#3E1F22", "fault_fill": "#D63D41", "fault_fill_text": "#FFFFFF",
        "info": "#66AFF0", "info_bg": "#1B3047", "info_fill": "#2F72B8", "info_fill_text": "#FFFFFF",
        "seq": "#B79AF5", "seq_bg": "#2C2443", "seq_fill": "#7552C9", "seq_fill_text": "#FFFFFF",
        "off_bg": "#252B33", "off_text": "#A3ACB6",
        "plot_bg": "#161A1F", "plot_text": "#9AA3AD", "plot_axis": "#56606C", "plot_legend_bg": "#1E242BE6",
        "plot_crosshair": "#C9D1D9", "plot_pin": "#F28C56",
        # line colours, fixed order (validated: distinct for colour-blind readers, >= 3:1 on plot_bg)
        "series": ["#3987E5", "#D95926", "#199E70", "#C98500", "#D55181", "#008300", "#9085E9", "#E66767"],
        # event markers in the viewer
        "ev_fault": "#E5484D", "ev_cleared": "#4C9AEB", "ev_watchdog": "#EC835A", "ev_phase": "#2FA35B",
        "ev_tolerance": "#E9A23B", "ev_command": "#8C96A0", "ev_marker": "#E2E6EA",
        # alarm priorities (ISA-18.2 order: critical, high, medium, low): fill + text, and the
        # softer pair an unacknowledged alarm flashes to (and an acknowledged one stays at)
        "alarm_1": "#D63D41", "alarm_1_text": "#FFFFFF", "alarm_1_soft": "#5A2427", "alarm_1_soft_text": "#FFD6D6",
        "alarm_2": "#DC6B1E", "alarm_2_text": "#FFFFFF", "alarm_2_soft": "#4E3218", "alarm_2_soft_text": "#FFD9B8",
        "alarm_3": "#E5B33B", "alarm_3_text": "#1C1305", "alarm_3_soft": "#4A3D17", "alarm_3_soft_text": "#FFE7A8",
        "alarm_4": "#2F72B8", "alarm_4_text": "#FFFFFF", "alarm_4_soft": "#1F3A57", "alarm_4_soft_text": "#CFE3FA",
        "alarm_quiet": "#2A313A", "alarm_quiet_text": "#9AA3AD",
    },
    "light": {
        "window": "#D5DBE1", "panel": "#EEF1F4", "card": "#FFFFFF", "card_alt": "#F4F6F8",
        "input": "#FFFFFF", "raised": "#F8F9FB", "raised_hover": "#EBEFF3", "raised_press": "#DEE4EA",
        "border": "#C2CAD3", "border_soft": "#DCE2E8", "border_strong": "#9DA8B4",
        "text": "#1C232B", "text_muted": "#55606C", "text_faint": "#89939D",
        "accent": "#1F6FD1", "accent_hover": "#1A62BA", "accent_press": "#154F96",
        "accent_soft": "#D6E6FA", "on_accent": "#FFFFFF",
        "ok": "#23784A", "ok_bg": "#E1F2E8", "ok_fill": "#2E8B57", "ok_fill_text": "#FFFFFF",
        "warn": "#8F5300", "warn_bg": "#FDF0D9", "warn_fill": "#F0A73A", "warn_fill_text": "#1F1300",
        "fault": "#C0292E", "fault_bg": "#FBE3E4", "fault_fill": "#D33A3F", "fault_fill_text": "#FFFFFF",
        "info": "#1D63BA", "info_bg": "#E1ECFA", "info_fill": "#2F72B8", "info_fill_text": "#FFFFFF",
        "seq": "#6A41C4", "seq_bg": "#ECE5FA", "seq_fill": "#7552C9", "seq_fill_text": "#FFFFFF",
        "off_bg": "#E4E8EC", "off_text": "#55606C",
        "plot_bg": "#FFFFFF", "plot_text": "#55606C", "plot_axis": "#A3ADB8", "plot_legend_bg": "#FFFFFFE6",
        "plot_crosshair": "#3B4651", "plot_pin": "#C2410C",
        "series": ["#2A78D6", "#EB6834", "#1BAF7A", "#EDA100", "#E87BA4", "#008300", "#6250D6", "#E34948"],
        "ev_fault": "#D03B3B", "ev_cleared": "#1F6FD1", "ev_watchdog": "#D9692F", "ev_phase": "#13883E",
        "ev_tolerance": "#C98500", "ev_command": "#7C8792", "ev_marker": "#1C232B",
        "alarm_1": "#C62828", "alarm_1_text": "#FFFFFF", "alarm_1_soft": "#F8D3D3", "alarm_1_soft_text": "#7F1D1D",
        "alarm_2": "#E65100", "alarm_2_text": "#FFFFFF", "alarm_2_soft": "#FFE0C2", "alarm_2_soft_text": "#7A2E00",
        "alarm_3": "#F2B630", "alarm_3_text": "#1F1300", "alarm_3_soft": "#FFF1C2", "alarm_3_soft_text": "#5C4300",
        "alarm_4": "#1565C0", "alarm_4_text": "#FFFFFF", "alarm_4_soft": "#D6E6FA", "alarm_4_soft_text": "#0D3C73",
        "alarm_quiet": "#E4E8EC", "alarm_quiet_text": "#55606C",
    },
}


class _Notifier(QObject):
    changed = pyqtSignal(str)


_state = {"mode": DEFAULT_MODE, "notifier": None, "sync": None, "fonts": None}
_plots = weakref.WeakKeyDictionary()          # plot object -> extra restyle callback (or None)


def theme_changed():
    """The signal emitted (with the mode) after every switch."""
    if _state["notifier"] is None:
        _state["notifier"] = _Notifier()
    return _state["notifier"].changed


def current_mode() -> str:
    return _state["mode"]


def tokens(mode: str = None) -> dict:
    return PALETTES[mode or _state["mode"]]


def color(name: str, mode: str = None) -> QColor:
    """A theme colour as a QColor (8-digit hex = with alpha, as #RRGGBBAA)."""
    return qcolor(tokens(mode)[name])


def qcolor(hex_code: str) -> QColor:
    h = hex_code.lstrip("#")
    if len(h) == 8:
        return QColor(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), int(h[6:8], 16))
    return QColor("#" + h)


def rgba(hex_code: str, alpha: float) -> str:
    """'rgba(r, g, b, a)' for stylesheets: a colour at some opacity (0..1)."""
    c = qcolor(hex_code)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {int(round(alpha * 255))})"


def series_color(slot: int, mode: str = None) -> QColor:
    """Line colour for the n-th channel of a plot (0-based); after 8 the colours repeat (callers
    then draw those lines dashed, so no two lines on one plot look the same)."""
    s = tokens(mode)["series"]
    return qcolor(s[slot % len(s)])


def readable(color_value, background: str = None, minimum: float = 3.0) -> QColor:
    """A colour adjusted (darkened or lightened) until it stands out against the plot
    background by at least `minimum`:1 contrast - a user's yellow or white marker stays visible
    on the light theme's white plots."""
    c = QColor(color_value) if not isinstance(color_value, QColor) else QColor(color_value)
    bg = qcolor(background or tokens()["plot_bg"])
    if not c.isValid():
        return qcolor(tokens()["text"])
    dark_bg = _luminance(bg) < 0.4
    for _ in range(24):
        if _contrast(c, bg) >= minimum:
            break
        c = c.lighter(112) if dark_bg else c.darker(115)
    return c


def _luminance(c: QColor) -> float:
    def ch(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c.red()) + 0.7152 * ch(c.green()) + 0.0722 * ch(c.blue())


def _contrast(a: QColor, b: QColor) -> float:
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# --- Fonts -------------------------------------------------------------------------------------
UI_FONTS = ["Segoe UI", "Inter", "Noto Sans", "Ubuntu", "DejaVu Sans"]
MONO_FONTS = ["Cascadia Mono", "Consolas", "JetBrains Mono", "DejaVu Sans Mono", "Liberation Mono", "Noto Sans Mono"]


def fonts() -> dict:
    """{'ui': family, 'mono': family}: the first of each list that is installed."""
    if _state["fonts"] is None:
        have = set(QFontDatabase.families())
        ui = next((f for f in UI_FONTS if f in have), QApplication.font().family())
        mono = next((f for f in MONO_FONTS if f in have),
                    QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family())
        _state["fonts"] = {"ui": ui, "mono": mono}
    return _state["fonts"]


def mono_font(point_size: float = None) -> QFont:
    f = QFont(fonts()["mono"])
    f.setStyleHint(QFont.StyleHint.Monospace)
    if point_size:
        f.setPointSizeF(point_size)
    return f


# --- Icons drawn in the stylesheet (dock buttons) ---------------------------------------------
# (16 x 16 view box; the dock title buttons draw them at 10 px, so the glyphs fill most of it)
_ICONS = {
    "close": '<path d="M2.5 2.5 L13.5 13.5 M13.5 2.5 L2.5 13.5" stroke="{c}" stroke-width="2.2" '
             'stroke-linecap="round"/>',
    "float": '<rect x="2" y="2.5" width="12" height="11" rx="1.5" fill="none" stroke="{c}" stroke-width="1.8"/>'
             '<rect x="2" y="2.5" width="12" height="3.5" rx="1" fill="{c}"/>',
    "check": '<path d="M3.5 8.5 L6.5 11.5 L12.5 4.5" fill="none" stroke="{c}" stroke-width="2.2" '
             'stroke-linecap="round" stroke-linejoin="round"/>',
    "up": '<path d="M3.5 10.5 L8 6 L12.5 10.5" fill="none" stroke="{c}" stroke-width="2" '
          'stroke-linecap="round" stroke-linejoin="round"/>',
    "down": '<path d="M3.5 6 L8 10.5 L12.5 6" fill="none" stroke="{c}" stroke-width="2" '
            'stroke-linecap="round" stroke-linejoin="round"/>',
}


def _icon_path(name: str, colour: str) -> str:
    """The icon as a small SVG file (stylesheets take file paths), written once per design and colour."""
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
           + _ICONS[name].format(c=colour) + "</svg>")
    folder = os.path.join(tempfile.gettempdir(), "sindie_theme_icons")
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        log.debug("Theme icon folder %s not available", folder, exc_info=True)
    path = os.path.join(folder, f"{name}_{zlib.crc32(svg.encode()):08x}.svg")
    if not os.path.exists(path):
        tmp = f"{path}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(svg)
            os.replace(tmp, path)      # (both programs may write it at once: same name, same contents)
        except OSError:                # a missing icon is only cosmetic: never stop a start or a switch
            log.debug("Theme icon %s not written", path, exc_info=True)
            try:
                os.remove(tmp)
            except OSError:
                pass
    return path.replace("\\", "/")


# --- Qt palette and stylesheet ----------------------------------------------------------------
def build_palette(mode: str = None) -> QPalette:
    t = tokens(mode)
    p = QPalette()
    role = QPalette.ColorRole
    group = QPalette.ColorGroup
    for g in (group.Active, group.Inactive):
        p.setColor(g, role.Window, qcolor(t["panel"]))
        p.setColor(g, role.WindowText, qcolor(t["text"]))
        p.setColor(g, role.Base, qcolor(t["input"]))
        p.setColor(g, role.AlternateBase, qcolor(t["card_alt"]))
        p.setColor(g, role.Text, qcolor(t["text"]))
        p.setColor(g, role.Button, qcolor(t["raised"]))
        p.setColor(g, role.ButtonText, qcolor(t["text"]))
        p.setColor(g, role.Highlight, qcolor(t["accent"]))
        p.setColor(g, role.HighlightedText, qcolor(t["on_accent"]))
        p.setColor(g, role.ToolTipBase, qcolor(t["card_alt"]))
        p.setColor(g, role.ToolTipText, qcolor(t["text"]))
        p.setColor(g, role.PlaceholderText, qcolor(t["text_faint"]))
        p.setColor(g, role.Link, qcolor(t["accent"]))
        p.setColor(g, role.BrightText, qcolor(t["fault"]))
        p.setColor(g, role.Light, qcolor(t["border_strong"]))
        p.setColor(g, role.Midlight, qcolor(t["border"]))
        p.setColor(g, role.Mid, qcolor(t["border"]))
        p.setColor(g, role.Dark, qcolor(t["window"]))
        p.setColor(g, role.Shadow, qcolor(t["window"]))
    d = group.Disabled
    for r, key in ((role.Window, "panel"), (role.Base, "panel"), (role.Button, "panel"),
                   (role.WindowText, "text_faint"), (role.Text, "text_faint"), (role.ButtonText, "text_faint"),
                   (role.Highlight, "border"), (role.HighlightedText, "text_muted"),
                   (role.PlaceholderText, "text_faint"), (role.Light, "border"), (role.Mid, "border_soft"),
                   (role.Dark, "window")):
        p.setColor(d, r, qcolor(t[key]))
    return p


def build_stylesheet(mode: str = None) -> str:
    t = tokens(mode)
    f = fonts()
    m = mode or _state["mode"]
    close_icon = _icon_path("close", t["text_muted"])
    float_icon = _icon_path("float", t["text_muted"])
    up_icon, down_icon = _icon_path("up", t["text_muted"]), _icon_path("down", t["text_muted"])
    up_off, down_off = _icon_path("up", t["text_faint"]), _icon_path("down", t["text_faint"])
    check_on = _icon_path("check", t["on_accent"])
    check_menu = _icon_path("check", t["accent"])
    tone_label = {  # QLabel[tone=...]: status chips
        "ok": (t["ok_bg"], t["ok"], rgba(t["ok"], 0.45)),
        "info": (t["info_bg"], t["info"], rgba(t["info"], 0.45)),
        "seq": (t["seq_bg"], t["seq"], rgba(t["seq"], 0.45)),
        "off": (t["off_bg"], t["off_text"], t["border_soft"]),
        "warn": (t["warn_fill"], t["warn_fill_text"], t["warn_fill"]),
        "fault": (t["fault_fill"], t["fault_fill_text"], t["fault_fill"]),
        "on": (t["ok_fill"], t["ok_fill_text"], t["ok_fill"]),
    }
    tone_label["unknown"] = ("transparent", t["text_faint"], t["border_strong"])
    chips = "\n".join(
        f'QLabel[tone="{k}"] {{ background-color: {bg}; color: {fg}; '
        f'border: 1px {"dashed" if k == "unknown" else "solid"} {bd}; border-radius: 4px; '
        f'padding: 3px 8px; font-weight: 600; }}'
        for k, (bg, fg, bd) in tone_label.items())
    tone_button = {  # QPushButton[tone=...]: (background, text, border, hover background)
        "on": (t["ok_fill"], t["ok_fill_text"], t["ok_fill"], QColor(t["ok_fill"]).lighter(112).name()),
        "warn": (t["warn_fill"], t["warn_fill_text"], t["warn_fill"], QColor(t["warn_fill"]).lighter(108).name()),
        "fault": (t["fault_fill"], t["fault_fill_text"], t["fault_fill"], QColor(t["fault_fill"]).lighter(112).name()),
        "info": (t["info_fill"], t["info_fill_text"], t["info_fill"], QColor(t["info_fill"]).lighter(112).name()),
        "seq": (t["seq_fill"], t["seq_fill_text"], t["seq_fill"], QColor(t["seq_fill"]).lighter(112).name()),
        "primary": (t["accent"], t["on_accent"], t["accent"], t["accent_hover"]),
        "ok": (t["ok_bg"], t["ok"], rgba(t["ok"], 0.55), t["ok_bg"]),
    }
    buttons = "\n".join(
        f'QPushButton[tone="{k}"] {{ background-color: {bg}; color: {fg}; border: 1px solid {bd}; '
        f'font-weight: 600; }}\n'
        f'QPushButton[tone="{k}"]:hover {{ background-color: {hv}; }}'
        for k, (bg, fg, bd, hv) in tone_button.items())
    # Disabled: a state the button shows (output ON, a sequence holding it) stays visible, muted;
    # an action that can't be taken now goes plain grey (colour is kept for what needs attention)
    disabled_tones = "\n".join(
        f'QPushButton[tone="{k}"]:disabled {{ background-color: {rgba(bg, 0.28)}; color: {fg}; '
        f'border: 1px solid {rgba(bg, 0.55)}; }}'
        for k, (bg, fg) in {"on": (t["ok_fill"], t["ok"]), "seq": (t["seq_fill"], t["seq"])}.items())
    disabled_tones += "\n" + "\n".join(
        f'QPushButton[tone="{k}"]:disabled {{ background-color: {t["panel"]}; color: {t["text_faint"]}; '
        f'border: 1px solid {t["border_soft"]}; }}'
        for k in ("fault", "warn", "info", "primary", "ok", "armed"))
    return f"""
/* SINDIE {m} theme - generated by src/gui/theme.py */
QMainWindow {{ background-color: {t['window']}; }}
QMainWindow::separator {{ background-color: {t['window']}; width: 5px; height: 5px; }}
QMainWindow::separator:hover {{ background-color: {t['accent']}; }}
QToolTip {{ background-color: {t['card_alt']}; color: {t['text']}; border: 1px solid {t['border_strong']};
    padding: 4px 6px; }}

/* Panels: title bar, contents (each panel scrolls when it is made smaller than its contents) */
QDockWidget {{ color: {t['text']}; titlebar-close-icon: url({close_icon}); titlebar-normal-icon: url({float_icon}); }}
QDockWidget::title {{ background-color: {t['card_alt']}; border-bottom: 1px solid {t['border']};
    padding: 5px 8px; text-align: left; }}
QDockWidget::close-button, QDockWidget::float-button {{ border: none; background: transparent; padding: 1px;
    icon-size: 12px; }}
QDockWidget::close-button:hover, QDockWidget::float-button:hover {{ background-color: {t['raised_hover']};
    border-radius: 3px; }}
QScrollArea#PanelScroll {{ background-color: {t['panel']}; border: none; }}
QScrollArea#PanelScroll > QWidget {{ background-color: {t['panel']}; }}
QWidget#PanelBody {{ background-color: {t['panel']}; }}

/* Tabs (panels stacked as tabs, and tab widgets) */
QTabBar {{ qproperty-drawBase: 0; }}
QTabBar::tab {{ background: {t['window']}; color: {t['text_muted']}; padding: 5px 12px; margin-right: 1px;
    border: 1px solid {t['border_soft']}; border-bottom: none; border-top-left-radius: 4px;
    border-top-right-radius: 4px; }}
QTabBar::tab:selected {{ background: {t['card_alt']}; color: {t['text']}; border-color: {t['border']};
    border-top: 2px solid {t['accent']}; }}
QTabBar::tab:hover:!selected {{ color: {t['text']}; background: {t['panel']}; }}
QTabWidget::pane {{ border: 1px solid {t['border']}; background: {t['panel']}; top: -1px; }}

/* Group boxes and cards */
QGroupBox {{ background-color: {t['card']}; border: 1px solid {t['border_soft']}; border-radius: 6px;
    margin-top: 16px; padding: 10px 8px 8px 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: 8px; padding: 0 3px;
    color: {t['text_muted']}; font-weight: 600; }}
QFrame[role="card"] {{ background-color: {t['card_alt']}; border: 1px solid {t['border_soft']};
    border-radius: 5px; }}
QFrame[role="callout"] {{ background-color: {t['card_alt']}; border: 1px solid {t['border']}; border-radius: 6px; }}
QFrame[role="callout"][tone="warn"] {{ background-color: {t['warn_bg']}; border: 2px solid {t['warn_fill']}; }}
QFrame[role="callout"][tone="fault"] {{ background-color: {t['fault_bg']}; border: 2px solid {t['fault_fill']}; }}
QFrame[role="callout"][tone="info"] {{ background-color: {t['info_bg']}; border: 1px solid {rgba(t['info'], 0.5)}; }}
QLabel[role="note"] {{ background-color: {t['card_alt']}; color: {t['text']}; border: 1px solid {t['border']};
    border-radius: 5px; padding: 6px 8px; }}
QLabel[role="note"][level="warn"] {{ background-color: {t['warn_bg']}; border-color: {t['warn_fill']}; }}
QLabel[role="note"][level="fault"] {{ background-color: {t['fault_bg']}; border-color: {t['fault_fill']}; }}
QLabel[role="note"][level="info"] {{ background-color: {t['info_bg']}; border-color: {rgba(t['info'], 0.5)}; }}
QFrame[role="rule"] {{ background-color: {t['border_soft']}; max-height: 1px; min-height: 1px; border: none; }}

/* Text roles */
QLabel {{ background: transparent; }}
QLabel[role="readout"] {{ font-family: "{f['mono']}"; font-size: 12pt; color: {t['text']}; }}
QLabel[role="readout-sm"] {{ font-family: "{f['mono']}"; color: {t['text']}; }}
QLabel[role="readout-sub"] {{ font-family: "{f['mono']}"; color: {t['text_muted']}; }}
QLabel[role="readout-lg"] {{ font-family: "{f['mono']}"; font-size: 18pt; font-weight: 600; color: {t['text']}; }}
QLabel[role="readout-hero"] {{ font-family: "{f['mono']}"; font-size: 28pt; font-weight: 600; color: {t['text']}; }}
QLabel[role="caption"] {{ color: {t['text_muted']}; }}
QLabel[role="hint"] {{ color: {t['text_faint']}; }}
QLabel[role="title"] {{ font-weight: 600; color: {t['text']}; }}
QLabel[role="heading"] {{ font-size: 11pt; font-weight: 600; color: {t['text']}; }}
QLabel[role="error"] {{ color: {t['fault']}; }}
QLabel[role="warning"] {{ color: {t['warn']}; }}
QLabel[role="success"] {{ color: {t['ok']}; }}
QLabel[ink="ok"] {{ color: {t['ok']}; }}
QLabel[ink="warn"] {{ color: {t['warn']}; }}
QLabel[ink="fault"] {{ color: {t['fault']}; }}
QLabel[ink="info"] {{ color: {t['info']}; }}
QLabel[ink="muted"] {{ color: {t['text_muted']}; }}
QLabel[ink="faint"] {{ color: {t['text_faint']}; }}
QLabel[role="readout"][tolerance="out"], QLabel[role="readout-sm"][tolerance="out"], QLabel[tolerance="out"] {{
    border: 2px solid {t['warn_fill']}; border-radius: 3px; padding: 1px 3px; }}

/* Status chips */
{chips}

/* Banners: one line that sums up a panel's state (the alarm summary) */
QLabel[role="banner"] {{ font-size: 12pt; font-weight: 700; padding: 6px 10px; border-radius: 4px;
    color: {t['text_muted']}; background-color: {t['card_alt']}; }}
QLabel[role="banner"][level="1"] {{ background-color: {t['alarm_1']}; color: {t['alarm_1_text']}; }}
QLabel[role="banner"][level="2"] {{ background-color: {t['alarm_2']}; color: {t['alarm_2_text']}; }}
QLabel[role="banner"][level="3"] {{ background-color: {t['alarm_3']}; color: {t['alarm_3_text']}; }}
QLabel[role="banner"][level="4"] {{ background-color: {t['alarm_4']}; color: {t['alarm_4_text']}; }}
QLabel[role="banner"][level="active"] {{ background-color: {t['warn_bg']}; color: {t['warn']}; }}
QLabel[role="banner"][level="clear"] {{ background-color: {t['ok_bg']}; color: {t['ok']}; }}

/* Buttons */
QPushButton {{ background-color: {t['raised']}; color: {t['text']}; border: 1px solid {t['border']};
    border-radius: 4px; padding: 5px 10px; min-height: 18px; }}
QPushButton:hover {{ background-color: {t['raised_hover']}; border-color: {t['border_strong']}; }}
QPushButton:pressed {{ background-color: {t['raised_press']}; }}
QPushButton:focus {{ border-color: {t['accent']}; }}
QPushButton:disabled {{ background-color: {t['panel']}; color: {t['text_faint']}; border-color: {t['border_soft']}; }}
QPushButton:checked {{ background-color: {t['accent_soft']}; border-color: {t['accent']}; }}
{buttons}
{disabled_tones}
QPushButton[tone="armed"] {{ background-color: {t['warn_fill']}; color: {t['warn_fill_text']};
    border: 2px solid {QColor(t['warn_fill']).darker(140).name()}; font-weight: 700; }}
QPushButton[tone="unknown"] {{ background-color: transparent; color: {t['text_faint']};
    border: 1px dashed {t['border_strong']}; }}
QPushButton[tone="danger"] {{ background-color: transparent; color: {t['fault']};
    border: 1px solid {rgba(t['fault'], 0.7)}; }}
QPushButton[tone="danger"]:hover {{ background-color: {t['fault_fill']}; color: {t['fault_fill_text']}; }}
QPushButton[tone="danger"]:disabled {{ color: {t['text_faint']}; border-color: {t['border_soft']}; }}
QPushButton[tone="off"] {{ background-color: {t['raised']}; }}
QPushButton[role="link"] {{ background: transparent; border: none; color: {t['accent']}; padding: 2px 4px; }}
QPushButton[role="link"]:hover {{ text-decoration: underline; }}
QPushButton[role="flat"] {{ background: transparent; border: none; text-align: left; padding: 4px 2px; }}
QPushButton[role="toggle"]:checked {{ background-color: {t['accent']}; color: {t['on_accent']};
    border-color: {t['accent']}; font-weight: 600; }}
QPushButton[role="toggle"]:checked:hover {{ background-color: {t['accent_hover']}; }}
QPushButton[role="star"] {{ background: transparent; border: none; padding: 0; font-size: 13pt;
    color: {t['text_faint']}; }}
QPushButton[role="star"][on="true"] {{ color: {t['warn_fill']}; }}
QPushButton[role="star"]:hover {{ color: {t['warn']}; }}
QPushButton[role="flat"]:hover {{ color: {t['accent']}; }}
QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 4px; padding: 3px; }}
QToolButton:hover {{ background-color: {t['raised_hover']}; border-color: {t['border']}; }}

/* Entry fields */
QLineEdit, QAbstractSpinBox, QComboBox, QDateTimeEdit {{ background-color: {t['input']}; color: {t['text']};
    border: 1px solid {t['border']}; border-radius: 4px; padding: 3px 6px; min-height: 18px;
    selection-background-color: {t['accent']}; selection-color: {t['on_accent']}; }}
QLineEdit:focus, QAbstractSpinBox:focus, QComboBox:focus, QDateTimeEdit:focus {{ border-color: {t['accent']}; }}
QLineEdit:disabled, QAbstractSpinBox:disabled, QComboBox:disabled, QDateTimeEdit:disabled {{
    background-color: {t['panel']}; color: {t['text_faint']}; border-color: {t['border_soft']}; }}
QLineEdit[tone="fault"], QAbstractSpinBox[tone="fault"] {{ border: 2px solid {t['fault_fill']};
    background-color: {t['fault_bg']}; }}
QLineEdit[tone="warn"], QAbstractSpinBox[tone="warn"] {{ border: 2px solid {t['warn_fill']}; }}
QAbstractSpinBox {{ padding-right: 20px; }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{ subcontrol-origin: border; width: 18px;
    border: none; border-left: 1px solid {t['border_soft']}; background: transparent; }}
QAbstractSpinBox::up-button {{ subcontrol-position: top right; border-top-right-radius: 4px; }}
QAbstractSpinBox::down-button {{ subcontrol-position: bottom right; border-bottom-right-radius: 4px; }}
QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover {{ background: {t['raised_hover']}; }}
QAbstractSpinBox::up-button:pressed, QAbstractSpinBox::down-button:pressed {{ background: {t['raised_press']}; }}
QAbstractSpinBox::up-arrow {{ image: url({up_icon}); width: 9px; height: 9px; }}
QAbstractSpinBox::down-arrow {{ image: url({down_icon}); width: 9px; height: 9px; }}
QAbstractSpinBox::up-arrow:disabled, QAbstractSpinBox::up-arrow:off {{ image: url({up_off}); }}
QAbstractSpinBox::down-arrow:disabled, QAbstractSpinBox::down-arrow:off {{ image: url({down_off}); }}
QComboBox {{ padding-right: 22px; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right; width: 20px; border: none; }}
QComboBox::down-arrow {{ image: url({down_icon}); width: 10px; height: 10px; }}
QComboBox::down-arrow:disabled {{ image: url({down_off}); }}
QDateTimeEdit::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right; width: 20px;
    border: none; border-left: 1px solid {t['border_soft']}; }}
QDateTimeEdit::down-arrow {{ image: url({down_icon}); width: 10px; height: 10px; }}
QComboBox QAbstractItemView {{ background-color: {t['card']}; color: {t['text']}; border: 1px solid {t['border']};
    selection-background-color: {t['accent_soft']}; selection-color: {t['text']}; outline: none; }}
QPlainTextEdit, QTextEdit, QTextBrowser {{ background-color: {t['input']}; color: {t['text']};
    border: 1px solid {t['border']}; border-radius: 4px; selection-background-color: {t['accent']}; }}

/* Lists and tables */
QListView, QTreeView, QTableView {{ background-color: {t['input']}; alternate-background-color: {t['card_alt']};
    color: {t['text']}; border: 1px solid {t['border']}; border-radius: 4px; gridline-color: {t['border_soft']};
    selection-background-color: {t['accent_soft']}; selection-color: {t['text']}; outline: none; }}
QListView::item, QTreeView::item {{ padding: 2px 2px; }}
QListView[tone="fault"] {{ background-color: {t['fault_bg']}; color: {t['fault']}; font-weight: 600;
    border: 1px solid {rgba(t['fault_fill'], 0.6)}; }}
QListView[tone="warn"] {{ background-color: {t['warn_bg']}; color: {t['warn']}; border: 1px solid {rgba(t['warn_fill'], 0.6)}; }}
QListView[tone="off"] {{ color: {t['text_muted']}; }}
QListView[tone="unknown"] {{ color: {t['text_faint']}; border: 1px dashed {t['border_strong']}; }}
QHeaderView::section {{ background-color: {t['card_alt']}; color: {t['text_muted']}; font-weight: 600;
    border: none; border-right: 1px solid {t['border_soft']}; border-bottom: 1px solid {t['border']};
    padding: 4px 6px; }}
QTableCornerButton::section {{ background-color: {t['card_alt']}; border: none; }}

/* Scroll bars */
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {t['border']}; min-height: 24px; border-radius: 4px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {t['border']}; min-width: 24px; border-radius: 4px; margin: 2px; }}
QScrollBar::handle:hover {{ background: {t['border_strong']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* Menus, tool bars, splitters, progress */
QMenuBar {{ background-color: {t['window']}; color: {t['text']}; border-bottom: 1px solid {t['border_soft']}; }}
QMenuBar::item {{ padding: 4px 10px; background: transparent; }}
QMenuBar::item:selected {{ background-color: {t['raised_hover']}; border-radius: 3px; }}
QMenu {{ background-color: {t['card']}; color: {t['text']}; border: 1px solid {t['border']}; padding: 4px; }}
QMenu::item {{ padding: 5px 22px 5px 22px; border-radius: 3px; }}
QMenu::item:selected {{ background-color: {t['accent_soft']}; }}
QMenu::item:disabled {{ color: {t['text_faint']}; }}
QMenu::separator {{ height: 1px; background: {t['border_soft']}; margin: 4px 6px; }}
QToolBar {{ background-color: {t['window']}; border: none; border-bottom: 1px solid {t['border_soft']};
    padding: 2px 6px; spacing: 6px; }}
QToolBar QLabel {{ color: {t['text_muted']}; }}
QSplitter::handle {{ background-color: {t['window']}; }}
QSplitter::handle:hover {{ background-color: {t['accent']}; }}
QProgressBar {{ background-color: {t['input']}; border: 1px solid {t['border']}; border-radius: 4px;
    text-align: center; color: {t['text']}; min-height: 14px; }}
QProgressBar::chunk {{ background-color: {t['accent']}; border-radius: 3px; }}
QStatusBar {{ background-color: {t['window']}; color: {t['text_muted']}; }}
QCheckBox, QRadioButton {{ spacing: 6px; background: transparent; }}
QCheckBox::indicator, QGroupBox::indicator {{ width: 14px; height: 14px; border: 1px solid {t['border_strong']};
    border-radius: 3px; background: {t['input']}; }}
QCheckBox::indicator:hover, QGroupBox::indicator:hover {{ border-color: {t['accent']}; }}
QCheckBox::indicator:checked, QGroupBox::indicator:checked {{ background: {t['accent']}; border-color: {t['accent']};
    image: url({check_on}); }}
QCheckBox::indicator:disabled {{ background: {t['panel']}; border-color: {t['border_soft']}; }}
QCheckBox::indicator:checked:disabled {{ background: {t['border']}; }}
QRadioButton::indicator {{ width: 14px; height: 14px; border: 1px solid {t['border_strong']}; border-radius: 8px;
    background: {t['input']}; }}
QRadioButton::indicator:hover {{ border-color: {t['accent']}; }}
QRadioButton::indicator:checked {{ border: 4px solid {t['accent']}; background: {t['on_accent']}; width: 8px;
    height: 8px; }}
QMenu::indicator {{ width: 14px; height: 14px; left: 4px; }}
QMenu::indicator:checked {{ image: url({check_menu}); }}
"""


# --- pyqtgraph ---------------------------------------------------------------------------------
def style_plot(plot, restyle=None):
    """Colour a pyqtgraph PlotWidget / PlotItem / GraphicsLayoutWidget for the current theme,
    and again after every theme switch. restyle(plot): optional extra step for the caller's own
    items (curves, markers), run after the colours change. (A method is held weakly: the plot
    and its panel can still be freed.)"""
    _style_plot_now(plot)
    _plots[plot] = _weakly(restyle)
    if restyle is not None:
        restyle(plot)


def _weakly(fn):
    """A bound method that doesn't keep its object alive (None once the object has gone)."""
    if fn is None or not hasattr(fn, "__self__"):
        return fn
    ref, func = weakref.ref(fn.__self__), fn.__func__

    def call(plot):
        obj = ref()
        if obj is not None:
            func(obj, plot)
    return call


def _style_plot_now(plot):
    try:
        import pyqtgraph as pg
    except ImportError:
        return
    t = tokens()
    bg = qcolor(t["plot_bg"])
    items = []
    if isinstance(plot, pg.GraphicsLayoutWidget):
        plot.setBackground(bg)
        items = [i for i in plot.ci.items if isinstance(i, pg.PlotItem)]
    elif isinstance(plot, pg.PlotWidget):
        plot.setBackground(bg)
        items = [plot.getPlotItem()]
    elif isinstance(plot, pg.PlotItem):
        items = [plot]
    elif hasattr(plot, "setBackground"):
        plot.setBackground(bg)
    axis_pen = pg.mkPen(qcolor(t["plot_axis"]))
    text_pen = pg.mkPen(qcolor(t["plot_text"]))
    for item in items:
        for name in ("left", "bottom", "right", "top"):
            ax = item.getAxis(name) if name in item.axes else None
            if ax is not None:
                ax.setPen(axis_pen)
                ax.setTextPen(text_pen)
        legend = getattr(item, "legend", None)
        if legend is not None:
            style_legend(legend)
        title = getattr(item, "titleLabel", None)
        if title is not None and getattr(title, "text", ""):
            title.setText(title.text, color=t["text"])


def style_legend(legend):
    """A pyqtgraph legend in the theme: its box, border and the text of every entry (pyqtgraph's
    setLabelTextColor only stores the colour for new entries: existing ones are re-rendered here)."""
    import pyqtgraph as pg
    t = tokens()
    legend.setBrush(pg.mkBrush(qcolor(t["plot_legend_bg"])))
    legend.setPen(pg.mkPen(qcolor(t["border"])))
    ink = qcolor(t["text"])
    try:
        legend.setLabelTextColor(ink)
        for _sample, label in getattr(legend, "items", []):
            label.setText(label.text, color=ink)
    except AttributeError:
        pass


def _restyle_plots():
    for plot, restyle in list(_plots.items()):
        try:
            _style_plot_now(plot)
            if restyle is not None:
                restyle(plot)
        except RuntimeError:            # its Qt object has gone
            _plots.pop(plot, None)


# --- Switching --------------------------------------------------------------------------------
def apply_theme(app: QApplication, mode: str, persist: bool = False):
    """Restyle everything for `mode` now. persist: also save it for the next start (and for the
    other program, which follows within a second)."""
    mode = mode if mode in MODES else DEFAULT_MODE
    _state["mode"] = mode
    app.setPalette(build_palette(mode))
    app.setStyleSheet(build_stylesheet(mode))
    try:
        import pyqtgraph as pg
        pg.setConfigOption("background", tokens(mode)["plot_bg"])
        pg.setConfigOption("foreground", tokens(mode)["plot_text"])
    except ImportError:
        pass
    try:
        from qfluentwidgets import Theme, setTheme
        setTheme(Theme.DARK if mode == "dark" else Theme.LIGHT)
    except Exception:       # (pop-ups and icons only: never stop a theme change over them)
        log.debug("qfluentwidgets theme not set", exc_info=True)
    _restyle_plots()
    if persist:
        save_mode(mode)
    theme_changed().emit(mode)


def toggle(app: QApplication = None):
    apply_theme(app or QApplication.instance(), "light" if _state["mode"] == "dark" else "dark", persist=True)


# --- The saved choice, shared by both programs -------------------------------------------------
def appearance_path() -> str:
    """appearance.json: {"theme": "dark"}. In SINDIE_SETTINGS_DIR when that is set (the tests),
    otherwise in config/ (per machine, not in git)."""
    from src.core.env import getenv
    folder = getenv("SETTINGS_DIR").strip()
    if not folder:
        folder = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "config"))
    return os.path.join(folder, "appearance.json")


def saved_mode() -> str:
    try:
        with open(appearance_path(), encoding="utf-8") as f:
            mode = json.load(f).get("theme", DEFAULT_MODE)
        return mode if mode in MODES else DEFAULT_MODE
    except (OSError, ValueError, AttributeError):
        return DEFAULT_MODE


def save_mode(mode: str):
    path = appearance_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"theme": mode}, f)
        os.replace(tmp, path)
    except OSError:
        log.warning("Could not save the theme choice to %s", path, exc_info=True)


class _Follower(QObject):
    """Checks appearance.json once a second; when the other program switched theme, so does this one."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self._stamp = self._mtime()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._check)
        self.timer.start(1000)

    @staticmethod
    def _mtime():
        try:
            return os.stat(appearance_path()).st_mtime_ns
        except OSError:
            return None

    def _check(self):
        stamp = self._mtime()
        if stamp == self._stamp:
            return
        self._stamp = stamp
        mode = saved_mode()
        if mode != _state["mode"]:
            apply_theme(self.app, mode)


def install(app: QApplication, mode: str = None, follow: bool = True):
    """Start-up: the Fusion style (the same widgets on every platform), the interface font, and
    the saved theme (or `mode`). follow: switch along when the other program changes theme."""
    app.setStyle("Fusion")
    ui = fonts()["ui"]
    if app.font().family() != ui:
        f = QFont(app.font())
        f.setFamily(ui)
        app.setFont(f)
    apply_theme(app, mode or saved_mode())
    if follow and _state["sync"] is None:
        _state["sync"] = _Follower(app)


def add_theme_menu(menu, parent=None):
    """'Theme' entries for a View menu: Dark / Light (checked = current), Ctrl+Shift+T toggles."""
    from PyQt6.QtGui import QAction, QActionGroup, QKeySequence
    sub = menu.addMenu("Theme")
    group = QActionGroup(sub)
    group.setExclusive(True)
    actions = {}
    for mode, label in (("dark", "Dark"), ("light", "Light")):
        act = QAction(label, sub, checkable=True)
        act.setChecked(mode == current_mode())
        act.triggered.connect(lambda _=False, m=mode: apply_theme(QApplication.instance(), m, persist=True))
        group.addAction(act)
        sub.addAction(act)
        actions[mode] = act
    sub.addSeparator()
    toggle_act = QAction("Switch theme", sub)
    toggle_act.setShortcut(QKeySequence("Ctrl+Shift+T"))
    toggle_act.triggered.connect(lambda: toggle())
    sub.addAction(toggle_act)
    if parent is not None:
        parent.addAction(toggle_act)          # the shortcut works without opening the menu

    # (owned by the menu, so the connection goes with it: a closure would outlive the window)
    sub._theme_sync = _MenuSync(actions, sub)
    theme_changed().connect(sub._theme_sync.sync)
    return sub


class _MenuSync(QObject):
    """Ticks the Theme menu's entry for the current theme (it may change from the other program)."""

    def __init__(self, actions: dict, parent):
        super().__init__(parent)
        self.actions = actions

    def sync(self, mode: str):
        for m, a in self.actions.items():
            a.setChecked(m == mode)
