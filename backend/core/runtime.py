import os
from core import paths, config as _cfg

PORT = _cfg.get_server_port()


# A node is managed from its primary's panel and serves no panel of its own.
# Becoming or leaving a node restarts the service, so checking once is enough.
IS_NODE = os.path.exists(paths.AGENT_STATE_FILE)


FRONTEND_DIR = paths.FRONTEND_DIR


_COOKIE_NAME = "cloudbase_token"


_COOKIE_OPTS = dict(httponly=True, samesite="strict", path="/")


def _read_version() -> str:
    try:
        with open(paths.VERSION_FILE) as f:
            return f.read().strip() or "dev"
    except OSError:
        return "dev"


VERSION = _read_version()
