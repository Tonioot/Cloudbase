"""Export -> import round trip through the real API, against the throwaway database."""
import asyncio
import json

import pytest

from core import auth
from conftest import PASSWORD

PASSPHRASE = "a long enough passphrase"
SECRET_ENV = "s3cr3t-env-value-9f2c"
SECRET_TOKEN = "ghp_exampleTokenValue0123456789"


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    auth._login_attempts.clear()
    yield
    auth._login_attempts.clear()


def _run(coro):
    return asyncio.run(coro)


def _make_app(name: str, instances: int = 2, status: str = "running", port: int = 9100, **extra) -> int:
    """Insert an app (and instances) straight into the database, like an earlier install would have."""
    from core.database import AsyncSessionLocal
    from core.env_crypto import encrypt_env, encrypt_text
    from core.models import Application, ApplicationReplica
    from nodes.registry import ensure_local_node

    async def go():
        async with AsyncSessionLocal() as db:
            node = await ensure_local_node(db)
            app = Application(
                name=name, repo_url=f"https://github.com/acme/{name}", start_command="npm start", build_command="npm run build",
                port=3000, external_port=port, status=status, restart_policy="always", auto_start=True,
                env_vars=encrypt_env({"SECRET_KEY": SECRET_ENV, "NODE_ENV": "production"}),
                github_token=encrypt_text(SECRET_TOKEN), docker_memory_limit_mb=256,
                domain=None, extra_domains=json.dumps(["www.example.test"]), **extra)
            db.add(app)
            await db.flush()
            for i in range(instances):
                db.add(ApplicationReplica(app_id=app.id, node_id=node.id, status=status, external_port=port + 1 + i))
            await db.commit()
            return app.id
    return _run(go())


def _delete_app(app_id: int) -> None:
    from sqlalchemy import delete
    from core.database import AsyncSessionLocal
    from core.models import Application, ApplicationReplica

    async def go():
        async with AsyncSessionLocal() as db:
            await db.execute(delete(ApplicationReplica).where(ApplicationReplica.app_id == app_id))
            await db.execute(delete(Application).where(Application.id == app_id))
            await db.commit()
    _run(go())


def _load_app(name: str):
    from sqlalchemy import select
    from core.database import AsyncSessionLocal
    from core.models import Application, ApplicationReplica

    async def go():
        async with AsyncSessionLocal() as db:
            app = (await db.execute(select(Application).where(Application.name == name))).scalar_one_or_none()
            if not app:
                return None, []
            reps = (await db.execute(select(ApplicationReplica).where(ApplicationReplica.app_id == app.id))).scalars().all()
            return app, reps
    return _run(go())


def _export(client, app_ids=None, passphrase=PASSPHRASE, password=PASSWORD, secrets=True):
    return client.post("/api/transfer/export", json={
        "app_ids": app_ids, "passphrase": passphrase, "password": password, "include_secrets": secrets})


def _import(client, blob: bytes, passphrase=PASSPHRASE, **form):
    return client.post("/api/transfer/import", files={"file": ("x.cloudbase", blob)}, data={"passphrase": passphrase, **form})


@pytest.fixture
def deploys(monkeypatch):
    """Importing deploys in the background; record that instead of cloning from GitHub."""
    from transfer import importer
    calls = {"deployed": [], "started": []}

    async def fake_deploy(app):
        calls["deployed"].append(app.name)

    async def fake_start(app_id, db, actor):
        calls["started"].append(app_id)

    monkeypatch.setattr(importer, "_deploy_app", fake_deploy)
    monkeypatch.setattr(importer, "start_app_now", fake_start)
    return calls


# ── export ───────────────────────────────────────────────────────────────────
def test_export_is_encrypted_and_needs_the_password(admin):
    app_id = _make_app("tx-secrets", port=9100)
    try:
        r = _export(admin, [app_id])
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/octet-stream"
        assert r.headers["cache-control"] == "no-store"
        assert r.headers["content-disposition"].endswith('.cloudbase"')
        for plain in (SECRET_ENV.encode(), SECRET_TOKEN.encode(), b"tx-secrets", b"github.com"):
            assert plain not in r.content, plain

        assert _export(admin, [app_id], password="not my password").status_code == 403
        assert _export(admin, [app_id], passphrase="short").status_code == 400
    finally:
        _delete_app(app_id)


def test_export_requires_a_login(client):
    from fastapi.testclient import TestClient
    fresh = TestClient(client.app, base_url="http://localhost")
    assert _export(fresh).status_code in (401, 403)


def test_export_is_blocked_over_plain_http_from_a_public_address(admin, monkeypatch):
    from routers import transfer
    for private in ("192.168.1.20", "10.0.0.5", "100.64.3.2", "127.0.0.1"):    # LAN, VPN, loopback: allowed
        monkeypatch.setattr(transfer, "_client_ip", lambda request, ip=private: ip)
        assert _export(admin).status_code == 200, private
        auth._login_attempts.clear()
    monkeypatch.setattr(transfer, "_client_ip", lambda request: "8.8.8.8")
    r = _export(admin)
    assert r.status_code == 403 and "https" in r.json()["detail"]
    r = admin.post("/api/transfer/export", headers={"x-forwarded-proto": "https"},
                   json={"passphrase": PASSPHRASE, "password": PASSWORD})
    assert r.status_code == 200


def test_the_audit_log_records_the_export_without_secrets(admin):
    app_id = _make_app("tx-audit", port=9110)
    try:
        assert _export(admin, [app_id]).status_code == 200
        log = admin.get("/api/audit-log").text
        assert "transfer.export" in log
        assert SECRET_ENV not in log and SECRET_TOKEN not in log and PASSPHRASE not in log
    finally:
        _delete_app(app_id)


# ── the round trip ────────────────────────────────────────────────────────────
def test_round_trip_restores_envs_token_and_instances(admin, deploys):
    app_id = _make_app("tx-roundtrip", instances=2, port=9120)
    blob = _export(admin, [app_id]).content
    _delete_app(app_id)
    assert _load_app("tx-roundtrip")[0] is None

    r = _import(admin, blob)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skipped"] == []
    assert body["imported"] == [{"name": "tx-roundtrip", "instances": 2, "will_start": True}]

    app, replicas = _load_app("tx-roundtrip")
    try:
        from core.env_crypto import decrypt_env, decrypt_text
        assert decrypt_env(app.env_vars) == {"SECRET_KEY": SECRET_ENV, "NODE_ENV": "production"}
        assert decrypt_text(app.github_token) == SECRET_TOKEN
        assert app.repo_url == "https://github.com/acme/tx-roundtrip"
        assert (app.start_command, app.build_command, app.port) == ("npm start", "npm run build", 3000)
        assert app.restart_policy == "always" and app.auto_start is True and app.docker_memory_limit_mb == 256
        assert json.loads(app.extra_domains) == ["www.example.test"]
        assert len(replicas) == 2 and all(r.node_id is not None and r.external_port for r in replicas)
        assert len({r.external_port for r in replicas}) == 2                   # fresh, distinct ports
        # deployed and started in the background (BackgroundTasks ran after the response)
        assert deploys["deployed"] == ["tx-roundtrip"] and deploys["started"] == [app.id]
    finally:
        _delete_app(app.id)


def test_an_app_that_was_stopped_is_not_started(admin, deploys):
    app_id = _make_app("tx-stopped", instances=1, status="stopped", port=9130)
    blob = _export(admin, [app_id]).content
    _delete_app(app_id)
    body = _import(admin, blob).json()
    app, _ = _load_app("tx-stopped")
    try:
        assert body["imported"][0]["will_start"] is False
        assert deploys["deployed"] == ["tx-stopped"] and deploys["started"] == []
    finally:
        _delete_app(app.id)


def test_existing_names_are_skipped_with_a_reason(admin, deploys):
    app_id = _make_app("tx-exists", port=9140)
    try:
        blob = _export(admin, [app_id]).content
        body = _import(admin, blob).json()          # the app is still there
        assert body["imported"] == []
        assert body["skipped"][0]["name"] == "tx-exists" and "already" in body["skipped"][0]["reason"]
        assert deploys["deployed"] == []
    finally:
        _delete_app(app_id)


def test_one_bad_app_does_not_stop_the_others(admin, deploys, monkeypatch):
    from transfer import manifest
    good, bad = _make_app("tx-good", port=9150, instances=1), _make_app("tx-bad", port=9160, instances=1)
    blob = _export(admin, [good, bad]).content
    _delete_app(good)
    _delete_app(bad)
    # corrupt one entry after decrypting, as a hand-edited file would be
    real = manifest.check_manifest

    def tamper(m):
        for a in m["apps"]:
            if a["name"] == "tx-bad":
                a["repo_url"] = "file:///etc/passwd"
        return real(m)
    monkeypatch.setattr(manifest, "check_manifest", tamper)
    from routers import transfer
    monkeypatch.setattr(transfer.manifest, "check_manifest", tamper)

    body = _import(admin, blob).json()
    app, _ = _load_app("tx-good")
    try:
        assert [i["name"] for i in body["imported"]] == ["tx-good"]
        assert [s["name"] for s in body["skipped"]] == ["tx-bad"]
        assert _load_app("tx-bad")[0] is None
    finally:
        _delete_app(app.id)


def test_a_cron_job_is_imported_without_instances(admin, deploys):
    app_id = _make_app("tx-cron", instances=0, status="stopped", port=9170, cron_schedule="*/5 * * * *", no_web=True)
    blob = _export(admin, [app_id]).content
    _delete_app(app_id)
    body = _import(admin, blob).json()
    app, replicas = _load_app("tx-cron")
    try:
        assert body["imported"][0]["instances"] == 0
        assert app.cron_schedule == "*/5 * * * *" and replicas == []
    finally:
        _delete_app(app.id)


# ── import errors ─────────────────────────────────────────────────────────────
def test_import_with_the_wrong_passphrase(admin):
    app_id = _make_app("tx-wrongpass", port=9180)
    try:
        blob = _export(admin, [app_id]).content
        r = _import(admin, blob, passphrase="another long passphrase")
        assert r.status_code == 400 and "Wrong passphrase" in r.json()["detail"]
    finally:
        _delete_app(app_id)


def test_import_of_something_that_is_not_an_export(admin):
    assert _import(admin, b"just some text, not an export").status_code == 400
    assert _import(admin, b"").status_code == 400


def test_import_of_a_newer_format_is_refused_with_a_clear_message(admin):
    from transfer import crypto, manifest
    blob = crypto.encrypt_bytes(json.dumps({"format": manifest.FORMAT, "version": 999, "apps": []}).encode(), PASSPHRASE)
    r = _import(admin, blob)
    assert r.status_code == 400 and "newer Cloudbase" in r.json()["detail"]


def test_import_into_an_unknown_node(admin):
    from transfer import crypto, manifest
    blob = crypto.encrypt_bytes(json.dumps({"format": manifest.FORMAT, "version": 1, "apps": []}).encode(), PASSPHRASE)
    assert _import(admin, blob, target_node_id="99999").status_code == 400


def test_the_old_unencrypted_endpoints_are_gone(admin):
    assert admin.post("/api/apps/export", json={}).status_code in (404, 405, 422)


# ── who may take secrets out ──────────────────────────────────────────────────
# Secrets are write-only everywhere else in Cloudbase, so the export must not become a side door.
_counter = iter(range(1000))


def _login_with_permissions(admin, permissions: list[str]):
    """A new user, in a new role holding exactly these permissions, logged in on its own client."""
    from fastapi.testclient import TestClient
    n = next(_counter)
    ids = {p["name"]: p["id"] for p in admin.get("/api/roles/permissions").json()}
    role = admin.post("/api/roles", json={"name": f"tx-role-{n}", "description": "test",
                                          "permission_ids": [ids[p] for p in permissions]})
    assert role.status_code == 200, role.text
    admin.post("/api/users", json={"username": f"tx-user-{n}", "password": "a-test-password", "role_id": role.json()["id"]})
    auth._login_attempts.clear()
    client = TestClient(admin.app, base_url="http://localhost")
    assert client.post("/api/auth/login", json={"username": f"tx-user-{n}", "password": "a-test-password"}).status_code == 200
    return client


def test_an_export_without_secrets_has_the_names_but_never_the_values(admin):
    from transfer import crypto
    app_id = _make_app("tx-nosecrets", port=9190)
    try:
        r = _export(admin, [app_id], secrets=False, password="")          # no password needed without secrets
        assert r.status_code == 200
        text = crypto.decrypt_bytes(r.content, PASSPHRASE).decode()
        assert SECRET_ENV not in text and SECRET_TOKEN not in text
        entry = json.loads(text)["apps"][0]
        assert json.loads(text)["secrets_included"] is False
        assert entry["env_vars"] == {} and entry["env_names"] == ["NODE_ENV", "SECRET_KEY"]
        assert entry["git_token"] is None and entry["has_git_token"] is True
    finally:
        _delete_app(app_id)


def test_a_role_without_secrets_export_cannot_take_secrets_even_with_system_manage(admin):
    app_id = _make_app("tx-denied", port=9200)
    try:
        user = _login_with_permissions(admin, ["apps.view", "apps.configure", "system.manage"])
        r = _export(user, [app_id], secrets=True)
        assert r.status_code == 403 and "secrets.export" in r.json()["detail"]
        assert _export(user, [app_id], secrets=False, password="").status_code == 200      # settings only is fine
    finally:
        _delete_app(app_id)


def test_the_administrator_role_does_not_include_secrets_export(admin):
    roles = {r["name"]: r["id"] for r in admin.get("/api/roles").json()}
    admin.post("/api/users", json={"username": "tx-administrator", "password": "a-test-password", "role_id": roles["Administrator"]})
    from fastapi.testclient import TestClient
    auth._login_attempts.clear()
    user = TestClient(admin.app, base_url="http://localhost")
    assert user.post("/api/auth/login", json={"username": "tx-administrator", "password": "a-test-password"}).status_code == 200
    perms = user.get("/api/auth/check").json()["permissions"]
    assert "apps.configure" in perms and "system.manage" in perms          # a full administrator ...
    assert "secrets.export" not in perms                                   # ... still has to be given this on purpose
    assert _export(user, secrets=True).status_code == 403
    # the permission exists and the root account can hand it out
    assert any(p["name"] == "secrets.export" for p in admin.get("/api/roles/permissions").json())


def test_a_role_that_was_given_secrets_export_can_take_secrets_with_its_own_password(admin):
    app_id = _make_app("tx-allowed", port=9210)
    try:
        user = _login_with_permissions(admin, ["apps.view", "apps.configure", "secrets.export"])
        assert _export(user, [app_id], secrets=True, password=PASSWORD).status_code == 403     # root's password is not theirs
        auth._login_attempts.clear()
        assert _export(user, [app_id], secrets=True, password="a-test-password").status_code == 200
    finally:
        _delete_app(app_id)


def test_nobody_can_create_a_role_with_secrets_export_except_root(admin):
    user = _login_with_permissions(admin, ["apps.view", "roles.manage"])
    ids = {p["name"]: p["id"] for p in admin.get("/api/roles/permissions").json()}
    r = user.post("/api/roles", json={"name": "tx-sneaky", "permission_ids": [ids["secrets.export"]]})
    assert r.status_code in (401, 403)


def test_importing_a_file_without_secrets_says_what_to_fill_in(admin, deploys):
    app_id = _make_app("tx-fillin", port=9220, instances=1)
    blob = _export(admin, [app_id], secrets=False, password="").content
    _delete_app(app_id)
    body = _import(admin, blob).json()
    app, _ = _load_app("tx-fillin")
    try:
        warnings = " ".join(body["warnings"])
        assert "NODE_ENV, SECRET_KEY" in warnings and "Git token" in warnings
        from core.env_crypto import decrypt_env
        assert decrypt_env(app.env_vars) == {} and app.github_token is None
    finally:
        _delete_app(app.id)


def test_importing_needs_the_right_to_deploy_apps(admin):
    user = _login_with_permissions(admin, ["apps.view"])
    from transfer import crypto, manifest
    blob = crypto.encrypt_bytes(json.dumps({"format": manifest.FORMAT, "version": 1, "apps": []}).encode(), PASSPHRASE)
    assert _import(user, blob).status_code == 403
