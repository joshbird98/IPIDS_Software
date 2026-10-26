"""F12 figure capture's Plot lines: the plot lines drawn at a width in points on the page (pyqtgraph's
pens are cosmetic, so as they are a 1 px trace prints 1/1200 inch wide in the PDF), whatever pyqtgraph
does to the curves meanwhile, in the window's pixels rather than the plot's coordinates (which Qt's SVG
writer rounds to 6 significant figures), shown on the window while the dialog is open - and the plot
left as it was afterwards."""
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET

import numpy as np
import pytest


def _app():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def plot():
    app = _app()
    import pyqtgraph as pg
    pw = pg.PlotWidget()
    pw.resize(400, 300)
    pw.setBackground("w")
    thin = pw.plot([0, 1], [0.25, 0.25], pen=pg.mkPen("k", width=1))
    thick = pw.plot([0, 1], [0.75, 0.75], pen=pg.mkPen("k", width=2))
    for side in ("left", "bottom"):
        pw.getPlotItem().hideAxis(side)
    pw.show()
    app.processEvents()
    yield pw, thin, thick
    pw.close()
    pw.deleteLater()
    app.processEvents()


def gray(path):
    from PyQt6.QtGui import QImage
    img = QImage(str(path)).convertToFormat(QImage.Format.Format_Grayscale8)
    ptr = img.constBits()
    ptr.setsize(img.sizeInBytes())
    return np.frombuffer(ptr, np.uint8).reshape(img.height(), img.bytesPerLine())[:, :img.width()]


def runs(column):
    """Lengths of the dark runs down a column of pixels, top first."""
    dark = np.nonzero(column < 128)[0]
    return [len(r) for r in np.split(dark, np.nonzero(np.diff(dark) > 1)[0] + 1)] if dark.size else []


def line_runs(path):
    """Thicknesses (px) of the dark horizontal lines down the middle of an image, top first."""
    a = gray(path)
    return runs(a[:, a.shape[1] // 2])


def pen_of(item):
    pen = item.curve.opts["pen"]
    return pen.widthF(), pen.isCosmetic(), pen.color().name()


def left_as_it_was(*items):
    return all("paint" not in it.curve.__dict__ for it in items)


def test_plot_lines_print_at_the_width_asked_for_and_the_plot_is_left_as_it_was(plot, tmp_path):
    from src.gui.figure_capture import render_widget
    pw, thin, thick = plot
    pens = [pen_of(c) for c in (thin, thick)]
    render_widget(pw, str(tmp_path / "as_is"), width_cm=4.0, formats=("png",), png_dpi=600)
    assert line_runs(tmp_path / "as_is.png") == [2, 1]               # device pixels: hairlines at 600 dpi
    stats = {}
    render_widget(pw, str(tmp_path / "one_pt"), width_cm=4.0, formats=("png", "pdf"), png_dpi=600, line_pt=1.0,
                  stats=stats)
    assert stats["plot_lines"] == 2
    top, bottom = line_runs(tmp_path / "one_pt.png")                  # (the 2 px trace is on top)
    assert bottom == pytest.approx(600 / 72, abs=1.5) and top == pytest.approx(2 * 600 / 72, abs=1.5)
    assert [pen_of(c) for c in (thin, thick)] == pens and left_as_it_was(thin, thick)
    if shutil.which("pdftoppm"):
        subprocess.run(["pdftoppm", "-r", "1200", "-png", "-singlefile", str(tmp_path / "one_pt.pdf"),
                        str(tmp_path / "pdf")], check=True)
        top, bottom = line_runs(tmp_path / "pdf.png")
        assert bottom == pytest.approx(1200 / 72, abs=2) and top == pytest.approx(2 * 1200 / 72, abs=2)


def test_pyqtgraph_putting_the_curves_own_pens_back_mid_paint_does_not_undo_the_width(plot, tmp_path):
    """A plot that re-ranges as it paints has each PlotDataItem set its curve's pen (and data) again."""
    from src.gui.figure_capture import render_widget
    pw, thin, thick = plot

    def reset():
        thin.updateItems()
        thick.updateItems()
    pw.scene().sigPrepareForPaint.connect(reset)
    try:
        render_widget(pw, str(tmp_path / "reset"), width_cm=4.0, formats=("png",), png_dpi=600, line_pt=1.0)
    finally:
        pw.scene().sigPrepareForPaint.disconnect(reset)
    top, bottom = line_runs(tmp_path / "reset.png")
    assert bottom == pytest.approx(600 / 72, abs=1.5) and top == pytest.approx(2 * 600 / 72, abs=1.5)


def test_with_the_mouse_over_a_plot_the_capture_is_the_plot_and_its_lines_widen(plot, tmp_path):
    """F12 offers the widget under the mouse first: over a plot, that is the view's viewport."""
    from PyQt6.QtWidgets import QMainWindow
    from src.gui import figure_capture as fc
    pw, thin, thick = plot
    win = QMainWindow()
    win.setCentralWidget(pw)
    win.resize(500, 400)
    win.show()
    _app().processEvents()
    try:
        targets = fc.capture_targets(win, pw.viewport())
        assert targets[0] is pw
        stats = {}
        fc.render_widget(pw.viewport(), str(tmp_path / "vp"), width_cm=4.0, formats=("png",), line_pt=1.0,
                         stats=stats)
        assert stats["plot_lines"] == 2                               # (even given the viewport itself)
    finally:
        pw.setParent(None)
        win.close()


def svg_lines(path):
    """(the stroke's attributes, its points) of each stroked path in an SVG, longest first."""
    ns = "{http://www.w3.org/2000/svg}"
    out = []
    for g in ET.parse(path).iter(ns + "g"):
        for el in g:
            if el.tag == ns + "path" and el.get("d") and g.get("stroke", "none") != "none":
                pts = [(float(a), float(b)) for a, b in re.findall(r"[ML](-?[\d.e+-]+),(-?[\d.e+-]+)", el.get("d"))]
                out.append(({**g.attrib, **el.attrib}, pts))
    return sorted(out, key=lambda t: -len(t[1]))


def test_the_svg_keeps_every_sample_of_a_trace_far_from_zero(tmp_path):
    """The viewer's x is seconds from when it started: 200 samples at 10 Hz, 3.5 days on, came out in
    1 s steps (10 samples to an x) - the SVG had the plot's coordinates to 6 significant figures."""
    app = _app()
    import pyqtgraph as pg
    from src.gui.figure_capture import render_widget
    pw = pg.PlotWidget()
    pw.resize(400, 300)
    x = 3e5 + np.arange(200) * 0.1
    pw.plot(x, np.sin(x), pen=pg.mkPen("k", width=1))
    for side in ("left", "bottom"):
        pw.getPlotItem().hideAxis(side)
    pw.show()
    app.processEvents()
    try:
        for line_pt in (None, 1.5):
            base = str(tmp_path / f"far_{line_pt}")
            render_widget(pw, base, width_cm=4.0, formats=("svg",), line_pt=line_pt)
            attrs, pts = svg_lines(base + ".svg")[0]
            xs = np.array([p[0] for p in pts])
            assert len(pts) == 200 and np.all(np.diff(xs) > 1.0)          # every sample its own x, in order
            assert 0 <= xs.min() and xs.max() <= 400                       # in the figure's own pixels
            if line_pt is None:                                            # as on the screen: a hairline
                assert attrs["vector-effect"] == "non-scaling-stroke" and float(attrs["stroke-width"]) == 1
            else:                                                          # a width any SVG reader agrees on
                assert attrs["vector-effect"] == "none"
                assert float(attrs["stroke-width"]) == pytest.approx(1.5 * (400 / (4.0 / 2.54)) / 72, rel=1e-3)
                assert attrs["stroke-linecap"] == "round" and attrs["stroke-linejoin"] == "round"
    finally:
        pw.close()
        pw.deleteLater()
        app.processEvents()


def test_the_viewers_own_screen_svg_keeps_every_sample_too(tmp_path):
    """Export > screen SVG renders the plots' scene straight into Qt's SVG writer."""
    import types
    app = _app()
    import pyqtgraph as pg
    from src.gui.data_viewer.viewer_export import ExportMixin
    lw = pg.GraphicsLayoutWidget()
    lw.resize(400, 300)
    x = 3e5 + np.arange(200) * 0.1
    lw.addPlot().plot(x, np.sin(x), pen=pg.mkPen("k", width=1))
    lw.show()
    app.processEvents()
    try:
        (tmp_path / "screen.svg").write_bytes(ExportMixin._render_screen_svg(types.SimpleNamespace(layout_widget=lw)))
        attrs, pts = svg_lines(tmp_path / "screen.svg")[0]
        assert len(pts) == 200 and np.all(np.diff([p[0] for p in pts]) > 1.0)
        assert all("paint" not in it.__dict__ for it in lw.scene().items())
    finally:
        lw.close()
        lw.deleteLater()
        app.processEvents()


def test_lines_are_cut_where_they_cross_the_plot_edge():
    from src.gui.figure_capture import clip_lines
    xy = np.array([(-5, 5), (5, 5), (15, 5), (15, 20), (5, 8), (2, 2)], float)
    out = clip_lines(xy, 0, 0, 10, 10)
    rows = [None if np.isnan(r[0]) else tuple(float(v) for v in np.round(r, 3)) for r in out]
    assert rows == [None, (0, 5), (5, 5), (10, 5),                      # in at the left edge, out at the right
                    None, (6.667, 10), (5, 8), (2, 2)]                  # back in at the bottom
    assert clip_lines(np.array([(20, 20), (30, 30)], float), 0, 0, 10, 10).shape == (0, 2)


def test_the_svg_has_nothing_outside_the_plot():
    """Qt's SVG writer leaves out the plot's clip, so (thick) lines ran over the axes."""
    app = _app()
    import pyqtgraph as pg
    import tempfile
    from src.gui.figure_capture import render_widget
    pw = pg.PlotWidget()
    pw.resize(400, 300)
    x = np.linspace(0, 10, 500)
    pw.plot(x, np.sin(x), pen=pg.mkPen("k", width=1))
    pw.setXRange(3, 6, padding=0)
    pw.setYRange(-0.5, 0.5, padding=0)
    pw.show()
    app.processEvents()
    try:
        box = pw.mapFromScene(pw.getPlotItem().vb.sceneBoundingRect()).boundingRect()
        with tempfile.TemporaryDirectory() as d:
            render_widget(pw, d + "/clip", width_cm=4.0, formats=("svg",), line_pt=1.0)
            attrs, pts = svg_lines(d + "/clip.svg")[0]
        xs, ys = np.array(pts).T
        inside = np.count_nonzero((x >= 3) & (x <= 6) & (np.abs(np.sin(x)) <= 0.5))
        assert attrs["d"].count("M") == 2 and inside <= len(pts) <= inside + 4   # two pieces, cut at the edge
        assert box.left() - 0.5 <= xs.min() and xs.max() <= box.right() + 1.5
        assert box.top() - 0.5 <= ys.min() and ys.max() <= box.bottom() + 1.5
    finally:
        pw.close()
        pw.deleteLater()
        app.processEvents()


def test_the_legend_samples_match_the_lines(tmp_path):
    app = _app()
    import pyqtgraph as pg
    from PyQt6.QtCore import QPointF
    from src.gui.figure_capture import render_widget
    pw = pg.PlotWidget()
    pw.resize(400, 300)
    pw.setBackground("w")
    pw.addLegend()
    pw.plot([0, 1], [0, 1], pen=pg.mkPen("k", width=1), name="trace")
    pw.show()
    app.processEvents()
    try:
        sample = pw.getPlotItem().legend.items[0][0]
        at = pw.mapFromScene(sample.mapToScene(QPointF(10, 11)))
        k = 600 * (4.0 / 2.54) / 400                                       # device pixels per window pixel
        widths = []
        for line_pt in (None, 1.0):
            render_widget(pw, str(tmp_path / f"legend_{line_pt}"), width_cm=4.0, formats=("png",), png_dpi=600,
                          line_pt=line_pt)
            col = gray(tmp_path / f"legend_{line_pt}.png")[:, round(at.x() * k)]
            y = round(at.y() * k)
            widths.append(max(runs(col[y - 30:y + 30]) or [0]))
        assert widths[0] <= 2 and widths[1] == pytest.approx(600 / 72, abs=1.5)
        assert "paint" not in sample.__dict__
    finally:
        pw.close()
        pw.deleteLater()
        app.processEvents()


def test_the_dialog_shows_the_lines_on_the_window_as_wide_as_they_will_print_then_puts_them_back():
    from PyQt6.QtWidgets import QGraphicsView, QMainWindow
    app = _app()
    import pyqtgraph as pg
    from src.gui import figure_capture as fc
    win = QMainWindow()
    pw = pg.PlotWidget()
    win.setCentralWidget(pw)
    win.resize(800, 600)
    curve = pw.plot([0, 1], [0, 1], pen=pg.mkPen("k", width=1)).curve
    win.show()
    app.processEvents()
    mode = pw.viewportUpdateMode()
    dlg = fc.FigureCaptureDialog(win, hovered=pw.viewport())
    try:
        assert dlg.target() is pw
        dlg.sp_line.setValue(1.0)
        per_inch = dlg._w_px / (dlg.sp_width.value() / 2.54)
        assert "paint" in curve.__dict__ and dlg.lines.width_px == pytest.approx(per_inch / 72)
        assert pw.viewportUpdateMode() == QGraphicsView.ViewportUpdateMode.FullViewportUpdate
        width = dlg.sp_width.value()
        dlg.sp_width.setValue(width / 2)                                    # half as wide on the page: twice
        assert dlg.lines.width_px == pytest.approx(2 * per_inch / 72, rel=0.02)   # the window's pixels
        dlg.sp_line.setValue(0.0)                                           # "Unchanged (hairline)"
        assert "paint" not in curve.__dict__ and pw.viewportUpdateMode() == mode
        dlg.sp_line.setValue(2.0)
        assert "paint" in curve.__dict__
    finally:
        dlg.reject()
        fc._last["line_pt"] = fc.LINE_PT
    assert "paint" not in curve.__dict__ and pw.viewportUpdateMode() == mode
    win.close()
    app.processEvents()


def test_the_dialog_offers_the_line_width_and_remembers_it():
    from PyQt6.QtWidgets import QMainWindow, QWidget
    app = _app()
    from src.gui import figure_capture as fc
    win = QMainWindow()
    win.setCentralWidget(QWidget())
    win.resize(800, 600)
    win.show()
    app.processEvents()
    dlg = fc.FigureCaptureDialog(win)
    assert dlg.sp_line.value() == fc._last["line_pt"]
    dlg.sp_line.setValue(1.5)
    assert dlg.options()["line_pt"] == 1.5 and fc._last["line_pt"] == 1.5
    dlg.sp_line.setValue(0.0)                                         # "Unchanged (hairline)"
    assert dlg.sp_line.text() == "Unchanged (hairline)" and dlg.options()["line_pt"] is None
    fc._last["line_pt"] = fc.LINE_PT
    dlg.reject()
    win.close()
    app.processEvents()
