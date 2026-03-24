import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID

from app.database import get_db
from app.api.deps import get_current_user
from app.schemas import TaxRequest, TaxAnalysisReport, UserResponse
from app.services.taxes_agent import run_taxes_agent

logger = logging.getLogger(__name__)

router = APIRouter()

@router.post("/analyze", response_model=TaxAnalysisReport)
async def analyze_taxes(
    request: TaxRequest,
    current_user: UserResponse = Depends(get_current_user)
):
    """
    Uruchamia agenta ds. podatków (AI Tax Analyst).
    """
    try:
        if not current_user.id:
            raise HTTPException(status_code=400, detail="Brak autoryzacji do wywołania Agenta Podatków.")
            
        user_uuid = str(current_user.id)
        
        report = await run_taxes_agent(request, user_uuid)
        return report

    except ValueError as e:
        logger.warning(f"Validation/Generation error in taxes response: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error executing AI Taxes logic: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Wystąpił wewnętrzny błąd Agenta Podatkowego AI.")
