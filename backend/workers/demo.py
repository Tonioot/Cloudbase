import asyncio
import json
import logging
from collections import deque
from containers import processes as pm

log = logging.getLogger("cloudbase.main")


async def _dev_demo_logs(path: str) -> None:
    """Dev only: feed the demo apps' log views from dev-demo-logs.json (no Docker needed)."""
    import random
    from datetime import datetime
    try:
        with open(path, encoding="utf-8") as f:
            demo = {int(k): v for k, v in json.load(f).items()}
    except (OSError, ValueError) as e:
        log.warning("dev demo logs not loaded from %s: %s", path, e)
        return

    def emit(app_id: int, template: str) -> None:
        line = template.replace("{ts}", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        pm.log_buffers.setdefault(app_id, deque(maxlen=5000)).append(line)
        pm._push_container_line(app_id, line)

    for app_id, d in demo.items():
        for t in d["start"]:
            emit(app_id, t)
        for t in d["loop"] * 3:
            emit(app_id, t)
    while True:
        await asyncio.sleep(1.5)
        app_id, d = random.choice(list(demo.items()))
        emit(app_id, random.choice(d["loop"]))
