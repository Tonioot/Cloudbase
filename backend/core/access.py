import re
from fastapi import Request
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from core.database import AsyncSessionLocal
from core import auth, system_config as _syscfg
from nginx import pages as nginx_pages
from agent import state as agent_state
from core.models import Node as _Node
from core.runtime import IS_NODE

# Auth middleware — blocks all /api/ and /ws/ except public paths
_PUBLIC = {
    "/api/health",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/check",
}


_NODE_AGENT_PATHS = re.compile(r"/api/apps/\d+/source-archive")


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

        # A node has no panel and no clients: its agent calls functions, not its own API.
        # Only health checks get through.
        if IS_NODE:
            if path == "/api/health":
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
        if path in _PUBLIC or path == "/api/status" or path.startswith("/api/webhooks/"):   # a webhook proves itself with its signature
            return await call_next(request)

        # WebSocket upgrade requests reach BaseHTTPMiddleware as HTTP GET requests.
        # All /ws/* endpoints authenticate themselves via auth.authorize_websocket(),
        # which accepts cookies and X-Node-Token. Pass them through.
        if path.startswith("/ws/"):
            return await call_next(request)

        if path.startswith("/api/") and path not in _PUBLIC:
            # Signed in? Looked up once and kept for the endpoint, whose own dependency decides the permission
            # (auth.require_permission; tests/test_security.py checks that every endpoint has one).
            if await auth.request_user(request):
                return await call_next(request)

            # Node agents (X-Node-Token) only reach the app endpoint they call;
            # it checks that the node runs the app.
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
