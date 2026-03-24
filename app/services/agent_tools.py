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
            contractor_name, net_amount, gross_amount, vat_amount, issue_date,
            is_paid, status, category, department_id, risk_score,
            ai_analysis_json (JSONB z polem risk_score).
        Tabela `departments`: id, company_id, name, monthly_limit.

        Zwraca wyniki jako JSON string (max 50 wierszy).
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
        if "LIMIT" not in query_upper:
            query = query.rstrip().rstrip(';') + " LIMIT 50"

        # ── 5. Execute with timeout ──
        try:
            async with engine.connect() as conn:
                await conn.execute(
                    text(f"SET statement_timeout = '{SQL_STATEMENT_TIMEOUT_S}s'")
                )
                result = await conn.execute(text(query))
                rows = [dict(zip(result.keys(), row)) for row in result.fetchall()]

                logger.info(f"[SQL_RESULT] {len(rows)} row(s) returned")
                return json.dumps(rows, default=str, ensure_ascii=False)

        except Exception as e:
            logger.error(f"[SQL_ERROR] {str(e)}")
            return json.dumps({"error": f"Błąd SQL: {str(e)}"})

    return execute_sql_query


def make_search_corporate_memory(company_id: str) -> Callable:
    async def search_corporate_memory(query: str) -> str:
        """
        Przeszukuje pamięć korporacyjną firmy (CompanyContext).
        Parametr 'query' opisuje czego szukasz — kontekst firmy zostanie zwrócony jako JSON.
        Zawiera: profil firmy, sytuację finansową, unit economics, ryzyka, cele strategiczne.
        """
        logger.info(f"[MEMORY_TOOL] company={company_id[:8]}.. query={query[:200]}")

        try:
            async with AsyncSessionLocal() as db:
                context_record = await get_or_create_context(db, uuid.UUID(company_id))
                data = context_record.context_data

                if not data:
                    return json.dumps({
                        "info": "Pamięć korporacyjna jest pusta. Firma nie ma jeszcze zaindeksowanych dokumentów.",
                        "hint": "Poproś użytkownika o upload dokumentów finansowych (bilans, PnL, umowy)."
                    })

                logger.info(f"[MEMORY_RESULT] Returned context with {len(json.dumps(data, default=str))} chars")
                return json.dumps(data, default=str, ensure_ascii=False)

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
