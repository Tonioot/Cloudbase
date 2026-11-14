"""The firewall rules of the root helper, tried against the real kernel firewall.

Needs Linux, root, `ip` and `iptables`; it is skipped everywhere else. Nothing here touches Docker or the machine's
own network: it builds a pretend server, a pretend world (public addresses, a home network, another node, a cloud
metadata address) and one container per kind of app in network namespaces, applies the rules of
scripts/cloudbase-wireguard inside the server's namespace, and checks which connections get through.

Run it on a real server too (it cleans up after itself):

    sudo python3 tests/test_firewall_netns.py

What it cannot show: Docker's own isolation between containers on one bridge (inter-container communication off)
and Docker's own rules; those need Docker.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    import pytest
except ImportError:       # run as a plain script on a server
    pytest = None

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "cloudbase-wireguard"
HOST, WORLD = "cbt-host", "cbt-world"
# kind of app -> (bridge, the first three bytes of its subnet)
CONTAINERS = {"open": ("cbappl0", "172.30.2"), "internet": ("cbapps0", "172.30.1"), "none": ("cbappx0", "172.30.3"),
              "db": ("cbdb1", "172.30.4"), "link": ("cbl9", "172.30.5")}
# Addresses of the pretend world, all reachable through the server
PUBLIC, PUBLIC_DNS = "1.1.1.1", "8.8.8.8"
LAN, LAN_DNS = "192.168.1.50", "192.168.1.1"
NODE, OTHER_NODE = "10.77.0.2", "10.77.0.9"
METADATA, RFC172, CGNAT = "169.254.169.254", "172.16.5.5", "100.64.1.1"
WORLD_ADDRESSES = [PUBLIC, PUBLIC_DNS, LAN, LAN_DNS, NODE, OTHER_NODE, METADATA, RFC172, CGNAT]
WORLD_PORTS = [53, 80, 443, 20042, 20043]
SERVER_LISTENS = [22, 80, 443, 7823]               # on the server itself: SSH, nginx, the panel
LISTED = (NODE, 20042)                             # the one destination the apps are allowed on another node

SERVER = """
import socket, sys, threading, json
spec = json.loads(sys.argv[1])
def tcp(ip, port):
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind((ip, port)); s.listen(64)
    while True:
        c, _ = s.accept(); c.sendall(b"ok"); c.close()
def udp(ip, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind((ip, port))
    while True:
        _, a = s.recvfrom(64); s.sendto(b"ok", a)
for proto, ip, port in spec:
    threading.Thread(target=tcp if proto == "tcp" else udp, args=(ip, port), daemon=True).start()
print("up", flush=True)
threading.Event().wait()
"""

CLIENT = """
import socket, sys
proto, ip, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
try:
    if proto == "tcp":
        s = socket.create_connection((ip, port), timeout=1.2); data = s.recv(8)
    else:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(1.2); s.sendto(b"x", (ip, port)); data = s.recv(8)
    sys.exit(0 if data == b"ok" else 1)
except OSError:
    sys.exit(1)
"""


def can_run() -> bool:
    return (sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0
            and shutil.which("ip") is not None and shutil.which("iptables") is not None)


def sh(*args, ns: str | None = None, check: bool = True, stdin: str | None = None) -> subprocess.CompletedProcess:
    cmd = ["ip", "netns", "exec", ns, *args] if ns else list(args)
    done = subprocess.run(cmd, capture_output=True, text=True, input=stdin, timeout=60)
    if check and done.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {(done.stderr or done.stdout).strip()}")
    return done


class Lab:
    """The pretend server, world and containers."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="cb-firewall-")
        self.servers: list[subprocess.Popen] = []
        helper = SCRIPT.read_text(encoding="utf-8").replace('CONF_DIR = "/etc/cloudbase-wg"', f'CONF_DIR = "{self.dir}/conf"')
        self.helper = os.path.join(self.dir, "helper.py")
        Path(self.helper).write_text(helper, encoding="utf-8")

    # ── building and removing it ────────────────────────────────────────────
    def build(self) -> None:
        self.teardown()
        for ns in (HOST, WORLD):
            sh("ip", "netns", "add", ns)
            sh("ip", "link", "set", "lo", "up", ns=ns)
        sh("ip", "link", "add", "hw0", "type", "veth", "peer", "name", "wh0")
        sh("ip", "link", "set", "hw0", "netns", HOST)
        sh("ip", "link", "set", "wh0", "netns", WORLD)
        sh("ip", "addr", "add", "10.250.0.1/30", "dev", "hw0", ns=HOST)
        sh("ip", "addr", "add", "10.250.0.2/30", "dev", "wh0", ns=WORLD)
        for ns, dev in ((HOST, "hw0"), (WORLD, "wh0")):
            sh("ip", "link", "set", dev, "up", ns=ns)
        sh("sysctl", "-q", "-w", "net.ipv4.ip_forward=1", ns=HOST)
        sh("ip", "route", "add", "default", "via", "10.250.0.1", ns=WORLD)
        for address in WORLD_ADDRESSES:
            sh("ip", "addr", "add", f"{address}/32", "dev", "lo", ns=WORLD)
            sh("ip", "route", "add", f"{address}/32", "via", "10.250.0.2", ns=HOST)
        for kind, (bridge, net) in CONTAINERS.items():
            ns = f"cbt-{kind}"
            sh("ip", "netns", "add", ns)
            sh("ip", "link", "set", "lo", "up", ns=ns)
            sh("ip", "link", "add", f"v-{kind}", "type", "veth", "peer", "name", f"c-{kind}")
            sh("ip", "link", "set", f"v-{kind}", "netns", HOST)
            sh("ip", "link", "set", f"c-{kind}", "netns", ns)
            sh("ip", "link", "add", bridge, "type", "bridge", ns=HOST)
            sh("ip", "addr", "add", f"{net}.1/24", "dev", bridge, ns=HOST)
            sh("ip", "link", "set", f"v-{kind}", "master", bridge, ns=HOST)
            for dev in (bridge, f"v-{kind}"):
                sh("ip", "link", "set", dev, "up", ns=HOST)
            sh("ip", "addr", "add", f"{net}.2/24", "dev", f"c-{kind}", ns=ns)
            sh("ip", "link", "set", f"c-{kind}", "up", ns=ns)
            sh("ip", "route", "add", "default", "via", f"{net}.1", ns=ns)
        world = [("tcp", a, p) for a in WORLD_ADDRESSES for p in WORLD_PORTS] + [("udp", a, 53) for a in (LAN_DNS, PUBLIC_DNS)]
        self.serve(WORLD, world)
        self.serve(HOST, [("tcp", "0.0.0.0", p) for p in SERVER_LISTENS])
        for kind in CONTAINERS:
            self.serve(f"cbt-{kind}", [("tcp", "0.0.0.0", 8000)])

    def serve(self, ns: str, spec: list) -> None:
        proc = subprocess.Popen(["ip", "netns", "exec", ns, sys.executable, "-c", SERVER, json.dumps(spec)],
                                stdout=subprocess.PIPE, text=True)
        assert proc.stdout.readline().strip() == "up"
        self.servers.append(proc)

    def teardown(self) -> None:
        for proc in self.servers:
            proc.kill()
        self.servers = []
        for ns in (HOST, WORLD, *(f"cbt-{kind}" for kind in CONTAINERS)):
            sh("ip", "netns", "del", ns, check=False)       # the veth pairs and bridges go with their namespaces

    def close(self) -> None:
        self.teardown()
        shutil.rmtree(self.dir, ignore_errors=True)

    # ── the helper, as root inside the pretend server ──────────────────────
    def helper_run(self, command: str, stdin: str | None = None) -> str:
        return sh(sys.executable, self.helper, command, ns=HOST, stdin=stdin).stdout

    def rules(self, allow: list[tuple[str, int]]) -> None:
        self.helper_run("isolate")
        self.helper_run("egress", json.dumps({"allow": [{"ip": ip, "port": port} for ip, port in allow]}))

    def iptables(self, *args) -> str:
        return sh("iptables", *args, ns=HOST).stdout

    # ── trying a connection ────────────────────────────────────────────────
    def connects(self, from_ns: str, proto: str, ip: str, port: int) -> bool:
        return sh(sys.executable, "-c", CLIENT, proto, ip, str(port), ns=from_ns, check=False).returncode == 0


def scenarios(allowed: tuple[str, int] | None = LISTED) -> list[tuple]:
    """(description, from, proto, ip, port, expected): what should and should not get through.

    From a container: "open" is an app with open access, "internet" internet only, "none" only its connections, "db" a
    container on a database network and "link" one on the network of an app link (those two only have the rule around
    the server itself)."""
    out = []

    def outgoing(text, proto, ip, port, open_, internet, none):
        out.extend([(text, "open", proto, ip, port, open_), (text, "internet", proto, ip, port, internet),
                    (text, "none", proto, ip, port, none)])

    listed = allowed is not None
    outgoing("the internet", "tcp", PUBLIC, 443, True, True, False)
    outgoing("a device on the home network", "tcp", LAN, 443, True, False, False)
    outgoing("the listed port on another node", "tcp", NODE, 20042, True, listed, listed)
    outgoing("another port on that node", "tcp", NODE, 20043, True, False, False)
    outgoing("another node that is not listed", "tcp", OTHER_NODE, 20042, True, False, False)
    outgoing("the cloud metadata address", "tcp", METADATA, 80, True, False, False)
    outgoing("a 172.16 address", "tcp", RFC172, 443, True, False, False)
    outgoing("a 100.64 (carrier-grade NAT, Tailscale) address", "tcp", CGNAT, 443, True, False, False)
    outgoing("DNS over TCP to the router", "tcp", LAN_DNS, 53, True, True, False)
    outgoing("DNS over UDP to the router", "udp", LAN_DNS, 53, True, True, False)
    outgoing("DNS over UDP to a public resolver", "udp", PUBLIC_DNS, 53, True, True, False)
    for kind, (_, net) in CONTAINERS.items():
        for where, ip in (("the server's address on its bridge", f"{net}.1"), ("the server's other address", "10.250.0.1")):
            out.append((f"SSH on {where}", kind, "tcp", ip, 22, False))
            out.append((f"the panel on {where}", kind, "tcp", ip, 7823, False))
            for port in (80, 443):
                out.append((f"nginx ({port}) on {where}", kind, "tcp", ip, port, kind != "none"))
    return out


def run_scenarios(lab: Lab, scenario_list: list[tuple]) -> list[str]:
    def one(item):
        text, kind, proto, ip, port, expected = item
        got = lab.connects(f"cbt-{kind}", proto, ip, port)
        return None if got == expected else f"{kind:8} {proto} {ip}:{port:<5} {text}: {'got through' if got else 'was blocked'}, expected {'through' if expected else 'blocked'}"
    with ThreadPoolExecutor(max_workers=16) as pool:
        return [m for m in pool.map(one, scenario_list) if m]


def run_inbound(lab: Lab) -> list[str]:
    """The other direction must keep working: nginx to an app, and the internet to an app (answers go back out)."""
    problems = []
    for kind, (_, net) in CONTAINERS.items():
        if not lab.connects(HOST, "tcp", f"{net}.2", 8000):
            problems.append(f"the server could not connect to the {kind} container")
        if not lab.connects(WORLD, "tcp", f"{net}.2", 8000):
            problems.append(f"the world could not connect to the {kind} container (its answers are blocked)")
    return problems


def forward_jumps(lab: Lab) -> dict[str, int]:
    """How often each of our chains is called from FORWARD and INPUT."""
    counts: dict[str, int] = {}
    for table in ("FORWARD", "INPUT"):
        for line in lab.iptables("-S", table).splitlines():
            if line.startswith(f"-A {table} ") and " -j CLOUDBASE-" in line:
                key = f"{table} {line.split(' -i ')[1].split()[0]}"
                counts[key] = counts.get(key, 0) + 1
    return counts


def check_everything() -> list[str]:
    lab = Lab()
    problems: list[str] = []
    try:
        lab.build()

        # Before any rule: everything gets through (this is the lab itself being sane)
        baseline = [("the internet", "internet", "tcp", PUBLIC, 443, True), ("the panel", "none", "tcp", "172.30.3.1", 7823, True),
                    ("the home network", "internet", "tcp", LAN, 443, True)]
        problems += [f"without rules: {m}" for m in run_scenarios(lab, baseline)]

        lab.rules([LISTED])
        problems += run_scenarios(lab, scenarios(LISTED))
        problems += run_inbound(lab)

        # Applying twice changes nothing
        lab.rules([LISTED])
        counts = forward_jumps(lab)
        if any(n != 1 for n in counts.values()) or len(counts) != 7:
            problems.append(f"the rules were added twice or are missing: {counts}")
        problems += [f"after applying twice: {m}" for m in run_scenarios(lab, scenarios(LISTED))]

        # After a reboot the chains are gone; the helper's boot command puts them back, with the stored list
        for table in ("FORWARD", "INPUT"):
            lab.iptables("-F", table)
        problems += [f"flushed, so open: {m}" for m in run_scenarios(lab, [("the home network", "internet", "tcp", LAN, 443, True)])]
        lab.helper_run("boot")
        problems += [f"after boot: {m}" for m in run_scenarios(lab, scenarios(LISTED))]
        problems += run_inbound(lab)

        # A destination that is taken away is closed again
        lab.rules([])
        problems += [f"after closing: {m}" for m in run_scenarios(lab, scenarios(None))]
    finally:
        lab.close()
    return problems


def _needs_linux_root(function):
    return pytest.mark.skipif(not can_run(), reason="needs Linux, root, ip and iptables")(function) if pytest else function


@_needs_linux_root
def test_the_firewall_rules_against_the_real_kernel():
    problems = check_everything()
    assert not problems, "\n" + "\n".join(problems)


if __name__ == "__main__":
    if not can_run():
        sys.exit("This needs Linux, root, ip and iptables (try: sudo python3 tests/test_firewall_netns.py)")
    started = time.monotonic()
    found = check_everything()
    print("\n".join(found) if found else "all firewall checks passed")
    print(f"({time.monotonic() - started:.0f}s)")
    sys.exit(1 if found else 0)
