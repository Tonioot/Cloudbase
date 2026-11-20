"""Projects: a user with memberships sees only their projects, each with the role given there; a project's shared
variables reach its apps; the apps of a connected project reach each other."""
import asyncio

import pytest
from fastapi.testclient import TestClient

PASSWORD = "a-member-password"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def world(admin):
    """Two projects with one app each, and a user who is a member of one of them only."""
    from sqlalchemy import select
    from core import auth
    from core.database import AsyncSessionLocal
    from core.models import Application, Role, User

    r = admin.post("/api/projects", json={"name": "Webshop", "description": "the shop"})
    assert r.status_code == 200, r.text
    shop = r.json()["id"]
    default = next(p["id"] for p in admin.get("/api/projects").json() if p["is_default"])

    async def setup():
        async with AsyncSessionLocal() as db:
            roles = {r.name: r for r in (await db.execute(select(Role))).scalars()}
            shop_app = Application(name="shop-api", repo_url="https://example.com/shop.git", project_id=shop)
            other_app = Application(name="intern-tool", repo_url="https://example.com/tool.git", project_id=default)
            member = User(username="shop-dev", password_hash=auth.hash_password(PASSWORD),
                          role="Viewer", role_id=roles["Viewer"].id)
            db.add_all([shop_app, other_app, member])
            await db.commit()
            return shop_app.id, other_app.id, member.id, roles["Administrator"].id
    shop_app, other_app, member_id, admin_role = run(setup())
    members = {"members": [{"user_id": member_id, "role_id": admin_role}]}
    # Adding someone who sees every project would change nothing for them: that has to be asked for
    assert admin.put(f"/api/projects/{shop}/members", json=members).status_code == 409
    r = admin.put(f"/api/projects/{shop}/members", json={**members, "limit_users": True})
    assert r.status_code == 200, r.text

    import main
    member = TestClient(main.app, base_url="http://localhost")
    assert member.post("/api/auth/login", json={"username": "shop-dev", "password": PASSWORD}).status_code == 200
    return {"shop": shop, "default": default, "shop_app": shop_app, "other_app": other_app, "member": member}


def test_a_member_sees_only_the_apps_of_their_projects(world, admin):
    member = world["member"]
    assert {a["name"] for a in member.get("/api/apps").json()} == {"shop-api"}
    assert {p["name"] for p in member.get("/api/projects").json()} == {"Webshop"}
    assert member.get(f"/api/apps/{world['shop_app']}").status_code == 200
    assert member.get(f"/api/apps/{world['other_app']}").status_code == 404        # not even that it exists
    assert member.get(f"/api/projects/{world['default']}").status_code == 404
    # everyone without memberships still sees everything
    assert {"shop-api", "intern-tool"} <= {a["name"] for a in admin.get("/api/apps").json()}


def test_being_a_member_counts_only_for_users_limited_to_their_projects(world, admin):
    from sqlalchemy import select
    from core.database import AsyncSessionLocal
    from core.models import User
    member = world["member"]
    user_id = next(u["id"] for u in admin.get("/api/users").json() if u["username"] == "shop-dev")
    assert next(u for u in admin.get("/api/users").json() if u["id"] == user_id)["project_access"] == "selected"
    # Back to every project: the membership stays but does nothing
    assert admin.put(f"/api/users/{user_id}", json={"project_access": "all"}).status_code == 200
    assert {"shop-api", "intern-tool"} <= {a["name"] for a in member.get("/api/apps").json()}
    assert admin.put(f"/api/users/{user_id}", json={"project_access": "selected"}).status_code == 200
    assert {a["name"] for a in member.get("/api/apps").json()} == {"shop-api"}
    assert admin.put(f"/api/users/{user_id}", json={"project_access": "some"}).status_code == 400

    async def access():
        async with AsyncSessionLocal() as db:
            return (await db.execute(select(User.project_access).where(User.id == user_id))).scalar_one()
    assert run(access()) == "selected"


def test_a_limited_user_without_projects_sees_none(world, admin):
    r = admin.post("/api/users", json={"username": "no-projects", "password": "a-member-password",
                                       "role_id": next(r["id"] for r in admin.get("/api/roles").json() if r["name"] == "Administrator"),
                                       "project_access": "selected"})
    assert r.status_code == 200, r.text
    import main
    c = TestClient(main.app, base_url="http://localhost")
    assert c.post("/api/auth/login", json={"username": "no-projects", "password": "a-member-password"}).status_code == 200
    assert c.get("/api/apps").status_code == 403                 # a limited user with no project has nothing
    assert c.get(f"/api/apps/{world['shop_app']}").status_code in (403, 404)


def test_a_member_has_the_role_of_their_project_there(world):
    member = world["member"]
    # Administrator in Webshop (their own role is only Viewer): may change the app there
    assert member.put(f"/api/notifications/apps/{world['shop_app']}/mute", json={"minutes": 0}).status_code == 200
    assert member.put(f"/api/notifications/apps/{world['other_app']}/mute", json={"minutes": 0}).status_code == 404
    assert member.get(f"/api/projects/{world['shop']}/env").status_code == 200     # projects.manage in Webshop
    # but may not create projects, which is not about one project
    assert member.post("/api/projects", json={"name": "Mine"}).status_code == 403
    # nor move their app into a project they cannot see
    r = member.post(f"/api/projects/move/app/{world['shop_app']}", json={"project_id": world["default"]})
    assert r.status_code == 404


def test_new_apps_of_a_member_go_into_their_project(world):
    from core import projects
    from core.database import AsyncSessionLocal

    async def check():
        user = {"projects": {world["shop"]: {"apps.deploy"}}, "permissions": set()}
        async with AsyncSessionLocal() as db:
            assert await projects.project_for_new(db, user, None, "apps.deploy") == world["shop"]
            with pytest.raises(Exception) as refused:
                await projects.project_for_new(db, user, world["default"], "apps.deploy")
            assert refused.value.status_code == 404
            everyone = {"projects": None, "permissions": None}
            assert await projects.project_for_new(db, everyone, None, "apps.deploy") == world["default"]
    run(check())


def test_shared_variables_reach_the_apps_and_their_own_win(world, admin):
    from core import projects
    from core.database import AsyncSessionLocal
    from core.env_crypto import encrypt_env
    from core.models import Application

    assert admin.put(f"/api/projects/{world['shop']}/env",
                     json={"env": {"SENTRY_DSN": "https://sentry", "REGION": "eu"}}).status_code == 200
    assert admin.put(f"/api/projects/{world['shop']}/env", json={"env": {"bad name": "x"}}).status_code == 400

    async def check():
        async with AsyncSessionLocal() as db:
            app = await db.get(Application, world["shop_app"])
            app.env_vars = encrypt_env({"REGION": "us"})
            await db.commit()
            assert projects.app_env(app) == {"SENTRY_DSN": "https://sentry", "REGION": "us"}
    run(check())


def test_the_apps_of_a_connected_project_reach_each_other(world):
    from sqlalchemy import select
    from apps import app_links
    from core.database import AsyncSessionLocal
    from core.models import Application

    async def check():
        async with AsyncSessionLocal() as db:
            db.add(Application(name="shop-web", repo_url="https://example.com/web.git", project_id=world["shop"]))
            db.add(Application(name="shop-worker", repo_url="https://example.com/w.git", project_id=world["shop"], no_web=True))
            await db.commit()
            api = await db.get(Application, world["shop_app"])
            options = await app_links.runtime_options(db, api)
            linked = {e["env_var"]: e for e in options.get("linked_apps") or []}
            assert "SHOP_WEB_URL" in linked and "SHOP_WORKER_URL" not in linked      # only apps with a web server
            assert linked["SHOP_WEB_URL"]["network"] == f"cloudbase-project-{world['shop']}"
            assert {"network": f"cloudbase-project-{world['shop']}", "alias": f"app-{api.id}"} in options["exposed_to"]
            # an app of the default project (not connected) gets nothing of the kind
            tool = (await db.execute(select(Application).where(Application.name == "intern-tool"))).scalar_one()
            assert "linked_apps" not in await app_links.runtime_options(db, tool)
    run(check())


def test_a_project_with_apps_cannot_be_deleted_and_the_default_never(world, admin):
    assert admin.delete(f"/api/projects/{world['shop']}").status_code == 409
    assert admin.delete(f"/api/projects/{world['default']}").status_code == 400
    empty = admin.post("/api/projects", json={"name": "Empty"}).json()["id"]
    assert admin.delete(f"/api/projects/{empty}").status_code == 200
    assert admin.post("/api/projects", json={"name": "webshop"}).status_code == 409    # names are unique, any case
