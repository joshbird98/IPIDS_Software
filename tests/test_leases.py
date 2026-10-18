"""Control leases (src/core/leases.py): who may drive a subsystem while a sequence runs."""
import pytest

from src.core.leases import LeaseTable

SUB = "ion_beam.beamline.magnet"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def table():
    clock = Clock()
    return LeaseTable([SUB, "ion_beam.system"], clock=clock, wall=clock, unleasable=["ion_beam.system"]), clock


def op(table, op, holder="seq-1", purpose="mass scan", ttl=5.0):
    return table.handle(SUB, {"op": op, "holder": holder, "purpose": purpose, "ttl": ttl}, origin="hmi")


def test_claim_admits_only_the_holder(table):
    t, _ = table
    assert t.refusal(SUB, None) is None
    ok, reason, event = op(t, "claim")
    assert ok and "took control" in event
    assert t.refusal(SUB, "seq-1") is None
    assert "mass scan" in t.refusal(SUB, None) and "take control" in t.refusal(SUB, None)
    assert "mass scan" in t.refusal(SUB, "seq-2")
    ok, reason, _ = op(t, "claim", holder="seq-2", purpose="recipe")
    assert not ok and "mass scan" in reason


def test_lease_expires_unless_renewed(table):
    t, clock = table
    op(t, "claim", ttl=5.0)
    clock.t += 4.0
    assert op(t, "renew")[0]
    clock.t += 4.0
    assert t.active(SUB) is not None          # renewed at +4 s: good until +9 s
    clock.t += 1.5
    assert t.active(SUB) is None              # the holder died: free again
    assert t.refusal(SUB, None) is None


def test_operator_take_revokes_until_the_holder_claims_again(table):
    t, _ = table
    op(t, "claim")
    ok, reason, event = t.handle(SUB, {"op": "take"}, origin="hmi")
    assert ok and "from mass scan" in reason and "hmi took control" in event
    assert t.active(SUB) is None
    ok, reason, _ = op(t, "renew")
    assert not ok and "taken by the operator" in reason      # the sequence notices and stops
    assert op(t, "claim")[0]                                 # an explicit re-claim (e.g. resume) works
    assert op(t, "renew")[0]


def test_renew_reclaims_a_free_subsystem(table):
    """After a service restart the table is empty: renewing carries on holding it."""
    t, _ = table
    assert op(t, "renew")[0]
    assert t.active(SUB).holder == "seq-1"


def test_release_only_by_the_holder(table):
    t, _ = table
    op(t, "claim")
    assert not op(t, "release", holder="seq-2")[0]
    ok, _, event = op(t, "release")
    assert ok and "released" in event and t.active(SUB) is None
    assert op(t, "release")[0]                               # releasing a free one is fine


@pytest.mark.parametrize("value, why", [
    ("claim", "must be"), ({"op": "grab", "holder": "x"}, "unknown lease op"),
    ({"op": "claim"}, "holder"), ({"op": "claim", "holder": "x", "ttl": "soon"}, "bad ttl"),
])
def test_bad_lease_requests(table, value, why):
    t, _ = table
    ok, reason, _ = t.handle(SUB, value)
    assert not ok and why in reason


def test_unleasable_and_unknown_subsystems(table):
    t, _ = table
    ok, reason, _ = t.handle("ion_beam.system", {"op": "claim", "holder": "x"})
    assert not ok and "can't be leased" in reason
    ok, reason, _ = t.handle("ion_beam.nowhere", {"op": "claim", "holder": "x"})
    assert not ok


def test_ttl_is_clamped(table):
    t, clock = table
    op(t, "claim", ttl=1e9)
    clock.t += 61.0
    assert t.active(SUB) is None              # at most 60 s without renewal


def test_published_holder(table):
    t, _ = table
    state = {}
    t.publish_into(state)
    assert state == {f"{SUB}.lease_holder": None, f"{SUB}.lease_purpose": None}
    op(t, "claim")
    t.publish_into(state)
    assert state[f"{SUB}.lease_holder"] == "seq-1" and state[f"{SUB}.lease_purpose"] == "mass scan"
