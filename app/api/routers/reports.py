from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.api.deps import get_current_user
from app.models import Report, User
from app.schemas import ReportCreate, ReportResponse
from app.services.ai_report_service import ReportOrchestrator

router = APIRouter()

from fastapi.responses import StreamingResponse

@router.post("/generate")
async def generate_report(
    request: ReportCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Generate a new financial report based on the template and user query.
    Extracts data using AI-generated SQL and summarizes it with a Narrator AI.
    Streams progress using Server-Sent Events (SSE).
    """
    return StreamingResponse(
        ReportOrchestrator.stream_generate_and_save_report(
            user=current_user,
            db=db,
            template_name=request.template_name,
            parameters=request.parameters
        ),
        media_type="text/event-stream"
    )

@router.get("", response_model=list[ReportResponse])
async def get_reports(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    List all generated reports for the current user's company.
    """
    stmt = (
        select(Report)
        .where(Report.company_id == current_user.company_id)
        .order_by(Report.created_at.desc())
    )
    result = await db.execute(stmt)
    reports = result.scalars().all()
    
    return [report for report in reports]
