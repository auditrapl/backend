"""
Matching transakcji bankowych z fakturami.
Algorytm: kwota (40%) + NIP w tytule (35%) + numer faktury w tytule (25%).
Próg: ≥0.40 → suggested match.
"""
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models import BankTransaction, Invoice

logger = logging.getLogger(__name__)

THRESHOLD = 0.40  # minimum score do suggested match


async def auto_match_transactions(
    db: AsyncSession,
    company_id: uuid.UUID,
    transaction_ids: list[uuid.UUID] | None = None,
) -> dict:
    """
    Dla każdej unmatched transakcji znajdź najlepszą fakturę.
    Zwraca: { matched: int, unmatched: int, total: int }
    """
    # Pobierz transakcje do matchowania
    query = (
        select(BankTransaction)
        .where(
            BankTransaction.company_id == company_id,
            BankTransaction.match_status == "unmatched",
        )
    )
    if transaction_ids:
        query = query.where(BankTransaction.id.in_(transaction_ids))

    result = await db.execute(query)
    transactions = result.scalars().all()

    if not transactions:
        return {"matched": 0, "unmatched": 0, "total": 0}

    # Pobierz nieopłacone faktury
    inv_result = await db.execute(
        select(Invoice)
        .where(
            Invoice.company_id == company_id,
            Invoice.is_paid == False,
            Invoice.gross_amount.isnot(None),
        )
    )
    unpaid_invoices = inv_result.scalars().all()

    # Zbiór już dopasowanych invoice_ids (żeby nie matchować jednej faktury dwa razy)
    matched_invoice_ids: set[uuid.UUID] = set()

    matched_count = 0
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    for tx in transactions:
        best_invoice = None
        best_score = 0.0

        tx_title = (tx.title or "").strip()
        tx_amount = abs(float(tx.amount)) if tx.amount else 0.0

        for inv in unpaid_invoices:
            if inv.id in matched_invoice_ids:
                continue

            score = 0.0
            inv_gross = float(inv.gross_amount) if inv.gross_amount else 0.0

            # 1. Kwota dokładna ±0.01 → +0.40
            if inv_gross > 0 and abs(tx_amount - inv_gross) < 0.02:
                score += 0.40

            # 2. NIP kontrahenta w tytule → +0.35
            if inv.contractor_nip and inv.contractor_nip in tx_title:
                score += 0.35
            elif inv.contractor_name and len(inv.contractor_name) > 3:
                if inv.contractor_name.lower() in tx_title.lower():
                    score += 0.25  # nazwa mniej pewna niż NIP

            # 3. Numer faktury w tytule → +0.25
            if inv.internal_number and inv.internal_number in tx_title:
                score += 0.25
            elif inv.ksef_number and inv.ksef_number in tx_title:
                score += 0.25

            if score > best_score:
                best_score = score
                best_invoice = inv

        if best_invoice and best_score >= THRESHOLD:
            tx.matched_invoice_id = best_invoice.id
            tx.match_confidence = round(best_score, 2)
            tx.match_status = "suggested"
            tx.matched_by = "auto"
            tx.matched_at = now
            matched_invoice_ids.add(best_invoice.id)
            matched_count += 1

    await db.commit()

    return {
        "matched": matched_count,
        "unmatched": len(transactions) - matched_count,
        "total": len(transactions),
    }
