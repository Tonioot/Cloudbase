"""The other two kinds of export: the Cloudbase settings (users, roles, channels, tokens, storage, System settings)
and everything (a full backup of the installation that the panel and `cloudbase restore` can both put back)."""
import asyncio
import io
import json
import os
import sqlite3
import tarfile

import pytest

from transfer import full


def _run(coro):
    return asyncio.run(coro)


# ── Cloudbase settings ────────────────────────────────────────────────────────

def test_the_settings_come_along_and_what_exists_is_skipped(client):
    from sqlalchemy import delete, select
    from core import auth, token_vault
    from core.database import AsyncSessionLocal
    from core.models import NotificationChannel, Role, User
    from notify import config as notify_config
    from transfer import settings

    async def make():
        async with AsyncSessionLocal() as db:
            role = Role(name="Deployers", description="Can deploy")
            db.add(role)
            await db.flush()
            db.add(User(username="deployer", password_hash=auth.hash_password("a-deployer-password"), role="Deployers", role_id=role.id))
            channel = NotificationChannel(name="Team Slack", type="slack", events="[]", app_ids="[]")
            notify_config.store_config(channel, {"webhook_url": "https://hooks.slack.com/services/T/B/secret"})
            db.add(channel)
            await db.commit()
        token_vault.add("work", "ghp_the-token-value")
    _run(make())

    async def export():
        async with AsyncSessionLocal() as db:
            return await settings.settings_entry(db)
    entry = json.loads(json.dumps(_run(export())))           # through JSON, as in the file
    roles = [r["name"] for r in entry["roles"]]
    assert "Deployers" in roles and not {"Viewer", "Administrator"} & set(roles)    # built-in roles stay out
    deployer = next(u for u in entry["users"] if u["username"] == "deployer")
    assert deployer["role"] == "Deployers" and all(u["username"] != "admin" for u in entry["users"])
    slack = next(c for c in entry["channels"] if c["name"] == "Team Slack")
    assert slack["config"]["webhook_url"].endswith("/secret")                   # in the clear: it is a settings export
    assert {"label": "work", "token": "ghp_the-token-value"} in entry["git_tokens"]

    async def wipe():
        async with AsyncSessionLocal() as db:
            await db.execute(delete(User).where(User.username == "deployer"))
            await db.execute(delete(Role).where(Role.name == "Deployers"))
            await db.execute(delete(NotificationChannel).where(NotificationChannel.name == "Team Slack"))
            await db.commit()
        token_vault.save([t for t in token_vault.load() if t["label"] != "work"])
    _run(wipe())

    async def bring_back():
        async with AsyncSessionLocal() as db:
            result = await settings.import_settings(db, entry)
            await db.commit()
            return result
    result = _run(bring_back())
    assert "Role 'Deployers'" in result["added"] and any("User 'deployer'" in a for a in result["added"])

    async def check():
        async with AsyncSessionLocal() as db:
            user = (await db.execute(select(User).where(User.username == "deployer"))).scalar_one()
            role = await db.get(Role, user.role_id)
            channel = (await db.execute(select(NotificationChannel).where(NotificationChannel.name == "Team Slack"))).scalar_one()
            return user, role.name, notify_config.load_config(channel)
    user, role_name, config = _run(check())
    assert role_name == "Deployers" and auth.verify_password("a-deployer-password", user.password_hash)
    assert config["webhook_url"].endswith("/secret")
    work = next(t for t in token_vault.load() if t["label"] == "work")
    assert token_vault.resolve(work["id"]) == "ghp_the-token-value"

    again = _run(bring_back())                                # a second import adds nothing
    assert not [a for a in again["added"] if a.startswith(("Role 'Deployers'", "User 'deployer'", "Notification channel 'Team Slack'", "Git token 'work'"))]
    assert "Role 'Deployers' (already exists)" in again["skipped"]
    _run(wipe())


def test_a_settings_file_cannot_set_a_password_that_is_not_a_hash(client):
    from sqlalchemy import select
    from core.database import AsyncSessionLocal
    from core.models import User
    from transfer import settings

    async def go():
        async with AsyncSessionLocal() as db:
            await settings.import_settings(db, {"users": [{"username": "plain", "password_hash": "letmein"}]})
            await db.commit()
            return (await db.execute(select(User).where(User.username == "plain"))).scalar_one_or_none()
    assert _run(go()) is None


def test_only_the_admin_account_exports_settings_or_makes_a_full_backup(client):
    from fastapi.testclient import TestClient
    import main
    viewer = TestClient(main.app, base_url="http://localhost")
    _make_viewer()
    assert viewer.post("/api/auth/login", json={"username": "full-viewer", "password": "a-viewer-password"}).status_code == 200
    assert viewer.post("/api/transfer/export", json={"app_ids": [], "include_settings": True, "password": "x"}).status_code == 403
    assert viewer.post("/api/transfer/full-backup", json={"password": "a-viewer-password"}).status_code == 403


def test_a_full_backup_needs_the_password(admin):
    assert admin.post("/api/transfer/full-backup", json={"password": "wrong"}).status_code == 403


def _make_viewer():
    from sqlalchemy import select
    from core import auth
    from core.database import AsyncSessionLocal
    from core.models import Role, User

    async def go():
        async with AsyncSessionLocal() as db:
            if (await db.execute(select(User).where(User.username == "full-viewer"))).scalar_one_or_none():
                return
            viewer = (await db.execute(select(Role).where(Role.name == "Viewer"))).scalar_one()
            db.add(User(username="full-viewer", password_hash=auth.hash_password("a-viewer-password"), role="Viewer", role_id=viewer.id))
            await db.commit()
    _run(go())


# ── Everything ────────────────────────────────────────────────────────────────

@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """A throwaway ~/.cloudbase with a database, keys and a certificate."""
    home = tmp_path / "data"
    home.mkdir()
    con = sqlite3.connect(home / "cloudbase.db")
    con.executescript("CREATE TABLE users (id INTEGER); CREATE TABLE applications (id INTEGER); INSERT INTO users VALUES (1);")
    con.commit()
    con.close()
    (home / "env_key").write_text("the-env-key")
    (home / "config.yaml").write_text("server: {}\n")
    (home / "certs").mkdir()
    (home / "certs" / "panel.pem").write_text("cert")
    monkeypatch.setattr(full, "DATA_DIR", str(home))
    monkeypatch.setattr(full, "PENDING", str(home / "restore-pending.tar.gz"))
    return home


def test_a_full_backup_is_checked_and_put_back_at_the_next_start(data_dir, tmp_path):
    out = str(tmp_path / "backup.tar.gz")
    full.pack(out, [])
    assert full.is_full_backup(out)
    info = full.check(out)
    assert info["users"] == 1 and info["apps"] == 0 and info["env_key"] and info["kind"] == "full"
    with tarfile.open(out) as tar:                       # the same layout as `cloudbase backup`
        assert {"cloudbase/cloudbase.db", "cloudbase/env_key", "cloudbase/certs/panel.pem"} <= set(tar.getnames())

    # This server moves on; then the backup is restored
    (data_dir / "env_key").write_text("a-newer-key")
    (data_dir / "cloudbase.db-wal").write_text("journal of the database being replaced")
    full.stage(out)
    assert full.apply_pending() is True
    assert (data_dir / "env_key").read_text() == "the-env-key"
    assert (data_dir / "certs" / "panel.pem").read_text() == "cert"
    assert not (data_dir / "cloudbase.db-wal").exists()            # never applied to the restored database
    kept = [p for p in data_dir.iterdir() if p.name.startswith("before-restore-")]
    assert kept and (kept[0] / "env_key").read_text() == "a-newer-key"
    assert not os.path.exists(full.PENDING) and full.apply_pending() is False


def _tar_gz(path, members):
    with tarfile.open(path, "w:gz") as tar:
        for name, data in members.items():
            if data is None:
                info = tarfile.TarInfo(name)
                info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
                tar.addfile(info)
                continue
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def test_a_backup_cannot_write_outside_cloudbase_or_bring_links(data_dir, tmp_path):
    db_bytes = (data_dir / "cloudbase.db").read_bytes()
    linked = str(tmp_path / "linked.tar.gz")
    _tar_gz(linked, {"cloudbase/cloudbase.db": db_bytes, "cloudbase/certs/evil": None})
    with pytest.raises(full.RestoreError, match="link"):
        full.check(linked)

    sneaky = str(tmp_path / "sneaky.tar.gz")
    _tar_gz(sneaky, {"cloudbase/cloudbase.db": db_bytes, "cloudbase/../../outside": b"x", "/etc/cron.d/x": b"x",
                     "cloudbase/agent_state.json": b"{}", "cloudbase/backups/1.dump": b"dump"})
    full.check(sneaky)
    full.stage(sneaky)
    full.apply_pending()
    assert not (tmp_path / "outside").exists() and not (data_dir / "agent_state.json").exists()
    assert (data_dir / "backups" / "1.dump").read_bytes() == b"dump"


def test_an_export_or_a_damaged_file_is_not_restored(admin, tmp_path):
    bad = str(tmp_path / "bad.tar.gz")
    _tar_gz(bad, {"cloudbase/cloudbase.db": b"not a database"})
    with pytest.raises(full.RestoreError):
        full.check(bad)
    export = io.BytesIO()
    with tarfile.open(fileobj=export, mode="w") as tar:
        info = tarfile.TarInfo("manifest.json")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"{}"))
    from conftest import PASSWORD
    r = admin.post("/api/transfer/restore", data={"password": PASSWORD},
                   files={"file": ("export.cloudbase", export.getvalue(), "application/octet-stream")})
    assert r.status_code == 400 and "export file" in r.text
    assert not os.path.exists(full.PENDING)                    # nothing staged, so nothing restarts


def test_the_preview_says_what_an_import_will_do_before_it_does_it(admin, client):
    from sqlalchemy import select
    from core.database import AsyncSessionLocal
    from core.models import Application

    async def existing():
        async with AsyncSessionLocal() as db:
            if not (await db.execute(select(Application).where(Application.name == "preview-taken"))).scalar_one_or_none():
                db.add(Application(name="preview-taken", repo_url="https://example.com/x.git", app_type="node", start_command="node .", port=3000))
                await db.commit()
    _run(existing())
    manifest = {"format": "cloudbase-export", "version": 1, "apps": [
        {"name": "preview-taken", "instances": 2}, {"name": "preview-new", "instances": 3, "run": True}],
        "databases": [{"name": "preview-db", "engine": "postgres", "version": "16", "data": {"file": "data/0.dump"}, "links": [{"app": "preview-new"}]}],
        "settings": {"roles": [{"name": "Viewer"}], "system": {"limits": {"max_apps": 50}}}}
    from transfer import manifest as manifest_module
    manifest["format"] = manifest_module.FORMAT
    plan = admin.post("/api/transfer/preview", json={"manifest": manifest}).json()
    assert {a["name"]: a["action"] for a in plan["apps"]} == {"preview-taken": "skip", "preview-new": "add"}
    assert plan["apps"][1]["start"] and plan["databases"][0]["data"] and plan["databases"][0]["links"] == ["preview-new"]
    items = {(i["what"], i["action"]) for i in plan["settings"]["items"]}
    assert ("Role", "skip") in items and ("System settings", "replace") in items

    async def still_absent():
        async with AsyncSessionLocal() as db:
            return (await db.execute(select(Application).where(Application.name == "preview-new"))).scalar_one_or_none()
    assert _run(still_absent()) is None                       # a preview changes nothing
    assert admin.post("/api/transfer/preview", json={"manifest": {"format": "nope"}}).status_code == 400
