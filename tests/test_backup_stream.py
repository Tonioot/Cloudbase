"""A node dumps a database straight into the upload to the primary: nothing on disk, and a dump that fails halfway
must fail the command instead of leaving a half backup."""
import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from agent.commands import databases
from dbs import backup_restore


class _Response:
    def raise_for_status(self):
        pass


class _Client:
    """Reads the upload body the way httpx does and remembers what arrived."""

    def __init__(self, fail_get=False):
        self.received = b""
        self.urls = []
        self.fail_get = fail_get

    async def get(self, url, timeout=None):
        self.urls.append(url)
        if self.fail_get:
            raise OSError("unreachable")
        return _Response()

    async def post(self, url, content, headers, timeout):
        self.urls.append(url)
        async for chunk in content:
            self.received += chunk
        return _Response()


def _state():
    return SimpleNamespace(main_url="https://panel.example.com", auth_token="t")


def _payload(hub_url=None):
    return {"spec": {"database_id": 8}, "backup_id": 117, "hub_url": hub_url}


def test_dump_goes_to_the_upload_without_a_file(monkeypatch):
    data = [b"a" * 100_000 for _ in range(40)]       # more than the queue holds: the dump has to wait

    def fake_dump(spec, sink):
        for chunk in data:
            sink.write(chunk)

    monkeypatch.setattr(backup_restore, "dump_to_stream", fake_dump)
    client = _Client()
    info = asyncio.run(databases.cmd_db_backup(client, _state(), None, _payload()))
    whole = b"".join(data)
    assert client.received == whole
    assert info == {"size_bytes": len(whole), "sha256": hashlib.sha256(whole).hexdigest()}
    assert client.urls == ["https://panel.example.com/api/nodes/agent/databases/8/backups/117/upload"]


def test_failed_dump_fails_the_command(monkeypatch):
    def fake_dump(spec, sink):
        sink.write(b"half")
        raise RuntimeError("Dump failed (exit 1): no space")

    monkeypatch.setattr(backup_restore, "dump_to_stream", fake_dump)
    with pytest.raises(RuntimeError, match="no space"):
        asyncio.run(databases.cmd_db_backup(_Client(), _state(), None, _payload()))


def test_node_network_is_used_when_it_answers(monkeypatch):
    monkeypatch.setattr(backup_restore, "dump_to_stream", lambda spec, sink: sink.write(b"x"))
    client = _Client()
    asyncio.run(databases.cmd_db_backup(client, _state(), None, _payload("http://10.77.0.1:7823")))
    assert client.urls[-1] == "http://10.77.0.1:7823/api/nodes/agent/databases/8/backups/117/upload"


def test_panel_address_when_the_node_network_does_not_answer(monkeypatch):
    monkeypatch.setattr(backup_restore, "dump_to_stream", lambda spec, sink: sink.write(b"x"))
    client = _Client(fail_get=True)
    asyncio.run(databases.cmd_db_backup(client, _state(), None, _payload("http://10.77.0.1:7823")))
    assert client.urls[-1].startswith("https://panel.example.com/")
