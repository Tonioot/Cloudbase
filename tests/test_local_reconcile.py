"""The primary runs its own instances from the list, like every node (nodes/local.py).

The reconciler under test manages a node row of its own, so it never meets the one the running app has for
this server; Docker is a stand-in, as in test_reconcile.py.
"""
import asyncio

import pytest

from agent import reconcile
from nodes import local, observed


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _app_started(admin):
    """The database exists once the app has started."""


# ── rows straight in the database ──────────────────────────────────────────────
def _node(name, is_local=False):
    from core.database import AsyncSessionLocal
    from core.models import Node

    async def go():
        async with AsyncSessionLocal() as db:
            node = Node(name=name, role="node", status="online", auth_token=f"token-{name}", is_local=is_local,
                        enabled=True, agent_version="2.0.0")
            db.add(node)
            await db.commit()
            return node.id
    return _run(go())


def _app(name, **fields):
    from core.database import AsyncSessionLocal
    from core.env_crypto import encrypt_env
    from core.models import Application

    async def go():
        async with AsyncSessionLocal() as db:
            app = Application(name=name, repo_url="https://example.test/x", start_command="npm start", port=3000,
                              status="stopped", env_vars=encrypt_env({"A": "1"}), **fields)
            db.add(app)
            await db.commit()
            return app.id
    return _run(go())


def _replica(app_id, node_id, port, status="stopped", desired=None, **fields):
    from core.database import AsyncSessionLocal
    from core.models import ApplicationReplica

    async def go():
        async with AsyncSessionLocal() as db:
            rep = ApplicationReplica(app_id=app_id, node_id=node_id, external_port=port, status=status, desired=desired, **fields)
            db.add(rep)
            await db.commit()
            return rep.id
    return _run(go())


def _row(replica_id):
    from core.database import AsyncSessionLocal
    from core.models import ApplicationReplica

    async def go():
        async with AsyncSessionLocal() as db:
            return await db.get(ApplicationReplica, replica_id)
    return _run(go())


def _with(app_id, replica_id, node_id, action):
    """Run action(app, replica, node, db) in one session and commit."""
    from core.database import AsyncSessionLocal
    from core.models import Application, ApplicationReplica, Node

    async def go():
        async with AsyncSessionLocal() as db:
            app, rep, node = await db.get(Application, app_id), await db.get(ApplicationReplica, replica_id), await db.get(Node, node_id)
            result = await action(app, rep, node, db)
            await db.commit()
            return result
    return _run(go())


class Docker:
    """A stand-in for Docker on this server."""

    def __init__(self, monkeypatch):
        self.containers, self.started, self.removed, self.built = {}, [], [], []
        monkeypatch.setattr(reconcile.containers_replicas, "list_replica_containers", lambda: [dict(c) for c in self.containers.values()])
        monkeypatch.setattr(reconcile.pm, "stop_docker_replica", self._stop)
        monkeypatch.setattr(reconcile.pm, "start_docker_replica", self._start)
        monkeypatch.setattr(local, "ensure_image", self._ensure_image)
        self.promoted = []
        monkeypatch.setattr(observed.promote, "start_promotion", self.promoted.append)

    def add(self, app_id, replica_id, status="running", spec=None):
        self.containers[replica_id] = {"name": f"cloudbase-app-{app_id}-replica-{replica_id}", "id": f"cid{replica_id}" + "0" * 20,
                                       "app_id": app_id, "replica_id": replica_id, "status": status, "spec": spec, "exit_code": 0}

    def _stop(self, app_id, replica_id):
        self.removed.append(replica_id)
        self.containers.pop(replica_id, None)
        return True

    def _start(self, app_id, replica_id, app_name, internal_port, external_port, env, options, image_app_id):
        self.started.append({"replica_id": replica_id, "port": external_port, "env": env, "options": options})
        self.add(app_id, replica_id, spec=options["spec_hash"])
        return self.containers[replica_id]["id"]

    async def _ensure_image(self, client, state, spec, progress, source=None, push=None):
        progress("building_image")
        self.built.append((spec["app_id"], source(spec["app_id"], spec["app_name"]) if source else None))


async def cycle(rec):
    """One round of what runs all the time: ask for the list, make Docker equal to it, report."""
    await rec.pull()
    await rec.reconcile_once()
    for _, task in list(rec.tasks.values()):
        await task
    await rec.reconcile_once()
    await rec.report()


def test_an_instance_started_on_the_primary_goes_through_the_list(monkeypatch):
    from apps import runtime
    docker = Docker(monkeypatch)
    node_id, app_id = _node("lr-start"), _app("lr-start-app")
    rid = _replica(app_id, node_id, 10501)
    _with(app_id, rid, node_id, lambda a, r, n, db: runtime._start_replica_runtime(a, r, n, {"A": "1"}, db))
    assert _row(rid).status == "starting" and _row(rid).desired == "running" and _row(rid).spec_hash

    _run(cycle(local.LocalReconciler(node_id)))

    started = docker.started
    assert len(started) == 1 and started[0]["port"] == 10501 and started[0]["env"] == {"A": "1"}
    assert "bind_host" not in started[0]["options"] or started[0]["options"]["bind_host"] != "10.77.0.2"
    assert docker.built == [(app_id, docker.built[0][1])] and docker.built[0][1]            # built from the source that is already here
    row = _row(rid)
    assert row.container_id and row.status == "starting"                                    # live once it can be reached
    assert docker.promoted == [rid]


def test_stopping_an_instance_on_the_primary_removes_its_container(monkeypatch):
    from apps import runtime
    docker = Docker(monkeypatch)
    node_id, app_id = _node("lr-stop"), _app("lr-stop-app")
    rid = _replica(app_id, node_id, 10502)
    _with(app_id, rid, node_id, lambda a, r, n, db: runtime._start_replica_runtime(a, r, n, {}, db))
    rec = local.LocalReconciler(node_id)
    _run(cycle(rec))
    assert rid in docker.containers

    _with(app_id, rid, node_id, lambda a, r, n, db: runtime._stop_replica_runtime(a, r, n, db))
    assert _row(rid).status == "stopping"
    _run(cycle(rec))
    assert rid not in docker.containers and _row(rid).status == "stopped"


def test_a_deleted_instance_is_removed_because_it_is_no_longer_on_the_list(monkeypatch):
    from core.database import AsyncSessionLocal
    from core.models import ApplicationReplica
    docker = Docker(monkeypatch)
    node_id, app_id = _node("lr-del"), _app("lr-del-app")
    rid = _replica(app_id, node_id, 10503, status="running", desired="running")
    docker.add(app_id, rid, spec=None)

    async def delete():
        async with AsyncSessionLocal() as db:
            await db.delete(await db.get(ApplicationReplica, rid))
            await db.commit()
    rec = local.LocalReconciler(node_id)
    _run(cycle(rec))                                   # an empty list at first: nothing is known to be wrong yet
    docker.add(app_id, rid, spec=None)
    _run(delete())
    _run(cycle(rec))
    assert rid in docker.removed


def test_instances_from_before_the_list_keep_running_and_their_containers_are_adopted(monkeypatch):
    docker = Docker(monkeypatch)
    node_id, app_id = _node("lr-old"), _app("lr-old-app")
    running = _replica(app_id, None, 10504, status="running")           # no node: it meant "this server"
    stopped = _replica(app_id, None, 10505, status="stopped")
    docker.add(app_id, running, spec=None)                              # made before specs existed: no label

    _run(local.adopt_existing(node_id))
    assert (_row(running).node_id, _row(running).desired) == (node_id, "running")
    assert (_row(stopped).node_id, _row(stopped).desired) == (node_id, "stopped")

    _run(cycle(local.LocalReconciler(node_id)))
    assert running in docker.containers and docker.started == [] and docker.removed == []
    assert _row(running).status == "running"


def test_an_instances_own_limits_are_part_of_its_spec():
    from apps import runtime
    from core.models import Application, ApplicationReplica
    app = Application(name="lim", repo_url="https://example.test/x", start_command="x", port=3000,
                      docker_cpu_limit=2.0, docker_memory_limit_mb=512)
    plain = runtime._instance_payload(app, ApplicationReplica(app_id=1, external_port=1), {}, "rev")
    own = runtime._instance_payload(app, ApplicationReplica(app_id=1, external_port=1, docker_cpu_limit=0.5,
                                                            docker_read_only_root=True), {}, "rev")
    assert (plain["docker_cpu_limit"], plain["docker_memory_limit_mb"], plain["docker_read_only_root"]) == (2.0, 512, False)
    assert (own["docker_cpu_limit"], own["docker_memory_limit_mb"], own["docker_read_only_root"]) == (0.5, 512, True)


def test_a_worker_has_no_port_to_wait_for():
    from mesh import routing
    node_id, app_id = _node("lr-worker"), _app("lr-worker-app", no_web=True)
    rid = _replica(app_id, node_id, 10506, status="running")
    assert _run(routing.wait_reachable(rid, 1)) == ""
