"""Set up in one go (install.sh --panel-domain … --api-token …): the panel does what the bootstrap file asks, once, and goes
on where it was after a restart; the token it made can replace itself."""
import asyncio
import json
import secrets

import pytest


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def boot(client, tmp_path, monkeypatch):
    from workers import bootstrap
    path = tmp_path / "bootstrap.json"
    monkeypatch.setattr(bootstrap, "PATH", str(path))
    monkeypatch.setattr(bootstrap, "RETRY_SECONDS", 0)
    calls = {"base": [], "db": [], "panel": [], "issue": 0}

    async def set_base(db, *, base_domain, base_ssl_cert, base_ssl_key):
        calls["base"].append(base_domain)

    async def set_db(db, domain, ssl_cert="", ssl_key=""):
        calls["db"].append(domain)

    async def nothing(*a, **k):
        return None
    monkeypatch.setattr(bootstrap._syscfg, "set_base_settings", set_base)
    monkeypatch.setattr(bootstrap._syscfg, "set_db_domain", set_db)
    monkeypatch.setattr(bootstrap, "sync_public_access", nothing)
    monkeypatch.setattr(bootstrap, "_refresh_all_app_nginx", nothing)
    return bootstrap, path, calls


def _token():
    return "cbt_" + secrets.token_urlsafe(48)


def test_the_bootstrap_makes_the_token_sets_the_domains_and_the_panel(boot, client, monkeypatch):
    bootstrap, path, calls = boot
    token = _token()
    path.write_text(json.dumps({"api_token": token, "base_domain": "utr-009.example.test", "db_domain": "utr-009.db.example.test",
                                "panel_domain": "utr-009.panel.example.test"}))

    async def issue(domains, cert_name=None, email=None):
        calls["issue"] += 1
        if calls["issue"] == 1:
            raise bootstrap.nginx_certificates.CertificateError("Let's Encrypt couldn't find a DNS record")   # DNS not there yet
        return "/etc/letsencrypt/live/x/fullchain.pem", "/etc/letsencrypt/live/x/privkey.pem"

    async def panel(domain, cert, key):
        calls["panel"].append((domain, cert))
    monkeypatch.setattr(bootstrap.nginx_certificates, "issue", issue)
    monkeypatch.setattr(bootstrap, "apply_panel_domain", panel)

    _run(bootstrap.apply_pending())
    assert calls["base"] == ["utr-009.example.test"] and calls["db"] == ["utr-009.db.example.test"]
    assert calls["issue"] == 2 and calls["panel"] == [("utr-009.panel.example.test", "/etc/letsencrypt/live/x/fullchain.pem")]
    assert not path.exists()                                                    # all done: the file is gone
    # the token works, with what a platform needs and nothing more
    r = client.get("/api/system/capacity", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert client.get("/api/tokens", headers={"Authorization": f"Bearer {token}"}).status_code in (401, 403)


def test_a_restart_goes_on_where_it_was_and_the_token_leaves_the_file(boot, monkeypatch):
    bootstrap, path, calls = boot
    token = _token()
    path.write_text(json.dumps({"api_token": token, "base_domain": "a.example.test", "panel_domain": "p.example.test"}))

    async def never(domains, cert_name=None, email=None):
        raise bootstrap.nginx_certificates.CertificateError("no DNS yet")
    monkeypatch.setattr(bootstrap.nginx_certificates, "issue", never)
    monkeypatch.setattr(bootstrap, "GIVE_UP_SECONDS", -1)                    # give up at once, as after a day
    _run(bootstrap.apply_pending())
    left = json.loads(path.read_text())
    assert "api_token" not in left and set(left["done"]) == {"api_token", "domains"}
    # started again: only the panel is left, nothing is done twice
    _run(bootstrap.apply_pending())
    assert calls["base"] == ["a.example.test"]


def test_a_bad_token_in_the_file_is_refused(boot):
    bootstrap, path, _ = boot
    path.write_text(json.dumps({"api_token": "not-a-token"}))
    _run(bootstrap.apply_pending())
    assert "api_token" in json.loads(path.read_text())                         # nothing made; it stays to be looked at


def test_a_token_can_replace_itself(boot, client):
    bootstrap, path, _ = boot
    token = _token()
    path.write_text(json.dumps({"api_token": token}))
    _run(bootstrap.apply_pending())
    r = client.post("/api/tokens/rotate", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    new = r.json()["token"]
    assert new != token and new.startswith("cbt_")
    assert client.get("/api/system/capacity", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert client.get("/api/system/capacity", headers={"Authorization": f"Bearer {new}"}).status_code == 200


def test_only_a_token_can_replace_itself(admin):
    assert admin.post("/api/tokens/rotate").status_code == 400
