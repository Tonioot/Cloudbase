"""API tokens: another program calls the API with a key of its own (a list of permissions, maybe some projects), tied
to no user and no role."""
import asyncio

from fastapi.testclient import TestClient

READ_ONLY = ["apps.view", "databases.view", "nodes.view"]
EVERYTHING = ["apps.view", "apps.deploy", "apps.configure", "projects.manage", "databases.view", "databases.create"]


def _bare_client():
    import main
    return TestClient(main.app, base_url="http://localhost")


def _make(admin, permissions, **extra):
    r = admin.post("/api/tokens", json={"name": "ci", "permissions": permissions, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _bearer(made):
    return {"Authorization": f"Bearer {made['token']}"}


def test_a_token_works_without_a_session_and_its_secret_is_shown_once(admin):
    made = _make(admin, READ_ONLY)
    assert made["token"].startswith("cbt_") and len(made["token"]) >= 60
    c = _bare_client()
    assert c.get("/api/apps").status_code == 401
    assert c.get("/api/apps", headers=_bearer(made)).status_code == 200
    listed = next(t for t in admin.get("/api/tokens").json() if t["id"] == made["id"])
    assert listed["last_used_at"] and listed["permissions"] == sorted(READ_ONLY) and "token" not in listed


def test_a_token_has_only_the_permissions_it_was_given(admin):
    c = _bare_client()
    read_only = _bearer(_make(admin, READ_ONLY))
    assert c.post("/api/projects", json={"name": "nope"}, headers=read_only).status_code == 403
    everything = _bearer(_make(admin, EVERYTHING))
    assert c.post("/api/projects", json={"name": "made-by-token"}, headers=everything).status_code == 200
    assert c.get("/api/users", headers=everything).status_code == 403          # users, roles and tokens are the root account's
    assert c.get("/api/roles/permissions", headers=everything).status_code == 200
    assert c.post("/api/roles", json={"name": "sneaky", "permission_ids": []}, headers=everything).status_code == 403
    assert c.get("/api/tokens", headers=everything).status_code == 403
    assert c.post("/api/tokens", json={"name": "x", "permissions": ["apps.view"]}, headers=everything).status_code == 403


def test_changing_a_role_never_changes_a_token(admin):
    made = _make(admin, READ_ONLY)
    admin.post("/api/roles", json={"name": "token-test-role", "description": "", "permission_ids": []})
    assert _bare_client().post("/api/projects", json={"name": "still-no"}, headers=_bearer(made)).status_code == 403


def test_only_known_permissions_and_at_least_one(admin):
    assert admin.post("/api/tokens", json={"name": "x", "permissions": ["apps.view", "made.up"]}).status_code == 400
    assert admin.post("/api/tokens", json={"name": "x", "permissions": []}).status_code == 400
    assert admin.post("/api/tokens", json={"name": "x", "permissions": ["users.manage"]}).status_code == 400   # no longer exists


def test_a_token_for_some_projects_sees_only_those(admin):
    from core.database import AsyncSessionLocal
    from core.models import Application
    mine = admin.post("/api/projects", json={"name": "token-mine"}).json()["id"]
    other = admin.post("/api/projects", json={"name": "token-other"}).json()["id"]

    async def add():
        async with AsyncSessionLocal() as db:
            db.add_all([Application(name="tok-mine", repo_url="https://example.com/a.git", project_id=mine),
                        Application(name="tok-other", repo_url="https://example.com/b.git", project_id=other)])
            await db.commit()
    asyncio.run(add())
    made = _make(admin, EVERYTHING, project_ids=[mine])
    assert made["project_ids"] == [mine]
    names = {a["name"] for a in _bare_client().get("/api/apps", headers=_bearer(made)).json()}
    assert "tok-mine" in names and "tok-other" not in names
    assert admin.post("/api/tokens", json={"name": "x", "permissions": READ_ONLY, "project_ids": [99999]}).status_code == 400


def test_a_deleted_expired_or_unknown_token_stops_working(admin):
    from datetime import datetime, timedelta
    from sqlalchemy import update
    from core.database import AsyncSessionLocal
    from core.models import ApiToken
    c = _bare_client()
    made = _make(admin, READ_ONLY)
    assert c.get("/api/apps", headers=_bearer(made)).status_code == 200
    assert admin.delete(f"/api/tokens/{made['id']}").status_code == 200
    assert c.get("/api/apps", headers=_bearer(made)).status_code == 401
    assert c.get("/api/apps", headers={"Authorization": "Bearer cbt_unknown"}).status_code == 401

    old = _make(admin, READ_ONLY, expires_in_days=1)

    async def expire():
        async with AsyncSessionLocal() as db:
            await db.execute(update(ApiToken).where(ApiToken.id == old["id"]).values(expires_at=datetime.utcnow() - timedelta(hours=1)))
            await db.commit()
    asyncio.run(expire())
    assert c.get("/api/apps", headers=_bearer(old)).status_code == 401


def test_the_audit_log_names_the_token(admin):
    made = _make(admin, EVERYTHING)
    _bare_client().post("/api/projects", json={"name": "audited-by-token"}, headers=_bearer(made))
    assert "token:ci" in str(admin.get("/api/audit-log").json())


def test_a_token_lookup_that_fails_is_a_503_not_a_401(client, monkeypatch):
    """A busy (locked) database while checking a token used to answer 401 'Not authenticated': a client then believes its
    token was deleted. It is a 503, which a client tries again."""
    from core import auth

    async def broken(token):
        raise auth.AuthUnavailable("database is locked")
    monkeypatch.setattr(auth, "api_token_user", broken)
    res = client.get("/api/apps", headers={"Authorization": "Bearer cbt_whatever"})
    assert res.status_code == 503
