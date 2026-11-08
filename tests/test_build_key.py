"""Deploys build only when the code or the settings changed; primary and nodes use the same check."""
import asyncio
import shutil
import subprocess

import pytest

from containers import build

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is needed")

ARGS = ("python", "python app.py", 8000, "pip install .", {"A": "1", "B": "2"}, "a" * 40)


def _git(directory, *args):
    subprocess.run(["git", "-C", str(directory), "-c", "user.email=t@t", "-c", "user.name=t", *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "app.py").write_text("print('hi')")
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "first")
    return tmp_path


# ── the key ───────────────────────────────────────────────────────────────────
def test_the_same_inputs_give_the_same_key_whatever_the_order_of_the_environment():
    assert build.build_key(*ARGS) == build.build_key("python", "python app.py", 8000, "pip install .", {"B": "2", "A": "1"}, "a" * 40)


@pytest.mark.parametrize("changed", [
    ("node", "python app.py", 8000, "pip install .", {"A": "1", "B": "2"}, "a" * 40),
    ("python", "python other.py", 8000, "pip install .", {"A": "1", "B": "2"}, "a" * 40),
    ("python", "python app.py", 9000, "pip install .", {"A": "1", "B": "2"}, "a" * 40),
    ("python", "python app.py", 8000, "pip install -e .", {"A": "1", "B": "2"}, "a" * 40),
    ("python", "python app.py", 8000, "pip install .", {"A": "1", "B": "3"}, "a" * 40),
    ("python", "python app.py", 8000, "pip install .", {"A": "1"}, "a" * 40),
    ("python", "python app.py", 8000, "pip install .", {"A": "1", "B": "2"}, "b" * 40),
])
def test_anything_the_build_reads_changes_the_key(changed):
    assert build.build_key(*changed) != build.build_key(*ARGS)


def test_without_a_revision_nothing_is_known_to_be_current():
    assert build.build_key("python", "x", 8000, "", {}, None) is None
    assert build.build_key("python", "x", 8000, "", {}, "") is None


# ── the revision of a working directory ──────────────────────────────────────────
def test_a_clean_checkout_has_a_revision(repo):
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert build.source_revision_of(str(repo)) == head


def test_files_cloudbase_writes_itself_do_not_count_as_changes(repo):
    (repo / ".dockerignore").write_text("node_modules\n")
    assert build.source_revision_of(str(repo))


def test_edited_or_added_files_mean_there_is_no_revision_to_trust(repo):
    (repo / "app.py").write_text("print('changed')")
    assert build.source_revision_of(str(repo)) is None
    _git(repo, "checkout", "app.py")
    assert build.source_revision_of(str(repo))
    (repo / "uploaded.txt").write_text("new")
    assert build.source_revision_of(str(repo)) is None


def test_a_directory_without_git_has_no_revision(tmp_path):
    assert build.source_revision_of(str(tmp_path)) is None


# ── the image gets its key ───────────────────────────────────────────────────────
def test_a_built_image_carries_the_revision_and_the_key(repo, monkeypatch):
    seen = {}
    monkeypatch.setattr(build, "_cleanup_before_build", lambda *a, **k: None)
    monkeypatch.setattr(build, "buildkit_available", lambda: False)
    monkeypatch.setattr(build, "_run_build", lambda app_id, app_dir, img, push, buildargs=None, labels=None, secret_env=None: seen.update(labels=labels))
    build.build_image(1, str(repo), lambda *a: None, "python", "python app.py", 8000)
    revision = build.source_revision_of(str(repo))
    assert seen["labels"][build.REVISION_LABEL] == revision
    assert seen["labels"][build.KEY_LABEL] == build.build_key("python", "python app.py", 8000, "", {}, revision)


# ── building only when needed ────────────────────────────────────────────────────
@pytest.fixture
def builder(repo, monkeypatch):
    calls, state, lines = [], {"key": None}, []
    monkeypatch.setattr(build, "build_image", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(build, "image_key", lambda app_id: state["key"])

    def run(**overrides):
        args = dict(app_type="python", start_command="python app.py", port=8000, build_command="", build_env={})
        args.update(overrides)
        return build.build_image_unless_current(1, str(repo), lambda _a, line: lines.append(line), **args)
    return run, calls, state, lines, repo


def test_an_image_that_is_current_is_not_built_again(builder):
    run, calls, state, lines, repo = builder
    state["key"] = build.build_key("python", "python app.py", 8000, "", {}, build.source_revision_of(str(repo)))
    img, built = run()
    assert built is False and calls == [] and any("not rebuilding" in line for line in lines)


def test_new_code_is_built(builder):
    run, calls, state, lines, repo = builder
    state["key"] = build.build_key("python", "python app.py", 8000, "", {}, "0" * 40)
    assert run()[1] is True and len(calls) == 1


def test_changed_settings_are_built_even_with_the_same_code(builder):
    run, calls, state, lines, repo = builder
    state["key"] = build.build_key("python", "python app.py", 8000, "", {}, build.source_revision_of(str(repo)))
    assert run(build_env={"NEXT_PUBLIC_URL": "https://new.example"})[1] is True and len(calls) == 1
    assert run(build_command="npm run build")[1] is True and len(calls) == 2


def test_an_image_without_a_key_is_built_once_to_get_one(builder):
    run, calls, state, lines, repo = builder
    state["key"] = None                               # built before keys existed
    assert run()[1] is True


def test_changed_files_are_always_built(builder):
    run, calls, state, lines, repo = builder
    state["key"] = build.build_key("python", "python app.py", 8000, "", {}, build.source_revision_of(str(repo)))
    (repo / "app.py").write_text("print('edited by hand')")
    assert run()[1] is True and len(calls) == 1


# ── the node uses the same check ─────────────────────────────────────────────────
def _node(monkeypatch, label):
    from agent import source
    from containers import prepare
    log = {"downloads": 0, "builds": 0}

    async def download(*a, **k):
        log["downloads"] += 1
        return "/tmp/app"

    async def build_local(*a, **k):
        log["builds"] += 1

    monkeypatch.setattr(prepare, "local_image_ok", lambda img: True)
    monkeypatch.setattr(source, "_download_and_extract_source", download)
    monkeypatch.setattr(prepare, "build_image_local", build_local)
    monkeypatch.setattr(prepare.containers_build, "image_key", lambda app_id: label)
    monkeypatch.setattr(prepare, "_built", set())
    return source, log


SPEC = {"app_id": 1, "app_name": "shop", "app_type": "python", "start_command": "python app.py", "internal_port": 8000,
        "build_command": "", "env_vars": {"A": "1"}, "source_revision": "c" * 40}


def _key(spec):
    from containers import prepare
    return build.build_key(*prepare.build_arguments(spec))


def test_the_node_skips_the_build_when_the_key_matches(monkeypatch):
    source, log = _node(monkeypatch, _key(SPEC))
    asyncio.run(source.ensure_image(None, None, SPEC, lambda step: None))
    assert log == {"downloads": 0, "builds": 0}


def test_the_node_rebuilds_when_only_a_setting_changed(monkeypatch):
    """The same revision but another build variable: the old check (revision only) would have kept the old image."""
    source, log = _node(monkeypatch, _key(SPEC))
    changed = {**SPEC, "env_vars": {"A": "2"}}
    asyncio.run(source.ensure_image(None, None, changed, lambda step: None))
    assert log == {"downloads": 1, "builds": 1}


def test_the_node_rebuilds_for_a_new_revision_but_not_twice_for_the_same_request(monkeypatch):
    source, log = _node(monkeypatch, _key({**SPEC, "source_revision": "d" * 40}))
    asyncio.run(source.ensure_image(None, None, SPEC, lambda step: None))
    asyncio.run(source.ensure_image(None, None, SPEC, lambda step: None))
    assert log == {"downloads": 1, "builds": 1}              # the second time it was built for this already


def test_the_node_builds_an_image_from_before_keys_existed(monkeypatch):
    source, log = _node(monkeypatch, None)
    asyncio.run(source.ensure_image(None, None, SPEC, lambda step: None))
    assert log["builds"] == 1


def test_the_node_uses_what_it_has_when_no_revision_is_known(monkeypatch):
    source, log = _node(monkeypatch, None)
    asyncio.run(source.ensure_image(None, None, {**SPEC, "source_revision": None}, lambda step: None))
    assert log == {"downloads": 0, "builds": 0}
