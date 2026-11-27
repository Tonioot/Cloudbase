"""Connections: what an app's start needs to know about the apps and databases it reaches and the apps that reach it.

Two kinds. Everything in a project whose apps reach each other gets a fixed address: the instance's spec holds the
variables, and every node is told where the targets run now (project_routes), so they are followed without a restart.
A link made by hand between two apps writes where the target's instances are into the spec when the app starts
(apps/runtime.py); the node turns that into a URL and networks (containers/client.py), and a target that moves is
followed at the next start.
"""
import logging
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from containers import naming
from apps.images import _image_port
from apps.nginx_sync import _has_public_nginx_domain
from core import projects as core_projects
from core.models import AppLink, Application, Database, DatabaseLink, Instance, Node, Project
from dbs import docker as dbs_docker, engines as db_engines, links as db_links

log = logging.getLogger("cloudbase.apps")

ENV_VAR_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
LIVE = ("running", "starting")
SERVING = ("running",)      # takes traffic from the other apps of its project: started and healthy


def default_env_var(target) -> str:
    """BACKEND_URL for an app (or database) called "backend"."""
    base = re.sub(r"[^A-Z0-9]+", "_", (target.name or "APP").upper()).strip("_") or "APP"
    return f"{'_' + base if base[0].isdigit() else base}_URL"


def _reachable_from_other_nodes(target: Application, node: Node) -> bool:
    """Whether the instance publishes its port on the node network: an instance on a node does; one on the primary
    does not while nginx is in front of it (then it listens on localhost only)."""
    return not node.is_local or not _has_public_nginx_domain(target)


def _reachable(app: Application) -> bool:
    """Whether other apps can talk to it: it has a web server of its own (a static site served from disk has no
    container to talk to)."""
    served_from_disk = app.app_type == "static" and bool(app.static_release)      # apps/static_sites.serves
    return not app.no_web and not app.cron_schedule and not served_from_disk


async def _targets(db: AsyncSession, target: Application, nodes: dict) -> list[dict]:
    """Where the running instances of an app are, and the address each is reached at from other nodes."""
    out = []
    instances = (await db.execute(
        select(Instance).where(Instance.app_id == target.id, Instance.status.in_(LIVE)).order_by(Instance.id))).scalars().all()
    for instance in instances:
        node = nodes.get(instance.node_id)
        address = db_links.node_address(node) if node is not None and instance.external_port else None
        out.append({"node_id": instance.node_id, "port": instance.external_port,
                    "address": address if node is not None and _reachable_from_other_nodes(target, node) else None})
    return out


async def _project_siblings(db: AsyncSession, app: Application) -> list[Application]:
    """The other web apps of the app's project, when the apps of that project reach each other."""
    if app.project_id is None or not core_projects.apps_connected(app.project_id):
        return []
    rows = (await db.execute(select(Application).where(
        Application.project_id == app.project_id, Application.id != app.id).order_by(Application.id))).scalars().all()
    return [a for a in rows if _reachable(a)]


def database_variables(databases) -> list[tuple[str, object]]:
    """(variable, database) for the databases of one project, in order of their ids: the name given to a database, else
    <NAME>_URL; a second database that would get the same one gets <NAME>_2_URL."""
    out, taken = [], set()
    for database in sorted(databases, key=lambda d: d.id):
        wanted = database.env_var_name or default_env_var(database)
        name, number = wanted, 2
        while name in taken:
            stem = wanted[:-4] if wanted.endswith("_URL") else wanted
            name, number = f"{stem}_{number}" + ("_URL" if wanted.endswith("_URL") else ""), number + 1
        taken.add(name)
        out.append((name, database))
    return out


def _database_url(database) -> str | None:
    """The database's address inside its project, with its login."""
    try:
        engine = db_engines.get_engine(database.engine)
        return engine.url(db_links.decrypt_credentials(database), naming.database_alias(database.id),
                          naming.route_port(database_id=database.id))
    except Exception:
        log.warning("[project] cannot build the address of database %s", database.id, exc_info=True)
        return None


async def project_connections(db: AsyncSession, app: Application, taken: set[str] = frozenset()) -> list[dict]:
    """What an app of a connected project gets: every database and every other web app of the project, each as a
    variable with an address that stays the same (containers/routes.py). A variable the app sets itself, shares
    through its project or has from a connection made by hand (`taken` and the app's own) wins; a database goes
    before an app of the same name."""
    if app.project_id is None or not core_projects.apps_connected(app.project_id):
        return []
    taken = set(taken) | set(core_projects.app_env(app)) | await reserved_env_names(db, app.id)
    network = naming.project_network_name(app.project_id)
    out = []
    databases = (await db.execute(select(Database).where(Database.project_id == app.project_id))).scalars().all()
    for env_var, database in database_variables(databases):
        url = None if env_var in taken else _database_url(database)
        taken.add(env_var)
        if url:
            out.append({"kind": "database", "id": database.id, "name": database.name, "env_var": env_var, "url": url,
                        "alias": naming.database_alias(database.id), "network": network})
    for target in await _project_siblings(db, app):
        env_var = default_env_var(target)
        if env_var in taken:
            continue
        taken.add(env_var)
        alias = naming.app_alias(target.id)
        out.append({"kind": "app", "id": target.id, "name": target.name, "env_var": env_var, "alias": alias,
                    "url": f"http://{alias}:{naming.route_port(app_id=target.id)}", "network": network})
    return out


async def runtime_options(db: AsyncSession, app: Application) -> dict:
    """The docker options of one app that come from its connections: {"linked_apps": [...], "exposed_to": [...],
    "project": [...]}.

    linked_apps are the apps connected by hand; project is what its project gives it (project_connections)."""
    options: dict = {}
    incoming = (await db.execute(select(AppLink.id).where(AppLink.target_app_id == app.id))).scalars().all()
    exposed = [{"network": naming.app_link_network_name(i), "alias": naming.app_alias(app.id)} for i in incoming]
    if core_projects.apps_connected(app.project_id) and _reachable(app):
        exposed.append({"network": naming.project_network_name(app.project_id), "alias": naming.app_alias(app.id)})
    if exposed:
        options["exposed_to"] = exposed
    outgoing = (await db.execute(
        select(AppLink, Application).join(Application, Application.id == AppLink.target_app_id)
        .where(AppLink.app_id == app.id).order_by(AppLink.id))).all()
    if outgoing:
        nodes = {n.id: n for n in (await db.execute(select(Node))).scalars()}
        options["linked_apps"] = [
            {"link_id": link.id, "env_var": link.env_var_name, "target_app_id": target.id,
             "alias": naming.app_alias(target.id), "port": _image_port(target) or 80,
             "network": naming.app_link_network_name(link.id), "targets": await _targets(db, target, nodes)}
            for link, target in outgoing]
    project = await project_connections(db, app)
    if project:
        options["project"] = [{k: e[k] for k in ("env_var", "url", "alias", "network")} for e in project]
    return options


async def project_routes(db: AsyncSession) -> list[dict]:
    """Where every app and database of the connected projects runs now, for the nodes to forward their fixed addresses
    to (containers/routes.py). An app's instances only once they are healthy."""
    projects = [p for p in (await db.execute(select(Project.id).order_by(Project.id))).scalars()
                if core_projects.apps_connected(p)]
    if not projects:
        return []
    nodes = {n.id: n for n in (await db.execute(select(Node))).scalars()}
    out = []
    databases = (await db.execute(select(Database).where(Database.project_id.in_(projects))
                                  .order_by(Database.id))).scalars().all()
    for database in databases:
        node = nodes.get(database.node_id)
        if node is None or database.desired_state != "running":
            continue
        try:
            internal = db_engines.get_engine(database.engine).port
        except Exception:
            continue
        out.append({"network": naming.project_network_name(database.project_id),
                    "port": naming.route_port(database_id=database.id),
                    "targets": [{"node_id": node.id, "container": naming.database_container_name(database.id),
                                 "internal_port": internal, "address": db_links.node_address(node),
                                 "port": dbs_docker.published_port(database.id, database.node_port)}]})
    apps = (await db.execute(select(Application).where(Application.project_id.in_(projects))
                             .order_by(Application.id))).scalars().all()
    reachable = {a.id: a for a in apps if _reachable(a)}
    instances = (await db.execute(select(Instance).where(
        Instance.app_id.in_(list(reachable)), Instance.status.in_(SERVING)).order_by(Instance.id))).scalars().all()
    for app in reachable.values():
        targets = []
        for instance in (i for i in instances if i.app_id == app.id):
            node = nodes.get(instance.node_id)
            if node is None:
                continue
            published = instance.external_port and _reachable_from_other_nodes(app, node)
            targets.append({"node_id": node.id, "container": naming.instance_container_name(app.id, instance.id),
                            "internal_port": _image_port(app), "port": instance.external_port,
                            "address": db_links.node_address(node) if published else None})
        if targets:
            out.append({"network": naming.project_network_name(app.project_id),
                        "port": naming.route_port(app_id=app.id), "targets": targets})
    return out


async def reserved_env_names(db: AsyncSession, app_id: int) -> set[str]:
    """Names an app cannot set itself: its links own them."""
    names = set((await db.execute(select(AppLink.env_var_name).where(AppLink.app_id == app_id))).scalars())
    names |= set((await db.execute(select(DatabaseLink.env_var_name).where(DatabaseLink.app_id == app_id))).scalars())
    return names
