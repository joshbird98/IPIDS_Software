"""Mass scan trace files (CSV, as the sequencer writes them: src/sequencer/mass_scan.py), the
mass calibration stored in them, and the zero of a trace's beam current."""
import csv
import math
from dataclasses import dataclass, field

import numpy as np


@dataclass
class Trace:
    amps: list = field(default_factory=list)          # measured magnet current
    beam: list = field(default_factory=list)          # beam current
    energy_kev: list = field(default_factory=list)    # target + extraction (0 if not recorded)
    has_energy: bool = False
    k: float = None                                   # calibration saved with the trace, if any
    i_off: float = None


def load_trace(path: str) -> Trace:
    """Raises OSError / ValueError / KeyError for an unreadable file."""
    trace = Trace()
    with open(path, "r", newline="") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        if "Measured_Magnet_A" not in fields or "Measured_Beam_A" not in fields:
            raise ValueError("not a mass scan trace (no Measured_Magnet_A / Measured_Beam_A columns)")
        trace.has_energy = "Target_kV" in fields and "Extraction_kV" in fields
        for i, row in enumerate(reader):
            if i == 0 and row.get("Calib_k") and row.get("Calib_I_off"):
                try:
                    trace.k, trace.i_off = float(row["Calib_k"]), float(row["Calib_I_off"])
                except ValueError:
                    pass
            trace.amps.append(float(row["Measured_Magnet_A"]))
            trace.beam.append(float(row["Measured_Beam_A"]))
            if trace.has_energy:
                trace.energy_kev.append(float(row.get("Target_kV") or 0.0) + float(row.get("Extraction_kV") or 0.0))
            else:
                trace.energy_kev.append(0.0)
    return trace


def save_calibration(path: str, k: float, i_off: float):
    """Write k and I_off into every row of a trace (columns Calib_k, Calib_I_off)."""
    with open(path, "r", newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return
    header = rows[0]
    if "Calib_k" not in header:
        header.extend(["Calib_k", "Calib_I_off"])
    k_idx, i_idx = header.index("Calib_k"), header.index("Calib_I_off")
    for row in rows[1:]:
        while len(row) <= max(k_idx, i_idx):
            row.append("")
        row[k_idx], row[i_idx] = f"{k:.4f}", f"{i_off:.4f}"
    with open(path, "w", newline="") as f:
        csv.writer(f).writerows(rows)


def estimate_zero(beam, min_points: int = 8):
    """What the cup reads with no beam on it (the offset of the current measurement), from a
    trace's beam currents: the median of its quietest readings. They start as the lowest fifth of
    the readings and settle as every reading within three spreads (1.48 x the median absolute
    deviation, at least 0.5 pA) of their median. None if fewer than min_points readings remain.
    Assumes some of the trace saw no beam: on a trace that is all peak it returns the lowest of
    the peak's flanks, which the panel refuses as too far from zero."""
    y = np.asarray(beam, dtype=float)
    y = y[np.isfinite(y)]
    if y.size < min_points:
        return None
    quiet = np.sort(y)[:max(min_points, y.size // 5)]
    zero = float(np.median(quiet))
    for _ in range(20):
        spread = max(1.4826 * float(np.median(np.abs(quiet - zero))), 0.5e-12)
        quiet = y[np.abs(y - zero) <= 3.0 * spread]
        if quiet.size < min_points:
            return None
        settled = float(np.median(quiet))
        if settled == zero:
            break
        zero = settled
    return zero


def fit_calibration(points, i_off_now: float):
    """Fit AMU = k (I + I_off)^2 / E to known peaks [{amps, energy_ev, true_amu}].
    One peak: k only (I_off kept). Two or more: sqrt(AMU * E) = A I + B, so k = A^2, I_off = B/A.
    Returns (k, i_off, r_squared or None). Raises ValueError if the peaks don't make sense."""
    if not points:
        raise ValueError("no calibration peaks")
    if len(points) == 1:
        p = points[0]
        if p["amps"] + i_off_now == 0:
            raise ValueError("the peak is at I = -I_off")
        return p["true_amu"] * p["energy_ev"] / (p["amps"] + i_off_now) ** 2, i_off_now, None
    x = np.array([p["amps"] for p in points], dtype=float)
    y = np.array([math.sqrt(p["true_amu"] * p["energy_ev"]) for p in points])
    a, b = np.polyfit(x, y, 1)
    if a <= 0:
        raise ValueError("negative slope - check the peaks and the masses given")
    residual = np.sum((y - (a * x + b)) ** 2)
    total = np.sum((y - np.mean(y)) ** 2)
    return float(a ** 2), float(b / a), float(1.0 - residual / total) if total > 0 else 1.0
