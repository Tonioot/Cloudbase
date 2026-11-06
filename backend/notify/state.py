import asyncio
from datetime import datetime

_wake = asyncio.Event()


def _utcnow() -> datetime:
    return datetime.utcnow()
