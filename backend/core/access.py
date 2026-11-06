import re
from fastapi import Request
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from core.database import AsyncSessionLocal
from core import auth, system_config as _syscfg
from nginx import pages as nginx_pages
from agent import state as agent_state
from core.models import Node as _Node
from core.runtime import IS_NODE, _COOKIE_NAME

# Auth middleware — blocks all /api/ and /ws/ except public paths
_PUBLIC = {
    "/api/health",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/check",
}


# Permission prefix maps — second line of defence (explicit Depends on each endpoint is primary).
# Order matters: more specific prefixes first.
# Write map: fallback permission required for any write to a prefix.
_PERMISSION_WRITE_MAP = (
    ("/api/apps",      "apps.configure"),  # safest fallback; granular checks on endpoints
    ("/api/nodes",     "nodes.configure"),
    ("/api/databases", "databases.manage"),
    ("/api/system",    "system.manage"),
    ("/api/users",     "users.manage"),
    ("/api/roles",     "roles.manage"),
    ("/api/audit-log", "audit.view"),
    ("/api/notifications", "notifications.manage"),
)


_PERMISSION_READ_MAP = (
    ("/api/apps",      "apps.view"),
    ("/api/nodes",     "nodes.view"),
    ("/api/databases", "databases.view"),
    ("/api/system",    "system.manage"),
    ("/api/users",     "users.manage"),
    ("/api/roles",     "roles.manage"),
    ("/api/audit-log", "audit.view"),
    ("/api/notifications", "notifications.view"),
)


# Per-path overrides — used when the prefix default doesn't fit the endpoint's semantics.
_PERMISSION_EXACT_OVERRIDES: dict[tuple[str, str], str] = {
    ("POST", "/api/nodes/invites"): "nodes.add",
    # /api/roles/permissions and /api/roles list are read by any authenticated user (UI needs it)
    ("GET",  "/api/roles"): None,
    ("GET",  "/api/roles/permissions"): None,
}


_PERMISSION_PREFIX_OVERRIDES: tuple[tuple[str, str, str | None], ...] = (
    # (METHOD, path prefix, required permission OR None to allow any authenticated user)
    ("GET",    "/api/roles/",                None),
    ("GET",    "/api/apps/system/certs",     "apps.view"),
    # GitHub token endpoints — under /api/system but use tokens.manage instead
    ("GET",    "/api/system/github-tokens",  "tokens.manage"),
    ("POST",   "/api/system/github-tokens",  "tokens.manage"),
    ("DELETE", "/api/system/github-tokens",  "tokens.manage"),
    # Apps write/delete: each endpoint has its own granular Depends() check.
    # Middleware only requires apps.view as a backstop so it never over-blocks.
    ("POST",   "/api/apps",                  "apps.view"),
    ("PUT",    "/api/apps/",                 "apps.view"),
    ("DELETE", "/api/apps/",                 "apps.view"),
    ("PATCH",  "/api/apps/",                 "apps.view"),
    # Nodes write/delete: same pattern
    ("POST",   "/api/nodes/",                "nodes.view"),
    ("PUT",    "/api/nodes/",                "nodes.view"),
    ("DELETE", "/api/nodes/",                "nodes.view"),
    ("PATCH",  "/api/nodes/",                "nodes.view"),
    # Databases: create / manage / delete are checked per endpoint
    ("POST",   "/api/databases",             "databases.view"),
    ("PATCH",  "/api/databases/",            "databases.view"),
    ("DELETE", "/api/databases/",            "databases.view"),
    # Notifications: marking the inbox read is part of viewing; per-app muting is app config
    ("POST",   "/api/notifications/read",    "notifications.view"),
    ("GET",    "/api/notifications/apps/",   "apps.view"),
    ("PUT",    "/api/notifications/apps/",   "apps.configure"),
    ("GET",    "/api/notifications/channels", "notifications.manage"),
    ("GET",    "/api/notifications/deliveries", "notifications.manage"),
    ("GET",    "/api/notifications/settings", "notifications.manage"),
)


# Sub-path → permission overrides for /api/apps/* endpoints based on URL segments.
# These run AFTER prefix matching and let us require fine-grained read perms
# (logs.view, stats.view) for specific app sub-resources.
def _resolve_apps_get_permission(path: str) -> str:
    """For GET /api/apps/* paths, return the required permission."""
    # /logs, /logs/tail, /replicas/*/logs → apps.view
    if "/logs" in path:
        return "apps.view"
    # /stats, /stats-remote, /aggregate-stats, /stats/history → apps.view
    if "/stats" in path or "stats-remote" in path:
        return "apps.view"
    # Everything else under /api/apps/ → apps.view
    return "apps.view"


def _resolve_required_permission(method: str, path: str) -> tuple[bool, str | None]:
    """
    Return (has_rule, required_permission).
      has_rule=False  → no permission rule applies to this path (allow if authenticated)
      has_rule=True, required_permission=None → explicitly allow any authenticated user
      has_rule=True, required_permission="X"  → require permission X
    """
    # 1. Exact overrides
    if (method, path) in _PERMISSION_EXACT_OVERRIDES:
        return True, _PERMISSION_EXACT_OVERRIDES[(method, path)]

    # 2. Prefix overrides
    for m, prefix, perm in _PERMISSION_PREFIX_OVERRIDES:
        if m == method and path.startswith(prefix) and perm is not None:
            return True, perm

    # 3. Method-based prefix maps
    is_write = method not in ("GET", "HEAD", "OPTIONS")
    table = _PERMISSION_WRITE_MAP if is_write else _PERMISSION_READ_MAP
    for prefix, perm in table:
        if path.startswith(prefix):
            # Special-case GET /api/apps/* — resolve to fine-grained perm
            if not is_write and prefix == "/api/apps":
                return True, _resolve_apps_get_permission(path)
            return True, perm

    return False, None


_NODE_AGENT_PATHS = re.compile(r"/api/apps/\d+/(source-archive|replicas/\d+/substatus)")


def _is_local_client(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def _reached_through_domain(request: Request, panel_domain: str) -> bool:
    """Already on the panel domain or HTTPS, e.g. through a Cloudflare Tunnel or
    another proxy that doesn't connect from localhost. Redirecting those would
    loop forever; only someone typing http://<ip>:<port> needs sending over."""
    host = (request.headers.get("host") or "").split(":")[0].lower()
    return host == panel_domain.lower() or request.headers.get("x-forwarded-proto") == "https"


def _is_node_traffic(request: Request, path: str) -> bool:
    if path == "/api/health" or path.startswith(("/api/nodes/agent/", "/api/nodes/ws/")):
        return True
    return bool(request.headers.get("X-Node-Token")) and bool(_NODE_AGENT_PATHS.fullmatch(path))


class _AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        method = request.method

        # On a node, the only clients are this server's own agent (X-Agent-Token,
        # also on its local log/stats WebSockets) and health checks.
        if IS_NODE:
            agent_token = request.headers.get("X-Agent-Token", "")
            if path == "/api/health" or (agent_token and auth.verify_agent_token(agent_token)):
                return await call_next(request)
            if path.startswith("/api/") or path.startswith("/ws/"):
                return JSONResponse({"detail": "This server is a Cloudbase node; use the primary's panel"}, status_code=403)
            state = agent_state._load_state()
            page = nginx_pages.generate_cloudbase_node_html(state and state.main_url, state and state.node_name)
            return HTMLResponse(page, status_code=403)

        # With an HTTPS panel domain, other machines reach the panel only through
        # nginx (which connects from localhost). Node agents keep using this port.
        # Over an SSH tunnel (ssh -L) the panel still opens here, from localhost.
        panel_domain = _syscfg.get_panel_https_domain()
        if (panel_domain and not _is_local_client(request) and not _is_node_traffic(request, path)
                and not _reached_through_domain(request, panel_domain)):
            if method in ("GET", "HEAD") and not path.startswith("/api/"):
                query = f"?{request.url.query}" if request.url.query else ""
                return RedirectResponse(f"https://{panel_domain}{path}{query}", status_code=302)
            return JSONResponse({"detail": f"Use the panel at https://{panel_domain}"}, status_code=403)

        # A browser sends Origin on every cross-origin write. Refuse writes from any
        # other site (including apps on a sibling subdomain, which count as
        # same-site and so do get the SameSite=Strict cookie).
        if method not in ("GET", "HEAD", "OPTIONS") and path.startswith("/api/"):
            if not auth.origin_allowed(request.headers.get("origin"), request.headers):
                return JSONResponse({"detail": "Cross-origin request refused"}, status_code=403)

        # Node-agent endpoints use X-Node-Token / hello message auth — bypass cookie auth
        if path.startswith("/api/nodes/agent/") or path.startswith("/api/nodes/ws/agent"):
            return await call_next(request)
        if path in _PUBLIC:
            return await call_next(request)

        # WebSocket upgrade requests reach BaseHTTPMiddleware as HTTP GET requests.
        # All /ws/* endpoints authenticate themselves via auth.authorize_websocket(),
        # which now accepts cookies, X-Agent-Token, and X-Node-Token. Pass them through.
        if path.startswith("/ws/"):
            return await call_next(request)

        if path.startswith("/api/") and path not in _PUBLIC:
            token = request.cookies.get(_COOKIE_NAME)
            if token:
                user = await auth.session_user(token)
                if user:
                    # Root (built-in admin) bypasses permission checks entirely
                    if user["username"] == "admin":
                        return await call_next(request)
                    has_rule, required_perm = _resolve_required_permission(method, path)
                    if has_rule and required_perm is not None:
                        perms = await auth.get_user_permissions(user["username"])
                        if required_perm not in perms:
                            return JSONResponse({"detail": "Permission denied"}, status_code=403)
                    return await call_next(request)

            # Allow node_agent.py running locally via X-Agent-Token header
            agent_token = request.headers.get("X-Agent-Token", "")
            if agent_token and auth.verify_agent_token(agent_token):
                return await call_next(request)
            # Remote node agents (X-Node-Token) only reach the few app endpoints
            # they call; those check that the node runs the app.
            node_token = request.headers.get("X-Node-Token", "")
            if node_token and _NODE_AGENT_PATHS.fullmatch(path):
                from sqlalchemy import select as _select, and_ as _and
                async with AsyncSessionLocal() as _db:
                    _res = await _db.execute(
                        _select(_Node).where(_and(_Node.auth_token == node_token, _Node.enabled == True))
                    )
                    if _res.scalar_one_or_none():
                        return await call_next(request)
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        return await call_next(request)
