"""The numbers of the network page: the WireGuard round trip and the traffic speed the primary keeps per node."""
from mesh import probe
from nodes import metrics


def test_round_trip_is_read_from_ping():
    out = b"64 bytes from 10.77.0.2: icmp_seq=1 ttl=64 time=3.42 ms\n"
    assert probe.parse_rtt(out) == 3.42
    assert probe.parse_rtt(b"64 bytes from 10.77.0.2: icmp_seq=1 ttl=64 time<1 ms") == 1.0
    assert probe.parse_rtt(b"1 packets transmitted, 0 received, 100% packet loss") is None


def test_traffic_speed_comes_from_two_readings():
    metrics._traffic.clear()
    metrics.note_traffic(1, {"net_rx_bytes": 1000, "net_tx_bytes": 500}, now=100.0)
    assert metrics.traffic_rate(1) is None                       # one reading: no speed yet
    metrics.note_traffic(1, {"net_rx_bytes": 6000, "net_tx_bytes": 1500}, now=105.0)
    assert metrics.traffic_rate(1) == {"in": 1000.0, "out": 200.0}


def test_readings_too_close_together_are_skipped():
    metrics._traffic.clear()
    metrics.note_traffic(2, {"net_rx_bytes": 0, "net_tx_bytes": 0}, now=10.0)
    metrics.note_traffic(2, {"net_rx_bytes": 1000, "net_tx_bytes": 1000}, now=15.0)
    metrics.note_traffic(2, {"net_rx_bytes": 9999, "net_tx_bytes": 9999}, now=15.2)
    assert metrics.traffic_rate(2) == {"in": 200.0, "out": 200.0}


def test_a_restarted_server_gives_no_speed_until_the_next_reading():
    metrics._traffic.clear()
    metrics.note_traffic(3, {"net_rx_bytes": 5000, "net_tx_bytes": 5000}, now=0.0)
    metrics.note_traffic(3, {"net_rx_bytes": 10, "net_tx_bytes": 10}, now=5.0)
    assert metrics.traffic_rate(3) is None
    metrics.note_traffic(3, {"net_rx_bytes": 510, "net_tx_bytes": 10}, now=10.0)
    assert metrics.traffic_rate(3) == {"in": 100.0, "out": 0.0}
