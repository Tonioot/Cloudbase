"""The source a node builds from: packed without reading it all into memory, and with a reason when it fails."""
import asyncio
import io
import os
import tarfile

import pytest

from routers.applications import repo


def _tree(root):
    (root / "app.py").write_text("print('hi')")
    (root / "Dockerfile").write_text("FROM scratch\n")
    (root / "pkg").mkdir()
    (root / "pkg" / "mod.py").write_text("x = 1")
    for skipped in (".git", "node_modules", "venv", "pkg/__pycache__"):
        (root / skipped).mkdir(parents=True, exist_ok=True)
        (root / skipped / "big.bin").write_bytes(b"0" * 1000)


def _names(path):
    with tarfile.open(path, "r:gz") as tar:
        return sorted(m.name for m in tar.getmembers())


def test_the_source_is_packed_without_what_a_node_never_needs(tmp_path):
    _tree(tmp_path)
    path, skipped = repo._pack_source(str(tmp_path))
    try:
        assert skipped == []
        assert _names(path) == [".", "Dockerfile", "app.py", "pkg", os.path.join("pkg", "mod.py").replace(os.sep, "/")] \
            or _names(path) == sorted([".", "Dockerfile", "app.py", "pkg", "pkg/mod.py"])
    finally:
        os.remove(path)


def test_what_comes_out_is_what_the_node_unpacks(tmp_path):
    _tree(tmp_path)
    path, _ = repo._pack_source(str(tmp_path))
    try:
        out = tmp_path / "out"
        out.mkdir()
        with tarfile.open(path, "r:gz") as tar:
            tar.extractall(out, filter="data") if hasattr(tarfile, "data_filter") else tar.extractall(out)
        assert (out / "app.py").read_text() == "print('hi')" and (out / "pkg" / "mod.py").exists()
        assert not (out / "node_modules").exists() and not (out / ".git").exists()
    finally:
        os.remove(path)


def test_a_file_that_cannot_be_read_is_left_out_not_fatal(tmp_path, monkeypatch):
    _tree(tmp_path)
    real_add = tarfile.TarFile.add

    def add(self, name, *args, **kwargs):
        if str(name).endswith("mod.py"):
            raise PermissionError(13, "Permission denied")
        return real_add(self, name, *args, **kwargs)

    monkeypatch.setattr(tarfile.TarFile, "add", add)
    path, skipped = repo._pack_source(str(tmp_path))
    try:
        assert len(skipped) == 1 and "mod.py" in skipped[0] and "PermissionError" in skipped[0]
        assert "app.py" in _names(path)
    finally:
        os.remove(path)


def test_a_failure_leaves_no_temporary_file_behind(tmp_path, monkeypatch):
    import tempfile
    _tree(tmp_path)
    before = set(os.listdir(tempfile.gettempdir()))
    monkeypatch.setattr(tarfile, "open", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        repo._pack_source(str(tmp_path))
    assert {n for n in set(os.listdir(tempfile.gettempdir())) - before if n.startswith("cb-source-")} == set()


# ── the endpoint, as a node calls it ───────────────────────────────────────────────
def _setup(tmp_path):
    from pathlib import Path
    from containers import processes as pm
    from core.database import AsyncSessionLocal
    from core.models import Application, ApplicationReplica, Node
    source = Path(pm.get_app_dir("src-app"))          # the source must lie in the apps directory: anything else is never served
    source.mkdir(parents=True, exist_ok=True)
    _tree(source)

    async def go():
        async with AsyncSessionLocal() as db:
            node = Node(name="src-node", role="node", status="online", auth_token="token-src", is_local=False, enabled=True)
            app = Application(name="src-app", repo_url="https://example.test/x", start_command="python app.py", port=8000,
                              status="running", working_dir=str(source), app_type="python")
            db.add_all([node, app])
            await db.flush()
            db.add(ApplicationReplica(app_id=app.id, node_id=node.id, status="running", external_port=9501))
            await db.commit()
            return app.id, node.id
    return asyncio.run(go())


@pytest.fixture
def served(client, tmp_path):
    app_id, node_id = _setup(tmp_path)
    yield app_id
    import shutil
    from containers import processes as pm
    shutil.rmtree(pm.get_app_dir("src-app"), ignore_errors=True)
    from sqlalchemy import delete
    from core.database import AsyncSessionLocal
    from core.models import Application, ApplicationReplica, Node

    async def cleanup():
        async with AsyncSessionLocal() as db:
            await db.execute(delete(ApplicationReplica).where(ApplicationReplica.app_id == app_id))
            await db.execute(delete(Application).where(Application.id == app_id))
            await db.execute(delete(Node).where(Node.id == node_id))
            await db.commit()
    asyncio.run(cleanup())


def test_a_node_gets_the_source_of_its_app(client, served):
    r = client.get(f"/api/apps/{served}/source-archive", headers={"X-Node-Token": "token-src"})
    assert r.status_code == 200 and r.headers["content-type"] == "application/gzip"
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tar:
        names = tar.getnames()
    assert "app.py" in names and not any("node_modules" in n or ".git" in n for n in names)


def test_without_the_token_of_a_node_that_runs_the_app_there_is_no_source(client, served):
    assert client.get(f"/api/apps/{served}/source-archive").status_code in (401, 403)
    assert client.get(f"/api/apps/{served}/source-archive", headers={"X-Node-Token": "wrong"}).status_code in (401, 403)


def test_a_failure_says_why(client, served, monkeypatch):
    def broken(source_dir):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(repo, "_pack_source", broken)
    r = client.get(f"/api/apps/{served}/source-archive", headers={"X-Node-Token": "token-src"})
    assert r.status_code == 500 and "Could not pack the source" in r.json()["detail"] and "No space left" in r.json()["detail"]


def test_the_node_reports_the_reason_not_just_a_status(monkeypatch):
    from agent import source

    class Response:
        status_code = 500
        text = "boom"

        def json(self):
            return {"detail": "Could not pack the source of 'Vexora': OSError: No space left on device"}

    class Client:
        async def get(self, *a, **k):
            return Response()

    class State:
        main_url = "https://primary"
        auth_token = "t"

    monkeypatch.setattr(source, "_local_app_dir", lambda name: "/nonexistent")
    with pytest.raises(RuntimeError, match="No space left on device"):
        asyncio.run(source._download_and_extract_source(Client(), State(), 1, "Vexora"))
