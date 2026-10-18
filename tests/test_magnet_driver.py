"""The Modbus magnet PSU driver (src/drivers/magnet_modbus.py) never runs without its nominal
ratings: setpoints are sent as a fraction of them, and it used to carry on with 1.0 V / 1.0 A when
the ratings read failed - turning any request of 1 A or more into a full-scale current."""
import struct

import pytest

from src.drivers import magnet_modbus as mm


def regs(*floats):
    out = []
    for f in floats:
        hi, lo = struct.unpack(">HH", struct.pack(">f", f))
        out += [hi, lo]
    return out


class Response:
    def __init__(self, registers=None, error=False):
        self.registers, self._error = registers, error

    def isError(self):
        return self._error


class FakeClient:
    ratings = None          # set per test

    def __init__(self, *a, **k):
        self.coils, self.closed = [], False

    def connect(self):
        return True

    def close(self):
        self.closed = True

    def read_holding_registers(self, address, count):
        assert (address, count) == (121, 6)
        return FakeClient.ratings

    def write_coil(self, address, value):
        self.coils.append((address, value))


@pytest.fixture
def psu(monkeypatch):
    monkeypatch.setattr(mm, "ModbusTcpClient", FakeClient)
    return mm.MagnetModbusProtocol("10.0.0.1")


def test_connects_with_its_ratings(psu):
    FakeClient.ratings = Response(regs(30.0, 100.0, 3000.0))
    assert psu.connect() and psu.connected
    assert (psu.nom_v, psu.nom_i, psu.nom_p) == (30.0, 100.0, 3000.0)
    assert psu._to_counts(10.0, psu.nom_i) == int(10.0 * mm.FULL_SCALE / 100.0)      # 10 % of full scale


@pytest.mark.parametrize("answer", [Response(error=True), Response(regs(30.0, 0.0, 3000.0)),
                                    Response(regs(30.0, float("nan"), 3000.0)), Response([1, 2, 3])])
def test_no_ratings_no_connection(psu, answer):
    FakeClient.ratings = answer
    assert not psu.connect() and not psu.connected
    assert psu.client.closed and psu.client.coils == []          # nothing written to the PSU
    with pytest.raises(ConnectionError):
        psu.set_current(10.0)
