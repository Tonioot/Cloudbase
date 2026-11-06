import asyncio
import logging
from fastapi import WebSocket
from collections import deque

log = logging.getLogger("cloudbase.nodes")


# Connected node-agent websockets: node_id -> websocket
_node_ws_connections: dict[int, WebSocket] = {}


# Per-node event set when a new command is queued.
_node_command_events: dict[int, asyncio.Event] = {}


# Per-command event set when a result arrives.
_command_done_events: dict[int, asyncio.Event] = {}


# --- Streaming infrastructure ---
# stream_id -> list of queues fed by agent stream_data frames
_node_stream_queues: dict[str, list[asyncio.Queue]] = {}


# node_id -> list of queues for browser-facing event subscribers
_node_event_subscribers: dict[int, list[asyncio.Queue]] = {}


# ping_id -> asyncio.Future awaiting pong from agent
_pending_pings: dict[str, asyncio.Future] = {}


_node_stats_history: dict[int, deque] = {}


def subscribe_node_stream(stream_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=2000)
    _node_stream_queues.setdefault(stream_id, []).append(q)
    return q


def unsubscribe_node_stream(stream_id: str, q: asyncio.Queue) -> None:
    lst = _node_stream_queues.get(stream_id, [])
    try:
        lst.remove(q)
    except ValueError:
        pass
    if not lst:
        _node_stream_queues.pop(stream_id, None)


def push_node_stream_data(stream_id: str, data: str) -> None:
    listeners = _node_stream_queues.get(stream_id, [])
    if not listeners:
        log.debug("stream_data received for stream_id=%s but no listeners", stream_id)
    for q in listeners:
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            log.warning("stream queue full for stream_id=%s, dropping frame", stream_id)


def _record_node_stats(node_id: int, raw: str) -> None:
    """Buffer the last 60 stats snapshots for a node (used to replay history on WS connect)."""
    _node_stats_history.setdefault(node_id, deque(maxlen=60)).append(raw)


def _subscribe_node_events(node_id: int) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    _node_event_subscribers.setdefault(node_id, []).append(q)
    return q


def _unsubscribe_node_events(node_id: int, q: asyncio.Queue) -> None:
    lst = _node_event_subscribers.get(node_id, [])
    try:
        lst.remove(q)
    except ValueError:
        pass
    if not lst:
        _node_event_subscribers.pop(node_id, None)


def _push_node_event(node_id: int, event: dict) -> None:
    for q in _node_event_subscribers.get(node_id, []):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


def _get_node_event(node_id: int) -> asyncio.Event:
    ev = _node_command_events.get(node_id)
    if ev is None:
        ev = asyncio.Event()
        _node_command_events[node_id] = ev
    return ev


def _get_command_event(command_id: int) -> asyncio.Event:
    ev = _command_done_events.get(command_id)
    if ev is None:
        ev = asyncio.Event()
        _command_done_events[command_id] = ev
    return ev
