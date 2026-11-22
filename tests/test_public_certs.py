import asyncio
from types import SimpleNamespace

from dbs import public_certs


def _db(engine="postgres", tls=True, hostname=None, id=1):
    return SimpleNamespace(id=id, name="shop", engine=engine, public_tls=tls, public_hostname=hostname)


def _resolve(db, host, monkeypatch, domain="db.example.com", own=("", ""), exported=None, wants=True):
    started = []
    monkeypatch.setattr(public_certs._syscfg, "get_db_domain_cached", lambda: domain)
    monkeypatch.setattr(public_certs._syscfg, "get_db_ssl_cached", lambda: own)
    monkeypatch.setattr(public_certs, "_export", lambda h: exported)
    monkeypatch.setattr(public_certs._certificates, "wants_auto_certificate", lambda h: wants)

    async def ensure(host, on_ready):
        started.append(host)
    monkeypatch.setattr(public_certs._certificates, "ensure_auto_certificate", ensure)

    async def run():
        result = await public_certs.resolve(db, host, lambda: None)
        await asyncio.sleep(0)          # let a started issue task run
        return result
    return asyncio.run(run()), started


def test_bare_ip_keeps_the_cloudbase_ca(monkeypatch):
    (state, cert), started = _resolve(_db(), "203.0.113.5", monkeypatch, domain="")
    assert (state, cert, started) == ("cloudbase", None, [])


def test_mysql_and_tls_off_need_no_certificate_of_ours(monkeypatch):
    assert _resolve(_db("mysql"), "shop.db.example.com", monkeypatch)[0][0] == "none"
    assert _resolve(_db(tls=False), "shop.db.example.com", monkeypatch)[0][0] == "none"


def test_uploaded_wildcard_serves_names_under_the_database_domain(monkeypatch):
    (state, cert), _ = _resolve(_db(), "shop.db.example.com", monkeypatch, own=("/c.pem", "/k.pem"))
    assert state == "own" and cert[:2] == ("/c.pem", "/k.pem")


def test_a_hostname_of_its_own_is_never_covered_by_the_wildcard(monkeypatch):
    (state, _), started = _resolve(_db(hostname="data.other.org"), "data.other.org", monkeypatch, own=("/c.pem", "/k.pem"))
    assert state == "waiting" and started == ["data.other.org"]


def test_automatic_uses_the_lets_encrypt_copy_when_there_is_one(monkeypatch):
    (state, cert), started = _resolve(_db(), "shop.db.example.com", monkeypatch, exported=("/a.pem", "/b.pem", "abc"))
    assert (state, cert, started) == ("letsencrypt", ("/a.pem", "/b.pem", "abc"), [])


def test_without_a_certificate_yet_the_port_waits_and_one_is_requested(monkeypatch):
    (state, cert), started = _resolve(_db(), "shop.db.example.com", monkeypatch)
    assert (state, cert, started) == ("waiting", None, ["shop.db.example.com"])
