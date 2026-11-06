from containers import manager as dm


def _gen(app_type="nodejs", **kw):
    kw.setdefault("start_command", "npm start")
    kw.setdefault("port", 3000)
    return dm.generate_dockerfile(app_type, **kw)


def test_generated_dockerfile_starts_with_marker():
    for app_type in ("nodejs", "python", "ruby", "go", "php", "unknown", "static"):
        assert _gen(app_type).startswith(dm.GENERATED_MARKER), app_type


def test_start_command_becomes_exec_form():
    assert 'CMD ["npm", "start"]' in _gen()


def test_shell_syntax_start_command_runs_through_sh():
    assert dm._cmd_to_json("a && b") == '["/bin/sh", "-c", "a && b"]'


def test_no_port_means_no_expose():
    assert "EXPOSE" not in _gen(port=0)
    assert "EXPOSE 3000" in _gen()


def test_uvicorn_start_command_binds_all_interfaces():
    out = dm._normalize_docker_start_command("python", "uvicorn app:app --host 127.0.0.1", 8000)
    assert "--host 0.0.0.0" in out and "127.0.0.1" not in out
    assert "--host 0.0.0.0" in dm._normalize_docker_start_command("python", "uvicorn app:app", 8000)


def test_build_step_without_buildkit_passes_no_env(buildkit):
    buildkit(False)
    out = _gen(build_command="npm run build", build_env_keys=["API_URL", "SECRET"])
    assert "RUN npm run build" in out
    assert "ARG" not in out and "--mount" not in out


def test_build_step_with_buildkit_uses_a_secret_not_args(buildkit):
    buildkit(True)
    out = _gen(build_command="npm run build", build_env_keys=["API_URL", "SECRET"])
    assert "--mount=type=secret,id=buildenv" in out
    assert ". /run/secrets/buildenv && ( npm run build )" in out
    # env var names must not appear in the Dockerfile either
    assert "API_URL" not in out and "SECRET" not in out
    # only the cache-busting hash is an ARG
    assert [line for line in out.splitlines() if line.startswith("ARG")] == ["ARG CB_ENV_HASH"]


def test_build_step_with_buildkit_but_no_env_has_no_secret(buildkit):
    buildkit(True)
    out = _gen(build_command="npm run build", build_env_keys=[])
    assert "secret" not in out and "ARG" not in out


def test_invalid_env_names_are_ignored(buildkit):
    buildkit(True)
    out = _gen(build_command="npm run build", build_env_keys=["bad name; rm -rf /"])
    assert "secret" not in out and "rm -rf" not in out


def test_next_builds_get_a_cache_mount(buildkit):
    buildkit(True)
    assert "target=/app/.next/cache" in _gen(build_command="next build")
    assert "target=/app/.next/cache" not in _gen(build_command="vite build")


def test_cache_mounts_only_with_buildkit(buildkit, tmp_path):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "requirements.txt").write_text("flask\n")
    buildkit(False)
    assert "--mount" not in _gen("nodejs", app_dir=str(tmp_path))
    assert "pip install --no-cache-dir" in _gen("python", app_dir=str(tmp_path))
    buildkit(True)
    assert "target=/root/.npm" in _gen("nodejs", app_dir=str(tmp_path))
    py = _gen("python", app_dir=str(tmp_path))
    assert "target=/root/.cache/pip" in py and "--no-cache-dir" not in py


def test_npm_ci_used_only_with_a_lockfile(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    assert "npm install" in _gen("nodejs", app_dir=str(tmp_path)) and "npm ci" not in _gen("nodejs", app_dir=str(tmp_path))
    (tmp_path / "package-lock.json").write_text("{}")
    assert "npm ci" in _gen("nodejs", app_dir=str(tmp_path))


def test_static_site_without_build_is_just_nginx(tmp_path):
    (tmp_path / "index.html").write_text("hi")
    out = dm.generate_dockerfile("static", "", 0, app_dir=str(tmp_path))
    assert "FROM nginx:alpine" in out and "node:" not in out and "EXPOSE 80" in out


def test_static_site_with_build_is_multi_stage(tmp_path):
    out = dm.generate_dockerfile("static", "dist", 0, app_dir=str(tmp_path), build_command="npm run build")
    assert "AS builder" in out and "COPY --from=builder /app/dist/" in out


def test_static_publish_dir_cannot_escape_the_repo():
    import pytest
    with pytest.raises(ValueError):
        dm.resolve_static_dir("", "../etc")


def test_ensure_dockerfile_never_overwrites_a_custom_one(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")  # no marker, no .git -> the user's own
    dm.ensure_dockerfile(str(tmp_path), "nodejs", "npm start", 3000)
    assert (tmp_path / "Dockerfile").read_text() == "FROM scratch\n"


def test_ensure_dockerfile_regenerates_its_own(tmp_path):
    dm.ensure_dockerfile(str(tmp_path), "nodejs", "npm start", 3000)
    first = (tmp_path / "Dockerfile").read_text()
    dm.ensure_dockerfile(str(tmp_path), "nodejs", "node server.js", 3000)
    assert (tmp_path / "Dockerfile").read_text() != first
    assert (tmp_path / ".dockerignore").exists()
