from src.services.service_manager import RestartPolicy


def test_backoff_grows_and_caps():
    p = RestartPolicy(steps=(2, 5, 10, 30), stable_after=120)
    delays = [p.on_failure(now=0) for _ in range(6)]
    assert delays == [2, 5, 10, 30, 30, 30]
    assert p.failures == 6


def test_waits_for_the_delay():
    p = RestartPolicy(steps=(2, 5))
    p.on_failure(now=100)
    assert not p.ready(101.9)
    assert p.ready(102.0)
    assert p.retry_in(101) == 1


def test_count_resets_only_after_stable_running():
    p = RestartPolicy(steps=(2, 5, 10), stable_after=120)
    p.on_failure(0)
    p.on_failure(10)
    p.on_started()
    p.on_online(20)
    p.on_online(100)
    assert p.failures == 2            # ran for 80 s: not yet trusted
    p.on_online(141)
    assert p.failures == 0            # ran for > 120 s
    assert p.on_failure(150) == 2     # next crash starts from the shortest delay again


def test_crash_while_online_restarts_stability_clock():
    p = RestartPolicy(steps=(2, 5, 10), stable_after=120)
    p.on_failure(0)
    p.on_started()
    p.on_online(10)
    assert p.on_failure(100) == 5     # crashed again before being stable
    p.on_started()
    p.on_online(110)
    p.on_online(200)
    assert p.failures == 2
