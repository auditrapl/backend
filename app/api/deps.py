from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app.database import get_db
from app.models import User
from app.core import security
import uuid

async def get_current_user(
    token: str = Depends(security.oauth2_scheme),
    db: AsyncSession = Depends(get_db)
) -> User:
    # 1. Verify JWT
    payload = security.verify_jwt(token)
    
    # 2. Extract Claims
    user_id_str = payload.get("sub")
    email = payload.get("email")
    
    if not user_id_str:
        # Should be caught by verify_jwt logic or is malformed
        raise security.HTTPException(status_code=401, detail="Invalid token payload")

    user_uuid = uuid.UUID(user_id_str)
    
    # 3. Query User
    result = await db.execute(select(User).where(User.id == user_uuid))
    user = result.scalars().first()
    
    # 4. Auto-Sync Strategy
    if not user:
        # User not found locally -> Sync from Token
        user = User(id=user_uuid, email=email)
        db.add(user)
        # We commit here to ensure the user exists for subsequent foreign key checks
        await db.commit()
        await db.refresh(user)
        
    return user
