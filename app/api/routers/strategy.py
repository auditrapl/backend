from fastapi import APIRouter, Depends, HTTPException, status
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from app.api.deps import get_current_user, get_db
from app.models import User, StrategyHistory
from app.schemas import StrategyRequest, StrategyResponse, StrategyHistoryCreate, StrategyHistoryResponse
from app.services.strategy_agent import run_strategy_agent

router = APIRouter(prefix="/api/strategy", tags=["Strategy Module API"])

@router.post("/simulate", response_model=StrategyResponse)
async def simulate_strategy_endpoint(
    request: StrategyRequest,
    current_user: User = Depends(get_current_user)
) -> Any:
    """
    Uruchamia moduł Wirtualnego CFO (Agent Strategii).
    """
    try:
        company_id = str(current_user.company_id) if current_user.company_id else ""
        if not company_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Użytkownik nie jest przypisany do żadnej firmy."
            )
            
        response_data = await run_strategy_agent(
            user_prompt=request.prompt, 
            company_id=company_id,
            date_from=request.date_from,
            date_to=request.date_to
        )
        
        if response_data.get("status") == "error":
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=response_data.get("message")
            )
            
        return response_data
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Strategy Simulation Error: {str(e)}"
        )

@router.post("/history", response_model=StrategyHistoryResponse)
async def save_strategy_history(
    request: StrategyHistoryCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    try:
        if not current_user.company_id:
            raise HTTPException(status_code=400, detail="Użytkownik nie przypisany do firmy.")
            
        new_history = StrategyHistory(
            company_id=current_user.company_id,
            user_id=current_user.id,
            prompt=request.prompt,
            date_from=request.date_from,
            date_to=request.date_to,
            response_data=request.response_data
        )
        db.add(new_history)
        await db.commit()
        await db.refresh(new_history)
        return new_history
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/history", response_model=list[StrategyHistoryResponse])
async def get_strategy_history(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    try:
        if not current_user.company_id:
            return []
            
        stmt = select(StrategyHistory).where(
            StrategyHistory.company_id == current_user.company_id
        ).order_by(desc(StrategyHistory.created_at))
        
        result = await db.execute(stmt)
        return result.scalars().all()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
