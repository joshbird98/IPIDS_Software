"""Analyser magnet mass calibration, as used by the Mass Scan panel:

    AMU = k * (I + I_off)^2 / E        I = magnet current (A), E = beam energy (eV)

k and I_off come from a fit to known peaks in a mass scan (Ctrl+click the peaks, then
"Calculate Fit"); E is target + extraction voltage for singly charged ions.
"""
import math


def mass_for_current(amps: float, energy_ev: float, k: float, i_off: float) -> float:
    return k * (amps + i_off) ** 2 / energy_ev


def current_for_mass(amu: float, energy_ev: float, k: float, i_off: float) -> float:
    """Magnet current that puts `amu` through the analyser (the inverse of mass_for_current)."""
    if amu <= 0 or energy_ev <= 0 or k <= 0:
        raise ValueError(f"need a positive mass, beam energy and k (got {amu}, {energy_ev}, {k})")
    return math.sqrt(amu * energy_ev / k) - i_off
