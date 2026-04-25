"""
Parser CSV wyciągów bankowych — auto-detect formatu (mBank, ING, PKO, Millennium, generic).
"""
import csv
import io
import logging
import re
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from typing import Optional

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class ParsedTransaction(BaseModel):
    transaction_date: date
    amount: Decimal
    title: str
    counterparty_name: Optional[str] = None
    counterparty_account: Optional[str] = None
    reference: Optional[str] = None
    balance_after: Optional[Decimal] = None


# ==============================================================================
# FORMAT DETECTION
# ==============================================================================

# Nagłówki charakterystyczne dla banków
_SIGNATURES = {
    "mbank": ["data operacji", "opis operacji", "kwota"],
    "ing": ["data transakcji", "dane kontrahenta", "kwota"],
    "pko": ["data operacji", "typ transakcji", "kwota"],
    "millennium": ["data księgowania", "opis", "kwota"],
    "santander": ["data operacji", "odbiorca/nadawca", "kwota"],
}


def _detect_format(header_row: list[str]) -> str:
    """Wykryj bank na podstawie nagłówków CSV."""
    # Usuń znaki # z nagłówków (mBank prefixuje #Data operacji)
    normalized = [h.strip().lstrip("#").strip().lower() for h in header_row]
    header_text = " ".join(normalized)

    for bank, signatures in _SIGNATURES.items():
        matches = sum(1 for sig in signatures if sig in header_text)
        if matches >= 2:
            return bank

    return "generic"


def _parse_decimal(value: str) -> Decimal | None:
    """Parsuj kwotę z polskiego formatu (spacja/kropka jako tysiące, przecinek jako dziesiętne)."""
    if not value or not value.strip():
        return None
    cleaned = value.strip()
    # Usuń walutę
    cleaned = re.sub(r'[A-Za-z]', '', cleaned).strip()
    # Zamień spacje i kropki-tysięczne
    cleaned = cleaned.replace(' ', '').replace('\xa0', '')
    # Jeśli jest przecinek i kropka, to kropka = tysiące, przecinek = dziesiętne
    if ',' in cleaned and '.' in cleaned:
        cleaned = cleaned.replace('.', '').replace(',', '.')
    elif ',' in cleaned:
        cleaned = cleaned.replace(',', '.')
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


def _parse_date(value: str) -> date | None:
    """Parsuj datę z popularnych formatów."""
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%Y.%m.%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


# ==============================================================================
# PARSERY PER BANK
# ==============================================================================

def _find_col(headers: list[str], *candidates: str) -> int | None:
    """Znajdź indeks kolumny po nazwie (case-insensitive, partial match, strips #)."""
    for i, h in enumerate(headers):
        h_lower = h.strip().lstrip("#").strip().lower()
        for c in candidates:
            if c in h_lower:
                return i
    return None


def _parse_rows(headers: list[str], rows: list[list[str]], bank: str) -> list[ParsedTransaction]:
    """Parsuj wiersze CSV na transakcje — wspólna logika."""
    # Znajdź kolumny
    date_col = _find_col(headers, "data operacji", "data transakcji", "data księgowania", "data", "date")
    amount_col = _find_col(headers, "kwota", "amount")
    title_col = _find_col(headers, "opis operacji", "tytuł", "opis", "tytul", "title", "description")
    counterparty_col = _find_col(headers, "dane kontrahenta", "odbiorca", "nadawca", "kontrahent", "nazwa")
    account_col = _find_col(headers, "rachunek", "numer rachunku", "nr rachunku", "numer konta", "account")
    balance_col = _find_col(headers, "saldo", "saldo po", "balance")
    ref_col = _find_col(headers, "referencj", "nr ref", "reference")
    # mBank: osobne kolumny wpływy/wydatki zamiast jednej "kwota"
    inflow_col = _find_col(headers, "wpływy", "wpływ", "uznania")
    outflow_col = _find_col(headers, "wydatki", "wydatek", "obciążenia")

    # Kwota: albo jedna kolumna "kwota", albo dwie (wpływy/wydatki)
    has_amount = amount_col is not None
    has_split = inflow_col is not None or outflow_col is not None

    if date_col is None or (not has_amount and not has_split):
        logger.warning(f"[{bank}] Nie znaleziono kolumny daty lub kwoty w: {[h.strip() for h in headers]}")
        return []

    results: list[ParsedTransaction] = []

    for row in rows:
        if len(row) <= date_col:
            continue

        tx_date = _parse_date(row[date_col])
        if tx_date is None:
            continue

        # Kwota — z jednej kolumny lub z wpływy/wydatki
        amount: Decimal | None = None
        if has_amount and amount_col is not None and amount_col < len(row):
            amount = _parse_decimal(row[amount_col])

        if amount is None and has_split:
            # Sprawdź wpływy (dodatnie)
            if inflow_col is not None and inflow_col < len(row):
                val = _parse_decimal(row[inflow_col])
                if val is not None and val != 0:
                    amount = abs(val)
            # Sprawdź wydatki (ujemne)
            if amount is None and outflow_col is not None and outflow_col < len(row):
                val = _parse_decimal(row[outflow_col])
                if val is not None and val != 0:
                    amount = -abs(val)

        if amount is None:
            continue

        # Tytuł — złóż z dostępnych kolumn
        title_parts = []
        if title_col is not None and title_col < len(row):
            title_parts.append(row[title_col].strip())
        if not title_parts and counterparty_col is not None and counterparty_col < len(row):
            title_parts.append(row[counterparty_col].strip())
        title = " ".join(p for p in title_parts if p) or "Brak opisu"

        counterparty = None
        if counterparty_col is not None and counterparty_col < len(row):
            counterparty = row[counterparty_col].strip() or None

        account = None
        if account_col is not None and account_col < len(row):
            account = row[account_col].strip() or None

        balance = None
        if balance_col is not None and balance_col < len(row):
            balance = _parse_decimal(row[balance_col])

        reference = None
        if ref_col is not None and ref_col < len(row):
            reference = row[ref_col].strip() or None

        results.append(ParsedTransaction(
            transaction_date=tx_date,
            amount=amount,
            title=title,
            counterparty_name=counterparty,
            counterparty_account=account,
            reference=reference,
            balance_after=balance,
        ))

    return results


# ==============================================================================
# PUBLICZNY INTERFEJS
# ==============================================================================

def parse_bank_csv(content: bytes, filename: str = "") -> list[ParsedTransaction]:
    """
    Parsuj CSV z banku. Auto-detect formatu.
    Obsługuje: mBank, ING, PKO BP, Millennium, Santander, generic.
    """
    # Próbuj różne encodingi
    text = None
    for enc in ("utf-8", "cp1250", "latin-1", "utf-8-sig"):
        try:
            text = content.decode(enc)
            break
        except (UnicodeDecodeError, ValueError):
            continue

    if text is None:
        logger.error(f"Cannot decode CSV: {filename}")
        return []

    # CSV — próbuj różne separatory
    best_rows: list[list[str]] = []
    best_delimiter = ";"
    for delimiter in (";", ",", "\t"):
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        rows = list(reader)
        # Wybierz separator który daje najwięcej kolumn w jakimkolwiek wierszu
        max_cols = max((len(r) for r in rows), default=0)
        if max_cols > max((len(r) for r in best_rows), default=0):
            best_rows = rows
            best_delimiter = delimiter

    rows = best_rows
    if len(rows) < 2:
        logger.error(f"Cannot parse CSV: {filename}")
        return []

    # Szukaj wiersza-nagłówka — skanuj do 50 linii (mBank ma ~20 linii metadanych)
    # Nagłówek musi mieć ≥5 kolumn i zawierać "data" w co najmniej jednej kolumnie
    header_idx = None
    for i, row in enumerate(rows[:50]):
        non_empty = sum(1 for c in row if c.strip())
        if non_empty < 5:
            continue
        # Sprawdź czy którakolwiek kolumna zawiera "data" (kluczowe dla nagłówka)
        cols_lower = [c.strip().lstrip("#").strip().lower() for c in row]
        has_date_col = any("data" in c for c in cols_lower)
        has_amount_col = any(kw in c for c in cols_lower for kw in ["kwota", "amount", "wpływy", "wydatki"])
        if has_date_col and has_amount_col:
            header_idx = i
            break

    if header_idx is None:
        # Fallback: pierwszy wiersz z ≥6 niepustymi komórkami
        for i, row in enumerate(rows[:50]):
            non_empty = sum(1 for c in row if c.strip())
            if non_empty >= 6:
                header_idx = i
                break

    if header_idx is None:
        logger.error(f"Cannot find header row in CSV: {filename}")
        return []

    headers = rows[header_idx]
    data_rows = rows[header_idx + 1:]

    bank = _detect_format(headers)
    logger.info(f"Bank CSV detected: {bank} (file: {filename}, header at line {header_idx}, delimiter='{best_delimiter}', data rows: {len(data_rows)})")
    logger.info(f"Headers: {[h.strip() for h in headers if h.strip()]}")

    transactions = _parse_rows(headers, data_rows, bank)
    logger.info(f"Parsed {len(transactions)} transactions from {filename}")

    return transactions
