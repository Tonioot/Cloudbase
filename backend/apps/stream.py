import asyncio
import json
import logging
import secrets
from core.models import Node
from nodes.state import _node_ws_connections, unsubscribe_node_stream, subscribe_node_stream

log = logging.getLogger("cloudbase.stats")


def _aggregate_frames(frames: list[dict]) -> dict:
    """Merge stats from multiple nodes/instances into one aggregated frame.

    CPU → average across nodes (each node already reports % of its own cores).
    Memory / network / disk → sum (additive resources).
    """
    n = len(frames)
    if n == 0:
        return {"status": "stopped"}
    if n == 1:
        return frames[0]

    def _sum(key):
        return round(sum(f.get(key) or 0 for f in frames), 2)

    def _avg(key):
        vals = [f.get(key) for f in frames if f.get(key) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    def _max(key):
        vals = [f.get(key) for f in frames if f.get(key) is not None]
        return max(vals) if vals else None

    agg = {
        "status":       "running",
        "docker":       any(f.get("docker") for f in frames),
        "timestamp":    frames[-1].get("timestamp"),
        "cpu_percent":  _avg("cpu_percent"),
        "memory_mb":    _sum("memory_mb"),
        "memory_vms_mb": _sum("memory_vms_mb"),
        "net_rx_mb":    _sum("net_rx_mb"),
        "net_tx_mb":    _sum("net_tx_mb"),
        "disk_read_mb": _sum("disk_read_mb"),
        "disk_write_mb": _sum("disk_write_mb"),
        "uptime_seconds": _max("uptime_seconds"),
        "system_cpu_percent": _avg("system_cpu_percent"),
        # Aggregation metadata consumed by the frontend
        "_instance_count": n,
        "_aggregated": True,
    }
    return {k: v for k, v in agg.items() if v is not None}


async def _open_remote_stream(node: Node, app_id: int, app_name: str, out_q: asyncio.Queue) -> asyncio.Task:
    """Start a background task that reads stats from one remote node and puts frames on out_q."""

    async def _run():
        while True:
            try:
                agent_ws = _node_ws_connections.get(node.id)
                if agent_ws is None:                 # not connected (yet): there is no other way to ask it
                    await asyncio.sleep(2)
                    continue
                stream_id = secrets.token_hex(8)
                q = subscribe_node_stream(stream_id)
                try:
                    await agent_ws.send_json({
                        "type": "command",
                        "command": {
                            "id": -1,
                            "command_type": "stream_stats",
                            "payload": {"app_id": app_id, "app_name": app_name, "stream_id": stream_id},
                        },
                    })
                    while True:
                        try:
                            data = await asyncio.wait_for(q.get(), timeout=30.0)
                        except asyncio.TimeoutError:
                            # If the same node websocket is still active, keep the
                            # stream open instead of thrashing reconnects with 0 frames.
                            if _node_ws_connections.get(node.id) is agent_ws:
                                continue
                            # WS changed/dropped; break so outer loop reconnects.
                            break
                        parsed = json.loads(data) if isinstance(data, str) else data
                        await out_q.put((node.id, parsed))
                except asyncio.TimeoutError:
                    pass
                except Exception as e:
                    log.debug("remote stream node %d ended: %s", node.id, e)
                finally:
                    unsubscribe_node_stream(stream_id, q)
                    try:
                        if _node_ws_connections.get(node.id) is agent_ws:
                            await agent_ws.send_json({"type": "cancel_stream", "stream_id": stream_id})
                    except Exception:
                        pass
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.debug("remote stats loop node %d error: %s", node.id, e)
                await asyncio.sleep(2)

    return asyncio.create_task(_run())
