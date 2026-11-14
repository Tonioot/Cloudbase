"""The Connections tab: databases and other apps an app is linked to, and the variable each connection owns."""
import asyncio

import pytest
from sqlalchemy import delete

from apps import app_links
from core.database import AsyncSessionLocal
from core.env_crypto import decrypt_env, encrypt_env
from core.models import AppLink, Application, Instance, Node


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def two_apps():
    """A front and a back app, removed again afterwards."""
    async def make():
        async with AsyncSessionLocal() as db:
            front = Application(name="conn-front", repo_url="https://example.com/f.git", port=3000, env_vars=encrypt_env({"BACKEND_URL": "old", "KEEP": "1"}))
            back = Application(name="conn-back", repo_url="https://example.com/b.git", port=4000)
            db.add_all([front, back])
            await db.commit()
            return front.id, back.id

    async def clean(ids):
        async with AsyncSessionLocal() as db:
            for app_id in ids:
                app = await db.get(Application, app_id)
                if app is not None:
                    await db.delete(app)
            await db.commit()
    ids = run(make())
    yield ids
    run(clean(ids))


def test_an_app_connects_to_another_and_the_connection_owns_the_variable(admin, two_apps):
    front, back = two_apps
    body = {"kind": "app", "target_id": back, "env_var_name": "BACKEND_URL"}
    first = admin.post(f"/api/apps/{front}/connections", json=body)
    assert first.status_code == 409 and "already sets BACKEND_URL" in first.json()["detail"]      # an own value is never dropped silently

    done = admin.post(f"/api/apps/{front}/connections", json={**body, "replace_env": True})
    assert done.status_code == 200, done.text
    assert done.json()["env_var_name"] == "BACKEND_URL"
    listed = admin.get(f"/api/apps/{front}/connections").json()
    assert [a["name"] for a in listed["apps"]] == ["conn-back"]
    assert listed["reserved_env"] == [done.json()["env_var_name"]]
    assert [u["name"] for u in admin.get(f"/api/apps/{back}/connections").json()["used_by"]] == ["conn-front"]


def test_a_connection_takes_the_place_of_an_own_variable_with_the_same_name(admin, two_apps):
    front, back = two_apps
    admin.post(f"/api/apps/{front}/connections", json={"kind": "app", "target_id": back, "env_var_name": "BACKEND_URL", "replace_env": True})

    async def env():
        async with AsyncSessionLocal() as db:
            return decrypt_env((await db.get(Application, front)).env_vars)
    assert run(env()) == {"KEEP": "1"}

    clash = admin.put(f"/api/apps/{front}", json={"env_vars": {"BACKEND_URL": "from an imported .env"}})
    assert clash.status_code == 409 and "managed by a connection" in clash.json()["detail"]
    assert admin.put(f"/api/apps/{front}", json={"env_vars": {"OTHER": "x"}}).status_code == 200


def test_an_app_cannot_connect_to_itself_twice_or_to_something_without_a_web_server(admin, two_apps):
    front, back = two_apps
    assert admin.post(f"/api/apps/{front}/connections", json={"kind": "app", "target_id": front}).status_code == 400
    body = {"kind": "app", "target_id": back, "env_var_name": "API_URL"}
    assert admin.post(f"/api/apps/{front}/connections", json=body).status_code == 200
    assert admin.post(f"/api/apps/{front}/connections", json={**body, "env_var_name": "OTHER_URL"}).status_code == 409
    options = admin.get(f"/api/apps/{front}/connections/options").json()
    assert all(a["id"] not in (front, back) for a in options["apps"])           # not itself, not what it already has


def test_removing_a_connection_frees_the_variable(admin, two_apps):
    front, back = two_apps
    link = admin.post(f"/api/apps/{front}/connections", json={"kind": "app", "target_id": back, "env_var_name": "API_URL"}).json()
    gone = admin.delete(f"/api/apps/{front}/connections/app/{link['id']}")
    assert gone.status_code == 200 and sorted(gone.json()["restart"]) == sorted([front, back])
    assert admin.get(f"/api/apps/{front}/connections").json()["reserved_env"] == []
    assert admin.put(f"/api/apps/{front}", json={"env_vars": {"API_URL": "mine"}}).status_code == 200


def test_the_start_of_an_app_learns_where_its_targets_run_and_who_reaches_it(two_apps):
    front, back = two_apps

    async def go():
        async with AsyncSessionLocal() as db:
            node = Node(name="conn-node", status="online", auth_token="conn-token", is_local=False, enabled=True, wg_ip="10.77.0.4")
            db.add(node)
            await db.flush()
            db.add(Instance(app_id=back, node_id=node.id, external_port=20042, status="running"))
            link = AppLink(app_id=front, target_app_id=back, env_var_name="API_URL")
            db.add(link)
            await db.commit()
            front_options = await app_links.runtime_options(db, await db.get(Application, front))
            back_options = await app_links.runtime_options(db, await db.get(Application, back))
            ids = node.id, link.id
        async with AsyncSessionLocal() as db:
            await db.execute(delete(Instance).where(Instance.app_id == back))
            await db.delete(await db.get(Node, ids[0]))
            await db.commit()
        return ids, front_options, back_options
    (node_id, link_id), front_options, back_options = run(go())

    assert front_options["linked_apps"] == [{
        "link_id": link_id, "env_var": "API_URL", "target_app_id": back, "alias": f"app-{back}", "port": 4000,
        "network": f"cloudbase-link-{link_id}",
        "targets": [{"node_id": node_id, "port": 20042, "address": "10.77.0.4"}]}]
    assert back_options == {"exposed_to": [{"network": f"cloudbase-link-{link_id}", "alias": f"app-{back}"}]}
    assert "exposed_to" not in front_options


def test_the_default_variable_is_made_from_the_name_of_the_target():
    assert app_links.default_env_var(Application(name="my-backend")) == "MY_BACKEND_URL"
    assert app_links.default_env_var(Application(name="2fa")) == "_2FA_URL"


def test_an_app_can_be_blocked_from_the_local_network_and_old_apps_are_not(admin, two_apps):
    front, _back = two_apps
    assert admin.get(f"/api/apps/{front}").json()["block_local_network"] is False        # made before the setting existed
    assert admin.put(f"/api/apps/{front}", json={"block_local_network": True}).status_code == 200
    assert admin.get(f"/api/apps/{front}").json()["block_local_network"] is True
