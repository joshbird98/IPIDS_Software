"""Mass Scan panel (src/gui/control/widgets/mass_scan/widget.py) and its parts:

    ui.py              layout
    axis.py            linear / symmetric-log current axis; the top x axis in the other unit
    thesis_figure.py   Export figure: the plot as framed, as thesis figures (copied as make_figure.py)
    isotopes.py        isotopes and the molecular ions peaks are matched against
    peaks.py           peak finding, resolution and tails
    traces.py          trace files and the mass calibration fit
    periodic_table.py  picking the expected elements
The scan itself runs in the sequencer service (src/sequencer/mass_scan.py).
"""
from src.gui.control.widgets.mass_scan.widget import PrecisionMassScannerWidget

__all__ = ["PrecisionMassScannerWidget"]
