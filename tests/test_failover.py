"""An app that lost all its instances to an offline node is moved; one the user stopped is left alone."""
from workers import failover


def test_an_app_that_only_has_instances_on_an_offline_node_is_moved():
    assert failover.needs_failover(["node_offline"])
    assert failover.needs_failover(["node_offline", "node_offline"])
    assert failover.needs_failover(["node_offline", "error"])


def test_an_app_that_still_runs_somewhere_is_left_to_the_autoscaler():
    assert not failover.needs_failover(["running", "node_offline"])
    assert not failover.needs_failover(["starting", "node_offline"])


def test_an_app_the_user_stopped_stays_stopped():
    assert not failover.needs_failover(["stopped"])
    assert not failover.needs_failover(["stopping", "node_offline"])
    assert not failover.needs_failover(["stopped", "node_offline"])
    assert not failover.needs_failover([])
    assert not failover.needs_failover(["error"])           # a crashing app is not moved around
