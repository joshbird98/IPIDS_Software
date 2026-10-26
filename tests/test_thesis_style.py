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
                        "tickStrings" in it.__dict__, it.label.font().toString()))
        elif isinstance(it, LegendItem):
            out.append((it.opts["pen"].color().name(), it.opts["pen"].widthF(), it.opts["brush"].color().name(),
                        [(dict(label.opts), label.item.font().toString(), sample.minimumSize(), sample.maximumSize())
                         for sample, label in it.items]))
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
