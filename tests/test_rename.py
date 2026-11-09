"""An app is known by its id (source folder, Docker image, nginx config); its name can change (rename).
"""
import asyncio

import pytest


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _app_started(admin):
    """The database exists once the app has started."""


def _app(name, **fields):
    from core.database import AsyncSessionLocal
    from core.models import Application

    async def go():
        async with AsyncSessionLocal() as db:
            app = Application(name=name, repo_url="https://example.test/x", start_command="x", port=3000, **fields)
            db.add(app)
            await db.commit()
            return app.id
    return _run(go())


def _row(app_id):
    from core.database import AsyncSessionLocal
    from core.models import Application

    async def go():
        async with AsyncSessionLocal() as db:
            return await db.get(Application, app_id)
    return _run(go())


# ── what an app is called on disk, in Docker and in nginx ─────────────────────────
def test_folder_image_and_nginx_names_come_from_the_id():
    from containers import naming, processes as pm
    from nginx import config, paths
    assert naming.image_name(7) == "cloudbase/app-7:latest"
    assert pm.get_app_dir(7).replace("\\", "/").endswith("/apps/app-7")
    assert paths.app_key(7) == "app-7"
    cfg = config.generate_config("Any Name", "shop.example.test", ["127.0.0.1:1", "127.0.0.1:2"], app_id=7)
    assert "upstream cloudbase_app_7 " in cfg
    assert "cloudbase_any_name" not in cfg


# ── renaming ─────────────────────────────────────────────────────────────────────
def test_an_app_can_be_renamed(admin):
    app_id = _app("rn-old")
    r = admin.put(f"/api/apps/{app_id}", json={"name": "rn-new"})
    assert r.status_code == 200 and r.json()["name"] == "rn-new"
    assert _row(app_id).name == "rn-new"


def test_a_name_that_is_taken_cannot_be_used(admin):
    first, second = _app("rn-first"), _app("rn-second")
    assert admin.put(f"/api/apps/{second}", json={"name": "rn-first"}).status_code == 400
    assert admin.put(f"/api/apps/{second}", json={"name": "RN First"}).status_code == 400          # the same subdomain
    assert admin.put(f"/api/apps/{first}", json={"name": "rn-first"}).status_code == 200            # its own name is fine
    assert _row(second).name == "rn-second"


def test_renaming_gives_the_app_a_new_subdomain_and_removes_the_old_certificate(admin, monkeypatch):
    from core import system_config
    from routers.applications import update
    monkeypatch.setattr(system_config, "get_base_domain_cached", lambda: "apps.example.test")
    removed, rewritten = [], []

    async def fake_write(app, db, node, **kw):
        rewritten.append(app.name)
    monkeypatch.setattr(update, "_write_app_nginx_config", fake_write)
    monkeypatch.setattr(update.nginx_certbot, "delete", lambda name: removed.append(name) or True)

    app_id = _app("sub-old")
    assert admin.put(f"/api/apps/{app_id}", json={"name": "Sub New"}).status_code == 200
    assert rewritten == ["Sub New"]                                      # nginx now answers on sub-new.apps.example.test
    assert removed == ["sub-old.apps.example.test"]


def test_a_rename_does_not_touch_the_folder_or_the_image(admin, monkeypatch):
    from containers import naming
    app_id = _app("keep-old", working_dir="/somewhere/app-folder", docker_image=naming.image_name(0))
    admin.put(f"/api/apps/{app_id}", json={"name": "keep-new"})
    row = _row(app_id)
    assert row.working_dir == "/somewhere/app-folder" and row.docker_image == naming.image_name(0)


# ── the one-off move of an existing server ───────────────────────────────────────
