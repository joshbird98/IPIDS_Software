import pytest

from src.core.telemetry_cache import TelemetryCache


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


def feed(cache, clock, topic, payload, n=10, dt=0.1):
    for _ in range(n):
        clock.t += dt
        cache.ingest(topic, payload)


def test_fresh_values_visible_and_stale_ones_dropped(clock):
    c = TelemetryCache(clock=clock)
    feed(c, clock, "SPELLMAN_DATA", {"ion_beam.source.einzel.rb_voltage": 10.0})
    assert c.view()["ion_beam.source.einzel.rb_voltage"] == 10.0
    assert c.get_fresh("ion_beam.source.einzel.rb_voltage") == 10.0
    clock.t += 3.0   # service went silent
    assert "ion_beam.source.einzel.rb_voltage" not in c.view()
    assert c.get_fresh("ion_beam.source.einzel.rb_voltage") is None
    assert c.get("ion_beam.source.einzel.rb_voltage") == 10.0   # raw value still there for diagnostics


def test_slow_publishers_get_a_longer_allowance(clock):
    c = TelemetryCache(clock=clock)
    feed(c, clock, "SMU_DATA", {"ion_beam.beamline.faraday.smu.rb_current": 1e-9}, n=5, dt=1.5)  # high NPLC
    clock.t += 4.0
    assert c.is_fresh("ion_beam.beamline.faraday.smu.rb_current")   # 5 x 1.5 s allowance
    clock.t += 5.0
    assert not c.is_fresh("ion_beam.beamline.faraday.smu.rb_current")


def test_null_means_unknown(clock):
    c = TelemetryCache(clock=clock)
    feed(c, clock, "SPELLMAN_DATA", {"ion_beam.source.einzel.stat_enabled": 1.0})
    feed(c, clock, "SPELLMAN_DATA", {"ion_beam.source.einzel.stat_enabled": None}, n=1)
    assert "ion_beam.source.einzel.stat_enabled" not in c.view()
    assert not c.is_fresh("ion_beam.source.einzel.stat_enabled")


def test_generic_keys_namespaced_and_plc_connected_derived(clock):
    c = TelemetryCache(clock=clock)
    feed(c, clock, "PLC_DATA", {"system.connected": 0.0, "timestamp": 1.0})
    feed(c, clock, "SRC_TURBO_DATA", {"system.connected": 1.0, "timestamp": 2.0})  # turbo's own flag
    v = c.view()
    assert v["system.connected"] is False          # the PLC's, not the turbo's
    assert v["PLC_DATA/system.connected"] == 0.0
    assert v["SRC_TURBO_DATA/system.connected"] == 1.0
    assert v["pump.connected"] is True
    feed(c, clock, "PLC_DATA", {"system.connected": 1.0})
    assert c.view()["system.connected"] is True
    clock.t += 5
    assert c.view()["system.connected"] is False   # PLC service silent
    assert c.view()["pump.connected"] is False


def test_snapshot_for_worker_threads(clock):
    c = TelemetryCache(clock=clock)
    feed(c, clock, "MAGNET_DATA", {"ion_beam.beamline.magnet.rb_current": 12.5})
    feed(c, clock, "SMU_DATA", {"ion_beam.beamline.faraday.smu.rb_current": 3e-9}, n=2)
    clock.t += 1.0
    snap = c.snapshot()
    assert snap.get_fresh("ion_beam.beamline.magnet.rb_current") == 12.5
    assert snap.age("ion_beam.beamline.magnet.rb_current") == pytest.approx(1.2)  # 2 SMU msgs + 1 s
    clock.t += 10
    assert snap.is_fresh("ion_beam.beamline.magnet.rb_current")   # frozen at the time it was taken
    assert not c.snapshot().is_fresh("ion_beam.beamline.magnet.rb_current")
    read = c.reader()
    assert read("ion_beam.beamline.magnet.rb_current") is None
    assert read("never.seen") is None
