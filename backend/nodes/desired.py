"""What the primary wants each node to run (the desired state), and the list a node asks for.

Instances on another node are not started with commands. The primary writes down, per instance,
whether it should run and how (a *spec*: image revision, port, environment, limits). The node's
agent asks for that list, compares it with what Docker really runs, and makes them equal
(containers/reconciler.py); it reports what it sees (nodes/observed.py). Because the list is state, not
a one-off command, nothing can get lost: a restart, a dropped connection or a crash in the middle
of a build simply means the node looks at the list again.

A spec is replaced only when the primary starts the instance again (start, restart, deploy), and
every replacement changes spec_hash, so the node recreates the container exactly then. Editing an
app's settings does not touch running instances until they are restarted.
"""
import asyncio
import hashlib
import json
import logging
import secrets

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import HTTPException

from core.env_crypto import encrypt_text
from core.models import Instance, Node
from nodes.state import _get_node_event

log = logging.getLogger("cloudbase.nodes")

NUDGE_DELAY_SECONDS = 0.3


def spec_hash(spec: dict) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True, default=str).encode()).hexdigest()[:16]


def list_hash(entries: list[dict]) -> str:
    text = "|".join(f"{e['instance_id']}:{e['desired']}:{e['spec_hash'] or ''}" for e in entries)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def nudge(node_id: int) -> None:
    """Tell the node's agent to look at its list now instead of at its next regular check."""
    asyncio.get_running_loop().call_later(NUDGE_DELAY_SECONDS, _get_node_event(node_id).set)


# The oldest agent this panel works with: 4.0 sends its stats by itself, the panel no longer asks for them
MIN_AGENT_MAJOR = 4


def agent_too_old(node: Node | None) -> bool:
    """True for an agent that would misread the list. An unknown version (not reported yet) is let through."""
    major = ((node.agent_version or "") if node else "").split(".")[0]
    return major.isdigit() and int(major) < MIN_AGENT_MAJOR


def _too_old(node: Node) -> HTTPException:
    return HTTPException(409, f"Node '{node.name}' runs an older Cloudbase ({node.agent_version}); run 'cloudbase update' on it first")


async def _require_current_agent(db: AsyncSession, node_id: int) -> None:
    node = await db.get(Node, node_id)
    if agent_too_old(node):
        raise _too_old(node)


async def start(db: AsyncSession, *, node_id: int, instance_id: int, payload: dict) -> str:
    """The instance should run with this payload (replaces any earlier spec, so a running one is recreated).

    The caller commits; the node is told once the list has changed."""
    await _require_current_agent(db, node_id)
    spec = {**payload, "instance_id": instance_id, "nonce": secrets.token_hex(4)}
    digest = spec_hash(spec)
    await db.execute(update(Instance).where(Instance.id == instance_id).values(
        desired="running", spec_json=encrypt_text(json.dumps(spec)), spec_hash=digest))
    nudge(node_id)
    return digest


async def stop(db: AsyncSession, *, node_id: int, instance_id: int) -> None:
    """The instance should not run. Its row stays (so it can be started again); the caller commits."""
    await db.execute(update(Instance).where(Instance.id == instance_id).values(desired="stopped"))
    nudge(node_id)
