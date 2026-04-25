from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func
from typing import List
from uuid import UUID
from app.database import get_db
from app.models import Invoice, User
from app.schemas import InvoiceCreate, InvoiceResponse, InvoiceUpdate
from app.api.deps import get_current_user
from app.auth import verify_token
from app.services.audit_service import AuditService
from app.services.notification_service import NotificationService
from app.services.context_operational import refresh_safecash_background
from app.services.fa3_parser import parse_fa3_xml

router = APIRouter(tags=["invoices"], dependencies=[Depends(verify_token)])

@router.get("", response_model=dict)
@router.get("/", response_model=dict, include_in_schema=False)
async def get_invoices(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    if not current_user.company_id:
        return {"invoices": []}
        
    result = await db.execute(
        select(Invoice)
        .where(Invoice.company_id == current_user.company_id)
        .order_by(Invoice.created_at.desc())
    )
    invoices = result.scalars().all()
    return {"invoices": [InvoiceResponse.model_validate(inv).model_dump() for inv in invoices]}

@router.post("", response_model=InvoiceResponse, status_code=201)
@router.post("/", response_model=InvoiceResponse, status_code=201, include_in_schema=False)
async def create_invoice(
    invoice: InvoiceCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if not current_user.company_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User has no company assigned. Please register a company first."
        )

    try:
        invoice_data = invoice.model_dump(exclude_unset=True)

        # Auto-parse KSeF FA(3) XML if provided and enrich invoice payload.
        if invoice_data.get("ksef_xml"):
            try:
                parsed = parse_fa3_xml(invoice_data["ksef_xml"])
                invoice_data["parsed_data"] = parsed
                invoice_data["ksef_number"] = invoice_data.get("ksef_number") or parsed["invoice"].get("ksef_number")
                invoice_data["internal_number"] = invoice_data.get("internal_number") or parsed["invoice"].get("number")
                invoice_data["contractor_name"] = invoice_data.get("contractor_name") or parsed["seller"].get("name")
                invoice_data["contractor_nip"] = invoice_data.get("contractor_nip") or parsed["seller"].get("nip")
                invoice_data["currency"] = invoice_data.get("currency") or parsed["invoice"].get("currency")
                invoice_data["net_amount"] = invoice_data.get("net_amount") or parsed["totals"].get("net")
                invoice_data["vat_amount"] = invoice_data.get("vat_amount") or parsed["totals"].get("vat")
                invoice_data["gross_amount"] = invoice_data.get("gross_amount") or parsed["totals"].get("gross")
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid KSeF XML: {exc}",
                )
        
        for dt_field in ("issue_date", "due_date", "payment_date"):
            if invoice_data.get(dt_field) and invoice_data[dt_field].tzinfo:
                invoice_data[dt_field] = invoice_data[dt_field].replace(tzinfo=None)

        # Auto payment_days jeśli podano payment_date i issue_date
        if invoice_data.get("payment_date") and invoice_data.get("issue_date"):
            invoice_data["payment_days"] = (invoice_data["payment_date"] - invoice_data["issue_date"]).days

        new_invoice = Invoice(
            **invoice_data,
            company_id=current_user.company_id,
            user_id=current_user.id
        )
        
        db.add(new_invoice)
        await db.commit()
        await db.refresh(new_invoice)

        # 🧠 Fire-and-forget: trigger AI audit in background
        # Response returns immediately — user sees invoice with status='new'
        # Background task updates risk_score, ai_analysis_json, status='audited'
        background_tasks.add_task(AuditService.run_background_audit, new_invoice.id)

        if new_invoice.department_id and new_invoice.net_amount:
            background_tasks.add_task(NotificationService.run_background_budget_check, new_invoice.department_id, new_invoice.company_id)

        # SafeCash: przelicz gotówkę z faktur
        background_tasks.add_task(refresh_safecash_background, str(new_invoice.company_id))

        return new_invoice
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/bulk", status_code=201)
async def create_invoices_bulk(
    invoices: List[InvoiceCreate],
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Bulk import invoices from a JSON array.
    Accepts both a single object (wrapped) and an array of InvoiceCreate objects.
    Each invoice gets auto-assigned to the user's company and triggers background AI audit.
    """
    if not current_user.company_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User has no company assigned. Please register a company first.",
        )

    created = []
    try:
        for inv_schema in invoices:
            inv_data = inv_schema.model_dump(exclude_unset=True)

            if inv_data.get("ksef_xml"):
                try:
                    parsed = parse_fa3_xml(inv_data["ksef_xml"])
                    inv_data["parsed_data"] = parsed
                    inv_data["ksef_number"] = inv_data.get("ksef_number") or parsed["invoice"].get("ksef_number")
                    inv_data["internal_number"] = inv_data.get("internal_number") or parsed["invoice"].get("number")
                    inv_data["contractor_name"] = inv_data.get("contractor_name") or parsed["seller"].get("name")
                    inv_data["contractor_nip"] = inv_data.get("contractor_nip") or parsed["seller"].get("nip")
                    inv_data["currency"] = inv_data.get("currency") or parsed["invoice"].get("currency")
                    inv_data["net_amount"] = inv_data.get("net_amount") or parsed["totals"].get("net")
                    inv_data["vat_amount"] = inv_data.get("vat_amount") or parsed["totals"].get("vat")
                    inv_data["gross_amount"] = inv_data.get("gross_amount") or parsed["totals"].get("gross")
                except ValueError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Invalid KSeF XML in bulk payload: {exc}",
                    )

            for dt_field in ("issue_date", "due_date", "payment_date"):
                if inv_data.get(dt_field) and inv_data[dt_field].tzinfo:
                    inv_data[dt_field] = inv_data[dt_field].replace(tzinfo=None)

            if inv_data.get("payment_date") and inv_data.get("issue_date"):
                inv_data["payment_days"] = (inv_data["payment_date"] - inv_data["issue_date"]).days

            new_invoice = Invoice(
                **inv_data,
                company_id=current_user.company_id,
                user_id=current_user.id,
            )
            db.add(new_invoice)
            await db.flush()  # get ID without committing yet
            created.append(new_invoice)

        await db.commit()

        # Refresh all and schedule background audits
        for inv in created:
            await db.refresh(inv)
            background_tasks.add_task(AuditService.run_background_audit, inv.id)
            if inv.department_id and inv.net_amount:
                background_tasks.add_task(NotificationService.run_background_budget_check, inv.department_id, inv.company_id)

        # SafeCash: przelicz gotówkę z faktur (raz po całym bulku)
        if created:
            background_tasks.add_task(refresh_safecash_background, str(current_user.company_id))

        return {
            "created_count": len(created),
            "invoices": [InvoiceResponse.model_validate(inv).model_dump() for inv in created],
        }
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Bulk import failed: {str(e)}")

@router.patch("/{invoice_id}", response_model=InvoiceResponse)
async def update_invoice(
    invoice_id: UUID,
    invoice_update: InvoiceUpdate,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    query = select(Invoice).where(
        Invoice.id == invoice_id,
        Invoice.company_id == current_user.company_id
    )
    result = await db.execute(query)
    db_invoice = result.scalar_one_or_none()
    
    if not db_invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
        
    update_data = invoice_update.model_dump(exclude_unset=True)

    for dt_field in ("issue_date", "due_date", "payment_date"):
        if update_data.get(dt_field) and update_data[dt_field].tzinfo:
            update_data[dt_field] = update_data[dt_field].replace(tzinfo=None)

    for key, value in update_data.items():
        setattr(db_invoice, key, value)

    # Auto payment_date: gdy is_paid zmienia się na True i brak payment_date
    if update_data.get("is_paid") is True and not db_invoice.payment_date:
        db_invoice.payment_date = datetime.utcnow()

    # Auto payment_days: oblicz z issue_date i payment_date
    if db_invoice.payment_date and db_invoice.issue_date:
        db_invoice.payment_days = (db_invoice.payment_date - db_invoice.issue_date).days

    await db.commit()
    await db.refresh(db_invoice)
    
    if db_invoice.department_id and db_invoice.net_amount:
        background_tasks.add_task(NotificationService.run_background_budget_check, db_invoice.department_id, db_invoice.company_id)

    # SafeCash: przelicz gotówkę z faktur
    background_tasks.add_task(refresh_safecash_background, str(db_invoice.company_id))

    return db_invoice


@router.delete("/{invoice_id}")
async def delete_invoice(
    invoice_id: UUID,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Usuń fakturę i powiązane rekordy (audit_logs, proactive_insights, bank_transactions)."""
    from app.models import AuditLog, ProactiveInsight, BankTransaction
    from sqlalchemy import delete as sql_delete

    result = await db.execute(
        select(Invoice).where(Invoice.id == invoice_id, Invoice.company_id == current_user.company_id)
    )
    inv = result.scalar_one_or_none()
    if not inv:
        raise HTTPException(status_code=404, detail="Faktura nie znaleziona")

    company_id = str(inv.company_id)

    # Usuń powiązane rekordy (FK constraints)
    await db.execute(sql_delete(AuditLog).where(AuditLog.invoice_id == invoice_id))
    await db.execute(sql_delete(ProactiveInsight).where(ProactiveInsight.source_invoice_id == invoice_id))
    await db.execute(
        sql_delete(BankTransaction).where(BankTransaction.matched_invoice_id == invoice_id)
    )

    await db.delete(inv)
    await db.commit()

    background_tasks.add_task(refresh_safecash_background, company_id)
    return {"status": "deleted"}


@router.post("/{invoice_id}/apply-suggestion", response_model=InvoiceResponse)
async def apply_ai_suggestion(
    invoice_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Zatwierdź AI sugestię oddziału/kategorii.
    - Jeśli dział już istnieje → przypina fakturę do niego.
    - Jeśli AI sugeruje nowy dział (is_new_department=true) → automatycznie tworzy go i przypina fakturę.
    """
    result = await db.execute(
        select(Invoice).where(Invoice.id == invoice_id, Invoice.company_id == current_user.company_id)
    )
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Faktura nie znaleziona")

    ai = invoice.ai_analysis_json or {}
    dept_suggestion = ai.get("department_assignment", {})

    if not dept_suggestion:
        raise HTTPException(status_code=400, detail="Brak sugestii AI dla tej faktury")

    dept_name = (dept_suggestion.get("department_name") or "").strip()
    if not dept_name:
        raise HTTPException(status_code=400, detail="Sugestia AI nie zawiera nazwy działu")

    from app.models import Department

    # Szukaj istniejącego działu (case-insensitive, trim)
    dept_result = await db.execute(
        select(Department).where(
            Department.company_id == current_user.company_id,
            func.lower(func.trim(Department.name)) == dept_name.lower(),
        )
    )
    dept = dept_result.scalar_one_or_none()

    # Nie ma → utwórz (bez względu na flagę is_new_department; jeśli AI powiedziało "Marketing"
    # a dział "Marketing" nie istnieje, to i tak trzeba go utworzyć)
    if not dept:
        dept = Department(
            company_id=current_user.company_id,
            name=dept_name,
        )
        db.add(dept)
        await db.flush()

    invoice.department_id = dept.id

    # Apply category if suggested
    suggested_cat = ai.get("audit_result", {}).get("suggested_category") or dept_name
    if suggested_cat:
        invoice.category_name = invoice.category_name or suggested_cat

    # Mark as manually confirmed
    invoice.category_source = "manual"
    await db.commit()
    await db.refresh(invoice)
    return invoice

