import json
import logging
import uuid
from typing import Callable, Any

from sqlalchemy import text
from app.database import engine
from app.services.context_manager import get_or_create_context
from app.database import AsyncSessionLocal

logger = logging.getLogger(__name__)

# Max execution time for a single SQL query (seconds)
SQL_STATEMENT_TIMEOUT_S = 10

# ── Two-tier cache for corporate memory ──
# L1 (canonical: NIP, model biznesowy, strategia) — zmienia się raz na kilka tygodni.
# L2 (operational: SafeCash, metryki faktur) — zmienia się po każdej fakturze.
# Assembled text = L1 + L2 + L3 → cache z krótszym TTL (bo zawiera L2).
# Ale invalidation L2 dzieje się tylko po invoice change (background task),
# nie w trakcie czatu — więc 5 min jest bezpieczne.
_memory_cache: dict[str, tuple[float, str]] = {}  # company_id → (timestamp, assembled_text)
_MEMORY_CACHE_TTL = 300.0  # 5 min — pokrywa wielokrotne tool calls w 1 rozmowie

def invalidate_memory_cache(company_id: str):
    """Call after invoice changes or context updates to force fresh data."""
    _memory_cache.pop(company_id, None)


FORBIDDEN_SQL_KEYWORDS = [
    "DROP ", "DELETE ", "UPDATE ", "INSERT ", "ALTER ", "TRUNCATE ",
    "GRANT ", "REVOKE ", "CREATE ", "EXEC ", "EXECUTE ",
]


def make_execute_sql_query(company_id: str) -> Callable:
    async def execute_sql_query(query: str) -> str:
        """
        Wykonuje zapytanie PostgreSQL (tylko odczyt - SELECT/WITH).
        WAŻNE: Każde zapytanie MUSI zawierać filtr company_id = '<company_id>'.

        Tabela `invoices`: id, company_id, invoice_type ('expense'/'income'),
            contractor_name, contractor_nip, net_amount, gross_amount, vat_amount,
            issue_date, due_date, is_paid, status, category_name, category_source,
            department_id, risk_score, ai_analysis_json (JSONB).
            UWAGA: category_source = 'manual' (user przypisal) | 'ai_inferred' (AI zgadl) | 'ksef_extracted' (z KSeF).
            Jesli category_source = 'ai_inferred' → to HIPOTEZA, nie fakt. Oznacz w odpowiedzi.
        Tabela `departments`: id, company_id, name, description, monthly_limit.

        Zwraca JSON: {"rows": [...], "row_count": N, "truncated": bool, "applied_limit": int, "warning": ...}.
        Jeśli truncated=true, wynik jest fragmentem. NIE licz SUM/AVG/TOP na takim zbiorze —
        uruchom osobne zapytanie z SUM()/COUNT() bez LIMIT. Inaczej dostaniesz halucynację agregatu.
        """
        logger.info(f"[SQL_TOOL] company={company_id[:8]}.. query={query[:200]}")

        # ── 1. Read-only check ──
        query_upper = query.strip().upper()
        if not query_upper.startswith("SELECT") and not query_upper.startswith("WITH"):
            return json.dumps({
                "error": "Dozwolone są wyłącznie zapytania SELECT lub CTE (WITH ... SELECT)."
            })

        # ── 2. Forbidden keywords ──
        for kw in FORBIDDEN_SQL_KEYWORDS:
            if kw in query_upper:
                return json.dumps({
                    "error": f"Wykryto niedozwolone słowo kluczowe: {kw.strip()}. Zapytanie zablokowane."
                })

        # ── 3. ENFORCE company_id (hard reject, not just a warning) ──
        # Normalize UUIDs: strip dashes for comparison
        cid_normalized = company_id.replace('-', '').lower()
        query_normalized = query.replace('-', '').replace("'", "").replace('"', '').lower()
        if cid_normalized not in query_normalized:
            return json.dumps({
                "error": (
                    f"SECURITY: Zapytanie MUSI zawierać filtr po company_id = '{company_id}'. "
                    f"Dodaj WHERE company_id = '{company_id}' do zapytania i spróbuj ponownie."
                )
            })

        # ── 4. Enforce LIMIT (prevent full table scans) ──
        applied_limit: int | None = None
        if "LIMIT" not in query_upper:
            applied_limit = 50
            query = query.rstrip().rstrip(';') + f" LIMIT {applied_limit}"
        else:
            # Extract LIMIT value if user-supplied, for truncation signalling
            import re as _re
            m = _re.search(r"LIMIT\s+(\d+)", query_upper)
            if m:
                try:
                    applied_limit = int(m.group(1))
                except ValueError:
                    applied_limit = None

        # ── 5. Execute with timeout ──
        try:
            async with engine.connect() as conn:
                await conn.execute(
                    text(f"SET statement_timeout = '{SQL_STATEMENT_TIMEOUT_S}s'")
                )
                result = await conn.execute(text(query))
                rows = [dict(zip(result.keys(), row)) for row in result.fetchall()]

                # ── Truncation signalling (anti-hallucination) ──
                # If we returned exactly LIMIT rows, caller must ASSUME more exist.
                # Agent MUST NOT compute SUM/AVG/TOP on truncated data without
                # re-querying with an explicit aggregate.
                truncated = applied_limit is not None and len(rows) >= applied_limit
                warning = None
                if truncated:
                    warning = (
                        f"DANE UCIĘTE do {applied_limit} wierszy. Pełny zbiór jest większy. "
                        f"NIE licz SUM/AVG/TOP/COUNT na tej próbce — uruchom osobne zapytanie "
                        f"z SUM()/AVG()/COUNT() bez LIMIT, albo powiedz userowi 'Zestawienie "
                        f"ograniczone do {applied_limit} pozycji, pełne wartości niedostępne.'"
                    )

                payload = {
                    "rows": rows,
                    "row_count": len(rows),
                    "truncated": truncated,
                    "applied_limit": applied_limit,
                }
                if warning:
                    payload["warning"] = warning

                logger.info(
                    f"[SQL_RESULT] {len(rows)} row(s) returned, truncated={truncated}"
                )
                return json.dumps(payload, default=str, ensure_ascii=False)

        except Exception as e:
            logger.error(f"[SQL_ERROR] {str(e)}")
            return json.dumps({"error": f"Błąd SQL: {str(e)}"})

    return execute_sql_query


async def get_category_coverage(company_id: str) -> dict:
    """Sprawdza jaki % faktur ma przypisaną kategorię/oddział. Deterministic, zero AI."""
    from app.database import AsyncSessionLocal
    from sqlalchemy import text

    async with AsyncSessionLocal() as db:
        result = await db.execute(text("""
            SELECT
                COUNT(*) AS total,
                COUNT(department_id) AS with_department,
                COUNT(CASE WHEN category_source = 'manual' THEN 1 END) AS manual_category,
                COUNT(CASE WHEN category_source = 'ai_inferred' THEN 1 END) AS ai_category,
                COUNT(category_name) AS with_any_category
            FROM invoices
            WHERE company_id = :cid
        """), {"cid": company_id})
        row = result.fetchone()
        if not row or row.total == 0:
            return {"total": 0, "coverage_pct": 0, "can_show_structure": False}

        coverage = (row.with_department + row.manual_category) / row.total
        return {
            "total": row.total,
            "with_department": row.with_department,
            "manual_category": row.manual_category,
            "ai_category": row.ai_category,
            "coverage_pct": round(coverage * 100, 1),
            "can_show_structure": coverage >= 0.5,
        }


def make_search_corporate_memory(company_id: str) -> Callable:
    async def search_corporate_memory(query: str) -> str:
        """
        Przeszukuje pamięć korporacyjną firmy (3-warstwowy kontekst: kanoniczny + operacyjny + snapshoty).
        Parametr 'query' opisuje czego szukasz.
        Zawiera: profil firmy, SafeCash (gotówka z faktur), MRR, burn rate,
        należności, zobowiązania, runway, ryzyka, cele strategiczne.
        SafeCash to TWARDE ŹRÓDŁO PRAWDY o gotówce — wyliczane automatycznie z opłaconych faktur.
        """
        import time as _time
        logger.info(f"[MEMORY_TOOL] company={company_id[:8]}.. query={query[:200]}")

        # ── Cache check (TTL 60s) — instant calls reuse cached context ──
        cached = _memory_cache.get(company_id)
        if cached:
            ts, text = cached
            if (_time.time() - ts) < _MEMORY_CACHE_TTL:
                logger.info(f"[MEMORY_CACHE_HIT] company={company_id[:8]}.. ({len(text)} chars)")
                return text

        try:
            from app.services.context_assembler import assemble_full_context_text
            async with AsyncSessionLocal() as db:
                context_text = await assemble_full_context_text(
                    db, uuid.UUID(company_id), include_snapshots=True
                )

                if not context_text or context_text == "Brak kontekstu firmowego.":
                    return json.dumps({
                        "info": "Pamięć korporacyjna jest pusta. Firma nie ma jeszcze zaindeksowanych dokumentów.",
                        "hint": "Poproś użytkownika o upload dokumentów finansowych (bilans, PnL, umowy)."
                    })

                # ── Cache store ──
                _memory_cache[company_id] = (_time.time(), context_text)
                logger.info(f"[MEMORY_RESULT] Returned + cached context ({len(context_text)} chars)")
                return context_text

        except Exception as e:
            logger.error(f"[MEMORY_ERROR] {str(e)}")
            return json.dumps({"error": f"Błąd pamięci korporacyjnej: {str(e)}"})

    return search_corporate_memory


def make_search_documents(company_id: str) -> Callable:
    async def search_documents(document_type: str = "", query: str = "") -> str:
        """
        Przeszukuje zaindeksowane dokumenty firmy (faktury, umowy, wyciągi, bilanse itp.).
        Zwraca AI summaries (typ dokumentu, wartość finansowa, terminy, ryzyka).

        Parametry:
        - document_type: opcjonalny filtr po typie dokumentu (np. 'Faktura', 'Bilans', 'Umowa').
                         Zostaw pusty żeby szukać we wszystkich typach.
        - query: opcjonalny tekst do wyszukania w nazwie pliku.

        Zwraca max 10 najnowszych pasujących dokumentów z ich AI summaries.
        Jeśli potrzebujesz pełnej treści dokumentu, użyj narzędzia get_document_content z nazwą pliku.
        """
        logger.info(f"[DOCS_TOOL] company={company_id[:8]}.. type={document_type!r} query={query!r}")

        try:
            from sqlalchemy import select
            from app.models import CompanyDocument

            async with AsyncSessionLocal() as db:
                stmt = (
                    select(CompanyDocument)
                    .where(CompanyDocument.company_id == uuid.UUID(company_id))
                )

                if document_type.strip():
                    stmt = stmt.where(
                        CompanyDocument.document_type.ilike(f"%{document_type.strip()}%")
                    )

                if query.strip():
                    stmt = stmt.where(
                        CompanyDocument.file_name.ilike(f"%{query.strip()}%")
                    )

                stmt = stmt.order_by(CompanyDocument.created_at.desc()).limit(10)
                result = await db.execute(stmt)
                docs = result.scalars().all()

                if not docs:
                    return json.dumps({
                        "info": "Nie znaleziono dokumentów pasujących do kryteriów.",
                        "hint": "Spróbuj zmienić kryteria wyszukiwania lub zostaw oba parametry puste żeby zobaczyć wszystkie dokumenty."
                    })

                items = []
                for doc in docs:
                    summary = doc.ai_summary or {}
                    items.append({
                        "file_name": doc.file_name,
                        "document_type": doc.document_type or summary.get("typ_dokumentu", "Nieznany"),
                        "wartosc_finansowa": summary.get("wartosc_finansowa", "N/A"),
                        "krytyczne_terminy": summary.get("krytyczne_terminy", "N/A"),
                        "anomalie_lub_ryzyka": summary.get("anomalie_lub_ryzyka", "Brak"),
                        "ai_reasoning": summary.get("ai_reasoning", "Brak"),
                        "indexed_at": str(doc.created_at) if doc.created_at else None,
                    })

                logger.info(f"[DOCS_RESULT] {len(items)} document(s) found")
                return json.dumps(items, default=str, ensure_ascii=False)

        except Exception as e:
            logger.error(f"[DOCS_ERROR] {str(e)}")
            return json.dumps({"error": f"Błąd wyszukiwania dokumentów: {str(e)}"})

    return search_documents


def make_get_document_content(company_id: str) -> Callable:
    async def get_document_content(file_name: str) -> str:
        """
        Pobiera pełną treść tekstową dokumentu firmy (wyekstrahowaną z PDF/CSV/XML).
        Użyj tego narzędzia gdy AI summary z search_documents nie daje wystarczających szczegółów
        i potrzebujesz przeczytać cały dokument.

        Parametr file_name: dokładna nazwa pliku (z search_documents).
        Zwraca surowy tekst dokumentu (max 30 000 znaków).
        """
        logger.info(f"[CONTENT_TOOL] company={company_id[:8]}.. file={file_name}")

        try:
            from sqlalchemy import select
            from app.models import Document
            from app.services.webdav_service import WebDAVService
            from app.services.document_indexer import extract_text_from_file

            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(Document)
                    .where(
                        Document.company_id == uuid.UUID(company_id),
                        Document.filename == file_name
                    )
                    .limit(1)
                )
                doc = result.scalar_one_or_none()

                if not doc:
                    return json.dumps({
                        "error": f"Nie znaleziono pliku '{file_name}' w dokumentach firmy."
                    })

                # Pobierz plik z WebDAV/local storage
                webdav = WebDAVService()
                file_bytes = b""
                async for chunk in webdav.stream_file(doc.filepath):
                    file_bytes += chunk

                if not file_bytes:
                    return json.dumps({"error": "Plik jest pusty lub nie udało się go pobrać."})

                # Wyekstrahuj tekst
                text_content = await extract_text_from_file(file_bytes, file_name)

                # Limit 30k znaków żeby nie zapchać kontekstu
                if len(text_content) > 30_000:
                    text_content = text_content[:30_000] + "\n\n... (treść skrócona do 30 000 znaków)"

                logger.info(f"[CONTENT_RESULT] Extracted {len(text_content)} chars from {file_name}")
                return text_content

        except Exception as e:
            logger.error(f"[CONTENT_ERROR] {str(e)}")
            return json.dumps({"error": f"Błąd pobierania treści dokumentu: {str(e)}"})

    return get_document_content
