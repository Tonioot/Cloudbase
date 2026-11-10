# Backend architecture

Entry points (other files call these by path, so they stay where they are):

- `main.py` — `uvicorn main:app`. Only wiring: creates the app, adds middleware, includes the routers.
- `node_agent.py` — `python node_agent.py …` on a node (called by `install.sh` / `start.sh`).

## Layers

Dependencies point one way. A package may import from the ones below it, never from above.

```
main.py, lifespan.py        wiring: may import routers and workers
routers/   workers/         HTTP endpoints / background loops  — thin: validate input, call a service
apps/ nodes/ dbs/ notify/   services: the actual logic per area
cron/ nginx/ containers/
core/                       config, database, models, auth, encryption, logging, paths
agent/                      the node agent (runs on remote servers, talks to the primary)
```

| Package | What lives there |
|---|---|
| `core/` | `config`, `database` + `models`, `auth` (sessions and the per-endpoint permission checks; `access`: the middleware, which only checks that somebody is signed in and looks the user up once per request), `env_crypto`, `token_vault`, `system_config`, `applog`/`system_logs`, `paths` (every file location), `runtime` (port, `IS_NODE`), `history` |
| `apps/` | application logic: `git`, `images`, `validation`, `nginx_sync`, `runtime` (starting/stopping instances), `deployments` (run a deploy), `rollout` (replace instances: add new ones, wait until promote put them live, remove the old ones; also used by the autoscaler), `lifecycle`, `serializers`, `state` |
| `nodes/` | `state` (live connections), `desired` (what each node should run: written by start/stop), `snapshot` (the list a node asks for), `observed` (a node's report becomes a status), `local` (the primary's own instances run from the same list), `promote` (an instance goes live once reachable; leaves nginx when the link breaks), `commands` (one-off questions and jobs: logs, stats, backups, cron), `registry` (local node, lookups), `liveness` (silent nodes go offline) |
| `mesh/` | the node network (WireGuard, hub and spoke with the primary as hub): `addressing`, `helper` (client for the root helper `scripts/cloudbase-wireguard`, via sudo), `hub` (interface, peers, enrolment, handshake health), `routing` (`backend_for`, `wait_reachable`) |
| `containers/` | Docker on one host: `client`, `naming`, `buildkit`, `dockerfiles`, `build`, `prepare` (get an image onto this machine), `reconciler` (make Docker equal to the list), `cleanup`, `apps`, `instances`, `jobs`, `stats_logs`, `processes` |
| `nginx/` | `paths`, `pages` (HTML), `config` (nginx config text), `files` (writers), `certbot` (read-only lookups), `certificates` (issuing), `panel` |
| `dbs/` | managed databases: `docker`, `lifecycle`, `backup_restore`, `updates`, `ops`, `provision`, `view`, `public`, `proxy`, `workers`, `backups`, `backup_scheduler`, … |
| `transfer/` | export and import of apps and databases as one file (a tar): `archive` (manifest + dumps), `manifest`/`databases` (what goes in), `jobs`/`exporter` (export runs in the background), `importer`/`db_import` (what comes out) |
| `notify/` | `events`, `settings`, `config`, `emit`, `dispatch`, `watch`, `channels`, `mailer` |
| `routers/` | one package per area (`applications/`, `nodes/`, `databases/`, `database_backups/`), each with a module per concern; the package `__init__` composes them in order |

## Rules

1. **Routers and workers stay thin.** Logic that more than one caller needs goes in a service. A router never imports another router area; a service never imports a router.
2. **No imports inside functions to dodge cycles.** If two modules need each other, move the shared part into a third module below them.
3. **State has one owner.** A module-level dict or flag is defined in one module, and only that module rebinds it. Others read it through a function (an imported name would go stale when it is rebound).
4. **Files stay under ~500 lines, functions under ~100.** Split by concern, not by size.
5. **One action at a time per app.** Whatever changes an app's instances (deploy, start, stop, restart, scale, removing an instance, delete, the autoscaler) holds the app through `apps/state.py` (`@locked("…")`, `app_lock`, or `acquire_app`/`release_app`); a second action is refused with a 409. Code that runs inside such an action must not take the lock again (a deploy restarts through `restart_locked`).
6. **Derived state is not stored.** An app has no status column: `apps/serializers.py` `app_status()` computes it from the statuses of its instances (a cron job from its schedule), and background code that needs 'apps that run' uses `apps/runtime.py` `has_running_instance()` in SQL. Only the instance status is stored, and only `nodes/observed.py`, `nodes/promote.py` and `nodes/liveness.py` decide it (the start/stop/deploy code writes `desired`).
7. **Route order matters.** FastAPI uses the first matching route. Include order is set in `main.py` and in each `routers/<area>/__init__.py`.

`tests/test_app_structure.py` enforces rules 1, 2 and 7 (and that every route still exists); rules 3, 4, 5 and 6 are by review (`tests/test_app_lock.py` covers the endpoints of rule 5).

## Working on it

```
pip install -r requirements-dev.txt
pytest                                # unit tests, route snapshot, references, and a smoke test that boots the app
ruff check backend tests scripts
```

- Moved or renamed a module? `tests/test_references.py` checks every `from x import y` and `module.attr`; `tests/test_scripts.py` checks the Python snippets inside `start.sh` / `install.sh`.
- Added or removed an endpoint on purpose? Regenerate the snapshot: `UPDATE_SNAPSHOTS=1 pytest tests/test_app_structure.py`, and review the diff of `tests/routes_snapshot.txt`.
- Tests run against a throwaway home directory (`tests/conftest.py`); they never touch `~/.cloudbase`.

## Names

An app runs as **instances** (model `Instance`, table `application_instances`, container `cloudbase-app-<id>-instance-<n>`); that is the one name for them, in the code, the API, the agent protocol and the panel.

An app is known by its **id** on disk, in Docker and in nginx: the source lives in `~/.cloudbase/apps/app-<id>`, the image is `cloudbase/app-<id>:latest`, the nginx config is `app-<id>` with the upstream `cloudbase_app_<id>` (`containers/naming.py`, `nginx/paths.py`). Its name is only what people read, and it decides the automatic subdomain `<name>.<base domain>`. Renaming an app (`PUT /api/apps/{id}` with `name`) therefore rewrites its nginx config for the new subdomain and removes the certificate of the old one, and leaves everything else alone.

## The frontend

Plain HTML pages with their own CSS and ES modules, no build step. `core/frontend_assets.py` makes switching between pages quick when a page leaves the server: `?v=<content hash>` on every css/js URL (cached for a year, see `routers/frontend.py`), an import map so `import './x.js'` hits the same cache, `modulepreload` for the whole module graph (one parallel round instead of a chain of 4-6), self-hosted fonts in `frontend/fonts/` (named after their content hash), and the page colour set inline. In the browser the sidebar lists and hidden menu items are remembered per tab (`sessionStorage`, `cb-index`, `cb-hidden-perms`) so they are there from the first frame, and `@view-transition` keeps the old page up until the new one can be drawn. `tests/test_frontend.py` checks all of it.

## How the primary drives a node

The primary is a node too, and its own instances work exactly the same way: starting, restarting, deploying, scaling and stopping only write the list. `nodes/local.py` runs the same reconciler as the agent (the engine in `containers/reconciler.py`) inside the panel, reading the list from the database and handing its report straight to `nodes/observed.py`; the source is already on the primary, so it builds from there and nothing is downloaded. There is no separate crash monitor or startup repair: a container that exits, or that is left over, is handled by the next pass.

Instances on another node are **not** started with commands. The primary keeps, per instance, whether it
should run and a *spec* (revision, port, environment, limits): `desired`, `spec_json`, `spec_hash` on the
instance row (`nodes/desired.py`). Starting, restarting or deploying writes a new spec (a new `spec_hash`);
stopping sets `desired = stopped`; deleting the row removes it from the list.

The node's agent (`agent/reconcile.py`, the HTTP side of the engine in `containers/reconciler.py`) asks for its list (`GET /api/nodes/agent/desired?have=<hash>`, also
when the primary nudges it over the websocket), compares it with the containers Docker really has, and repairs
the difference: start what is missing (downloading the source and building the image first), recreate what was
started with another spec, remove what is not on the list. It reports what it sees
(`POST /api/nodes/agent/observed`); `nodes/observed.py` turns that into the instance status, and
`nodes/promote.py` only lets an instance go live when the primary can reach it over the node network.

Safety rules: a node removes nothing before it has received the list in this run; a failing start is retried
with a growing delay and given up after a few attempts until the spec changes; a container Docker is restarting
by itself is reported, not fought.

What a node sends by itself, unasked (`agent/report.py`, stored by `nodes/reports.py`): the stats of its instances
every 5 s (with what its databases are doing before their container exists, such as an image pull) and the state and usage of its databases every 30 s. The primary samples its own instances at the same pace
(`workers/stats.py`), whether anybody looks or not. The stats collector and the database monitor read that instead of asking. Every machine also
cleans its own Docker disk (`containers/cleanup.py` `cleanup_forever`).

Databases across nodes use the node network, like instances do: a database publishes its port only on its node's
node-network address (`dbs/docker.publish_address`; the primary's is the hub address), and an app on another node gets
`<that address>:<published port>` as its URL (`dbs/links.py`, `dbs/proxy.resolve_links`). The hub forwards between
nodes (`scripts/cloudbase-wireguard`), so a node reaches another node's database through it. The primary's public
database ports (`dbs/proxy.py`, TLS and allowlist) connect the same way (`dbs/public.open_db_stream`). A container that
publishes on another address than the node's current one is replaced on its next start; the data volume stays.

What stays a command (`nodes/commands.py`, over the websocket, answered over HTTP): one-off things only — logs,
database operations and backups, cron runs, update checks and a Docker cleanup somebody starts by hand. Commands live in memory (`ask()` waits for the answer); none outlives the
panel or the agent process, so nothing is stored, and the node page shows the last 100 per node. Every command runs beside the connection, so a slow one never holds
up the others; a new agent process (new `session` in its hello) fails what the old one was doing.

The agent calls functions, not its own server: there is no local API call and no local agent token. It is also a process of its own (`python node_agent.py`, see `agent/runner.py`): a node runs no web server, no database and none of the panel's code, and shows that it lives by keeping `~/.cloudbase/agent.alive` fresh (`agent/alive.py`), which `start.sh`, `cloudbase doctor` and the installer read.
