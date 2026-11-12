"""Who may call what is decided in one place: the endpoint's own dependency (auth.require_permission /
require_superadmin / require_auth, or a node-token check). The middleware only checks that somebody is signed in,
and the user (with their permissions) is looked up once per request."""
import asyncio

import pytest
from fastapi.routing import APIRoute

# Endpoints that need no signed-in user, or check something else themselves: on purpose, and nothing else
OPEN = {
    "/api/health",                     # liveness, for load balancers and `cloudbase doctor`
    "/api/status",                     # the public status page: only apps that opted in
    "/api/webhooks/{app_id}",       # a Git host: the signature of the delivery is checked by the endpoint
    "/api/auth/check", "/api/auth/login", "/api/auth/session", "/api/auth/logout", "/api/auth/change-password",
}
NODE_TOKEN_PREFIX = "/api/nodes/agent/"   # a node's agent, with its X-Node-Token (checked by the endpoint)
CHECKS = ("require_permission", "require_auth", "require_superadmin", "_require_node_agent", "_source_archive_auth")


def _flatten(routes):
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _flatten(inner.routes)
        else:
            yield route


def _checks(dependant):
    for dep in dependant.dependencies:
        yield getattr(dep.call, "__qualname__", "")
        yield from _checks(dep)


def test_every_api_endpoint_decides_who_may_call_it(client):
    import main
    missing = []
    for route in _flatten(main.app.routes):
        if not isinstance(route, APIRoute) or not route.path.startswith("/api") or route.path in OPEN:
            continue
        if route.path.startswith(NODE_TOKEN_PREFIX):
            continue
        if not any(name.startswith(CHECKS) for name in _checks(route.dependant)):
            missing.append(f"{sorted(route.methods)} {route.path}")
    assert not missing, "endpoints without a permission check (add Depends(auth.require_permission(...))):\n" + "\n".join(missing)


def test_a_request_looks_the_user_up_once(admin, monkeypatch):
    from core import auth
    real = auth.session_user
    lookups = []

    async def counted(token):
        lookups.append(token)
        return await real(token)
    monkeypatch.setattr(auth, "session_user", counted)
    assert admin.get("/api/apps").status_code == 200          # middleware + require_permission + "can manage apps"
    assert len(lookups) == 1


def test_signed_out_is_401_and_without_the_permission_403(client):
    from core import auth
    from core.database import AsyncSessionLocal
    from core.models import User
    from fastapi.testclient import TestClient
    import main

    async def make_viewer():
        async with AsyncSessionLocal() as db:
            from sqlalchemy import select
            from core.models import Role
            viewer = (await db.execute(select(Role).where(Role.name == "Viewer"))).scalar_one()
            if (await db.execute(select(User).where(User.username == "route-viewer"))).scalar_one_or_none() is None:
                db.add(User(username="route-viewer", password_hash=auth.hash_password("a-viewer-password"),
                            role="Viewer", role_id=viewer.id))
                await db.commit()
    asyncio.run(make_viewer())
    fresh = TestClient(main.app, base_url="http://localhost")
    assert fresh.get("/api/apps").status_code == 401
    assert fresh.post("/api/auth/login", json={"username": "route-viewer", "password": "a-viewer-password"}).status_code == 200
    assert fresh.get("/api/apps").status_code == 200                       # apps.view
    r = fresh.post("/api/databases", json={"name": "nope", "engine": "postgres", "version": "16"})
    assert r.status_code == 403 and "databases.create" in r.text           # decided by the endpoint
    assert fresh.get("/api/users").status_code == 403                      # only the root account
    assert fresh.get("/api/apps/system/certs").status_code == 200          # apps.view (it had no check of its own)


@pytest.mark.parametrize("path", ["/api/apps/system/certs"])
def test_no_endpoint_relied_on_the_old_path_map(path):
    """The map in the middleware is gone; this endpoint was the one that leaned on it."""
    import main
    route = next(r for r in _flatten(main.app.routes) if isinstance(r, APIRoute) and r.path == path)
    assert any(name.startswith("require_permission") for name in _checks(route.dependant))
