"""The node page: what runs on a node, and that the page's script and markup belong together."""
import asyncio
import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def node_with_work(client):
    from sqlalchemy import delete
    from core.database import AsyncSessionLocal
    from core.models import Application, ApplicationReplica, Database, Node

    async def setup():
        async with AsyncSessionLocal() as db:
            node = Node(name="page-node", role="node", status="online", auth_token="token-page", is_local=False, enabled=True,
                        wg_ip="10.77.0.9", wg_public_key="P" * 44, wg_state="up", agent_version="2.0.0")
            other = Node(name="other-page-node", role="node", status="online", auth_token="token-page-2", is_local=False, enabled=True)
            app_b = Application(name="b-shop", repo_url="https://example.test/b", start_command="x", port=3000)
            app_a = Application(name="a-blog", repo_url="https://example.test/a", start_command="x", port=3000)
            db.add_all([node, other, app_a, app_b])
            await db.flush()
            db.add_all([
                ApplicationReplica(app_id=app_b.id, node_id=node.id, status="running", external_port=9601),
                ApplicationReplica(app_id=app_a.id, node_id=node.id, status="error", external_port=9602, last_error="no space left"),
                ApplicationReplica(app_id=app_a.id, node_id=other.id, status="running", external_port=9603),
                Database(name="page-db", engine="postgres", version="16", node_id=node.id, status="running", credentials="x"),
                Database(name="elsewhere-db", engine="redis", version="7", node_id=other.id, status="running", credentials="x"),
            ])
            await db.commit()
            return node.id, other.id, [app_a.id, app_b.id]
    node_id, other_id, app_ids = _run(setup())
    yield node_id

    async def cleanup():
        async with AsyncSessionLocal() as db:
            await db.execute(delete(Database).where(Database.node_id.in_([node_id, other_id])))
            await db.execute(delete(ApplicationReplica).where(ApplicationReplica.app_id.in_(app_ids)))
            await db.execute(delete(Application).where(Application.id.in_(app_ids)))
            await db.execute(delete(Node).where(Node.id.in_([node_id, other_id])))
            await db.commit()
    _run(cleanup())


def test_the_page_gets_the_instances_and_databases_of_that_node_only(admin, node_with_work):
    body = admin.get(f"/api/nodes/{node_with_work}/workloads").json()
    assert [(i["app_name"], i["status"]) for i in body["instances"]] == [("a-blog", "error"), ("b-shop", "running")]   # by name
    assert [d["name"] for d in body["databases"]] == ["page-db"]
    assert body["databases"][0] == {"id": body["databases"][0]["id"], "name": "page-db", "engine": "postgres", "version": "16", "status": "running"}


def test_each_instance_says_where_the_primary_reaches_it(admin, node_with_work):
    instances = {i["app_name"]: i for i in admin.get(f"/api/nodes/{node_with_work}/workloads").json()["instances"]}
    assert instances["b-shop"]["address"] == "10.77.0.9:9601"                 # over the node network
    assert instances["a-blog"]["last_error"] == "no space left" and instances["a-blog"]["port"] == 9602


def test_a_node_without_work_has_empty_lists(admin):
    from nodes.registry import ensure_local_node
    from core.database import AsyncSessionLocal

    async def local_id():
        async with AsyncSessionLocal() as db:
            return (await ensure_local_node(db)).id
    body = admin.get(f"/api/nodes/{_run(local_id())}/workloads").json()
    assert set(body) == {"instances", "databases"}


def test_an_unknown_node_is_not_found(admin):
    assert admin.get("/api/nodes/987654/workloads").status_code == 404


# ── the page itself ───────────────────────────────────────────────────────────
def test_every_element_the_script_looks_for_is_on_the_page():
    html = (FRONTEND / "node.html").read_text(encoding="utf-8")
    script = (FRONTEND / "js" / "node_detail.js").read_text(encoding="utf-8")
    ids = set(re.findall(r"getElementById\('([^']+)'\)", script)) | set(re.findall(r"setTile\('([^']+)'", script))
    page_ids = set(re.findall(r'\bid="([^"]+)"', html))
    assert not (ids - page_ids), f"the script uses ids the page does not have: {sorted(ids - page_ids)}"


def test_the_page_loads_its_own_stylesheet_and_nothing_is_left_of_the_old_layout():
    html = (FRONTEND / "node.html").read_text(encoding="utf-8")
    assert 'href="css/node.css"' in html and (FRONTEND / "css" / "node.css").exists()
    for gone in ("node-summary", "node-overview-panel", "node-ping-badge", "node-chart-stack", "node-bottom"):
        assert gone not in html
        for css in (FRONTEND / "css").glob("*.css"):
            assert gone not in css.read_text(encoding="utf-8"), f"{gone} in {css.name}"


def test_the_page_tells_what_the_connection_is_in_every_state():
    script = (FRONTEND / "js" / "node_detail.js").read_text(encoding="utf-8")
    for words in ("This server", "Offline", "Connected", "Network down", "Not on the node network", "cloudbase wireguard"):
        assert words in script


def test_everything_that_comes_from_a_node_is_escaped():
    """Names, errors and addresses come from outside: none may reach the page as markup."""
    script = (FRONTEND / "js" / "node_detail.js").read_text(encoding="utf-8")
    rows = script[script.index("const instanceRow"):script.index("list.innerHTML = `<table")]
    for field in ("i.app_name", "i.last_error", "i.address", "d.name", "d.engine", "d.version"):
        assert f"escHtml({field}" in rows, field
