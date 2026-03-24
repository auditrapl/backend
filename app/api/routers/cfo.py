import logging
import uuid
from datetime import datetime, timedelta
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
from app.api.deps import get_current_user
from app.database import get_db, AsyncSessionLocal
from app.schemas import CfoRequest, CfoReport, UserResponse, SharedCfoReportCreate, SharedCfoReportResponse, ProactiveInsightResponse, MorningBriefingResponse
from app.models import SharedCfoReport, ProactiveInsight, CompanyContextModel
from app.services.agentic_cfo import agentic_cfo_stream
from app.services.context_operational import process_and_upsert_operational
from app.services.morning_briefing import generate_briefing_for_company

logger = logging.getLogger(__name__)

router = APIRouter()

@router.post("/simulate")
async def simulate_cfo_scenario(
    request: CfoRequest,
    background_tasks: BackgroundTasks,
    current_user: UserResponse = Depends(get_current_user)
):
    """
    Endpoint obsługujący zapytania ("Szanse i Ryzyka") do Wirtualnego CFO (Majora).
    Zwraca Server-Sent Events (SSE) z postępami pod 'data: ...'.
    """
    try:
        user_uuid = str(current_user.id)
        # Assuming the company_id is bound or we pass a default from current_user's company bounds
        company_uuid = str(getattr(current_user, "company_id", "00000000-0000-0000-0000-000000000000"))
        
        # ─── PAMIĘĆ KONWERSACYJNA (Ciche uczenie się w tle z czatu) ───
        async def background_chat_upsert(c_id: str, prompt_text: str):
            async with AsyncSessionLocal() as bg_db:
                try:
                    import uuid
                    await process_and_upsert_operational(bg_db, uuid.UUID(c_id), prompt_text)
                except Exception as ex:
                    print(f"Błąd podczas analizy prompta 'Implicit Learning': {ex}")
                    
        background_tasks.add_task(background_chat_upsert, company_uuid, request.prompt)
        # ──────────────────────────────────────────────────────────────
        
        async def event_generator():
            try:
                # Iterate over the async generator
                async for chunk in agentic_cfo_stream(request, company_uuid):
                    # SSE format requires: data: <string>\n\n
                    yield f"data: {chunk}\n\n"
            except Exception as e:
                logger.error(f"Error executing CFO AI strategy logic Stream: {e}", exc_info=True)
                yield f"data: {{\"status\": \"error\", \"message\": \"Wystąpił błąd Matrixa Wewnętrznego.\"}}\n\n"

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    except Exception as e:
        logger.error(f"Error initializing CFO Strategy: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Wystąpił błąd Matrixa Wewnętrznego.")

@router.post("/share", response_model=SharedCfoReportResponse)
async def share_cfo_report(
    request: SharedCfoReportCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user)
):
    try:
        company_uuid = getattr(current_user, "company_id", None)
        
        new_share = SharedCfoReport(
            company_id=company_uuid,
            prompt=request.prompt,
            report_data=request.report_data
        )
        db.add(new_share)
        await db.commit()
        await db.refresh(new_share)
        
        return new_share
    except Exception as e:
        logger.error(f"Error saving shared CFO report: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Nie udało się zapisać i udostępnić raportu.")

@router.get("/shared/{share_id}", response_model=SharedCfoReportResponse)
async def get_shared_cfo_report(
    share_id: uuid.UUID,
    db: AsyncSession = Depends(get_db)
):
    try:
        result = await db.execute(select(SharedCfoReport).where(SharedCfoReport.id == share_id))
        shared_report = result.scalar_one_or_none()
        
        if not shared_report:
            raise HTTPException(status_code=404, detail="Nie znaleziono udostępnionego raportu CFO.")
            
        return shared_report
    except Exception as e:
        logger.error(f"Error retrieving shared CFO report: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Wystąpił błąd podczas pobierania wybranego raportu.")

@router.get("/morning-briefing", response_model=MorningBriefingResponse)
async def get_morning_briefing(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user)
):
    """
    Lightning-fast endpoint: zwraca pre-generowane insighty z Nocnej Zmiany.
    Zero wywołań AI — dane prosto z bazy.
    """
    company_uuid = getattr(current_user, "company_id", None)
    if not company_uuid:
        raise HTTPException(status_code=400, detail="Brak przypisanej firmy.")

    result = await db.execute(
        select(ProactiveInsight)
        .where(ProactiveInsight.company_id == company_uuid)
        .order_by(ProactiveInsight.created_at.desc())
        .limit(3)
    )
    insights = result.scalars().all()
    summary = next((i.summary for i in insights if i.summary), None)
    return MorningBriefingResponse(summary=summary, insights=list(insights))


@router.post("/morning-briefing/generate", response_model=MorningBriefingResponse)
async def generate_morning_briefing_manual(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user)
):
    """
    Ręczne generowanie Porannej Odprawy — max 1 raz na 24h.
    Wywołuje Gemini AI i zapisuje insighty do bazy.
    """
    company_uuid = getattr(current_user, "company_id", None)
    if not company_uuid:
        raise HTTPException(status_code=400, detail="Brak przypisanej firmy.")

    # ── Cooldown 24h ──
    result = await db.execute(
        select(ProactiveInsight.created_at)
        .where(ProactiveInsight.company_id == company_uuid)
        .order_by(ProactiveInsight.created_at.desc())
        .limit(1)
    )
    last_created = result.scalar_one_or_none()

    if last_created:
        now = datetime.utcnow()
        next_available = last_created + timedelta(hours=24)
        if now < next_available:
            raise HTTPException(
                status_code=429,
                detail={
                    "message": "Odprawę można wygenerować raz na 24 godziny.",
                    "next_available_at": next_available.isoformat(),
                },
            )

    # ── Sprawdź kontekst firmy ──
    ctx_result = await db.execute(
        select(CompanyContextModel).where(CompanyContextModel.company_id == company_uuid)
    )
    context_record = ctx_result.scalar_one_or_none()
    if not context_record:
        raise HTTPException(status_code=400, detail="Brak kontekstu firmy. Dodaj dokumenty lub faktury.")

    # ── Generuj odprawę ──
    await generate_briefing_for_company(db, company_uuid, context_record.context_data or {})

    # ── Zwróć świeże insighty ──
    result = await db.execute(
        select(ProactiveInsight)
        .where(ProactiveInsight.company_id == company_uuid)
        .order_by(ProactiveInsight.created_at.desc())
        .limit(3)
    )
    insights = result.scalars().all()
    summary = next((i.summary for i in insights if i.summary), None)
    return MorningBriefingResponse(summary=summary, insights=list(insights))


@router.get("/context")
async def get_company_context(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user)
):
    """
    Backward-compat: zwraca pełny 3-warstwowy kontekst firmy.
    Deleguje do /api/context/full.
    """
    try:
        import uuid
        from app.services.context_canonical import get_or_create_canonical
        from app.services.context_operational import get_or_create_operational

        company_uuid = getattr(current_user, "company_id", None)
        if not company_uuid:
            raise HTTPException(status_code=400, detail="Brak przypisanej firmy.")

        company_id = uuid.UUID(str(company_uuid))
        canonical = await get_or_create_canonical(db, company_id)
        operational = await get_or_create_operational(db, company_id)

        return {
            "canonical": canonical.model_dump(),
            "operational": operational.model_dump(mode="json"),
        }
    except Exception as e:
        logger.error(f"Error getting company context: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Błąd pobierania pamięci operacyjnej.")
