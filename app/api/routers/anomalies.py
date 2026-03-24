import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID

from app.database import get_db
from app.api.deps import get_current_user
from app.schemas import AnomalyRequest, AnomalyReport, UserResponse
from app.services.anomaly_agent import run_anomaly_agent

logger = logging.getLogger(__name__)

router = APIRouter()

@router.post("/detect", response_model=AnomalyReport)
async def detect_anomalies(
    request: AnomalyRequest,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user)
):
    """
    Uruchamia agenta anomalii (SQL + LLM) na żądanie użytkownika.
    """
    try:
        # Pamiętajmy, że podajemy company_id, który pobieramy z zalogowanego usera.
        if not current_user.company_id:
            raise HTTPException(status_code=400, detail="Użytkownik nie ma przypisanej firmy.")
        
        company_id = UUID(str(current_user.company_id))
        
        report = await run_anomaly_agent(request, db, company_id)
        return report

    except ValueError as e:
        logger.warning(f"Validation/Generation error in anomaly response: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error executing anomaly detection: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Wystąpił wewnętrzny błąd generowania anomalii.")
