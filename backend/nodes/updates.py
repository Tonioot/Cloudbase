"""The "Update Cloudbase" button of a node: the primary updates itself, a node is asked through its agent."""
import asyncio

from core import selfupdate
from core.models import Node
from nodes import commands as node_commands
from nodes.state import _node_ws_connections

_OLD_AGENT = ("This node runs a Cloudbase that can't update itself yet. Run once on that server: cloudbase update")


async def update_node(node: Node) -> dict:
    """Returns {"status": "updated" | "installing" | "current" | "needs_system" | "error" | "skipped", "message": ...}."""
    if node.is_local:
        result = await asyncio.to_thread(selfupdate.update)
        if result["status"] == "updated":
            selfupdate.restart_soon(graceful=True)   # the panel stops after this answer is sent
        return result
    if node.id not in _node_ws_connections:
        return {"status": "skipped", "message": "The node is not connected"}
    done = await node_commands.ask(node.id, "self_update", {}, timeout=300)
    if done.status != "done":
        message = done.error_message or "The update failed"
        return {"status": "error", "message": _OLD_AGENT if "Unsupported command" in message else message}
    return done.result or {"status": "error", "message": "The node gave no answer"}
