"""What instance containers share: the restart policy and the host port range."""
from core import config as _cfg

def _restart_policy_config(policy: str | None) -> dict:
    policy = (policy or "no").strip().lower()
    if policy == "always":
        return {"Name": "always"}
    if policy == "unless-stopped":
        return {"Name": "unless-stopped"}
    if policy == "on-failure":
        return {"Name": "on-failure", "MaximumRetryCount": 5}
    return {"Name": "no"}


# ── Port allocation ───────────────────────────────────────────────────────────

EXTERNAL_PORT_START = _cfg.get_port("instance_min")


EXTERNAL_PORT_END   = _cfg.get_port("instance_max")


def pick_free_external_port(used_ports: set[int]) -> int:
    """Return the lowest unused host port in the configured instance range that is also free on the OS."""
    import psutil
    try:
        active = {c.laddr.port for c in psutil.net_connections(kind="inet") if c.laddr}
    except Exception:
        active = set()

    for p in range(EXTERNAL_PORT_START, EXTERNAL_PORT_END + 1):
        if p not in used_ports and p not in active:
            return p
    raise RuntimeError(
        f"No free external port available in range {EXTERNAL_PORT_START}–{EXTERNAL_PORT_END}"
    )
