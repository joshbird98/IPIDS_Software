"""The Mass Scan plot's current axis: linear, or a symmetric log that keeps negative currents."""
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
