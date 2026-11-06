"""Start the whole app (lifespan, database, background tasks) and call real endpoints.

Everything runs against the throwaway home directory from conftest.py, never a real server.
"""
import pytest
from fastapi.testclient import TestClient

PASSWORD = "smoke-test-password"


@pytest.fixture(scope="module")
def client():
    from core import auth
    auth.save_hashed_password(auth.hash_password(PASSWORD))   # known credentials for the seeded admin
    import main
    with TestClient(main.app, base_url="http://localhost") as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
    assert r.status_code == 200, r.text
    return client


def test_health_is_public(client):
    assert client.get("/api/health").status_code == 200


def test_api_requires_a_login(client):
    fresh = TestClient(client.app, base_url="http://localhost")
    assert fresh.get("/api/apps").status_code in (401, 403)
    assert fresh.get("/api/users").status_code in (401, 403)


def test_wrong_password_is_rejected(client):
    from core import auth
    auth._login_attempts.clear()
    r = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401


def test_login_and_session(admin):
    r = admin.get("/api/auth/session")
    assert r.status_code == 200 and r.json()


@pytest.mark.parametrize("path", [
    "/api/apps", "/api/nodes", "/api/users", "/api/roles", "/api/databases", "/api/audit-log",
    "/api/notifications", "/api/system/settings", "/api/system/log-sources", "/api/overview/history",
    "/api/system/github-tokens",
])
def test_listing_endpoints_answer(admin, path):
    r = admin.get(path)
    assert r.status_code == 200, f"{path}: {r.status_code} {r.text[:200]}"


def test_local_node_exists_after_startup(admin):
    nodes = admin.get("/api/nodes").json()
    assert any(n.get("is_local") for n in nodes)


def test_panel_pages_are_served(client):
    r = client.get("/login")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert client.get("/css/base.css").status_code == 200
    # unknown API paths are a JSON 404, not the SPA shell
    r = client.get("/api/does-not-exist")
    assert r.status_code in (401, 404)


def test_security_headers_on_pages(client):
    r = client.get("/login")
    assert r.headers.get("x-frame-options") == "SAMEORIGIN"
    assert r.headers.get("x-content-type-options") == "nosniff"
