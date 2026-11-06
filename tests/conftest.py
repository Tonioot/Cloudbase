"""Test setup.

Cloudbase modules read and write ~/.cloudbase (config, keys, database) as soon as
they are imported, so the home directory is pointed at a throwaway folder before
anything from backend/ is imported. Tests must never touch the real one.
"""
import atexit
import os
import shutil
import tempfile

_HOME = tempfile.mkdtemp(prefix="cloudbase-test-home-")
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)
for _var in ("HOME", "USERPROFILE"):
    os.environ[_var] = _HOME
os.environ["CLOUDBASE_BUILDKIT"] = "0"  # tests never shell out to docker unless they opt in

import pytest  # noqa: E402


@pytest.fixture
def buildkit(monkeypatch):
    """Pretend BuildKit is (un)available, without running docker."""
    from containers import buildkit as docker_manager

    def _set(value: bool):
        monkeypatch.setattr(docker_manager, "_buildkit_available", value)
    return _set


@pytest.fixture(autouse=True)
def _guard_home():
    """Fail loudly if a test ever resolves ~ to somewhere other than the sandbox."""
    assert os.path.expanduser("~") == _HOME
    yield


PASSWORD = "smoke-test-password"


@pytest.fixture(scope="session")
def client():
    """The whole app, started once with its lifespan, against the throwaway home directory."""
    from fastapi.testclient import TestClient
    from core import auth
    auth.save_hashed_password(auth.hash_password(PASSWORD))   # known credentials for the seeded admin
    import main
    with TestClient(main.app, base_url="http://localhost") as c:
        yield c


@pytest.fixture(scope="session")
def admin(client):
    """The same client, logged in as admin."""
    r = client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
    assert r.status_code == 200, r.text
    return client

