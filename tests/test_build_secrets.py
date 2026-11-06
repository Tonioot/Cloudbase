"""The build env must never leak: not on the command line, not in the Dockerfile."""
import os
import shutil
import stat
import subprocess

import pytest

from containers import build as containers_build

SECRET = "s3cr3t-value-123"


class _FakeProc:
    def __init__(self, lines=("#1 building",), code=0):
        self.stdout = iter(l + "\n" for l in lines)
        self._code = code
        self.returncode = code

    def wait(self):
        return self._code


@pytest.fixture
def popen(monkeypatch):
    calls = []

    def fake(cmd, **kw):
        calls.append({"cmd": cmd, "env": kw.get("env", {}), "secret_file": _read_secret(cmd)})
        return _FakeProc(code=fake.code)
    fake.code = 0
    monkeypatch.setattr(containers_build.subprocess, "Popen", fake)
    fake.calls = calls
    return fake


def _read_secret(cmd):
    for i, part in enumerate(cmd):
        if part == "--secret":
            src = dict(kv.split("=", 1) for kv in cmd[i + 1].split(","))["src"]
            with open(src, encoding="utf-8") as f:
                return f.read()
    return None


def test_secret_never_on_the_command_line(popen):
    containers_build._build_with_cli(1, "/app", "img", lambda *a: None,
                       buildargs={"CB_ENV_HASH": "abc"}, secret_env={"API_KEY": SECRET})
    call = popen.calls[0]
    assert SECRET not in " ".join(call["cmd"])
    assert "export API_KEY=" in call["secret_file"] and SECRET in call["secret_file"]
    assert "--build-arg" in call["cmd"] and "CB_ENV_HASH=abc" in call["cmd"]


def test_secret_file_is_removed_after_the_build(popen):
    containers_build._build_with_cli(1, "/app", "img", lambda *a: None, secret_env={"K": "v"})
    cmd = popen.calls[0]["cmd"]
    src = dict(kv.split("=", 1) for kv in cmd[cmd.index("--secret") + 1].split(","))["src"]
    assert not os.path.exists(src)


def test_secret_file_is_removed_when_the_build_fails(popen):
    popen.code = 1
    with pytest.raises(RuntimeError, match="docker build failed"):
        containers_build._build_with_cli(1, "/app", "img", lambda *a: None, secret_env={"K": "v"})
    cmd = popen.calls[0]["cmd"]
    src = dict(kv.split("=", 1) for kv in cmd[cmd.index("--secret") + 1].split(","))["src"]
    assert not os.path.exists(src)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_secret_file_is_owner_only():
    path = containers_build._write_secret_file({"K": "v"})
    try:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    finally:
        os.remove(path)


def test_custom_dockerfile_args_come_from_the_environment_not_argv(popen):
    containers_build._build_with_cli(1, "/app", "img", lambda *a: None, buildargs={"NPM_TOKEN": SECRET})
    call = popen.calls[0]
    assert SECRET not in " ".join(call["cmd"])
    assert "NPM_TOKEN" in call["cmd"] and call["env"]["NPM_TOKEN"] == SECRET


def test_reserved_names_are_not_allowed_to_override_the_process_env(popen):
    before = os.environ.get("PATH")
    containers_build._build_with_cli(1, "/app", "img", lambda *a: None,
                       buildargs={"PATH": "/evil", "DOCKER_HOST": "tcp://evil", "OK": "1"})
    call = popen.calls[0]
    assert call["env"]["PATH"] == before
    assert call["env"].get("DOCKER_HOST") != "tcp://evil"
    assert "OK" in call["cmd"] and "PATH" not in call["cmd"]


def test_failure_message_contains_the_last_log_lines(monkeypatch):
    monkeypatch.setattr(containers_build.subprocess, "Popen", lambda cmd, **kw: _FakeProc(["a", "b", "boom"], code=1))
    with pytest.raises(RuntimeError, match="boom"):
        containers_build._build_with_cli(1, "/app", "img", lambda *a: None)


def test_env_hash_is_stable_order_independent_and_value_sensitive():
    a = containers_build._env_hash({"A": "1", "B": "2"})
    assert a == containers_build._env_hash({"B": "2", "A": "1"})
    assert a != containers_build._env_hash({"A": "1", "B": "3"})
    assert SECRET not in containers_build._env_hash({"A": SECRET})


@pytest.mark.skipif(not shutil.which("sh") or os.name == "nt", reason="needs a POSIX sh")
def test_secret_file_round_trips_awkward_values():
    values = {"A": "it's a $HOME `x`", "B": "line1\nline2", "C": 'quote"d', "D": ""}
    path = containers_build._write_secret_file(values)
    try:
        for key, expected in values.items():
            out = subprocess.run(["sh", "-c", f'. "{path}" && printf %s "${key}"'],
                                 capture_output=True, text=True).stdout
            assert out == expected, key
    finally:
        os.remove(path)
