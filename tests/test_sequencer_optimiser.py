"""The 2D optimiser as a sequencer job (src/sequencer/optimiser.py), and scan points between the
sequencer and the HMI (src/sequencer/points.py)."""
import pytest

from src.core import commands as C
from src.sequencer.optimiser import OptimiserConfig, OptimiserJob, restore_tags, starting_value
from src.sequencer.points import TAIL, PointFollower, PointLog
from src.sequencer.protocol import COMPLETE, FAILED, STOPPED, key
from tests.sequencer_world import MAGNET, SMU, STEERER, World

X, Y = f"{MAGNET}.sp_requested_current", f"{STEERER}.sp_requested_current"
REGISTRY = {X: {"auto_controllable": True, "min_val": 0.0, "max_val": 95.0, "default_label": "Magnet"},
            Y: {"auto_controllable": True, "min_val": 0.0, "max_val": 80.0, "default_label": "Steerer"},
            f"{MAGNET}.sp_actual_current": {}, f"{STEERER}.sp_actual_current": {},
            f"{MAGNET}.rb_current": {}, "ion_beam.source.target.sp_requested_voltage": {"min_val": 0, "max_val": 10}}


def cfg(**kw):
    value = {"x_tag": X, "x_start": 24.0, "x_stop": 26.0, "x_steps": 3, "y_tag": Y, "y_start": 1.0, "y_stop": 2.0,
             "y_steps": 2, "dwell": 0.1, "rb_tag": f"{SMU}.rb_current"}
    value.update(kw)
    return OptimiserConfig.from_value(value, REGISTRY)


def test_sweep_records_the_grid_and_restores_both_axes():
    world = World(REGISTRY)
    job = OptimiserJob(world.ctx, 5, cfg(), restore=(10.0, 5.0))
    world.run_until(job, lambda: job.state == COMPLETE)
    grid = {(int(i), int(j)) for i, j, _ in job.points.current}
    assert grid == {(i, j) for i in range(3) for j in range(2)}
    assert world.devices["magnet"].values[X] == 10.0 and world.devices["steerer_magnet"].values[Y] == 5.0
    assert not any(t.endswith("cmd_ctrl_mode") for t, *_ in world.devices["magnet"].log)
    world.step(job)
    assert world.devices["magnet"].leases.active(MAGNET) is None
    assert job.telemetry()[key("optimiser", "progress")] == [6, 6]
    markers = [e for e in world.events if e[0] == "USER_MARKER"]
    assert markers[0][2] == "Scan of Magnet against Steerer started" and "complete (6 points)" in markers[-1][2]


def test_goto_ends_the_sweep_at_the_chosen_point():
    world = World(REGISTRY)
    job = OptimiserJob(world.ctx, 5, cfg(x_steps=10, y_steps=10), restore=(10.0, 5.0))
    world.run_until(job, lambda: len(job.points.current) >= 3)
    assert not job.handle("goto", {"x": 200.0, "y": 1.0})[0]           # out of range
    assert job.handle("goto", {"x": 25.5, "y": 1.5})[0]
    world.run_until(job, lambda: job.state == STOPPED)
    assert world.devices["magnet"].values[X] == 25.5 and world.devices["steerer_magnet"].values[Y] == 1.5
    assert not job.handle("goto", {"x": 25.0, "y": 1.0})[0]            # nothing running any more


def test_dead_readback_fails_but_still_restores():
    world = World(REGISTRY)
    job = OptimiserJob(world.ctx, 5, cfg(rb_tag="ion_beam.nowhere.rb_current"), restore=(10.0, 5.0))
    world.run_until(job, lambda: job.state == FAILED)
    assert "not live" in job.message and world.devices["magnet"].values[X] == 10.0


def test_refused_axis_fails_the_sweep():
    world = World(REGISTRY)
    world.devices["steerer_magnet"].handler = lambda tag, value: "steerer interlocked"
    job = OptimiserJob(world.ctx, 5, cfg(), restore=(10.0, 5.0))
    world.run_until(job, lambda: job.state == FAILED)
    assert "Y setpoint refused: steerer interlocked" in job.message


@pytest.mark.parametrize("change, why", [
    ({"y_tag": X}, "same setpoint"), ({"x_tag": "ion_beam.source.target.sp_requested_voltage"}, "may sweep"),
    ({"x_stop": 99.0}, "outside the allowed range"), ({"x_steps": 1}, "steps must be"),
    ({"dwell": 0.0}, "dwell"), ({"y_start": 2.0}, "start and stop are the same"),
])
def test_bad_optimiser_settings(change, why):
    with pytest.raises(ValueError, match=why):
        cfg(**change)


def test_starting_value_prefers_the_devices_own_setpoint():
    assert restore_tags(X) == [f"{MAGNET}.sp_actual_current", f"{MAGNET}.rb_current"]
    live = {f"{MAGNET}.sp_actual_current": 12.0, f"{MAGNET}.rb_current": 11.9}
    assert starting_value(X, live.get, REGISTRY) == 12.0
    assert starting_value(X, {f"{MAGNET}.rb_current": 11.9}.get, REGISTRY) == 11.9
    assert starting_value(X, {}.get, REGISTRY) is None


# --------------------------------------------------------------------------- points
class Ticket:
    def __init__(self, data=None, ok=True):
        self.data, self.ok, self._done = data, ok, False

    def done(self):
        return self._done


def test_point_log_tail_and_passes():
    log = PointLog()
    log.start_pass(1)
    for i in range(TAIL + 5):
        log.add((i, 2 * i))
    tail = log.tail()
    assert len(tail) == TAIL and tail[0] == [5, 5.0, 10.0] and tail[-1][0] == TAIL + 4
    log.start_pass(2)
    log.add((1, 1))
    assert len(log.get(1)) == TAIL + 5 and log.get(2) == [[1.0, 1.0]] and log.get(3) is None


def test_follower_appends_and_fills_gaps_by_fetching():
    clock = [0.0]
    fetched = []

    def fetch(run, pass_no):
        fetched.append((run, pass_no))
        return Ticket([[0.0], [1.0], [2.0], [3.0]])

    f = PointFollower(fetch, clock=lambda: clock[0])
    assert f.update(1, 1, 2, [[0, 0.0], [1, 1.0]]) == [("new", None), ("append", [[0.0], [1.0]])]
    assert f.update(1, 1, 2, [[0, 0.0], [1, 1.0]]) == []                    # nothing new
    assert f.update(1, 1, 3, [[1, 1.0], [2, 2.0]]) == [("append", [[2.0]])]
    # missed point 3 and joined at 4: fetch the whole pass
    assert f.update(1, 1, 5, [[4, 4.0]]) == [] and fetched == [(1, 1)]
    f._ticket._done = True
    assert f.poll() == [("replace", [[0.0], [1.0], [2.0], [3.0]])]
    assert f.update(1, 1, 5, [[4, 4.0]]) == [("append", [[4.0]])]
    assert f.update(1, 2, 1, [[0, 9.0]]) == [("new", None), ("append", [[9.0]])]   # next pass
    assert f.update(2, 1, 0, []) == [("new", None)]                                 # next run


def test_follower_does_not_hammer_a_refusing_sequencer():
    clock = [0.0]
    calls = []

    def fetch(run, pass_no):
        calls.append(clock[0])
        t = Ticket(None, ok=False)
        t._done = True
        return t

    f = PointFollower(fetch, clock=lambda: clock[0])
    for _ in range(10):
        f.update(1, 1, 100, [[99, 1.0]])
        clock[0] += 0.1
    assert len(calls) == 1
    clock[0] += 2.0
    f.update(1, 1, 100, [[99, 1.0]])
    assert len(calls) == 2
