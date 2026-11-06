import asyncio
import json

def _sse_line(data: str) -> str:
    """Format a single SSE data line."""
    return f"data: {json.dumps(data)}\n\n"


_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


# Reverse proxies (nginx default proxy_read_timeout = 60s, Cloudflare ~100s) drop
# idle connections. Build steps like `npm install` can be silent for minutes, so
# send an SSE comment as keep-alive whenever no log line arrived for this long.
_SSE_HEARTBEAT_SECONDS = 15


async def _queue_get_or_heartbeat(queue: asyncio.Queue):
    """Return the next queue item, or the sentinel _SSE_PING after a quiet period."""
    try:
        return await asyncio.wait_for(queue.get(), timeout=_SSE_HEARTBEAT_SECONDS)
    except asyncio.TimeoutError:
        return _SSE_PING


_SSE_PING = object()


_DEPLOY_STREAM_DONE = object()
