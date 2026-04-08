from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from typing import List

from app.database import get_db
from app.models import Notification, User
from app.schemas import NotificationResponse
from app.api.deps import get_current_user
from app.auth import verify_token

router = APIRouter(tags=["notifications"], dependencies=[Depends(verify_token)])

@router.get("", response_model=List[NotificationResponse])
async def get_notifications(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Retrieve all unread notifications for the user's organization.
    """
    if not current_user.company_id:
        return []

    result = await db.execute(
        select(Notification)
        .where(
            Notification.company_id == current_user.company_id,
            Notification.is_read == False
        )
        .order_by(Notification.created_at.desc())
        .limit(50)
    )
    return result.scalars().all()
