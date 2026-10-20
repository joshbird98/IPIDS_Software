"""Peak finding on a mass spectrum: which peaks, their resolution (M/dM at half height), how much
of each leaks into its neighbours (the tail 0.5 amu either side), and what they might be."""
import math
from dataclasses import dataclass

import numpy as np

from src.gui.control.widgets.mass_scan.isotopes import match

LABEL_SPACING_AMU = 0.6         # closer peaks share one label on the plot
TAIL_OFFSET_AMU = 0.5


@dataclass
class Peak:
    index: int                  # into the data arrays
    amu: float
    current: float
    fwhm_amu: float
    tail_left: float
    tail_right: float
    match_html: str = ""
    kind: str = "none"          # what match() made of it: expected / other / none
    labelled: bool = False      # gets a label on the plot (the biggest of a cluster)

    @property
    def resolution(self) -> float:
        return self.amu / self.fwhm_amu if self.fwhm_amu > 0 else 0.0

    @property
    def crosstalk_ppm(self) -> float:
        worst = max(self.tail_left, self.tail_right)
        return worst / self.current * 1e6 if self.current > 0 else 0.0


def find(amu, current, sensitivity_pct: float, max_peaks: int, mass_db: dict) -> list:
    """The most prominent peaks (favouring ones near whole masses), sorted by mass. Needs scipy;
    fewer than 10 points give no peaks."""
    from scipy.signal import find_peaks, peak_widths
    x = np.asarray(amu, dtype=float)
    y = np.clip(np.asarray(current, dtype=float), 0, None)
    if len(x) < 10 or len(y) != len(x):
        return []
    prominence = max(1e-13, (np.max(y) - np.min(y)) * sensitivity_pct / 100.0)
    found, props = find_peaks(y, prominence=prominence)
    if not len(found):
        return []
    # Score: prominence, damped for peaks far from a whole mass
    scores = [p * math.exp(-0.5 * (abs(x[i] - round(x[i])) / 0.1) ** 2) for i, p in zip(found, props["prominences"])]
    chosen = np.sort(found[np.argsort(scores)[::-1][:max_peaks]])
    _w, _h, left, right = peak_widths(y, chosen, rel_height=0.5)
    positions = np.arange(len(x))
    fwhm = np.interp(right, positions, x) - np.interp(left, positions, x)

    order = np.argsort(x)               # np.interp needs rising x (a scan may run downwards)
    xs, ys = x[order], y[order]
    peaks = [Peak(int(i), float(x[i]), float(y[i]), float(abs(w)),
                  float(np.interp(x[i] - TAIL_OFFSET_AMU, xs, ys)), float(np.interp(x[i] + TAIL_OFFSET_AMU, xs, ys)))
             for i, w in zip(chosen, fwhm)]
    placed = []
    for peak in sorted(peaks, key=lambda p: p.current, reverse=True):
        peak.match_html, peak.kind = match(mass_db, peak.amu)
        if peak.match_html and not any(abs(peak.amu - px) < LABEL_SPACING_AMU for px in placed):
            placed.append(peak.amu)
            peak.labelled = True
    return peaks


def average_resolution(peaks) -> float:
    values = [p.resolution for p in peaks if p.resolution > 0]
    return sum(values) / len(values) if values else 0.0


def tail_text(ppm: float) -> str:
    if ppm < 1:
        return "< 1 ppm"
    if ppm > 500000:
        return "Overlapped"
    return f"{ppm:,.0f} ppm"


def tail_colour(ppm: float, green=(46, 125, 50), amber=(255, 143, 0), red=(198, 40, 40)) -> tuple:
    """Green up to 100 ppm, through amber at 1000 ppm, to red from 10 000 ppm (RGB tuples; the
    panel passes its theme's colours)."""
    green, amber, red = np.array(green), np.array(amber), np.array(red)
    v = math.log10(max(1.0, ppm))
    if v <= 2.0:
        c = green
    elif v < 3.0:
        c = green + (v - 2.0) * (amber - green)
    elif v < 4.0:
        c = amber + (v - 3.0) * (red - amber)
    else:
        c = red
    return int(c[0]), int(c[1]), int(c[2])
