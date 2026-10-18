"""The safety-relay permissive used by the Spellman, magnet, X-steerer and SMU services.

The review found that only the newest PLC message used to count: a relay trip-and-reset
between two checks (the SMU's loop can be blocked for seconds by an unresponsive SMU) went
unseen, and the SMU could then restore a bias from before the trip.
"""
import time

import orjson
import pytest
import zmq

from src.core.relay_watch import RELAY_TAG, RelayWatch


class FakeSub:
    def __init__(self):
        self.q = []

    def push(self, relay, ts):
        self.q.append([b"PLC_DATA", orjson.dumps({RELAY_TAG: relay, "timestamp": ts})])

    def recv_multipart(self, flags=0):
        if not self.q:
            raise zmq.Again()
        return self.q.pop(0)


@pytest.fixture
def watch():
    clock = {"t": 100.0}
    sub = FakeSub()
    w = RelayWatch(sub, max_age=1.0, clock=lambda: clock["t"])
    return w, sub, clock


def test_relay_on_with_current_data(watch):
    w, sub, clock = watch
    assert not w.check()                       # nothing heard yet
    sub.push(1.0, 99.95)
    assert w.check() and w.fresh()


def test_trip_and_reset_between_two_checks_is_a_trip(watch):
    w, sub, clock = watch
    sub.push(1.0, 99.9)
    assert w.check()
    for i, relay in enumerate([1.0, 0.0, None, 1.0]):   # off, PLC lost, back on - all before the next check
        sub.push(relay, 100.0 + 0.1 * i)
    clock["t"] = 100.4
    assert not w.check()
    sub.push(1.0, 100.5)
    clock["t"] = 100.5
    assert w.check()                           # a trip only for the check that saw it


def test_gap_in_the_plc_data_is_a_trip(watch):
    w, sub, clock = watch
    sub.push(1.0, 100.0)
    assert w.check()
    sub.push(1.0, 102.0)                       # 2 s without PLC data (e.g. PLC service restarted)
    clock["t"] = 102.0
    assert not w.check()
    sub.push(1.0, 102.1)
    clock["t"] = 102.1
    assert w.check()


def test_silence_is_off(watch):
    w, sub, clock = watch
    sub.push(1.0, 100.0)
    assert w.check()
    clock["t"] = 101.2
    assert not w.check()


def test_stale_queued_message_is_not_fresh(watch):
    """A message that sat in the queue is 'received now' but carries the PLC's old timestamp."""
    w, sub, clock = watch
    sub.push(1.0, 95.0)
    assert w.check() and not w.fresh()


def test_smu_reconnect_after_unseen_trip_does_not_restore_bias(monkeypatch):
    """The review's scenario on the real SMU service object (its loop not running)."""
    monkeypatch.setenv("SINDIE_SIMULATE", "1")
    from src.services.service_smu import SP_BIAS, SmuMicroservice
    svc = SmuMicroservice()
    try:
        sub = FakeSub()
        svc.relay.sock = sub
        sub.push(1.0, time.time())
        assert svc.update_relay()
        svc.sp_cache[SP_BIAS] = 50.0                 # bias live when the SMU stopped answering
        svc.hw.output_state = True
        now = time.time()
        for i, relay in enumerate([0.0, None, 1.0]):  # E-stop pressed and reset while the loop was blocked
            sub.push(relay, now - 0.3 + 0.1 * i)
        assert not svc.update_relay()
        assert svc.sp_cache[SP_BIAS] == 0.0          # the trip reset the bias
        sub.push(1.0, time.time())
        assert svc.connect_device()                  # the SMU answers again (bias was on before)
        svc.after_connect(0)
        assert svc.hw.output_state is False and svc.sp_cache[SP_BIAS] == 0.0
    finally:
        svc.shutdown()


def _close(svc):
    svc.shutdown()


def test_magnet_trip_between_checks_forces_output_off(monkeypatch):
    monkeypatch.setenv("SINDIE_SIMULATE", "1")
    from src.sim import magnet as simmag
    simmag._STATE.clear()
    from src.services.service_magnet_psu import MagnetMicroservice
    svc = MagnetMicroservice()
    try:
        sub = FakeSub()
        svc.relay.sock = sub
        assert svc.hw.connect()
        sub.push(1.0, time.time())
        assert svc.update_relay()
        svc.hw.set_current(20.0)
        svc.hw.set_output(True)
        now = time.time()
        sub.push(0.0, now - 0.2)                     # tripped and reset before the next check
        sub.push(1.0, now - 0.1)
        assert not svc.update_relay()
        svc.force_safe_outputs()
        assert svc.hw.psu.output is False and svc.hw.psu.i_set == 0.0
    finally:
        _close(svc)


def test_spellman_trip_between_checks_counts_as_power_loss(monkeypatch):
    monkeypatch.setenv("SINDIE_SIMULATE", "1")
    from src.services.service_spellman_mpd import SpellmanMicroservice
    svc = SpellmanMicroservice()
    try:
        sub = FakeSub()
        svc.relay.sock = sub
        sub.push(1.0, time.time())
        assert svc.update_relay()
        now = time.time()
        sub.push(None, now - 0.2)                    # PLC lost, then back with the relay on
        sub.push(1.0, now - 0.1)
        assert not svc.update_relay()               # one cycle off: buses severed, state reset
        sub.push(1.0, time.time())
        assert svc.update_relay()
    finally:
        _close(svc)
