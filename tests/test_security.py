"""Encryption, passwords, tokens and the input checks in front of git/shell."""
import time
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import HTTPException

from core import auth
from core import env_crypto


# ── env var encryption ────────────────────────────────────────────────────────
def test_env_round_trip_and_not_plaintext():
    env = {"API_KEY": "abc", "UNICODE": "é€", "MULTI": "a\nb"}
    token = env_crypto.encrypt_env(env)
    assert "abc" not in token
    assert env_crypto.decrypt_env(token) == env


def test_env_empty_and_garbage():
    assert env_crypto.decrypt_env("") == {}
    assert env_crypto.decrypt_env("not json and not encrypted") == {}


def test_env_that_is_not_encrypted_is_not_read():
    assert env_crypto.decrypt_env('{"A": "1"}') == {}


def test_text_round_trip_and_unreadable_text_gives_nothing():
    assert env_crypto.decrypt_text(env_crypto.encrypt_text("ghp_token")) == "ghp_token"
    assert env_crypto.decrypt_text("not-encrypted") == ""
    assert env_crypto.encrypt_text(None) == ""


# ── passwords and tokens ──────────────────────────────────────────────────────
def test_password_hash_and_verify():
    hashed = auth.hash_password("correct horse")
    assert hashed != "correct horse"
    assert auth.verify_password("correct horse", hashed)
    assert not auth.verify_password("wrong", hashed)
    assert not auth.verify_password("x", "not-a-hash")


def test_token_round_trip():
    user = auth.decode_token(auth.create_access_token("alice", "editor", session_version=3))
    assert user == {"username": "alice", "role": "editor", "sv": 3}


def test_tampered_or_foreign_tokens_are_rejected():
    token = auth.create_access_token("alice", "editor")
    assert auth.decode_token(token + "x") is None
    assert auth.decode_token("") is None
    forged = jwt.encode({"sub": "admin", "role": "admin", "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
                        "some-other-secret-that-is-long-enough-123456", algorithm="HS256")
    assert auth.decode_token(forged) is None


def test_expired_token_is_rejected():
    expired = jwt.encode({"sub": "alice", "role": "admin", "exp": datetime.now(timezone.utc) - timedelta(seconds=5)},
                         auth.SECRET_KEY, algorithm="HS256")
    assert auth.decode_token(expired) is None
    assert auth.get_token_expires_in(expired) is None


def test_none_algorithm_token_is_rejected():
    unsigned = jwt.encode({"sub": "admin", "role": "admin"}, key=None, algorithm="none")
    assert auth.decode_token(unsigned) is None


def test_login_rate_limit_blocks_after_five_attempts():
    ip = "203.0.113.9"
    auth._login_attempts.pop(ip, None)
    for _ in range(auth.MAX_ATTEMPTS):
        auth._check_rate_limit(ip)
    with pytest.raises(HTTPException) as exc:
        auth._check_rate_limit(ip)
    assert exc.value.status_code == 429
    # other addresses are unaffected, and the window expires
    auth._check_rate_limit("203.0.113.10")
    auth._login_attempts[ip] = [time.time() - auth.WINDOW_SECONDS - 1] * auth.MAX_ATTEMPTS
    auth._check_rate_limit(ip)


# ── input checks in front of git ──────────────────────────────────────────────
@pytest.fixture(scope="module")
def apps():
    """The git/shell input checks, wherever they live (a hard import: broken modules must fail CI)."""
    from types import SimpleNamespace
    from apps import git, validation
    return SimpleNamespace(
        _check_git_ref=validation._check_git_ref,
        _check_repo_url=validation._check_repo_url,
        _build_clone_url=git._build_clone_url,
    )


@pytest.mark.parametrize("ref", ["main", "feature/x-1", "v1.2.3", "a1b2c3d", "HEAD~2", "origin/main", None, "  "])
def test_valid_git_refs(apps, ref):
    apps._check_git_ref(ref)


@pytest.mark.parametrize("ref", ["--upload-pack=touch /tmp/x", "-rf", "a..b", "main; rm -rf /", "a b", "$(id)", "x" * 201])
def test_dangerous_git_refs_are_rejected(apps, ref):
    with pytest.raises(HTTPException):
        apps._check_git_ref(ref)


@pytest.mark.parametrize("url", ["https://github.com/a/b.git", "git@github.com:a/b.git", "ssh://git@host/a/b"])
def test_valid_repo_urls(apps, url):
    assert apps._check_repo_url(url) == url


@pytest.mark.parametrize("url", ["", "file:///etc/passwd", "ext::sh -c id", "--upload-pack=x", "ftp://host/x"])
def test_dangerous_repo_urls_are_rejected(apps, url):
    with pytest.raises(HTTPException):
        apps._check_repo_url(url)


def test_clone_url_embeds_a_token_only_for_https(apps):
    assert "tok" in apps._build_clone_url("https://github.com/a/b.git", "tok")
    assert apps._build_clone_url("git@github.com:a/b.git", "tok") == "git@github.com:a/b.git"
    assert apps._build_clone_url("https://github.com/a/b.git", None) == "https://github.com/a/b.git"


def test_the_traceback_of_a_failed_request_reaches_the_server_log():
    """uvicorn writes it to a handler of its own that only the journal sees: `cloudbase logs` showed nothing."""
    import logging
    from core import system_logs
    system_logs.setup_logging()
    uvicorn_errors = logging.getLogger("uvicorn.error")
    assert uvicorn_errors.propagate and uvicorn_errors.handlers == []
    try:
        raise OSError("boom in a request")
    except OSError:
        uvicorn_errors.exception("Exception in ASGI application")
    for handler in logging.getLogger().handlers:
        handler.flush()
    text = open(system_logs.SERVER_LOG, encoding="utf-8").read()
    assert "[ERROR] uvicorn.error: Exception in ASGI application" in text and "boom in a request" in text
