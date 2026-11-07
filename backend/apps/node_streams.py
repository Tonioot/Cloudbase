"""Live streams about the containers and the machine this process runs on.

The panel shows them for the primary itself; a node's agent sends the same frames to the primary
(agent/dispatch.py). Each function runs until it is cancelled and hands every frame to `send`.
"""
import asyncio
import threading
import time
from collections import deque
from typing import Awaitable, Callable

import psutil

from containers import client as containers_client, stats_logs as containers_stats_logs

_system_history: deque = deque(maxlen=60)


async def follow_container_logs(app_id: int, send: Callable[[str], Awaitable[None]]) -> None:
    """Every replica container of app_id on this machine: recent lines first, then live.

    Looks again every few seconds, so instances started later are followed too."""
    loop = asyncio.get_running_loop()
    lines: asyncio.Queue = asyncio.Queue()
    followed: set[str] = set()
    streams: list = []
    prefix = f"cloudbase-app-{app_id}-replica-"

    def reader(name: str, tail: int) -> None:
        try:
            stream = containers_client._get_client().containers.get(name).logs(stream=True, follow=True, timestamps=False, tail=tail)
            streams.append(stream)
            for raw in stream:
                loop.call_soon_threadsafe(lines.put_nowait, raw.decode("utf-8", errors="replace").rstrip())
        except Exception:
            pass
        finally:
            loop.call_soon_threadsafe(followed.discard, name)        # allow re-attach after a restart

    def containers() -> list[str]:
        try:
            return [c.name for c in containers_client._get_client().containers.list(filters={"name": prefix})
                    if c.name.startswith(prefix)]
        except Exception:
            return []

    async def scan(first: bool) -> None:
        for name in await asyncio.to_thread(containers):
            if name not in followed:
                followed.add(name)
                threading.Thread(target=reader, args=(name, 200 if first else 50), daemon=True).start()

    try:
        await scan(first=True)
        last_scan = loop.time()
        while True:
            try:
                await send(await asyncio.wait_for(lines.get(), timeout=3.0) + "\n")
            except asyncio.TimeoutError:
                pass
            if loop.time() - last_scan >= 3.0:
                await scan(first=False)
                last_scan = loop.time()
    finally:
        for stream in streams:
            try:
                stream.close()
            except Exception:
                pass


async def stream_container_stats(app_id: int, send: Callable[[dict], Awaitable[None]]) -> None:
    """Every 2 s: stats of app_id's replica containers on this machine, added up."""
    while True:
        frame = await containers_stats_logs.app_aggregate_stats(app_id)
        frame["timestamp"] = int(time.time() * 1000)
        await send(frame)
        await asyncio.sleep(2)


async def stream_system_stats(send: Callable[[dict], Awaitable[None]]) -> None:
    """CPU, memory and disk of this machine every 2 s, after replaying the last minutes so charts fill at once."""
    for snapshot in list(_system_history):
        await send(snapshot)
    psutil.cpu_percent(interval=None)
    while True:
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        cpu = await asyncio.to_thread(psutil.cpu_percent, 1)
        frame = {
            "timestamp": int(time.time() * 1000),
            "cpu_percent": cpu,
            "memory_total_mb": round(mem.total / 1024 / 1024),
            "memory_used_mb": round(mem.used / 1024 / 1024),
            "memory_percent": mem.percent,
            "disk_total_gb": round(disk.total / 1024 / 1024 / 1024, 1),
            "disk_used_gb": round(disk.used / 1024 / 1024 / 1024, 1),
            "disk_percent": disk.percent,
        }
        _system_history.append(frame)
        await send(frame)
        await asyncio.sleep(2)
