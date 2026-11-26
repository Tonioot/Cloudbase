""""Update all": every connected node first, then the primary (which restarts the panel)."""
import asyncio
from types import SimpleNamespace

from fastapi import HTTPException

from nodes import updates


def test_the_nodes_go_first_and_the_primary_last_also_when_a_node_fails(monkeypatch):
    order = []

    async def update_node(node):
        order.append(node.name)
        if node.name == "broken":
            raise HTTPException(504, "Timed out waiting for node command result")
        if node.name == "primary":
            assert {"a", "b", "broken"} <= set(order[:-1])          # every node was done before the primary
            return {"status": "updated", "commit": "abc1234"}
        return {"status": "updated", "commit": "abc1234"}

    monkeypatch.setattr(updates, "update_node", update_node)
    nodes = [SimpleNamespace(id=1, name="primary", is_local=True, enabled=True),
             SimpleNamespace(id=2, name="a", is_local=False, enabled=True),
             SimpleNamespace(id=3, name="broken", is_local=False, enabled=True),
             SimpleNamespace(id=4, name="b", is_local=False, enabled=True),
             SimpleNamespace(id=5, name="off", is_local=False, enabled=False)]
    res = asyncio.run(updates.update_all(nodes))
    assert order[-1] == "primary" and "off" not in order
    assert [(n["name"], n["status"]) for n in res["nodes"]] == [("a", "updated"), ("broken", "error"), ("b", "updated")]
    assert "Timed out" in res["nodes"][1]["message"]
    assert res["primary"]["status"] == "updated"
