"""The "Update Cloudbase" button of a node: the primary updates itself, a node is asked through its agent."""
import asyncio

from core import power, selfupdate
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


async def update_all(nodes: list[Node]) -> dict:
    """The "Update all" button: every connected node first, side by side, and only then the primary itself, which
    restarts the panel after the answer is sent. Returns {"nodes": [{node_id, name, status, message, ...}], "primary": ...}.

    The primary goes last so it is never newer than the agents it talks to for longer than their own update takes, and
    so the answer about the nodes still reaches the panel before it restarts. It is updated also when a node failed:
    that node's answer says why, and its own page can try it again."""
    remote = [n for n in nodes if not n.is_local and n.enabled]
    results = await asyncio.gather(*(update_node(n) for n in remote), return_exceptions=True)
    out = []
    for node, result in zip(remote, results):
        if isinstance(result, BaseException):
            result = {"status": "error", "message": str(getattr(result, "detail", None) or result) or "The update failed"}
        out.append({"node_id": node.id, "name": node.name, **result})
    local = next((n for n in nodes if n.is_local), None)
    primary = await update_node(local) if local else {"status": "skipped", "message": "This server is not a primary"}
    return {"nodes": out, "primary": primary}


async def restart_node(node: Node) -> dict:
    """Restart the server of a node ("Restart node"). Returns {"status": "restarting" | "error" | "skipped", "message": ...}.

    The primary restarts itself: the panel is gone for a minute and everything on it comes back with the machine."""
    if node.is_local:
        return await asyncio.to_thread(power.reboot)
    if node.id not in _node_ws_connections:
        return {"status": "skipped", "message": "The node is not connected"}
    done = await node_commands.ask(node.id, "reboot", {}, timeout=60)
    if done.status != "done":
        message = done.error_message or "The restart failed"
        return {"status": "error", "message": "This node runs a Cloudbase that can't restart the server yet. Run once on that server: cloudbase update" if "Unsupported command" in message else message}
    return done.result or {"status": "error", "message": "The node gave no answer"}
