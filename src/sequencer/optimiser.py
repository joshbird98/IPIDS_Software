"""2D optimiser: raster two setpoints over a grid, recording a beam reading at every point,
then put both back where they were (or go to a point the operator picked on the map).

This is the HMI's former Optimizer2DWorker, moved into the sequencer. It holds leases on the
two subsystems it sweeps; the old version set their cmd_ctrl_mode to 1, which on PLC
subsystems meant "PLC Auto".
"""
import math
from dataclasses import asdict, dataclass

import numpy as np

from src.core import hard_caps
from src.sequencer.jobs import GeneratorJob
from src.sequencer.points import PointLog
from src.sequencer.protocol import OPTIMISER, key

DEFAULT_RB_TAG = "ion_beam.beamline.faraday.smu.rb_current"
STEPS_RANGE = (2, 500)
DWELL_RANGE = (0.05, 60.0)
X_SETTLE_S = 0.2            # after each new X value, before the Y sweep at it


def restore_tags(sp_tag: str) -> list:
    """Where to read a setpoint's current value: the device's own setpoint readback, then the
    measured readback."""
    base, leaf = sp_tag.rsplit(".", 1)
    out = []
    if leaf.startswith("sp_requested_"):
        out.append(f"{base}.{leaf.replace('sp_requested_', 'sp_actual_', 1)}")
    out.append(f"{base}.{leaf.replace('sp_requested_', 'rb_', 1).replace('cmd_', 'rb_', 1)}")
    return out


@dataclass
class OptimiserConfig:
    x_tag: str
    x_start: float
    x_stop: float
    x_steps: int
    y_tag: str
    y_start: float
    y_stop: float
    y_steps: int
    dwell: float = 0.25
    rb_tag: str = DEFAULT_RB_TAG

    @classmethod
    def from_value(cls, value, registry: dict) -> "OptimiserConfig":
        if not isinstance(value, dict):
            raise ValueError("optimiser settings must be a JSON object")
        unknown = sorted(set(value) - set(cls.__dataclass_fields__))
        if unknown:
            raise ValueError(f"unknown optimiser setting(s): {', '.join(unknown)}")
        try:
            cfg = cls(**value)
            for name in ("x_start", "x_stop", "y_start", "y_stop", "dwell"):
                setattr(cfg, name, float(getattr(cfg, name)))
            for name in ("x_steps", "y_steps"):
                v = getattr(cfg, name)
                if float(v) != int(v):
                    raise ValueError(f"{name} must be a whole number, not {v!r}")
                setattr(cfg, name, int(v))
        except TypeError as e:
            raise ValueError(f"missing or bad optimiser setting: {e}") from e
        if cfg.x_tag == cfg.y_tag:
            raise ValueError("X and Y are the same setpoint")
        for axis in ("x", "y"):
            tag = getattr(cfg, f"{axis}_tag")
            meta = registry.get(tag)
            if not meta or not meta.get("auto_controllable"):
                raise ValueError(f"{tag} is not a setpoint the optimiser may sweep")
            lo, hi = meta.get("min_val"), meta.get("max_val")
            if lo is None or hi is None:
                raise ValueError(f"no min/max limits for {tag} in the tag registry")
            start, stop = getattr(cfg, f"{axis}_start"), getattr(cfg, f"{axis}_stop")
            if not all(math.isfinite(v) for v in (start, stop)):
                raise ValueError(f"{axis.upper()} start/stop must be finite numbers")
            if start == stop:
                raise ValueError(f"{axis.upper()} start and stop are the same")
            cap = hard_caps.command_cap(tag)
            for v in (start, stop):
                if cap is not None and cap.breach(v):                    # said as the cap, with its reason
                    raise ValueError(cap.explain(f"{axis.upper()} {tag}", v))
                if not lo <= v <= hi:
                    raise ValueError(f"{axis.upper()} {v:g} is outside the allowed range [{lo:g}, {hi:g}] of {tag}")
            if not STEPS_RANGE[0] <= getattr(cfg, f"{axis}_steps") <= STEPS_RANGE[1]:
                raise ValueError(f"{axis.upper()} steps must be {STEPS_RANGE[0]}..{STEPS_RANGE[1]}")
        if not DWELL_RANGE[0] <= cfg.dwell <= DWELL_RANGE[1]:
            raise ValueError(f"dwell must be {DWELL_RANGE[0]:g}..{DWELL_RANGE[1]:g} s")
        if not cfg.rb_tag:
            raise ValueError("no readback to optimise")
        return cfg

    def within(self, axis: str, value: float, registry: dict) -> bool:
        meta = registry.get(getattr(self, f"{axis}_tag"), {})
        lo, hi = meta.get("min_val"), meta.get("max_val")
        return lo is not None and hi is not None and lo <= value <= hi


def starting_value(sp_tag: str, fresh, registry: dict):
    """The setpoint's live value now (to restore it afterwards), or None."""
    for tag in restore_tags(sp_tag):
        if tag in registry or not registry:
            value = fresh(tag)
            if value is not None:
                return value
    return None


class OptimiserJob(GeneratorJob):
    KIND = OPTIMISER

    def __init__(self, ctx, run, cfg: OptimiserConfig, restore: tuple):
        super().__init__(ctx, run, "optimiser")
        self.cfg = cfg
        self.restore = restore              # (x, y) to put back afterwards
        self.goto = None                    # (x, y) picked by the operator instead
        self.points = PointLog()
        self.progress = (0, cfg.x_steps * cfg.y_steps)
        self._started_text = ""

    def _labels(self):
        reg = self.ctx.registry
        return (reg.get(self.cfg.x_tag, {}).get("default_label", self.cfg.x_tag),
                reg.get(self.cfg.y_tag, {}).get("default_label", self.cfg.y_tag))

    def body(self):
        cfg = self.cfg
        x_label, y_label = self._labels()
        self._started_text = f"Scan of {x_label} against {y_label}"
        self.event(f"{self._started_text} started", marker_color="#00E5FF")
        self.points.start_pass(1)
        xs = np.linspace(cfg.x_start, cfg.x_stop, cfg.x_steps)
        ys = np.linspace(cfg.y_start, cfg.y_stop, cfg.y_steps)
        self.status = "2D raster sweep"
        done = 0
        for i, vx in enumerate(xs):
            if self.stopping:
                break
            tx = self.send(cfg.x_tag, float(vx))
            yield from self.sleep(X_SETTLE_S)
            for j, vy in enumerate(ys):
                if self.stopping:
                    break
                ty = self.send(cfg.y_tag, float(vy))
                yield from self.sleep(cfg.dwell)
                if self.stopping:
                    break
                for ticket, axis in ((tx, "X"), (ty, "Y")):
                    if ticket.done() and not ticket.ok:
                        self.fail(f"{axis} setpoint refused: {ticket.reason}")
                # Live value only: a stale reading would put a fake flat region in the map
                value = self.fresh_float(cfg.rb_tag)
                if value is None:
                    self.fail(f"readback {cfg.rb_tag} is not live")
                self.points.add((i, j, value))
                done += 1
                self.progress = (done, cfg.x_steps * cfg.y_steps)

    def cleanup(self):
        x, y = self.goto or self.restore
        self.status = (f"going to X={x:g}, Y={y:g}" if self.goto else "restoring the pre-scan values")
        tx, ty = self.send(self.cfg.x_tag, float(x)), self.send(self.cfg.y_tag, float(y))
        for ticket in (tx, ty):
            yield from self.wait_reply(ticket)
            if not ticket.ok:
                self.log.warning("Optimiser could not set %s back: %s", ticket.tag, ticket.reason)
        yield from self.sleep(0.5, interruptible=False)

    def finish(self, state, message):
        super().finish(state, message)
        if self._started_text:
            self.event(f"{self._started_text}: {message}", marker_color="#00E5FF")

    def complete_message(self) -> str:
        return f"complete ({self.progress[0]} points)"

    def cmd_goto(self, value):
        try:
            x, y = float(value["x"]), float(value["y"])
        except (TypeError, KeyError, ValueError):
            return False, "goto needs {\"x\": number, \"y\": number}", None
        if not self.active or self._phase != "body":
            return False, "no sweep is running - set the values directly", None
        reg = self.ctx.registry
        for axis, v in (("x", x), ("y", y)):
            cap = hard_caps.command_cap(getattr(self.cfg, f"{axis}_tag"))
            if cap is not None and cap.breach(v):
                return False, cap.explain(f"{axis.upper()}", v), None
        if not (self.cfg.within("x", x, reg) and self.cfg.within("y", y, reg)):
            return False, f"({x:g}, {y:g}) is outside the setpoints' allowed ranges", None
        self.goto = (x, y)
        self.stop(f"Stopped: going to X={x:g}, Y={y:g} instead of restoring")
        return True, "stopping the sweep there", None

    def cmd_fetch(self, value):
        points = self.points.get(1)
        return True, f"{len(points)} points", points

    def telemetry(self) -> dict:
        d = super().telemetry()
        k = self.KIND
        d.update({key(k, "config"): asdict(self.cfg), key(k, "pass"): self.points.pass_no,
                  key(k, "points"): len(self.points.current), key(k, "tail"): self.points.tail(),
                  key(k, "progress"): list(self.progress), key(k, "restore"): list(self.restore),
                  key(k, "goto"): list(self.goto) if self.goto else None})
        return d
