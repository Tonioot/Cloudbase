import time
import asyncio
from typing import Optional
import httpx
from containers import client as containers_client
from agent.log import _agent_log
from agent.state import AgentState

# ─── Orphan cleanup ───────────────────────────────────────────────────────────

_ORPHAN_MIN_AGE_SECONDS = 120


# Replica IDs found orphaned on the previous check; removed only if still orphaned now.
_orphan_suspects: set[int] = set()


def _parse_docker_created(value: str) -> Optional[float]:
    """Parse the Docker API 'Created' value ('2026-10-02T18:43:30.123456789Z') to a timestamp."""
    from datetime import datetime, timezone
    try:
        base = value.strip().rstrip("Z").split(".")[0]
        return datetime.strptime(base, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


def _list_replica_containers() -> list[tuple[str, Optional[float]]]:
    client = containers_client._get_client()
    out = []
    for c in client.containers.list(all=True, filters={"name": "cloudbase-app-"}):
        out.append((c.name, _parse_docker_created(c.attrs.get("Created") or "")))
    return out


def _remove_container(name: str) -> None:
    containers_client._get_client().containers.get(name).remove(force=True)


async def _cleanup_orphaned_replica_containers(client: httpx.AsyncClient, state: AgentState) -> None:
    """Stop any local Docker containers whose replica_id no longer exists on the primary.

    Container names follow the pattern  cloudbase-app-{app_id}-replica-{replica_id}.
    We ask the primary for the set of live replica IDs on this node, then kill every
    matching container whose replica_id is not in that set.
    """
    import re
    node_headers = {"X-Node-Token": state.auth_token}
    try:
        resp = await client.get(
            f"{state.main_url}/api/nodes/agent/my-replicas",
            headers=node_headers, timeout=10,
        )
        if resp.status_code != 200:
            _agent_log(f"[cleanup] my-replicas returned HTTP {resp.status_code}, skipping orphan check")
            return
        live_ids: set[int] = {r["id"] for r in resp.json().get("replicas", [])}

        # Enumerate local replica containers through the Docker API (same access
        # the node uses to start them — the docker CLI may not be on the PATH
        # of the service, which made this check silently do nothing).
        containers = await asyncio.to_thread(_list_replica_containers)

        pattern = re.compile(r"^cloudbase-app-(\d+)-replica-(\d+)$")
        seen_missing: set[int] = set()
        now = time.time()
        for cname, created in containers:
            m = pattern.match(cname)
            if not m:
                continue
            replica_id = int(m.group(2))
            if replica_id in live_ids:
                continue
            # A restart briefly reports the replica as stopped on the primary;
            # never touch a container that was just created.
            if created is not None and now - created < _ORPHAN_MIN_AGE_SECONDS:
                continue
            seen_missing.add(replica_id)
            # Only remove after two consecutive checks agree it is orphaned.
            if replica_id in _orphan_suspects:
                _agent_log(f"[cleanup] Removing orphan replica container '{cname}' (replica_id={replica_id} is stopped or no longer exists on the primary)")
                try:
                    await asyncio.to_thread(_remove_container, cname)
                except Exception as e:
                    _agent_log(f"[cleanup] Failed to remove '{cname}': {e}")
            else:
                _agent_log(f"[cleanup] Replica container '{cname}' not known to primary — will remove if still unknown next check")
        _orphan_suspects.clear()
        _orphan_suspects.update(seen_missing)
    except Exception as e:
        _agent_log(f"[cleanup] Replica container orphan check failed: {e}")
