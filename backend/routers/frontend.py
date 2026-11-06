"""The panel's own pages: static assets, the HTML shell and the SPA fallback.

Registered last (see register) because the catch-all route must not shadow API routes.
"""
import os

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core import paths
from core.runtime import FRONTEND_DIR


def register(app: FastAPI) -> None:
    @app.middleware("http")
    async def _revalidate_frontend(request: Request, call_next):
        """Make browsers revalidate panel pages and assets (ETag → 304) instead of
        using cached copies after an update. Without it a fresh page could run an
        older cached script, e.g. a button without its click handler."""
        response = await call_next(request)
        path = request.url.path
        if not path.startswith(("/api/", "/ws/")):
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
        @app.get("/login.html", include_in_schema=False)
        async def login_page():
            return FileResponse(os.path.join(FRONTEND_DIR, "login.html"))

        @app.get("/app.html", include_in_schema=False)
        @app.get("/app", include_in_schema=False)
        async def app_page():
            return FileResponse(os.path.join(FRONTEND_DIR, "app.html"))

        @app.get("/node.html", include_in_schema=False)
        @app.get("/node", include_in_schema=False)
        async def node_page():
            return FileResponse(os.path.join(FRONTEND_DIR, "node.html"))

        @app.get("/database.html", include_in_schema=False)
        @app.get("/database", include_in_schema=False)
        async def database_page():
            return FileResponse(os.path.join(FRONTEND_DIR, "database.html"))

        @app.get("/audit.html", include_in_schema=False)
        @app.get("/audit", include_in_schema=False)
        async def audit_page():
            return FileResponse(os.path.join(FRONTEND_DIR, "audit.html"))

        @app.get("/notifications.html", include_in_schema=False)
        @app.get("/notifications", include_in_schema=False)
        async def notifications_page():
            return FileResponse(os.path.join(FRONTEND_DIR, "notifications.html"))

        @app.get("/settings.html", include_in_schema=False)
        @app.get("/settings", include_in_schema=False)
        async def settings_page():
            return FileResponse(os.path.join(FRONTEND_DIR, "settings.html"))

        @app.get("/guides.html", include_in_schema=False)
        @app.get("/guides", include_in_schema=False)
        async def guides_page():
            return FileResponse(os.path.join(FRONTEND_DIR, "guides.html"))

        @app.get("/", include_in_schema=False)
        async def index_page():
            return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))

        @app.get("/{full_path:path}", include_in_schema=False)
        async def catch_all(full_path: str):
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
            return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
