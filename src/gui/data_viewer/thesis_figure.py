"""
Publication-quality figure from an IPIDS Data Viewer export.

This file is copied into every export folder as `make_figure.py`, so a figure
can be regenerated and restyled long after the export, on any machine with
numpy + matplotlib (polars or pandas optional, for reading data.parquet):

    python make_figure.py                         # figure.pdf / .svg / .png
    python make_figure.py --width-cm 16 --lane-height-cm 5 --sans
    python make_figure.py --clock                  # clock time instead of elapsed
    python make_figure.py --start 120 --end 900    # crop (elapsed seconds)

Style: light background, Okabe-Ito colour-blind-safe palette, 9 pt text,
vector output with embedded (editable) fonts, one panel per viewer lane,
log-scale channels on their own (right-hand) axis where a lane mixes scales,
automatic SI prefixes (e.g. A -> uA) for single-unit axes, optional min/max
band for time-averaged exports, event markers as labelled dashed lines.
Long series are reduced to a min/max envelope (~max_points per line), which
is visually lossless at print size and keeps PDFs small.
"""
import argparse
import csv
import json
import os
import time
from datetime import datetime

import numpy as np

OKABE_ITO = ["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9", "#000000", "#999999"]
SI_UNITS = {"A", "V", "W", "Hz", "s", "Ω", "Ohm", "F", "H", "J", "Pa"}
# Registry units -> symbols used in figures (data files keep the registry unit)
DISPLAY_UNITS = {"mB": "mbar", "Ohm": "\u03a9", "degC": "\u00b0C"}
SI_PREFIXES = [(1e-12, "p"), (1e-9, "n"), (1e-6, "µ"), (1e-3, "m"), (1.0, ""), (1e3, "k"), (1e6, "M"), (1e9, "G")]

DEFAULTS = dict(width_cm=15.0, lane_height_cm=4.5, time_axis="elapsed", font="serif", font_size=9.0,
                dpi=300, line_width=0.9, max_points=4000, formats=("pdf", "svg", "png"),
                show_markers=True, show_band=True, title=None, start=None, end=None)


# --------------------------------------------------------------------------- loading
def load_export(folder):
    """-> (columns: dict[name -> np.ndarray], metadata dict)."""
    with open(os.path.join(folder, "metadata.json")) as f:
        meta = json.load(f)
    pq, cs = os.path.join(folder, "data.parquet"), os.path.join(folder, "data.csv")
    if os.path.exists(pq):
        try:
            import polars as pl
            df = pl.read_parquet(pq)
            return {c: (df[c].to_numpy() if c != "Human_Time" else None) for c in df.columns}, meta
        except ImportError:
            pass
        try:
            import pandas as pd
            df = pd.read_parquet(pq)
            return {c: (df[c].to_numpy() if c != "Human_Time" else None) for c in df.columns}, meta
        except ImportError:
            pass
    try:
        import pandas as pd
        df = pd.read_csv(cs, na_values=["nan", ""])
        return {c: (df[c].to_numpy(dtype=float) if c != "Human_Time" else None) for c in df.columns}, meta
    except ImportError:
        with open(cs, newline="") as f:
            rows = list(csv.reader(f))
        hdr, body = rows[0], rows[1:]
        out = {}
        for i, h in enumerate(hdr):
            if h == "Human_Time":
                out[h] = None
                continue
            out[h] = np.array([float(r[i]) if r[i] not in ("", "nan") else np.nan for r in body])
        return out, meta


# --------------------------------------------------------------------------- helpers
def decimate(x, y, max_points):
    """Min/max envelope with ~max_points points; keeps peaks and NaN gaps."""
    n = len(x)
    if n <= max_points:
        return x, y
    buckets = max(1, max_points // 2)
    edges = np.linspace(0, n, buckets + 1).astype(int)
    xs, ys = [], []
    for a, b in zip(edges[:-1], edges[1:]):
        if b <= a:
            continue
        seg = y[a:b]
        fin = np.isfinite(seg)
        if not fin.any():
            xs.append(x[a]); ys.append(np.nan)
            continue
        i_min = a + int(np.nanargmin(seg)); i_max = a + int(np.nanargmax(seg))
        for i in sorted({i_min, i_max}):
            xs.append(x[i]); ys.append(y[i])
    return np.asarray(xs), np.asarray(ys)


def si_prefix(values, unit):
    """(factor, prefixed unit) so that the axis numbers are in [1, 1000)."""
    if unit not in SI_UNITS:
        return 1.0, unit
    v = np.concatenate([np.abs(a[np.isfinite(a)]) for a in values if a is not None and len(a)] or [np.array([])])
    if v.size == 0 or np.nanmax(v) == 0:
        return 1.0, unit
    m = np.nanpercentile(v, 99)
    factor, p = 1.0, ""
    for f, pre in SI_PREFIXES:
        if m >= f:
            factor, p = f, pre
    return factor, p + unit


def local_datetimes(ts):
    hours = np.floor(ts / 3600.0).astype(np.int64)
    uh, inv = np.unique(hours, return_inverse=True)
    offs = np.array([time.localtime(int(h) * 3600).tm_gmtoff for h in uh], dtype=np.float64)
    return ((ts + offs[inv]) * 1e6).astype("datetime64[us]")


def elapsed_axis(span_s):
    for limit, unit, div in ((180, "s", 1.0), (3 * 3600, "min", 60.0), (3 * 86400, "h", 3600.0)):
        if span_s <= limit:
            return unit, div
    return "days", 86400.0


# --------------------------------------------------------------------------- figure
def render(cols, meta, out_base, **opts):
    """Draw and save the figure. Returns the list of files written."""
    import matplotlib
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    import matplotlib.dates as mdates

    o = dict(DEFAULTS)
    o.update({k: v for k, v in opts.items() if v is not None})
    channels = meta["channels"]
    t0 = float(meta["time_range"]["start_unix"])
    ts = np.asarray(cols["Timestamp_UNIX"], dtype=np.float64)
    sel = np.ones(len(ts), dtype=bool)
    if o["start"] is not None:
        sel &= ts >= t0 + float(o["start"])
    if o["end"] is not None:
        sel &= ts <= t0 + float(o["end"])
    ts = ts[sel]

    lanes = sorted({ch.get("lane", "Lane 1") for ch in channels})
    rc = {
        "font.size": o["font_size"], "axes.labelsize": o["font_size"], "axes.titlesize": o["font_size"],
        "legend.fontsize": o["font_size"] - 1, "xtick.labelsize": o["font_size"] - 1,
        "ytick.labelsize": o["font_size"] - 1, "lines.linewidth": o["line_width"],
        "axes.linewidth": 0.6, "xtick.major.width": 0.6, "ytick.major.width": 0.6,
        "xtick.direction": "in", "ytick.direction": "in", "xtick.top": True, "ytick.right": False,
        "axes.grid": True, "grid.color": "0.85", "grid.linewidth": 0.5,
        "pdf.fonttype": 42, "ps.fonttype": 42, "svg.fonttype": "none",
        "savefig.bbox": "tight", "savefig.pad_inches": 0.02,
        "legend.frameon": False, "legend.handlelength": 1.6,
    }
    if o["font"] == "serif":
        rc.update({"font.family": "serif",
                   "font.serif": ["Times New Roman", "Times", "STIXGeneral", "DejaVu Serif"],
                   "mathtext.fontset": "stix"})
    else:
        rc.update({"font.family": "sans-serif",
                   "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"]})

    written = []
    with matplotlib.rc_context(rc):
        fig = Figure(figsize=(o["width_cm"] / 2.54, len(lanes) * o["lane_height_cm"] / 2.54),
                     layout="constrained")
        FigureCanvasAgg(fig)
        axes = fig.subplots(len(lanes), 1, sharex=True, squeeze=False)[:, 0]

        if o["time_axis"] == "clock":
            x = local_datetimes(ts)
        else:
            unit, div = elapsed_axis((ts[-1] - ts[0]) if len(ts) > 1 else 1.0)
            x = (ts - t0) / div

        color_i = 0
        for ax, lane in zip(axes, lanes):
            lane_ch = [ch for ch in channels if ch.get("lane", "Lane 1") == lane]
            lin = [ch for ch in lane_ch if ch.get("scale") != "log"]
            logc = [ch for ch in lane_ch if ch.get("scale") == "log"]
            groups = []
            if lin:
                groups.append((ax, lin, False))
            if logc:
                groups.append((ax.twinx() if lin else ax, logc, True))
            handles = []
            for gax, chs, is_log in groups:
                units = {DISPLAY_UNITS.get(ch.get("unit") or "", ch.get("unit") or "") for ch in chs}
                mults = {float(ch.get("multiplier", 1.0) or 1.0) for ch in chs}
                series = []
                for ch in chs:
                    y = cols.get(ch["column"])
                    y = None if y is None else np.asarray(y, dtype=np.float64)[sel]
                    m = float(ch.get("multiplier", 1.0) or 1.0)
                    series.append((ch, None if y is None else y * m, m))
                factor, unit_lbl = 1.0, (next(iter(units)) if len(units) == 1 else None)
                if not is_log and len(units) == 1 and mults == {1.0}:
                    factor, unit_lbl = si_prefix([s[1] for s in series], next(iter(units)))
                for ch, y, m in series:
                    color = OKABE_ITO[color_i % len(OKABE_ITO)]
                    color_i += 1
                    if y is None or not np.isfinite(y).any():
                        continue
                    name = ch["label"] + (f" (×{m:g})" if m != 1.0 else "")
                    if len(units) > 1 and ch.get("unit"):
                        name += f" ({DISPLAY_UNITS.get(ch['unit'], ch['unit'])})"
                    lo_col, hi_col = f"{ch['column']} min", f"{ch['column']} max"
                    if o["show_band"] and lo_col in cols and hi_col in cols:
                        lo = np.asarray(cols[lo_col], dtype=np.float64)[sel] * m / factor
                        hi = np.asarray(cols[hi_col], dtype=np.float64)[sel] * m / factor
                        gax.fill_between(x, lo, hi, color=color, alpha=0.2, linewidth=0, step=None)
                    if o["time_axis"] == "clock":
                        xx, yy = x, y / factor
                        if len(xx) > o["max_points"]:
                            xf, yy = decimate(ts, y / factor, o["max_points"])
                            xx = local_datetimes(xf)
                    else:
                        xx, yy = decimate(x, y / factor, o["max_points"])
                    (h,) = gax.plot(xx, yy, color=color, label=name,
                                    linestyle="--" if (is_log and lin) else "-")
                    handles.append(h)
                if is_log:
                    gax.set_yscale("log")
                if len(chs) == 1 and handles and handles[-1].get_label().startswith(chs[0]["label"]):
                    lbl = chs[0]["label"] + (f" ({unit_lbl})" if unit_lbl else "")
                else:
                    lbl = f"({unit_lbl})" if unit_lbl else ""
                gax.set_ylabel(lbl)
                if is_log and lin:
                    gax.grid(False)
            if len(handles) > 1 or (len(handles) == 1 and len(groups) > 1):
                ax.legend(handles=handles, loc="best", ncols=1 if len(handles) < 4 else 2)

        # event markers
        if o["show_markers"]:
            for mk in meta.get("markers", []):
                t = float(mk["timestamp"])
                if not (ts[0] <= t <= ts[-1]) if len(ts) else True:
                    continue
                xm = local_datetimes(np.array([t]))[0] if o["time_axis"] == "clock" else (t - t0) / div
                for ax in axes:
                    ax.axvline(xm, color="0.35", linestyle=(0, (3, 2)), linewidth=0.6, zorder=0.5)
                if mk.get("text"):
                    # label above the top panel so it never collides with data or legend
                    axes[0].annotate(mk["text"], xy=(xm, 1.0), xycoords=("data", "axes fraction"),
                                     xytext=(0, 2), textcoords="offset points", rotation=0,
                                     va="bottom", ha="center", fontsize=o["font_size"] - 1, color="0.25",
                                     annotation_clip=False)

        if o["time_axis"] == "clock":
            loc = mdates.AutoDateLocator()
            axes[-1].xaxis.set_major_locator(loc)
            axes[-1].xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))
            axes[-1].set_xlabel("Local time")
        else:
            start = datetime.fromtimestamp(t0 + (float(o["start"]) if o["start"] else 0.0))
            axes[-1].set_xlabel(f"Time since {start:%H:%M:%S, %d %b %Y} ({unit})")
            if o["start"] is not None or o["end"] is not None:
                axes[-1].set_xlim(float(o["start"] or 0) / div, (float(o["end"]) if o["end"] else
                                                                 (ts[-1] - t0)) / div)
        fig.align_ylabels(axes)
        if o["title"]:
            fig.suptitle(o["title"])

        for fmt in o["formats"]:
            path = f"{out_base}.{fmt}"
            fig.savefig(path, dpi=o["dpi"] if fmt == "png" else None)
            written.append(path)
    return written


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--folder", default=here, help="export folder (default: this script's folder)")
    ap.add_argument("--out", default=None, help="output base name (default: <folder>/figure)")
    ap.add_argument("--width-cm", type=float)
    ap.add_argument("--lane-height-cm", type=float)
    ap.add_argument("--font-size", type=float)
    ap.add_argument("--sans", action="store_true", help="sans-serif font (default serif)")
    ap.add_argument("--clock", action="store_true", help="clock time on the x axis")
    ap.add_argument("--start", type=float, help="crop start, elapsed seconds")
    ap.add_argument("--end", type=float, help="crop end, elapsed seconds")
    ap.add_argument("--no-markers", action="store_true")
    ap.add_argument("--no-band", action="store_true", help="hide min/max band (averaged exports)")
    ap.add_argument("--title")
    ap.add_argument("--formats", default="pdf,svg,png")
    a = ap.parse_args()
    cols, meta = load_export(a.folder)
    files = render(cols, meta, a.out or os.path.join(a.folder, "figure"),
                   width_cm=a.width_cm, lane_height_cm=a.lane_height_cm, font_size=a.font_size,
                   font="sans" if a.sans else "serif", time_axis="clock" if a.clock else "elapsed",
                   start=a.start, end=a.end, show_markers=not a.no_markers, show_band=not a.no_band,
                   title=a.title, formats=tuple(a.formats.split(",")))
    print("\n".join(files))


if __name__ == "__main__":
    main()
