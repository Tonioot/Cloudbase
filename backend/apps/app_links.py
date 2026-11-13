"""Links between apps: what an app's start needs to know about the apps it reaches and the apps that reach it.

Written into the spec of an instance when it starts (apps/runtime.py), because that is when the primary knows where
the target's instances are. The node that starts the container turns it into a URL and networks
(containers/client.py). A target that moves to another node is followed at the next start of the linking app.
"""
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from containers import naming
from apps.images import _image_port
from apps.nginx_sync import _has_public_nginx_domain
from core.models import AppLink, Application, DatabaseLink, Instance, Node
from dbs import links as db_links

ENV_VAR_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
LIVE = ("running", "starting")


def default_env_var(target: Application) -> str:
    """BACKEND_URL for an app called "backend"."""
    base = re.sub(r"[^A-Z0-9]+", "_", (target.name or "APP").upper()).strip("_") or "APP"
    return f"{'_' + base if base[0].isdigit() else base}_URL"


def _reachable_from_other_nodes(target: Application, node: Node) -> bool:
    """Whether the instance publishes its port on the node network: an instance on a node does; one on the primary
    does not while nginx is in front of it (then it listens on localhost only)."""
    return not node.is_local or not _has_public_nginx_domain(target)


async def runtime_options(db: AsyncSession, app: Application) -> dict:
    """The docker options of one app that come from its links: {"linked_apps": [...], "exposed_to": [...]}."""
    options: dict = {}
    incoming = (await db.execute(select(AppLink.id).where(AppLink.target_app_id == app.id))).scalars().all()
    if incoming:
        options["exposed_to"] = [{"network": naming.app_link_network_name(i), "alias": naming.app_alias(app.id)} for i in incoming]
    outgoing = (await db.execute(
        select(AppLink, Application).join(Application, Application.id == AppLink.target_app_id)
        .where(AppLink.app_id == app.id).order_by(AppLink.id))).all()
    if not outgoing:
        return options
    nodes = {n.id: n for n in (await db.execute(select(Node))).scalars()}
    linked = []
    for link, target in outgoing:
        targets = []
        instances = (await db.execute(
            select(Instance).where(Instance.app_id == target.id, Instance.status.in_(LIVE)).order_by(Instance.id))).scalars().all()
        for instance in instances:
            node = nodes.get(instance.node_id)
            address = db_links.node_address(node) if node is not None and instance.external_port else None
            targets.append({"node_id": instance.node_id, "port": instance.external_port,
                            "address": address if node is not None and _reachable_from_other_nodes(target, node) else None})
        linked.append({"link_id": link.id, "env_var": link.env_var_name, "target_app_id": target.id,
                       "alias": naming.app_alias(target.id), "port": _image_port(target) or 80,
                       "network": naming.app_link_network_name(link.id), "targets": targets})
    options["linked_apps"] = linked
    return options


async def reserved_env_names(db: AsyncSession, app_id: int) -> set[str]:
    """Names an app cannot set itself: its links own them."""
    names = set((await db.execute(select(AppLink.env_var_name).where(AppLink.app_id == app_id))).scalars())
    names |= set((await db.execute(select(DatabaseLink.env_var_name).where(DatabaseLink.app_id == app_id))).scalars())
    return names
