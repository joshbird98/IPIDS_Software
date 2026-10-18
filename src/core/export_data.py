"""
Data export for analysis and publication figures (no Qt; runs in a worker thread).

Modes
-----
exact   Every logged row in the range, read from the logger's parquet files
        (not the on-screen, downsampled copy). If the range runs past what is on
        disk, the logger is first asked to flush (FORCE_FLUSH); anything still not
        on disk comes from the viewer's live buffer (native service timestamps,
        each channel held at its latest sample on a merged time base).
mean    Fixed-width time bins aligned to the export start, computed from the same
        exact data, file by file (bounded memory for long ranges). Optional
        min / max / std columns per channel.
displayed  What is drawn on screen (min/max envelope for zoomed-out views) -
        assembled by the viewer and passed in as a table.

Outputs (in one folder per export)
----------------------------------
data.csv / data.parquet   Timestamp_UNIX, Human_Time (local), Elapsed_s, then one
                          column per channel named "<label> [<unit>]".
metadata.json             range, mode, sources, channels (tag, unit, lane, scale,
                          multiplier, description), markers, software version.
markers.csv               event markers in the range.
"""
import json
import os
import subprocess
import time
from datetime import datetime

import numpy as np
import polars as pl

TIME_COLS = ("Timestamp_UNIX", "Human_Time", "Elapsed_s")


# ----------------------------------------------------------------------------- helpers
def logger_tick_seconds(project_root):
    try:
        with open(os.path.join(project_root, "config", "logger_config.json")) as f:
            return float(json.load(f).get("tick_rate_seconds", 0.05))
    except (OSError, ValueError, TypeError, AttributeError):   # missing / unreadable: the shipped rate
        return 0.05


def request_logger_flush(timeout_s=3.0):
    """Ask the logger service to write its in-RAM rows to disk. True if confirmed (False if the
    logger isn't running or didn't answer in time)."""
    import zmq
    from src.core.network_map import ZMQ_PORT_LOGGER_CMD
    try:
        ctx = zmq.Context.instance()
        s = ctx.socket(zmq.REQ)
        s.setsockopt(zmq.RCVTIMEO, int(timeout_s * 1000))
        s.setsockopt(zmq.LINGER, 0)
        s.connect(ZMQ_PORT_LOGGER_CMD)
        try:
            s.send_string("FORCE_FLUSH")
            reply = s.recv_string()
            return "COMPLETE" in reply.upper() or "OK" in reply.upper()
        finally:
            s.close()
    except zmq.ZMQError:        # includes zmq.Again: no answer within the timeout
        return False


def git_version(project_root):
    try:
        out = subprocess.run(["git", "-C", project_root, "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=3)
        rev = out.stdout.strip()
        if not rev:
            return None
        dirty = subprocess.run(["git", "-C", project_root, "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True, timeout=5).stdout.strip()
        return rev + ("-modified" if dirty else "")
    except (OSError, subprocess.SubprocessError):     # no git, or it hung
        return None


def local_time_strings(ts):
    """Vectorised local wall-clock strings, identical to
    datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M:%S.%f') (DST-aware)."""
    ts = np.asarray(ts, dtype=np.float64)
    if len(ts) == 0:
        return pl.Series([], dtype=pl.String)
    finite = np.isfinite(ts)
    t = np.where(finite, ts, 0.0)
    hours = np.floor(t / 3600.0).astype(np.int64)
    uh, inv = np.unique(hours, return_inverse=True)
    offs = np.array([time.localtime(int(h) * 3600).tm_gmtoff for h in uh], dtype=np.int64)
    sec = np.floor(t)
    us = (sec.astype(np.int64) + offs[inv]) * 1_000_000 + np.round((t - sec) * 1e6).astype(np.int64)
    out = pl.Series(us).cast(pl.Datetime("us")).dt.strftime("%Y-%m-%d %H:%M:%S%.6f")
    return out.set(pl.Series(~finite), None) if (~finite).any() else out


def utc_offset_hours(t):
    return time.localtime(t).tm_gmtoff / 3600.0


def column_name(ch):
    unit = ch.get("unit") or ""
    return f"{ch['label']} [{unit}]" if unit else ch["label"]


def _unique_names(channels):
    seen = {}
    for ch in channels:
        base = column_name(ch)
        n = seen.get(base, 0)
        seen[base] = n + 1
        ch["column"] = base if n == 0 else f"{base} ({n + 1})"
    return channels


# ----------------------------------------------------------------------------- live tail
def live_tail_frame(live_store, t_from, t_to, tags):
    """Live-buffer samples in (t_from, t_to] on a merged time base (union of the
    channels' own sample times; each channel held at its latest sample)."""
    if live_store is None:
        return None
    win = live_store.window(t_from, t_to, tags)
    xs = [x[x > t_from] for x, _ in win.values()]
    xs = [x for x in xs if len(x)]
    if not xs:
        return None
    base = np.unique(np.concatenate(xs))
    cols = {"timestamp": base}
    for t in tags:
        if t in win:
            x, y = win[t]
            k = np.searchsorted(x, base, side="right") - 1
            cols[t] = np.where(k >= 0, y[np.clip(k, 0, len(y) - 1)], np.nan)
    return pl.DataFrame(cols)


# ----------------------------------------------------------------------------- collection
def collect_exact(engine, live_store, t0, t1, tags, flush=True, progress=None):
    """All rows in [t0, t1] for tags -> (polars DataFrame, info)."""
    say = progress or (lambda m: None)
    info = {"log_rows": 0, "live_rows": 0, "live_from_unix": None, "logger_flushed": None, "unreadable_files": []}
    if flush and t1 > time.time() - 20 * 60:
        say("Asking logger to flush recent data...")
        info["logger_flushed"] = request_logger_flush()
    say("Reading log files...")
    frames = []
    for df in engine.iter_exact_frames(t0, t1, tags, refresh_listing=True, skipped=info["unreadable_files"]):
        frames.append(df)
    disk = None
    if frames:
        disk = pl.concat(frames, how="diagonal_relaxed").sort("timestamp") \
                 .unique(subset="timestamp", keep="first", maintain_order=True)
        info["log_rows"] = disk.height
    disk_end = float(disk["timestamp"].max()) if disk is not None and disk.height else t0
    parts = [disk] if disk is not None else []
    if disk_end < t1 - 1.0:
        tail = live_tail_frame(live_store, disk_end, t1, tags)
        if tail is not None and tail.height:
            parts.append(tail)
            info["live_rows"] = tail.height
            info["live_from_unix"] = float(tail["timestamp"][0])
    if not parts:
        return pl.DataFrame({"timestamp": np.array([], dtype=np.float64)}), info
    df = pl.concat(parts, how="diagonal_relaxed").sort("timestamp")
    for t in tags:   # channels with no data in the range still get a column
        if t not in df.columns:
            df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias(t))
    return df.select(["timestamp"] + list(tags)), info


def _bin_aggs(tags, spread):
    aggs = []
    for t in tags:
        v = pl.col(t).fill_nan(None)
        aggs += [v.sum().alias(f"{t}__sum"), v.count().alias(f"{t}__n")]
        if spread:
            aggs += [v.min().alias(f"{t}__min"), v.max().alias(f"{t}__max"),
                     (v * v).sum().alias(f"{t}__sq")]
    return aggs


def collect_mean(engine, live_store, t0, t1, tags, bin_s, spread=False, flush=True, progress=None):
    """Time-binned means (and optional min/max/std) -> (DataFrame, info).
    Each file is aggregated separately, then partial bins are combined, so a
    multi-day export never holds all raw rows in memory."""
    say = progress or (lambda m: None)
    info = {"log_rows": 0, "live_rows": 0, "live_from_unix": None, "logger_flushed": None, "unreadable_files": []}
    if flush and t1 > time.time() - 20 * 60:
        say("Asking logger to flush recent data...")
        info["logger_flushed"] = request_logger_flush()

    def agg(df):
        present = [t for t in tags if t in df.columns]
        if not present:
            return None
        return (df.with_columns(((pl.col("timestamp") - t0) / bin_s).floor().cast(pl.Int64).alias("_bin"))
                  .group_by("_bin").agg(_bin_aggs(present, spread)))

    partials = []
    last_ts = t0
    for i, df in enumerate(engine.iter_exact_frames(t0, t1, tags, refresh_listing=True,
                                                    skipped=info["unreadable_files"])):
        say(f"Averaging log files... ({i + 1})")
        info["log_rows"] += df.height
        last_ts = max(last_ts, float(df["timestamp"].max()))
        a = agg(df)
        if a is not None:
            partials.append(a)
    if last_ts < t1 - 1.0 and live_store is not None:
        # live tail: aggregate each channel from its OWN samples (not the merged base)
        win = live_store.window(last_ts, t1, tags)
        for t, (x, y) in win.items():
            m = x > last_ts
            if m.any():
                info["live_rows"] += int(m.sum())
                info["live_from_unix"] = float(x[m][0]) if info["live_from_unix"] is None \
                    else min(info["live_from_unix"], float(x[m][0]))
                a = agg(pl.DataFrame({"timestamp": x[m], t: y[m]}))
                if a is not None:
                    partials.append(a)
    if not partials:
        return pl.DataFrame({"timestamp": np.array([], dtype=np.float64)}), info

    merged = pl.concat(partials, how="diagonal_relaxed")
    combine = []
    for c in merged.columns:
        if c == "_bin":
            continue
        if c.endswith("__min"):
            combine.append(pl.col(c).min())
        elif c.endswith("__max"):
            combine.append(pl.col(c).max())
        else:
            combine.append(pl.col(c).sum())
    g = merged.group_by("_bin").agg(combine).sort("_bin")
    out = {"timestamp": (t0 + (g["_bin"].to_numpy() + 0.5) * bin_s)}
    for t in tags:
        if f"{t}__sum" not in g.columns:
            out[t] = np.full(g.height, np.nan)
            if spread:
                out[f"{t}__min"] = out[f"{t}__max"] = out[f"{t}__std"] = np.full(g.height, np.nan)
            continue
        n = g[f"{t}__n"].fill_null(0).to_numpy().astype(np.float64)
        s = g[f"{t}__sum"].fill_null(0).to_numpy()
        with np.errstate(invalid="ignore", divide="ignore"):
            mean = np.where(n > 0, s / n, np.nan)
            out[t] = mean
            if spread:
                sq = g[f"{t}__sq"].fill_null(0).to_numpy()
                var = np.where(n > 1, (sq - n * mean * mean) / (n - 1), np.nan)
                out[f"{t}__std"] = np.sqrt(np.clip(var, 0, None))
                out[f"{t}__min"] = g[f"{t}__min"].cast(pl.Float64).to_numpy()
                out[f"{t}__max"] = g[f"{t}__max"].cast(pl.Float64).to_numpy()
    df = pl.DataFrame(out)
    # keep bins whose centre lies in the range
    return df.filter((pl.col("timestamp") >= t0) & (pl.col("timestamp") <= t1)), info


# ----------------------------------------------------------------------------- writing
def build_table(df, channels, t0, spread=False):
    """Internal frame (timestamp + tag columns) -> publication table."""
    ts = df["timestamp"].to_numpy()
    cols = {"Timestamp_UNIX": ts, "Human_Time": local_time_strings(ts), "Elapsed_s": ts - t0}
    for ch in channels:
        t = ch["tag"]
        name = ch["column"]
        cols[name] = df[t].cast(pl.Float64).fill_nan(None) if t in df.columns else pl.Series([None] * len(ts),
                                                                                              dtype=pl.Float64)
        if spread:
            for stat in ("min", "max", "std"):
                k = f"{t}__{stat}"
                if k in df.columns:
                    cols[f"{name} {stat}"] = df[k].cast(pl.Float64).fill_nan(None)
        m = ch.get("multiplier", 1.0)
        if m not in (None, 1, 1.0):
            cols[f"{ch['label']} (x{m:g})"] = pl.Series(cols[name]) * m
    return pl.DataFrame(cols)


def write_export(folder, table, meta, markers, write_csv=True, write_parquet=True):
    os.makedirs(folder, exist_ok=True)
    written = []
    if write_csv:
        p = os.path.join(folder, "data.csv")
        table.write_csv(p, null_value="nan")
        written.append(p)
    if write_parquet:
        p = os.path.join(folder, "data.parquet")
        table.write_parquet(p)
        written.append(p)
    if markers:
        p = os.path.join(folder, "markers.csv")
        ts = np.array([m["timestamp"] for m in markers], dtype=np.float64)
        pl.DataFrame({"Timestamp_UNIX": ts, "Human_Time": local_time_strings(ts),
                      "Elapsed_s": ts - meta["time_range"]["start_unix"],
                      "Type": [m.get("type", "") for m in markers],
                      "Text": [m.get("text", "") for m in markers],
                      "Color": [m.get("color", "") for m in markers]}).write_csv(p)
        written.append(p)
    p = os.path.join(folder, "metadata.json")
    with open(p, "w") as f:
        json.dump(meta, f, indent=2, default=float)
    written.append(p)
    return written


def make_metadata(t0, t1, mode, channels, info, markers, project_root, bin_s=None, spread=False,
                  tick_s=None, view_note=None):
    def iso(t):
        return datetime.fromtimestamp(t).astimezone().isoformat(timespec="milliseconds")
    notes = [
        "Timestamp_UNIX: seconds since 1970-01-01 UTC. Human_Time: local wall-clock time "
        f"(UTC{utc_offset_hours(t0):+g} h at start). Elapsed_s: seconds since the export start.",
        "Channel columns are '<label> [<unit>]' in the logged (base) units; empty/'nan' = no sample.",
    ]
    if mode == "exact":
        notes.append(f"Exact mode: every row recorded by the logger (sample-and-hold of all services at "
                     f"{1 / tick_s:g} Hz) in the range." if tick_s else "Exact mode: every logged row.")
        if info.get("live_rows"):
            notes.append("Rows from Timestamp_UNIX >= live_from_unix were not yet on disk and come from the "
                         "viewer's live buffer at each service's own sample times (channels held at their "
                         "latest sample).")
    elif mode == "mean":
        notes.append(f"Mean mode: arithmetic mean of all samples in fixed {bin_s:g} s bins aligned to the "
                     "export start; Timestamp_UNIX is the bin centre."
                     + (" '<column> min/max/std' give the spread within each bin." if spread else ""))
    else:
        notes.append("Displayed mode: the data drawn on screen. Zoomed-out historical views are min/max "
                     "envelopes (pairs of rows per bin), not raw samples.")
    if info.get("unreadable_files"):
        notes.append("INCOMPLETE: these log files could not be read, so their rows are missing: "
                     + "; ".join(info["unreadable_files"]))
    if any(ch.get("multiplier", 1.0) not in (None, 1, 1.0) for ch in channels):
        notes.append("Channels with a display multiplier have an extra '<label> (xM)' column with the "
                     "multiplied values; the main column is unmultiplied.")
    return {
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "software": {"name": "SINDIE Data Viewer", "git": git_version(project_root)},
        "time_range": {"start_unix": t0, "end_unix": t1, "start_local": iso(t0), "end_local": iso(t1),
                       "duration_s": t1 - t0},
        "mode": mode, "bin_seconds": bin_s, "spread_columns": bool(spread),
        "logger_tick_seconds": tick_s,
        "sources": info,
        "channels": channels,
        "markers": markers,
        "notes": notes,
        "view": view_note,
    }


# ----------------------------------------------------------------------------- thesis helpers
def summary_table(df, channels, mode):
    """Per-channel statistics over the exported range (for tables / quoted values).
    In mean mode the statistics are of the bin means (stated in the Basis column)."""
    rows = []
    for ch in channels:
        t = ch["tag"]
        y = df[t].cast(pl.Float64).fill_nan(None) if t in df.columns else pl.Series([], dtype=pl.Float64)
        ts = df["timestamp"].filter(y.is_not_null()) if t in df.columns else pl.Series([], dtype=pl.Float64)
        v = y.drop_nulls().to_numpy()
        n = len(v)
        rows.append({
            "Channel": ch["label"], "Unit": ch.get("unit", ""), "Tag": t,
            "Basis": {"exact": "logged samples", "mean": "bin means", "displayed": "displayed points"}[mode],
            "N": n,
            "Mean": float(np.mean(v)) if n else None,
            "Std": float(np.std(v, ddof=1)) if n > 1 else None,
            "Min": float(np.min(v)) if n else None,
            "Max": float(np.max(v)) if n else None,
            "First": float(v[0]) if n else None,
            "Last": float(v[-1]) if n else None,
            "First_time_unix": float(ts[0]) if n else None,
            "Last_time_unix": float(ts[-1]) if n else None,
        })
    return pl.DataFrame(rows)


def caption_text(meta):
    """Draft figure caption (edit freely)."""
    chans = meta["channels"]
    lanes = sorted({c.get("lane", "Lane 1") for c in chans})
    pos = {1: [""], 2: ["top", "bottom"], 3: ["top", "middle", "bottom"]}.get(len(lanes))
    parts = []
    for i, lane in enumerate(lanes):
        names = [c["label"] for c in chans if c.get("lane", "Lane 1") == lane]
        txt = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        where = (f" ({pos[i]})" if pos and pos[i] else f" (panel {chr(97 + i)})") if len(lanes) > 1 else ""
        parts.append(txt + where)
    t0 = datetime.fromtimestamp(meta["time_range"]["start_unix"])
    t1 = datetime.fromtimestamp(meta["time_range"]["end_unix"])
    when = (f"between {t0:%H:%M:%S} and {t1:%H:%M:%S} on {t0:%d %B %Y}" if t0.date() == t1.date()
            else f"from {t0:%H:%M, %d %B %Y} to {t1:%H:%M, %d %B %Y}")
    s = "; ".join(parts) + f", recorded {when} (local time). "
    if meta["mode"] == "exact":
        tick = meta.get("logger_tick_seconds")
        s += "Data are all logged samples" + (f" ({1 / tick:g} Hz)" if tick else "") + "."
    elif meta["mode"] == "mean":
        s += f"Lines show {meta['bin_seconds']:g} s means" + \
             ("; shaded bands show the minimum-maximum range within each bin." if meta.get("spread_columns") else ".")
    else:
        s += "Data as displayed in the viewer."
    if meta.get("markers"):
        s += " Dashed vertical lines mark logged events."
    return s
