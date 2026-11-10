"""The base of the node network: addressing, the root helper's validation, and its client."""
import base64
import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from mesh import addressing, helper

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "cloudbase-wireguard"
KEY_A = base64.b64encode(b"a" * 32).decode()
KEY_B = base64.b64encode(b"b" * 32).decode()


@pytest.fixture
def wg(tmp_path, monkeypatch):
    loader = importlib.machinery.SourceFileLoader("cloudbase_wireguard", str(SCRIPT))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("cloudbase_wireguard", loader))
    loader.exec_module(module)
    monkeypatch.setattr(module, "CONF_DIR", str(tmp_path))
    return module


# ── addressing ────────────────────────────────────────────────────────────────
def test_the_hub_owns_the_first_address_and_nodes_get_the_next_free_ones():
    assert addressing.hub_ip("10.77.0.0/16") == "10.77.0.1"
    assert addressing.hub_interface("10.77.0.0/16") == "10.77.0.1/16"
    assert addressing.next_free_ip("10.77.0.0/16", []) == "10.77.0.2"
    assert addressing.next_free_ip("10.77.0.0/16", ["10.77.0.2", None, "10.77.0.4"]) == "10.77.0.3"


def test_a_full_network_is_reported():
    with pytest.raises(ValueError):
        addressing.next_free_ip("10.77.0.0/30", ["10.77.0.2"])      # only .1 (hub) and .2 exist


@pytest.mark.parametrize("subnet", ["8.8.8.0/24", "10.0.0.0/30", "fd00::/64", "10.0.0.0/4", "nonsense"])
def test_only_private_ranges_of_a_sensible_size_are_accepted(subnet):
    with pytest.raises(ValueError):
        addressing.network(subnet)


def test_keys_and_endpoints():
    assert addressing.is_valid_key(KEY_A)
    for bad in ("", None, "short=", KEY_A + "\n", KEY_A[:-1] + "x", "a" * 44):
        assert not addressing.is_valid_key(bad)
    for good in ("pi.example.com:51820", "203.0.113.5:51820", "[2001:db8::1]:51820"):
        assert addressing.is_valid_endpoint(good), good
    for bad in ("nohost", "host:0", "host:99999", "ho st:51820", "a;b:51820", ":51820", "$(x):1"):
        assert not addressing.is_valid_endpoint(bad), bad


# ── the helper's validation ───────────────────────────────────────────────────
def test_the_helper_agrees_with_the_backend_on_keys_and_endpoints(wg):
    assert wg.check_key(KEY_A) == KEY_A
    for bad in ("x", KEY_A + "\nPostUp = rm -rf /", None):
        with pytest.raises(wg.HelperError):
            wg.check_key(bad)
    assert wg.check_endpoint("pi.example.com:51820")
    for bad in ("host", "host:1", "a b:51820", "x\nPostUp=1:51820"):
        with pytest.raises(wg.HelperError):
            wg.check_endpoint(bad)


def test_the_helper_only_accepts_private_addresses(wg):
    assert wg.check_interface("10.77.0.1/16") == "10.77.0.1/16"
    for bad in ("8.8.8.8/32", "127.0.0.1/32", "10.77.0.1/16; rm", "fd00::1/64", "10.0.0.1/4"):
        with pytest.raises(wg.HelperError):
            wg.check_interface(bad)
    with pytest.raises(wg.HelperError):
        wg.check_port(80)


def test_the_rendered_configuration_contains_nothing_but_validated_values(wg):
    state = {"address": "10.77.0.1/16", "listen_port": 51820,
             "peers": [{"public_key": KEY_B, "allowed_ips": ["10.77.0.2/32"]}]}
    conf = wg.render_conf(state, KEY_A)
    assert conf.splitlines()[:4] == ["[Interface]", f"PrivateKey = {KEY_A}", "Address = 10.77.0.1/16", "ListenPort = 51820"]
    assert f"PublicKey = {KEY_B}" in conf and "AllowedIPs = 10.77.0.2/32" in conf
    assert "PostUp" not in conf and "PreUp" not in conf
    evil = {**state, "peers": [{"public_key": KEY_B + "\nPostUp = x", "allowed_ips": ["10.77.0.2/32"]}]}
    with pytest.raises(wg.HelperError):
        wg.render_conf(evil, KEY_A)


def test_peers_must_be_nodes_of_the_hub_network(wg, monkeypatch):
    wg.save_state({"role": "hub", "address": "10.77.0.1/16", "listen_port": 51820, "peers": []})
    applied = []
    monkeypatch.setattr(wg, "apply", lambda state, previous=None: applied.append(state))

    out = json.loads(wg.cmd_peers([], json.dumps([{"public_key": KEY_B, "ip": "10.77.0.2"}])))
    assert out == {"peers": 1} and applied[0]["peers"] == [{"public_key": KEY_B, "allowed_ips": ["10.77.0.2/32"]}]

    for bad in ([{"public_key": KEY_B, "ip": "10.99.0.2"}],                       # outside the network
                [{"public_key": KEY_B, "ip": "10.77.0.1"}],                       # the hub itself
                [{"public_key": KEY_B, "ip": "10.77.0.2"}, {"public_key": KEY_A, "ip": "10.77.0.2"}],   # same address twice
                [{"public_key": KEY_B, "ip": "10.77.0.2"}, {"public_key": KEY_B, "ip": "10.77.0.3"}],   # same key twice
                [{"public_key": "nope", "ip": "10.77.0.2"}], "not a list", [1]):
        with pytest.raises(wg.HelperError):
            wg.cmd_peers([], json.dumps(bad))
    with pytest.raises(wg.HelperError):
        wg.cmd_peers([], "{not json")


def test_peers_are_refused_on_a_node(wg):
    wg.save_state({"role": "node", "address": "10.77.0.2/32", "peers": []})
    with pytest.raises(wg.HelperError):
        wg.cmd_peers([], "[]")


def test_the_helper_refuses_to_run_without_root(wg, monkeypatch, capsys):
    monkeypatch.delenv("CLOUDBASE_WG_ALLOW_NON_ROOT_FOR_TESTS_ONLY", raising=False)
    monkeypatch.setattr(wg.os, "geteuid", lambda: 1000, raising=False)
    assert wg.main(["status"]) == 1 and "root" in capsys.readouterr().err


def test_state_and_key_files_are_private(wg):
    if sys.platform == "win32":
        pytest.skip("file modes are not meaningful on Windows")
    wg.save_state({"role": "hub"})
    assert (Path(wg.CONF_DIR) / "state.json").stat().st_mode & 0o777 == 0o600


# ── the client ────────────────────────────────────────────────────────────────
@pytest.fixture
def fake_helper(tmp_path, monkeypatch):
    script = tmp_path / "fake.py"
    script.write_text(
        "import sys, json\n"
        "cmd = sys.argv[1]\n"
        "if cmd == 'fail':\n    print('boom', file=sys.stderr); sys.exit(1)\n"
        "if cmd == 'peers':\n    print(json.dumps({'peers': len(json.load(sys.stdin))}))\n"
        "elif cmd == 'hub':\n    print(json.dumps({'public_key': 'K' + sys.argv[2]}))\n"
        "else:\n    print(json.dumps({'up': True, 'args': sys.argv[1:]}))\n")
    monkeypatch.setenv("CLOUDBASE_WG_HELPER", f"{sys.executable} {script}")
    return script


def test_the_client_talks_to_the_helper(fake_helper):
    assert helper.start_hub("10.77.0.1/16", 51820) == "K10.77.0.1/16"
    assert helper.set_peers([{"public_key": KEY_A, "ip": "10.77.0.2"}] * 3) == 3
    assert helper.status()["up"] is True


def test_the_client_reports_failures(fake_helper, monkeypatch):
    with pytest.raises(helper.HelperError, match="boom"):
        helper._run(["fail"])
    monkeypatch.setenv("CLOUDBASE_WG_HELPER", "/definitely/not/here")
    with pytest.raises(helper.HelperError, match="not installed"):
        helper.status()


# ── installation ──────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent


def test_the_setup_script_installs_the_helper_the_client_expects():
    setup = (ROOT / "scripts" / "setup-wireguard.sh").read_text(encoding="utf-8")
    assert f'HELPER="{helper.HELPER}"' in setup
    assert "NOPASSWD: ${HELPER} *" in setup and "Before=docker.service" in setup and "cloudbase-wireguard" in setup


def test_install_and_start_know_about_the_node_network():
    install = (ROOT / "install.sh").read_text(encoding="utf-8")
    start = (ROOT / "start.sh").read_text(encoding="utf-8")
    assert "setup-wireguard.sh" in install and "step_wireguard" in install
    assert "setup-wireguard.sh" in start and "wireguard|wg)" in start


def test_no_trace_of_the_reverse_tunnels_is_left():
    for gone in ("routers/nodes/tunnel.py", "nodes/tunnel.py", "agent/tunnels.py"):
        assert not (ROOT / "backend" / gone).exists(), gone
    from core import models
    assert not hasattr(models.Instance, "tunnel_port")
