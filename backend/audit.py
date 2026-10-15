import json
import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from models import AuditLog

log = logging.getLogger("cloudbase.audit")


async def log_audit(
    db: AsyncSession,
    action: str,
    actor: str = "system",
    app_id: Optional[int] = None,
    detail: Optional[dict] = None,
) -> None:
    """Write an audit log entry. Calls flush() so the caller's commit() persists it.
    Swallows all exceptions so audit failures never break operations."""
    try:
        detail_str = None
        if detail:
            try:
                detail_str = json.dumps(detail)
            except Exception:
                detail_str = json.dumps({k: str(v) for k, v in detail.items()})
        entry = AuditLog(
            action=action,
            actor=actor,
            app_id=app_id,
            detail=detail_str,
        )
        db.add(entry)
        await db.flush()
        # Also in the server log, and (via app_id) in the app's Cloudbase log.
        # Details stay in the audit table only — they can hold IPs and names.
        if app_id is not None:
            log.info("%s by %s", action, actor, extra={"app_id": app_id})
        else:
            log.info("%s by %s", action, actor)
    except Exception:
        log.debug("audit log write failed", exc_info=True)
