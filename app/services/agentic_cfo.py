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
    maximum_remote_calls=3,
)

# ─────────────────────────────────────────────────────────────────────────────
# TOOL INSTRUMENTATION — wraps async tool with logging so we can physically
# see every tool call in the terminal. Preserves __doc__, __name__,
# __annotations__ (SDK uses these to build FunctionDeclaration).
# ─────────────────────────────────────────────────────────────────────────────
def instrument_tool(fn, display_name: str, progress_queue: asyncio.Queue | None = None):
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        call_args = {**dict(zip(fn.__code__.co_varnames, args)), **kwargs}
        args_preview = json.dumps(call_args, default=str, ensure_ascii=False)[:300]
        logger.info(
            f"\n{'='*60}\n"
            f"[AFC_TOOL_EXEC] >>> {display_name}\n"
            f"  args: {args_preview}\n"
            f"{'='*60}"
        )
        # Emit tool_call event
        if progress_queue:
            tool_short = display_name.split("[")[0]  # "execute_sql_query"
            await progress_queue.put({
                "type": "tool_call", "tool": tool_short, "args": args_preview,
            })
        try:
            result = await fn(*args, **kwargs)
            preview = (result[:300] + "...") if len(result) > 300 else result
            logger.info(f"[AFC_TOOL_RESULT] <<< {display_name}: {preview}")
            # Emit tool_result event
            if progress_queue:
                tool_short = display_name.split("[")[0]
                await progress_queue.put({
                    "type": "tool_result", "tool": tool_short, "preview": preview[:200],
                })
            return result
        except Exception as e:
            logger.error(f"[AFC_TOOL_ERROR] !!! {display_name} failed: {e}", exc_info=True)
            if progress_queue:
                tool_short = display_name.split("[")[0]
                await progress_queue.put({
                    "type": "tool_error", "tool": tool_short, "error": str(e)[:200],
                })
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

- Wybierz 'instant' DLA: Prostych pytań o suche fakty, sprawdzenie 1 liczby, odpowiedź na jedno konkretne pytanie.
  PRZYKŁADY instant:
  • "Ile mam gotówki?" / "Ile mam kasy?"
  • "Ile faktur mam w systemie?"
  • "Kto jest moim największym klientem?"
  • "Który kontrahent generuje najwyższe koszty?"
  • "Ile mam niezapłaconych faktur?"
  • "Jaka jest moja marża brutto?"
  • "Ile zapłaciłem ZUS w marcu?"
  ZASADA: Jeśli odpowiedź to 1 liczba, 1 nazwa, 1 lista lub proste TAK/NIE → instant.

- Wybierz 'analysis' DLA: Pytań wymagających analizy TRENDU, porównania okresów lub struktury.
  PRZYKŁADY analysis:
  • "Przeanalizuj mój cashflow za ostatnie 3 miesiące"
  • "Jak wygląda trend wydatków?"
  • "Czy mam ryzyko koncentracji klientów?"
  • "Przeanalizuj moje należności"
  • "Porównaj przychody Q1 vs Q2"
  ZASADA: Wymaga obliczeń na wielu wierszach danych + wizualizacji/wykresu.

- Wybierz 'strategic_debate' DLA: Dylematów biznesowych z DECYZJĄ DO PODJĘCIA.
  PRZYKŁADY strategic_debate:
  • "Czy stać mnie na zatrudnienie nowego handlowca za 12k?"
  • "Chcę zainwestować 50k w marketing — czy to się opłaci?"
  • "Czy powinniśmy wejść na rynek niemiecki?"
  • "Co jeśli zatrudnimy 3 osoby?"
  • "Restrukturyzacja działu sprzedaży — symulacja"
  ZASADA: Wymaga scenariuszy (optymistyczny/pesymistyczny) i wielu perspektyw.

Dodatkowo klasyfikuj 'mode':
- INVESTMENT: użytkownik proponuje wydatek/inwestycję/zatrudnienie (np. "wydajmy 50k", "zatrudnijmy")
- DIAGNOSTIC: użytkownik pyta o stan/problem/analizę (np. "jak wygląda cashflow?", "jakie ryzyka?")
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

ZASADY FORMATU:
1. Zawsze odpowiedz w formacie JSON określonym przez schema.
2. Jeśli pytanie dotyczyło analizy strategicznej i masz opinie sub-agentów — DashboardResponse.
3. Jeśli pytanie o fakty / prostą odpowiedź — TextResponse.
4. Zbuduj agent_timeline opartą na zebranych materiałach od sub-agentów.

NAJWAŻNIEJSZA ZASADA:
Nigdy nie odpowiadaj ścianą tekstu. Każda wartość finansowa MUSI być w komponencie (metric_grid, data_table, alert_banner) — NIE zakopana w akapicie. Jeśli odpowiedź wygląda jak akapit z Wikipedii — przepisz ją używając komponentów.

ZŁA ODPOWIEDŹ: "Kwota 101 400 PLN to zysk netto. Wynika z przychodów 235 000 PLN minus koszty 133 600 PLN."
DOBRA ODPOWIEDŹ: metric_grid z 3 kartami (Zysk netto: 101 400 PLN, Przychody: 235 000 PLN, Koszty: 133 600 PLN) + text_card z 1 zdaniem kontekstu.

Gdy musisz napisać narrative/text_card zawierający liczby — użyj formatu **liczba** (np. **130 472 PLN**, **45%**, **15.04.2026**) żeby frontend mógł je wyróżnić.

STYL KOMUNIKACJI — OBOWIĄZKOWE:
- Pisz jak doświadczony Dyrektor Finansowy piszący raport dla zarządu.
- Zero emocji, zero dramatyzmu, zero metafor, zero copywritingu.
- Każde zdanie = konkretny fakt z liczbą LUB rekomendacja akcji.
- ZAKAZANE wyrażenia: "musimy", "nie możemy", "złoty środek", "obrona i atak", "niestety", "co ważne", "kluczowe jest", "genialny", "potężny", "dramatyczny".
- Zamiast "Gotówka stopnieje w kilka tygodni" → "Runway: 22 tygodnie przy burn rate 38 000 PLN/mies."
- Nagłówki sekcji: tylko neutralne nazwy finansowe (np. "Płynność", "Należności", "Rekomendacja"). NIE dramatyczne tytuły.
- Liczby zawsze z jednostką i separatorem tysięcy: 840 000 PLN, 38 000 PLN/mies.

ZASADY KPI / METRIC LABELS:
- Label metryki = NAZWA METRYKI, nie komentarz AI.
- Poprawne: "Dostępna gotówka", "Runway", "Przeterminowane należności", "Zobowiązania pilne", "Burn rate", "MRR"
- ZAKAZANE: "Iluzja bezpieczeństwa", "Do technicznej niewypłacalności", "Tykająca bomba" — to NIE SĄ labele finansowe.
- Komentarz AI idzie do osobnego bloku text_card, nie do labela metryki.

DOSTĘPNE KOMPONENTY DO BUDOWANIA ODPOWIEDZI:

metric_grid — Pokazuje kluczowe wartości finansowe. Używaj gdy odpowiedź zawiera konkretne, ważne liczby które CEO powinien zobaczyć od razu. Label = nazwa metryki ("Dostępna gotówka", "Burn rate", "Runway"), NIE komentarz AI.

alert_banner — Podkreśla pilną informację wymagającą uwagi. Używaj gdy jest realne ryzyko, deadline lub anomalia którą CEO MUSI wiedzieć.

data_table — Zestaw danych z wieloma atrybutami per wiersz. Używaj gdy dane mają strukturę listy — faktury, kontrahenci, transakcje, zobowiązania.

text_card — Blok tekstowy z analizą lub rekomendacją. Pisz jak CFO do zarządu — fakty, wnioski, bez emocji. Rekomendacje zaczynaj od czasownika: "Zadzwoń...", "Opłać...", "Negocjuj..."

chart — Wizualizacja trendu lub rozkładu. Używaj gdy dane mają wymiar czasowy lub porównawczy który wykres pokazuje lepiej niż tabela.

timeline — Sekwencja kroków w czasie. Używaj TYLKO gdy CEO wprost pyta o plan działania, harmonogram lub playbook.

ZASADA BUDOWANIA ODPOWIEDZI:
Dobierz komponenty które razem najlepiej odpowiadają na pytanie.
Nie ma stałej kolejności — buduj jak CFO prezentujący dane zarządowi.
Dane przed narracją. Akcja przed uzasadnieniem.
Nie używaj komponentu jeśli nie wnosi wartości do tej konkretnej odpowiedzi.

DŁUGOŚĆ ODPOWIEDZI — dobieraj do złożoności pytania:

KRÓTKA (TextResponse, 2-4 zdania) gdy:
- pytanie o jedną liczbę: "Ile mam na koncie?" → 1 KPI + 1 zdanie kontekstu
- potwierdzenie: "Czy zapłaciłem ZUS?" → TAK/NIE + data + kwota

ŚREDNIA (DashboardResponse, KPI + tabela + 3-5 zdań) gdy:
- pytanie o stan finansów: "Jak wygląda cashflow?"
- pytanie o należności/zobowiązania
- pytanie o kontrahenta

PEŁNY RAPORT (DashboardResponse, bez limitu bloków) gdy:
- pytanie analityczne: "Jak wyglądają finanse na Q1?"
- pytanie strategiczne: "Czy mogę zatrudnić nową osobę?"
- pytanie o ryzyko: "Jakie mam ryzyka finansowe?"
- CEO pyta wprost o raport/analizę/podsumowanie

ZASADA: Lepiej dać za dużo danych niż za mało. CEO może przewinąć. CEO nie może uzupełnić brakujących danych.
NIE generuj timeline/playbooku automatycznie — TYLKO gdy CEO wprost pyta o plan krok po kroku.
Nigdy nie powtarzaj pytania CEO. Nigdy nie pisz "Oczywiście!", "Świetne pytanie!", "Rozumiem".
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
async def agentic_cfo_stream(
    request: CfoRequest,
    company_id: str,
    conversation_id: str | None = None,
) -> AsyncGenerator[str, None]:
    """
    Agentic Workflow z jawnym AFC (Automatic Function Calling).
    Każdy sub-agent dostaje narzędzia SQL + Memory, SDK wykonuje pętlę
    tool_call → execute → tool_result → ... → final_text automatycznie.

    conversation_id: opcjonalny UUID wątku — jeśli podany, historia rozmowy
    zostanie wstrzyknięta w system instruction agentów.
    """
    user_prompt = request.prompt

    # ── PROGRESS QUEUE — real-time tool call events streamed to frontend ───
    progress_queue: asyncio.Queue = asyncio.Queue()

    yield json.dumps({"status": "progress", "type": "phase_start", "phase": "triage", "label": "Klasyfikacja zapytania"})

    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

    # ── 1. TRIAGE (gemini-2.5-flash) ──────────────────────────────────────────────
    try:
        triage = await triage_request(client, user_prompt)
        logger.info(f"[TRIAGE] route={triage.route}, reasoning={triage.reasoning}")
        yield json.dumps({"status": "progress", "type": "phase_end", "phase": "triage", "result": triage.route, "detail": triage.reasoning})
    except Exception as e:
        logger.error(f"[TRIAGE_ERROR] {e}", exc_info=True)
        triage = TriageDecision(route="analysis", reasoning="Fallback do analizy (błąd triage).")
        yield json.dumps({"status": "progress", "type": "phase_end", "phase": "triage", "result": "analysis", "detail": triage.reasoning})

    # ── 2A. FETCH ATTACHED DOCUMENTS ─────────────────────────────────────────
    doc_context_str = ""
    if request.document_ids:
        yield json.dumps({"status": "progress", "type": "phase_start", "phase": "docs", "label": f"Wczytywanie {len(request.document_ids)} dokumentów"})
        try:
            doc_context_str = await _fetch_attached_documents(company_id, request.document_ids)
            if doc_context_str:
                logger.info(f"[DOCS] Załadowano kontekst dokumentów ({len(doc_context_str)} chars)")
            else:
                logger.warning("[DOCS] Brak AI summaries dla podanych document_ids")
        except Exception as e:
            logger.error(f"[DOCS_ERROR] {e}", exc_info=True)

    # ── 2A2. CONVERSATION HISTORY ────────────────────────────────────────────
    conversation_context_str = ""
    if conversation_id:
        try:
            import uuid as _uuid
            from app.database import AsyncSessionLocal
            from app.services.conversation_context import (
                get_conversation_history_text,
                build_conversation_system_addendum,
            )
            async with AsyncSessionLocal() as ctx_db:
                history_text = await get_conversation_history_text(
                    ctx_db, _uuid.UUID(conversation_id)
                )
                conversation_context_str = build_conversation_system_addendum(history_text)
                if conversation_context_str:
                    logger.info(f"[CONV_CTX] Loaded {len(conversation_context_str)} chars of conversation history")
        except Exception as e:
            logger.error(f"[CONV_CTX_ERROR] {e}", exc_info=True)

    # ── 2B. INITIALIZE INSTRUMENTED TOOLS ─────────────────────────────────────
    # Raw tools scoped to company_id (closure) + logging wrappers
    cid_short = company_id[:8]
    sql_tool = instrument_tool(
        make_execute_sql_query(company_id),
        f"execute_sql_query[{cid_short}]",
        progress_queue,
    )
    memory_tool = instrument_tool(
        make_search_corporate_memory(company_id),
        f"search_corporate_memory[{cid_short}]",
        progress_queue,
    )
    docs_tool = instrument_tool(
        make_search_documents(company_id),
        f"search_documents[{cid_short}]",
        progress_queue,
    )
    content_tool = instrument_tool(
        make_get_document_content(company_id),
        f"get_document_content[{cid_short}]",
        progress_queue,
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
    ) + doc_context_str + conversation_context_str

    all_tool_calls: list[dict] = []
    opinions: list[str] = []

    # Helper: drain progress_queue and yield all pending events
    async def _drain_progress(phase: str):
        events = []
        while not progress_queue.empty():
            try:
                evt = progress_queue.get_nowait()
                evt["phase"] = phase
                evt["status"] = "progress"
                events.append(json.dumps(evt))
            except asyncio.QueueEmpty:
                break
        return events

    # Helper: run an async task while draining tool call events from the queue
    async def _run_with_progress(coro, phase: str):
        task = asyncio.create_task(coro)
        events_to_yield = []
        while not task.done():
            try:
                evt = await asyncio.wait_for(progress_queue.get(), timeout=0.3)
                evt["phase"] = phase
                evt["status"] = "progress"
                events_to_yield.append(json.dumps(evt))
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception:
                break
        # Drain remaining
        for e in await _drain_progress(phase):
            events_to_yield.append(e)
        # Get result — re-raises if the task failed
        try:
            result = task.result()
        except Exception:
            result = await task
        return result, events_to_yield

    # ── 3. EXECUTION ─────────────────────────────────────────────────────────

    if triage.route == "instant":
        # ── ŚCIEŻKA INSTANT: 1 LLM call (AFC + structured output) ───────
        yield json.dumps({"status": "progress", "type": "phase_start", "phase": "instant", "label": "Szybkie wyszukiwanie danych"})

        INSTANT_SYSTEM = (
            "Jesteś ekspertem finansowym CFO. Pobierz dane narzędziami i odpowiedz w formacie JSON.\n"
            "PRIORYTET NARZĘDZI:\n"
            "1. search_corporate_memory — ZAWSZE zacznij od tego! SafeCash, MRR, burn rate, należności, zobowiązania.\n"
            "2. execute_sql_query — dla konkretnych zapytań o faktury, kontrahentów, kwoty.\n"
            "3. search_documents — TYLKO gdy pytanie dotyczy konkretnego dokumentu.\n\n"
            "ZASADY FORMATOWANIA ODPOWIEDZI:\n"
            "- Jeśli odpowiedź zawiera JAKIEKOLWIEK wartości finansowe (kwoty PLN, procenty, liczby) "
            "→ użyj DashboardResponse z metric_grid. NIE używaj TextResponse dla liczb.\n"
            "- Jeśli data_table pokazuje zaległości → dodaj text_card z rekomendacją akcji.\n"
            "- NIE generuj chart na trasie instant. Chart to narzędzie analysis/debate.\n"
            "- Dla prostych TAK/NIE bez liczb → TextResponse.\n"
            "- Max 1 metric_grid, max 1 text_card, max 1 data_table. ZAKAZ duplikatów bloków.\n"
            "- Pisz jak CFO: fakty, liczby, zero emocji. Labele metryk = nazwy finansowe, nie komentarze.\n"
            "NIE ZGADUJ — odpowiadaj WYŁĄCZNIE na podstawie wyników z narzędzi."
            + db_context
        )

        # Faza 1: pobranie danych przez AFC (tool calls)
        chat = client.aio.chats.create(
            model="gemini-2.5-flash",
            config=types.GenerateContentConfig(
                system_instruction=INSTANT_SYSTEM,
                temperature=0.1,
                tools=all_tools,
                automatic_function_calling=AFC_CONFIG_FAST,
            )
        )
        try:
            response, tool_events = await _run_with_progress(
                chat.send_message(user_prompt), "instant"
            )
            for evt in tool_events:
                yield evt

            calls = _extract_tool_calls_from_response(response)
            all_tool_calls.extend(calls)
            logger.info(f"[AFC_OK] instant: {len(calls)} tool call(s)")

            yield json.dumps({"status": "progress", "type": "phase_end", "phase": "instant", "detail": f"{len(calls)} narzędzi"})

            # Faza 2: formatowanie — single-shot z MajorCfoOutput schema (ten sam model, bez dodatkowego call)
            raw_answer = response.text
            try:
                format_res = await client.aio.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=(
                        f"Zapytanie CEO: {user_prompt}\n\n"
                        f"Dane z narzędzi:\n{raw_answer}\n\n"
                        f"Sformatuj jako JSON. Liczby → DashboardResponse z metric_grid. Bez chart."
                    ),
                    config=types.GenerateContentConfig(
                        system_instruction=INSTANT_SYSTEM,
                        response_mime_type="application/json",
                        response_schema=MajorCfoOutput,
                        temperature=0.1,
                    )
                )
                formatted = MajorCfoOutput.model_validate_json(format_res.text)

                if not formatted.response.agent_timeline:
                    formatted.response.agent_timeline = []
                formatted.response.agent_timeline.insert(0, AgentTimelineStep(
                    agent_name="Instant Agent",
                    step_type="reasoning",
                    content=f"Szybkie wyszukiwanie ({len(calls)} narzędzi).",
                    timestamp=datetime.utcnow().isoformat(),
                ))

                yield json.dumps({
                    "status": "success",
                    "report": formatted.response.model_dump(mode="json"),
                    "meta": {
                        "route": "instant",
                        "total_tool_calls": len(all_tool_calls),
                        "tools_used": list(set(tc["tool"] for tc in all_tool_calls)),
                    }
                })
            except Exception as fmt_err:
                logger.warning(f"[INSTANT_FORMAT] Fallback do TextResponse: {fmt_err}")
                yield json.dumps({
                    "status": "success",
                    "report": TextResponse(
                        response_type="chat",
                        message=raw_answer,
                        agent_timeline=[AgentTimelineStep(
                            agent_name="Instant Agent", step_type="reasoning",
                            content=f"Szybkie wyszukiwanie ({len(calls)} narzędzi). {raw_answer[:200]}",
                            timestamp=datetime.utcnow().isoformat(),
                        )],
                    ).model_dump(mode="json"),
                    "meta": {"route": "instant", "total_tool_calls": len(all_tool_calls),
                             "tools_used": list(set(tc["tool"] for tc in all_tool_calls))},
                })
            return

        except Exception as e:
            logger.error(f"[AGENT_ERROR] instant: {e}", exc_info=True)
            yield json.dumps({"status": "error", "message": f"Błąd szybkiej odpowiedzi: {e}"})
            return

    elif triage.route == "analysis":
        # ── ŚCIEŻKA ANALYSIS: Pro, 1 agent + synteza, 3 wywołania LLM ─────
        yield json.dumps({"status": "progress", "type": "phase_start", "phase": "analysis", "label": "Agent analizuje dane"})

        chat = client.aio.chats.create(
            model="gemini-2.5-flash",
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
            response, tool_events = await _run_with_progress(
                chat.send_message(user_prompt), "analysis"
            )
            for evt in tool_events:
                yield evt

            calls = _extract_tool_calls_from_response(response)
            all_tool_calls.extend(calls)
            if calls:
                logger.info(f"[AFC_OK] analysis: {len(calls)} tool call(s) confirmed")
            else:
                logger.warning("[AFC_WARNING] analysis: 0 tool calls — model may have hallucinated!")

            response_text = response.text or "(Agent nie zwrócił tekstu — prawdopodobnie niedokończony function_call)"
            opinions.append(f"Wnioski analityczne z bazy:\n{response_text}")
            yield json.dumps({"status": "progress", "type": "phase_end", "phase": "analysis", "detail": f"{len(calls)} narzędzi"})

        except Exception as e:
            logger.error(f"[AGENT_ERROR] analysis: {e}", exc_info=True)
            opinions.append(f"Nie udało się pobrać danych z bazy. Błąd: {e}")
            yield json.dumps({"status": "progress", "type": "phase_end", "phase": "analysis", "detail": f"Błąd: {e}"})

    elif triage.route == "strategic_debate":
        # ── ŚCIEŻKA STRATEGIC_DEBATE: pre-fetch + 3 agenty + synteza ─────
        agent_names = ["Pesymista", "Optymista", "Aktuariusz"]

        # ── PRE-FETCH: pobierz shared data RAZ, wstrzyknij do wszystkich agentów ──
        yield json.dumps({"status": "progress", "type": "phase_start", "phase": "prefetch", "label": "Pobieranie danych bazowych"})
        shared_data = ""
        try:
            memory_tool_raw = make_search_corporate_memory(company_id)
            sql_tool_raw = make_execute_sql_query(company_id)
            memory_result, sql_result = await asyncio.gather(
                memory_tool_raw("pełny kontekst finansowy firmy"),
                sql_tool_raw(
                    f"WITH monthly AS ("
                    f"SELECT DATE_TRUNC('month', issue_date) as m, invoice_type, "
                    f"SUM(gross_amount) as total, COUNT(*) as cnt "
                    f"FROM invoices WHERE company_id = '{company_id}' "
                    f"GROUP BY 1, 2 ORDER BY 1 DESC LIMIT 20"
                    f") SELECT * FROM monthly"
                ),
            )
            shared_data = (
                f"\n\n== GOTOWE DANE BAZOWE (pobrane raz, nie odpytuj ponownie) ==\n"
                f"--- Pamięć korporacyjna ---\n{memory_result}\n"
                f"--- Miesięczne przychody/koszty ---\n{sql_result}\n"
                f"UŻYJ TYCH DANYCH jako punkt startowy. SQL odpytuj TYLKO dla specyficznych "
                f"zapytań których tu nie ma (np. konkretny kontrahent, due_date).\n"
            )
            logger.info(f"[PREFETCH] Shared data: {len(shared_data)} chars")
        except Exception as e:
            logger.warning(f"[PREFETCH] Failed, agents will query independently: {e}")
        yield json.dumps({"status": "progress", "type": "phase_end", "phase": "prefetch", "detail": "gotowe"})

        for an in agent_names:
            yield json.dumps({"status": "progress", "type": "phase_start", "phase": f"agent_{an.lower()}", "label": f"Agent {an}"})

        async def run_subagent(name: str, sys_prompt: str) -> str:
            await progress_queue.put({"type": "agent_start", "phase": f"agent_{name.lower()}", "agent": name})
            chat = client.aio.chats.create(
                model="gemini-2.5-flash",  # Sub-agenci na Flash, Pro tylko dla Major CFO synthesis
                config=types.GenerateContentConfig(
                    system_instruction=f"[TRYB: {triage.mode}]\n" + sys_prompt + db_context + shared_data,
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
                    logger.info(f"[AFC_OK] Agent '{name}': 0 tool calls (used shared data)")

                await progress_queue.put({"type": "agent_done", "phase": f"agent_{name.lower()}", "agent": name, "detail": f"{len(calls)} narzędzi"})
                resp_text = response.text or "(Agent nie zwrócił tekstu)"
                return f"=== RAPORT: {name} ===\n{resp_text}"

            except Exception as e:
                logger.error(f"[AGENT_ERROR] {name}: {e}", exc_info=True)
                await progress_queue.put({"type": "agent_done", "phase": f"agent_{name.lower()}", "agent": name, "detail": f"Błąd"})
                return f"=== RAPORT: {name} ===\nAwaria subagenta: {e}"

        async def _run_all_subagents():
            return await asyncio.gather(
                run_subagent("Pesymista", AGENT_PESSIMIST),
                run_subagent("Optymista", AGENT_OPTIMIST),
                run_subagent("Aktuariusz", AGENT_ACTUARY),
            )
        debates_result, tool_events = await _run_with_progress(_run_all_subagents(), "debate")
        for evt in tool_events:
            yield evt

        opinions.extend(debates_result)

        for an in agent_names:
            yield json.dumps({"status": "progress", "type": "phase_end", "phase": f"agent_{an.lower()}", "detail": "gotowe"})

    # ── 4. MAJOR CFO SYNTHESIS (tylko analysis + strategic_debate) ─────────
    yield json.dumps({"status": "progress", "type": "phase_start", "phase": "synthesis", "label": "Synteza Major CFO"})

    debates_str = "\n\n".join(opinions)

    tools_summary = json.dumps(all_tool_calls, ensure_ascii=False, default=str) if all_tool_calls else "Brak wywołań narzędzi."

    # ── CROSS-VALIDATION: wymuś na Major CFO explicit rozstrzygnięcie rozbieżności ──
    cross_validation_note = ""
    if triage.route == "strategic_debate" and len(opinions) >= 3:
        cross_validation_note = (
            "\n\n⚠️ CROSS-VALIDATION (OBOWIĄZKOWA):\n"
            "Porównaj liczby kluczowe między raportami sub-agentów: Break-even, Runway, "
            "prognozowany cashflow w miesiącu 3 i 6. Jeśli rozbieżność > 30% — "
            "MUSISZ w executive_summary explicit napisać który agent się myli i dlaczego, "
            "podając TWOJĄ wyliczoną wartość jako ostateczną. Nie uśredniaj — uzasadnij.\n"
        )

    final_prompt = (
        f"[TRYB: {triage.mode}]\n"
        f"Zapytanie CEO: {user_prompt}\n\n"
        f"Materiały zebrane przez zespół / sub-agentów:\n{debates_str}\n\n"
        f"Log wywołań narzędzi (SQL/Memory) przez agentów:\n{tools_summary}\n\n"
        f"{cross_validation_note}"
        f"Zsyntetyzuj finałowy raport wybierając odpowiedni response_type."
    )

    # Retry logic — Gemini czasem zwraca pusty response lub niedokończony function_call
    SYNTHESIS_MODELS = ["gemini-3.1-pro-preview", "gemini-2.5-flash"]
    last_error = None

    for model_name in SYNTHESIS_MODELS:
        try:
            logger.info(f"[SYNTHESIS] Trying model: {model_name}")
            final_res = await client.aio.models.generate_content(
                model=model_name,
                contents=final_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=MAJOR_CFO_PROMPT,
                    response_mime_type="application/json",
                    response_schema=MajorCfoOutput,
                    temperature=0.2,
                )
            )

            if not final_res.text or len(final_res.text.strip()) < 10:
                raise ValueError(f"Pusta odpowiedź Gemini ({model_name}): '{final_res.text[:50] if final_res.text else 'None'}'")

            final_data = MajorCfoOutput.model_validate_json(final_res.text)

            # ── POST-PROCESSING: deduplikacja bloków layout ──
            resp = final_data.response
            if hasattr(resp, 'layout') and resp.layout:
                seen_types = set()
                deduped = []
                for block in resp.layout:
                    bt = block.block_type if hasattr(block, 'block_type') else (block.get('block_type') if isinstance(block, dict) else None)
                    if bt and bt in seen_types:
                        logger.warning(f"[DEDUP] Usunięto zduplikowany blok: {bt}")
                        continue
                    if bt:
                        seen_types.add(bt)
                    deduped.append(block)
                resp.layout = deduped

            yield json.dumps({"status": "progress", "type": "phase_end", "phase": "synthesis", "detail": "Raport gotowy"})
            yield json.dumps({
                "status": "success",
                "report": resp.model_dump(mode="json"),
                "meta": {
                    "route": triage.route,
                    "total_tool_calls": len(all_tool_calls),
                    "tools_used": list(set(tc["tool"] for tc in all_tool_calls)),
                }
            })
            break  # Sukces — wyjdź z pętli retry

        except Exception as e:
            last_error = e
            is_quota = "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e) or "quota" in str(e).lower()
            if model_name != SYNTHESIS_MODELS[-1]:
                logger.warning(f"[SYNTHESIS_FALLBACK] {model_name} failed ({e}). Falling back to next model...")
                if is_quota:
                    yield json.dumps({"status": "progress", "type": "phase_update", "phase": "synthesis", "detail": f"Limit {model_name} — przełączam na zapasowy model..."})
                await asyncio.sleep(0.5)
            else:
                logger.error(f"[SYNTHESIS_ERROR] All models failed. Last: {e}", exc_info=True)
                yield json.dumps({"status": "error", "message": f"Błąd syntezy Major CFO: {e}"})
