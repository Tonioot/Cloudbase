import os
import json
from typing import Optional
from dataclasses import dataclass
from core import paths
from agent.log import _agent_log

# Configuration
STATE_PATH = paths.AGENT_STATE_FILE


@dataclass
class AgentState:
    main_url: str
    auth_token: str
    node_id: int
    node_name: str
    heartbeat_interval: int = 15
    wg_ip: str = ""            # this node's address on the node network, once enrolled


def _load_state() -> Optional[AgentState]:
    if not os.path.exists(STATE_PATH):
        return None
    try:
        with open(STATE_PATH, "r") as f:
            data = json.load(f)
        # Strip unknown fields so old state files with removed fields (e.g. app_id_map) still load
        known = {f for f in AgentState.__dataclass_fields__}
        data = {k: v for k, v in data.items() if k in known}
        return AgentState(**data)
    except Exception as e:
        _agent_log(f"[agent] Error loading state: {e}")
        return None


def is_node() -> bool:
    """True when this server is registered as a node of a primary."""
    return os.path.exists(STATE_PATH)


def _save_state(state: AgentState) -> None:
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(state.__dict__, f)
    except Exception as e:
        _agent_log(f"[agent] Error saving state: {e}")
