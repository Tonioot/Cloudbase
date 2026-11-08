"""The node's reconciler: the engine of containers/reconciler.py, asking the primary over HTTP.

A node asks for its list (`GET /agent/desired`), reports what it sees (`POST /agent/observed`), publishes the
ports of its instances on its node-network address and downloads the source of an app from the primary before
it builds the image. The primary runs the same engine on its own database (nodes/local.py).
"""
import httpx

from agent.source import ensure_image
from agent.state import AgentState
from containers.reconciler import Reconciler as Engine


class Reconciler(Engine):
    def __init__(self, client: httpx.AsyncClient, state: AgentState):
        super().__init__()
        self.client, self.state = client, state

    async def _fetch_list(self) -> dict:
        resp = await self.client.get(
            f"{self.state.main_url}/api/nodes/agent/desired", params={"have": self.list_hash},
            headers={"X-Node-Token": self.state.auth_token}, timeout=30)
        resp.raise_for_status()
        return resp.json()

    async def _send_report(self, body: dict) -> None:
        resp = await self.client.post(
            f"{self.state.main_url}/api/nodes/agent/observed", json={"replicas": body},
            headers={"X-Node-Token": self.state.auth_token}, timeout=30)
        resp.raise_for_status()

    def _network_ready(self) -> bool:
        return bool(self.state.wg_ip)

    def _bind_host(self) -> str | None:
        return self.state.wg_ip

    async def _prepare(self, spec: dict, progress) -> None:
        await ensure_image(self.client, self.state, spec, progress)
