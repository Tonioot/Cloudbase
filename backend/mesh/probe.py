"""How fast the node network itself is: one ICMP echo from the primary to a node's address on cbwg0.

The agent's ping travels over the websocket to the panel address (and through nginx when the panel has a domain);
this one only crosses WireGuard, so it shows the link the instances and databases actually use."""
import asyncio
import re
from typing import Optional

TIMEOUT_SECONDS = 2
_TIME = re.compile(rb"time[=<]\s*([\d.]+)\s*ms")


def parse_rtt(output: bytes) -> Optional[float]:
    """The round trip in ms from the output of `ping`, or None when no reply came."""
    match = _TIME.search(output or b"")
    return float(match.group(1)) if match else None


async def rtt_ms(address: str) -> Optional[float]:
    """Round trip to `address` in ms, or None (no reply, no ping command, or not Linux)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-n", "-c", "1", "-W", str(TIMEOUT_SECONDS), address,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except OSError:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), TIMEOUT_SECONDS + 2)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    return parse_rtt(out) if proc.returncode == 0 else None
