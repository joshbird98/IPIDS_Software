"""Simulated Keysight B2900 SMU reading a Faraday cup.

Subclasses the service's SCPI driver and replaces only the socket layer, so the
service's ranging, NPLC and telemetry code runs for real. The beam current is a
small mass spectrum: it listens to the magnet and PLC telemetry and puts
Gaussian peaks where the HMI mass-scan calibration (k = 1391.9, offset 0.223 A)
expects them, so a mass scan in the simulator finds O, Si-28/29/30 and friends.

Injected faults (IPIDS_SIM_FAULTS): smu_disconnect.
"""
import math
import random
import threading
import time

import orjson
import zmq

from src.sim import injected_faults

K_FACTOR = 1391.9
I_OFFSET = 0.223
PEAK_SIGMA_A = 0.06
# amu -> peak height in amps (negative-ion sputter source on a silicon target, roughly)
SPECIES = {12: 4e-9, 16: 30e-9, 17: 8e-9, 24: 3e-9, 28: 120e-9, 29: 6.5e-9, 30: 4.5e-9, 56: 2e-9}


class _BeamModel:
    """Background subscriber holding the latest magnet current and beam energy."""

    def __init__(self):
        from src.core.network_map import ZMQ_PORT_MAGNET_PUB, ZMQ_PORT_PLC_PUB
        self.mag_a = 0.0
        self.energy_kev = 0.0
        self._t = threading.Thread(target=self._run, args=(ZMQ_PORT_MAGNET_PUB, ZMQ_PORT_PLC_PUB),
                                   name="sim-beam", daemon=True)
        self._t.start()

    def _run(self, mag_ep, plc_ep):
        ctx = zmq.Context()
        sub = ctx.socket(zmq.SUB)
        sub.setsockopt(zmq.SUBSCRIBE, b"")
        sub.connect(mag_ep)
        sub.connect(plc_ep)
        while True:
            if not sub.poll(500):
                continue
            try:
                d = orjson.loads(sub.recv_multipart()[1])
            except (ValueError, IndexError):
                continue
            if "ion_beam.beamline.magnet.rb_current" in d:
                self.mag_a = d["ion_beam.beamline.magnet.rb_current"] or 0.0
            if "ion_beam.source.extraction.rb_voltage" in d:
                ext = d.get("ion_beam.source.extraction.rb_voltage") or 0.0
                tgt = d.get("ion_beam.source.target.rb_voltage") or 0.0
                self.energy_kev = ext + tgt

    def beam_current(self):
        e_ev = self.energy_kev * 1000.0
        if e_ev < 1000.0:   # no extraction -> no beam
            return 0.0
        total = 0.0
        for amu, height in SPECIES.items():
            centre = math.sqrt(amu * e_ev / K_FACTOR) - I_OFFSET
            total += height * math.exp(-((self.mag_a - centre) ** 2) / (2 * PEAK_SIGMA_A ** 2))
        return total


_BEAM = None


def beam():
    global _BEAM
    if _BEAM is None:
        _BEAM = _BeamModel()
    return _BEAM


class _NullSock:
    def settimeout(self, t):
        pass

    def close(self):
        pass


def make_fake_smu(base_class):
    """Build the fake as a subclass of the service's own SmuScpiProtocol."""

    class FakeSmuProtocol(base_class):
        def __init__(self, ip, port=5025):
            super().__init__(ip, port)
            self._volt = 0.0
            self._range = 10e-9
            self._nplc = 1.0
            beam()

        def connect(self):
            if "smu_disconnect" in injected_faults():
                self.connected = False
                return False
            self.sock = _NullSock()
            self.connected = True
            self._volt = 0.0   # the real connect() sends *RST: source back to 0 V
            self.set_nplc(self.current_nplc)
            self.timeout_counter = 0
            return True

        def send_cmd(self, cmd):
            if not self.connected:
                return
            c = cmd.upper()
            try:
                if c.startswith(":SOUR:VOLT ") and "PROT" not in c and "MODE" not in c:
                    self._volt = float(cmd.split()[-1])
                elif c.startswith(":SENS:CURR:RANG ") and "AUTO" not in c:
                    self._range = float(cmd.split()[-1])
                elif c.startswith(":SENS:CURR:NPLC "):
                    self._nplc = float(cmd.split()[-1])
            except ValueError:
                pass

        def query(self, cmd):
            if not self.connected:
                return None
            if "smu_disconnect" in injected_faults():
                self.connected = False
                return None
            if "MEAS" in cmd.upper():
                time.sleep(min(0.5, 2 * self._nplc / 50.0))
                i = beam().beam_current() + random.gauss(0.0, 2e-11) + self._volt * 1e-12
                if abs(i) > 1.05 * self._range:
                    i = 9.91e37
                v = self._volt if self.output_state else 0.0
                return f"{v:+.6E};{i:+.6E}"
            if cmd.strip() == "*OPC?":
                return "1"
            return "0"

    return FakeSmuProtocol
