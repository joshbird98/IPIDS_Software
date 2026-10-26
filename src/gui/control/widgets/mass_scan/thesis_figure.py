"""
Thesis figures from a SINDIE mass scan (the Mass Scan panel's Export figure button).

This file is copied into every figure folder as make_figure.py, so the figures can be drawn again,
and restyled, long after the export, on any machine with numpy and matplotlib:

    python make_figure.py                            # every variant, as PDF, SVG and PNG
    python make_figure.py --only full                # one variant
    python make_figure.py --xlim 26 31 --ylim 1e-12 3e-7
    python make_figure.py --sans --font-size 8 --mass-label "Mass (u)"
    python make_figure.py --shift-pA 6.5             # another shift of the beam current

The folder holds what was on the plot when it was exported: data.csv (each point's magnet current,
mass, beam current and beam energy, within the visible range), peaks.csv (the peaks found, if peak
detection was on) and figure.json (how the plot was framed, the calibration, where the data came
from). The beam currents in data.csv and peaks.csv are the readings as measured; the figures add
figure.json's current_shift_A to them (the panel's Shift, which takes out the offset of the
current measurement). The variants (figure.json lists the ones made):

    full                  150 mm wide (\\textwidth), framed as on the screen
    half                  72 mm wide (0.48\\textwidth), for a subfigure
    full_log, full_linear the other beam current scale
    full_by_current, full_by_mass   against the other x quantity, the same part of the scan
    full_unlabelled       without the peak labels

The page of each PDF is the figure itself, so \\includegraphics[width=\\textwidth] (or \\linewidth
in a 0.48\\textwidth subfigure) prints it at 1:1 and the 9 pt text stays 9 pt. PNGs are 2700 px wide
for full width and 1600 px for half; SVGs keep their text as text, for Inkscape.
"""
import argparse
import csv
import json
import math
import os
import re

import numpy as np

LINE_COLOUR = "#0072B2"          # Okabe-Ito blue, as the data viewer's figures
LINTHRESH = 1e-12                # the symmetric log's linear region (as on the panel): 1 pA
SIZES = {"full": (150.0, 80.0, 2700), "half": (72.0, 58.0, 1600)}       # width mm, height mm, PNG px
X_LABELS = {"amu": "Mass (amu)", "amps": "Magnet current (A)"}
FORMATS = ("pdf", "svg", "png")
SI_PREFIXES = [(1e-15, "f"), (1e-12, "p"), (1e-9, "n"), (1e-6, "µ"), (1e-3, "m"), (1.0, "")]


# --------------------------------------------------------------------------- small helpers
def html_to_mathtext(html: str) -> str:
    """A peak label as the panel shows it, for matplotlib:
    '<sup>12</sup>C<sub>2</sub><br><sup>28</sup>Si' -> '$^{12}$C$_{2}$' + newline + '$^{28}$Si'."""
    s = re.sub(r"<br\s*/?>", "\n", html or "")
    s = re.sub(r"<sup>(.*?)</sup>", r"$^{\1}$", s)
    s = re.sub(r"<sub>(.*?)</sub>", r"$_{\1}$", s)
    return re.sub(r"<[^>]+>", "", s)


def html_to_plain(html: str) -> str:
    """'<sup>12</sup>C<sub>2</sub><br><sup>28</sup>Si' -> '12C2 / 28Si'."""
    return re.sub(r"<[^>]+>", "", re.sub(r"<br\s*/?>", " / ", html or ""))


def mass_of(amps, k, i_off, energy_ev):
    """AMU = k (I + I_off)^2 / E (the panel's calibration); below I = -I_off counts as -I_off."""
    return k * np.clip(np.asarray(amps, dtype=float) + i_off, 0.0, None) ** 2 / energy_ev


def current_of(amu, k, i_off, energy_ev):
    return np.sqrt(np.clip(np.asarray(amu, dtype=float), 0.0, None) * energy_ev / k) - i_off


def si_prefix(values):
    """(factor, unit) so that the largest current reads between 1 and 1000: (1e-9, 'nA')."""
    v = np.abs(np.asarray(values, dtype=float))
    v = v[np.isfinite(v)]
    top = float(v.max()) if v.size else 0.0
    factor, prefix = 1.0, ""
    for f, p in SI_PREFIXES:
        if top >= f:
            factor, prefix = f, p
    return factor, prefix + "A"


def auto_ylim(y, scale):
    """A current range for the data: from 0 (or the most negative) on a linear axis; a few decades
    under the highest on a log one. None if a log axis has nothing to show."""
    y = np.asarray(y, dtype=float)
    y = y[np.isfinite(y)]
    if not y.size:
        return None
    if scale == "linear":
        lo, hi = min(0.0, float(y.min())), float(y.max())
        pad = 0.05 * ((hi - lo) or abs(hi) or 1e-12)
        return [lo - pad if lo < 0 else 0.0, hi + pad]
    pos = y[y > 0]
    if not pos.size:
        return None
    lo = 10.0 ** math.floor(math.log10(float(np.percentile(pos, 2))))
    return [lo, float(pos.max()) * 2.0]


# --------------------------------------------------------------------------- the folder
def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def shift_of(settings) -> float:
    """The shift added to every beam current in the figures, in A (0 for folders made before it)."""
    return float(settings.get("current_shift_A") or 0.0)


def shifted(data, peaks, shift):
    """Copies of data and peaks with shift (A) added to their beam currents: as the figures draw them."""
    data = dict(data, Beam_A=np.asarray(data["Beam_A"], dtype=float) + shift)
    return data, [dict(p, current=p["current"] + shift) for p in peaks]


def read_folder(folder):
    """-> (data {column: array}, peaks [dict], settings dict), the beam currents as measured (see
    shifted())."""
    with open(os.path.join(folder, "figure.json"), encoding="utf-8") as f:
        settings = json.load(f)
    with open(os.path.join(folder, "data.csv"), newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    data = {c: np.array([float(r[c]) for r in rows]) for c in ("Magnet_A", "Mass_amu", "Beam_A", "Energy_keV")}
    peaks = []
    path = os.path.join(folder, "peaks.csv")
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                peaks.append({"amu": float(r["Mass_amu"]), "amps": float(r["Magnet_A"]),
                              "current": float(r["Beam_A"]), "label": r["Label_matplotlib"],
                              "labelled": r["Labelled"] == "1"})
    return data, peaks, settings


def variants(settings, data, peaks):
    """The figures to make: as framed on the screen at full and half width, then the other current
    scale, the other x quantity and (with peak labels) none."""
    screen = settings["screen"]
    cal = settings["calibration"]
    k, i_off, e_ev = cal["k"], cal["i_off"], cal["energy_kev_mean"] * 1000.0
    labels = any(p["labelled"] for p in peaks)
    base = dict(x_unit=screen["x_unit"], top_axis=screen["top_axis"], y_scale=screen["y_scale"],
                xlim=list(screen["xlim"]), ylim=list(screen["ylim"]), labels=labels)
    out = [dict(base, name="full", size="full"), dict(base, name="half", size="half")]
    if screen["y_scale"] == "linear":
        ylim = auto_ylim(data["Beam_A"], "log")
        if ylim:
            out.append(dict(base, name="full_log", size="full", y_scale="log", ylim=ylim))
    elif auto_ylim(data["Beam_A"], "linear"):
        out.append(dict(base, name="full_linear", size="full", y_scale="linear",
                        ylim=auto_ylim(data["Beam_A"], "linear")))
    if screen["x_unit"] == "amu":
        xlim = [float(current_of(x, k, i_off, e_ev)) for x in screen["xlim"]]
        out.append(dict(base, name="full_by_current", size="full", x_unit="amps", xlim=xlim))
    else:
        xlim = [float(mass_of(x, k, i_off, e_ev)) for x in screen["xlim"]]
        out.append(dict(base, name="full_by_mass", size="full", x_unit="amu", xlim=xlim))
    if labels:
        out.append(dict(base, name="full_unlabelled", size="full", labels=False))
    return out


# --------------------------------------------------------------------------- drawing
def _has_font(name) -> bool:
    from matplotlib import font_manager
    try:
        font_manager.findfont(name, fallback_to_default=False)
        return True
    except ValueError:
        return False


def _rc(font, font_size):
    rc = {
        "font.size": font_size, "axes.labelsize": font_size, "xtick.labelsize": font_size - 1,
        "ytick.labelsize": font_size - 1, "lines.linewidth": 0.8,
        "axes.linewidth": 0.6, "xtick.major.width": 0.6, "ytick.major.width": 0.6,
        "xtick.minor.width": 0.4, "ytick.minor.width": 0.4,
        "xtick.direction": "in", "ytick.direction": "in", "ytick.right": True,
        "axes.grid": True, "grid.color": "0.88", "grid.linewidth": 0.4,
        "pdf.fonttype": 42, "ps.fonttype": 42, "svg.fonttype": "none",   # text stays text
        "savefig.bbox": None, "figure.constrained_layout.h_pad": 0.02, "figure.constrained_layout.w_pad": 0.02,
    }
    # The superscripts (isotopes, 10^-9) in the text's own font where it is installed, so that an
    # SVG opened in Inkscape needs no other font
    face, fallback = ("Times New Roman", "stix") if font == "serif" else ("Arial", "dejavusans")
    if font == "serif":
        rc.update({"font.family": "serif", "font.serif": ["Times New Roman", "Times", "STIXGeneral", "DejaVu Serif"]})
    else:
        rc.update({"font.family": "sans-serif", "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"]})
    if _has_font(face):
        rc.update({"mathtext.fontset": "custom", "mathtext.rm": face, "mathtext.it": f"{face}:italic",
                   "mathtext.bf": f"{face}:bold", "mathtext.cal": face, "mathtext.sf": face, "mathtext.tt": face})
    else:
        rc["mathtext.fontset"] = fallback
    return rc


def _place_labels(fig, ax, annotations, stack=True):
    """Peak labels kept inside the frame sideways. Where two would overlap, the smaller peak's is
    lifted above the other's (stack) or left out (in a narrow figure). If the highest then pokes
    out of the top, the top of the current axis is raised to take it."""
    for _ in range(2):
        if not annotations:
            return
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
        frame = ax.get_window_extent(renderer)
        to_pt = 72.0 / fig.dpi
        placed = []
        for ann in sorted(annotations, key=lambda a: -a.peak_current):      # the biggest peaks first
            box = ann.get_window_extent(renderer)
            shift = min(0.0, frame.x1 - 3 - box.x1) or max(0.0, frame.x0 + 3 - box.x0)
            if shift:
                ann.xyann = (ann.xyann[0] + shift * to_pt, ann.xyann[1])
            for _step in range(15):
                box = ann.get_window_extent(renderer)
                clash = next((p for p in placed if box.overlaps(p)), None)
                if clash is None or not stack:
                    break
                ann.xyann = (ann.xyann[0], ann.xyann[1] + (clash.y1 - box.y0) * to_pt + 1.0)
            if clash is not None and not stack:
                ann.remove()
                annotations.remove(ann)
                continue
            placed.append(ann.get_window_extent(renderer))
        if not placed:
            return
        top = max(b.y1 for b in placed)
        if top <= frame.y1 - 2:
            return
        need = (top - frame.y0) / max(1.0, frame.y1 - frame.y0) * 1.04
        transform = ax.yaxis.get_transform()
        y0, y1 = ax.get_ylim()
        t0, t1 = transform.transform([y0, y1])
        ax.set_ylim(y0, float(transform.inverted().transform([t0 + (t1 - t0) * need])[0]))


def render(data, peaks, settings, variant, out_base, formats=FORMATS, font="serif", font_size=9.0,
           mass_label=None):
    """One figure, saved as out_base.<format> for each format. Returns the files written."""
    import matplotlib
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    width_mm, height_mm, png_px = SIZES[variant["size"]]
    width_mm, height_mm = variant.get("width_mm", width_mm), variant.get("height_mm", height_mm)
    cal = settings["calibration"]
    k, i_off, e_ev = cal["k"], cal["i_off"], cal["energy_kev_mean"] * 1000.0
    x_unit, scale = variant["x_unit"], variant["y_scale"]
    labels = dict(X_LABELS, **({"amu": mass_label} if mass_label else {}))

    x = np.asarray(data["Magnet_A"] if x_unit == "amps" else data["Mass_amu"], dtype=float)
    y = np.asarray(data["Beam_A"], dtype=float)
    order = np.argsort(x)
    x, y = x[order], y[order]
    xlim = sorted(variant["xlim"])
    ylim = list(variant["ylim"])

    written = []
    with matplotlib.rc_context(_rc(font, font_size)):
        fig = Figure(figsize=(width_mm / 25.4, height_mm / 25.4), layout="constrained")
        FigureCanvasAgg(fig)
        ax = fig.add_subplot()
        factor = 1.0
        if scale == "linear":
            in_view = y[(x >= xlim[0]) & (x <= xlim[1])]
            factor, unit = si_prefix(np.concatenate([in_view, ylim]))
            ax.plot(x, y / factor, color=LINE_COLOUR)
            ax.set_ylim(ylim[0] / factor, ylim[1] / factor)
            ax.set_ylabel(f"Beam current ({unit})")
        else:
            if scale == "log":
                ax.set_yscale("log")
                ax.plot(x, np.where(y > 0, y, np.nan), color=LINE_COLOUR)   # (a log axis has no 0 or below)
            else:
                ax.set_yscale("symlog", linthresh=LINTHRESH, linscale=0.4)
                ax.plot(x, y, color=LINE_COLOUR)
                ax.axhline(0.0, color="0.6", linewidth=0.4, zorder=0.6)
            ax.set_ylim(*ylim)
            ax.set_ylabel("Beam current (A)")
            ax.grid(True, which="major")
        ax.set_xlim(*xlim)
        ax.set_xlabel(labels[x_unit])
        if variant["top_axis"]:
            other = "amps" if x_unit == "amu" else "amu"
            if x_unit == "amu":
                funcs = (lambda m: current_of(m, k, i_off, e_ev), lambda a: mass_of(a, k, i_off, e_ev))
            else:
                funcs = (lambda a: mass_of(a, k, i_off, e_ev), lambda m: current_of(m, k, i_off, e_ev))
            top = ax.secondary_xaxis("top", functions=funcs)
            top.set_xlabel(labels[other])
            top.tick_params(direction="in")
        else:
            ax.tick_params(top=True)
        annotations = []
        if variant["labels"]:
            for p in peaks:
                px = p["amps"] if x_unit == "amps" else p["amu"]
                py = p["current"] / factor
                if not p["labelled"] or not xlim[0] <= px <= xlim[1] or (scale == "log" and py <= 0):
                    continue
                lo, hi = sorted(ax.get_ylim())
                if not lo <= py <= hi:
                    continue
                ann = ax.annotate(p["label"], xy=(px, py), xytext=(0, 2.0), textcoords="offset points",
                                  ha="center", va="bottom", fontsize=font_size - 1.5, linespacing=1.0,
                                  color="0.1", annotation_clip=False)
                ann.peak_current = p["current"]
                annotations.append(ann)
        _place_labels(fig, ax, annotations, stack=variant["size"] != "half")
        for fmt in formats:
            path = f"{out_base}.{fmt}"
            fig.savefig(path, dpi=png_px / (width_mm / 25.4) if fmt == "png" else None)
            written.append(path)
    return written


def render_folder(folder, only=None, formats=FORMATS, font="serif", font_size=9.0, mass_label=None,
                  xlim=None, ylim=None, no_labels=False, prefix="mass_scan", shift_pa=None):
    """Every variant (or `only` one) from a figure folder. xlim / ylim: a new framing, in the
    screen's x unit and in A; shift_pa: another shift of the beam current, in pA. Returns the
    files written."""
    data, peaks, settings = read_folder(folder)
    if shift_pa is not None:
        settings["current_shift_A"] = float(shift_pa) * 1e-12
    data, peaks = shifted(data, peaks, shift_of(settings))
    if xlim:
        settings["screen"]["xlim"] = list(xlim)
    if ylim:
        settings["screen"]["ylim"] = list(ylim)
    written = []
    for v in variants(settings, data, peaks):
        if only and v["name"] != only:
            continue
        if no_labels:
            v["labels"] = False
        written += render(data, peaks, settings, v, os.path.join(folder, f"{prefix}_{v['name']}"), formats,
                          font, font_size, mass_label)
    return written


# --------------------------------------------------------------------------- writing a new folder
def caption_text(settings) -> str:
    """A draft caption, in LaTeX."""
    s, cal = settings["screen"], settings["calibration"]
    lo, hi = sorted(s["xlim"])
    span = (f"{lo:.1f} to {hi:.1f}~amu" if s["x_unit"] == "amu" else f"{lo:.2f} to {hi:.2f}~A")
    energy = (f"at a beam energy of {cal['energy_kev_mean']:.1f}~keV" if cal.get("energy_recorded", True) else
              f"taking a beam energy of {cal['energy_kev_mean']:.0f}~keV (none was recorded)")
    shift = shift_of(settings) * 1e12
    return (f"Mass scan: the beam current on the Faraday cup against "
            f"{'mass' if s['x_unit'] == 'amu' else 'the analyser magnet current'}, {span}, {energy}. "
            f"Masses from the analyser magnet current $I$ with $m = k\\,(I + I_\\mathrm{{off}})^2/E$, "
            f"$k = {cal['k']:.1f}$, $I_\\mathrm{{off}} = {cal['i_off']:.3f}$~A."
            + (f" The beam current readings are shifted by ${shift:+.1f}$~pA, to take out the offset of the "
               f"current measurement." if shift else ""))


def readme_text(settings) -> str:
    names = [v["name"] for v in settings["variants"]]
    lines = [
        "Mass scan figures, exported from the SINDIE HMI's Mass Scan panel.",
        "",
        f"Made {settings['made']} (software {settings['software'] or 'version unknown'}).",
        f"Data: {settings['source']['description']}.",
        f"Calibration: k = {settings['calibration']['k']:.2f}, I_off = {settings['calibration']['i_off']:.4f} A "
        f"({settings['calibration']['source']}); mean beam energy {settings['calibration']['energy_kev_mean']:.2f} keV"
        + ("" if settings["calibration"].get("energy_recorded", True) else " (none recorded: assumed)") + ".",
        (f"Beam current: every reading shifted by {shift_of(settings) * 1e12:+.1f} pA in the figures, to take out "
         "the offset of the current measurement (current_shift_A in figure.json; make_figure.py --shift-pA "
         "draws them with another)." if shift_of(settings) else "Beam current: the readings as measured (no shift)."),
        "",
        "Figures (each as .pdf, .svg and .png):",
    ]
    meaning = {"full": "150 mm wide (\\textwidth), framed as on the screen",
               "half": "72 mm wide (0.48\\textwidth), for a subfigure",
               "full_log": "full width, log current axis", "full_linear": "full width, linear current axis",
               "full_by_current": "full width, against the magnet current",
               "full_by_mass": "full width, against mass", "full_unlabelled": "full width, no peak labels"}
    for n in names:
        lines.append(f"  mass_scan_{n}  - {meaning.get(n, n)}")
    lines += [
        "",
        "In LaTeX, at the width each was made for (so the 9 pt text prints at 9 pt):",
        "  \\includegraphics[width=\\textwidth]{mass_scan_full.pdf}",
        "  \\begin{subfigure}{0.48\\textwidth}\\includegraphics[width=\\linewidth]{mass_scan_half.pdf}\\end{subfigure}",
        "",
        "Rename the PDF (and its SVG) by content before putting it in the thesis's figures folder, e.g.",
        "mass_scan_si_target.pdf; keep data.csv, peaks.csv, figure.json and make_figure.py together in",
        "analysis/, so the figure can be drawn again: python make_figure.py --help.",
        "",
        "data.csv: the points within the visible range (and one either side), in scan order:",
        "  Magnet_A (measured magnet current), Mass_amu (from the calibration, at the point's own beam",
        "  energy), Beam_A (Faraday cup current as measured, before any shift), Energy_keV (target +",
        "  extraction; 0 if not recorded).",
        "peaks.csv: the peaks the panel found (if peak detection was on), with their labels (Beam_A as",
        "  measured, too).",
        "caption.txt: a draft caption.",
    ]
    return "\n".join(lines) + "\n"


def export(folder, points, peaks, settings, formats=FORMATS):
    """A new figure folder: data.csv, peaks.csv, figure.json, caption.txt, README.txt,
    make_figure.py (this file) and every variant. points: [(magnet A, mass amu, beam A, energy keV)];
    peaks: [{amu, amps, current, html, labelled}], beam currents as measured; the figures add
    settings["current_shift_A"] (if any). Returns the figure files written."""
    os.makedirs(folder, exist_ok=True)
    write_csv(os.path.join(folder, "data.csv"), ["Magnet_A", "Mass_amu", "Beam_A", "Energy_keV"],
              [[f"{a:.6f}", f"{m:.6f}", f"{b:.6e}", f"{e:.4f}"] for a, m, b, e in points])
    peak_rows = [{"amu": p["amu"], "amps": p["amps"], "current": p["current"],
                  "label": html_to_mathtext(p.get("html", "")), "labelled": bool(p.get("labelled"))} for p in peaks]
    if peak_rows:
        write_csv(os.path.join(folder, "peaks.csv"),
                  ["Mass_amu", "Magnet_A", "Beam_A", "Labelled", "Label_matplotlib", "Label_plain"],
                  [[f"{p['amu']:.4f}", f"{p['amps']:.5f}", f"{p['current']:.6e}", "1" if p["labelled"] else "0",
                    p["label"], html_to_plain(pk.get("html", ""))] for p, pk in zip(peak_rows, peaks)])
    data = {"Magnet_A": np.array([p[0] for p in points], dtype=float),
            "Mass_amu": np.array([p[1] for p in points], dtype=float),
            "Beam_A": np.array([p[2] for p in points], dtype=float),
            "Energy_keV": np.array([p[3] for p in points], dtype=float)}
    settings = dict(settings)
    data, peak_rows = shifted(data, peak_rows, shift_of(settings))          # (as the figures draw them)
    settings["variants"] = variants(settings, data, peak_rows)
    with open(os.path.join(folder, "figure.json"), "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    with open(os.path.join(folder, "caption.txt"), "w", encoding="utf-8") as f:
        f.write(caption_text(settings) + "\n")
    with open(os.path.join(folder, "README.txt"), "w", encoding="utf-8") as f:
        f.write(readme_text(settings))
    with open(__file__, encoding="utf-8") as src, \
            open(os.path.join(folder, "make_figure.py"), "w", encoding="utf-8") as dst:
        dst.write(src.read())
    written = []
    for v in settings["variants"]:
        written += render(data, peak_rows, settings, v, os.path.join(folder, f"mass_scan_{v['name']}"), formats)
    return written


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--folder", default=here, help="the figure folder (default: this script's folder)")
    ap.add_argument("--only", help="one variant: full, half, full_log, full_linear, full_by_current, ...")
    ap.add_argument("--xlim", type=float, nargs=2, metavar=("LO", "HI"), help="x range, in the screen's x unit")
    ap.add_argument("--ylim", type=float, nargs=2, metavar=("LO", "HI"), help="beam current range, in A")
    ap.add_argument("--font-size", type=float, default=9.0, help="text size in pt (9)")
    ap.add_argument("--sans", action="store_true", help="sans-serif text (default serif)")
    ap.add_argument("--mass-label", help='the mass axis title (default "Mass (amu)")')
    ap.add_argument("--no-labels", action="store_true", help="no peak labels")
    ap.add_argument("--shift-pA", type=float, dest="shift_pa",
                    help="add this to every beam current, in pA (default: the export's own shift, figure.json)")
    ap.add_argument("--formats", default=",".join(FORMATS))
    a = ap.parse_args()
    files = render_folder(a.folder, only=a.only, formats=tuple(a.formats.split(",")),
                          font="sans" if a.sans else "serif", font_size=a.font_size, mass_label=a.mass_label,
                          xlim=a.xlim, ylim=a.ylim, no_labels=a.no_labels, shift_pa=a.shift_pa)
    print("\n".join(files) if files else "No figure made (is --only one of the variants in figure.json?)")


if __name__ == "__main__":
    main()
