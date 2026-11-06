"""Addresses on the node network. The primary is the hub and always owns the first address."""
import ipaddress
import re

KEY_RE = re.compile(r"^[A-Za-z0-9+/]{43}=\Z")
_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\Z")


def network(subnet: str) -> ipaddress.IPv4Network:
    net = ipaddress.ip_network(subnet, strict=False)
    if net.version != 4 or not net.is_private or not 8 <= net.prefixlen <= 24:
        raise ValueError("the node network must be a private IPv4 range between /8 and /24")
    return net


def hub_ip(subnet: str) -> str:
    return str(next(network(subnet).hosts()))


def hub_interface(subnet: str) -> str:
    """Address of the primary with the prefix length, e.g. 10.77.0.1/16."""
    net = network(subnet)
    return f"{hub_ip(subnet)}/{net.prefixlen}"


def next_free_ip(subnet: str, taken) -> str:
    """The lowest unused node address (the hub's address is never given out)."""
    used = {ip for ip in taken if ip}
    hosts = network(subnet).hosts()
    next(hosts)                                  # the hub
    for ip in hosts:
        if str(ip) not in used:
            return str(ip)
    raise ValueError("the node network is full")


def is_valid_key(value) -> bool:
    return isinstance(value, str) and bool(KEY_RE.match(value))


def is_valid_endpoint(value) -> bool:
    """host:port, where host is a name or an IP address."""
    host, sep, port = str(value or "").rpartition(":")
    if not sep or not port.isdigit() or not 1 <= int(port) <= 65535:
        return False
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return bool(_HOST_RE.match(host))
