"""Precision mass scan: step the analysing magnet current, wait for the magnet and the SMU to
settle, average the Faraday-cup current, repeat (and loop, if asked).

This is the HMI's former PrecisionScanWorker, moved into the sequencer so a scan carries on if
the HMI is closed. MassScan is the scan itself, written as a generator; MassScanJob runs it as a
sequencer job (lease on the magnet and the SMU instead of the retired cmd_ctrl_mode), and the
recipe routine "mass_scan" (src/core/routines.py) runs the same code inside a recipe.
"""
import csv
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime

import numpy as np

from src.core import paths
from src.sequencer.jobs import GeneratorJob, JobFailed
from src.sequencer.points import PointLog
from src.sequencer.protocol import MASS_SCAN, key

SP_TAG = "ion_beam.beamline.magnet.sp_requested_current"
RB_MAG_TAG = "ion_beam.beamline.magnet.rb_current"
RB_BEAM_TAG = "ion_beam.beamline.faraday.smu.rb_current"
SMU_SETTLED_TAG = "ion_beam.beamline.faraday.smu.stat_range_settled"
NPLC_TAG = "ion_beam.beamline.faraday.smu.cmd_nplc"
TARGET_KV_TAG = "ion_beam.source.target.rb_voltage"
EXT_KV_TAG = "ion_beam.source.extraction.rb_voltage"

DEFAULT_NPLC = 1.0          # what the SMU is put back to afterwards (fast readings for the HMI)
NPLC_RANGE = (0.001, 100.0)
SETTLE_TIMEOUT_S = 5.0      # longest wait for the magnet + SMU range to settle at each point
STALE_LIMIT_S = 5.0         # stop if the magnet / Faraday-cup readings are not live for this long
MAX_POINTS = 100_000
DEFAULT_EXPORT = os.path.join(paths.EXPORTS_DIR, "Mass_Scans", "MassScan")
CSV_HEADER = ["Timestamp", "Requested_Magnet_A", "Measured_Magnet_A", "Measured_Beam_A", "Target_kV",
              "Extraction_kV"]


@dataclass
class ScanConfig:
    start_a: float
    stop_a: float
    step_a: float
    tolerance: float = 0.05
    settle_time: float = 0.1
    samples: int = 5
    sample_interval: float = 0.02
    nplc: float = 10.0
    loop: bool = False
    export: bool = False
    export_path: str = ""

    @classmethod
    def from_value(cls, value, magnet_limits=None) -> "ScanConfig":
        """Validated config from a start command's value. Raises ValueError with the reason."""
        if not isinstance(value, dict):
            raise ValueError("mass scan settings must be a JSON object")
        known = set(cls.__dataclass_fields__)
        unknown = sorted(set(value) - known)
        if unknown:
            raise ValueError(f"unknown mass scan setting(s): {', '.join(unknown)}")
        try:
            cfg = cls(**value)
            for name in ("start_a", "stop_a", "step_a", "tolerance", "settle_time", "sample_interval", "nplc"):
                setattr(cfg, name, float(getattr(cfg, name)))
            if float(cfg.samples) != int(cfg.samples):
                raise ValueError(f"samples must be a whole number, not {cfg.samples!r}")
            cfg.samples = int(cfg.samples)
        except TypeError as e:
            raise ValueError(f"missing or bad mass scan setting: {e}") from e
        cfg.loop, cfg.export = bool(cfg.loop), bool(cfg.export)
        cfg.export_path = str(cfg.export_path or "")
        if not all(math.isfinite(v) for v in (cfg.start_a, cfg.stop_a, cfg.step_a, cfg.tolerance, cfg.settle_time,
                                               cfg.sample_interval, cfg.nplc)):
            raise ValueError("mass scan settings must be finite numbers")
        if magnet_limits is None or None in magnet_limits:
            raise ValueError(f"no min/max limits for {SP_TAG} in the tag registry")
        lo, hi = magnet_limits
        for name in ("start_a", "stop_a"):
            if not lo <= getattr(cfg, name) <= hi:
                raise ValueError(f"{name} {getattr(cfg, name):g} A is outside the magnet's range [{lo:g}, {hi:g}] A")
        if cfg.start_a == cfg.stop_a:
            raise ValueError("start and stop currents are the same")
        if cfg.step_a <= 0:
            raise ValueError("the step must be more than 0 A")
        if cfg.n_points > MAX_POINTS:
            raise ValueError(f"{cfg.n_points} points is more than the {MAX_POINTS} allowed - use a bigger step")
        if cfg.tolerance <= 0:
            raise ValueError("the magnet tolerance must be more than 0 A")
        if cfg.settle_time < 0 or cfg.sample_interval < 0:
            raise ValueError("settle time and sample interval can't be negative")
        if cfg.samples < 1:
            raise ValueError("at least one sample per point is needed")
        if not NPLC_RANGE[0] <= cfg.nplc <= NPLC_RANGE[1]:
            raise ValueError(f"NPLC {cfg.nplc:g} is outside [{NPLC_RANGE[0]:g}, {NPLC_RANGE[1]:g}]")
        return cfg

    @property
    def n_points(self) -> int:
        return int(round(abs(self.stop_a - self.start_a) / self.step_a)) + 1

    def setpoints(self) -> np.ndarray:
        return np.linspace(self.start_a, self.stop_a, self.n_points)

    @property
    def interval(self) -> float:
        """Time between samples: long enough for the SMU to finish an integration."""
        return max(self.sample_interval, self.nplc * 0.02 + 0.05)

    def nplc_timeout(self, nplc: float) -> float:
        # A long integration blocks the SMU service: allow for one measurement before the reply
        return max(3.0, 3.0 + 0.04 * nplc)


class MassScan:
    """The scan. io provides send(tag, value[, timeout]) -> ticket, fresh_float(tag), now() and
    stopping (bool). Iterate run() (it yields whenever it waits); restore() afterwards."""

    def __init__(self, cfg: ScanConfig, io, on_point=None, on_pass_start=None, on_pass_done=None):
        self.cfg = cfg
        self.io = io
        self.on_point = on_point or (lambda point: None)
        self.on_pass_start = on_pass_start or (lambda pass_no: None)
        self.on_pass_done = on_pass_done or (lambda pass_no, points: None)
        self.pass_no = 0
        self.passes_done = 0
        self.points = []            # current pass: (sp, magnet, beam, target_kv, extraction_kv)
        self.progress = (0, cfg.n_points)
        self.status = "starting"

    # ------------------------------------------------------------------ waiting
    def _sleep(self, seconds, interruptible=True):
        end = self.io.now() + seconds
        while self.io.now() < end:
            if interruptible and self.io.stopping:
                return
            yield

    @staticmethod
    def _wait(ticket):
        while not ticket.done():
            yield
        return ticket

    def _live(self, tag):
        return self.io.fresh_float(tag)

    # ------------------------------------------------------------------ the scan
    def run(self):
        cfg, io = self.cfg, self.io
        self.status = f"setting the SMU to NPLC {cfg.nplc:g}"
        ticket = yield from self._wait(io.send(NPLC_TAG, cfg.nplc, timeout=cfg.nplc_timeout(cfg.nplc)))
        if not ticket.ok:
            raise JobFailed(f"SMU did not accept NPLC {cfg.nplc:g}: {ticket.reason}")
        yield from self._sleep(0.5)

        setpoints = cfg.setpoints()
        while not io.stopping:
            self.pass_no += 1
            self.points = []
            self.progress = (0, len(setpoints))
            self.on_pass_start(self.pass_no)
            self.status = f"scanning (pass {self.pass_no})" if cfg.loop else "scanning"
            for idx, sp_amps in enumerate(setpoints):
                if io.stopping:
                    break
                point = yield from self._measure(float(sp_amps))
                if point is None:
                    break
                self.points.append(point)
                self.progress = (idx + 1, len(setpoints))
                self.on_point(point)
            if io.stopping:
                break
            self.passes_done += 1
            self.on_pass_done(self.pass_no, list(self.points))
            if not cfg.loop:
                break
            self.status = "back to the start for the next pass"
            io.send(SP_TAG, float(setpoints[0]))
            yield from self._sleep(2.0)

    def _measure(self, sp_amps):
        """One point: set, settle, sample. Returns (sp, magnet, beam, target kV, extraction kV),
        or None if a stop was requested meanwhile."""
        cfg, io = self.cfg, self.io
        ticket = io.send(SP_TAG, sp_amps)

        # 1. Dynamic settling: magnet within tolerance and the SMU range settled (at most 5 s)
        deadline = io.now() + SETTLE_TIMEOUT_S
        while True:
            if io.stopping:
                return None
            if ticket.done() and not ticket.ok:
                raise JobFailed(f"magnet refused {sp_amps:.3f} A: {ticket.reason}")
            rb_mag = self._live(RB_MAG_TAG)
            settled = self._live(SMU_SETTLED_TAG)
            if rb_mag is not None and abs(rb_mag - sp_amps) <= cfg.tolerance and settled == 1.0:
                break
            if io.now() >= deadline:
                if rb_mag is None or settled is None:
                    raise JobFailed("magnet or SMU telemetry is not live")
                break               # not settled in time: measure anyway, as before
            yield

        # 2. Static settling
        yield from self._sleep(cfg.settle_time)
        if io.stopping:
            return None

        # 3. Samples (all taken with the SMU range settled; stale readings never count)
        samples = []
        last_live = io.now()
        while len(samples) < cfg.samples:
            if io.stopping:
                return None
            settled = self._live(SMU_SETTLED_TAG)
            mag = self._live(RB_MAG_TAG)
            beam = self._live(RB_BEAM_TAG)
            if settled is None or mag is None or beam is None:
                if io.now() - last_live > STALE_LIMIT_S:
                    raise JobFailed("magnet or Faraday-cup readings stopped updating")
                yield from self._sleep(0.1)
                continue
            last_live = io.now()
            if settled != 1.0:          # the SMU changed range: start this point's samples again
                samples.clear()
                yield from self._sleep(0.1)
                continue
            t_kv, e_kv = self._live(TARGET_KV_TAG), self._live(EXT_KV_TAG)
            samples.append((mag, beam, math.nan if t_kv is None else t_kv, math.nan if e_kv is None else e_kv))
            yield from self._sleep(cfg.interval)
        if io.stopping:
            return None

        data = np.array(samples, dtype=float)
        if cfg.samples >= 10:           # trim 10% at each end
            trim = max(1, int(cfg.samples * 0.1))
            data = data[trim:-trim]
        mag, beam = float(np.mean(data[:, 0])), float(np.mean(data[:, 1]))
        t_kv = float(np.nanmean(data[:, 2])) if np.isfinite(data[:, 2]).any() else 0.0
        e_kv = float(np.nanmean(data[:, 3])) if np.isfinite(data[:, 3]).any() else 0.0
        return sp_amps, mag, beam, t_kv, e_kv

    def restore(self):
        """Put the SMU back to fast readings (whatever happened to the scan)."""
        self.status = "restoring the SMU integration time"
        ticket = yield from self._wait(self.io.send(NPLC_TAG, DEFAULT_NPLC, timeout=self.cfg.nplc_timeout(self.cfg.nplc)))
        return ticket


def strongest_peak(points, min_prominence: float = 0.1):
    """Magnet setpoint (A) of the most prominent beam-current peak of a pass, or None.
    Prominence is relative to the pass's current span, so it works at pA and at uA alike."""
    if len(points) < 3:
        return None
    from scipy.signal import find_peaks
    sp = np.array([p[0] for p in points], dtype=float)
    beam = np.array([p[2] for p in points], dtype=float)
    span = float(np.ptp(beam))
    if not np.isfinite(span) or span <= 0:
        return None
    peaks, props = find_peaks(beam, prominence=min_prominence * span)
    if not len(peaks):
        return None
    return float(sp[peaks[int(np.argmax(props["prominences"]))]])


def export_csv(points, base_path: str = "", when: datetime = None) -> str:
    """Write one pass as '<base>_<YYYYmmdd_HHMMSS>.csv'; returns the file's path."""
    base_path = (base_path or "").strip() or DEFAULT_EXPORT
    when = when or datetime.now()
    folder = os.path.dirname(os.path.abspath(base_path))
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{os.path.basename(base_path)}_{when.strftime('%Y%m%d_%H%M%S')}.csv")
    stamp = when.isoformat()
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADER)
        for point in points:
            writer.writerow([stamp] + list(point))
    return path


class MassScanJob(GeneratorJob):
    KIND = MASS_SCAN

    def __init__(self, ctx, run, cfg: ScanConfig):
        super().__init__(ctx, run, "mass scan")
        self.cfg = cfg
        self.points = PointLog()
        self.files = []
        self.scan = MassScan(cfg, io=self, on_point=self.points.add, on_pass_start=self.points.start_pass,
                             on_pass_done=self._pass_done)

    def body(self):
        c = self.cfg
        self.event(f"Mass Scan started: {c.start_a:g} A to {c.stop_a:g} A in {c.step_a:g} A steps "
                   f"(NPLC {c.nplc:g}{', looping' if c.loop else ''})")
        yield from self.scan.run()

    def cleanup(self):
        ticket = yield from self.scan.restore()
        if not ticket.ok:
            self.log.warning("SMU integration time not restored: %s", ticket.reason)

    def complete_message(self) -> str:
        return f"complete ({self.scan.progress[0]} points)"

    def _pass_done(self, pass_no, points):
        if not self.cfg.export or not points:
            return
        try:
            self.files.append(export_csv(points, self.cfg.export_path))
        except OSError as e:
            self.log.error("Mass scan pass %d not saved: %s", pass_no, e)
            self.event(f"Mass scan pass {pass_no} could not be saved: {e}", severity="ERROR")

    def running_status(self) -> str:
        return self.scan.status

    def cmd_fetch(self, value):
        pass_no = value.get("pass") if isinstance(value, dict) else value
        try:
            points = self.points.get(int(pass_no))
        except (TypeError, ValueError):
            return False, f"bad pass number {pass_no!r}", None
        if points is None:
            return False, f"pass {pass_no} is not kept any more", None
        return True, f"{len(points)} points", points

    def telemetry(self) -> dict:
        d = super().telemetry()
        k = self.KIND
        d.update({key(k, "config"): asdict(self.cfg), key(k, "pass"): self.points.pass_no,
                  key(k, "points"): len(self.points.current), key(k, "tail"): self.points.tail(),
                  key(k, "passes_done"): self.scan.passes_done, key(k, "progress"): list(self.scan.progress),
                  key(k, "file"): self.files[-1] if self.files else None})
        return d
