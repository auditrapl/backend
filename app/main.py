import logging
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from app.database import engine, Base
from app.api import invoices, auth, departments, dashboard, notifications, documents, billing
from app.api.routers import reports, strategy, anomalies, taxes, cfo, context as context_router, cashflow, conversations
from app.services.morning_briefing import generate_daily_briefings_for_all_companies, send_morning_emails_for_all_companies
from app.services.context_snapshots import (
    trigger_weekly_snapshots_for_all_companies,
    trigger_monthly_snapshots_for_all_companies,
)
from app.auth import verify_token

logger = logging.getLogger("auditra")

scheduler = AsyncIOScheduler(timezone="Europe/Warsaw")

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # ─── NOCNA ZMIANA: CRON 03:00 codziennie ───
    scheduler.add_job(
        generate_daily_briefings_for_all_companies,
        trigger=CronTrigger(hour=3, minute=0),
        id="morning_briefing",
        name="Nocna Zmiana — Morning Briefing",
        replace_existing=True,
    )

    # ─── PORANNA WYSYŁKA EMAIL: CRON 07:00 codziennie ───
    scheduler.add_job(
        send_morning_emails_for_all_companies,
        trigger=CronTrigger(hour=7, minute=0),
        id="morning_email",
        name="Poranna Wysyłka Email — Morning Briefing",
        replace_existing=True,
    )

    # ─── SNAPSHOTY KONTEKSTU: tygodniowe (niedziela 01:00) i miesięczne (1. dnia 02:00) ───
    from app.database import AsyncSessionLocal as _ASL

    scheduler.add_job(
        trigger_weekly_snapshots_for_all_companies,
        args=[_ASL],
        trigger=CronTrigger(day_of_week="sun", hour=1, minute=0),
        id="weekly_context_snapshot",
        name="Tygodniowy Snapshot Kontekstu",
        replace_existing=True,
    )
    scheduler.add_job(
        trigger_monthly_snapshots_for_all_companies,
        args=[_ASL],
        trigger=CronTrigger(day=1, hour=2, minute=0),
        id="monthly_context_snapshot",
        name="Miesięczny Snapshot Kontekstu (AI Summary)",
        replace_existing=True,
    )

    scheduler.start()
    logger.info("APScheduler started — Briefing Gen 03:00 | Email Send 07:00 | Weekly Snapshot Sun 01:00 | Monthly 1st 02:00")

    yield

    scheduler.shutdown(wait=False)
    logger.info("APScheduler shut down")

app = FastAPI(
    title="Auditra API",
    description="Backend for Auditra Fintech SaaS",
    version="0.2.0",
    lifespan=lifespan
)

# CORS Configuration
import os
_cors_env = os.getenv("CORS_ORIGINS", "")
origins = [o.strip() for o in _cors_env.split(",") if o.strip()] if _cors_env else [
    "http://localhost:3000",
    "http://localhost:8000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/auth", tags=["Auth"])
app.include_router(invoices.router, prefix="/invoices", tags=["Invoices"])
app.include_router(departments.router, prefix="/departments", tags=["Departments"])
app.include_router(dashboard.router, prefix="/dashboard", tags=["Dashboard AI"])
app.include_router(reports.router, prefix="/api/reports", tags=["Reports API"])
app.include_router(strategy.router, tags=["Strategy Module API"])
app.include_router(notifications.router, prefix="/api/notifications", tags=["Notifications API"])
app.include_router(documents.router, tags=["Documents Core API"])
app.include_router(anomalies.router, prefix="/api/anomalies", tags=["Anomalies API"])
app.include_router(taxes.router, prefix="/api/taxes", tags=["Taxes API"])
app.include_router(cfo.router, prefix="/api/cfo", tags=["Major CFO API"])
app.include_router(context_router.router, prefix="/api/context", tags=["Context Management API"])
app.include_router(cashflow.router, prefix="/api/cashflow", tags=["Cashflow API"])
app.include_router(conversations.router, prefix="/api/cfo/conversations", tags=["CFO Conversations API"])
app.include_router(billing.router, prefix="/billing", tags=["Billing API"])
app.add_api_route("/api/webhooks/stripe", billing.stripe_webhook, methods=["POST"], tags=["Billing API"])

@app.get("/")
async def root(_: dict = Depends(verify_token)):
    return {"message": "Welcome to Auditra API"}
