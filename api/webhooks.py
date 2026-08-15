from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from api.auth import get_current_api_user
from database.models import User, Webhook, get_db


router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])


@router.get("")
def list_webhooks(current_user: Annotated[User, Depends(get_current_api_user)], db: Session = Depends(get_db)) -> list[dict]:
    webhooks = db.query(Webhook).filter(Webhook.user_id == current_user.id).order_by(Webhook.created_at.desc()).all()
    return [
        {
            "id": webhook.id,
            "webhook_url": webhook.webhook_url,
            "event_type": webhook.event_type,
            "is_active": webhook.is_active,
            "created_at": webhook.created_at.isoformat(),
        }
        for webhook in webhooks
    ]
