"""
DEPRECATED — Ten plik NIE jest używany przez żaden router/endpoint.
Frontend używa agentic_cfo.py → agentic_cfo_stream() (via conversations.py i cfo.py).
Zachowany tymczasowo jako referencja architektury MoE + text-to-SQL.
Do usunięcia po pełnej migracji na agentic flow.
"""
import os
import json
import logging
import asyncio
from datetime import datetime
from uuid import UUID
from typing import Literal
from pydantic import BaseModel, Field

from google import genai
from google.genai import types

from app.schemas import (
    CfoReport, CfoRequest, CfoScenarioAnalysis, CfoActionPlan, 
    CfoDataSource, MajorCfoLayoutOutput
)
from app.services.strategy_agent import generate_sql, execute_sql
from app.services.webdav_service import WebDAVService


# ─── INTELLIGENT DISPATCHER (DYNAMIC MULTI-AGENT ROUTING) ─────────────────────

AGENT_KEYS = Literal["VP_GROWTH", "RISK_OFFICER", "ACTUARY", "TAX_STRATEGIST", "INFO_RETRIEVER"]


class ActiveAgents(BaseModel):
    selected_agents: list[AGENT_KEYS]


_DISPATCHER_SYSTEM_PROMPT = (
    "Jesteś Dyspozytorem Zarządu. Przeczytaj zapytanie CEO i wybierz pasujących ekspertów.\n"
    "- VP_GROWTH: inwestycje, skalowanie, zwiększanie przychodów, marketing, zatrudnianie handlowców.\n"
    "- RISK_OFFICER: ocena ryzyka, zatory płatnicze, kryzysy cashflow, dylematy strategiczne.\n"
    "- ACTUARY: symulacje czasowe z danymi historycznymi, projekcje cashflow, runway, break-even.\n"
    "- TAX_STRATEGIST: optymalizacja VAT/CIT, analiza kosztów, efektywność faktur.\n"
    "- INFO_RETRIEVER: wybierz WYŁĄCZNIE TEGO JEDNEGO agenta, "
    "jeśli zapytanie to proste pytanie o suche fakty (np. 'ile mamy gotówki', 'jaki jest nasz NIP', 'czym się zajmujemy').\n"
    "Zwróć listę kluczy pasujących ekspertów."
)


async def select_best_agents(client: genai.Client, user_prompt: str) -> list[str]:
    """
    Inteligentny Dyspozytor: wybiera 1-3 ekspertów z puli 5 agentów.
    Używa gemini-2.5-flash z temperature=0.0 dla deterministycznej odpowiedzi.
    Przy błędzie bezpiecznie wybiera pełną trójkę analityczną.
    """
    try:
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"Zapytanie CEO: {user_prompt}",
            config=types.GenerateContentConfig(
                system_instruction=_DISPATCHER_SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=ActiveAgents,
                temperature=0.0,
            ),
        )
        parsed = ActiveAgents.model_validate_json(response.text)
        selected = parsed.selected_agents
        logger.info(f"[CFO Dispatcher] Wybranych agentów: {selected}")
        return selected if selected else ["VP_GROWTH", "RISK_OFFICER", "ACTUARY"]
    except Exception as e:
        logger.warning(f"[CFO Dispatcher] Błąd klasyfikacji, fallback → pełna trójka: {e}")
        return ["VP_GROWTH", "RISK_OFFICER", "ACTUARY"]

logger = logging.getLogger(__name__)

# Use gemini-3.1-pro-preview as requested
MODEL_NAME = "gemini-3.1-pro-preview" #flash

# DB Schema reference for generate_sql
DB_SCHEMA = """
Masz dostęp do następujących tabel i widoków. Twoje zapytania MUST ALWAYS filtorwać po `company_id`!

1. Tabela: companies
Kolumny:
- id (UUID)
- name (VARCHAR)
- nip (VARCHAR)
- industry (VARCHAR)
- description (TEXT)

2. Tabela: departments
Kolumny:
- id (UUID)
- company_id (UUID)
- name (VARCHAR)
- monthly_limit (NUMERIC)

3. Tabela: invoices (KLUCZOWA)
Kolumny:
- id (UUID), company_id (UUID), department_id (UUID)
- invoice_type (VARCHAR: 'expense' lub 'income')
- net_amount (numeric), vat_amount (numeric), gross_amount (numeric)
- currency (VARCHAR, default: 'PLN')
- issue_date (datetime) -- DATA WYSTAWIENIA (kluczowa do filtrowania czasu!)
- status (VARCHAR), is_paid (BOOLEAN) -- TRUE = zapłacona, FALSE = niezapłacona
- category_name (VARCHAR)
- contractor_name (VARCHAR) -- WAŻNE: używaj ILIKE '%nazwisko%' do szukania konkretnych firm/osób!
- contractor_nip (VARCHAR)
- parsed_data (JSONB) -- ZAWIERA Termin płatności: CAST(parsed_data->>'due_date' AS TIMESTAMP)
- ai_analysis_json (JSONB) -- ZAWIERA POLE risk_score (0-100)!

UWAGI:
- Status faktury określa pole `is_paid` (boolean). Kwoty licz dla poszczególnych `company_id`.

ZASADY PISANIA SQL - ZŁAM KTRÓRĄKOLWIEK, A SYSTEM WYBUCHNIE:
1. ZWRÓĆ TYLKO JEDNO ZAPYTANIE (SINGLE STATEMENT). Absolutnie zakazane jest używanie wielu zapytań oddzielonych średnikiem (;) w jednej odpowiedzi. Twój sterownik to asyncpg - wywali błąd! Jeśli potrzebujesz wielu agregacji, użyj CTE (`WITH ... SELECT`).
2. W tabeli NIE MA kolumny `risk_score`! Ta wartość jest ukryta w JSON.
   Używaj: CAST(NULLIF(ai_analysis_json->>'risk_score', '') AS INTEGER)
3. W tabeli NIE MA kolumny `due_date`!
   Jeśli musisz filtrować, sortować lub zwracać termin płatności, używaj ZAWSZE: CAST(parsed_data->>'due_date' AS TIMESTAMP)
4. [ZNAJOMOŚĆ CZASU]: Obecny rok to 2026!
5. Do filtrowania miesięcy/lat używaj kolumny `issue_date`.
6. [LOGIKA PODATKOWA - WAŻNE]: Zobowiązania wobec ZUS i Urzędu Skarbowego (VAT) płatne w bieżącym miesiącu (np. w marcu) są wystawiane na koniec POPRZEDNIEGO miesiąca. Jeśli Prezes pyta o ZUS/VAT na marzec, MUSISZ szukać z `EXTRACT(MONTH FROM issue_date) = 2` (Luty)!
7. Gdy pytają o B2B/ZUS/VAT użyj ILIKE '%Kowalski%' lub ILIKE '%ZUS%' lub ILIKE '%Urząd Skarbowy%'.
8. Gdy pytają o zatory/płynność, możesz używać widoku `v_ar_ap_aging` lub szukać `is_paid = false`.
9. Zwracaj CZYSTY KOD SQL, bez znaczników markdown.
"""

# MajorCfoOutput is now MajorCfoLayoutOutput (Generative UI) defined in schemas.py

def generate_baseline_projection(db_results: list) -> list:
    """Fallback / helper to ensure we give the LLM some structural history context to build upon."""
    if len(db_results) == 0:
         return [{"month": "Poprzedni Miesiąc", "income": 100000, "expense": 70000}]
    # Very simplified fallback
    if not dict(db_results[0]):
        return [{"month": "Poprzedni Miesiąc", "income": 100000, "expense": 70000}]
    return db_results


async def fetch_document_summaries(company_id: str, limit: int = 10) -> str:
    """
    Pobiera skompresowane podsumowania AI z tabeli company_documents.
    Zwraca sformatowany string gotowy do wstrzyknięcia w prompt Sub-Agenta.
    Limit: max `limit` najnowszych dokumentów, żeby nie zapchać kontekstu.
    """
    from app.database import AsyncSessionLocal
    from app.models import CompanyDocument
    from sqlalchemy import select
    
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(CompanyDocument)
                .where(CompanyDocument.company_id == UUID(company_id))
                .order_by(CompanyDocument.created_at.desc())
                .limit(limit)
            )
            docs = result.scalars().all()
            
            if not docs:
                return ""
            
            parts = []
            for i, doc in enumerate(docs, 1):
                summary = doc.ai_summary or {}
                parts.append(
                    f"[Dokument #{i}: {doc.file_name}]\n"
                    f"  Typ: {summary.get('typ_dokumentu', 'Nieznany')}\n"
                    f"  Wartość: {summary.get('wartosc_finansowa', 'N/A')}\n"
                    f"  Terminy: {summary.get('krytyczne_terminy', 'N/A')}\n"
                    f"  Ryzyka: {summary.get('anomalie_lub_ryzyka', 'Brak')}\n"
                    f"  Reasoning: {summary.get('ai_reasoning', 'Brak')}"
                )
            
            return "\n\n".join(parts)
    except Exception as e:
        logger.warning(f"Nie udało się pobrać document summaries: {e}")
        return ""


async def fetch_corporate_memory(company_id: str, include_snapshots: bool = True) -> str:
    """
    Pobiera 3-warstwowy kontekst firmy (L1 Kanoniczny + L2 Operacyjny + L3 Snapshoty).
    Zwraca sformatowany tekst gotowy do wstrzyknięcia w prompt agenta LLM.
    """
    from app.database import AsyncSessionLocal
    from app.services.context_assembler import assemble_full_context_text
    import uuid

    try:
        async with AsyncSessionLocal() as db:
            return await assemble_full_context_text(
                db,
                uuid.UUID(company_id),
                include_snapshots=include_snapshots,
            )
    except Exception as e:
        logger.warning(f"Nie udało się pobrać corporate memory: {e}")
        return ""


async def generate_sub_agent_analysis(client: genai.Client, system_prompt: str, user_prompt: str, baseline_str: str, doc_context_str: str = "") -> CfoScenarioAnalysis:
    """Generyczna funkcja do wywoływania pojedynczego Sub-Agenta z modelem Structured Outputs."""
    
    # Sekcja dokumentowa — dołączana TYLKO jeśli istnieją zaindeksowane dokumenty
    doc_section = ""
    if doc_context_str:
        doc_section = f"""

== KONTEKST DOKUMENTOWY (Skompresowane AI summaries z plików firmowych) ==
Poniżej znajdują się TWARDE FAKTY wyciągnięte przez Agenta Indeksującego z dokumentów firmy.
Użyj tych danych jako DODATKOWEGO kontekstu do swojej analizy. NIE IGNORUJ ICH.
{doc_context_str}
"""
    
    full_prompt = f"""
== ZAPYTANIE PREZESA ==
{user_prompt}

== KONTEKST HISTORYCZNY (SQL) ==
{baseline_str}
{doc_section}
Zadanie: Opierając się WYŁĄCZNIE na swoich priorytetach wynikających z roli (System Prompt), wygeneruj symulację na najbliższe 6 miesięcy.
W polu `internal_analysis` opisz swój tok rozumowania, dlaczego przyjąłeś takie a nie inne założenia. To jest "myślenie na głos" agenta.
Wypełnij wniosek scenariusza i wygeneruj 6 punktów wykresu `chart_data`.
"""

    response = await client.aio.models.generate_content(
        model=MODEL_NAME,
        contents=full_prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            response_schema=CfoScenarioAnalysis,
            temperature=0.4
        )
    )
    return CfoScenarioAnalysis.model_validate_json(response.text)

async def generate_major_cfo_recommendation(
    client: genai.Client,
    user_prompt: str,
    baseline_str: str,
    sub_agent_results: list[CfoScenarioAnalysis],
) -> MajorCfoLayoutOutput:
    """Główny Major CFO — Generative UI. Przyjmuje dynamiczną listę raportów Sub-Agentów."""

    from app.prompts.cfo_prompts import MAJOR_CFO_SYSTEM_PROMPT as sys_prompt

    # Dynamicznie budujemy sekcję raportów na podstawie tego, którzy agenci zostali wywołani
    agents_reports_str = "\n\n".join(
        f"== RAPORT: {res.name} ==\n{res.model_dump_json(indent=2)}"
        for res in sub_agent_results
    )

    full_prompt = f"""
== ZAPYTANIE PREZESA ==
{user_prompt}

== HISTORYCZNE DANE BAZOWE ==
{baseline_str}

{agents_reports_str}

Zadanie: Określ TRYB (Inwestycja vs Diagnostyka). Wyciągnij Złoty Środek. Ułóż tablicę `layout` wg odpowiedniego Szablonu. Pamiętaj o żelaznej kolejności klocków.
"""

    response = await client.aio.models.generate_content(
        model=MODEL_NAME,
        contents=full_prompt,
        config=types.GenerateContentConfig(
            system_instruction=sys_prompt,
            response_mime_type="application/json",
            response_schema=MajorCfoLayoutOutput,
            temperature=0.4
        )
    )
    return MajorCfoLayoutOutput.model_validate_json(response.text)



async def run_cfo_simulation_stream(request: CfoRequest, company_id: str):
    """
    Asynchroniczny generator, zwraca statusy w SSE i ostatecznie JSON symulacji.
    Wykorzystuje architekturę Mixture of Experts (3 równoległych Agentów -> 1 Major CFO).
    """
    user_prompt = request.prompt
    
    yield json.dumps({"status": "loading", "message": "Inicjuję środowisko bazodanowe Majora..."})
    await asyncio.sleep(0.1)
    
    # ─── KROK 1: POBRANIE DANYCH BAZOWYCH ───────────────────────────────────────
    try:
        yield json.dumps({"status": "loading", "message": "Generuję zapytanie SQL do weryfikacji danych historycznych..."})
        sql_query = await generate_sql(user_prompt, DB_SCHEMA, company_id)
        
        yield json.dumps({"status": "loading", "message": "Uruchamiam silnik analityczny na bazie PostgreSQL..."})
        db_results = await execute_sql(sql_query)
        baseline = generate_baseline_projection(db_results)

        # --- EXTRACT DATA SOURCES ---
        data_sources = []
        seen_signatures = set()  # Deduplikacja: pomiń duplikaty (ten sam label + kwota + data)
        
        for idx, row in enumerate(db_results):
            try:
                row_dict = dict(row)
                source_id = str(row_dict.get('id', f"row-{idx}"))
                # Date mapping FIRST so we can use it in the label fallback
                date_val = row_dict.get('issue_date') or row_dict.get('date') or row_dict.get('month')
                date_str = str(date_val)[:10] if date_val else None
                
                # Intelligent label mapping
                label = (
                    row_dict.get('contractor_name') or 
                    row_dict.get('category_name') or 
                    row_dict.get('name') or 
                    row_dict.get('invoice_number') or
                    (f"Podsumowanie Okresu: {date_str}" if date_str else f"Rekord #{idx+1}")
                )
                
                # ─── POPRAWKA: Explicit None checks zamiast łańcucha 'or' ────────
                # Priorytet 1: własna kwota wiersza (net/gross) — per-row amounts
                # Priorytet 2: kolumny agregatowe — TYLKO gdy brak kwoty per-wiersz
                # Używamy 'is not None' żeby 0.0 też był prawidłową wartością
                val = None
                for col in ('net_amount', 'gross_amount'):
                    candidate = row_dict.get(col)
                    if candidate is not None:
                        val = candidate
                        break
                
                if val is None:
                    for col in ('total_income', 'total_expense', 'monthly_balance', 'balance', 'sum', 'amount'):
                        candidate = row_dict.get(col)
                        if candidate is not None:
                            val = candidate
                            break
                # ────────────────────────────────────────────────────────────────
                
                val_str = f"{float(val):,.2f} PLN" if val is not None else "n/a"
                
                # Deduplikacja: pomiń wiersz jeśli mamy już ten sam label + kwota + data
                sig = f"{str(label)}|{val_str}|{date_str}"
                if sig in seen_signatures:
                    continue
                seen_signatures.add(sig)

                data_sources.append(CfoDataSource(
                    id=source_id,
                    type="invoice" if "invoice" in sql_query.lower() else "db_row",
                    label=str(label),
                    value=val_str,
                    date=date_str,
                    details=json.dumps(row_dict, default=str, ensure_ascii=False)
                ))
            except Exception:
                continue
            
            if len(data_sources) >= 50: # Limit context size
                break
                
    except Exception as e:
        logger.warning(f"Failed to extract direct context via Text-to-SQL for CFO Terminal. Error: {e}")
        baseline = [{"Info": "Brak precyzyjnych danych historycznych. Założenie małej/średniej firmy: przychody~150k, koszty~110k."}]
        sql_query = "Fallback"
        data_sources = []
        
    baseline_str = json.dumps(baseline, indent=2, default=str)
    # ─── KROK 1.5: POBRANIE PAMIĘCI KORPORACYJNEJ & KONTEKSTU DOKUMENTOWEGO ───────
    yield json.dumps({"status": "loading", "message": "Synchronizuję Globalną Pamięć Korporacyjną i skanuję zaindeksowane dokumenty..."})
    await asyncio.sleep(0.1)
    
    corporate_memory_str, doc_summaries_str = await asyncio.gather(
        fetch_corporate_memory(company_id),
        fetch_document_summaries(company_id)
    )
    
    # Łączymy twarde fakty (Pamięć AI) ze skrócikami z analiz RAG (Dokumenty)
    combined_context_parts = []
    if corporate_memory_str:
        combined_context_parts.append(corporate_memory_str)
        logger.info(f"[CFO] Wstrzyknięto Corporate Memory ({len(corporate_memory_str)} znaków)")
    
    if doc_summaries_str:
        combined_context_parts.append(doc_summaries_str)
        logger.info(f"[CFO] Wstrzyknięto Summaries Dokumentów ({len(doc_summaries_str)} znaków)")
        
    doc_context_str = "\n\n".join(combined_context_parts)
    
    if not doc_context_str:
        logger.info(f"[CFO] Brak zewnętrznej wiedzy (Memory/Docs) dla company_id={company_id}")
    
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

    # ─── KROK 1.7: INTELLIGENT DISPATCHER ──────────────────────────────────────
    yield json.dumps({"status": "loading", "message": "Dyspozytor analizuje zapytanie i dobiera ekspertów..."})
    await asyncio.sleep(0.05)
    selected_keys = await select_best_agents(client, user_prompt)
    logger.info(f"[CFO] Aktywni agenci: {selected_keys}")

    # ─── PULA AGENTÓW: klucz → (system_prompt, display_name) ───────────────────────
    from app.prompts.cfo_prompts import (
        AGENT_OPTIMIST_PROMPT,
        AGENT_PESSIMIST_PROMPT,
        AGENT_BASELINE_PROMPT,
        AGENT_RISK_OFFICER_PROMPT,
    )
    # Aby dodać nowego agenta: wystarczy wstawić go do tego słownika.
    try:
        from app.prompts.cfo_prompts import AGENT_TAX_STRATEGIST_PROMPT
    except ImportError:
        AGENT_TAX_STRATEGIST_PROMPT = AGENT_BASELINE_PROMPT  # fallback

    PROMPTS_MAP: dict[str, tuple[str, str]] = {
        "VP_GROWTH":      (AGENT_OPTIMIST_PROMPT,       "VP of Growth (Agresywna)"),
        "RISK_OFFICER":   (AGENT_RISK_OFFICER_PROMPT,   "Chief Risk Officer (Płynność i Ryzyko)"),
        "ACTUARY":        (AGENT_BASELINE_PROMPT,       "Główny Aktuariusz (Status Quo)"),
    }

    # ─── DUMMY dla trybu informacyjnego (INFO_RETRIEVER) ──────────────────────
    _INFO_ANALYSIS = (
        "Zapytanie informacyjne — pominięto symulację scenariuszową. "
        "Major CFO odpowie na podstawie danych SQL i Pamięci Korporacyjnej."
    )
    _DUMMY = CfoScenarioAnalysis(
        name="Info (Baza)",
        processed_raw_context=_INFO_ANALYSIS,
        internal_analysis=_INFO_ANALYSIS,
        scenario_conclusion=_INFO_ANALYSIS,
        health_score=100,
        risk_level="Niskie",
        impact_description="Tryb informacyjny — brak symulacji.",
        chart_data=[],
    )

    try:
        if selected_keys == ["INFO_RETRIEVER"] or selected_keys == []:
            # ─── TRYB INFORMACYJNY: zero LLM calli dla sub-agentów ──────────────
            yield json.dumps({"status": "loading", "message": "Tryb Informacyjny — pomijam symulacje, zbieram fakty..."})
            sub_agent_results: list[CfoScenarioAnalysis] = [_DUMMY]

        else:
            # ─── TRYB DYNAMICZNY: uruchamiamy tylko wybrane agenty ─────────────
            active_labels = ", ".join(
                PROMPTS_MAP[k][1] for k in selected_keys if k in PROMPTS_MAP
            )
            yield json.dumps({"status": "loading", "message": f"Aktywni eksperci: {active_labels}. Uruchamiam równolegle..."})
            await asyncio.sleep(0.05)

            tasks = [
                generate_sub_agent_analysis(
                    client,
                    PROMPTS_MAP[key][0],
                    user_prompt,
                    baseline_str,
                    doc_context_str,
                )
                for key in selected_keys
                if key in PROMPTS_MAP
            ]

            raw_results = await asyncio.gather(*tasks)

            # Przypisz display_name z mapy
            sub_agent_results = []
            for key, result in zip(
                [k for k in selected_keys if k in PROMPTS_MAP], raw_results
            ):
                result.name = PROMPTS_MAP[key][1]
                sub_agent_results.append(result)

        yield json.dumps({"status": "loading", "message": "Zakończono analizy cząstkowe. Przekazuję do Major CFO..."})
        await asyncio.sleep(0.1)

        # ─── KROK 3: MAJOR CFO SYNTETYZUJE ───────────────────────────────────────
        major_res = await generate_major_cfo_recommendation(
            client, user_prompt, baseline_str,
            sub_agent_results=sub_agent_results,
        )

        # Złoty Środek: średnia health score ze wszystkich agentów
        avg_health = sum(r.health_score for r in sub_agent_results) // max(len(sub_agent_results), 1)
        golden_mean_scenario = sub_agent_results[0].model_copy(update={
            "name": "Złoty Środek",
            "health_score": avg_health,
            "risk_level": "Średnio",
            "impact_description": f"Zbalansowany wariant Majora CFO. Health Score: {avg_health}/100."
        })
        
        yield json.dumps({"status": "loading", "message": "Rekomendacja Majora gotowa. Kompiluję JSON..."})

        # Agregacja toku myślenia wszystkich agentów
        aggregated_thoughts = "\n\n".join(
            f"=== {res.name} ===\n{res.internal_analysis}" for res in sub_agent_results
        ) + f"\n\n=== 🟣 MAJOR CFO (SYNTEZA + LAYOUT) ===\n{major_res.internal_thought_process}"

        # Serialize layout as raw dicts for the report (UIBlock union → dict)
        layout_dicts = [block.model_dump() for block in major_res.layout]

        # Budujemy końcowy obiekt CfoReport oczekiwany przez Frontend
        final_report = CfoReport(
            internal_thought_process=aggregated_thoughts,
            executive_summary=major_res.executive_summary,
            hero_recommendation=major_res.executive_summary,
            major_action_plan=CfoActionPlan(
                opportunities=[],
                risks=[],
                efficiency_metrics=[],
                execution_playbook=[]
            ),
            data_sources=data_sources,
            layout=layout_dicts,
            scenarios_analysis=sub_agent_results + [golden_mean_scenario],
        )
        
        try:
            webdav = WebDAVService()
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"cfo_report_{timestamp}.json"
            
            yield json.dumps({"status": "loading", "message": "Zapisuję zrzut pamięci (Karta Pamięci) oraz pełny raport na Hetzner Storage..."})
            await asyncio.sleep(0.1)
            
            report_json = final_report.model_dump_json(indent=2)
            relative_filepath = await webdav.upload_text_content(filename, report_json, company_id)
            logger.info(f"Pomyślnie zapisano pełny zrzut mózgu AI (raport CFO) na WebDAV: {filename}")
            
            yield json.dumps({"status": "loading", "message": "Tworzę wpis w Repozytorium Dokumentów..."})
            await asyncio.sleep(0.1)
            
            from app.database import AsyncSessionLocal
            from app.models import Document
            
            async with AsyncSessionLocal() as db:
                new_doc = Document(
                    company_id=UUID(company_id) if isinstance(company_id, str) else company_id,
                    filename=filename,
                    file_type="JSON Report",
                    filepath=relative_filepath,
                    size_bytes=len(report_json.encode('utf-8'))
                )
                db.add(new_doc)
                await db.commit()
                logger.info(f"Pomyślnie dodano raport CFO do bazy danych (Dokument ID: {new_doc.id})")
                
        except Exception as e:
            logger.warning(f"Nie udało się zapisać raportu CFO na WebDAV: {e}", exc_info=True)
            # Kontynuujemy pomimo błędu WebDAV
        
        # Zbieramy nazwy zaindeksowanych dokumentów do wyświetlenia w UI
        indexed_docs_meta: list[dict] = []
        if doc_context_str:
            from app.database import AsyncSessionLocal as _ASL
            from app.models import CompanyDocument as _CD
            from sqlalchemy import select as _sel
            try:
                async with _ASL() as _db:
                    _res = await _db.execute(
                        _sel(_CD.file_name, _CD.document_type, _CD.created_at)
                        .where(_CD.company_id == UUID(company_id))
                        .order_by(_CD.created_at.desc())
                        .limit(10)
                    )
                    for row in _res.all():
                        indexed_docs_meta.append({
                            "file_name": row.file_name,
                            "document_type": row.document_type or "Dokument"
                        })
            except Exception:
                pass
        
        yield json.dumps({
            "status": "success", 
            "report": final_report.model_dump(mode='json'), 
            "sql": sql_query,
            "indexed_documents": indexed_docs_meta
        }, default=str)

        
    except Exception as e:
        logger.error(f"Major CFO Multi-Agent Gen Error: {e}", exc_info=True)
        yield json.dumps({"status": "error", "message": f"Wystąpił błąd Matrixa: {str(e)}"})
