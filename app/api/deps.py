from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app.database import get_db
from app.models import User
from app.auth import verify_token, VerifiedToken
import uuid

async def get_current_user(
    token_payload: VerifiedToken = Depends(verify_token),
    db: AsyncSession = Depends(get_db)
) -> User:
    user_id_str = token_payload.get("sub")
    email = token_payload.get("email")
    
    if not user_id_str:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )

    try:
        user_uuid = uuid.UUID(user_id_str)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )
    
    result = await db.execute(select(User).where(User.id == user_uuid))
    user = result.scalars().first()
    
    if not user:
        user = User(id=user_uuid, email=email)
        db.add(user)
        await db.commit()
        await db.refresh(user)
        
    return user
