"""Simulated Spellman MPD supplies behind a Waveshare RS485 bridge.

Speaks the same request/response strings as SpellmanMPDProtocol.transaction()
(e.g. 'M0?' -> '0109M0=12000.0'), keeps a per-address model of output enable,
setpoints and a lagging output, and loses its state when the bus is severed
(the safety relay de-energises the supplies), just like the real units.

Injected faults (IPIDS_SIM_FAULTS):
    spellman_no_reply   every transaction times out (supply unpowered / cable out)
    spellman_refuse     set commands are answered with an error ('*')
"""
import threading
import time

from src.sim import Clock, first_order, injected_faults, noisy

SOCKET_TIMEOUT = 1.0


class _Device:
    def __init__(self):
        self.reset()

    def reset(self):
        self.enabled = False
        self.v_set = 0.0      # volts
        self.i_set = 0.0      # microamps
        self.v_out = 0.0
        self.i_out = 0.0
        self.fault_bits = 0

    def update(self, dt):
        target = self.v_set if self.enabled else 0.0
        self.v_out = first_order(self.v_out, target, dt, 0.3)
        # ~1 uA per kV of load, limited by the current setpoint
        load = self.v_out / 1000.0
        self.i_out = min(load, self.i_set) if self.i_set > 0 else 0.0

    def status_word(self):
        word = 0xC0  # hardware + software enable permitted
        if self.enabled:
            word |= 0x01
        return word | self.fault_bits


class _SimBus:
    """One RS485 bus; device state lives here so it survives TCP reconnects but not power cycles."""

    def __init__(self):
        self.devices = {}
        self.powered = True
        self.lock = threading.Lock()
        self.clock = Clock()


_BUSES = {}


class _FakeSocket:
    def __init__(self, bus):
        self.bus = bus

    def close(self):
        # The service severs the bus when the safety relay drops: the supplies lose power.
        with self.bus.lock:
            self.bus.powered = False
            for d in self.bus.devices.values():
                d.reset()


class FakeMPDProtocol:
    def __init__(self, ip: str, port: int, bus_id: str):
        self.ip = ip
        self.port = port
        self.bus_id = bus_id
        self.bus = _BUSES.setdefault(bus_id, _SimBus())
        self.sock = None
        self.connected = False

    def connect(self):
        with self.bus.lock:
            self.bus.powered = True
        self.sock = _FakeSocket(self.bus)
        self.connected = True

    def transaction(self, address: str, dev_type: str, cmd: str, operator: str = "", data: str = ""):
        if not self.connected:
            return None
        faults = injected_faults()
        if "spellman_no_reply" in faults:
            time.sleep(SOCKET_TIMEOUT)
            return None
        time.sleep(0.005)
        with self.bus.lock:
            dev = self.bus.devices.setdefault(address, _Device())
            dev.update(self.bus.clock.dt())
            head = f"{address}{dev_type}{cmd}"
            if operator == "?":
                if cmd == "M0":
                    return f"{head}={max(0.0, noisy(dev.v_out, 0.0005)):.1f}"
                if cmd == "M1":
                    return f"{head}={max(0.0, noisy(dev.i_out, 0.002)):.1f}"
                if cmd == "SR":
                    return f"{head}={dev.status_word():02X}"
                if cmd == "V1":
                    return f"{head}={dev.v_set:.1f}"
                if cmd == "I1":
                    return f"{head}={dev.i_set:.1f}"
                return f"{head}*"
            if operator == "=":
                if "spellman_refuse" in faults:
                    return f"{head}*"
                try:
                    if cmd == "V1":
                        dev.v_set = max(0.0, float(data))
                    elif cmd == "I1":
                        dev.i_set = max(0.0, float(data))
                    elif cmd == "EN":
                        dev.enabled = data.strip() == "1"
                    elif cmd == "CF":
                        dev.fault_bits = 0
                    else:
                        return f"{head}*"
                except ValueError:
                    return f"{head}*"
                return f"{head}={data}"
            return None
