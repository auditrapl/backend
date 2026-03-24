import os
import asyncio
import json
import logging
import functools
from typing import AsyncGenerator
from google import genai
from google.genai import types

from app.schemas import (
    TriageDecision, TextResponse, DashboardResponse, MajorCfoOutput, CfoRequest, AgentTimelineStep
)
from app.services.agent_tools import make_execute_sql_query, make_search_corporate_memory, make_search_documents, make_get_document_content
from datetime import datetime

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# AFC CONFIG — Explicit. No silent black-box behavior.
# SDK v1.62.0: AFC is enabled by default for Python callables, but we make it
# explicit so the intent is clear and auditable.
# maximum_remote_calls = 10 → max 10 round-trips (tool call → tool result) per
# agent before the SDK forces a final text response.
# ─────────────────────────────────────────────────────────────────────────────
AFC_CONFIG = types.AutomaticFunctionCallingConfig(
    disable=False,
    maximum_remote_calls=10,
)
AFC_CONFIG_FAST = types.AutomaticFunctionCallingConfig(
    disable=False,
    maximum_remote_calls=5,
)

# ─────────────────────────────────────────────────────────────────────────────
# TOOL INSTRUMENTATION — wraps async tool with logging so we can physically
# see every tool call in the terminal. Preserves __doc__, __name__,
# __annotations__ (SDK uses these to build FunctionDeclaration).
# ─────────────────────────────────────────────────────────────────────────────
def instrument_tool(fn, display_name: str):
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        call_args = {**dict(zip(fn.__code__.co_varnames, args)), **kwargs}
        logger.info(
            f"\n{'='*60}\n"
            f"[AFC_TOOL_EXEC] >>> {display_name}\n"
            f"  args: {json.dumps(call_args, default=str, ensure_ascii=False)[:500]}\n"
            f"{'='*60}"
        )
        try:
            result = await fn(*args, **kwargs)
            preview = (result[:400] + "...(truncated)") if len(result) > 400 else result
            logger.info(f"[AFC_TOOL_RESULT] <<< {display_name}: {preview}")
            return result
        except Exception as e:
            logger.error(f"[AFC_TOOL_ERROR] !!! {display_name} failed: {e}", exc_info=True)
            return json.dumps({"error": f"Narzędzie '{display_name}' zwróciło błąd: {str(e)}"})
    return wrapper


def _extract_tool_calls_from_response(response) -> list[dict]:
    """Extracts confirmed tool calls from AFC history on the response object."""
    calls = []
    history = getattr(response, "automatic_function_calling_history", None)
    if not history:
        return calls
    for content in history:
        if not content.parts:
            continue
        for part in content.parts:
            fc = getattr(part, "function_call", None)
            if fc:
                calls.append({
                    "tool": fc.name,
                    "args": dict(fc.args) if fc.args else {},
                })
    return calls


# ─────────────────────────────────────────────────────────────────────────────
# SYSTEM PROMPTS
# ─────────────────────────────────────────────────────────────────────────────
TRIAGE_PROMPT = """Jesteś pierwszym punktem kontaktu (Triage) dla zapytań CEO.
Musisz sklasyfikować zapytanie i wybrać ścieżkę 'route'.

- Wybierz 'instant' DLA: Prostych pytań o suche fakty, sprawdzenie 1 liczby, kto wystawił fakturę,
  jaki był podatek, ile faktur w danym miesiącu, proste sumy, daty, statusy. Odpowiedź w 1-3 zdaniach.

- Wybierz 'analysis' DLA: Pytań wymagających analizy jednego zagadnienia — trend wydatków,
  porównanie okresów, struktura kosztów, analiza cashflow, przegląd dokumentu, analiza jednego kontrahenta.
  Wymaga formatowania i wizualizacji (wykresy, tabele).

- Wybierz 'strategic_debate' DLA: Złożonych dylematów biznesowych wymagających wielu perspektyw —
  zatrudnienie ludzi, wejście na nowy rynek, restrukturyzacja, analiza płynności z wieloma scenariuszami,
  duże decyzje inwestycyjne, pytania "co jeśli" z wieloma zmiennymi.
"""

AGENT_PESSIMIST = """Jesteś Pesymistą (Chief Risk Officer). Wszędzie wietrzysz ryzyko, opóźnienia, zatory płatnicze i brak płynności.
Bezwzględnie użyj powierzonych Ci narzędzi (SQL / Corporate Memory / Dokumenty) by zweryfikować stan faktyczny! Znajdź najgorsze momenty.
Jeśli pytanie dotyczy dokumentów firmy, użyj search_documents żeby znaleźć relewantne dokumenty, a get_document_content żeby przeczytać ich treść.
Gdy wyciągniesz wnioski, zwróć rzetelny ale pesymistyczny raport (czysty tekst)."""

AGENT_OPTIMIST = """Jesteś Optymistą (VP of Growth). Szukasz szans, możliwości inwestycji i skalowania firmy.
Bezwzględnie użyj powierzonych Ci narzędzi (SQL / Corporate Memory / Dokumenty) by udowodnić, że inwestycja się opłaci (J-Curve). Twój cel to rozwój.
Jeśli pytanie dotyczy dokumentów firmy, użyj search_documents żeby znaleźć relewantne dokumenty, a get_document_content żeby przeczytać ich treść.
Gdy wyciągniesz dane, podsumuj optymistyczny scenariusz biznesowy na wzrost przychodów (czysty tekst)."""

AGENT_ACTUARY = """Jesteś Aktuariuszem. Chłodna kalkulacja i zachowawcza matematyka bazująca na historii.
Bezwzględnie używaj narzędzi (SQL / Dokumenty) do wyciągnięcia średnich wydatków i wpływów z przeszłości. Traktujesz budżet liniowo.
Jeśli pytanie dotyczy dokumentów firmy, użyj search_documents żeby znaleźć relewantne dokumenty, a get_document_content żeby przeczytać ich treść.
Gdy wyciągniesz wzorce matematyczne, zwróć dokładne historyczne matematyczne prognozy (czysty tekst)."""

MAJOR_CFO_PROMPT = """Jesteś Głównym Orkiestratorem (Major CFO).
Otrzymujesz pytanie CEO oraz (jeśli to była debata strategiczna) szczegółowe raporty 3 sub-agentów.

ZASADY:
1. Zawsze odpowiedz formacie JSON określonym przez schema.
2. Jeśli pytanie dotyczyło analizy strategicznej i masz opinie sub-agentów, stwórz 'DashboardResponse' łącząc perspektywy w spójny executive_summary i zdefiniuj layout korzystając z klocków (Tabela, Wykres, itd).
3. Jeśli pytanie było tylko o fakty i masz tylko dyskusję 1 agenta - po prostu podaj 'TextResponse'.
4. WYMAGANE: Zbuduj narracyjną oś czasu (agent_timeline) opartą na zebranych materiałach od sub-agentów. Każdy krok ma być opisany prozą z perspektywy AI, wyjaśniając co dokładnie robili agenci, z jakich narzędzi korzystali i do jakich twardych danych dotarli.
"""


# ─────────────────────────────────────────────────────────────────────────────
# DOCUMENT CONTEXT — fetch AI summaries for user-attached documents
# ─────────────────────────────────────────────────────────────────────────────
async def _fetch_attached_documents(company_id: str, document_ids: list[str]) -> str:
    """
    Pobiera AI summaries z company_documents dla dokumentów załączonych przez użytkownika.
    Resolwuje Document.id → filename → CompanyDocument.ai_summary (ten sam wzorzec co ai-summary endpoint).
    """
    from uuid import UUID
    from sqlalchemy import select
    from app.database import AsyncSessionLocal
    from app.models import Document, CompanyDocument

    async with AsyncSessionLocal() as db:
        # 1. Pobierz nazwy plików z tabeli documents (weryfikacja company_id)
        doc_uuids = []
        for did in document_ids:
            try:
                doc_uuids.append(UUID(did))
            except (ValueError, AttributeError):
                continue

        if not doc_uuids:
            return ""

        result = await db.execute(
            select(Document.filename)
            .where(
                Document.id.in_(doc_uuids),
                Document.company_id == UUID(company_id)
            )
        )
        filenames = [row[0] for row in result.all()]

        if not filenames:
            return ""

        # 2. Pobierz AI summaries z company_documents po file_name
        result = await db.execute(
            select(CompanyDocument)
            .where(
                CompanyDocument.company_id == UUID(company_id),
                CompanyDocument.file_name.in_(filenames)
            )
            .order_by(CompanyDocument.created_at.desc())
        )
        docs = result.scalars().all()

        if not docs:
            return f"\n\nDOKUMENTY ZAŁĄCZONE PRZEZ PREZESA:\n(Załączono {len(filenames)} plik(ów), ale AI indexer jeszcze ich nie przetworzył.)\n"

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

        return "\n\nDOKUMENTY ZAŁĄCZONE PRZEZ PREZESA:\n" + "\n\n".join(parts) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# ETAP 1: TRIAGE
# ─────────────────────────────────────────────────────────────────────────────
async def triage_request(client: genai.Client, user_prompt: str) -> TriageDecision:
    response = await client.aio.models.generate_content(
        model='gemini-2.5-flash',
        contents=f"Zapytanie CEO: {user_prompt}",
        config=types.GenerateContentConfig(
            system_instruction=TRIAGE_PROMPT,
            response_mime_type="application/json",
            response_schema=TriageDecision,
            temperature=0.0
        )
    )
    return TriageDecision.model_validate_json(response.text)


# ─────────────────────────────────────────────────────────────────────────────
# ETAP 2+3: AGENTIC PIPELINE (Triage → Tools → Debate → Synthesis)
# ─────────────────────────────────────────────────────────────────────────────
async def agentic_cfo_stream(request: CfoRequest, company_id: str) -> AsyncGenerator[str, None]:
    """
    Agentic Workflow z jawnym AFC (Automatic Function Calling).
    Każdy sub-agent dostaje narzędzia SQL + Memory, SDK wykonuje pętlę
    tool_call → execute → tool_result → ... → final_text automatycznie.
    Instrument_tool wrapper loguje każde wywołanie do terminala.
    """
    user_prompt = request.prompt
    yield json.dumps({"status": "loading", "message": "Major CFO: Klasyfikuję zapytanie (Triage)..."})

    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

    # ── 1. TRIAGE (gemini-2.5-flash, szybki, tani) ──────────────────────────
    try:
        triage = await triage_request(client, user_prompt)
        logger.info(f"[TRIAGE] route={triage.route}, reasoning={triage.reasoning}")
        yield json.dumps({"status": "loading", "message": f"Triage → {triage.route}: {triage.reasoning}"})
    except Exception as e:
        logger.error(f"[TRIAGE_ERROR] {e}", exc_info=True)
        triage = TriageDecision(route="analysis", reasoning="Fallback do analizy (błąd triage).")
        yield json.dumps({"status": "loading", "message": f"Triage fallback: {triage.reasoning}"})

    # ── 2A. FETCH ATTACHED DOCUMENTS ─────────────────────────────────────────
    doc_context_str = ""
    if request.document_ids:
        yield json.dumps({"status": "loading", "message": f"Wczytuję {len(request.document_ids)} załączonych dokumentów..."})
        try:
            doc_context_str = await _fetch_attached_documents(company_id, request.document_ids)
            if doc_context_str:
                logger.info(f"[DOCS] Załadowano kontekst dokumentów ({len(doc_context_str)} chars)")
            else:
                logger.warning("[DOCS] Brak AI summaries dla podanych document_ids")
        except Exception as e:
            logger.error(f"[DOCS_ERROR] {e}", exc_info=True)

    # ── 2B. INITIALIZE INSTRUMENTED TOOLS ─────────────────────────────────────
    # Raw tools scoped to company_id (closure) + logging wrappers
    cid_short = company_id[:8]
    sql_tool = instrument_tool(
        make_execute_sql_query(company_id),
        f"execute_sql_query[{cid_short}]"
    )
    memory_tool = instrument_tool(
        make_search_corporate_memory(company_id),
        f"search_corporate_memory[{cid_short}]"
    )
    docs_tool = instrument_tool(
        make_search_documents(company_id),
        f"search_documents[{cid_short}]"
    )
    content_tool = instrument_tool(
        make_get_document_content(company_id),
        f"get_document_content[{cid_short}]"
    )

    all_tools = [sql_tool, memory_tool, docs_tool, content_tool]

    # Context snippet injected into every sub-agent system prompt so the model
    # knows the company_id UUID to use in SQL WHERE clauses.
    db_context = (
        f"\n\nKONTEKST BAZY DANYCH:\n"
        f"- company_id tej firmy: '{company_id}'\n"
        f"- KAŻDE zapytanie SQL MUSI zawierać: WHERE company_id = '{company_id}'\n"
        f"- Tabela invoices: id, company_id, invoice_type ('expense'/'income'), "
        f"contractor_name, net_amount, gross_amount, vat_amount, issue_date, "
        f"is_paid, status, category, department_id, risk_score.\n"
        f"- Tabela departments: id, company_id, name, monthly_limit.\n"
        f"\nDOSTĘPNE NARZĘDZIA DOKUMENTOWE:\n"
        f"- search_documents(document_type, query) — przeszukaj zaindeksowane dokumenty firmy (faktury, umowy, bilanse). Zwraca AI summaries.\n"
        f"- get_document_content(file_name) — pobierz pełną treść dokumentu gdy AI summary nie wystarczy.\n"
    ) + doc_context_str

    all_tool_calls: list[dict] = []
    opinions: list[str] = []

    # ── 3. EXECUTION ─────────────────────────────────────────────────────────

    if triage.route == "instant":
        # ── ŚCIEŻKA INSTANT: Flash, bez syntezy, 2 wywołania LLM ──────────
        yield json.dumps({"status": "loading", "message": "Instant: Szybkie wyszukiwanie danych..."})

        chat = client.aio.chats.create(
            model="gemini-2.5-flash",
            config=types.GenerateContentConfig(
                system_instruction=(
                    "Jesteś ekspertem finansowym. Odpowiadaj zwięźle (1-3 zdań). "
                    "Używaj narzędzi żeby pobrać twarde dane: "
                    "execute_sql_query (baza danych), search_documents (dokumenty firmy), "
                    "get_document_content (pełna treść dokumentu). NIE ZGADUJ — odpowiadaj WYŁĄCZNIE "
                    "na podstawie wyników z narzędzi."
                    + db_context
                ),
                temperature=0.1,
                tools=all_tools,
                automatic_function_calling=AFC_CONFIG_FAST,
            )
        )
        try:
            response = await chat.send_message(user_prompt)

            calls = _extract_tool_calls_from_response(response)
            all_tool_calls.extend(calls)
            logger.info(f"[AFC_OK] instant: {len(calls)} tool call(s)")

            # Bezpośredni output — BEZ syntezy Major CFO
            instant_response = TextResponse(
                response_type="chat",
                message=response.text,
                agent_timeline=[
                    AgentTimelineStep(
                        agent_name="Instant Agent",
                        action=f"Szybkie wyszukiwanie danych ({len(calls)} zapytań do narzędzi)",
                        result=response.text[:200] + ("..." if len(response.text) > 200 else ""),
                        timestamp=datetime.utcnow().isoformat(),
                    )
                ],
            )

            yield json.dumps({
                "status": "success",
                "report": instant_response.model_dump(mode="json"),
                "meta": {
                    "route": "instant",
                    "total_tool_calls": len(all_tool_calls),
                    "tools_used": list(set(tc["tool"] for tc in all_tool_calls)),
                }
            })
            return  # Koniec — nie wchodzimy w syntezę

        except Exception as e:
            logger.error(f"[AGENT_ERROR] instant: {e}", exc_info=True)
            yield json.dumps({"status": "error", "message": f"Błąd szybkiej odpowiedzi: {e}"})
            return

    elif triage.route == "analysis":
        # ── ŚCIEŻKA ANALYSIS: Pro, 1 agent + synteza, 3 wywołania LLM ─────
        yield json.dumps({"status": "loading", "message": "Analiza: Agent bada dane..."})

        chat = client.aio.chats.create(
            model="gemini-3.1-pro-preview",
            config=types.GenerateContentConfig(
                system_instruction=(
                    "Jesteś ekspertem finansowym. Używaj narzędzi żeby pobrać twarde dane: "
                    "execute_sql_query (baza danych), search_documents (dokumenty firmy), "
                    "get_document_content (pełna treść dokumentu). NIE ZGADUJ — odpowiadaj WYŁĄCZNIE "
                    "na podstawie wyników z narzędzi. Jeśli narzędzie zwróci błąd, poinformuj o tym."
                    + db_context
                ),
                temperature=0.1,
                tools=all_tools,
                automatic_function_calling=AFC_CONFIG,
            )
        )
        try:
            response = await chat.send_message(user_prompt)

            calls = _extract_tool_calls_from_response(response)
            all_tool_calls.extend(calls)
            if calls:
                logger.info(f"[AFC_OK] analysis: {len(calls)} tool call(s) confirmed")
            else:
                logger.warning("[AFC_WARNING] analysis: 0 tool calls — model may have hallucinated!")

            opinions.append(f"Wnioski analityczne z bazy:\n{response.text}")

        except Exception as e:
            logger.error(f"[AGENT_ERROR] analysis: {e}", exc_info=True)
            opinions.append(f"Nie udało się pobrać danych z bazy. Błąd: {e}")

        yield json.dumps({"status": "loading", "message": "Synteza Major CFO..."})

    elif triage.route == "strategic_debate":
        # ── ŚCIEŻKA STRATEGIC_DEBATE: 3 agenty + synteza, 5 wywołań LLM ───
        yield json.dumps({"status": "loading", "message": "Debata: Uruchamiam 3 agentów (Pesymista / Optymista / Aktuariusz)..."})

        async def run_subagent(name: str, sys_prompt: str) -> str:
            chat = client.aio.chats.create(
                model="gemini-3.1-pro-preview",
                config=types.GenerateContentConfig(
                    system_instruction=sys_prompt + db_context,
                    temperature=0.4,
                    tools=all_tools,
                    automatic_function_calling=AFC_CONFIG,
                )
            )
            try:
                response = await chat.send_message(user_prompt)

                calls = _extract_tool_calls_from_response(response)
                for c in calls:
                    c["agent"] = name
                all_tool_calls.extend(calls)

                if calls:
                    logger.info(f"[AFC_OK] Agent '{name}': {len(calls)} tool call(s)")
                else:
                    logger.warning(f"[AFC_WARNING] Agent '{name}': 0 tool calls!")

                return f"=== RAPORT: {name} ===\n{response.text}"

            except Exception as e:
                logger.error(f"[AGENT_ERROR] {name}: {e}", exc_info=True)
                return f"=== RAPORT: {name} ===\nAwaria subagenta: {e}"

        debates = await asyncio.gather(
            run_subagent("Pesymista", AGENT_PESSIMIST),
            run_subagent("Optymista", AGENT_OPTIMIST),
            run_subagent("Aktuariusz", AGENT_ACTUARY),
        )
        opinions.extend(debates)

        yield json.dumps({
            "status": "loading",
            "message": f"Debata zakończona ({len(all_tool_calls)} wywołań narzędzi). Synteza Major CFO..."
        })

    # ── 4. MAJOR CFO SYNTHESIS (tylko analysis + strategic_debate) ─────────
    debates_str = "\n\n".join(opinions)

    tools_summary = json.dumps(all_tool_calls, ensure_ascii=False, default=str) if all_tool_calls else "Brak wywołań narzędzi."

    final_prompt = (
        f"Zapytanie CEO: {user_prompt}\n\n"
        f"Materiały zebrane przez zespół / sub-agentów:\n{debates_str}\n\n"
        f"Log wywołań narzędzi (SQL/Memory) przez agentów:\n{tools_summary}\n\n"
        f"Zsyntetyzuj finałowy raport wybierając odpowiedni response_type."
    )

    try:
        final_res = await client.aio.models.generate_content(
            model="gemini-3.1-pro-preview",
            contents=final_prompt,
            config=types.GenerateContentConfig(
                system_instruction=MAJOR_CFO_PROMPT,
                response_mime_type="application/json",
                response_schema=MajorCfoOutput,
                temperature=0.2,
            )
        )

        final_data = MajorCfoOutput.model_validate_json(final_res.text)

        yield json.dumps({
            "status": "success",
            "report": final_data.response.model_dump(mode="json"),
            "meta": {
                "route": triage.route,
                "total_tool_calls": len(all_tool_calls),
                "tools_used": list(set(tc["tool"] for tc in all_tool_calls)),
            }
        })

    except Exception as e:
        logger.error(f"[SYNTHESIS_ERROR] Major CFO: {e}", exc_info=True)
        yield json.dumps({"status": "error", "message": f"Błąd syntezy Major CFO: {e}"})
