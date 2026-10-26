"""F12 figure capture's Plot lines: the plot lines drawn at a width in points on the page (pyqtgraph's
pens are cosmetic, so as they are a 1 px trace prints 1/1200 inch wide in the PDF), and the pens put
back as they were afterwards."""
import shutil
import subprocess

import numpy as np
import pytest


@pytest.fixture
def plot():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
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


def line_runs(path):
    """Thicknesses (px) of the dark horizontal lines down the middle of an image, top first."""
    from PyQt6.QtGui import QImage
    img = QImage(str(path)).convertToFormat(QImage.Format.Format_Grayscale8)
    ptr = img.constBits()
    ptr.setsize(img.sizeInBytes())
    a = np.frombuffer(ptr, np.uint8).reshape(img.height(), img.bytesPerLine())[:, :img.width()]
    dark = np.nonzero(a[:, img.width() // 2] < 128)[0]
    return [len(r) for r in np.split(dark, np.nonzero(np.diff(dark) > 1)[0] + 1)] if dark.size else []


def test_plot_lines_print_at_the_width_asked_for_and_the_pens_come_back(plot, tmp_path):
    from src.gui.figure_capture import render_widget
    pw, thin, thick = plot
    pens = [pen_of(c) for c in (thin, thick)]
    render_widget(pw, str(tmp_path / "as_is"), width_cm=4.0, formats=("png",), png_dpi=600)
    assert line_runs(tmp_path / "as_is.png") == [2, 1]               # device pixels: hairlines at 600 dpi
    render_widget(pw, str(tmp_path / "one_pt"), width_cm=4.0, formats=("png", "pdf"), png_dpi=600, line_pt=1.0)
    top, bottom = line_runs(tmp_path / "one_pt.png")                  # (the 2 px trace is on top)
    assert bottom == pytest.approx(600 / 72, abs=1.5) and top == pytest.approx(2 * 600 / 72, abs=1.5)
    assert [pen_of(c) for c in (thin, thick)] == pens             # the pens as they were
    if shutil.which("pdftoppm"):
        subprocess.run(["pdftoppm", "-r", "1200", "-png", "-singlefile", str(tmp_path / "one_pt.pdf"),
                        str(tmp_path / "pdf")], check=True)
        top, bottom = line_runs(tmp_path / "pdf.png")
        assert bottom == pytest.approx(1200 / 72, abs=2) and top == pytest.approx(2 * 1200 / 72, abs=2)


def pen_of(item):
    pen = item.curve.opts["pen"]
    return pen.widthF(), pen.isCosmetic(), pen.color().name()


def test_the_dialog_offers_the_line_width_and_remembers_it():
    from PyQt6.QtWidgets import QApplication, QMainWindow, QWidget
    app = QApplication.instance() or QApplication([])
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
