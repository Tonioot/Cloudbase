"""The node's side of the node network: join the primary's WireGuard hub and remember the address."""
import asyncio
import time
from urllib.parse import urlparse

import httpx

from agent.log import _agent_log
from agent.state import AgentState, _save_state
from mesh import addressing, helper

RETRY_SECONDS = 30
HANDSHAKE_WAIT_SECONDS = 15
WATCH_SECONDS = 60
# WireGuard shakes hands again every two minutes while the link lives (the keepalive keeps it busy). Nothing for this
# long: the hub is gone, or it is a new one (a primary restored from a backup has a new key) — join again.
LOST_AFTER_SECONDS = 300


def _with_port(host: str, port: int) -> str:
    if host.startswith("[") or host.count(":") == 1:
        return host                                   # already host:port or [v6]:port
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _candidates(state: AgentState, answer: dict) -> list[str]:
    """Where the primary may listen, best first: what the primary says, then the address of the panel."""
    hosts = list(answer.get("endpoints") or [])
    panel = urlparse(state.main_url).hostname
    if panel:
        hosts.append(panel)
    out: list[str] = []
    for host in hosts:
        endpoint = _with_port(host, int(answer["port"]))
        if endpoint not in out and addressing.is_valid_endpoint(endpoint):
            out.append(endpoint)
    return out


def _now() -> float:
    return time.time()


def _last_handshake(status: dict) -> int:
    return max((p.get("latest_handshake") or 0 for p in status.get("peers", [])), default=0)


async def _handshake_within(seconds: int) -> bool:
    """Whether the hub answered: WireGuard has shaken hands with it lately (not an old handshake from before)."""
    for _ in range(seconds):
        try:
            status = await asyncio.to_thread(helper.status)
        except helper.HelperError:
            return False
        if _now() - _last_handshake(status) < LOST_AFTER_SECONDS:
            return True
        await asyncio.sleep(1)
    return False


async def _until_lost() -> None:
    """Return once the hub has not answered for LOST_AFTER_SECONDS."""
    while True:
        await asyncio.sleep(WATCH_SECONDS)
        try:
            status = await asyncio.to_thread(helper.status)
        except helper.HelperError:
            continue                                  # the helper itself is in trouble: nothing to judge by
        if _now() - _last_handshake(status) >= LOST_AFTER_SECONDS:
            return


async def join_once(client: httpx.AsyncClient, state: AgentState) -> None:
    public_key = await asyncio.to_thread(helper.keygen)
    resp = await client.post(f"{state.main_url}/api/nodes/agent/wireguard", json={"public_key": public_key},
                             headers={"X-Node-Token": state.auth_token}, timeout=30)
    resp.raise_for_status()
    answer = resp.json()
    ip = answer["address"].split("/")[0]
    tried = _candidates(state, answer)
    for endpoint in tried:
        await asyncio.to_thread(helper.start_node, answer["address"], answer["hub_public_key"], endpoint, answer["allowed"])
        if state.wg_ip != ip:
            state.wg_ip = ip                           # the address exists now, whether or not the hub answers yet
            _save_state(state)
        if await _handshake_within(HANDSHAKE_WAIT_SECONDS):
            _agent_log(f"[mesh] joined the node network as {ip} (hub {endpoint})")
            return
        _agent_log(f"[mesh] no answer from {endpoint}")
    raise RuntimeError(
        f"the primary did not answer on UDP port {answer['port']} at {', '.join(tried) or 'any address'}. "
        f"Forward UDP port {answer['port']} to the primary (router/firewall)")


async def join_forever(client: httpx.AsyncClient, state: AgentState) -> None:
    """Join, keep trying until it works (the primary may be starting, or WireGuard not installed yet), and stay
    joined: when the hub stops answering, join again. The primary then hands out its current key and address, so a
    primary restored on a new server takes its nodes back by itself."""
    while True:
        try:
            await join_once(client, state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _agent_log(f"[mesh] could not join the node network ({type(exc).__name__}: {exc}); retrying in {RETRY_SECONDS}s")
            await asyncio.sleep(RETRY_SECONDS)
            continue
        await _until_lost()
        _agent_log(f"[mesh] the hub has not answered for {LOST_AFTER_SECONDS // 60} minutes; joining the node network again")
