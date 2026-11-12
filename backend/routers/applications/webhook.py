from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from apps import webhooks as apps_webhooks
from apps.serializers import _get_or_404
from core import auth as _auth
from core.audit import log_audit
from core.database import get_db

router = APIRouter(prefix="/api/apps", tags=["applications"])


def _view(app) -> dict:
    secret = apps_webhooks.secret_of(app)
    return {"enabled": bool(secret), "path": f"/api/webhooks/{app.id}", "secret": secret}


@router.get("/{app_id}/webhook")
async def get_webhook(app_id: int, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("apps.configure"))):
    return _view(await _get_or_404(app_id, db))


@router.post("/{app_id}/webhook")
async def create_webhook(app_id: int, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("apps.configure")),
                         actor: str = Depends(_auth.get_current_actor)):
    """Switch the webhook on, or give it a new secret (the old one stops working at once)."""
    app = await _get_or_404(app_id, db)
    app.webhook_secret = apps_webhooks.store(apps_webhooks.new_secret())
    await log_audit(db, "app.webhook_secret", actor=actor, app_id=app_id, detail={"name": app.name})
    await db.commit()
    return _view(app)


@router.delete("/{app_id}/webhook")
async def delete_webhook(app_id: int, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("apps.configure")),
                         actor: str = Depends(_auth.get_current_actor)):
    app = await _get_or_404(app_id, db)
    app.webhook_secret = None
    await log_audit(db, "app.webhook_off", actor=actor, app_id=app_id, detail={"name": app.name})
    await db.commit()
    return _view(app)
