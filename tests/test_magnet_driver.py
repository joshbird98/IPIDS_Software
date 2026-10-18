"""The Modbus magnet PSU driver (src/drivers/magnet_modbus.py) never scales a setpoint with
unknown nominal ratings: it used to carry on with 1 V / 1 A when the ratings read failed, turning
any request of 1 A or more into a full-scale current. Without ratings it still connects, so the
supply can always be set to zero and switched off (the X steerer's software interlock relies on
that), but anything else is refused until the ratings arrive."""
import struct

import pytest
from pymodbus.exceptions import ModbusIOException

from src.drivers import magnet_modbus as mm

NO_ANSWER = "no answer"     # FakeClient.ratings: the read raises, as pymodbus does after its retries time out


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
    ratings = None                  # the answer to the ratings read, set per test
    telemetry = Response([0, 0x0080, 26214, 5242])     # output on; half of nominal V, a tenth of nominal A
    ratings_reads = 0

    def __init__(self, *a, **k):
        self.writes, self.closed = [], False

    def connect(self):
        return True

    def close(self):
        self.closed = True

    def read_holding_registers(self, address, count):
        if (address, count) == (121, 6):
            FakeClient.ratings_reads += 1
            answer = FakeClient.ratings
        else:
            answer = FakeClient.telemetry
        if answer is NO_ANSWER:
            raise ModbusIOException("No response received after 3 retries")
        return answer

    def write_coil(self, address, value):
        self.writes.append(("coil", address, value))
        return Response()

    def write_register(self, address, value):
        self.writes.append(("reg", address, value))
        return Response()


class Clock:
    t = 100.0

    def __call__(self):
        return self.t


@pytest.fixture
def psu(monkeypatch):
    monkeypatch.setattr(mm, "ModbusTcpClient", FakeClient)
    FakeClient.ratings_reads = 0
    clock = Clock()
    p = mm.MagnetModbusProtocol("10.0.0.1", clock=clock)
    p.clock = clock
    return p


def test_with_its_ratings_everything_works(psu):
    FakeClient.ratings = Response(regs(30.0, 100.0, 3000.0))
    assert psu.connect() and psu.connected and psu.ratings_ok
    assert (psu.nom_v, psu.nom_i) == (30.0, 100.0)
    psu.set_current(10.0)
    assert psu.client.writes[-1] == ("reg", 501, int(10.0 * mm.FULL_SCALE / 100.0))      # 10 % of full scale
    v, i, on, alarms = psu.read_telemetry()
    assert v == pytest.approx(15.0, abs=0.01) and i == pytest.approx(10.0, abs=0.01) and on


@pytest.mark.parametrize("answer", [Response(error=True), Response(regs(30.0, 0.0, 3000.0)),
                                    Response(regs(30.0, float("nan"), 3000.0)), Response([1, 2, 3]), NO_ANSWER])
def test_without_ratings_only_zero_and_off(psu, answer):
    FakeClient.ratings = answer
    assert psu.connect() and psu.connected and not psu.ratings_ok
    assert psu.client.writes[:2] == [("coil", 402, True), ("coil", 411, True)]
    for refused in (lambda: psu.set_current(10.0), lambda: psu.set_voltage(5.0), lambda: psu.set_output(True)):
        with pytest.raises(mm.RatingsUnknown):
            refused()
    sent = len(psu.client.writes)
    psu.set_current(0.0)
    psu.set_voltage(0.0)
    psu.set_output(False)                                        # switching off always works
    assert psu.client.writes[sent:] == [("reg", 501, 0), ("reg", 500, 0), ("coil", 405, False)]
    assert psu.read_telemetry()[:3] == (None, None, True)        # readbacks unknown, output state known


def test_ratings_that_arrive_later_are_picked_up(psu):
    FakeClient.ratings = Response(error=True)
    assert psu.connect() and not psu.ratings_ok
    FakeClient.ratings = Response(regs(30.0, 100.0, float("nan")))      # the power rating isn't needed
    psu.clock.t += 1.0
    assert psu.read_telemetry()[0] is None and not psu.ratings_ok       # not retried every poll
    psu.clock.t += mm.RATINGS_RETRY_S
    v, i, on, _ = psu.read_telemetry()
    assert psu.ratings_ok and i == pytest.approx(10.0, abs=0.01)
    psu.set_current(5.0)


def test_unanswered_ratings_read_is_not_retried_from_the_poll(psu):
    """An unanswered request holds the poll loop ~2 s - as long as the manager's hung-service timeout -
    so after one the ratings wait for the next reconnect instead of being retried every few seconds."""
    FakeClient.ratings = Response(error=True)
    assert psu.connect() and FakeClient.ratings_reads == 1
    FakeClient.ratings = NO_ANSWER
    psu.clock.t += mm.RATINGS_RETRY_S
    assert psu.read_telemetry()[:3] == (None, None, True) and psu.connected      # the link itself is fine
    assert FakeClient.ratings_reads == 2 and not psu.ratings_retrying
    psu.clock.t += 10 * mm.RATINGS_RETRY_S
    psu.read_telemetry()
    assert FakeClient.ratings_reads == 2 and not psu.ratings_ok                    # not tried again
    FakeClient.ratings = Response(regs(30.0, 100.0, 3000.0))
    assert psu.connect() and psu.ratings_ok and FakeClient.ratings_reads == 3      # a reconnect reads them


def test_a_silent_psu_costs_one_unanswered_request_per_poll(psu, monkeypatch):
    """If the PSU stops answering just when a ratings retry is due, the poll waits for the telemetry
    read only (~2 s), not for the ratings read on top of it."""
    FakeClient.ratings = Response(error=True)
    assert psu.connect() and FakeClient.ratings_reads == 1
    psu.clock.t += mm.RATINGS_RETRY_S
    FakeClient.ratings = NO_ANSWER
    monkeypatch.setattr(FakeClient, "telemetry", NO_ANSWER)
    assert psu.read_telemetry() is None and not psu.connected
    assert FakeClient.ratings_reads == 1


def test_no_answer_at_all_is_a_failed_connect(psu, monkeypatch):
    FakeClient.ratings = Response(regs(30.0, 100.0, 3000.0))

    def silent(self, address, value):
        raise ModbusIOException("No response received after 3 retries")
    monkeypatch.setattr(FakeClient, "write_coil", silent)
    assert not psu.connect() and not psu.connected
