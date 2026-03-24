import httpx
import uuid
import asyncio
import logging
from datetime import datetime
from decimal import Decimal
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func
from app.models import Notification, Invoice, Department

logger = logging.getLogger(__name__)

class NotificationService:
    
    @staticmethod
    async def create_notification(
        db: AsyncSession, 
        company_id: uuid.UUID, 
        type: str, 
        title: str, 
        message: str, 
        department_id: uuid.UUID | None = None
    ) -> Notification:
        notification = Notification(
            company_id=company_id,
            department_id=department_id,
            type=type,
            title=title,
            message=message
        )
        db.add(notification)
        await db.commit()
        await db.refresh(notification)
        
        if type == 'CRITICAL':
            payload = {
                "text": f"Ostrzeżenie CRITICAL: {title}\n{message}",
                "company_id": str(company_id),
                "department_id": str(department_id) if department_id else None
            }
            # Fire and forget webhook
            asyncio.create_task(NotificationService.trigger_webhook(payload))
            
        return notification

    @staticmethod
    async def trigger_webhook(payload: dict):
        try:
            async with httpx.AsyncClient() as client:
                webhook_url = "https://webhook.site/dummy-webhook-url-for-demo"
                response = await client.post(webhook_url, json=payload)
                response.raise_for_status()
                logger.info(f"Webhook triggered successfully: {response.status_code}")
        except Exception as e:
            logger.error(f"Failed to trigger webhook: {e}")

    @staticmethod
    async def check_budget_and_notify(
        db: AsyncSession, 
        department_id: uuid.UUID, 
        company_id: uuid.UUID
    ):
        # Pobranie limitu dzia\u0142u
        dept_result = await db.execute(
            select(Department).where(Department.id == department_id, Department.company_id == company_id)
        )
        department = dept_result.scalar_one_or_none()
        
        if not department or not department.monthly_limit:
            return

        limit = Decimal(department.monthly_limit)
        if limit <= 0:
            return
        
        # Obliczenie sumy wydatków w bie\u017c\u0105cym miesi\u0105cu
        now = datetime.utcnow()
        start_of_month = datetime(now.year, now.month, 1)

        inv_result = await db.execute(
            select(func.sum(Invoice.net_amount)).where(
                Invoice.department_id == department_id,
                Invoice.company_id == company_id,
                Invoice.issue_date >= start_of_month
            )
        )
        
        total_spent = inv_result.scalar_one_or_none()
        if total_spent is None:
            total_spent = Decimal(0)
        else:
            total_spent = Decimal(total_spent)

        # Sprawdzenie progów
        percentage = (total_spent / limit) * 100
        
        if percentage > 100:
            await NotificationService.create_notification(
                db=db,
                company_id=company_id,
                department_id=department_id,
                type='CRITICAL',
                title=f"Przekroczono limit bud\u017cetu dla dzia\u0142u {department.name}!",
                message=f"Wydatki w tym miesi\u0105cu ({total_spent:.2f} PLN) przekroczy\u0142y limit {limit:.2f} PLN! Stopie\u0144 wykorzystania: {percentage:.1f}%."
            )
        elif percentage >= 80:
            await NotificationService.create_notification(
                db=db,
                company_id=company_id,
                department_id=department_id,
                type='WARNING',
                title=f"Uwaga: Dzia\u0142 {department.name} zbli\u017ca si\u0119 do limitu bud\u017cetu",
                message=f"Wydatki w tym miesi\u0105cu to ju\u017c {total_spent:.2f} PLN, co stanowi {percentage:.1f}% limitu ({limit:.2f} PLN)."
            )

    @staticmethod
    async def run_background_budget_check(department_id: uuid.UUID, company_id: uuid.UUID) -> None:
        from app.database import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            try:
                await NotificationService.check_budget_and_notify(db, department_id, company_id)
            except Exception as e:
                logger.error(f"Background budget check failed for department {department_id}: {e}")
