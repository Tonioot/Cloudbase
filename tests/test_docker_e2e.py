"""Cloudbase's own code against a real Docker and the real firewall.

The unit tests use a pretend Docker; the namespace test (test_firewall_netns.py) uses the real kernel firewall but no
Docker. This one runs the real container code (containers/client.py) on a real Docker daemon, with the real helper
rules, and checks what apps can and cannot reach: other apps, databases, the server itself, a pretend home network and
pretend nodes.

It changes the machine while it runs (Docker networks and containers named cbtest-*, firewall chains named CLOUDBASE-*
for the bridges cbapps0, cbappl0, cbappx0 and cbdb*, a namespace for a pretend world) and removes all of it afterwards.
So it only runs when you ask for it:

    sudo CLOUDBASE_E2E=1 python3 tests/test_docker_e2e.py

Needs Linux, root, a running Docker, the Python package `docker`, and a small image with busybox `nc`
(CLOUDBASE_E2E_IMAGE, default alpine:3; it is not pulled for you).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    import pytest
except ImportError:       # run as a plain script on a server
    pytest = None

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
for extra in ("/var/tmp/cbtest-py",):          # where docker-py may have been installed without touching the system
    if os.path.isdir(extra):
        sys.path.append(extra)

IMAGE = os.environ.get("CLOUDBASE_E2E_IMAGE", "alpine:3")
WORLD = "cbe-world"
# Pretend places, reachable through this machine
LAN, PUBLIC = "192.168.77.50", "203.0.113.9"
NODE, OTHER_NODE = "10.99.0.2", "10.99.0.9"
WORLD_ADDRESSES = [LAN, PUBLIC, NODE, OTHER_NODE]
WORLD_PORTS = [443, 20042, 20043]
PANEL_PORT = 17823               # something running on this machine itself
LISTEN = "while true; do echo ok | nc -l -p 8000; done"
DB_ID, LINK_ID, APP_Y_ID = 9001, 9001, 9002

SERVER = """
import socket, sys, threading, json
for ip, port in json.loads(sys.argv[1]):
    def run(ip=ip, port=port):
        s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind((ip, port)); s.listen(64)
        while True:
            c, _ = s.accept(); c.sendall(b"ok"); c.close()
    threading.Thread(target=run, daemon=True).start()
print("up", flush=True)
threading.Event().wait()
"""
CLIENT = """
import socket, sys
try:
    s = socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=1.5); sys.exit(0 if s.recv(8).startswith(b"ok") else 1)
except OSError:
    sys.exit(1)
"""


def can_run() -> bool:
    if not (os.environ.get("CLOUDBASE_E2E") == "1" and sys.platform.startswith("linux") and hasattr(os, "geteuid")
            and os.geteuid() == 0 and shutil.which("docker") and shutil.which("ip") and shutil.which("iptables")):
        return False
    try:
        import docker  # noqa: F401
        return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0
    except Exception:
        return False


def sh(*args, check=True, ns=None, stdin=None, timeout=60) -> subprocess.CompletedProcess:
    cmd = ["ip", "netns", "exec", ns, *args] if ns else list(args)
    done = subprocess.run(cmd, capture_output=True, text=True, input=stdin, timeout=timeout)
    if check and done.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {(done.stderr or done.stdout).strip()}")
    return done


class Lab:
    def __init__(self):
        import docker
        from containers import client as cc, naming
        from dbs import proxy as db_proxy
        self.docker, self.cc, self.naming, self.db_proxy = docker, cc, naming, db_proxy
        self.client = docker.from_env()
        self.dir = tempfile.mkdtemp(prefix="cb-e2e-")
        self.procs: list[subprocess.Popen] = []
        self.results: list[tuple[str, bool, bool]] = []
        helper = (ROOT / "scripts" / "cloudbase-wireguard").read_text(encoding="utf-8")
        self.helper = os.path.join(self.dir, "helper.py")
        Path(self.helper).write_text(helper.replace('CONF_DIR = "/etc/cloudbase-wg"', f'CONF_DIR = "{self.dir}/conf"'), encoding="utf-8")
        # Never touch what is on this machine already: the networks get test names (the bridge names stay, the
        # firewall rules are for those), and the old shared network is not looked at
        cc._ACCESS_NETWORKS = {access: (f"cbtest-{access}", bridge) for access, (_, bridge) in cc._ACCESS_NETWORKS.items()}
        naming.LEGACY_NETWORK = "cbtest-no-such-network"
        cc._firewall_done = True            # the helper is applied here by hand
        db_proxy.local_node_id = lambda: 1  # this machine is node 1

    # ── the pretend world ───────────────────────────────────────────────────
    def build_world(self) -> None:
        sh("ip", "netns", "add", WORLD)
        sh("ip", "link", "set", "lo", "up", ns=WORLD)
        sh("ip", "link", "add", "cbe-h", "type", "veth", "peer", "name", "cbe-w")
        sh("ip", "link", "set", "cbe-w", "netns", WORLD)
        sh("ip", "addr", "add", "10.251.0.1/30", "dev", "cbe-h")
        sh("ip", "link", "set", "cbe-h", "up")
        sh("ip", "addr", "add", "10.251.0.2/30", "dev", "cbe-w", ns=WORLD)
        sh("ip", "link", "set", "cbe-w", "up", ns=WORLD)
        sh("ip", "route", "add", "default", "via", "10.251.0.1", ns=WORLD)
        for address in WORLD_ADDRESSES:
            sh("ip", "addr", "add", f"{address}/32", "dev", "lo", ns=WORLD)
            sh("ip", "route", "add", f"{address}/32", "via", "10.251.0.2")
        spec = [(a, p) for a in WORLD_ADDRESSES for p in WORLD_PORTS]
        self.serve(["ip", "netns", "exec", WORLD], spec)
        self.serve([], [("0.0.0.0", PANEL_PORT)])          # on this machine itself

    def serve(self, prefix: list[str], spec: list) -> None:
        proc = subprocess.Popen([*prefix, sys.executable, "-c", SERVER, json.dumps(spec)], stdout=subprocess.PIPE, text=True)
        assert proc.stdout.readline().strip() == "up"
        self.procs.append(proc)

    # ── the helper rules ────────────────────────────────────────────────────
    def rules(self, allow: list[tuple[str, int]]) -> None:
        sh(sys.executable, self.helper, "isolate")
        sh(sys.executable, self.helper, "egress", stdin=json.dumps({"allow": [{"ip": i, "port": p} for i, p in allow]}))

    def remove_rules(self) -> None:
        for table, chains in (("INPUT", ("cbapps0", "cbappl0", "cbappx0", "cbdb+", "cbl+")), ("FORWARD", ("cbapps0", "cbappx0"))):
            for interface in chains:
                for chain in ("CLOUDBASE-APPS", "CLOUDBASE-APPS-CLOSED", "CLOUDBASE-EGRESS", "CLOUDBASE-EGRESS-CLOSED"):
                    while sh("iptables", "-D", table, "-i", interface, "-j", chain, check=False).returncode == 0:
                        pass
        for interface in ("cbapps0", "cbappl0", "cbappx0", "cbdb+", "cbl+"):
            while sh("iptables", "-t", "nat", "-D", "PREROUTING", "-i", interface, "-m", "addrtype", "--dst-type", "LOCAL",
                     "-j", "ACCEPT", check=False).returncode == 0:
                pass
        for chain in ("CLOUDBASE-APPS", "CLOUDBASE-APPS-CLOSED", "CLOUDBASE-EGRESS", "CLOUDBASE-EGRESS-CLOSED"):
            sh("iptables", "-F", chain, check=False)
            sh("iptables", "-X", chain, check=False)

    # ── containers, through Cloudbase's own code ────────────────────────────
    def start(self, name: str, options: dict, ports: dict | None = None, listen: bool = True):
        kwargs = {"detach": True, "name": name, "entrypoint": ["sh", "-c"],
                  "command": [LISTEN if listen else "sleep 3600"]}
        if ports:
            kwargs["ports"] = ports
        extra = self.cc._apply_linked_databases(self.client, kwargs, options)
        return self.cc.start_container(self.client, IMAGE, kwargs, extra)

    def address(self, name: str, network: str) -> str:
        return self.client.containers.get(name).attrs["NetworkSettings"]["Networks"][network]["IPAddress"]

    def gateway(self, network: str) -> str:
        return self.client.networks.get(network).attrs["IPAM"]["Config"][0]["Gateway"]

    def reach(self, name: str, host: str, port: int) -> bool:
        return sh("docker", "exec", name, "sh", "-c", f"nc -w 2 {host} {port} </dev/null | grep -q ok", check=False).returncode == 0

    def reach_from_here(self, host: str, port: int) -> bool:
        return sh(sys.executable, "-c", CLIENT, host, str(port), check=False).returncode == 0

    def resolves(self, name: str, host: str) -> bool:
        return sh("docker", "exec", name, "getent", "hosts", host, check=False).returncode == 0

    def expect(self, text: str, got: bool, expected: bool) -> None:
        self.results.append((text, got, expected))

    def cleanup(self) -> None:
        for proc in self.procs:
            proc.kill()
        for c in self.client.containers.list(all=True):
            if c.name.startswith("cbtest-") or c.name == f"cloudbase-db-{DB_ID}":
                c.remove(force=True)
        for n in self.client.networks.list():
            if n.name.startswith("cbtest-") or n.name in (f"cloudbase-db-{DB_ID}", f"cloudbase-link-{LINK_ID}"):
                try:
                    n.remove()
                except Exception:
                    pass
        self.remove_rules()
        sh("ip", "netns", "del", WORLD, check=False)
        sh("ip", "link", "del", "cbe-h", check=False)
        for address in WORLD_ADDRESSES:
            sh("ip", "route", "del", f"{address}/32", check=False)
        shutil.rmtree(self.dir, ignore_errors=True)


def link(database_id: int, node_id: int = 1) -> dict:
    return {"database_id": database_id, "node_id": node_id, "env_var": "DATABASE_URL",
            "direct_url": f"postgresql://u:p@cloudbase-db-{database_id}:8000/app", "remote_url": None}


def check_everything() -> list[str]:
    lab = Lab()
    try:
        lab.build_world()
        run(lab)
    finally:
        lab.cleanup()
    return [f"{text}: {'reachable' if got else 'not reachable'}, expected {'reachable' if expected else 'not reachable'}"
            for text, got, expected in lab.results if got != expected]


def run(lab: Lab) -> None:
    cc, naming = lab.cc, lab.naming
    published = {"8000/tcp": ("0.0.0.0", 18000)}

    # ── Cloudbase makes the networks it says it makes ───────────────────────
    for access, (name, bridge) in cc._ACCESS_NETWORKS.items():
        lab.expect(f"the bridge of '{access}' is the one the firewall rules are written for",
                   bridge == {"open": "cbappl0", "internet": "cbapps0", "none": "cbappx0"}[access], True)
        cc.ensure_apps_network(access)
        info = lab.client.networks.get(name).attrs
        lab.expect(f"network {name} has inter-container communication off",
                   info["Options"].get("com.docker.network.bridge.enable_icc") == "false", True)
        lab.expect(f"network {name} uses the bridge {bridge} that the firewall rules are written for",
                   info["Options"].get("com.docker.network.bridge.name") == bridge, True)

    # ── apps on one network do not see each other ───────────────────────────
    lab.start("cbtest-a", {"network_access": "internet"})
    lab.start("cbtest-b", {"network_access": "internet"}, ports=published)
    lab.start("cbtest-c", {"network_access": "open"})
    lab.start("cbtest-closed", {"network_access": "none"}, ports={"8000/tcp": ("0.0.0.0", 18001)})
    time.sleep(2)
    b_ip = lab.address("cbtest-b", "cbtest-internet")
    gateway = lab.gateway("cbtest-internet")
    lab.expect("an app reaches another app on the same network by its address", lab.reach("cbtest-a", b_ip, 8000), False)
    lab.expect("an app reaches an app on another apps network by its address", lab.reach("cbtest-c", b_ip, 8000), False)
    lab.expect("an app reaches another app on the same network by its container name", lab.reach("cbtest-a", "cbtest-b", 8000), False)

    # ── the hole that existed: another app's published port, through the server's own address ──
    lab.expect("BEFORE the rules: an app reaches another app's published port through the server (the hole)",
               lab.reach("cbtest-a", gateway, 18000), True)

    lab.rules([(NODE, 20042)])
    time.sleep(1)

    # ── the server itself ───────────────────────────────────────────────────
    for mode, name in (("internet", "cbtest-a"), ("open", "cbtest-c"), ("none", "cbtest-closed")):
        net = cc._ACCESS_NETWORKS[mode][0]
        gw = lab.gateway(net)
        lab.expect(f"{mode}: another app's published port through the server", lab.reach(name, gw, 18000), False)
        lab.expect(f"{mode}: something running on the server itself", lab.reach(name, gw, PANEL_PORT), False)
    # What must keep working: nginx and the apps' own published ports from outside
    lab.expect("the server reaches an app's published port", lab.reach_from_here("127.0.0.1", 18000), True)
    lab.expect("the server reaches the published port of an app without outgoing connections", lab.reach_from_here("127.0.0.1", 18001), True)
    lab.expect("the server reaches an app's published port on its other address", lab.reach_from_here(lab.gateway("cbtest-open"), 18000), True)

    # ── outgoing connections ────────────────────────────────────────────────
    cases = [("the internet", PUBLIC, 443, True, True, False),
             ("a device on the home network", LAN, 443, True, False, False),
             ("the listed port on another node", NODE, 20042, True, True, True),
             ("another port on that node", NODE, 20043, True, False, False),
             ("another node that is not listed", OTHER_NODE, 20042, True, False, False)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        def one(item):
            text, host, port, open_, internet, none = item
            return [(f"open: {text}", lab.reach("cbtest-c", host, port), open_),
                    (f"internet: {text}", lab.reach("cbtest-a", host, port), internet),
                    (f"none: {text}", lab.reach("cbtest-closed", host, port), none)]
        for rows in pool.map(one, cases):
            for text, got, expected in rows:
                lab.expect(text, got, expected)
    lab.expect("an app without outgoing connections resolves an outside name", lab.resolves("cbtest-closed", "example.com"), False)
    lab.expect("an app with internet access resolves an outside name (skip if this machine has no internet DNS)",
               lab.resolves("cbtest-a", "example.com"), True)

    # ── an app connected to an app ──────────────────────────────────────────
    network = naming.app_link_network_name(LINK_ID)
    alias = naming.app_alias(APP_Y_ID)
    entry = {"link_id": LINK_ID, "env_var": "BACKEND_URL", "target_app_id": APP_Y_ID, "alias": alias, "port": 8000,
             "network": network, "targets": [{"node_id": 1, "port": 20000, "address": "10.99.0.1"}]}
    lab.start("cbtest-front", {"network_access": "internet", "linked_apps": [entry]})
    lab.start("cbtest-back", {"network_access": "internet", "exposed_to": [{"network": network, "alias": alias}]})
    time.sleep(2)
    env = dict(e.split("=", 1) for e in lab.client.containers.get("cbtest-front").attrs["Config"]["Env"])
    lab.expect("the front app gets the address of the back app", env.get("BACKEND_URL") == f"http://{alias}:8000", True)
    lab.expect("the front app reaches the back app by its name", lab.reach("cbtest-front", alias, 8000), True)
    lab.expect("an app that is not connected resolves the back app's name", lab.resolves("cbtest-a", alias), False)
    # The route between two connected apps is private to them, and it goes both ways: only the calling app is told the address
    front_on_link = lab.address("cbtest-front", network)
    lab.expect("the back app reaches the front app on the link network by its address there (the route is two-way)",
               lab.reach("cbtest-back", front_on_link, 8000), True)
    lab.expect("an app that is not connected reaches the front app's address on the link network",
               lab.reach("cbtest-a", front_on_link, 8000), False)

    # ── an app connected to a database ──────────────────────────────────────
    db_network = cc.ensure_database_network(DB_ID)
    db_name = naming.database_container_name(DB_ID)
    lab.client.containers.run(IMAGE, name=db_name, detach=True, network=db_network, entrypoint=["sh", "-c"], command=[LISTEN])
    lab.start("cbtest-withdb", {"network_access": "none", "linked_databases": [link(DB_ID)]})
    lab.start("cbtest-peerlink", {"network_access": "none", "linked_apps": [entry]})
    time.sleep(2)
    env = dict(e.split("=", 1) for e in lab.client.containers.get("cbtest-withdb").attrs["Config"]["Env"])
    lab.expect("the app gets the database address", env.get("DATABASE_URL", "").startswith("postgresql://"), True)
    lab.expect("the linked app reaches its database (even without outgoing connections)", lab.reach("cbtest-withdb", db_name, 8000), True)
    lab.expect("the linked app, without outgoing connections, still resolves its database by name", lab.resolves("cbtest-withdb", db_name), True)
    lab.expect("an app without outgoing connections still resolves an app it is connected to", lab.resolves("cbtest-peerlink", alias), True)
    lab.expect("an app that is not linked resolves the database", lab.resolves("cbtest-a", db_name), False)
    lab.expect("an app that is not linked reaches the database by its address",
               lab.reach("cbtest-a", lab.address(db_name, db_network), 8000), False)

    # ── a database on another node: only that address is open ───────────────
    remote = {**link(8000, node_id=2), "remote_url": f"postgresql://u:p@{NODE}:20042/app"}
    from containers import egress
    lab.expect("the firewall list of an app with a remote database holds its address",
               egress.targets_of({"network_access": "none", "linked_databases": [remote]}) == {(NODE, 20042)}, True)
    lab.start("cbtest-remotedb", {"network_access": "none", "linked_databases": [remote]})
    time.sleep(1)
    lab.expect("an app without outgoing connections reaches its database on another node", lab.reach("cbtest-remotedb", NODE, 20042), True)
    lab.expect("... and nothing else on that node", lab.reach("cbtest-remotedb", NODE, 20043), False)

    # ── closing a destination again ─────────────────────────────────────────
    lab.rules([])
    time.sleep(1)
    lab.expect("after the destination is removed from the list", lab.reach("cbtest-closed", NODE, 20042), False)

    # ── rules put back after a reboot ───────────────────────────────────────
    lab.rules([(NODE, 20042)])
    for table in ("FORWARD", "INPUT"):
        for chain, interface in (("CLOUDBASE-EGRESS", "cbapps0"), ("CLOUDBASE-EGRESS-CLOSED", "cbappx0"),
                                 ("CLOUDBASE-APPS", "cbapps0"), ("CLOUDBASE-APPS", "cbappl0"), ("CLOUDBASE-APPS", "cbdb+"),
                                 ("CLOUDBASE-APPS-CLOSED", "cbappx0")):
            sh("iptables", "-D", table, "-i", interface, "-j", chain, check=False)
    lab.expect("with the rules gone the home network is open again (so the test is real)", lab.reach("cbtest-a", LAN, 443), True)
    sh(sys.executable, lab.helper, "boot")
    lab.expect("after the helper's boot command the home network is closed again", lab.reach("cbtest-a", LAN, 443), False)
    lab.expect("... and the stored destination is open again", lab.reach("cbtest-closed", NODE, 20042), True)


def _needs(function):
    return pytest.mark.skipif(not can_run(), reason="set CLOUDBASE_E2E=1 on Linux as root with Docker to run this")(function) if pytest else function


@_needs
def test_cloudbase_against_a_real_docker_and_firewall():
    problems = check_everything()
    assert not problems, "\n" + "\n".join(problems)


if __name__ == "__main__":
    if not can_run():
        sys.exit("Needs CLOUDBASE_E2E=1, Linux, root, a running Docker and the Python package docker.\n"
                 "  sudo CLOUDBASE_E2E=1 python3 tests/test_docker_e2e.py")
    started = time.monotonic()
    found = check_everything()
    print("\n".join(found) if found else "all end-to-end checks passed")
    print(f"({time.monotonic() - started:.0f}s)")
    sys.exit(1 if found else 0)
