"""F12 capture's Style > Thesis figure (src/gui/thesis_style.py): the plots drawn in the style of the
thesis' own figures (its repository's analysis/common/thesis_style.py) - Computer Modern, 9.5 pt and 8.5 pt,
dark grey 0.8 pt axes with the ticks out, a light grey grid behind the data, white, the light theme's
colours - at their sizes on the page; and everything as it was afterwards. Also: the SVG's text in
proportion (Qt's SVG writer turned a font's points into units at the page's resolution, not the screen's)."""
import re
import shutil
import subprocess

import numpy as np
import pytest


def _app():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def lanes():
    """Two lanes as the viewer has them (dark theme): channel A drawn by two curves - its history, empty
    and in the legend, and its live data - B beside it, C in the lane below in A's colour (the viewer's
    colours restart in each lane), that lane's left axis without numbers, and an event marker."""
    app = _app()
    import pyqtgraph as pg
    from PyQt6.QtCore import Qt
    from src.gui import theme
    dark = theme.tokens("dark")["series"]
    lw = pg.GraphicsLayoutWidget()
    lw.resize(800, 500)
    lw.setBackground("#161A1F")
    top, bottom = lw.addPlot(row=0, col=0), lw.addPlot(row=1, col=0)
    blue, orange = pg.mkPen(dark[0], width=1), pg.mkPen(dark[1], width=1)
    x = np.linspace(0, 100, 500)
    for p in (top, bottom):
        p.showGrid(x=True, y=True, alpha=0.35)
        p.addLegend(offset=(10, 10))
        p.getAxis("left").setPen(pg.mkPen("#56606C"))
        p.getAxis("left").setTextPen(pg.mkPen("#9AA3AD"))
    a_hist = pg.PlotDataItem(pen=blue)
    top.addItem(a_hist)
    top.legend.addItem(a_hist, "A [W]")
    a_live = pg.PlotDataItem(x, 100 * np.sin(x / 10), pen=blue)
    top.addItem(a_live)
    b = top.plot(x, 100 * np.cos(x / 10), pen=orange, name="B [W]")
    c = bottom.plot(x, np.exp(-x / 30), pen=blue, name="C [mbar]")
    bottom.getAxis("left").setStyle(showValues=False)
    marker = pg.InfiniteLine(pos=50, angle=90, pen=pg.mkPen("#FFEA00", width=1.5, style=Qt.PenStyle.DashLine),
                             label="Beam on", labelOpts={"position": 0.5, "color": "#FFEA00", "fill": "#1E242B"})
    top.addItem(marker)
    lw.show()
    app.processEvents()
    yield dict(lw=lw, top=top, bottom=bottom, a_hist=a_hist, a_live=a_live, b=b, c=c, marker=marker)
    lw.close()
    lw.deleteLater()
    app.processEvents()


def settle(widget):
    from src.gui.figure_capture import _settle
    _settle(widget)


def state(lw):
    """Everything the style changes, to compare before and after."""
    import pyqtgraph as pg
    from pyqtgraph.graphicsItems.LegendItem import LegendItem
    out = [lw.backgroundBrush().color().name(), len(lw.scene().items())]
    for it in lw.scene().items():
        if isinstance(it, pg.AxisItem):
            style = {k: (v.toString() if hasattr(v, "toString") else v) for k, v in it.style.items()}
            pens = [None if q is None else (q.color().name(), q.widthF(), q.isCosmetic())
                    for q in (it._pen, it._textPen, it._tickPen)]
            out.append((it.orientation, pens, style, it.grid, dict(it.labelStyle), it.fixedWidth, it.fixedHeight,
                        "tickStrings" in it.__dict__, it.label.font().toString(), it.labelText, it.labelUnits,
                        it.label.isVisible(), it.autoSIPrefix))
        elif isinstance(it, LegendItem):
            out.append((it.opts["pen"].color().name(), it.opts["pen"].widthF(), it.opts["brush"].color().name(),
                        [(dict(label.opts), label.item.font().toString(), sample.minimumSize(), sample.maximumSize(),
                          label.text) for sample, label in it.items]))
        elif isinstance(it, pg.TextItem):
            out.append((it.textItem.font().toString(), it.color.name(), it.fill.color().name()))
    return out


def test_the_thesis_style_draws_the_plots_as_the_thesis_figures_then_puts_them_back(lanes):
    import pyqtgraph as pg
    from src.gui import theme
    from src.gui.thesis_style import ThesisStyle, _GridLines
    lw, top, bottom = lanes["lw"], lanes["top"], lanes["bottom"]
    before = state(lw)
    look = ThesisStyle(lw, per_inch=200.0)
    px = lambda pt: pt / 72 * 200.0                                     # noqa: E731
    try:
        assert lw.backgroundBrush().color().name() == "#ffffff"
        left = top.getAxis("left")
        assert left._pen.color().name() == "#52514e" and not left._pen.isCosmetic()
        assert left._pen.widthF() == pytest.approx(px(0.8)) and left._textPen.color().name() == "#0b0b0b"
        font = left.style["tickFont"]
        assert font.pixelSize() == round(px(8.5)) and font.family() == "Latin Modern Roman"
        assert left.style["tickLength"] == round(px(3.5))                # ticks out
        assert bottom.getAxis("left").style["tickLength"] == 0           # (no numbers there: no ticks)
        assert left.grid is False                                        # (pyqtgraph's grid: over the data)
        grids = [it for it in lw.scene().items() if isinstance(it, _GridLines)]
        assert len(grids) == 4 and all(g.zValue() < 0 for g in grids)   # behind the data
        assert grids[0].pen.color().name() == "#e1e0d9" and grids[0].pen.widthF() == pytest.approx(px(0.6))
        legend = top.legend
        assert legend.opts["brush"].color().name() == "#ffffff" and legend.opts["pen"].widthF() == pytest.approx(px(0.5))
        assert all(label.opts["color"] == "#0B0B0B" and label.opts["size"] == f"{round(px(8.5))}px"
                   for _s, label in legend.items)
        # the light theme's colour for each channel's place in its lane (the thesis' colours), a channel's
        # two curves alike; a colour that isn't one of the channels' is left as it is
        light = [c.lower() for c in theme.tokens("light")["series"]]
        colour = lambda item: look.colour_for(item).name()               # noqa: E731
        assert colour(lanes["a_live"].curve) == colour(lanes["a_hist"].curve) == light[0] == "#2a78d6"
        assert colour(lanes["b"].curve) == light[1] == "#eb6834"
        assert colour(lanes["c"].curve) == light[0]
        assert look.colour_for(pg.PlotDataItem([0, 1], [0, 1], pen="#123456").curve) is None
        # the event marker: its line and label made to stand out on white
        assert theme._contrast(look.colour_for(lanes["marker"]), theme.qcolor("#FFFFFF")) >= 3.0
        assert theme._contrast(lanes["marker"].label.color, theme.qcolor("#FFFFFF")) >= 4.5
    finally:
        look.remove()
    _app().processEvents()
    assert state(lw) == before
    assert not any(isinstance(it, _GridLines) for it in lw.scene().items())
    assert isinstance(top.getAxis("left"), pg.AxisItem)


def test_the_plots_line_up(lanes):
    """The lower lane has no numbers on the left and the upper one does: the thesis figures' panels line up."""
    from src.gui.thesis_style import ThesisStyle
    lw, top, bottom = lanes["lw"], lanes["top"], lanes["bottom"]
    look = ThesisStyle(lw, per_inch=150.0)
    try:
        settle(lw)
        assert abs(top.vb.sceneBoundingRect().left() - bottom.vb.sceneBoundingRect().left()) > 5
        look.align()
        settle(lw)
        a, b = top.vb.sceneBoundingRect(), bottom.vb.sceneBoundingRect()
        assert a.left() == pytest.approx(b.left(), abs=1) and a.right() == pytest.approx(b.right(), abs=1)
    finally:
        look.remove()
    settle(lw)
    assert bottom.getAxis("left").fixedWidth is None


def test_numbers_as_the_thesis_figures_write_them():
    """(What follows ^ the axis draws as a superscript: the thesis' type has no superscript figures.)"""
    from src.gui.thesis_style import tidy_number
    assert tidy_number("1.6e-07") == "1.6\u00d710^\u22127"
    assert tidy_number("1e-07") == tidy_number("1.0e-07") == "10^\u22127"
    assert tidy_number("2.5e+03") == "2.5\u00d710^3"
    assert tidy_number("-0.5") == "\u22120.5" and tidy_number("-1e-07") == "\u221210^\u22127"
    for same in ("0", "150", "16:51", "2026-10-26", "Oct 26\n12:00", ""):
        assert tidy_number(same) == same


def test_a_power_of_ten_is_drawn_as_a_superscript(tmp_path):
    app = _app()
    import pyqtgraph as pg
    from src.gui.figure_capture import render_widget
    from src.gui.thesis_style import ThesisStyle
    pw = pg.PlotWidget()
    pw.resize(400, 300)
    pw.plot([0, 1], [1e-7, 4e-7], pen="b")
    pw.getAxis("left").tickStrings = lambda values, scale, spacing: [f"{v:.1e}" for v in values]
    pw.show()
    app.processEvents()
    look = ThesisStyle(pw, per_inch=150.0)
    try:
        settle(pw)
        render_widget(pw, str(tmp_path / "e"), width_cm=4.0, formats=("svg",))
    finally:
        look.remove()
    pw.close()
    svg = (tmp_path / "e.svg").read_text(encoding="utf-8")
    big, small = round(8.5 / 72 * 150), round(round(8.5 / 72 * 150) * 0.7)
    texts = re.findall(r'font-size="(\d+)"[^>]*>\s*<text[^>]*>([^<]*)</text>', svg)
    assert (str(big), "10") in texts and (str(big), "2.0\u00d710") in texts         # (1.0e-07: just 10^-7)
    assert (str(small), "\u22127") in texts
    assert not any("^" in t or "e-07" in t for _size, t in texts)


def test_thesis_text_prints_at_its_size(tmp_path):
    """9 pt and 8 pt on the page in the SVG and the PDF alike (sizes in pixels: a size in points goes
    into an SVG at the SVG writer's resolution)."""
    app = _app()
    import pyqtgraph as pg
    from src.gui.figure_capture import render_widget
    from src.gui.thesis_style import ThesisStyle
    pw = pg.PlotWidget()
    pw.resize(400, 300)
    pw.addLegend()
    pw.plot(np.arange(10), np.arange(10.0), pen="b", name="Trace")
    pw.setLabel("left", "Current", units="A")
    pw.show()
    app.processEvents()
    per_inch = 400 / (4.0 / 2.54)
    look = ThesisStyle(pw, per_inch)
    try:
        settle(pw)
        render_widget(pw, str(tmp_path / "t"), width_cm=4.0, formats=("svg", "pdf"))
    finally:
        look.remove()
    pw.close()
    svg = (tmp_path / "t.svg").read_text()
    sizes = {float(v) for v in re.findall(r'font-size="([\d.]+)"', svg)}
    assert round(8.5 / 72 * per_inch) in sizes and round(9.5 / 72 * per_inch) in sizes  # numbers, legend; title
    assert re.search(r'<svg width="(40\.\d+|39\.9\d+)mm"', svg)
    if shutil.which("pdftotext"):
        out = subprocess.run(["pdftotext", "-bbox", str(tmp_path / "t.pdf"), "-"], capture_output=True, text=True).stdout
        box = {w: (float(x1) - float(x0), float(y1) - float(y0)) for x0, y0, x1, y1, w in
               re.findall(r'xMin="(-?[\d.]+)" yMin="(-?[\d.]+)" xMax="(-?[\d.]+)" yMax="(-?[\d.]+)">([^<]+)<', out)}
        # (a word's box: its type's ascent and descent, 1.0 to 1.5 times its size)
        assert 8.5 < box["Trace"][1] < 8.5 * 1.5                         # 8.5 pt type
        assert 9.5 < box["Current"][0] < 9.5 * 1.5                       # 9.5 pt type (turned on its side)


def test_svg_text_is_in_proportion_with_the_window(tmp_path):
    """The window's own text (9 pt: 12 of its pixels) is 12 units in the SVG, as in the PDF and PNG -
    at the page's resolution it was 31.75, 2.6 times too big, and the labels ran into each other."""
    app = _app()
    import pyqtgraph as pg
    from src.gui.figure_capture import render_widget
    pw = pg.PlotWidget()
    pw.resize(600, 400)
    pw.addLegend()
    pw.plot([0, 1], [0, 1], pen="r", name="Trace")
    pw.show()
    app.processEvents()
    render_widget(pw, str(tmp_path / "p"), width_cm=6.0, formats=("svg",))
    pw.close()
    svg = (tmp_path / "p.svg").read_text()
    point_size = app.font().pointSizeF()
    sizes = {float(v) for v in re.findall(r'font-size="([\d.]+)"', svg)}
    assert sizes and max(sizes) <= point_size * 96 / 72 + 1
    assert re.search(r'<svg width="(60\.\d+|59\.9\d+)mm" height="(40\.\d+|39\.9\d+)mm"', svg)


def test_marker_lines_print_as_wide_as_the_plot_lines(tmp_path):
    app = _app()
    import pyqtgraph as pg
    from src.gui.figure_capture import render_widget
    from tests.test_figure_capture_lines import line_runs
    pw = pg.PlotWidget()
    pw.resize(400, 300)
    pw.setBackground("w")
    for side in ("left", "bottom"):
        pw.getPlotItem().hideAxis(side)
    pw.setXRange(0, 1)
    pw.setYRange(0, 1)
    pw.addItem(pg.InfiniteLine(pos=0.5, angle=0, pen=pg.mkPen("k", width=1)))
    pw.show()
    app.processEvents()
    render_widget(pw, str(tmp_path / "m"), width_cm=4.0, formats=("png",), png_dpi=600, line_pt=1.0)
    pw.close()
    assert line_runs(tmp_path / "m.png") == [pytest.approx(600 / 72, abs=1.5)]


def window_with_a_plot():
    from PyQt6.QtWidgets import QMainWindow
    import pyqtgraph as pg
    win = QMainWindow()
    pw = pg.PlotWidget()
    pw.addLegend()
    pw.plot(np.arange(10), np.arange(10.0), pen=pg.mkPen("#4C9BE8"), name="Trace")
    pw.showGrid(x=True, y=True)
    win.setCentralWidget(pw)
    win.resize(800, 600)
    win.show()
    _app().processEvents()
    return win, pw


def test_the_dialog_shows_the_style_on_the_window_and_puts_it_back():
    from src.gui import figure_capture as fc
    win, pw = window_with_a_plot()
    left = pw.getAxis("left")
    pen = left.pen().color().name()
    dlg = fc.FigureCaptureDialog(win, hovered=pw.viewport())
    try:
        assert dlg.sp_width.value() == 16.0                              # as the thesis' figures are drawn
        dlg.cmb_style.setCurrentIndex(dlg.cmb_style.findData("thesis"))
        assert dlg.look is not None and left.pen().color().name() == "#52514e"
        assert dlg.sp_line.value() == 1.5                                # the thesis figures' lines
        assert dlg.options()["style"] == "thesis"
        dlg.cmb_style.setCurrentIndex(0)                                 # As on screen
        assert dlg.look is None and left.pen().color().name() == pen
        dlg.sp_line.setValue(0.75)
        dlg.cmb_style.setCurrentIndex(dlg.cmb_style.findData("thesis"))
        assert left.style["tickFont"].family() == "Latin Modern Roman" and dlg.sp_line.value() == 1.5
    finally:
        dlg.reject()
        fc._last.update(line_pt=fc.LINE_PT, style=None)
    assert dlg.look is None and left.pen().color().name() == pen and left.style["tickFont"] is None
    win.close()


def test_a_capture_in_the_thesis_style(tmp_path):
    from src.gui import figure_capture as fc
    win, pw = window_with_a_plot()
    left = pw.getAxis("left")
    before = (left.pen().color().name(), left.grid, pw.backgroundBrush().color().name())
    dlg = fc.FigureCaptureDialog(win, hovered=pw.viewport())
    dlg.cmb_style.setCurrentIndex(dlg.cmb_style.findData("thesis"))
    dlg.ed_dir.setText(str(tmp_path))
    dlg.ed_name.setText("thesis")
    dlg.done(1)
    opts = dlg.options()
    fc._last.update(line_pt=fc.LINE_PT, style=None)
    files = fc.capture(win, opts, notify=False)
    assert sorted(f.rsplit(".", 1)[1] for f in files) == ["pdf", "png", "svg"]
    svg = open(next(f for f in files if f.endswith(".svg"))).read()
    assert 'stroke="#e1e0d9"' in svg                                    # the grid
    assert "font-family=\"'Latin Modern Roman', 'LM Roman 10', 'CMU Serif', cmr10, serif\"" in svg   # the type
    assert (left.pen().color().name(), left.grid, pw.backgroundBrush().color().name()) == before
    win.close()


def test_in_a_thesis_figure_markers_are_as_fine_as_the_axes(tmp_path):
    """The traces at 1.5 pt (the thesis figures' lines), an event marker's line at 0.8 pt, its axes'."""
    app = _app()
    import pyqtgraph as pg
    from src.gui import figure_capture as fc, theme
    from tests.test_figure_capture_lines import line_runs
    pw = pg.PlotWidget()
    pw.resize(400, 300)
    for side in ("left", "bottom"):
        pw.getPlotItem().hideAxis(side)
    pw.setXRange(0, 1)
    pw.setYRange(0, 1)
    pw.plot([0, 1], [0.25, 0.25], pen=pg.mkPen(theme.tokens("light")["series"][0], width=1))
    pw.addItem(pg.InfiniteLine(pos=0.75, angle=0, pen=pg.mkPen("k", width=1)))
    pw.show()
    app.processEvents()
    opts = {"widget": pw, "size": None, "restore": None, "trim": False, "width_cm": 4.0, "formats": ("png",),
            "dpi": 600, "line_pt": 1.5, "style": "thesis", "folder": str(tmp_path), "name": "m"}
    files = fc.capture(pw, opts, notify=False)
    pw.close()
    marker, trace = line_runs(files[0])
    assert marker == pytest.approx(0.8 * 600 / 72, abs=1.5) and trace == pytest.approx(1.5 * 600 / 72, abs=1.5)


# --------------------------------------------------------------------------- the figure's words
def test_a_channel_named_as_a_figure_names_it():
    """The registry's names as a thesis figure's legend gives them (Josh's figures of 27 October 2026 had
    "Source.Filament RB Power [W]" and "Source VG1 Pressure [mB]" in their legends)."""
    from src.gui.thesis_style import display_unit, plain_label, split_name
    assert split_name("Source.Filament RB Power [W]") == ("Source.Filament RB Power", "W")
    assert split_name("Source VG1 Pressure [mB]") == ("Source VG1 Pressure", "mB")
    assert split_name("Trace") == ("Trace", "") and split_name("") == ("", "")
    assert display_unit("mB") == "mbar" and display_unit("degC") == "°C" and display_unit("mA") == "mA"
    for name, plain in (("Source.Filament RB Power", "Filament power"),
                        ("Source.Thermionic RB Power", "Thermionic power"),
                        ("Source.Thermionic RB Current", "Thermionic current"),
                        ("Source.Thermionic SP Actual Current", "Thermionic current set point"),
                        ("Source VG1 Pressure", "Source VG1 pressure"),
                        ("Magnet Current SP (Act)", "Magnet current set point"),
                        ("Bearing Temp", "Bearing temperature"),
                        ("Source.Cesium RB Temp", "Caesium temperature"),
                        ("Endstation RB Dose Count", "Endstation dose count"),
                        ("Faraday Beam Current", "Faraday beam current"),
                        ("Bombardment power", "Bombardment power"),          # (a name of Josh's own: as it is)
                        ("Cs oven heater", "Cs oven heater"), ("PSU Voltage", "PSU voltage"), ("", "")):
        assert plain_label(name) == plain, name


@pytest.fixture
def viewer_lanes():
    """Two lanes as the viewer builds them (viewer_lanes.py): clock time along the bottom, written under the
    lower lane only; each lane's log channels in a view of their own on a log axis on the right; the
    legend's names as the viewer writes them, with the unit in brackets."""
    app = _app()
    import time
    import pyqtgraph as pg
    from src.gui import theme
    from src.gui.data_viewer.viewer_widgets import LogAxisItem, TimeAxisItem
    dark = theme.tokens("dark")["series"]
    lw = pg.GraphicsLayoutWidget()
    lw.resize(900, 560)
    lw.setBackground("#161A1F")
    x = np.linspace(-660, 0, 2000)
    lanes = []
    for i in range(2):
        clock = TimeAxisItem(orientation="bottom")
        clock.set_offset(time.mktime((2026, 10, 27, 8, 56, 0, 0, 0, -1)))
        clock.hide_text = i == 0
        log_axis = LogAxisItem(orientation="right")
        p = lw.addPlot(row=i, col=0, axisItems={"bottom": clock, "right": log_axis})
        p.showGrid(x=True, y=True, alpha=0.35)
        right = pg.ViewBox()
        p.scene().addItem(right)
        log_axis.linkToView(right)
        right.setXLink(p)
        p.vb.sigResized.connect(lambda _, vb=right, plot=p: vb.setGeometry(plot.vb.sceneBoundingRect()))
        lanes.append((p, right, log_axis, p.addLegend(offset=(10, 10))))
    (top, right1, log1, legend1), (bottom, right2, log2, legend2) = lanes
    log1.setVisible(False)
    right1.setVisible(False)
    fil = pg.PlotDataItem(x, 150 * (1 - np.exp(-(x + 660) / 120)), pen=pg.mkPen(dark[0], width=1))
    thm = pg.PlotDataItem(x, 200 * (1 - np.exp(-(x + 600) / 200)), pen=pg.mkPen(dark[1], width=1))
    for c, name in ((fil, "Source.Filament RB Power [W]"), (thm, "Source.Thermionic RB Power [W]")):
        top.addItem(c)
        legend1.addItem(c, name=name)
    vg1 = pg.PlotDataItem(x, np.log10(2e-7 + 2e-7 * np.exp(-((x + 420) / 30) ** 2)), pen=pg.mkPen(dark[0], width=1))
    right2.addItem(vg1)
    legend2.addItem(vg1, name="Source VG1 Pressure [mB]")
    bottom.getAxis("left").setStyle(showValues=False)
    bottom.setXLink(top)
    top.setXRange(-660, 0, padding=0)
    lw.show()
    for _ in range(3):
        app.processEvents()
    yield dict(lw=lw, top=top, bottom=bottom, legends=(legend1, legend2), log=log2)
    lw.close()
    lw.deleteLater()
    app.processEvents()


def test_the_figures_texts_are_worked_out_from_the_names_and_units(viewer_lanes):
    from src.gui.thesis_style import figure_texts
    texts = [(f.kind, f.key, f.what, f.default) for f in figure_texts(viewer_lanes["lw"])]
    assert texts == [
        ("axis", "axis:left:Source.Filament RB Power [W]|Source.Thermionic RB Power [W]", "Lane 1, left axis",
         "Power (W)"),
        ("legend", "legend:Source.Filament RB Power [W]", "Source.Filament RB Power [W]", "Filament power"),
        ("legend", "legend:Source.Thermionic RB Power [W]", "Source.Thermionic RB Power [W]", "Thermionic power"),
        ("axis", "axis:right:Source VG1 Pressure [mB]", "Lane 2, right axis", "Pressure (mbar)"),
        ("legend", "legend:Source VG1 Pressure [mB]", "Source VG1 Pressure [mB]", "Source VG1 pressure"),
        ("axis", "axis:time", "Time axis", "Time of day")]
    viewer_lanes["top"].setXRange(-4 * 86400, 0, padding=0)             # days: the viewer writes the dates
    settle(viewer_lanes["lw"])
    assert figure_texts(viewer_lanes["lw"])[-1].default in ("Date", "Date and time")


def test_a_thesis_figure_has_the_plain_names_and_axis_titles_then_the_window_has_its_own(viewer_lanes):
    from src.gui.thesis_style import ThesisStyle
    lw, top, bottom = viewer_lanes["lw"], viewer_lanes["top"], viewer_lanes["bottom"]
    before = state(lw)
    look = ThesisStyle(lw, per_inch=150.0)
    try:
        settle(lw)
        names = [[label.text for _s, label in legend.items] for legend in viewer_legends(viewer_lanes)]
        assert names == [["Filament power", "Thermionic power"], ["Source VG1 pressure"]]
        titles = {(p is top, side): (ax.labelText, ax.label.isVisible())
                  for p in (top, bottom) for side, ax in ((s, p.getAxis(s)) for s in ("left", "right", "bottom"))}
        assert titles[(True, "left")] == ("Power (W)", True)
        assert titles[(False, "right")] == ("Pressure (mbar)", True)
        assert titles[(False, "bottom")] == ("Time of day", True)
        assert not titles[(True, "bottom")][1] and not titles[(False, "left")][1]     # (no numbers there)
        assert top.getAxis("left").label.font().family() == "Latin Modern Roman"
    finally:
        look.remove()
    settle(lw)
    assert state(lw) == before                                          # the viewer's names, no titles


def viewer_legends(lanes):
    return lanes["legends"]


def test_the_figures_texts_can_be_changed(viewer_lanes):
    """texts: a legend name of one's own, an axis title of one's own, no time axis title; a legend name left
    empty is the worked-out one."""
    from src.gui.thesis_style import ThesisStyle
    lw, top, bottom = viewer_lanes["lw"], viewer_lanes["top"], viewer_lanes["bottom"]
    texts = {"legend:Source.Thermionic RB Power [W]": "Bombardment power", "legend:Source VG1 Pressure [mB]": "",
             "axis:left:Source.Filament RB Power [W]|Source.Thermionic RB Power [W]": "Heating power (W)",
             "axis:time": "", "legend:a channel no longer shown": "x"}
    look = ThesisStyle(lw, per_inch=150.0, texts=texts)
    try:
        legend1, legend2 = viewer_legends(viewer_lanes)
        assert [label.text for _s, label in legend1.items] == ["Filament power", "Bombardment power"]
        assert [label.text for _s, label in legend2.items] == ["Source VG1 pressure"]
        assert top.getAxis("left").labelText == "Heating power (W)"
        assert not bottom.getAxis("bottom").label.isVisible()
    finally:
        look.remove()


def test_a_lane_of_mixed_units_keeps_them_in_its_legend():
    """No one unit for the axis: no title, and each name says its unit."""
    app = _app()
    import pyqtgraph as pg
    from src.gui.thesis_style import figure_texts
    pw = pg.PlotWidget()
    pw.addLegend()
    pw.plot([0, 1], [0, 1], pen="b", name="Source.Filament RB Power [W]")
    pw.plot([0, 1], [1, 0], pen="r", name="Source.Filament RB Current [A]")
    pw.plot([0, 1], [1, 1], pen="g", name="Counts")
    pw.show()
    app.processEvents()
    texts = {f.key: f.default for f in figure_texts(pw)}
    pw.close()
    assert texts == {"axis:left:Counts|Source.Filament RB Current [A]|Source.Filament RB Power [W]": "",
                     "legend:Source.Filament RB Power [W]": "Filament power (W)",
                     "legend:Source.Filament RB Current [A]": "Filament current (A)", "legend:Counts": "Counts"}


def test_an_axis_title_doesnt_scale_the_numbers():
    """pyqtgraph scales a titled axis' numbers below 1 into thousandths and writes "(x0.001)": not in a
    thesis figure, whose title says its unit."""
    app = _app()
    import pyqtgraph as pg
    from src.gui.thesis_style import ThesisStyle
    pw = pg.PlotWidget()
    pw.addLegend()
    pw.plot([0, 1], [0.0, 0.5], pen="b", name="Beam current [A]")
    pw.show()
    app.processEvents()
    left = pw.getAxis("left")
    look = ThesisStyle(pw, per_inch=150.0)
    try:
        settle(pw)
        assert left.labelText == "Current (A)" and left.autoSIPrefixScale == 1.0
        assert "x0.001" not in left.labelString() and "(m" not in left.labelString()
    finally:
        look.remove()
    assert left.autoSIPrefix and not left.label.isVisible()
    pw.close()


def test_the_pdf_is_all_lines_and_text_no_picture(tmp_path):
    """The plot's background was painted from Qt's cached picture of it: a white JPEG the size of the plot
    in the PDF (Josh's of 27 October 2026), a PNG in the SVG. Now a filled rectangle; the traces were lines
    already."""
    app = _app()
    import pyqtgraph as pg
    from src.gui.figure_capture import render_widget
    pw = pg.PlotWidget()
    pw.resize(500, 300)
    pw.setBackground("w")
    pw.plot(np.arange(200), np.sin(np.arange(200) / 10), pen="b")
    pw.show()
    app.processEvents()
    pw.grab()                                                            # (Qt keeps the background's picture)
    cache = pw.cacheMode()
    render_widget(pw, str(tmp_path / "v"), width_cm=8.0, formats=("pdf", "svg"))
    assert pw.cacheMode() == cache                                       # (the window's as it was)
    pw.close()
    svg = (tmp_path / "v.svg").read_text(encoding="utf-8")
    assert "<image" not in svg and "<path" in svg
    if shutil.which("pdfimages"):
        listed = subprocess.run(["pdfimages", "-list", str(tmp_path / "v.pdf")], capture_output=True,
                                text=True).stdout.splitlines()
        assert len(listed) == 2, listed                                  # (the heading only)


def _texts_dialog(monkeypatch, tmp_path, lanes):
    from PyQt6.QtWidgets import QMainWindow
    from src.gui import figure_capture as fc
    monkeypatch.setattr(fc, "TEXTS_FILE", str(tmp_path / "figure_text.json"))
    win = QMainWindow()
    win.setCentralWidget(lanes["lw"])
    win.resize(900, 600)
    win.show()
    _app().processEvents()
    dlg = fc.FigureCaptureDialog(win, hovered=lanes["lw"].viewport())
    return win, dlg


def test_the_dialog_shows_the_figures_texts_to_change_and_remembers_them(monkeypatch, tmp_path, viewer_lanes):
    from src.gui import figure_capture as fc
    win, dlg = _texts_dialog(monkeypatch, tmp_path, viewer_lanes)
    legend1, _legend2 = viewer_legends(viewer_lanes)
    try:
        assert dlg.box_text.isHidden()                                   # (as on screen: the window's own)
        dlg.cmb_style.setCurrentIndex(dlg.cmb_style.findData("thesis"))
        assert not dlg.box_text.isHidden()
        fields = {k: ed.text() for k, (_spec, ed) in dlg._rows.items()}
        assert fields["legend:Source.Filament RB Power [W]"] == "Filament power"
        assert fields["axis:time"] == "Time of day" and fields["axis:right:Source VG1 Pressure [mB]"] == "Pressure (mbar)"
        assert [label.text for _s, label in legend1.items] == ["Filament power", "Thermionic power"]   # (the window)
        _spec, ed = dlg._rows["legend:Source.Thermionic RB Power [W]"]
        ed.setText("Bombardment power")
        ed.textEdited.emit("Bombardment power")
        dlg._preview()                                                   # (as the pause after typing does)
        assert [label.text for _s, label in legend1.items] == ["Filament power", "Bombardment power"]
        opts = dlg.options()
        assert opts["texts"]["legend:Source.Thermionic RB Power [W]"] == "Bombardment power"
        assert fc.load_texts() == {"legend:Source.Thermionic RB Power [W]": "Bombardment power"}   # remembered
    finally:
        dlg.reject()
        fc._last.update(line_pt=fc.LINE_PT, style=None)
    assert [label.text for _s, label in legend1.items] == ["Source.Filament RB Power [W]",
                                                           "Source.Thermionic RB Power [W]"]
    # the next capture's dialog has it; Worked-out texts puts it back, and it is forgotten
    dlg = fc.FigureCaptureDialog(win, hovered=viewer_lanes["lw"].viewport())
    try:
        dlg.cmb_style.setCurrentIndex(dlg.cmb_style.findData("thesis"))
        assert dlg._rows["legend:Source.Thermionic RB Power [W]"][1].text() == "Bombardment power"
        dlg._reset_texts()
        assert dlg._rows["legend:Source.Thermionic RB Power [W]"][1].text() == "Thermionic power"
        dlg.options()
        assert fc.load_texts() == {}
    finally:
        dlg.reject()
        fc._last.update(line_pt=fc.LINE_PT, style=None)
    win.takeCentralWidget()
    win.close()


def test_the_dialog_says_how_big_the_text_will_be_in_the_thesis(monkeypatch, tmp_path, viewer_lanes):
    """Josh's figures of 27 October 2026 were drawn 22 cm and 20 cm wide: included at the text width, their
    numbers and legend printed at about 6 pt."""
    from src.gui import figure_capture as fc
    win, dlg = _texts_dialog(monkeypatch, tmp_path, viewer_lanes)
    try:
        dlg.cmb_style.setCurrentIndex(dlg.cmb_style.findData("thesis"))
        assert "8.9 pt" in dlg.lbl_thesis.text() and "8.0 pt" in dlg.lbl_thesis.text()
        assert "as in the thesis' other figures" in dlg.lbl_thesis.text()
        dlg.sp_width.setValue(22.0)
        text = dlg.lbl_thesis.text()
        assert "6.5 pt" in text and "5.8 pt" in text and "draw it 16 cm wide" in text
        assert not dlg._form.isRowVisible(dlg.lbl_text)                 # (only plots: the window's text no matter)
        dlg.cmb_style.setCurrentIndex(0)
        assert not dlg._form.isRowVisible(dlg.lbl_thesis) and dlg._form.isRowVisible(dlg.lbl_text)
    finally:
        dlg.reject()
        fc._last.update(line_pt=fc.LINE_PT, style=None)
    win.takeCentralWidget()
    win.close()
