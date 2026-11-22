"""Public database ports come from a pool: a deleted database's port is free again, and the range is configurable."""
from types import SimpleNamespace

from core import config as _cfg
from dbs.public import pick_public_port


def _db(id, port=None):
    return SimpleNamespace(id=id, public_port=port)


def test_a_new_database_takes_the_lowest_free_port_not_one_by_id(monkeypatch):
    monkeypatch.setattr(_cfg, "get_port", lambda k: {"db_public_min": 31000, "db_public_max": 31009}[k])
    # id 10 would have been 31010 (outside the range) under the old scheme
    assert pick_public_port(_db(10), {31000, 31001}) == 31002
    assert pick_public_port(_db(10), set()) == 31000


def test_a_port_that_was_freed_is_used_again(monkeypatch):
    monkeypatch.setattr(_cfg, "get_port", lambda k: {"db_public_min": 31000, "db_public_max": 31009}[k])
    assert pick_public_port(_db(7), {31000, 31001, 31003}) == 31007   # the old scheme's port, still free
    assert pick_public_port(_db(1), {31000, 31001, 31003}) == 31002   # taken: the lowest free one


def test_a_database_keeps_its_port_while_it_is_in_the_range(monkeypatch):
    monkeypatch.setattr(_cfg, "get_port", lambda k: {"db_public_min": 31000, "db_public_max": 31009}[k])
    assert pick_public_port(_db(3, 31005), {31000}) == 31005
    assert pick_public_port(_db(3, 40000), {31000}) == 31003          # the range moved: a new port


def test_a_full_range_gives_nothing(monkeypatch):
    monkeypatch.setattr(_cfg, "get_port", lambda k: {"db_public_min": 31000, "db_public_max": 31001}[k])
    assert pick_public_port(_db(5), {31000, 31001}) is None



def test_public_host_prefers_own_hostname_then_database_domain_then_ip(monkeypatch):
    from types import SimpleNamespace
    from dbs import public
    monkeypatch.setattr(public, "_public_ip", "203.0.113.5")
    domain = {"v": "db.example.com"}
    monkeypatch.setattr(public._syscfg, "get_db_domain_cached", lambda: domain["v"])
    plain = SimpleNamespace(name="shop-db", public_hostname=None)
    own = SimpleNamespace(name="shop-db", public_hostname="data.other.org")
    assert public.public_host_for(plain) == "shop-db.db.example.com"
    assert public.public_host_for(own) == "data.other.org"
    assert public._certificate_hosts() == ["203.0.113.5", "localhost"]
    domain["v"] = ""
    assert public.public_host_for(plain) == "203.0.113.5"
    assert public.public_host_for(own) == "data.other.org"
    assert public._certificate_hosts() == ["203.0.113.5", "localhost"]
