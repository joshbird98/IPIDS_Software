"""The safety-relay permissive as a hardware service sees it, from the PLC service's telemetry.

Used by the services whose outputs are interlocked in software (X-steerer, SMU bias).
"""
import time

import orjson
import zmq

RELAY_TAG = "ion_beam.facilities.safety_relay_active"


class RelayWatch:
    """Drains the PLC telemetry socket and says whether the relay is confirmed energised.

    Every message since the previous check counts, not just the newest: if the service's
    loop was blocked (an unresponsive device can hold it for seconds), a trip and reset in
    between - or a gap in the PLC data longer than max_age - still counts as a trip for
    that check. null (PLC unreachable) counts as off, and so does no PLC data for max_age.
    """

    def __init__(self, sock, max_age: float = 1.0, clock=time.time):
        self.sock = sock
        self.max_age = max_age
        self.clock = clock
        self.relay = False         # newest relay state received
        self.last_rx = 0.0         # when the newest PLC message was received
        self.last_msg_ts = 0.0     # the PLC service's own timestamp of it (receive time if it has none)
        self._last_payload_ts = None

    def check(self) -> bool:
        """True = relay on, PLC data current, and no trip or data gap since the last check."""
        lapse = False
        while True:
            try:
                _topic, msg = self.sock.recv_multipart(flags=zmq.NOBLOCK)
            except zmq.Again:
                break
            try:
                payload = orjson.loads(msg)
            except ValueError:
                continue
            now = self.clock()
            ts = payload.get("timestamp")
            if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
                if self._last_payload_ts is not None and ts - self._last_payload_ts > self.max_age:
                    lapse = True   # no PLC data for a while: the relay state was unknown then
                self._last_payload_ts = ts
                self.last_msg_ts = ts
            else:
                self.last_msg_ts = now
            self.last_rx = now
            self.relay = bool(payload.get(RELAY_TAG))
            if not self.relay:
                lapse = True
        alive = self.clock() - self.last_rx < self.max_age
        return self.relay and alive and not lapse

    def fresh(self) -> bool:
        """The newest relay state comes from PLC data younger than max_age (by the PLC's own clock)."""
        return self.clock() - self.last_msg_ts < self.max_age
