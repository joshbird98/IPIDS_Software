"""The sequencer's command port (src/sequencer/port.py): leases claimed before the first command
to a subsystem, renewed, lost when the operator takes control, released at the end."""
from src.core import commands as C
from src.core.leases import TAKEN
from src.sequencer.port import JobPort, LeaseKeeper, SubsystemMap
from tests.sequencer_world import MAGNET, SMU, World

SP = f"{MAGNET}.sp_requested_current"


def make_port(world, holder="seq-test-1", purpose="test sequence"):
    lost = []
    keeper = LeaseKeeper(world.dispatcher, holder, purpose, world.subsystems, origin="test",
                         on_lost=lambda sub, why: lost.append((sub, why)), clock=world.clock)
    return JobPort(world.dispatcher, keeper, origin="test"), keeper, lost


def settle(world, port, seconds=0.0, dt=0.1):
    world.dispatcher.pump()
    port.tick()
    t = 0.0
    while t < seconds:
        world.clock.t += dt
        t += dt
        world.publish()
        world.dispatcher.pump()
        port.tick()
    world.dispatcher.pump()
    port.tick()


def test_subsystems_are_learned_from_telemetry():
    world = World()
    smap = SubsystemMap(world.telemetry.keys, clock=world.clock)
    assert smap.lookup(SP) == (MAGNET, True)
    assert smap.lookup("ion_beam.beamline.faraday.smu.cmd_nplc") == (SMU, True)
    assert smap.lookup("ion_beam.source.chamber.cmd_open_gv") == ("ion_beam.source.chamber", False)  # unleasable
    assert smap.lookup("ion_beam.nowhere.x") == (None, False)


def test_first_command_claims_the_lease_and_carries_the_holder():
    world = World()
    port, keeper, lost = make_port(world)
    ticket = port.send(SP, 20.0)
    settle(world, port)
    log = world.devices["magnet"].log
    assert [entry[0] for entry in log] == [f"{MAGNET}.cmd_lease", SP]        # claim first, same connection
    assert log[1][2] == "seq-test-1" and ticket.ok
    assert world.devices["magnet"].leases.active(MAGNET).purpose == "test sequence"
    port.send(SP, 21.0)
    settle(world, port)
    assert sum(1 for e in world.devices["magnet"].log if e[0].endswith("cmd_lease")) == 1   # claimed once
    assert not lost


def test_unleasable_subsystems_are_commanded_without_a_claim():
    world = World()
    port, keeper, _ = make_port(world)
    ticket = port.send("ion_beam.source.chamber.cmd_open_gv", True)
    settle(world, port)
    assert ticket.ok and [e[0] for e in world.devices["plc"].log] == ["ion_beam.source.chamber.cmd_open_gv"]
    assert keeper.held == {}


def test_renewals_keep_the_lease_past_its_ttl_and_release_frees_it():
    world = World()
    port, keeper, lost = make_port(world)
    port.send(SP, 20.0)
    settle(world, port, seconds=12.0)            # TTL is 5 s: only renewals keep it
    lease = world.devices["magnet"].leases.active(MAGNET)
    assert lease is not None and lease.holder == "seq-test-1" and not lost
    keeper.release_all()
    settle(world, port)
    assert world.devices["magnet"].leases.active(MAGNET) is None


def test_lease_lapses_without_renewals():
    world = World()
    port, keeper, _ = make_port(world)
    port.send(SP, 20.0)
    settle(world, port)
    world.clock.t += 6.0                         # the sequencer froze / died
    assert world.devices["magnet"].leases.active(MAGNET) is None


def test_operator_take_is_noticed_at_the_next_renewal_and_nothing_more_is_sent():
    world = World()
    port, keeper, lost = make_port(world)
    port.send(SP, 20.0)
    settle(world, port)
    world.take("magnet", MAGNET)
    settle(world, port, seconds=1.5)
    assert lost == [(MAGNET, TAKEN)] and MAGNET not in keeper.held
    n = len(world.devices["magnet"].log)
    ticket = port.send(SP, 22.0)
    assert ticket.done() and ticket.status == C.REJECTED and ticket.reason == TAKEN
    settle(world, port)
    assert len(world.devices["magnet"].log) == n  # refused here, never sent
    keeper.forgive()                              # e.g. the operator resumes a recipe
    assert port.send(SP, 22.0) is not None
    settle(world, port)
    assert world.devices["magnet"].leases.active(MAGNET).holder == "seq-test-1"


def test_a_command_refused_after_a_take_counts_as_lost_at_once():
    world = World()
    port, keeper, lost = make_port(world)
    port.send(SP, 20.0)
    settle(world, port)
    world.take("magnet", MAGNET)
    ticket = port.send(SP, 21.0)                  # sent before the renewal noticed
    world.dispatcher.pump()
    port.tick()
    assert ticket.status == C.REJECTED and ticket.reason == TAKEN
    assert lost == [(MAGNET, TAKEN)]


def test_claim_refused_when_another_sequence_holds_it():
    world = World()
    other, _, _ = make_port(world, holder="seq-other", purpose="mass scan")
    other.send(SP, 20.0)
    settle(world, other)
    port, keeper, lost = make_port(world)
    ticket = port.send(SP, 21.0)
    settle(world, port)
    assert ticket.status == C.REJECTED and "mass scan" in ticket.reason
    assert lost and lost[0][0] == MAGNET and "mass scan" in lost[0][1]


def test_service_down_keeps_the_lease_and_renewal_reclaims_after_a_restart():
    world = World()
    port, keeper, lost = make_port(world)
    port.send(SP, 20.0)
    settle(world, port)
    world.devices["magnet"].online = False
    settle(world, port, seconds=3.0)
    assert not lost and MAGNET in keeper.held     # unreachable is not a refusal
    world.devices["magnet"].leases._leases.clear()   # the service restarted: empty lease table
    world.devices["magnet"].online = True
    settle(world, port, seconds=1.5)
    assert world.devices["magnet"].leases.active(MAGNET).holder == "seq-test-1"


def test_a_command_that_reached_its_service_late_is_sent_again():
    """A service refuses a command that reaches it more than 0.5 s after it was sent (stale): it
    was never carried out, and the sequence still wants it."""
    world = World()
    port, keeper, lost = make_port(world)
    port.send(SP, 20.0)
    settle(world, port)
    world.devices["magnet"].late = 2
    ticket = port.send(SP, 21.0)
    settle(world, port, seconds=0.5)
    assert ticket.ok and world.devices["magnet"].values[SP] == 21.0 and not lost
    world.devices["magnet"].late = 3                    # late every time: the job hears of it
    ticket = port.send(SP, 22.0)
    settle(world, port, seconds=0.5)
    assert ticket.status == C.REJECTED and ticket.reason.startswith(C.STALE)
    assert world.devices["magnet"].values[SP] == 21.0


def test_a_renewal_that_reached_its_service_late_keeps_control():
    """Josh, 21 October 2026: the start-up recipe stopped - "could not keep control of the einzel:
    stale: sent 0.73 s ago" - because one renewal reached a busy service late."""
    world = World()
    port, keeper, lost = make_port(world)
    port.send(SP, 20.0)
    settle(world, port)
    world.devices["magnet"].late = 3                    # the next renewal, and both times it is sent again
    settle(world, port, seconds=1.5)
    assert not lost and MAGNET in keeper.held
    settle(world, port, seconds=6.0)                    # past the lease's TTL: the renewals carried on
    assert world.devices["magnet"].leases.active(MAGNET).holder == "seq-test-1" and not lost
