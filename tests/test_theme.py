"""The look of the HMI and the viewer (src/gui/theme.py, style.py, reflow.py) and the HMI's panel
layout (control_main.py): live theme switching shared through appearance.json, the text size and
colour-blind safe colours (off unless chosen, and off they change nothing), styling only on a
change, panels that reflow and scroll instead of growing the window, the default layout fitted to
the screen, and panels behind another tab not refreshed."""
import json
import os

import pytest
from PyQt6.QtCore import QRect
from PyQt6.QtWidgets import QApplication, QLabel, QPushButton


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def settings_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SINDIE_SETTINGS_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def theme_mod(app, settings_dir):
    from src.gui import theme
    before = theme.current_mode(), theme.text_scale(), theme.palette_name()
    yield theme
    theme.apply_appearance(app, *before)


# ------------------------------------------------------------------ theme
def test_switching_restyles_in_place_and_tells_the_widgets(app, theme_mod):
    theme = theme_mod
    seen = []
    theme.theme_changed().connect(seen.append)
    theme.apply_theme(app, "dark")
    dark_window = app.palette().window().color().name()
    assert dark_window == theme.tokens("dark")["panel"].lower()
    theme.apply_theme(app, "light")
    assert app.palette().window().color().name() == theme.tokens("light")["panel"].lower()
    assert "SINDIE light theme" in app.styleSheet()
    assert seen[-2:] == ["dark", "light"]
    theme.theme_changed().disconnect(seen.append)


def test_the_choice_is_saved_and_shared(app, theme_mod, settings_dir):
    theme = theme_mod
    theme.apply_theme(app, "light", persist=True)
    path = theme.appearance_path()
    assert os.path.dirname(path) == str(settings_dir)
    assert json.load(open(path)) == {"theme": "light", "text_size": 1.0, "palette": "standard"}
    assert theme.saved_mode() == "light"
    # The other program wrote "dark": this one follows on its next check
    follower = theme._Follower(app)
    try:
        with open(path, "w") as f:
            json.dump({"theme": "dark"}, f)
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, (follower._stamp or 0) + 1_000_000_000))   # a later write
        follower._check()
        assert theme.current_mode() == "dark"
    finally:
        follower.timer.stop()
        follower.deleteLater()


def test_a_broken_or_missing_choice_falls_back_to_the_default(theme_mod, settings_dir):
    theme = theme_mod
    assert theme.saved_mode() == theme.DEFAULT_MODE                   # no file yet
    with open(theme.appearance_path(), "w") as f:
        f.write("{not json")
    assert theme.saved_mode() == theme.DEFAULT_MODE
    with open(theme.appearance_path(), "w") as f:
        json.dump({"theme": "purple"}, f)
    assert theme.saved_mode() == theme.DEFAULT_MODE


def test_plots_follow_the_theme(app, theme_mod):
    import pyqtgraph as pg
    theme = theme_mod
    calls = []

    class Panel:
        def restyle(self, plot):
            calls.append(plot)

    panel = Panel()
    plot = pg.PlotWidget()
    plot.addLegend()
    plot.plot([0, 1], [0, 1], name="a curve")
    theme.apply_theme(app, "dark")
    theme.style_plot(plot, restyle=panel.restyle)
    assert plot.backgroundBrush().color().name() == theme.tokens("dark")["plot_bg"].lower()
    theme.apply_theme(app, "light")
    assert plot.backgroundBrush().color().name() == theme.tokens("light")["plot_bg"].lower()
    assert len(calls) == 2
    # the legend's existing entries are re-rendered in the new text colour
    _sample, label = plot.getPlotItem().legend.items[0]
    assert theme.tokens("light")["text"].lower().lstrip("#") in label.item.toHtml().lower()
    plot.deleteLater()


def test_readable_keeps_a_colour_visible_on_the_plot(theme_mod, app):
    theme = theme_mod
    theme.apply_theme(app, "light")
    white = theme.readable("#FFFFFF")                     # a white marker on the white plot
    assert theme._contrast(white, theme.qcolor("#FFFFFF")) >= 3.0
    theme.apply_theme(app, "dark")
    navy = theme.readable("#000080")                      # dark blue on the dark plot
    assert theme._contrast(navy, theme.qcolor(theme.tokens()["plot_bg"])) >= 3.0


def test_series_colours_repeat_after_eight(theme_mod):
    theme = theme_mod
    assert theme.series_color(0) == theme.series_color(8)
    assert len({theme.series_color(i).name() for i in range(8)}) == 8


# ------------------------------------------------------------------ text size, colour-blind safe colours
MEANINGS = ("ok", "warn", "fault", "info", "seq")


def test_the_options_are_off_until_chosen_and_then_change_nothing(app, theme_mod):
    """Josh (6 Nov): optional, and by default it should look as it does - the same colours, the
    same stylesheet, the same font, also after the options were tried and turned off again."""
    from PyQt6.QtGui import QFont
    theme = theme_mod
    theme.apply_appearance(app, "dark", 1.0, "standard")
    assert theme.DEFAULT_TEXT_SCALE == 1.0 and theme.DEFAULT_PALETTE == "standard"
    for mode in theme.MODES:
        assert theme.tokens(mode) is theme.PALETTES[mode]
    sheet = theme.build_stylesheet("dark")
    assert "font-size: 12pt" in sheet and "colour-blind" not in sheet
    assert sheet.startswith("\n/* SINDIE dark theme - generated")
    font = QFont(app.font())
    theme.apply_appearance(app, text_scale=1.5, palette="colour-blind")
    assert app.font() != font and theme.build_stylesheet("dark") != sheet
    theme.apply_appearance(app, text_scale=1.0, palette="standard")
    assert app.font() == font
    assert theme.build_stylesheet("dark") == sheet


def test_larger_text_grows_every_font(app, theme_mod):
    from PyQt6.QtGui import QFontMetricsF
    theme = theme_mod
    theme.apply_appearance(app, text_scale=1.0)
    lbl = QLabel("Extraction 15.008 kV")
    normal = QFontMetricsF(app.font()).horizontalAdvance(lbl.text())
    base = app.font().pointSizeF()
    theme.apply_appearance(app, text_scale=1.3)
    assert theme.text_scale() == 1.3 and theme.pt(10) == pytest.approx(13)
    if base > 0:
        assert app.font().pointSizeF() == pytest.approx(base * 1.3)
    assert QFontMetricsF(app.font()).horizontalAdvance(lbl.text()) > normal * 1.2
    sheet = app.styleSheet()
    assert "font-size: 15.6pt" in sheet and "font-size: 36.4pt" in sheet     # readouts: 12 pt and 28 pt
    assert "width: 18px; height: 18px" in sheet                              # tick boxes grow too
    assert "text at 1.3 x" in sheet.splitlines()[1]
    theme.apply_appearance(app, text_scale=1.12)                              # not a step: the nearest
    assert theme.text_scale() == 1.15
    lbl.deleteLater()


def test_plot_legends_follow_the_text_size(app, theme_mod):
    import pyqtgraph as pg
    theme = theme_mod
    theme.apply_appearance(app, text_scale=1.0)
    plot = pg.PlotWidget()
    plot.addLegend()
    plot.plot([0, 1], [0, 1], name="a curve")
    theme.style_plot(plot)
    _sample, label = plot.getPlotItem().legend.items[0]
    assert label.opts["size"] == "9pt"                      # (pyqtgraph's own: as before)
    theme.apply_appearance(app, text_scale=1.3)
    assert label.opts["size"] == "11.7pt"
    plot.plot([0, 1], [1, 0], name="a new curve")           # and one added afterwards
    assert plot.getPlotItem().legend.items[1][1].opts["size"] == "11.7pt"
    plot.deleteLater()


def test_the_options_are_saved_shared_and_followed(app, theme_mod, settings_dir):
    theme = theme_mod
    theme.apply_appearance(app, mode="light", text_scale=1.15, palette="colour-blind", persist=True)
    path = theme.appearance_path()
    assert json.load(open(path)) == {"theme": "light", "text_size": 1.15, "palette": "colour-blind"}
    assert theme.saved_appearance() == {"mode": "light", "text_scale": 1.15, "palette": "colour-blind"}
    theme.save_mode("dark")                         # the theme alone: the options are kept
    assert theme.saved_appearance() == {"mode": "dark", "text_scale": 1.15, "palette": "colour-blind"}
    follower = theme._Follower(app)                 # the other program changed them: this one follows
    try:
        with open(path, "w") as f:
            json.dump({"theme": "light", "text_size": 1.5, "palette": "standard"}, f)
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, (follower._stamp or 0) + 1_000_000_000))
        follower._check()
        assert (theme.current_mode(), theme.text_scale(), theme.palette_name()) == ("light", 1.5, "standard")
    finally:
        follower.timer.stop()
        follower.deleteLater()
    # A file from before the options (the theme only), and values that make no sense: the defaults
    with open(path, "w") as f:
        json.dump({"theme": "dark"}, f)
    assert theme.saved_appearance() == {"mode": "dark", "text_scale": 1.0, "palette": "standard"}
    with open(path, "w") as f:
        json.dump({"theme": "dark", "text_size": "huge", "palette": "sepia"}, f)
    assert theme.saved_appearance() == {"mode": "dark", "text_scale": 1.0, "palette": "standard"}


def test_the_view_menu_sets_the_options_and_shows_them(app, theme_mod, settings_dir):
    from PyQt6.QtWidgets import QMainWindow, QMenu
    theme = theme_mod
    theme.apply_appearance(app, text_scale=1.0, palette="standard")
    win = QMainWindow()
    menu = QMenu("View", win)
    theme.add_theme_menu(menu, win)
    sizes = next(a.menu() for a in menu.actions() if a.text() == "Text size")
    assert [a.text() for a in sizes.actions()] == ["Normal", "Large (115%)", "Larger (130%)", "Largest (150%)"]
    colours = next(a for a in menu.actions() if a.text() == "Colour-blind safe colours")
    assert sizes.actions()[0].isChecked() and not colours.isChecked()
    sizes.actions()[2].trigger()
    assert theme.text_scale() == 1.3 and theme.saved_appearance()["text_scale"] == 1.3
    colours.trigger()
    assert theme.palette_name() == "colour-blind" and theme.saved_appearance()["palette"] == "colour-blind"
    theme.apply_appearance(app, text_scale=1.0, palette="standard")        # (from the other program)
    assert sizes.actions()[0].isChecked() and not colours.isChecked()
    win.deleteLater()


def test_the_safe_colours_replace_only_colours_that_mean_something(app, theme_mod):
    theme = theme_mod
    for mode in theme.MODES:
        changed = set(theme.COLOUR_BLIND[mode])
        assert changed <= set(theme.PALETTES[mode])
        assert all(k.split("_")[0] in MEANINGS + ("alarm", "ev", "series") for k in changed), changed
    seen = []
    theme.theme_changed().connect(seen.append)
    theme.apply_appearance(app, mode="dark", palette="colour-blind")
    assert theme.tokens()["fault_fill"] == theme.COLOUR_BLIND["dark"]["fault_fill"]
    assert theme.COLOUR_BLIND["dark"]["fault_fill"] in app.styleSheet()
    assert "colour-blind safe colours" in app.styleSheet().splitlines()[1]
    assert seen == ["dark"]
    theme.theme_changed().disconnect(seen.append)


@pytest.mark.parametrize("mode", ["dark", "light"])
def test_the_safe_colours_stay_apart_for_colour_blind_eyes(theme_mod, mode):
    """Under simulated deuteranopia and protanopia (red-green colour blindness: about 1 man in 12)
    every two meanings stay apart: the fills (chips, buttons, the diagrams' parts), the text and
    lines, the alarm priorities, the viewer's event kinds and every two of its line colours (any
    two can share a plot) - and under tritanopia, which is rare, less but still apart. (In the
    standard colours, solid green and red come to Delta E 6, their text to 3; blue and violet to 2.)"""
    from tests.colour_vision import closest_pair
    t = theme_mod.tokens(mode, "colour-blind")
    sets = [
        ("fills", {m: t[f"{m}_fill"] for m in MEANINGS}, {"off": t["off_bg"], "off line": t["border_strong"]}, 10),
        ("text and lines", {m: t[m] for m in MEANINGS}, {"off": t["off_text"], "off line": t["border_strong"]}, 9),
        ("alarm priorities", {n: t[f"alarm_{n}"] for n in range(1, 5)}, None, 9),
        ("event kinds", {k: t[f"ev_{k}"] for k in ("fault", "cleared", "watchdog", "phase", "tolerance", "command")},
         None, 9),
        ("line colours", dict(enumerate(t["series"])), None, 9),
    ]
    for name, colours, against, least in sets:
        for kind, floor in (("deutan", least), ("protan", least), ("tritan", 6), (None, 12)):
            distance, a, b = closest_pair(colours, kind, against)
            assert distance >= floor, (mode, name, kind or "normal vision", a, b, round(distance, 1))


@pytest.mark.parametrize("mode", ["dark", "light"])
def test_the_safe_colours_keep_text_and_lines_readable(theme_mod, mode):
    from tests.colour_vision import contrast
    t = theme_mod.tokens(mode, "colour-blind")
    for m in MEANINGS:
        assert contrast(t[f"{m}_fill"], t[f"{m}_fill_text"]) >= 4.5, m
        for bg in ("panel", "card", "card_alt", f"{m}_bg"):
            assert contrast(t[m], t[bg]) >= 4.5, (m, bg)
    for n in range(1, 5):
        assert contrast(t[f"alarm_{n}"], t[f"alarm_{n}_text"]) >= 4.5, n
        assert contrast(t[f"alarm_{n}_soft"], t[f"alarm_{n}_soft_text"]) >= 4.5, n
    for colour in t["series"] + [t[f"ev_{k}"] for k in ("fault", "cleared", "watchdog", "phase", "tolerance")]:
        assert contrast(colour, t["plot_bg"]) >= 3.0, colour


# ------------------------------------------------------------------ style
def test_styling_happens_only_on_a_change(app, theme_mod):
    """The panels refresh 10 times a second: re-styling a widget every time (setStyleSheet, as
    they used to) cost ~600 restyles a second."""
    from src.gui.style import set_tone, tone
    lbl = QLabel("x")
    assert set_tone(lbl, "ok") is True
    assert tone(lbl) == "ok"
    assert set_tone(lbl, "ok") is False                    # the same: nothing done
    assert set_tone(lbl, "fault") is True
    assert set_tone(lbl, None) is True and tone(lbl) == ""
    assert set_tone(lbl, None) is False


def test_tones_are_drawn_by_the_stylesheet(app, theme_mod):
    theme = theme_mod
    theme.apply_theme(app, "dark")
    sheet = app.styleSheet()
    for t in ("ok", "on", "warn", "fault", "off", "info", "seq", "unknown"):
        assert f'QLabel[tone="{t}"]' in sheet
    for t in ("on", "warn", "fault", "info", "seq", "primary", "armed", "unknown", "danger"):
        assert f'QPushButton[tone="{t}"]' in sheet
    # (Qt stylesheets have no presence-only selectors such as QLabel[tone])
    assert "QLabel[tone]" not in sheet and "QPushButton[tone]" not in sheet


def test_a_chip_in_a_card_keeps_its_colour(app, theme_mod):
    """Found in review: a rule for labels inside cards outranked the chips' tone rules, so a vacuum
    gauge's TRIPPED chip lost its red fill (white text on a white card in the light theme)."""
    from PyQt6.QtWidgets import QFrame, QVBoxLayout
    from src.gui.style import chip, set_role
    theme = theme_mod
    for mode in ("light", "dark"):
        theme.apply_theme(app, mode)
        card = QFrame()
        set_role(card, "card")
        lay = QVBoxLayout(card)
        c = chip("TRIPPED", "fault")
        lay.addWidget(c)
        card.resize(240, 80)
        card.show()
        app.processEvents()
        img = c.grab().toImage()
        fill = img.pixelColor(5, img.height() // 2)          # in the chip's padding, clear of the text
        assert fill.name() == theme.tokens()["fault_fill"].lower(), mode
        card.deleteLater()


# ------------------------------------------------------------------ reflow
def test_cards_reflow_with_the_width(app):
    from src.gui.reflow import ReflowGrid
    grid = ReflowGrid(max_columns=3, spacing=8)
    cards = []
    for i in range(6):
        b = QPushButton(f"card {i}")
        b.setMinimumWidth(100)
        grid.add(b)
        cards.append(b)
    grid.show()
    for width, columns in ((400, 3), (300, 2), (230, 2), (150, 1)):
        grid.resize(width, 300)
        app.processEvents()
        assert grid.columns() == columns, width
    assert grid.minimumWidth() == 100            # one card: the panel can always fold it to one column
    grid.deleteLater()


def test_four_that_do_not_fit_go_two_by_two(app):
    from src.gui.reflow import ReflowGrid
    grid = ReflowGrid(max_columns=4)
    for i in range(4):
        b = QPushButton("x")
        b.setMinimumWidth(100)
        grid.add(b)
    grid.show()
    grid.resize(340, 100)                    # 3 would fit: 3 + 1 looks odd
    app.processEvents()
    assert grid.columns() == 2
    grid.deleteLater()


# ------------------------------------------------------------------ the HMI's panels
@pytest.fixture
def hmi(app, settings_dir):
    from src.gui import theme
    theme.apply_theme(app, theme.DEFAULT_MODE)
    from src.gui.control.control_main import ControlMainWindow
    w = ControlMainWindow()
    w.watchdog_timer.stop()
    w.resize(1500, 850)
    w.show()
    app.processEvents()
    yield w
    w.close()
    w.deleteLater()
    app.processEvents()


def test_the_window_fits_a_laptop_screen(hmi):
    hint = hmi.minimumSizeHint()
    assert hint.width() <= 1280 and hint.height() <= 700, hint


def test_a_panel_can_be_made_smaller_than_its_contents(hmi, app):
    """Each panel scrolls: dropped in a narrow column it doesn't push the window wider."""
    for dock in hmi._docks:
        scroll = dock.widget()
        assert scroll.objectName() == "PanelScroll"
        assert scroll.minimumSizeHint().width() < 300, dock.objectName()


def test_default_layout_and_reset_without_restart(hmi, app):
    from PyQt6.QtCore import Qt
    hmi.vac_dock.close()                                    # the operator rearranges...
    hmi.splitDockWidget(hmi.src_dock, hmi.optics_dock, Qt.Orientation.Horizontal)
    hmi.mass_scan_dock.setFloating(True)
    app.processEvents()
    hmi._reset_layout()                                     # ...and puts it all back
    app.processEvents()
    on_screen = {d.objectName() for d in hmi._docks if hmi.on_screen(d)}
    assert on_screen == {"VacuumDock", "HealthDock", "SourceDock", "AlarmDock"}
    assert set(d.objectName() for d in hmi.tabifiedDockWidgets(hmi.src_dock)) >= {"OpticsDock", "MassScanDock"}


def test_a_window_bigger_than_the_screen_is_brought_back(hmi, app):
    avail = hmi.screen().availableGeometry()
    hmi.setGeometry(QRect(avail.right() + 500, avail.bottom() + 500, avail.width() * 2, avail.height() * 2))
    hmi._fit_to_screen()
    frame = hmi.frameGeometry()
    assert avail.contains(frame.center())
    assert hmi.width() <= avail.width() and hmi.height() <= avail.height()


def test_an_old_saved_layout_is_not_restored(hmi, app):
    """Layouts saved before LAYOUT_VERSION 2 (every panel open, sized for a big monitor)."""
    state = hmi.saveState(1)
    assert not hmi.restoreState(state, hmi.LAYOUT_VERSION)


def test_panels_behind_another_tab_are_not_refreshed(hmi, app):
    calls = {}
    for widget in hmi._refreshed:
        name = type(widget).__name__
        calls[name] = 0

        def counting(data, n=name):
            calls[n] += 1
        widget.update_telemetry = counting
    hmi.src_dock.raise_()
    app.processEvents()
    hmi._ui_update_loop()
    assert calls["IonSourceWidget"] == 1 and calls["VacuumControlWidget"] == 1
    assert calls["BeamlineOpticsWidget"] == 0          # tabbed behind the source panel
    assert calls["FaradaySMUWidget"] == 0
    assert calls["AlarmPanel"] == 1                    # always: the horn
    assert calls["PrecisionMassScannerWidget"] == 1    # always: follows a running scan
    hmi.optics_dock.raise_()                           # its tab chosen: brought up to date at once...
    app.processEvents()
    hmi._ui_update_loop()                              # ...and refreshed; the source panel now isn't
    assert calls["BeamlineOpticsWidget"] >= 1 and calls["IonSourceWidget"] == 1


def test_theme_switch_reaches_the_hmi_without_a_restart(hmi, app):
    from src.gui import theme
    theme.apply_theme(app, "light")
    app.processEvents()
    assert hmi.src_widget.palette().window().color().name() == theme.tokens("light")["panel"].lower()
    chip = hmi.health_widget.lbl_comms
    assert chip.property("tone")                       # drawn by the stylesheet, so restyled with it
    theme.apply_theme(app, "dark")
