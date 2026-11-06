"""The node's side of the node network: join the primary's WireGuard hub and remember the address."""
import asyncio
from urllib.parse import urlparse

import httpx

from agent.log import _agent_log
from agent.state import AgentState, _save_state
from mesh import addressing, helper

RETRY_SECONDS = 30


def _endpoint(state: AgentState, answer: dict) -> str:
    """Where the primary listens: the configured endpoint, or the host the agent already talks to."""
    endpoint = answer.get("endpoint") or ""
    if endpoint:
        return endpoint if ":" in endpoint else f"{endpoint}:{answer['port']}"
    host = urlparse(state.main_url).hostname or ""
    return f"[{host}]:{answer['port']}" if ":" in host else f"{host}:{answer['port']}"


async def join_once(client: httpx.AsyncClient, state: AgentState) -> None:
    public_key = await asyncio.to_thread(helper.keygen)
    resp = await client.post(f"{state.main_url}/api/nodes/agent/wireguard", json={"public_key": public_key},
                             headers={"X-Node-Token": state.auth_token}, timeout=30)
    resp.raise_for_status()
    answer = resp.json()
    endpoint = _endpoint(state, answer)
    if not addressing.is_valid_endpoint(endpoint):
        raise RuntimeError(f"the primary gave an unusable endpoint: {endpoint}")
    await asyncio.to_thread(helper.start_node, answer["address"], answer["hub_public_key"], endpoint, answer["allowed"])
    ip = answer["address"].split("/")[0]
    if state.wg_ip != ip:
        state.wg_ip = ip
        _save_state(state)
    _agent_log(f"[mesh] joined the node network as {ip} (hub {endpoint})")


async def join_forever(client: httpx.AsyncClient, state: AgentState) -> None:
    """Join, and keep trying until it works (the primary may be starting, or WireGuard not installed yet)."""
    while True:
        try:
            await join_once(client, state)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _agent_log(f"[mesh] could not join the node network ({type(exc).__name__}: {exc}); retrying in {RETRY_SECONDS}s")
            await asyncio.sleep(RETRY_SECONDS)
