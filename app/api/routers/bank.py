"""
Router bankowy — upload CSV, matching z fakturami, confirm/reject.
"""
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, UploadFile, File
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.auth import verify_token
from app.api.deps import get_current_user
from app.database import get_db, AsyncSessionLocal
from app.models import BankTransaction, Invoice, User
from app.services.bank_parser import parse_bank_csv
from app.services.bank_matcher import auto_match_transactions
from app.services.context_operational import refresh_safecash_background

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(verify_token)])


# ==============================================================================
# SCHEMAS
# ==============================================================================

class BankTransactionResponse(BaseModel):
    id: str
    transaction_date: str
    amount: float
    title: Optional[str]
    counterparty_name: Optional[str]
    counterparty_account: Optional[str]
    balance_after: Optional[float]
    source_filename: Optional[str]
    matched_invoice_id: Optional[str]
    match_confidence: Optional[float]
    match_status: str
    matched_by: Optional[str]
    # Joined invoice data (if matched)
    invoice_number: Optional[str] = None
    invoice_contractor: Optional[str] = None
    invoice_gross: Optional[float] = None
    invoice_due_date: Optional[str] = None


class ManualMatchRequest(BaseModel):
    invoice_id: str


class BankSummary(BaseModel):
    total: int
    matched: int
    unmatched: int
    suggested: int
    confirmed: int
    rejected: int


# ==============================================================================
# UPLOAD
# ==============================================================================

@router.post("/upload")
async def upload_bank_statement(
    file: UploadFile = File(...),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Upload CSV z banku, parsuj, auto-match w tle."""
    if not current_user.company_id:
        raise HTTPException(status_code=400, detail="Brak firmy.")

    content = await file.read()
    filename = file.filename or "unknown.csv"

    # Parse CSV
    logger.info(f"Bank upload: {filename}, size={len(content)} bytes")
    # Log all lines for debugging format
    try:
        preview_text = content.decode("utf-8-sig", errors="replace")
        for i, line in enumerate(preview_text.split("\n")[:35]):
            logger.info(f"  LINE {i:02d}: {line.rstrip()}")
    except Exception:
        pass
    parsed = parse_bank_csv(content, filename)
    if not parsed:
        # Log raw content for debugging
        try:
            preview = content.decode("utf-8", errors="replace")[:500]
        except Exception:
            preview = "(cannot decode)"
        logger.error(f"Bank CSV parse returned 0 results. Preview:\n{preview}")
        raise HTTPException(status_code=400, detail=f"Nie rozpoznano formatu CSV. Pierwsze znaki: {content[:100].decode('utf-8', errors='replace')}")

    # Insert transactions
    tx_ids = []
    for p in parsed:
        tx = BankTransaction(
            company_id=current_user.company_id,
            transaction_date=datetime.combine(p.transaction_date, datetime.min.time()),
            amount=float(p.amount),
            title=p.title,
            counterparty_name=p.counterparty_name,
            counterparty_account=p.counterparty_account,
            reference=p.reference,
            balance_after=float(p.balance_after) if p.balance_after is not None else None,
            source_filename=filename,
        )
        db.add(tx)
        await db.flush()
        tx_ids.append(tx.id)

    await db.commit()

    # Auto-match w tle
    async def bg_match(company_id: uuid.UUID, ids: list[uuid.UUID]):
        async with AsyncSessionLocal() as bg_db:
            try:
                result = await auto_match_transactions(bg_db, company_id, ids)
                logger.info(f"Auto-match: {result}")
            except Exception as e:
                logger.error(f"Auto-match failed: {e}")

    background_tasks.add_task(bg_match, current_user.company_id, tx_ids)

    return {
        "uploaded": len(tx_ids),
        "filename": filename,
        "message": f"Wgrano {len(tx_ids)} transakcji. Dopasowywanie w toku...",
    }


# ==============================================================================
# LIST
# ==============================================================================

@router.get("/transactions", response_model=list[BankTransactionResponse])
async def list_transactions(
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Lista transakcji bankowych z joined invoice data."""
    if not current_user.company_id:
        raise HTTPException(status_code=400, detail="Brak firmy.")

    query = (
        select(BankTransaction)
        .where(BankTransaction.company_id == current_user.company_id)
        .order_by(BankTransaction.transaction_date.desc())
    )
    if status:
        query = query.where(BankTransaction.match_status == status)

    result = await db.execute(query)
    transactions = result.scalars().all()

    # Batch-load matched invoices
    invoice_ids = [tx.matched_invoice_id for tx in transactions if tx.matched_invoice_id]
    invoices_map = {}
    if invoice_ids:
        inv_result = await db.execute(select(Invoice).where(Invoice.id.in_(invoice_ids)))
        for inv in inv_result.scalars().all():
            invoices_map[inv.id] = inv

    responses = []
    for tx in transactions:
        inv = invoices_map.get(tx.matched_invoice_id) if tx.matched_invoice_id else None
        responses.append(BankTransactionResponse(
            id=str(tx.id),
            transaction_date=tx.transaction_date.strftime("%Y-%m-%d"),
            amount=float(tx.amount),
            title=tx.title,
            counterparty_name=tx.counterparty_name,
            counterparty_account=tx.counterparty_account,
            balance_after=float(tx.balance_after) if tx.balance_after is not None else None,
            source_filename=tx.source_filename,
            matched_invoice_id=str(tx.matched_invoice_id) if tx.matched_invoice_id else None,
            match_confidence=float(tx.match_confidence) if tx.match_confidence is not None else None,
            match_status=tx.match_status,
            matched_by=tx.matched_by,
            invoice_number=inv.internal_number or inv.ksef_number if inv else None,
            invoice_contractor=inv.contractor_name if inv else None,
            invoice_gross=float(inv.gross_amount) if inv and inv.gross_amount else None,
            invoice_due_date=inv.due_date.strftime("%Y-%m-%d") if inv and inv.due_date else None,
        ))

    return responses


# ==============================================================================
# CONFIRM / REJECT / MANUAL MATCH
# ==============================================================================

@router.post("/transactions/{tx_id}/confirm")
async def confirm_match(
    tx_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Potwierdź match → faktura.is_paid=True + refresh SafeCash."""
    tx = await _get_transaction(db, tx_id, current_user.company_id)

    if not tx.matched_invoice_id:
        raise HTTPException(status_code=400, detail="Transakcja nie ma dopasowanej faktury.")

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # Update transaction
    tx.match_status = "confirmed"
    tx.matched_at = now

    # Mark invoice as paid
    inv_result = await db.execute(select(Invoice).where(Invoice.id == tx.matched_invoice_id))
    inv = inv_result.scalar_one_or_none()
    if inv:
        inv.is_paid = True
        inv.payment_date = tx.transaction_date
        if inv.issue_date:
            inv.payment_days = (tx.transaction_date - inv.issue_date).days

    await db.commit()

    # Refresh SafeCash
    background_tasks.add_task(refresh_safecash_background, str(current_user.company_id))

    return {"status": "confirmed", "invoice_paid": True}


@router.post("/transactions/{tx_id}/reject")
async def reject_match(
    tx_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Odrzuć sugerowany match."""
    tx = await _get_transaction(db, tx_id, current_user.company_id)

    tx.match_status = "rejected"
    tx.matched_invoice_id = None
    tx.match_confidence = None
    tx.matched_by = None
    tx.matched_at = None

    await db.commit()

    return {"status": "rejected"}


@router.patch("/transactions/{tx_id}/match")
async def manual_match(
    tx_id: uuid.UUID,
    body: ManualMatchRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Ręczne dopasowanie transakcji do faktury."""
    tx = await _get_transaction(db, tx_id, current_user.company_id)

    invoice_id = uuid.UUID(body.invoice_id)
    inv_result = await db.execute(
        select(Invoice).where(Invoice.id == invoice_id, Invoice.company_id == current_user.company_id)
    )
    inv = inv_result.scalar_one_or_none()
    if not inv:
        raise HTTPException(status_code=404, detail="Faktura nie znaleziona.")

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    tx.matched_invoice_id = invoice_id
    tx.match_confidence = 1.0
    tx.match_status = "confirmed"
    tx.matched_by = "manual"
    tx.matched_at = now

    # Mark invoice paid
    inv.is_paid = True
    inv.payment_date = tx.transaction_date
    if inv.issue_date:
        inv.payment_days = (tx.transaction_date - inv.issue_date).days

    await db.commit()

    background_tasks.add_task(refresh_safecash_background, str(current_user.company_id))

    return {"status": "confirmed", "matched_by": "manual"}


@router.delete("/transactions")
async def delete_all_transactions(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Usuń wszystkie transakcje. Cofnij is_paid na powiązanych fakturach."""
    if not current_user.company_id:
        raise HTTPException(status_code=400, detail="Brak firmy.")

    # Cofnij is_paid na fakturach powiązanych z potwierdzonymi transakcjami
    confirmed = await db.execute(
        select(BankTransaction).where(
            BankTransaction.company_id == current_user.company_id,
            BankTransaction.match_status == "confirmed",
            BankTransaction.matched_invoice_id.isnot(None),
        )
    )
    invoice_ids = [tx.matched_invoice_id for tx in confirmed.scalars().all()]
    if invoice_ids:
        inv_result = await db.execute(select(Invoice).where(Invoice.id.in_(invoice_ids)))
        for inv in inv_result.scalars().all():
            inv.is_paid = False
            inv.payment_date = None
            inv.payment_days = None

    # Usuń wszystkie transakcje
    all_tx = await db.execute(
        select(BankTransaction).where(BankTransaction.company_id == current_user.company_id)
    )
    count = 0
    for tx in all_tx.scalars().all():
        await db.delete(tx)
        count += 1

    await db.commit()
    background_tasks.add_task(refresh_safecash_background, str(current_user.company_id))

    return {"status": "deleted", "count": count}


@router.delete("/transactions/{tx_id}")
async def delete_transaction(
    tx_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Usuń transakcję. Jeśli była confirmed → cofnij is_paid na fakturze."""
    tx = await _get_transaction(db, tx_id, current_user.company_id)

    # Jeśli transakcja była potwierdzona, cofnij płatność na fakturze
    if tx.match_status == "confirmed" and tx.matched_invoice_id:
        inv_result = await db.execute(select(Invoice).where(Invoice.id == tx.matched_invoice_id))
        inv = inv_result.scalar_one_or_none()
        if inv:
            inv.is_paid = False
            inv.payment_date = None
            inv.payment_days = None

    await db.delete(tx)
    await db.commit()

    background_tasks.add_task(refresh_safecash_background, str(current_user.company_id))

    return {"status": "deleted"}


# ==============================================================================
# SUMMARY (dashboard widget)
# ==============================================================================

@router.get("/summary", response_model=BankSummary)
async def bank_summary(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Stats do dashboard health widget."""
    if not current_user.company_id:
        return BankSummary(total=0, matched=0, unmatched=0, suggested=0, confirmed=0, rejected=0)

    result = await db.execute(
        select(
            func.count(BankTransaction.id).label("total"),
            func.count(BankTransaction.id).filter(BankTransaction.match_status == "confirmed").label("confirmed"),
            func.count(BankTransaction.id).filter(BankTransaction.match_status == "suggested").label("suggested"),
            func.count(BankTransaction.id).filter(BankTransaction.match_status == "unmatched").label("unmatched"),
            func.count(BankTransaction.id).filter(BankTransaction.match_status == "rejected").label("rejected"),
        )
        .where(BankTransaction.company_id == current_user.company_id)
    )
    row = result.one()

    return BankSummary(
        total=row.total,
        matched=row.confirmed + row.suggested,
        unmatched=row.unmatched,
        suggested=row.suggested,
        confirmed=row.confirmed,
        rejected=row.rejected,
    )


# ==============================================================================
# HELPERS
# ==============================================================================

async def _get_transaction(db: AsyncSession, tx_id: uuid.UUID, company_id: uuid.UUID) -> BankTransaction:
    result = await db.execute(
        select(BankTransaction).where(
            BankTransaction.id == tx_id,
            BankTransaction.company_id == company_id,
        )
    )
    tx = result.scalar_one_or_none()
    if not tx:
        raise HTTPException(status_code=404, detail="Transakcja nie znaleziona.")
    return tx
