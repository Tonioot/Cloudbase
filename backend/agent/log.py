import os
import logging.handlers

AGENT_LOG_FILE = os.path.expanduser("~/.cloudbase/logs/node-agent.log")


os.makedirs(os.path.dirname(AGENT_LOG_FILE), exist_ok=True)


# Own logger with its own file, so the panel can show agent activity on its own.
# It also propagates to the root logger (server.log / stdout) set up by main.py;
# configuring the root logger here would hijack the whole server's logging.
_log = logging.getLogger("cloudbase.agent")


_log.setLevel(logging.INFO)


if not _log.handlers:
    _agent_file_handler = logging.handlers.RotatingFileHandler(
        AGENT_LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    _agent_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    _log.addHandler(_agent_file_handler)


def _agent_log(message: str) -> None:
    _log.info(message)
