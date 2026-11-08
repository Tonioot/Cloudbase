"""An app is known by its id (source folder, Docker image, nginx config); its name can change (rename).

Also the one-off move of an existing server to those names (containers/legacy_names.py, apps/legacy_names.py).
"""
import asyncio
import os

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
            app = Application(name=name, repo_url="https://example.test/x", start_command="x", port=3000, status="stopped", **fields)
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
class FakeImages:
    def __init__(self, names):
        self.names, self.removed = set(names), []

    def get(self, name):
        if name not in self.names:
            raise KeyError(name)
        images = self

        class Image:
            def tag(self, new):
                images.names.add(new)
        return Image()

    def remove(self, name):
        self.removed.append(name)
        self.names.discard(name)


class FakeClient:
    def __init__(self, names):
        self.images = FakeImages(names)


@pytest.fixture
def old_server(tmp_path, monkeypatch):
    from containers import legacy_names, processes as pm
    monkeypatch.setattr(pm, "APPS_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(legacy_names, "APPS_BASE_DIR", str(tmp_path))
    client = FakeClient({"cloudbase/my-app-7:latest"})
    monkeypatch.setattr(legacy_names, "_get_client", lambda: client)
    (tmp_path / "My_App").mkdir()
    (tmp_path / "My_App" / "app.py").write_text("print('hi')")
    return tmp_path, client


def test_the_source_folder_and_the_image_get_their_new_names(old_server):
    from containers import legacy_names
    tmp_path, client = old_server
    done = legacy_names.adopt(7, "My App")
    assert not (tmp_path / "My_App").exists() and (tmp_path / "app-7" / "app.py").exists()
    assert client.images.names == {"cloudbase/app-7:latest"} and client.images.removed == ["cloudbase/my-app-7:latest"]
    assert len(done) == 2
    assert legacy_names.adopt(7, "My App") == []                          # nothing left to do the second time


def test_a_server_that_was_moved_keeps_working_when_it_runs_again(old_server):
    from containers import legacy_names
    tmp_path, client = old_server
    legacy_names.adopt(7, "My App")
    (tmp_path / "app-7" / "new.txt").write_text("x")
    legacy_names.adopt(7, "My App")
    assert (tmp_path / "app-7" / "new.txt").exists()                      # never moved over or removed


def test_the_stored_paths_of_an_app_follow_the_move(old_server):
    from apps import legacy_names as migrate
    from containers import processes as pm
    from core.database import AsyncSessionLocal
    from core.models import Application
    tmp_path, client = old_server
    app_id = _app("Stored Paths", working_dir=str(tmp_path / "Stored_Paths"))
    (tmp_path / "Stored_Paths").mkdir()
    client.images.names = {f"cloudbase/stored-paths-{app_id}:latest"}

    async def set_image():
        async with AsyncSessionLocal() as db:
            (await db.get(Application, app_id)).docker_image = f"cloudbase/stored-paths-{app_id}:latest"
            await db.commit()
    _run(set_image())

    _run(migrate.run())
    row = _row(app_id)
    assert row.working_dir == pm.get_app_dir(app_id) and row.docker_image == f"cloudbase/app-{app_id}:latest"
    assert os.path.isdir(pm.get_app_dir(app_id)) and not (tmp_path / "Stored_Paths").exists()
