"""The panel's own pages: static assets, the HTML shell and the SPA fallback.

Registered last (see register) because the catch-all route must not shadow API routes.
How the pages are made quick to switch between is explained in core/frontend_assets.py.
"""
import hashlib
import os

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from core import frontend_assets, paths
from core.runtime import FRONTEND_DIR

IMMUTABLE = "public, max-age=31536000, immutable"


def _page(request: Request, name: str) -> Response:
    """A panel page with versioned asset URLs; revalidated cheaply through its ETag."""
    body = frontend_assets.render_page(FRONTEND_DIR, name)
    etag = 'W/"' + hashlib.sha1(body.encode("utf-8")).hexdigest()[:20] + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return HTMLResponse(body, headers={"ETag": etag})


def _cache_policy(path: str, query_params) -> str | None:
    """Assets whose URL changes when their content does (?v=, or a hash in the file name) are cached
    for good; everything else is revalidated, so an update is never hidden behind an old copy."""
    if path.startswith("/fonts/"):
        return IMMUTABLE
    if path.startswith(("/js/", "/css/")) and "v" in query_params:
        return IMMUTABLE
    return None


def register(app: FastAPI) -> None:
    @app.middleware("http")
    async def _revalidate_frontend(request: Request, call_next):
        """Versioned assets are cached for a year; pages and unversioned files are revalidated
        (ETag → 304) so that after an update a fresh page never runs an older cached script,
        e.g. a button without its click handler."""
        response = await call_next(request)
        path = request.url.path
        policy = _cache_policy(path, request.query_params)
        if policy:
            response.headers["Cache-Control"] = policy
        elif not path.startswith(("/api/", "/ws/")):
            response.headers.setdefault("Cache-Control", "no-cache")
        # The panel may only be framed by itself (visitor page previews), never by
        # another site (clickjacking); responses are never sniffed as another type.
        response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'self'; base-uri 'self'; object-src 'none'")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    if os.path.isdir(FRONTEND_DIR):
        app.mount("/css", StaticFiles(directory=os.path.join(FRONTEND_DIR, "css")), name="css")
        app.mount("/js",  StaticFiles(directory=os.path.join(FRONTEND_DIR, "js")),  name="js")
        if os.path.isdir(os.path.join(FRONTEND_DIR, "fonts")):
            app.mount("/fonts", StaticFiles(directory=os.path.join(FRONTEND_DIR, "fonts")), name="fonts")

        @app.get("/favicon.png", include_in_schema=False)
        async def favicon():
            return FileResponse(os.path.join(FRONTEND_DIR, "cloudbase.png"), media_type="image/svg+xml")

        @app.get("/assets/cloudbase-mark.png", include_in_schema=False)
        async def logo_mark():
            # Small public logo: chat apps (Slack, Teams, Discord, ntfy) show it next to notifications
            return FileResponse(paths.LOGO_FILE,
                                media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})

        @app.get("/cloudbase.png", include_in_schema=False)
        async def logo():
            return FileResponse(os.path.join(FRONTEND_DIR, "cloudbase.png"), media_type="image/png")

        @app.get("/login", include_in_schema=False)
        async def login_page(request: Request):
            return _page(request, "login.html")

        @app.get("/app", include_in_schema=False)
        async def app_page(request: Request):
            return _page(request, "app.html")

        @app.get("/node", include_in_schema=False)
        async def node_page(request: Request):
            return _page(request, "node.html")

        @app.get("/database", include_in_schema=False)
        async def database_page(request: Request):
            return _page(request, "database.html")

        @app.get("/audit", include_in_schema=False)
        async def audit_page(request: Request):
            return _page(request, "audit.html")

        @app.get("/notifications", include_in_schema=False)
        async def notifications_page(request: Request):
            return _page(request, "notifications.html")

        @app.get("/settings", include_in_schema=False)
        async def settings_page(request: Request):
            return _page(request, "settings.html")

        @app.get("/guides", include_in_schema=False)
        async def guides_page(request: Request):
            return _page(request, "guides.html")

        @app.get("/", include_in_schema=False)
        async def index_page(request: Request):
            return _page(request, "index.html")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def catch_all(request: Request, full_path: str):
            # Never catch API or WebSocket paths
            if full_path.startswith("api/") or full_path.startswith("ws/"):
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            # Paths with a file extension that aren't known static assets → 404
            # (prevents hosting panel paths like /cgi-bin/, /wp-admin/, etc. from redirecting)
            _, ext = os.path.splitext(full_path)
            if ext and ext not in {".html", ".htm"}:
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            # Known SPA routes — serve the shell
            _SPA_PREFIXES = ("", "app", "node", "login", "settings", "nodes")
            root = full_path.split("/")[0]
            if root not in _SPA_PREFIXES:
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            return _page(request, "index.html")
