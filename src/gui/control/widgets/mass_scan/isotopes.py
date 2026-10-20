"""Isotopes, and the molecular ions (up to MAX_MASS amu) peaks are matched against.

isotope_db.json (next to this file) is built by src/utils/build_element_database.py from the
mendeleev package; without it a small built-in set is used. Compounds of up to four isotopes of
the elements the operator expects (picked on the periodic table) and up to two of the others
are listed under their integer mass, plus common adducts (+H, +O, ...).
"""
import itertools
import json
import logging
import os

import numpy as np

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "isotope_db.json")
MAX_MASS = 300
MATCH_TOLERANCE = 0.25          # a peak this close to an integer mass gets a name
ADDUCTS = [("+H", 1, "H", 0.15), ("+H2", 2, "H<sub>2</sub>", 0.05), ("+O", 16, "O", 0.02),
           ("+O2", 32, "O<sub>2</sub>", 0.005), ("+N", 14, "N", 0.01)]

_FALLBACK = {
    "H": [(1.008, "1H", "<sup>1</sup>H", 0.999)],
    "He": [(4.002, "4He", "<sup>4</sup>He", 0.999)],
    "C": [(12.000, "12C", "<sup>12</sup>C", 0.989), (13.003, "13C", "<sup>13</sup>C", 0.011)],
    "N": [(14.003, "14N", "<sup>14</sup>N", 0.996), (15.000, "15N", "<sup>15</sup>N", 0.004)],
    "O": [(15.995, "16O", "<sup>16</sup>O", 0.997), (17.999, "18O", "<sup>18</sup>O", 0.002)],
    "F": [(18.998, "19F", "<sup>19</sup>F", 1.000)],
    "Ne": [(19.992, "20Ne", "<sup>20</sup>Ne", 0.904), (21.991, "22Ne", "<sup>22</sup>Ne", 0.092)],
    "Si": [(27.977, "28Si", "<sup>28</sup>Si", 0.922), (28.976, "29Si", "<sup>29</sup>Si", 0.046),
           (29.974, "30Si", "<sup>30</sup>Si", 0.031)],
    "P": [(30.974, "31P", "<sup>31</sup>P", 1.000)],
    "S": [(31.972, "32S", "<sup>32</sup>S", 0.949), (33.968, "34S", "<sup>34</sup>S", 0.042)],
    "Cl": [(34.969, "35Cl", "<sup>35</sup>Cl", 0.757), (36.966, "37Cl", "<sup>37</sup>Cl", 0.242)],
    "Ar": [(39.962, "40Ar", "<sup>40</sup>Ar", 0.996)],
    "Ge": [(69.924, "70Ge", "<sup>70</sup>Ge", 0.205), (71.922, "72Ge", "<sup>72</sup>Ge", 0.274),
           (72.923, "73Ge", "<sup>73</sup>Ge", 0.077), (73.921, "74Ge", "<sup>74</sup>Ge", 0.365),
           (75.921, "76Ge", "<sup>76</sup>Ge", 0.078)],
}

log = logging.getLogger("hmi.mass_scan")
_cache = None


def load_isotopes(path: str = DB_FILE) -> list:
    """[{mass (int), plain, html, sym, prob}], read once."""
    global _cache
    if path == DB_FILE and _cache is not None:
        return _cache
    try:
        with open(path, "r") as f:
            isotopes = json.load(f)
        log.info("Loaded %d isotopes from %s", len(isotopes), path)
    except (OSError, ValueError) as e:
        log.info("No isotope database (%s) - using the built-in minimal set", e)
        isotopes = [{"mass": int(round(mass)), "html": html, "plain": plain, "sym": sym, "prob": prob}
                    for sym, isos in _FALLBACK.items() for mass, plain, html, prob in isos]
    if path == DB_FILE:
        _cache = isotopes
    return isotopes


def _sort_key(name: str) -> str:
    """Carbon first, then hydrogen, then the rest (as chemists write formulae)."""
    if "C" in name and "Cl" not in name and "Ca" not in name:
        return "0" + name
    if "H" in name and "He" not in name:
        return "1" + name
    return "2" + name


def compound(combo) -> dict:
    """One molecule from a tuple of isotopes: {plain, html, mass, prob}."""
    counts = {}
    for iso in combo:
        entry = counts.setdefault(iso["plain"], {"count": 0, "html": iso["html"]})
        entry["count"] += 1
    html, plain = [], []
    for name in sorted(counts, key=_sort_key):
        n, base = counts[name]["count"], counts[name]["html"]
        html.append(f"{base}<sub>{n}</sub>" if n > 1 else base)
        plain.append(f"{name}{n}" if n > 1 else name)
    return {"plain": "".join(plain), "html": "".join(html), "mass": sum(iso["mass"] for iso in combo),
            "prob": float(np.prod([iso["prob"] for iso in combo]))}


def build_mass_db(isotopes: list, selected: set) -> dict:
    """{integer mass: [{plain, html, mass, is_selected, prob}]}."""
    db = {}

    def add(entry, int_mass, is_selected):
        db.setdefault(int_mass, {}).setdefault(entry["plain"], dict(entry, is_selected=is_selected))

    chosen = [e for e in isotopes if e["sym"] in selected]
    others = [e for e in isotopes if e["sym"] not in selected]
    for pool, max_atoms, is_selected in ((chosen, 4, True), (others, 2, False)):
        for n in range(1, max_atoms + 1):
            for combo in itertools.combinations_with_replacement(pool, n):
                entry = compound(combo)
                if entry["mass"] <= MAX_MASS:
                    add(entry, int(round(entry["mass"])), is_selected)

    additions = []
    for int_mass, entries in db.items():
        for entry in entries.values():
            for add_plain, add_mass, add_html, add_prob in ADDUCTS:
                if int_mass + add_mass <= MAX_MASS:
                    additions.append((int_mass + add_mass, {
                        "plain": entry["plain"] + add_plain, "html": entry["html"] + add_html,
                        "mass": entry["mass"] + add_mass, "prob": entry["prob"] * add_prob}, entry["is_selected"]))
    for int_mass, entry, is_selected in additions:
        add(entry, int_mass, is_selected)
    return {m: list(entries.values()) for m, entries in db.items()}


def match(mass_db: dict, amu: float):
    """(html, kind) naming a peak at `amu`: up to two of the expected elements' compounds
    ("expected"), else the likeliest other ("other"); ("", "none") if nothing is close enough.
    (The panel draws each kind in its theme colour.)"""
    nearest = int(round(amu))
    if abs(amu - nearest) > MATCH_TOLERANCE:
        return "", "none"
    candidates = sorted(mass_db.get(nearest, []), key=lambda c: c["prob"], reverse=True)
    expected = [c for c in candidates if c["is_selected"]]
    if expected:
        return "<br>".join(c["html"] for c in expected[:2]), "expected"
    if candidates:
        return candidates[0]["html"], "other"
    return "", "none"
