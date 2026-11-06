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
| `core/` | `config`, `database` + `models`, `auth` (+ `access`: permission map and middleware), `env_crypto`, `token_vault`, `system_config`, `applog`/`system_logs`, `paths` (every file location), `runtime` (port, `IS_NODE`), `history` |
| `apps/` | application logic: `git`, `images`, `validation`, `nginx_sync`, `runtime` (starting/stopping replicas), `replica_stats`, `deployments` (run a deploy), `deploy_common`/`rolling`/`bluegreen`, `lifecycle`, `serializers`, `state` |
| `nodes/` | `state` (live connections), `commands` (the command queue), `registry` (local node, lookups), `recovery`, `promote` (an instance goes live once reachable; takes instances out when the link breaks) |
| `mesh/` | the node network (WireGuard, hub and spoke with the primary as hub): `addressing`, `helper` (client for the root helper `scripts/cloudbase-wireguard`, via sudo), `hub` (interface, peers, enrolment, handshake health), `routing` (`backend_for`, `wait_reachable`) |
| `containers/` | Docker on one host: `client`, `naming`, `buildkit`, `dockerfiles`, `build`, `cleanup`, `apps`, `replicas`, `jobs`, `stats_logs`, `processes` |
| `nginx/` | `paths`, `pages` (HTML), `config` (nginx config text), `files` (writers), `certbot` (read-only lookups), `certificates` (issuing), `panel` |
| `dbs/` | managed databases: `docker`, `lifecycle`, `backup_restore`, `updates`, `ops`, `provision`, `view`, `relay`, `proxy`, `workers`, `backups`, `backup_scheduler`, … |
| `transfer/` | export and import of apps and databases as one file (a tar): `archive` (manifest + dumps), `manifest`/`databases` (what goes in), `jobs`/`exporter` (export runs in the background), `importer`/`db_import` (what comes out) |
| `notify/` | `events`, `settings`, `config`, `emit`, `dispatch`, `watch`, `channels`, `mailer` |
| `routers/` | one package per area (`applications/`, `nodes/`, `databases/`, `database_backups/`), each with a module per concern; the package `__init__` composes them in order |

## Rules

1. **Routers and workers stay thin.** Logic that more than one caller needs goes in a service. A router never imports another router area; a service never imports a router.
2. **No imports inside functions to dodge cycles.** If two modules need each other, move the shared part into a third module below them.
3. **State has one owner.** A module-level dict or flag is defined in one module, and only that module rebinds it. Others read it through a function (an imported name would go stale when it is rebound).
4. **Files stay under ~500 lines, functions under ~100.** Split by concern, not by size.
5. **Route order matters.** FastAPI uses the first matching route. Include order is set in `main.py` and in each `routers/<area>/__init__.py`.

`tests/test_app_structure.py` enforces rules 1, 2 and 5 (and that every route still exists); rules 3 and 4 are by review.

## Working on it

```
pip install -r requirements-dev.txt
pytest                                # unit tests, route snapshot, references, and a smoke test that boots the app
ruff check backend tests scripts
```

- Moved or renamed a module? `tests/test_references.py` checks every `from x import y` and `module.attr`; `tests/test_scripts.py` checks the Python snippets inside `start.sh` / `install.sh`.
- Added or removed an endpoint on purpose? Regenerate the snapshot: `UPDATE_SNAPSHOTS=1 pytest tests/test_app_structure.py`, and review the diff of `tests/routes_snapshot.txt`.
- Tests run against a throwaway home directory (`tests/conftest.py`); they never touch `~/.cloudbase`.

## The frontend

Plain HTML pages with their own CSS and ES modules, no build step. `core/frontend_assets.py` makes switching between pages quick when a page leaves the server: `?v=<content hash>` on every css/js URL (cached for a year, see `routers/frontend.py`), an import map so `import './x.js'` hits the same cache, `modulepreload` for the whole module graph (one parallel round instead of a chain of 4-6), self-hosted fonts in `frontend/fonts/` (named after their content hash), and the page colour set inline. In the browser the sidebar lists and hidden menu items are remembered per tab (`sessionStorage`, `cb-index`, `cb-hidden-perms`) so they are there from the first frame, and `@view-transition` keeps the old page up until the new one can be drawn. `tests/test_frontend.py` checks all of it.
