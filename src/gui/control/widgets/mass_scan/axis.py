"""The Mass Scan plot's axes: the current axis, linear or a symmetric log that keeps negative
currents; and the second x axis, which labels the plot's positions in the other unit (mass or
magnet current)."""
import math

import numpy as np
import pyqtgraph as pg

# --- Symmetric log scale for the beam current ---
# The log view uses sign(I) * log10(1 + |I| / LINTHRESH): logarithmic for |I| well above
# LINTHRESH, linear through zero, so negative currents (the cup charging the other way,
# offsets) get their own logarithmic region below zero instead of being mirrored.
SYMLOG_LINTHRESH = 1e-12   # 1 pA, about the SMU's floor


def symlog(y):
    y = np.asarray(y, dtype=float)
    return np.sign(y) * np.log10(1.0 + np.abs(y) / SYMLOG_LINTHRESH)


def symlog_inv(t):
    t = np.asarray(t, dtype=float)
    return np.sign(t) * (10.0 ** np.abs(t) - 1.0) * SYMLOG_LINTHRESH


def format_current(val: float) -> str:
    # To 3 significant figures first: a tick a hair under a decade (the symlog round trip gives
    # 9.99999e-7 A for 1 uA) reads "1 µA", not "1e+03 nA"
    val = float(f"{val:.3g}")
    abs_val = abs(val)
    if abs_val == 0:
        return "0 A"
    if abs_val >= 1:
        return f"{val:.2f} A"
    if abs_val >= 1e-3:
        return f"{val * 1e3:.3g} mA"
    if abs_val >= 1e-6:
        return f"{val * 1e6:.3g} µA"
    if abs_val >= 1e-9:
        return f"{val * 1e9:.3g} nA"
    return f"{val * 1e12:.3g} pA"


# --- Custom Axis for Smart Current Formatting ---
class SmartCurrentAxis(pg.AxisItem):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.enableAutoSIPrefix(False)
        self.symlog = False   # values are symlog(current) - see symlog()

    def tickValues(self, minVal, maxVal, size):
        if not self.symlog:
            return super().tickValues(minVal, maxVal, size)
        # Decades of current (1 pA, 10 pA, ...) both sides of zero, and zero
        lo, hi = min(minVal, maxVal), max(minVal, maxVal)
        decades = [(float(symlog(sign * 10.0 ** e)), e) for e in range(-12, 1) for sign in (1.0, -1.0)]
        decades = [(t, e) for t, e in decades if lo <= t <= hi]
        step = 2 if len(decades) > 12 else 1          # very zoomed out: every other decade
        major = [t for t, e in decades if e % step == 0] + ([0.0] if lo <= 0.0 <= hi else [])
        minor = []
        if len(decades) <= 4:                         # zoomed into a decade or two: 2x .. 9x as well
            for e in range(-12, 1):
                for m in range(2, 10):
                    for sign in (1.0, -1.0):
                        t = float(symlog(sign * m * 10.0 ** e))
                        if lo <= t <= hi:
                            minor.append(t)
        return [(1.0, sorted(major)), (0.1, sorted(minor))]

    def tickStrings(self, values, scale, spacing):
        strings = []
        for v in values:
            try:
                val = float(symlog_inv(v)) if self.symlog else float(v)
                if self.symlog and spacing < 1.0:
                    strings.append("")            # minor ticks unlabelled
                    continue
                strings.append(format_current(val))
            except (OverflowError, ValueError):
                strings.append("")
        return strings


# --- The second x axis: the same positions in the other unit ---
def decimals_for(spacing: float) -> int:
    """Decimal places for tick labels `spacing` apart (2 -> 0, 0.5 -> 1, 0.02 -> 2)."""
    if not spacing or not math.isfinite(spacing) or spacing <= 0:
        return 0
    return max(0, -math.floor(math.log10(spacing) + 1e-9))


class OtherUnitAxis(pg.AxisItem):
    """An x axis (the top one) labelling the plot's positions in another unit: the plot against
    mass with the magnet current on top, or the other way round. Mass goes with the square of the
    current, so these ticks are evenly spaced in their own unit, not on the screen.

    to_other(x): a plot position in this axis's unit (clamped to its domain at the ends);
    to_plot(v): the plot position of a value in this axis's unit (ValueError outside the domain:
    no tick there). Either None: no ticks."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.enableAutoSIPrefix(False)
        self.to_other = self.to_plot = None

    def set_conversion(self, to_other, to_plot):
        self.to_other, self.to_plot = to_other, to_plot
        self.refresh()

    def refresh(self):
        """Redraw (the calibration or the beam energy behind the conversion changed)."""
        self.picture = None
        self.update()

    def tickValues(self, minVal, maxVal, size):
        if self.to_other is None or self.to_plot is None:
            return []
        lo, hi = sorted((minVal, maxVal))
        try:
            a, b = sorted((float(self.to_other(lo)), float(self.to_other(hi))))
        except (ValueError, OverflowError, ZeroDivisionError):
            return []
        if not (math.isfinite(a) and math.isfinite(b)) or b <= a:
            return []
        levels = []
        for spacing, values in super().tickValues(a, b, size):
            positions = []
            for v in values:
                try:
                    x = float(self.to_plot(v))
                except (ValueError, OverflowError, ZeroDivisionError):
                    continue
                if math.isfinite(x) and lo <= x <= hi:
                    positions.append(x)
            levels.append((spacing, positions))      # (the spacing in this axis's own unit)
        return levels

    def tickStrings(self, values, scale, spacing):
        places = decimals_for(spacing)
        strings = []
        for x in values:
            try:
                strings.append(f"{float(self.to_other(x)):.{places}f}")
            except (TypeError, ValueError, OverflowError, ZeroDivisionError):
                strings.append("")
        return strings
