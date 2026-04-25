"""
Mały Agent Indeksujący — Document Indexer Service
Przetwarza uploadowane pliki (PDF, CSV, XML, TXT) przez gemini-2.5-flash,
wyciąga twarde fakty finansowe i zapisuje skompresowany JSON do bazy.
"""
import os
import io
import json
import logging
from uuid import UUID

from google import genai
from google.genai import types

from app.database import AsyncSessionLocal
from app.models import CompanyDocument
from app.schemas import DocumentSummaryOutput
from app.prompts.cfo_prompts import DOCUMENT_INDEXER_PROMPT

logger = logging.getLogger(__name__)

INDEXER_MODEL = "gemini-2.5-flash"


# ─────────────────────────────────────────────────────────────────────────────
# TEXT EXTRACTION (multi-format)
# ─────────────────────────────────────────────────────────────────────────────

async def extract_text_from_file(file_bytes: bytes, file_name: str) -> str:
    """
    Wyciąga surowy tekst z pliku na podstawie rozszerzenia.
    Obsługiwane formaty: PDF, CSV, XML, TXT, JSON.
    """
    ext = os.path.splitext(file_name)[1].lower()
    
    try:
        if ext == ".pdf":
            return _extract_pdf(file_bytes)
        elif ext == ".csv":
            return file_bytes.decode("utf-8", errors="replace")
        elif ext == ".xml":
            return file_bytes.decode("utf-8", errors="replace")
        elif ext == ".json":
            return file_bytes.decode("utf-8", errors="replace")
        elif ext in (".txt", ".md"):
            return file_bytes.decode("utf-8", errors="replace")
        else:
            # Fallback: próbuj jako plain text
            return file_bytes.decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning(f"[DocumentIndexer] Nie udało się wyciągnąć tekstu z {file_name}: {e}")
        return f"[BŁĄD EKSTRAKCJI] Nie udało się odczytać pliku {file_name}: {str(e)}"


def _extract_pdf(file_bytes: bytes) -> str:
    """Ekstrakcja tekstu z PDF za pomocą pdfplumber (lepszy na tabelach niż PyPDF2)."""
    try:
        import pdfplumber
        text_parts = []
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for i, page in enumerate(pdf.pages):
                page_text = page.extract_text() or ""
                # Wyciągnij też tabele jeśli są
                tables = page.extract_tables()
                table_text = ""
                for table in tables:
                    for row in table:
                        cells = [str(c) if c else "" for c in row]
                        table_text += " | ".join(cells) + "\n"
                
                combined = page_text
                if table_text:
                    combined += f"\n[TABELA ze strony {i+1}]:\n{table_text}"
                text_parts.append(combined)
                
                # Limit: max 30 stron żeby nie zabić kontekstu
                if i >= 29:
                    text_parts.append(f"[...pominięto {len(pdf.pages) - 30} kolejnych stron...]")
                    break
        
        return "\n\n".join(text_parts)
    except ImportError:
        logger.warning("[DocumentIndexer] pdfplumber nie jest zainstalowany. Fallback na PyPDF2.")
        try:
            from PyPDF2 import PdfReader
            reader = PdfReader(io.BytesIO(file_bytes))
            text_parts = []
            for i, page in enumerate(reader.pages[:30]):
                text_parts.append(page.extract_text() or "")
            return "\n\n".join(text_parts)
        except ImportError:
            return "[BŁĄD] Brak biblioteki do parsowania PDF (zainstaluj pdfplumber lub PyPDF2)."


# ─────────────────────────────────────────────────────────────────────────────
# GEMINI INDEXER (Structured Outputs)
# ─────────────────────────────────────────────────────────────────────────────

async def _call_gemini_indexer(text_content: str, file_name: str) -> dict:
    """
    Wywołuje gemini-2.5-flash z Structured Outputs, żeby wyciągnąć
    DocumentSummaryOutput z surowego tekstu dokumentu.
    """
    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("Brak klucza API Google (GOOGLE_API_KEY / GEMINI_API_KEY)")
    client = genai.Client(api_key=api_key)
    
    user_prompt = f"""== NAZWA PLIKU ==
{file_name}

== SUROWY TEKST DOKUMENTU (pierwsze 50,000 znaków) ==
{text_content[:50000]}

Zadanie: Wyciągnij twarde fakty finansowe z powyższego tekstu i sklasyfikuj dokument.
"""
    
    response = await client.aio.models.generate_content(
        model=INDEXER_MODEL,
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=DOCUMENT_INDEXER_PROMPT,
            response_mime_type="application/json",
            response_schema=DocumentSummaryOutput,
            temperature=0.2  # Niska temperatura = precyzyjne fakty
        )
    )
    
    from app.services._json_parse import safe_model_validate_json
    parsed = safe_model_validate_json(DocumentSummaryOutput, response.text)
    return parsed.model_dump()


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ENTRY POINT: Process & Save
# ─────────────────────────────────────────────────────────────────────────────

async def process_and_save_document(
    company_id: str,
    file_bytes: bytes,
    file_name: str,
    file_url: str
) -> CompanyDocument | None:
    """
    Główna funkcja Małego Agenta Indeksującego.
    1. Wyciąga tekst z pliku (PDF/CSV/XML/TXT)
    2. Wysyła do gemini-2.5-flash → Structured Outputs → DocumentSummaryOutput
    3. Zapisuje rekord z AI summary (JSONB) do tabeli company_documents
    
    Returns: CompanyDocument lub None jeśli indeksowanie się nie powiodło.
    """
    logger.info(f"[DocumentIndexer] Rozpoczynam indeksowanie: {file_name} (company={company_id})")
    
    try:
        # Krok 1: Ekstrakcja tekstu
        raw_text = await extract_text_from_file(file_bytes, file_name)
        
        if not raw_text or len(raw_text.strip()) < 10:
            logger.warning(f"[DocumentIndexer] Plik {file_name} jest pusty lub nieczytelny.")
            ai_summary = {
                "typ_dokumentu": "Nieczytelny/Pusty plik",
                "wartosc_finansowa": "Brak danych",
                "krytyczne_terminy": "Brak danych",
                "anomalie_lub_ryzyka": "Plik jest pusty lub nieczytelny",
                "ai_reasoning": "Plik nie zawiera tekstu możliwego do wyekstrahowania."
            }
        else:
            # Krok 2: Gemini Structured Outputs
            logger.info(f"[DocumentIndexer] Wysyłam {file_name} ({len(raw_text)} znaków) do {INDEXER_MODEL}...")
            ai_summary = await _call_gemini_indexer(raw_text, file_name)
            logger.info(f"[DocumentIndexer] AI summary gotowe dla {file_name}: typ={ai_summary.get('typ_dokumentu', '?')}")
        
        # Krok 3: Zapis do PostgreSQL
        async with AsyncSessionLocal() as db:
            new_doc = CompanyDocument(
                company_id=UUID(company_id) if isinstance(company_id, str) else company_id,
                file_name=file_name,
                file_url=file_url,
                document_type=ai_summary.get("typ_dokumentu", "Nieznany"),
                ai_summary=ai_summary
            )
            db.add(new_doc)
            await db.commit()
            await db.refresh(new_doc)
            
            logger.info(f"[DocumentIndexer] Zapisano do company_documents: id={new_doc.id}, typ={new_doc.document_type}")
            return new_doc
    
    except Exception as e:
        logger.error(f"[DocumentIndexer] Błąd indeksowania {file_name}: {e}", exc_info=True)
        
        # Zapisz nawet jeśli AI failnęło — żeby mieć ślad w bazie
        try:
            async with AsyncSessionLocal() as db:
                fallback_doc = CompanyDocument(
                    company_id=UUID(company_id) if isinstance(company_id, str) else company_id,
                    file_name=file_name,
                    file_url=file_url,
                    document_type="Błąd indeksowania",
                    ai_summary={
                        "typ_dokumentu": "Błąd indeksowania",
                        "wartosc_finansowa": "N/A",
                        "krytyczne_terminy": "N/A",
                        "anomalie_lub_ryzyka": f"Błąd podczas przetwarzania: {str(e)[:200]}",
                        "ai_reasoning": "Agent Indeksujący napotkał błąd i nie był w stanie przetworzyć pliku."
                    }
                )
                db.add(fallback_doc)
                await db.commit()
                return fallback_doc
        except Exception as db_err:
            logger.error(f"[DocumentIndexer] Nie udało się zapisać fallback rekordu: {db_err}")
            return None
