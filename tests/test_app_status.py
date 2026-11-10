"""An app's status is not stored: it follows from its instances (a cron job from its schedule)."""
import asyncio

import pytest

from apps.runtime import status_of_instances
from apps.serializers import app_status


class _App:
    def __init__(self, cron_schedule=None, last_error=None, cron_enabled=True, id=0):
        self.cron_schedule, self.last_error, self.cron_enabled, self.id = cron_schedule, last_error, cron_enabled, id


@pytest.mark.parametrize("statuses,expected", [
    (["running"], "running"),
    (["running", "error", "stopped"], "running"),            # one running instance is enough
    (["starting", "stopped"], "starting"),
    (["stopping", "stopped"], "stopping"),
    (["stopped", "stopped"], "stopped"),
    (["error"], "error"),
    (["error", "stopped", "pending"], "error"),
    (["node_offline"], "stopped"),                            # unreachable: not running, not an error
    (["node_offline", "error"], "stopped"),
    (["pending"], "stopped"),
])
def test_status_from_instances(statuses, expected):
    assert status_of_instances(statuses) == expected
    assert app_status(_App(), statuses) == expected


def test_an_app_without_instances_is_stopped_or_shows_that_its_deploy_failed():
    assert app_status(_App(), []) == "stopped"
    assert app_status(_App(last_error="clone failed"), []) == "error"
    assert app_status(_App(last_error="clone failed"), ["stopped"]) == "stopped"      # once it has instances they decide


def test_a_cron_job_follows_its_schedule_not_instances():
    assert app_status(_App(cron_schedule="* * * * *"), []) == "scheduled"
    assert app_status(_App(cron_schedule="* * * * *", cron_enabled=False), []) == "paused"


# ── through the API ───────────────────────────────────────────────────────────
def _rows(app_name, statuses):
    from core.database import AsyncSessionLocal
    from core.models import Application, ApplicationReplica
    from nodes.registry import ensure_local_node

    async def go():
        async with AsyncSessionLocal() as db:
            node = await ensure_local_node(db)
            app = Application(name=app_name, repo_url="https://example.test/x", start_command="x", port=3000)
            db.add(app)
            await db.flush()
            for i, status in enumerate(statuses):
                db.add(ApplicationReplica(app_id=app.id, node_id=node.id, status=status, external_port=9700 + i + hash(app_name) % 50 * 10))
            await db.commit()
            return app.id
    return asyncio.run(go())


def _set_instance(app_id, status):
    from sqlalchemy import update
    from core.database import AsyncSessionLocal
    from core.models import ApplicationReplica

    async def go():
        async with AsyncSessionLocal() as db:
            await db.execute(update(ApplicationReplica).where(ApplicationReplica.app_id == app_id).values(status=status))
            await db.commit()
    asyncio.run(go())


def test_the_status_in_the_api_follows_the_instances_without_anyone_writing_it(admin):
    app_id = _rows("status-follows", ["starting"])
    assert admin.get(f"/api/apps/{app_id}").json()["status"] == "starting"
    _set_instance(app_id, "running")                           # only the instance changes (a node's report does exactly this)
    assert admin.get(f"/api/apps/{app_id}").json()["status"] == "running"
    listed = {a["id"]: a for a in admin.get("/api/apps").json()}
    assert listed[app_id]["status"] == "running"
    _set_instance(app_id, "stopped")
    assert admin.get(f"/api/apps/{app_id}").json()["status"] == "stopped"
    assert {a["id"]: a for a in admin.get("/api/apps").json()}[app_id]["status"] == "stopped"


def test_stop_and_start_answer_with_the_status_of_the_instances(admin):
    app_id = _rows("status-answers", [])
    assert admin.post(f"/api/apps/{app_id}/stop").json() == {"status": "stopped", "instance_count": 0}


def test_background_workers_pick_apps_by_their_running_instances(client):
    """The autoscaler and the stats collector select 'apps that run' in SQL from the instances."""
    from sqlalchemy import select
    from apps.runtime import has_running_instance
    from core.database import AsyncSessionLocal
    from core.models import Application
    up = _rows("sql-running", ["running", "stopped"])
    down = _rows("sql-stopped", ["stopped", "error"])
    none = _rows("sql-empty", [])

    async def go():
        async with AsyncSessionLocal() as db:
            return {a.id for a in (await db.execute(select(Application).where(has_running_instance()))).scalars().all()}
    ids = asyncio.run(go())
    assert up in ids and down not in ids and none not in ids


def test_an_instance_only_takes_a_known_status():
    """A typo in a status (or a status nothing understands) fails where it is written, not later in nginx or the panel."""
    from core.models import INSTANCE_STATUSES, ApplicationReplica
    row = ApplicationReplica(app_id=1, node_id=1)
    for status in INSTANCE_STATUSES:
        row.status = status
    with pytest.raises(ValueError, match="unknown instance status"):
        row.status = "runing"
    with pytest.raises(ValueError):
        ApplicationReplica(app_id=1, node_id=1, status="restarting")
