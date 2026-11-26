"""Static sites served by nginx from disk (apps/static_sites.py): only plain files of the site itself, capped, and an
nginx config that serves them without a backend."""
import asyncio
import io
import os
import tarfile
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from apps import static_sites
from containers import dockerfiles
from nginx import config as nginx_config, paths as nginx_paths


def _tar(path, entries):
    """entries: (name, kind, data) with kind file | dir | symlink | hardlink | fifo."""
    with tarfile.open(path, "w:gz") as tar:
        for name, kind, data in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
                continue
            info.type = {"dir": tarfile.DIRTYPE, "symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE,
                         "fifo": tarfile.FIFOTYPE}[kind]
            info.linkname = data.decode() if data else ""
            tar.addfile(info)


def test_an_archive_gives_only_the_sites_plain_files(tmp_path):
    archive = tmp_path / "site.tar.gz"
    _tar(archive, [("html", "dir", b""), ("html/index.html", "file", b"<h1>hi</h1>"), ("html/assets", "dir", b""),
                   ("html/assets/app.js", "file", b"x()"), ("html/passwd", "symlink", b"/etc/passwd"),
                   ("html/hard", "hardlink", b"html/index.html"), ("html/pipe", "fifo", b"")])
    dest = tmp_path / "out"
    count = static_sites._unpack(str(archive), str(dest))
    assert sorted(os.listdir(dest)) == ["assets", "index.html"]          # html/ is the top, links and fifos left out
    assert (dest / "assets" / "app.js").read_bytes() == b"x()"
    assert (count.files, count.bytes) == (2, len(b"<h1>hi</h1>") + 3)


@pytest.mark.parametrize("name", ["html/../evil.html", "/etc/cron.d/x", "html/a/../../b"])
def test_an_archive_with_a_path_out_of_its_folder_is_refused(tmp_path, name):
    archive = tmp_path / "bad.tar.gz"
    _tar(archive, [("html/index.html", "file", b"ok"), (name, "file", b"bad")])
    with pytest.raises(HTTPException) as e:
        static_sites._unpack(str(archive), str(tmp_path / "out"))
    assert e.value.status_code == 422
    assert not (tmp_path / "evil.html").exists() and not (tmp_path / "b").exists()


def test_a_site_too_big_or_with_too_many_files_is_refused(tmp_path, monkeypatch):
    archive = tmp_path / "big.tar.gz"
    _tar(archive, [(f"html/{i}.html", "file", b"x" * 10) for i in range(5)])
    monkeypatch.setattr(static_sites, "max_files", lambda: 3)
    with pytest.raises(HTTPException, match="more than 3 files"):
        static_sites._unpack(str(archive), str(tmp_path / "a"))
    monkeypatch.setattr(static_sites, "max_files", lambda: 100)
    monkeypatch.setattr(static_sites, "max_bytes", lambda: 25)
    with pytest.raises(HTTPException, match="larger than"):
        static_sites._unpack(str(archive), str(tmp_path / "b"))


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_copying_from_the_repository_leaves_out_links_git_and_cloudbases_dockerfile(tmp_path):
    src = tmp_path / "repo"
    (src / ".git").mkdir(parents=True)
    (src / ".git" / "config").write_text("secret")
    (src / "css").mkdir()
    (src / "index.html").write_text("hi")
    (src / "css" / "a.css").write_text("a{}")
    (src / "Dockerfile").write_text(dockerfiles.GENERATED_MARKER + "FROM nginx\n")
    os.symlink("/etc/passwd", src / "passwd")
    os.symlink(str(tmp_path), src / "up")
    dest = tmp_path / "out"
    static_sites._copy_tree(str(src), str(dest))
    assert sorted(os.listdir(dest)) == ["css", "index.html"]
    assert oct(os.stat(dest / "index.html").st_mode & 0o777) == "0o644"


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_publish_directory_that_links_out_of_the_repository_is_refused(tmp_path, monkeypatch):
    src = tmp_path / "repo"
    src.mkdir()
    os.symlink("/etc", src / "dist")
    app = SimpleNamespace(start_command="dist", working_dir=str(src))
    monkeypatch.setattr(static_sites.pm, "app_source_dir", lambda a: str(src))
    with pytest.raises(HTTPException, match="inside the repository"):
        static_sites._copy_from_source(app, str(tmp_path / "out"))


def test_only_the_asked_node_may_hand_in_the_site_once(tmp_path):
    path = str(tmp_path / "site.tar.gz")
    static_sites._builds["b1"] = static_sites._Build(app_id=7, node_id=3, path=path)

    async def chunks(*parts):
        for p in parts:
            yield p

    try:
        with pytest.raises(HTTPException) as e:
            asyncio.run(static_sites.receive_upload("b1", 7, 4, chunks(b"x")))      # another node
        assert e.value.status_code == 403
        with pytest.raises(HTTPException):
            asyncio.run(static_sites.receive_upload("b1", 8, 3, chunks(b"x")))      # another app
        assert static_sites.node_may_fetch(7, 3) and not static_sites.node_may_fetch(7, 4)
        assert asyncio.run(static_sites.receive_upload("b1", 7, 3, chunks(b"ab", b"cd"))) == 4
        assert open(path, "rb").read() == b"abcd"
        with pytest.raises(HTTPException):
            asyncio.run(static_sites.receive_upload("b1", 7, 3, chunks(b"again")))  # only once
    finally:
        static_sites._builds.pop("b1", None)
    assert not static_sites.node_may_fetch(7, 3)


def test_an_upload_past_the_limit_is_not_kept(tmp_path, monkeypatch):
    path = str(tmp_path / "site.tar.gz")
    static_sites._builds["b2"] = static_sites._Build(app_id=1, node_id=1, path=path)
    monkeypatch.setattr(static_sites, "max_bytes", lambda: 3)

    async def chunks():
        yield b"ab"
        yield b"cd"

    try:
        with pytest.raises(HTTPException) as e:
            asyncio.run(static_sites.receive_upload("b2", 1, 1, chunks()))
        assert e.value.status_code == 413
        assert not os.path.exists(path) and not os.path.exists(path + ".part")
    finally:
        static_sites._builds.pop("b2", None)


def test_nginx_serves_the_folder_itself_and_no_links(monkeypatch):
    monkeypatch.setattr(nginx_config._scfg, "get_base_domain_cached", lambda: "")
    root = nginx_paths.release_dir(5, "abc123-1700000000-ab12")
    cfg = nginx_config.generate_config("docs", "docs.example.com", [], app_id=5, site_root=root)
    assert f"root {root};" in cfg and "disable_symlinks on;" in cfg and "proxy_pass" not in cfg
    assert "try_files $uri $uri/ $uri.html /index.html =404;" in cfg
    assert "acme-challenge" in cfg                                           # certificates keep working
    stopped = nginx_config.generate_config("docs", "docs.example.com", [], app_id=5, site_root=None)
    assert "root " + root not in stopped and "downtime.html" in stopped      # stopped: the downtime page
    with pytest.raises(ValueError):
        nginx_config.generate_config("docs", "docs.example.com", [], app_id=5, site_root="/x; include /etc/passwd")


def test_a_release_name_is_one_of_ours_only():
    assert nginx_paths.release_dir(1, "abc-123") == f"{nginx_paths.SITES_DIR}/app-1/abc-123"
    for bad in ("../x", "a/b", "", "A B", "x" * 80):
        assert nginx_paths.release_dir(1, bad) is None
    name = static_sites._release_name("4F2a9c0D8e7b6a5f4e3d2c1b")
    assert nginx_paths.RELEASE_RE.fullmatch(name) and name.startswith("4f2a9c0d8e7b")


# ── publishing, through the database ─────────────────────────────────────────
def test_publishing_puts_the_files_live_removes_old_containers_and_keeps_a_few_versions(client, tmp_path, monkeypatch):
    from core.database import AsyncSessionLocal
    from core.models import Application, Instance
    from nodes.registry import ensure_local_node
    from apps import nginx_sync, serializers

    monkeypatch.setattr(nginx_paths, "SITES_DIR", str(tmp_path / "sites"))
    os.makedirs(tmp_path / "sites")
    written = []

    async def write(app, db):
        written.append((app.static_release, bool(app.static_stopped)))

    monkeypatch.setattr(nginx_sync, "_write_app_nginx_config", write)
    monkeypatch.setattr(nginx_sync, "_has_public_nginx_domain", lambda app: True)
    removed = []

    async def remove(app_id, ids):
        removed.extend(ids)

    monkeypatch.setattr(static_sites, "remove_instances", remove)
    lines = []

    async def go():
        async with AsyncSessionLocal() as db:
            node = await ensure_local_node(db)
            app = Application(name="disk-site", repo_url="https://example.test/site", app_type="static", port=80)
            db.add(app)
            await db.flush()
            source = static_sites.pm.get_app_dir(app.id)
            os.makedirs(source, exist_ok=True)
            with open(os.path.join(source, "index.html"), "w") as f:
                f.write("<h1>v1</h1>")
            app.working_dir = source
            old = Instance(app_id=app.id, node_id=node.id, status="running", external_port=9871)
            db.add(old)
            await db.commit()

            first = await static_sites.publish(app, db, lines.append, "aaaa1111")
            root = nginx_paths.release_dir(app.id, first["release"])
            assert open(os.path.join(root, "index.html")).read() == "<h1>v1</h1>"
            assert app.static_release == first["release"] and removed == [old.id]
            assert serializers.app_status(app, []) == "running" and static_sites.serves(app)

            releases = [first["release"]]
            for n in range(2, 6):
                with open(os.path.join(source, "index.html"), "w") as f:
                    f.write(f"<h1>v{n}</h1>")
                releases.append((await static_sites.publish(app, db, lines.append, f"bbbb{n}"))["release"])
            kept = sorted(os.listdir(nginx_paths.site_dir(app.id)))
            assert len(kept) == static_sites.KEEP and releases[-1] in kept and releases[0] not in kept

            await static_sites.set_stopped(app, db, True)
            assert serializers.app_status(app, []) == "stopped" and written[-1] == (releases[-1], True)
            await static_sites.set_stopped(app, db, False)
            assert serializers.app_status(app, []) == "running"

            static_sites.remove_site(app.id)
            assert not os.path.exists(nginx_paths.site_dir(app.id))
            await db.delete(app)
            await db.commit()

    asyncio.run(go())
    assert any("served by nginx" in line for line in lines)


def test_a_failed_nginx_write_keeps_the_version_that_was_live(client, tmp_path, monkeypatch):
    from core.database import AsyncSessionLocal
    from core.models import Application
    from apps import nginx_sync

    monkeypatch.setattr(nginx_paths, "SITES_DIR", str(tmp_path / "sites"))
    os.makedirs(tmp_path / "sites")
    monkeypatch.setattr(nginx_sync, "_has_public_nginx_domain", lambda app: True)

    async def refuse(app, db):
        raise HTTPException(500, "Nginx config failed")

    monkeypatch.setattr(nginx_sync, "_write_app_nginx_config", refuse)

    async def go():
        async with AsyncSessionLocal() as db:
            app = Application(name="disk-site-2", repo_url="https://example.test/site", app_type="static", port=80,
                              static_release="live-1")
            db.add(app)
            await db.flush()
            source = static_sites.pm.get_app_dir(app.id)
            os.makedirs(source, exist_ok=True)
            open(os.path.join(source, "index.html"), "w").write("x")
            app.working_dir = source
            await db.commit()
            with pytest.raises(HTTPException):
                await static_sites.publish(app, db, lambda line: None, "cccc")
            assert app.static_release == "live-1"
            assert os.listdir(nginx_paths.site_dir(app.id)) == []           # the new version is gone again
            await db.delete(app)
            await db.commit()

    asyncio.run(go())
