"""Tenant mode (core/tenant.py): what changes when the apps belong to people you don't know."""
import asyncio
import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def tenant_on(monkeypatch):
    from core import config
    settings = {**config._DEFAULTS["tenant"], "enabled": True}
    monkeypatch.setattr(config, "get_tenant", lambda: settings)
    return settings


def _node(node_id, is_local, cpu=0.0):
    return type("N", (), {"id": node_id, "is_local": is_local, "node_cpu_percent": cpu})()


# ── the setting ───────────────────────────────────────────────────────────────
def test_off_changes_nothing():
    from core import tenant
    assert tenant.enabled() is False and tenant.hardening() is None
    assert tenant.limits(None, None) == (None, None)
    assert tenant.network_access("open") == "open"
    assert tenant.may_run_on(_node(1, True))


def test_on_every_container_gets_limits_and_hardening(tenant_on):
    from core import tenant
    h = tenant.hardening()
    assert h["pids_limit"] == 512 and h["isolated_build"] and h["build_timeout_seconds"] == 1800
    assert "NET_RAW" not in h["cap_add"] and "SYS_ADMIN" not in h["cap_add"] and "CHOWN" in h["cap_add"]
    assert tenant.limits(None, None) == (1.0, 512)
    assert tenant.limits(2.0, 2048) == (2.0, 2048)                   # what was set stays
    assert tenant.network_access("open") == "internet" and tenant.network_access("none") == "none"
    assert not tenant.may_run_on(_node(1, True)) and tenant.may_run_on(_node(2, False))


# ── on the node: what Docker gets ─────────────────────────────────────────────
def test_the_container_gets_no_risky_capabilities_and_a_process_limit(tenant_on):
    from core import tenant
    from containers import hardening
    kwargs = {"security_opt": ["no-new-privileges:true"]}
    hardening.apply(kwargs, tenant.hardening())
    assert kwargs["cap_drop"] == ["ALL"] and kwargs["cap_add"] == tenant.CAPABILITIES
    assert kwargs["pids_limit"] == 512 and kwargs["ulimits"][0]["Name"] == "nofile"
    assert kwargs["security_opt"] == ["no-new-privileges:true"]
    assert "runtime" not in kwargs and "storage_opt" not in kwargs
    plain = {}
    hardening.apply(plain, None)
    assert plain == {}


def test_a_sandboxed_container_never_starts_without_its_sandbox(monkeypatch):
    from containers import hardening
    monkeypatch.setattr(hardening, "_available_runtimes", lambda: {"runc"})
    monkeypatch.setattr(hardening, "forget_runtimes", lambda: None)
    with pytest.raises(hardening.RuntimeMissing, match="setup-gvisor"):
        hardening.apply({}, {"runtime": "runsc", "cap_add": []})
    monkeypatch.setattr(hardening, "_available_runtimes", lambda: {"runc", "runsc"})
    from containers import disk_area
    monkeypatch.setattr(disk_area, "limit_supported", lambda *_: False)         # no file system for it: no limit, it still starts
    kwargs = {}
    hardening.apply(kwargs, {"runtime": "runsc", "cap_add": [], "disk_mb": 2048})
    assert "storage_opt" not in kwargs
    monkeypatch.setattr(disk_area, "limit_supported", lambda *_: True)
    kwargs = {}
    hardening.apply(kwargs, {"runtime": "runsc", "cap_add": [], "disk_mb": 2048})
    assert kwargs["runtime"] == "runsc" and kwargs["storage_opt"] == {"size": "2048M"}


# ── on the primary: what goes into the spec ───────────────────────────────────
def _app():
    from core.models import Application
    return Application(id=99001, name="t-app", repo_url="https://example.test/x", start_command="x", port=3000,
                       network_access="open")


def test_an_apps_spec_carries_the_hardening_and_never_the_local_network(tenant_on, monkeypatch):
    from apps import runtime
    monkeypatch.setattr(runtime.db_links, "runtime_options", lambda app_id: {})
    options = runtime._docker_runtime_options(_app())
    assert options["hardening"]["pids_limit"] == 512
    assert options["network_access"] == "internet"                   # an old "open" app is shut in too
    assert (options["cpu_limit"], options["memory_limit_mb"]) == (1.0, 512)
    assert options["bind_host"] == "127.0.0.1"


def test_without_tenant_mode_the_spec_is_as_before(monkeypatch):
    from apps import runtime
    monkeypatch.setattr(runtime.db_links, "runtime_options", lambda app_id: {})
    options = runtime._docker_runtime_options(_app())
    assert options["hardening"] is None and options["network_access"] == "open" and options["cpu_limit"] is None


def test_a_databases_spec_carries_the_hardening(tenant_on):
    import json
    from core.env_crypto import encrypt_text
    from core.models import Database
    from dbs.provision import _spec
    spec = _spec(Database(id=4, name="x", engine="postgres", version="16",
                          credentials=encrypt_text(json.dumps({"user": "u", "password": "p", "db_name": "d"}))))
    assert spec["hardening"]["cap_add"] and (spec["cpu_limit"], spec["memory_limit_mb"]) == (1.0, 512)


def test_the_local_network_is_refused_for_an_app(tenant_on):
    from fastapi import HTTPException
    from apps.validation import _network_access
    with pytest.raises(HTTPException):
        _network_access("open")
    assert _network_access(None, default="open") == "internet" and _network_access("none") == "none"


def test_new_instances_are_never_placed_on_the_primary(tenant_on):
    from workers.autoscaler import pick_nodes
    assert [n.id for n in pick_nodes([_node(1, True), _node(2, False, cpu=90.0)], 3)] == [2, 2, 2]
    assert pick_nodes([_node(1, True)], 2) == []


def test_no_database_is_created_on_the_primary(admin, tenant_on):
    from core.database import AsyncSessionLocal
    from nodes.registry import ensure_local_node

    async def primary():
        async with AsyncSessionLocal() as db:
            return (await ensure_local_node(db)).id
    r = admin.post("/api/databases", json={"name": "tenant-db", "engine": "postgres", "node_id": _run(primary())})
    assert r.status_code == 400 and "primary" in r.json()["detail"]


def test_the_node_list_says_whether_tenant_mode_is_on(client, monkeypatch):
    from core import config
    from core.database import AsyncSessionLocal
    from nodes import snapshot

    async def snap():
        async with AsyncSessionLocal() as db:
            return await snapshot.snapshot(db, 999999)
    off = _run(snap())
    monkeypatch.setattr(config, "get_tenant", lambda: {"enabled": True})
    on = _run(snap())
    assert off["tenant"] is False and on["tenant"] is True
    assert off["hash"] != on["hash"]                     # a node that has the list sees that it changed


def test_a_node_sets_its_firewall_when_tenant_mode_changes(monkeypatch):
    from containers import reconciler
    calls = []
    monkeypatch.setattr(reconciler.quota, "tenant_firewall", lambda on: calls.append(on))
    r = reconciler.Reconciler()
    for on in (True, True, False, False):
        _run(r._apply_tenant(on))
    assert calls == [True, False]


# ── builds ────────────────────────────────────────────────────────────────────
def test_a_strangers_build_never_gets_the_host_network(tmp_path, monkeypatch):
    from containers import build
    seen = {}
    monkeypatch.setattr(build, "_cleanup_before_build", lambda *a, **k: None)
    monkeypatch.setattr(build, "buildkit_available", lambda: True)
    monkeypatch.setattr(build, "linked_build_env",
                        lambda linked: ({"DATABASE_URL": "postgresql://u:p@172.18.0.2:5432/d"}, True))
    monkeypatch.setattr(build, "_run_build", lambda *a, **k: seen.update(k))
    (tmp_path / "app.py").write_text("print(1)\n")
    lines = []
    build.build_image(1, str(tmp_path), lambda _a, line: lines.append(line), "python", "python app.py", 8000,
                      linked_databases=[{"database_id": 1}], hardening={"build_timeout_seconds": 60})
    assert seen["host_network"] is False and seen["hardening"] == {"build_timeout_seconds": 60}
    assert any("can't reach the linked database" in line for line in lines)


def test_a_build_that_takes_too_long_is_stopped(tmp_path, monkeypatch):
    import subprocess
    from containers import build
    real = subprocess.Popen
    monkeypatch.setattr(build.subprocess, "Popen", lambda cmd, **k: real(
        [sys.executable, "-c", "import time; print('step 1', flush=True); time.sleep(30)"], **k))
    with pytest.raises(RuntimeError, match="longer than 1 s"):
        build._build_with_cli(1, str(tmp_path), "img", lambda *a: None, timeout_seconds=1)


def test_the_classic_builder_is_refused_in_tenant_mode(monkeypatch):
    from containers import build
    monkeypatch.setattr(build, "buildkit_available", lambda: False)
    with pytest.raises(RuntimeError, match="BuildKit"):
        build._run_build(1, ".", "img", lambda *a: None, hardening={"build_timeout_seconds": 60})


# ── the firewall (scripts/cloudbase-quota) ────────────────────────────────────
@pytest.fixture
def helper(tmp_path, monkeypatch):
    script = ROOT / "scripts" / "cloudbase-quota"
    loader = importlib.machinery.SourceFileLoader("cloudbase_quota_tenant", str(script))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("cloudbase_quota_tenant", loader))
    loader.exec_module(module)
    monkeypatch.setattr(module, "TENANT_FLAG", str(tmp_path / "tenant"))
    calls, filter_calls = [], []
    monkeypatch.setattr(module, "_iptables", lambda *args: calls.append(list(args)) or "")       # the mangle table
    monkeypatch.setattr(module, "_filter", lambda *args: filter_calls.append(list(args)) or "")
    module.calls, module.filter_calls = calls, filter_calls
    return module


def test_the_firewall_blocks_mail_and_keeps_builds_off_private_addresses(helper):
    helper.apply_tenant(True)
    rules = [c[2:] for c in helper.calls if c[:2] == ["-A", helper.TENANT_CHAIN]]
    for bridge in ("cbapps0", "cbappl0", "cbappx0", "docker0"):
        assert ["-i", bridge, "-p", "tcp", "--dport", "25", "-j", "DROP"] in rules
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"):
        assert ["-i", "docker0", "-d", network, "-j", "DROP"] in rules
    dns = rules.index(["-i", "docker0", "-p", "udp", "--dport", "53", "-j", "RETURN"])
    assert dns < rules.index(["-i", "docker0", "-d", "192.168.0.0/16", "-j", "DROP"])   # the resolver may be on the LAN
    to_machine = [c[2:] for c in helper.calls if c[:2] == ["-A", helper.BUILD_INPUT_CHAIN]]
    assert to_machine[-1] == ["-j", "DROP"]
    # straight from FORWARD in mangle: not behind Docker's own rules, whichever iptables Docker uses
    assert ["-C", "FORWARD", "-j", helper.TENANT_CHAIN] in helper.calls
    assert ["-C", "INPUT", "-i", "docker0", "-j", helper.BUILD_INPUT_CHAIN] in helper.calls
    assert ["-D", "DOCKER-USER", "-j", helper.TENANT_CHAIN] in helper.filter_calls     # the old place is cleared
    assert Path(helper.TENANT_FLAG).exists()                       # put back at boot


def test_off_empties_the_rules_and_forgets_them_at_boot(helper):
    helper.apply_tenant(True)
    helper.calls.clear()
    helper.apply_tenant(False)
    assert not [c for c in helper.calls if c[0] == "-A"]
    assert not Path(helper.TENANT_FLAG).exists()


# ── what a node's token can do ────────────────────────────────────────────────
def test_a_node_token_opens_none_of_the_panels_websockets(client):
    from starlette.websockets import WebSocketDisconnect
    from core.database import AsyncSessionLocal
    from core.models import Node

    async def make():
        async with AsyncSessionLocal() as db:
            db.add(Node(name="ws-node", status="online", auth_token="token-ws-node", is_local=False, enabled=True))
            await db.commit()
    _run(make())
    for path in ("/ws/system/stats", "/ws/apps/1/stats"):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(path, headers={"x-node-token": "token-ws-node"}) as ws:
                ws.receive_text()


def test_only_the_node_a_stream_was_opened_on_can_fill_it():
    from nodes import state
    q = state.subscribe_node_stream("s-1", node_id=3)
    state.push_node_stream_data("s-1", "from another node", node_id=4)
    state.push_node_stream_data("s-1", "from its node", node_id=3)
    assert q.get_nowait() == "from its node" and q.empty()
    state.unsubscribe_node_stream("s-1", q)


# ── installation ──────────────────────────────────────────────────────────────
def test_the_scripts_are_there_and_reachable_from_the_cli():
    start = (ROOT / "start.sh").read_text(encoding="utf-8")
    assert "setup-gvisor.sh" in start and "tenant-check.sh" in start
    gvisor = (ROOT / "scripts" / "setup-gvisor.sh").read_text(encoding="utf-8")
    assert "sha512sum -c" in gvisor and "runsc install" in gvisor
    config = (ROOT / "config.yaml").read_text(encoding="utf-8")
    assert "tenant:" in config and "enabled: false" in config


# ── cloudbase tenant (backend/tenant_setup.py) ────────────────────────────────
@pytest.fixture
def wizard(monkeypatch):
    import tenant_setup
    from core import config
    written = {}
    monkeypatch.setattr(config, "get_tenant", lambda: dict(config._DEFAULTS["tenant"]))
    monkeypatch.setattr(config, "save_config_sections", lambda sections: written.update(sections))
    monkeypatch.setattr(tenant_setup, "is_node", lambda: False)
    empty = {"apps": [], "cron": [], "databases": [], "nodes": [("node-1", "online")]}

    async def world():
        return dict(empty)
    monkeypatch.setattr(tenant_setup, "on_the_primary", world)
    monkeypatch.setattr(tenant_setup.sys.stdin, "isatty", lambda: True, raising=False)

    def run(argv, answers=()):
        replies = iter(answers)
        monkeypatch.setattr("builtins.input", lambda prompt="": next(replies))
        return tenant_setup.main(argv), written.get("tenant")
    run.world = empty
    return run


def test_the_wizard_asks_and_writes_what_was_chosen(wizard):
    # gVisor yes, not for databases, 0.5 CPU, 256 MB, 300 processes, 10 minutes, no disk limit, then confirm
    rc, written = wizard(["on"], ["y", "", "0.5", "256", "300", "10", "", ""])
    assert rc == 0
    assert written == {"enabled": True, "default_cpu_limit": 0.5, "default_memory_limit_mb": 256, "pids_limit": 300,
                       "runtime": "runsc", "sandbox_databases": False, "build_timeout_seconds": 600,
                       "container_disk_mb": 1024}


def test_the_wizard_asks_again_for_a_wrong_number_and_can_be_cancelled(wizard):
    rc, written = wizard(["on"], ["n", "lots", "1", "", "", "", "", "n"])
    assert rc == 3 and written is None


def test_yes_takes_the_defaults_without_asking(wizard):
    rc, written = wizard(["on", "--yes"])
    assert rc == 0 and written["enabled"] is True and written["runtime"] == ""


def test_with_something_on_the_primary_it_asks_first(wizard):
    wizard.world["apps"] = ["shop"]
    rc, written = wizard(["on"], ["n"])
    assert rc == 3 and written is None


def test_turning_off_and_status(wizard, monkeypatch):
    from core import config
    assert wizard(["off"])[0] == 3                                 # already off
    monkeypatch.setattr(config, "get_tenant", lambda: {**config._DEFAULTS["tenant"], "enabled": True})
    rc, written = wizard(["off"], ["y"])
    assert rc == 0 and written["enabled"] is False
    assert wizard(["status"])[0] == 3


def test_a_node_sends_you_to_the_primary(wizard, monkeypatch):
    import tenant_setup
    monkeypatch.setattr(tenant_setup, "is_node", lambda: True)
    assert wizard(["on"])[0] == 1


def test_cloudbase_tenant_runs_the_wizard_and_restarts():
    start = (ROOT / "start.sh").read_text(encoding="utf-8")
    assert "tenant_setup.py" in start and "cmd_tenant" in start and "systemctl restart" in start
