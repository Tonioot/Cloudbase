from agent.state import AgentState



def _normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url.startswith("http"):
        url = "http://" + url
    return url


def _ws_url(main_url: str) -> str:
    url = _normalize_url(main_url)
    if url.startswith("https"):
        return url.replace("https://", "wss://") + "/api/nodes/ws/agent"
    return url.replace("http://", "ws://") + "/api/nodes/ws/agent"



# ─── Database connections across nodes ───────────────────────────────────────
# A separate control WebSocket, so database connections are never held up by a
# long command (e.g. an image pull) in the main command loop.

def _primary_ws_base(state: AgentState) -> str:
    url = _normalize_url(state.main_url)
    return url.replace("https://", "wss://") if url.startswith("https") else url.replace("http://", "ws://")
