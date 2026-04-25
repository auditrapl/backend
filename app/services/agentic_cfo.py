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
from app.services.context_operational import process_and_upsert_operational
from app.database import AsyncSessionLocal
from datetime import datetime
import uuid as _uuid_mod

logger = logging.getLogger(__name__)


def _sanitize_error(e: Exception) -> str:
    """Map raw API/infra errors to user-friendly Polish messages. Never leak internals."""
    err = str(e).lower()
    if "429" in err or "resource_exhausted" in err or "quota" in err or "rate" in err:
        return "Serwer AI jest chwilowo przeciążony. Spróbuj ponownie za kilka minut."
    if "timeout" in err or "deadline" in err:
        return "Analiza trwała zbyt długo. Spróbuj ponownie lub uprość pytanie."
    if "500" in err or "internal" in err:
        return "Wystąpił tymczasowy błąd serwera AI. Spróbuj ponownie za chwilę."
    if "401" in err or "403" in err or "permission" in err or "auth" in err:
        return "Problem z autoryzacją usługi AI. Skontaktuj się z supportem."
    if "connection" in err or "network" in err or "dns" in err:
        return "Problem z połączeniem do serwera AI. Sprawdź połączenie internetowe."
    if "pusta odpowiedź" in err or "empty" in err:
        return "AI nie wygenerował odpowiedzi. Spróbuj przeformułować pytanie."
    return "Nie udało się wygenerować analizy. Spróbuj ponownie."


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
    """Extracts confirmed tool calls + their results from AFC history.
    Pairs each function_call with its function_response so synthesis
    can read actual SQL output, not just query shape."""
    calls: list[dict] = []
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
                    "result": None,
                })
                continue
            fr = getattr(part, "function_response", None)
            if fr and calls:
                # Attach to the most recent call without a result
                for c in reversed(calls):
                    if c["result"] is None:
                        resp = getattr(fr, "response", None)
                        if isinstance(resp, dict):
                            c["result"] = resp.get("result") or resp.get("output") or resp
                        else:
                            c["result"] = str(resp) if resp is not None else None
                        break
    return calls


# ─────────────────────────────────────────────────────────────────────────────
# SYSTEM PROMPTS
# ─────────────────────────────────────────────────────────────────────────────
TRIAGE_PROMPT = """Jesteś pierwszym punktem kontaktu (Triage) dla zapytań CEO.
Musisz sklasyfikować zapytanie i wybrać ścieżkę 'route'.

WAŻNA ZASADA: W razie wątpliwości wybieraj PROSTSZĄ trasę (instant > analysis > strategic_debate).
Większość pytań CEO to instant — chcą szybkiej odpowiedzi, nie analizy.

- Wybierz 'instant' DLA: Prostych pytań o suche fakty, sprawdzenie 1 liczby, odpowiedź na jedno konkretne pytanie.
  PRZYKŁADY instant:
  • "Ile mam gotówki?" / "Ile mam kasy?"
  • "Ile faktur mam w systemie?"
  • "Kto jest moim największym klientem?"
  • "Który kontrahent generuje najwyższe koszty?"
  • "Ile mam niezapłaconych faktur?"
  • "Jaka jest moja marża brutto?"
  • "Ile zapłaciłem ZUS w marcu?"
  • "Jakie mam koszty?" / "Co się stało z moimi wydatkami?"
  • "Pokaż mi listę zaległych faktur"
  • "Jaki mam DSO?" / "Jaki jest mój runway?"
  • "Jak stoimy finansowo?" (krótki text status — ALE jeśli user mówi "podsumuj", "raport", "przegląd" → analysis, NIE instant)
  ZASADA: Jeśli odpowiedź to 1 liczba, 1 nazwa, 1 lista, krótki stan rzeczy lub proste TAK/NIE → instant.
  NIE instant dla: "raport", "sporządz raport", "podsumuj finanse", "pełny przegląd", "dashboard".

- Wybierz 'analysis' DLA: Pytań WPROST wymagających analizy TRENDU, porównania okresów lub struktury.
  PRZYKŁADY analysis:
  • "Przeanalizuj mój cashflow za ostatnie 3 miesiące"
  • "Jak wygląda trend wydatków?"
  • "Czy mam ryzyko koncentracji klientów?"
  • "Przeanalizuj moje należności"
  • "Porównaj przychody Q1 vs Q2"
  • "Stwórz raport PnL za marzec"
  • "Pokaż wyniki na każdy oddział" / "Porównaj departamenty" / "Rentowność per oddział"
  • "Jaka jest struktura kosztów?"
  • "Pokaż cashflow" / "Prognoza cashflow" / "Zrób prognozę"
  • **"Sporządz raport" / "Daj raport" / "Raport" / "Pełny przegląd" / "Podsumowanie finansowe"**
    (bez sprecyzowania → full-month dashboard: PnL + cashflow trend + top oddziały + top klienci + koszty struktura)
  ZASADA: Wymaga obliczeń na wielu wierszach danych + wizualizacji/wykresu. Pytanie wskazuje na wielowymiarową analizę ("trend", "porównaj", "przeanalizuj", "raport", "w czasie", "per oddział", "struktura", "wyniki na", "rentowność", "cashflow", "prognoza", "forecast", "przegląd", "dashboard", "przegląd finansowy").

  🚨 SŁOWO "RAPORT" — ZAWSZE TRIGGER DLA analysis:
  Gdy user napisze dowolną formę: "raport", "sporządz raport", "daj raport", "pełny raport",
  "raport finansowy", "raport miesięczny", "report", "podsumowanie", "przegląd" — ZAWSZE:
    route='analysis', question_type='trend' (lub 'diagnostic' jeśli pyta dlaczego),
    output_complexity='multi_part', category='raportowanie'.
  Generuj MINIMUM 3 różne chart_type + metric_grid + text_card (exec summary).
  NIE kwalifikuj "raport" jako instant NAWET gdy brak szczegółów — raport z definicji = multi-chart.

  NIE KLASYFIKUJ jako analysis: pytań o JEDNĄ liczbę ("ile mam gotówki", "jaki mam burn rate") — to instant.

- Wybierz 'strategic_debate' DLA: Dylematów biznesowych z DECYZJĄ DO PODJĘCIA bez gotowych alternatyw LUB porównania 3+ strategii/scenariuszy/opcji.
  PRZYKŁADY strategic_debate:
  • "Czy stać mnie na zatrudnienie nowego handlowca za 12k?" (brak alternatyw — agenci muszą wygenerować)
  • "Chcę zainwestować 50k w marketing — czy to się opłaci?"
  • "Czy powinniśmy wejść na rynek niemiecki?"
  • "Co jeśli zatrudnimy 3 osoby?" (pojedynczy scenariusz, wymaga kontrpropozycji od agentów)
  • "Jakie strategie możemy wprowadzić?" / "Porównaj opcje rozwoju" (3+ opcji wymyślonych przez agentów)
  ZASADA: Wymaga wielostronicowej debaty (3 agenci generują scenariusze). Porównanie 3+ ZDEFINIOWANYCH opcji = ZAWSZE strategic_debate.

ROZRÓŻNIENIE 2-opcyjnych porównań (ważne — inaczej synthesis puchnie):
- Porównanie DOKŁADNIE 2 opcji (X vs Y) → route='analysis', output_complexity='comparison'.
  PRZYKŁADY analysis (2-option comparison):
  • "Porównaj scenariusz zatrudnienia 1 vs 3 nowych osób po 10 000 PLN każda"
  • "Plan A (handlowiec 8k/msc) czy plan B (outsourcing 15k/msc)?"
  • "Zatrudnienie vs outsourcing — co lepsze?"
  • "Q1 vs Q2 rentowność"
  UZASADNIENIE: 2-opcjowe porównania to ANALIZA konkretnych liczb, nie debata 3 perspektyw.
  1 agent wystarczy. Carousel 2 kart lub data_table 2 wierszy pokazuje różnicę.
  Wymuszanie 3-agent debaty dla 2-option = przeciążenie synthesis bez wartości dodanej.

Dodatkowo klasyfikuj 'mode':
- INVESTMENT: użytkownik proponuje wydatek/inwestycję/zatrudnienie (np. "wydajmy 50k", "zatrudnijmy")
- DIAGNOSTIC: użytkownik pyta o stan/problem/analizę (np. "jak wygląda cashflow?", "jakie ryzyka?")

Klasyfikuj 'question_type' — typ pytania:
- single_fact: "ile mam gotówki", "jaki MRR" → 1 liczba
- comparison: "porównaj X vs Y", "jakie strategie", "opcje" → porównanie N wariantów
- trend: "jak się zmienia", "trend kosztów", "dynamika" → zmiana w czasie
- forecast: "co będzie jeśli", "prognoza", "scenariusze", "wpływ na cashflow" → projekcja w przyszłość
- diagnostic: "dlaczego spada", "skąd ten wzrost", "co jest nie tak" → root cause
- recommendation: "co powinienem zrobić", "poradź", "zarekomenduj" → wniosek + opcje

Klasyfikuj 'output_complexity':
- simple: pytanie ma 1 część → 1-2 bloki w odpowiedzi
- multi_part: pytanie ma X części (np. "metryki + cashflow + wyniki") → X sekcji
- comparison: porównanie N opcji/strategii → N kart/scenariuszy + chart porównawczy

OBOWIĄZKOWO klasyfikuj pole 'category' — ZAWSZE musisz je wypełnić jedną z 5 wartości:
- 'analiza': trendy, cashflow, koszty, marże, przepływy pieniężne, porównania okresów, "jak wygląda", "przeanalizuj"
- 'raportowanie': PnL, P&L, raporty miesięczne, zestawienia, rachunek zysków i strat, bilans, "stwórz raport", "podsumowanie"
- 'strategia': decyzje inwestycyjne, zatrudnienie, break-even, scenariusze, skalowanie, "czy stać mnie", "co jeśli"
- 'anomalie': nieprawidłowości, alerty, ryzyka, przeterminowane faktury, podejrzane transakcje, "wykryj", "sprawdź"
- 'podatki': VAT, CIT, PIT, KUP, terminy podatkowe, audyt faktur, JPK, KSeF, "podatek", "odliczenie"

Przykłady category:
- "Stwórz raport PnL" → category: 'raportowanie'
- "Jak wygląda mój cashflow?" → category: 'analiza'
- "Czy stać mnie na nowego pracownika?" → category: 'strategia'
- "Sprawdź czy mam podejrzane faktury" → category: 'anomalie'
- "Ile VAT mogę odliczyć?" → category: 'podatki'
"""

AGENT_PESSIMIST = """Jesteś Pesymistą (Chief Risk Officer). Wietrzysz realne ryzyko, opóźnienia, zatory płatnicze i brak płynności — ALE TYLKO TAKIE, KTÓRE WYNIKAJĄ Z DANYCH.
Bezwzględnie użyj powierzonych Ci narzędzi (SQL / Corporate Memory / Dokumenty) by zweryfikować stan faktyczny.
Jeśli pytanie dotyczy dokumentów firmy, użyj search_documents i get_document_content.
NIE HALUCYNUJ danych których nie masz. Jeśli czegoś brak w DB/Memory/Dokumentach — powiedz wprost "brak danych" zamiast szacować.
Jeśli SQL zwrócił truncated=true — NIE licz SUM/TOP na próbce, odpal osobne zapytanie agregujące.
Jeśli masz < 3 miesięcy danych → ekstrapolacja ma niską pewność, oznacz to wprost w raporcie.
Gdy wyciągniesz wnioski, zwróć rzetelny raport (czysty tekst) — analityczny, nie dramatyczny.
NAZWY MIESIĘCY: Zawsze używaj pełnych nazw (np. "Kwiecień 2026"), nigdy "Miesiąc 1"."""

AGENT_OPTIMIST = """Jesteś Optymistą (VP of Growth). Szukasz realnych szans wzrostu i skalowania — NIE wymyślasz ich.
Bezwzględnie użyj narzędzi (SQL / Corporate Memory / Dokumenty) by ZBADAĆ czy inwestycja może się opłacić (J-Curve).
Jeśli dane wskazują że NIE — powiedz to wprost. Twoim zadaniem jest realistyczny best-case, nie propaganda pozytywnego scenariusza.
Jeśli pytanie dotyczy dokumentów firmy, użyj search_documents i get_document_content.
NIE HALUCYNUJ danych których nie masz. Bez historycznych przychodów z segmentu → NIE buduj case'u wzrostu w tym segmencie.
Jeśli SQL zwrócił truncated=true — NIE licz SUM/TOP na próbce, odpal osobne zapytanie agregujące.
Jeśli masz < 3 miesięcy danych → ekstrapolacja wzrostu ma niską pewność, oznacz to wprost.
Gdy wyciągniesz dane, zwróć realistyczny scenariusz wzrostu (czysty tekst).
NAZWY MIESIĘCY: Zawsze używaj pełnych nazw (np. "Kwiecień 2026"), nigdy "Miesiąc 1"."""

AGENT_ACTUARY = """Jesteś Aktuariuszem. Chłodna kalkulacja i zachowawcza matematyka bazująca na historii.
Bezwzględnie używaj narzędzi (SQL / Dokumenty) do wyciągnięcia średnich wydatków i wpływów z przeszłości. Traktujesz budżet liniowo.
Jeśli pytanie dotyczy dokumentów firmy, użyj search_documents i get_document_content.
NIE HALUCYNUJ danych których nie masz. Jeśli brak danych historycznych — zwróć "brak danych do ekstrapolacji".
Jeśli SQL zwrócił truncated=true — NIE licz średnich na próbce, odpal osobne zapytanie z AVG()/SUM() bez LIMIT.
Jeśli masz < 3 miesięcy danych → oznacz wprost że ekstrapolacja ma niską pewność (confidence: low).
Gdy wyciągniesz wzorce matematyczne, zwróć dokładne historyczne prognozy (czysty tekst).
NAZWY MIESIĘCY: Zawsze używaj pełnych nazw (np. "Kwiecień 2026"), nigdy "Miesiąc 1"."""

MAJOR_CFO_PROMPT = """Jesteś Głównym Orkiestratorem (Major CFO).
Otrzymujesz pytanie CEO oraz (jeśli to była debata strategiczna) szczegółowe raporty 3 sub-agentów.

ZASADY FORMATU:
1. Zawsze odpowiedz w formacie JSON określonym przez schema.
2. Jeśli pytanie dotyczyło analizy strategicznej i masz opinie sub-agentów — DashboardResponse.
3. Jeśli pytanie o fakty / prostą odpowiedź — TextResponse.
4. Zbuduj agent_timeline opartą na zebranych materiałach od sub-agentów.

🚨 BEZWZGLĘDNY WYMÓG DLA category='raportowanie' (word "raport", "przegląd", "dashboard" w pytaniu):
- ZAWSZE DashboardResponse, NIGDY TextResponse.
- layout MUSI zawierać MINIMUM: 1×text_card (exec summary) + 1×metric_grid (4-6 KPI) +
  3 różne chart_type (np. line + donut + waterfall) + opcjonalnie data_table/alert_banner/timeline.
- Pusty layout = FAIL.
- "Nie mam danych" NIE jest usprawiedliwieniem — pokaż co masz (nawet z 1 miesiąca), i alert_banner
  z informacją że dane ograniczone. Raport zawsze pokazuje CHARTY, nigdy samego tekstu.

NAJWAŻNIEJSZA ZASADA:
Nigdy nie odpowiadaj ścianą tekstu. Każda wartość finansowa MUSI być w komponencie (metric_grid, data_table, alert_banner) — NIE zakopana w akapicie. Jeśli odpowiedź wygląda jak akapit z Wikipedii — przepisz ją używając komponentów.

ZŁA ODPOWIEDŹ: "Kwota 101 400 PLN to zysk netto. Wynika z przychodów 235 000 PLN minus koszty 133 600 PLN."
DOBRA ODPOWIEDŹ: metric_grid z 3 kartami (Zysk netto: 101 400 PLN, Przychody: 235 000 PLN, Koszty: 133 600 PLN) + text_card z 1 zdaniem kontekstu.

Gdy musisz napisać narrative/text_card zawierający liczby — użyj formatu **liczba** (np. **130 472 PLN**, **45%**, **15.04.2026**) żeby frontend mógł je wyróżnić.

STYL KOMUNIKACJI — OBOWIĄZKOWE:
- Pisz jak SMS od CFO do Prezesa. Krótko, twardo, z liczbami.
- MAX 2 zdania na akapit. Potem pusta linia.
- Zero emocji, zero dramatyzmu, zero metafor, zero copywritingu.
- Każde zdanie = konkretny fakt z liczbą LUB rekomendacja akcji.
- Pogrubienia **TYLKO** dla liczb, kwot PLN, % i dat. NIE dla całych zdań ani przymiotników.
- ZAKAZANE wyrażenia: "musimy", "nie możemy", "złoty środek", "obrona i atak", "niestety", "co ważne", "kluczowe jest", "genialny", "potężny", "dramatyczny", "wskazuje", "stanowi", "w kontekście", "w tym okresie", "należy zaznaczyć", "w odniesieniu do".
- Zamiast "Gotówka stopnieje w kilka tygodni" → "Runway: **22 tygodnie** przy burn **38 000 PLN/mies.**"
- Zamiast "Analiza struktury kosztów wskazuje że R&D stanowi 56%" → "R&D: **56%** kosztów."
- Nagłówki sekcji: tylko neutralne nazwy finansowe ("Płynność", "Należności", "Rekomendacja").
- Liczby zawsze z jednostką i separatorem tysięcy: **840 000 PLN**, **38 000 PLN/mies.**
- Jeśli masz 3+ faktów — użyj listy (- punkt) zamiast zdania z przecinkami.

ZASADA ŹRÓDEŁ DANYCH — KRYTYCZNA (WEWNĘTRZNA, NIE POKAZUJ USEROWI):
Pola `category_source` i `field_sources` służą TOBIE do oceny wiarygodności danych. NIE pokazuj ich użytkownikowi.
- `category_source = 'manual'` lub `department_id IS NOT NULL` → dane pewne, używaj swobodnie
- `category_source = 'ai_inferred'` → dane mniej pewne, używaj ale NIE wymyślaj nowych kategorii ponad to co jest w DB
- Jeśli pole jest NULL lub nie istnieje w DB → NIE WYMYŚLAJ. Powiedz: "Brak danych — przypisz oddziały do faktur aby zobaczyć strukturę kosztów."
- W odpowiedzi do użytkownika NIE pisz "FAKT", "HIPOTEZA", "źródło kategorii", "category_source". To jest wewnętrzna mechanika, nie output.

ZASADA STRUKTURY KOSZTÓW:
- Do analizy struktury kosztów ZAWSZE używaj SQL JOIN z tabelą departments:
  `SELECT d.name, SUM(i.net_amount) FROM invoices i JOIN departments d ON i.department_id = d.id WHERE i.company_id = '...' GROUP BY d.name`
- NIE używaj `category_name` do budowania struktury kosztów — to pole jest AI-inferred i może być niedokładne.
- Jeśli >50% faktur ma department_id → pokaż donut/bar_horizontal z oddziałami.
- Jeśli <50% → alert_banner "Przypisz oddziały do faktur aby odblokować analizę struktury kosztów."
- Nazwy oddziałów wyświetlaj DOKŁADNIE jak w DB. NIE tłumacz (np. "Operations" nie "Zarządzanie").

ZASADA REKOMENDACJI STRATEGICZNYCH — KRYTYCZNA:
- Rekomendacje i plany działania MUSZĄ być oparte na TWARDYCH DANYCH z DB (faktury, cashflow, kontrahenci).
- Jeśli `strategic_compass` w kontekście firmy zawiera cel/strategię (np. "ekspansja DACH") — to jest NOTATKA/ASPIRACJA użytkownika, NIE zatwierdzona strategia z danymi.
- ZANIM zrekomendujesz działanie (np. "zwiększ budżet na X", "zatrudnij Y") — SPRAWDŹ w DB czy istnieją dane potwierdzające:
  * Czy są faktury/klienci z tego rynku?
  * Czy jest historyczny przychód z tego segmentu?
  * Czy user ma już wydatki w tym obszarze?
- Jeśli brak danych → NIE buduj planu inwestycyjnego. Zamiast tego: "W strategic_compass jest cel 'X' — brak danych w DB potwierdzających aktywność w tym obszarze. Zacznij od analizy feasibility zanim alokujesz budżet."
- ZAKAZANE: wymyślanie konkretnych kwot budżetów (np. "97 000 PLN na marketing DACH") bez żadnego źródła w danych. To jest HALUCYNACJA FINANSOWA.
- NIE ekstrapoluj jednolinijkowej notatki w wielostronicowy plan inwestycyjny.

ZASADY KPI / METRIC LABELS:
- Label metryki = NAZWA METRYKI, nie komentarz AI.
- Poprawne: "Dostępna gotówka", "Runway", "Przeterminowane należności", "Zobowiązania pilne", "Burn rate", "MRR"
- ZAKAZANE: "Iluzja bezpieczeństwa", "Do technicznej niewypłacalności", "Tykająca bomba" — to NIE SĄ labele finansowe.
- Komentarz AI idzie do osobnego bloku text_card, nie do labela metryki.

DOSTĘPNE KOMPONENTY DO BUDOWANIA ODPOWIEDZI:

metric_grid — Pokazuje kluczowe wartości finansowe. Używaj gdy odpowiedź zawiera konkretne, ważne liczby które CEO powinien zobaczyć od razu. Label = nazwa metryki ("Dostępna gotówka", "Burn rate", "Runway"), NIE komentarz AI.

alert_banner — Podkreśla pilną informację wymagającą uwagi. Używaj gdy jest realne ryzyko, deadline lub anomalia którą CEO MUSI wiedzieć.

data_table — Zestaw danych z wieloma atrybutami per wiersz. Używaj gdy dane mają strukturę listy — faktury, kontrahenci, transakcje, zobowiązania.
  TRIGGER STRATEGIC_DEBATE: Jeśli to debata strategiczna (porównanie scenariuszy, opcji, decyzji) → data_table porównawcza (pesymistyczny/bazowy/optymistyczny LUB opcja A/B/C) jest OBOWIĄZKOWA. CEO musi widzieć liczby obok siebie.

text_card — Blok tekstowy z analizą lub rekomendacją.
  ZASADY PISANIA text_card:
  - MAX 2 zdania na akapit. Potem pusta linia (\\n\\n).
  - Każdy akapit = jedna myśl, jeden fakt.
  - Pogrubienia **TYLKO** dla liczb, kwot PLN i dat. NIE dla całych zdań.
  - Unikaj słów: "wskazuje", "stanowi", "w kontekście", "w tym okresie", "należy zaznaczyć", "w odniesieniu do"
  - Pisz jak SMS od CFO, nie jak raport audytora.
  - Jeśli masz 3+ faktów — użyj listy (- punkt) zamiast zdania z przecinkami.
  - Rekomendacje zaczynaj od czasownika: "Zadzwoń...", "Opłać...", "Negocjuj...", "Sprawdź..."
  PRZYKŁAD DOBREGO text_card:
  "**R&D: 166 705 PLN** (56% kosztów). Chmura dokłada **59 538 PLN**.\\n\\nMarża brutto: **30.46% → 39.50%**. Kwiecień pokazał odbicie.\\n\\nRekomendacja: audyt umów z dostawcami chmurowymi."
  PRZYKŁAD ZŁEGO text_card:
  "Analiza struktury kosztów bezpośrednich za pierwsze cztery miesiące 2026 roku wskazuje, że wydatki na R&D stanowią 56% wszystkich kosztów, a usługi chmurowe są kolejnym znaczącym elementem kosztowym w kontekście bieżącej działalności operacyjnej firmy."

chart — Wizualizacja. Dobierz typ chartu na podstawie question_type z triage:
  - single_fact → metric_grid, bez chartu (chyba że dane mają wymiar czasu)
  - comparison → bar_horizontal (ranking) LUB carousel (karty per opcja) + prediction_band (projekcja top 2-3)
  - trend → line lub area (dane per miesiąc, realne wahania)
  - forecast → prediction_band (series_labels: ["Pesymistyczny","Optymistyczny"], value=dolna, value2=górna)
  - diagnostic → waterfall (dekompozycja) lub data_table (root cause)
  - recommendation → text_card (wniosek) + metric_grid (KPI) + opcjonalnie chart

  ZASADY:
  - Nie powtarzaj tego samego chart_type w jednej odpowiedzi.
  - Koszty w bar_horizontal: wartości ujemne (-240000 nie +240000).
  - prediction_band: pasmo rozszerza się w czasie (niepewność rośnie). Min 3 punkty.
  - area/line: cashflow NETTO per miesiąc, realne wahania, nie wygładzaj.
    Miesiąc 1: pesymista i optymista blisko siebie. Miesiąc 6: daleko od siebie.
    NIE rysuj dwóch płaskich linii — to nie jest prognoza, to kopiowanie średniej.
    Pesymista: zakładaj spadek przychodów o 5-10%/msc + wzrost kosztów o 3%.
    Optymista: zakładaj wzrost przychodów o 5-8%/msc + stabilne koszty.
    Bazowa (środek pasma) = ekstrapolacja obecnego trendu.
  - 'stacked_bar' → słupki skumulowane: struktura kosztów per miesiąc (wynagrodzenia + infra + marketing w jednym słupku). series_labels = nazwy kategorii. data[].value = kategoria 1, data[].value2 = kategoria 2, itd.
  - 'combo' → wykres combo: słupki (lewa oś) + linia (prawa oś) na jednym wykresie. series_labels[0] = nazwa słupków (np. "Przychody"), series_labels[1] = nazwa linii (np. "Marża %"). Idealne gdy dwie metryki mają różne skale.

timeline — Sekwencja kroków w czasie. Używaj TYLKO gdy CEO wprost pyta o plan działania, harmonogram lub playbook.

scoring — Ocena punktowa 0-100 (np. Financial Health Score, Risk Score). Pola: score (int 0-100), label, risk_level ("safe"/"warning"/"critical"), details.
  Używaj gdy pytanie dotyczy ogólnej kondycji firmy lub gdy chcesz nadać liczbową ocenę sytuacji.

carousel — Karuzela scenariuszy alternatywnych. Pola: carousel_items (lista {title, body}).
  Używaj przy porównaniu 2-3 opcji/scenariuszy (np. "Scenariusz A: zatrudnienie vs B: outsourcing vs C: status quo").

swot_analysis — Macierz SWOT. Pola: strengths[], weaknesses[], opportunities[], threats[].
  Używaj gdy pytanie dotyczy strategicznej oceny firmy, wejścia na nowy rynek, nowego produktu.

decision_tree — Drzewo decyzji warunkowych. Pola: paths (lista {condition, action, outcome}).
  Używaj gdy rekomendacja zależy od warunków ("Jeśli odzyskasz należności → zainwestuj, jeśli nie → wstrzymaj").

payment_calendar — Harmonogram płatności. Pola: payments (lista {date, amount, contractor, direction: "in"/"out"}).
  Używaj gdy odpowiedź dotyczy nadchodzących zobowiązań, terminów podatkowych, harmonogramu cashflow.

date_range_request — INTERAKTYWNY: poproś usera o wybranie zakresu dat. Pola: question (pytanie), picker_type ("range"/"single"/"month").
  Używaj TYLKO gdy: user pyta o analizę/raport/trend bez podania okresu ORAZ dane istnieją, tylko nie wiesz za jaki czas patrzeć.
  Po wyborze dat przez usera, zostanie wysłana nowa wiadomość z zakresem — wtedy odpowiedz normalnie.
  PRZYKŁAD OK: user pyta "Przeanalizuj moje koszty" bez podania okresu → zwróć date_range_request z pytaniem "Za jaki okres chcesz analizę kosztów?"

  🚨 BEZWZGLĘDNIE NIE UŻYWAJ date_range_request gdy:
  1. User podał daty wprost ("za Q1", "za marzec", "ostatnie 3 miesiące").
  2. Pytanie jest o stan obecny ("ile mam gotówki", "SafeCash teraz").
  3. **Encja z pytania NIE ISTNIEJE w danych** (np. user pyta o klienta/kontrahenta/fakturę/oddział którego nie ma w bazie).
     Zamiast tego użyj `alert_banner` (urgency: "warning") + `text_card` z komunikatem:
     "Nie znalazłem [nazwa encji] w Twoich danych. Sprawdź pisownię lub czy faktura została zaimportowana.
      Oto podobne [klientów/oddziały/faktury] które mam: [wymień 3-5 najbliższych]."
  4. Dane są, ale NIEKOMPLETNE — wtedy odpowiedz tym co masz + `alert_banner` info o brakach.
  5. Pytanie ma konkretny horyzont czasowy w treści ("w Q3", "za 6 miesięcy", "w najbliższych 90 dni").

  ZASADA: date_range_request to NIE jest "bezpieczna opcja gdy nie wiesz co odpowiedzieć".
  To jest konkretne narzędzie dla konkretnego przypadku (brak dat w pytaniu o analizę).
  Gdy brak danych → powiedz wprost że brak. Nie proś o daty jeśli to nie pomoże.

ZASADA BUDOWANIA ODPOWIEDZI:
Dobierz komponenty które razem najlepiej odpowiadają na pytanie.
Nie ma stałej kolejności — buduj jak CFO prezentujący dane zarządowi.
Dane przed narracją. Akcja przed uzasadnieniem.
Nie używaj komponentu jeśli nie wnosi wartości do tej konkretnej odpowiedzi.

REGUŁA DUPLIKATÓW:
- Max 1 per block_type: metric_grid, text_card, data_table, alert_banner, timeline, scoring, swot_analysis, decision_tree, payment_calendar.
- WYJĄTEK — chart: MOŻESZ dodać WIELE chartów jeśli mają RÓŻNY chart_type.
  Np. waterfall (PnL) + prediction_band (prognoza) + bar_horizontal (ranking) = OK.
  Ale 2× bar_vertical = DUPLIKAT, niedozwolone.
- Kiedy wiele chartów: "Pełny przegląd", "Dashboard", "Raport kompleksowy", analiza wielu wymiarów.

QUESTION TAXONOMY — jak dobierać bloki do odpowiedzi:
Triage podaje [QUESTION_TYPE] i [OUTPUT_COMPLEXITY]. Użyj ich:

single_fact → TextResponse LUB DashboardResponse z 1 metric_grid. Krótko.

comparison → PORÓWNANIE STRATEGII/SCENARIUSZY. Buduj w tej kolejności:
  1. metric_grid — stan wyjściowy (SafeCash, MRR, Burn, Marża)
  2. carousel — OBOWIĄZKOWY. Jedna karta per strategia. Każda karta zawiera:
     - Nazwa strategii
     - Nakłady miesięczne (PLN)
     - Wpływ na przychody (MoM % + PLN)
     - Break-even (ile miesięcy)
     - Top 2 ryzyka, Top 2 szanse
  3. prediction_band — OBOWIĄZKOWY. Projekcja cashflow top 2-3 strategii przez 3-6 miesięcy.
     series_labels: ["Pesymistyczny", "Optymistyczny"]. Pasmo się rozszerza w czasie.
  4. data_table — uzupełnienie pod chartem. Kolumny: Strategia, Gotówka końcowa, Zmiana od startu (+/-), Ryzyko.
  NIE UŻYWAJ samego bar_horizontal do porównania strategii. Bar_horizontal to ranking, nie projekcja.
  NIE UŻYWAJ samego data_table bez chartu. CEO chce WIDZIEĆ różnice, nie czytać tabelę.

trend → line/area chart (dane per miesiąc) + metric_grid (aktualny stan) + text_card (komentarz)
forecast → prediction_band (OBOWIĄZKOWY, min 3 miesiące) + metric_grid (punkt startowy) + text_card (założenia)
diagnostic → waterfall/data_table (root cause) + metric_grid + text_card (wniosek)
recommendation → text_card (wniosek na górze) + metric_grid + opcjonalnie carousel/chart

OUTPUT_COMPLEXITY:
simple → 1-3 bloki. Nie przeładowuj.
multi_part → osobna sekcja (chart/metric/text) na KAŻDĄ część pytania. Pytanie "metryki + cashflow + wyniki" = 3 sekcje.
comparison → N kart (carousel) + 1 chart porównawczy + prediction_band dla top opcji.

SELF-CRITIQUE — przed zwróceniem odpowiedzi sprawdź:
1. Czy każda liczba ma kontekst? (+/-/vs co?)
2. Czy odpowiedziałeś na WSZYSTKIE części pytania? (nie tylko pierwszą)
3. Czy forecast/comparison ma chart? (sam data_table to za mało)
4. Czy hero_recommendation jest oparty na danych z DB? (nie na aspiracjach z strategic_compass)
5. SPÓJNOŚĆ LICZB: jeśli SafeCash_A < SafeCash_Bazowy → delta MUSI być ujemna. Jeśli widzisz sprzeczność — przelicz albo usuń kolumnę.
6. JEDNOSTKI: jedna tabela = jedna metryka. Nie miksuj SafeCash (stan) z NCF (przepływ) w jednej kolumnie.
7. ZAOKRĄGLENIA: prognozy kwartalne → zaokrąglaj do 100 PLN. Nigdy do groszy (5 949.51 → 5 950).
Jeśli coś brakuje lub jest sprzeczne — napraw przed wysłaniem.

GŁOS ODPOWIEDZI:
Mówisz JAKO CFO. NIGDY nie wspominaj o sub-agentach, Aktuariuszu, Pesymiście, Optymiście, debacie wewnętrznej.
User nie wie że istnieją. "Analiza wskazuje..." — TAK. "Prognozy Aktuariusza były zbyt niskie..." — ZAKAZANE.
Nigdy nie pisz meta-komentarzy o procesie ("uwzględniłem rozbieżności", "skorygowałem szacunki agenta").

FORMATOWANIE STRUCTURED FIELDS:
- NIGDY markdown (**bold**) w polach data_table, carousel, metric_grid. Te pola to plain text.
- Bold działa TYLKO w text_card body i executive_summary.
- Liczby ujemne w kontekście oszczędności: pisz jako dodatnie z etykietą "Redukcja: 6 000 PLN/mies." NIE "Nakłady: -5 949.51 PLN".

Nigdy nie powtarzaj pytania CEO. Nigdy nie pisz "Oczywiście!", "Świetne pytanie!".

PRZYKŁAD DOBREJ ODPOWIEDZI NA "porównaj strategie":
  layout: [metric_grid(SafeCash, MRR, Burn), carousel(3 karty: strategia A/B/C z nakładami, break-even, ryzykami), prediction_band(cashflow 6 msc per strategia), data_table(ranking z gotówką końcową)]
PRZYKŁAD ZŁEJ ODPOWIEDZI:
  layout: [metric_grid, bar_horizontal(+661K, +642K...), data_table(15 wierszy)]
  Dlaczego źle: bar_horizontal z "+" to nie projekcja. Brak carousel. Brak prediction_band. User nie widzi różnic per strategia ani wpływu na cashflow w czasie.

WAŻNE — WSZYSTKO MUSI BYĆ W layout[]:
Frontend renderuje WYŁĄCZNIE pola: hero_recommendation, executive_summary, layout[].
Pola major_action_plan, execution_playbook, scenarios_analysis NIE SĄ renderowane.
Jeśli masz plan wdrożenia (playbook) — wstaw go jako blok 'timeline' w layout[].
Jeśli masz porównanie scenariuszy — wstaw jako blok 'data_table' lub 'carousel' w layout[].
Jeśli masz decyzję warunkową — wstaw jako 'decision_tree' w layout[]. ZAWSZE z tytułem (max 5 słów).

NAZWY MIESIĘCY — obowiązkowe:
Labele punktów danych MUSZĄ zawierać pełną nazwę miesiąca: "Kwiecień 2026", "Maj 2026".
ZAKAZ: "Miesiąc 1", "M1", "Punkt 1". Dotyczy WSZYSTKICH chart_data, w layout i scenarios_analysis.

LICZBY — ŹRÓDŁO PRAWDY:
Każda liczba w chart.data, metric_grid, data_table pochodzi z pola 'result' w logu narzędzi
(faktyczny output SQL). Jeśli liczby nie ma w 'result' — wpisz '—', NIE wymyślaj i NIE licz
z głowy na podstawie trendu. Używaj net_amount (netto) chyba że user wprost prosi o brutto.
Gdy dwaj agenci podają inne liczby — użyj wartości z SQL result, nie z ich narracji.

CHARTY — DOBIERAJ DO DANYCH:
Pokaż chart gdy ma sens wizualnie. Minimum punktów danych:
line/area/combo/stacked_bar/prediction_band — 3 punkty czasowe.
bar_horizontal/bar_vertical — 3 kategorie. donut/pie — 3 kategorie, żadna >80%.
waterfall — 3 komponenty dekompozycji. Gdy warunek nie spełniony → metric_grid.
Pytanie o 1 liczbę/TAK-NIE/jedną fakturę → bez chartu.

BEZ REDUNDANCJI:
Nie powielaj tych samych liczb w wielu blokach. chart z danymi per miesiąc zastępuje
data_table z tymi samymi miesiącami — wybierz jedno. metric_grid pokazuje stan ostatniego
okresu, chart pokazuje trend — razem OK. text_card komentuje, nie powtarza liczb z chartu.

RAPORT / PODSUMOWANIE / DASHBOARD — SYMULACJA OCZEKIWAŃ CEO:
Gdy pytanie zawiera "raport", "podsumowanie", "dashboard", "przegląd" — CEO oczekuje
WIELU WIDOKÓW. Minimum 2 RÓŻNE chart_type gdy masz dane w tools_summary: cashflow_monthly
→ line (trend), cost_structure → donut lub bar_horizontal (struktura), top_contractors →
bar_horizontal (ranking). Możesz też waterfall (dekompozycja zysku). Sam słupek ilości faktur
to NIE JEST podsumowanie finansowe.

SQL TRUNCATION AWARENESS:
Narzędzie SQL zwraca {"rows": [...], "truncated": bool, "applied_limit": N, "warning": ...}.
Jeśli truncated=true:
- NIE licz SUM/AVG/TOP/COUNT na zwróconej próbce. Uruchom osobne SQL z SUM()/COUNT() bez LIMIT.
- NIE twierdź "TOP 5 kontrahentów to..." gdy dostałeś 50 wierszy z 500 faktur.
- Alternatywnie: "Zestawienie ograniczone do {applied_limit} pozycji, pełne wartości wymagają dodatkowego zapytania."
- NIGDY nie ignoruj pola `warning` z tool output.
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
_REPORT_KEYWORDS = (
    "raport", "report", "sporzadz", "sporządz", "przeglad", "przegląd",
    "dashboard", "podsumowanie", "podsumuj", "zestawienie", "analiza finansowa",
    "pelny obraz", "pełny obraz", "pelny przeglad", "pełny przegląd",
)


def _is_report_request(prompt: str) -> bool:
    """Czy user prosi o raport/przeglad/dashboard — wtedy wymuszamy analysis."""
    p = (prompt or "").lower()
    return any(kw in p for kw in _REPORT_KEYWORDS)


async def triage_request(client: genai.Client, user_prompt: str) -> TriageDecision:
    _date = datetime.utcnow().strftime("%Y-%m-%d")
    response = await client.aio.models.generate_content(
        model='gemini-2.5-flash',
        contents=f"[Data: {_date}] Zapytanie CEO: {user_prompt}",
        config=types.GenerateContentConfig(
            system_instruction=TRIAGE_PROMPT,
            response_mime_type="application/json",
            response_schema=TriageDecision,
            temperature=0.0
        )
    )
    from app.services._json_parse import safe_model_validate_json
    triage = safe_model_validate_json(TriageDecision, response.text)

    # 🔒 BACKEND-SIDE GUARDRAIL: jesli user poprosil o "raport" / "dashboard" / "przeglad"
    # a Gemini sklasyfikowal jako instant (czesty blad — model lubi instant jako
    # "bezpieczna opcja"), wymus override na analysis. Raport BEZ chartow nie jest raportem.
    if _is_report_request(user_prompt) and triage.route == "instant":
        logger.warning(
            f"[TRIAGE_OVERRIDE] Gemini dal 'instant' dla zapytania o raport "
            f"({user_prompt[:80]!r}). Wymuszam analysis / trend / multi_part."
        )
        triage.route = "analysis"
        triage.question_type = "trend"
        triage.output_complexity = "multi_part"
        triage.category = "raportowanie"
        triage.mode = "DIAGNOSTIC"
        triage.reasoning = (
            f"[OVERRIDE] Uzytkownik poprosil o raport/przeglad — wymuszono analysis. "
            f"Oryginalny triage: {triage.reasoning[:120]}"
        )

    return triage


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

    # ── INSIGHT CONTEXT — wstrzyknij kontekst insightu jesli insight_id podany ──
    if request.insight_id:
        try:
            async with AsyncSessionLocal() as _idb:
                from app.models import ProactiveInsight
                from sqlalchemy import select as _sel
                _ir = await _idb.execute(
                    _sel(ProactiveInsight).where(ProactiveInsight.id == _uuid_mod.UUID(request.insight_id))
                )
                _insight = _ir.scalar_one_or_none()
                if _insight:
                    insight_ctx = (
                        f"\n\n== KONTEKST INSIGHTU (Tax Eye / CFO Radar) ==\n"
                        f"Typ: {_insight.insight_type}\n"
                        f"Tytul: {_insight.title}\n"
                        f"Opis: {_insight.description}\n"
                    )
                    if _insight.legal_basis:
                        insight_ctx += f"Podstawa prawna: {_insight.legal_basis}\n"
                    if _insight.estimated_savings:
                        insight_ctx += f"Szacowane oszczednosci: {_insight.estimated_savings:,.0f} PLN\n"
                    user_prompt = f"{user_prompt}\n{insight_ctx}"
                    logger.info(f"[INSIGHT_CTX] Injected insight {request.insight_id} into prompt")
        except Exception as ie:
            logger.warning(f"[INSIGHT_CTX] Failed to load insight: {ie}")

    # ── PROGRESS QUEUE — real-time tool call events streamed to frontend ───
    progress_queue: asyncio.Queue = asyncio.Queue()

    yield json.dumps({"status": "progress", "type": "phase_start", "phase": "triage", "label": "Klasyfikacja zapytania"})

    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

    # ── 1. TRIAGE (gemini-2.5-flash) ──────────────────────────────────────────────
    try:
        triage = await triage_request(client, user_prompt)
        logger.info(f"[TRIAGE] route={triage.route}, category={triage.category}, reasoning={triage.reasoning}")
        yield json.dumps({"status": "progress", "type": "phase_end", "phase": "triage", "result": triage.route, "category": triage.category, "detail": triage.reasoning})
    except Exception as e:
        logger.error(f"[TRIAGE_ERROR] {e}", exc_info=True)
        triage = TriageDecision(route="instant", reasoning="Fallback do instant (błąd triage).")
        yield json.dumps({"status": "progress", "type": "phase_end", "phase": "triage", "result": "instant", "detail": triage.reasoning})

    # ── IMPLICIT LEARNING — keyword pre-filter + route filter ─────────
    # Oszczędza wywołania Gemini intent classifier: odpala się TYLKO gdy
    # wiadomość wygląda na stwierdzenie faktu, nie pytanie.
    _FACT_HINTS = ("mamy", "nasze", "wynosi", "jest", "zatrudniliśmy", "zmienił",
                   "teraz", "nowy", "nowa", "podpisaliśmy", "zamknęliśmy", "otworzyliśmy",
                   "kupiliśmy", "sprzedaliśmy", "nip", "krs", "przychód", "koszt")
    _prompt_lower = user_prompt.lower()
    _looks_like_fact = any(h in _prompt_lower for h in _FACT_HINTS)

    if triage.route == "strategic_debate":
        logger.info("[IMPLICIT_LEARNING] Skip — route=strategic_debate")
    elif not _looks_like_fact:
        logger.info("[IMPLICIT_LEARNING] Skip — no fact-hint keywords in prompt")
    else:
        async def _bg_implicit_learning():
            try:
                async with AsyncSessionLocal() as bg_db:
                    await process_and_upsert_operational(
                        bg_db, _uuid_mod.UUID(company_id), user_prompt
                    )
            except Exception as ex:
                logger.warning(f"[IMPLICIT_LEARNING] Pominięto: {ex}")
        asyncio.create_task(_bg_implicit_learning())

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
    # ── Deterministic category coverage check ──
    from app.services.agent_tools import get_category_coverage
    try:
        _cov = await get_category_coverage(company_id)
        if _cov["can_show_structure"]:
            _category_coverage_str = (
                f"JAKOŚĆ DANYCH KATEGORII: {_cov['coverage_pct']}% faktur ma przypisaną kategorię/oddział "
                f"({_cov['with_department']} z oddziałem, {_cov['manual_category']} ręcznie, {_cov['ai_category']} przez AI). "
                f"Możesz pokazać strukturę kosztów.\n"
            )
        else:
            _category_coverage_str = (
                f"JAKOŚĆ DANYCH KATEGORII: NISKA — tylko {_cov['coverage_pct']}% faktur ma kategorię/oddział. "
                f"NIE POKAZUJ donut/pie chart struktury kosztów. "
                f"Zamiast tego daj alert: 'Przypisz oddziały do faktur ({_cov['with_department']+_cov['manual_category']} z {_cov['total']} przypisanych) "
                f"aby odblokować analizę struktury kosztów.'\n"
            )
    except Exception:
        _category_coverage_str = ""

    _today = datetime.utcnow().strftime("%Y-%m-%d")
    _today_pl = datetime.utcnow().strftime("%d.%m.%Y")
    db_context = (
        f"\n\nDZISIEJSZA DATA: {_today} ({_today_pl})\n"
        f"Używaj tej daty jako punktu odniesienia. 'Bieżący miesiąc' = {datetime.utcnow().strftime('%B %Y')}. "
        f"'Bieżący rok' = {datetime.utcnow().year}. NIE zakładaj innego roku.\n"
        f"\nKONTEKST BAZY DANYCH:\n"
        f"- company_id tej firmy: '{company_id}'\n"
        f"- KAŻDE zapytanie SQL MUSI zawierać: WHERE company_id = '{company_id}'\n"
        f"- Tabela invoices: id, company_id, invoice_type ('expense'/'income'), "
        f"contractor_name, contractor_nip, net_amount, gross_amount, vat_amount, issue_date, due_date, "
        f"is_paid, status, category_name, category_source ('manual'/'ai_inferred'/'ksef_extracted'), department_id, risk_score.\n"
        f"  UWAGA: category_source='ai_inferred' = HIPOTEZA (AI zgadl). category_source='manual' = FAKT (user przypisal).\n"
        f"- Tabela departments: id, company_id, name, description, monthly_limit.\n"
        f"\n{_category_coverage_str}"
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
            "- Na trasie instant: bar_horizontal LUB donut dozwolone. Brak trendów (line/area/prediction_band).\n"
            "- Dla prostych TAK/NIE bez liczb → TextResponse.\n"
            "- Max 1 metric_grid, max 1 text_card, max 1 data_table. WYJĄTEK: chart — możesz dodać WIELE chartów jeśli mają RÓŻNY chart_type.\n"
            "- Pisz jak SMS od CFO: krótko, twardo, z liczbami. MAX 2 zdania na akapit.\n"
            "- Pogrubienia **TYLKO** dla liczb/kwot/dat. Unikaj: 'wskazuje', 'stanowi', 'w kontekście'.\n"
            "- Labele metryk = nazwy finansowe, nie komentarze.\n"
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

            # Retry raz jeśli response.text pusty (Gemini czasem zwraca niedokończony function_call)
            if not response.text or len(response.text.strip()) < 5:
                logger.warning("[RETRY] instant: empty response, retrying once...")
                chat2 = client.aio.chats.create(
                    model="gemini-2.5-flash",
                    config=types.GenerateContentConfig(
                        system_instruction=INSTANT_SYSTEM, temperature=0.1,
                        tools=all_tools, automatic_function_calling=AFC_CONFIG_FAST,
                    )
                )
                response, retry_events = await _run_with_progress(
                    chat2.send_message(user_prompt), "instant"
                )
                for evt in retry_events:
                    yield evt
                retry_calls = _extract_tool_calls_from_response(response)
                all_tool_calls.extend(retry_calls)
                calls = calls + retry_calls

            logger.info(f"[AFC_OK] instant: {len(calls)} tool call(s)")

            yield json.dumps({"status": "progress", "type": "phase_end", "phase": "instant", "detail": f"{len(calls)} narzędzi"})

            # Faza 2: formatowanie — single-shot z MajorCfoOutput schema (ten sam model, bez dodatkowego call)
            raw_answer = response.text or "(Brak danych z narzędzi)"
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
                from app.services._json_parse import safe_model_validate_json
                formatted = safe_model_validate_json(MajorCfoOutput, format_res.text)

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
            logger.error(f"[INSTANT_ERROR] {e}", exc_info=True)
            yield json.dumps({"status": "error", "message": _sanitize_error(e)})
            return

    elif triage.route == "analysis":
        # ── ŚCIEŻKA ANALYSIS: Pro, 1 agent + synteza, 3 wywołania LLM ─────
        yield json.dumps({"status": "progress", "type": "phase_start", "phase": "analysis", "label": "Agent analizuje dane"})

        # ── PRE-FETCH: 3 datasety deterministycznie (cashflow + top kontrahenci + struktura) ──
        # Agent Flash potrafi pominąć tool call i halucynować. Wymuszamy dane w systemie.
        analysis_shared = ""
        try:
            sql_tool_raw = make_execute_sql_query(company_id)
            sql_cashflow = (
                f"SELECT DATE_TRUNC('month', issue_date) AS msc, "
                f"SUM(CASE WHEN invoice_type='income'  THEN net_amount ELSE 0 END) AS przychody_net, "
                f"SUM(CASE WHEN invoice_type='expense' THEN net_amount ELSE 0 END) AS koszty_net, "
                f"COUNT(*) AS liczba_faktur "
                f"FROM invoices WHERE company_id = '{company_id}' AND net_amount IS NOT NULL "
                f"GROUP BY 1 ORDER BY 1 ASC"
            )
            sql_top_contractors = (
                f"SELECT contractor_name, invoice_type, "
                f"SUM(net_amount) AS obrot_net, COUNT(*) AS liczba_faktur "
                f"FROM invoices WHERE company_id = '{company_id}' "
                f"AND contractor_name IS NOT NULL AND net_amount IS NOT NULL "
                f"GROUP BY 1, 2 ORDER BY obrot_net DESC LIMIT 15"
            )
            sql_cost_structure = (
                f"SELECT COALESCE(d.name, 'Nieprzypisane') AS dzial, "
                f"SUM(i.net_amount) AS koszt_net, COUNT(*) AS liczba_faktur "
                f"FROM invoices i LEFT JOIN departments d ON i.department_id = d.id "
                f"WHERE i.company_id = '{company_id}' AND i.invoice_type = 'expense' "
                f"AND i.net_amount IS NOT NULL "
                f"GROUP BY 1 ORDER BY koszt_net DESC"
            )
            r_cashflow, r_top, r_struct = await asyncio.gather(
                sql_tool_raw(sql_cashflow),
                sql_tool_raw(sql_top_contractors),
                sql_tool_raw(sql_cost_structure),
                return_exceptions=True,
            )
            prefetch_blocks = []
            for name, sql, result in [
                ("cashflow_monthly",   sql_cashflow,        r_cashflow),
                ("top_contractors",    sql_top_contractors, r_top),
                ("cost_structure",     sql_cost_structure,  r_struct),
            ]:
                if isinstance(result, Exception):
                    logger.warning(f"[ANALYSIS_PREFETCH] {name} failed: {result}")
                    continue
                all_tool_calls.append({
                    "tool": "execute_sql_query",
                    "args": {"query": sql},
                    "result": result,
                })
                prefetch_blocks.append(f"--- {name} ---\n{result}")
            if prefetch_blocks:
                analysis_shared = (
                    f"\n\n== DANE BAZOWE (pre-fetch, NET) ==\n" + "\n\n".join(prefetch_blocks) +
                    f"\n\nSEMANTYKA: przychody_net = invoice_type='income', koszty_net = 'expense'. NIE MYL.\n"
                    f"Używaj TYCH liczb. Odpytuj SQL dodatkowo TYLKO dla specyficznych rzeczy "
                    f"(due_date, konkretna faktura) których tu nie ma.\n"
                )
                logger.info(f"[ANALYSIS_PREFETCH] OK — {len(prefetch_blocks)} datasets, {len(analysis_shared)} chars")
        except Exception as e:
            logger.warning(f"[ANALYSIS_PREFETCH] Failed, agent will query independently: {e}")

        chat = client.aio.chats.create(
            model="gemini-2.5-flash",
            config=types.GenerateContentConfig(
                system_instruction=(
                    "Jesteś ekspertem finansowym. Używaj narzędzi żeby pobrać twarde dane: "
                    "execute_sql_query (baza danych), search_documents (dokumenty firmy), "
                    "get_document_content (pełna treść dokumentu). NIE ZGADUJ — odpowiadaj WYŁĄCZNIE "
                    "na podstawie wyników z narzędzi lub danych bazowych. Jeśli narzędzie zwróci błąd, poinformuj. "
                    "SQL: SUM(net_amount) zamiast gross_amount (chyba że user wprost prosi o brutto). "
                    "Filtr is_paid TYLKO gdy user pyta o cashflow zrealizowany/gotówkowy — "
                    "dla ogólnego cashflow/trendu NIE dodawaj is_paid."
                    + db_context + analysis_shared
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

            # Retry raz jeśli response.text pusty
            if not response.text or len(response.text.strip()) < 5:
                logger.warning("[RETRY] analysis: empty response, retrying once...")
                chat2 = client.aio.chats.create(
                    model="gemini-2.5-flash",
                    config=types.GenerateContentConfig(
                        system_instruction=(
                            "Jesteś ekspertem finansowym. Używaj narzędzi żeby pobrać twarde dane: "
                            "execute_sql_query (baza danych), search_documents (dokumenty firmy), "
                            "get_document_content (pełna treść dokumentu). NIE ZGADUJ."
                            + db_context
                        ),
                        temperature=0.1, tools=all_tools,
                        automatic_function_calling=AFC_CONFIG,
                    )
                )
                response, retry_events = await _run_with_progress(
                    chat2.send_message(user_prompt), "analysis"
                )
                for evt in retry_events:
                    yield evt
                retry_calls = _extract_tool_calls_from_response(response)
                all_tool_calls.extend(retry_calls)

            response_text = response.text or "(Agent nie zwrócił tekstu — ponów pytanie)"
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
                    f"SUM(net_amount) as total_net, COUNT(*) as cnt "
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

                # Retry raz jeśli response.text pusty
                resp_text = response.text
                if not resp_text or len(resp_text.strip()) < 5:
                    logger.warning(f"[RETRY] Agent '{name}': empty response, retrying once...")
                    chat2 = client.aio.chats.create(
                        model="gemini-2.5-flash",
                        config=types.GenerateContentConfig(
                            system_instruction=f"[TRYB: {triage.mode}]\n" + sys_prompt + db_context + shared_data,
                            temperature=0.4, tools=all_tools,
                            automatic_function_calling=AFC_CONFIG,
                        )
                    )
                    response = await chat2.send_message(user_prompt)
                    retry_calls = _extract_tool_calls_from_response(response)
                    for c in retry_calls:
                        c["agent"] = name
                    all_tool_calls.extend(retry_calls)
                    resp_text = response.text or f"(Agent {name} nie zwrócił tekstu po retry)"

                await progress_queue.put({"type": "agent_done", "phase": f"agent_{name.lower()}", "agent": name, "detail": f"{len(calls)} narzędzi"})
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
        f"[DZISIEJSZA DATA: {_today}]\n"
        f"[TRYB: {triage.mode}]\n"
        f"[QUESTION_TYPE: {triage.question_type}]\n"
        f"[OUTPUT_COMPLEXITY: {triage.output_complexity}]\n"
        f"Zapytanie CEO: {user_prompt}\n\n"
        f"Materiały zebrane przez zespół / sub-agentów:\n{debates_str}\n\n"
        f"Log wywołań narzędzi (ma pole 'result' z faktycznym outputem SQL — TO JEST ŹRÓDŁO PRAWDY dla liczb):\n{tools_summary}\n\n"
        f"{cross_validation_note}"
        f"Zsyntetyzuj finałowy raport. Dobierz bloki pasujące do QUESTION_TYPE i OUTPUT_COMPLEXITY.\n"
        f"Każda liczba w chart.data/metric_grid/data_table — kopiuj z 'result' powyżej (faktyczny output SQL).\n"
        f"Gdy konkretnej liczby brakuje w 'result' — wpisz '—' zamiast wymyślać. NIE estymuj na oko.\n"
        f"Używaj net_amount (netto) chyba że user wprost prosi o brutto.\n"
        f"layout[] MUSI zawierać co najmniej jeden blok. Pusty layout = błąd."
    )

    # Retry logic — Gemini czasem zwraca pusty response lub niedokończony function_call
    # Flash-only na dev/test — Pro włączamy dopiero na produkcji z płacącymi klientami
    # Pro jest 10-20x droższy. Zmień na ["gemini-3.1-pro-preview", "gemini-2.5-flash"] gdy gotowe.
    SYNTHESIS_MODELS = ["gemini-2.5-flash"]
    last_error = None

    for model_name in SYNTHESIS_MODELS:
        try:
            logger.info(f"[SYNTHESIS] Trying model: {model_name}")
            logger.info(
                f"[SYNTHESIS_REQ] model={model_name} route={triage.route} "
                f"prompt_len={len(final_prompt)} sys_len={len(MAJOR_CFO_PROMPT)} "
                f"opinions_count={len(opinions)}"
            )
            final_res = await client.aio.models.generate_content(
                model=model_name,
                contents=final_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=MAJOR_CFO_PROMPT,
                    response_mime_type="application/json",
                    response_schema=MajorCfoOutput,
                    temperature=0.2,
                    max_output_tokens=8192,
                    # Gemini 2.5-flash domyślnie włącza "thinking" które zjada tokeny z output budżetu.
                    # Dla structured JSON reasoning jest zbędny — dał truncated JSON na comparison prompts.
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                )
            )

            if not final_res.text or len(final_res.text.strip()) < 10:
                raise ValueError(f"Pusta odpowiedź Gemini ({model_name}): '{final_res.text[:50] if final_res.text else 'None'}'")

            # Track token usage
            from app.services.token_tracker import track_and_accumulate
            track_and_accumulate(final_res, model_name, f"cfo_synthesis_{triage.route}")

            # Gemini Flash czasem wpada w infinite \n loop na polskich znakach (np. "wysokości"→"wysoknoci").
                        # Kolapsuj długie sekwencje \n ZANIM sparsujemy — ratuje ~5% przypadków.
            raw_text = final_res.text
            import re as _re
            sanitized = _re.sub(r'(\\n){8,}', r'\\n', raw_text)
            if sanitized != raw_text:
                logger.warning(f"[SYNTHESIS_NEWLINE_LOOP] Skolapsowano powtórzenia \\\\n (len {len(raw_text)} → {len(sanitized)})")
                raw_text = sanitized

            try:
                from app.services._json_parse import safe_model_validate_json
                final_data = safe_model_validate_json(MajorCfoOutput, raw_text)
            except Exception as val_err:
                # Nie chowaj błędu w generic except. Pokaż dokładnie co padło.
                stripped = final_res.text.rstrip()
                truncated_suspect = bool(stripped) and not stripped.endswith('}') and not stripped.endswith(']')
                usage = getattr(final_res, 'usage_metadata', None)
                logger.error(
                    f"[SYNTHESIS_VALIDATION_FAIL] model={model_name} "
                    f"error_type={type(val_err).__name__} "
                    f"error_msg={str(val_err)[:500]} "
                    f"raw_text_len={len(final_res.text)} "
                    f"raw_text_preview={final_res.text[:800]!r} "
                    f"raw_text_tail={final_res.text[-400:]!r}"
                )
                if truncated_suspect:
                    logger.error(f"[SYNTHESIS_TRUNCATION_SUSPECTED] raw_text nie kończy się na '}}' ani ']' — prawdopodobne obcięcie output JSON.")
                if usage:
                    logger.error(
                        f"[SYNTHESIS_TOKEN_USAGE] prompt_tokens={getattr(usage, 'prompt_token_count', '?')} "
                        f"output_tokens={getattr(usage, 'candidates_token_count', '?')} "
                        f"total={getattr(usage, 'total_token_count', '?')}"
                    )
                raise

            # ── POST-PROCESSING: deduplikacja bloków layout ──
            # Charty są wyjątkiem — wiele chartów OK jeśli różny chart_type
            resp = final_data.response
            if hasattr(resp, 'layout') and resp.layout:
                seen_types = set()
                seen_chart_types = set()
                deduped = []
                for block in resp.layout:
                    bt = block.block_type if hasattr(block, 'block_type') else (block.get('block_type') if isinstance(block, dict) else None)
                    if bt == "chart":
                        ct = getattr(block, 'chart_type', None) or (block.get('chart_type') if isinstance(block, dict) else None)
                        dedup_key = f"chart:{ct}"
                        if dedup_key in seen_chart_types:
                            logger.warning(f"[DEDUP] Usunięto zduplikowany chart: {ct}")
                            continue
                        seen_chart_types.add(dedup_key)
                    elif bt and bt in seen_types:
                        logger.warning(f"[DEDUP] Usunięto zduplikowany blok: {bt}")
                        continue
                    if bt and bt != "chart":
                        seen_types.add(bt)
                    deduped.append(block)
                resp.layout = deduped

            # ── POST-PROCESSING: wytnij puste punkty z chart.data (np. Listopad=0, Grudzień=0) ──
            def _pt_val(pt, field):
                if isinstance(pt, dict): return pt.get(field)
                return getattr(pt, field, None)
            def _pt_set(pt, field, v):
                if isinstance(pt, dict): pt[field] = v
                else: setattr(pt, field, v)

            if hasattr(resp, 'layout') and resp.layout:
                for b in resp.layout:
                    bt = getattr(b, 'block_type', None) or (b.get('block_type') if isinstance(b, dict) else None)
                    if bt != "chart":
                        continue
                    data_pts = getattr(b, 'data', None) or (b.get('data') if isinstance(b, dict) else None) or []
                    pruned = []
                    dropped = 0
                    for pt in data_pts:
                        v1, v2 = _pt_val(pt, 'value'), _pt_val(pt, 'value2')
                        empty = (v1 in (None, 0, 0.0)) and (v2 in (None, 0, 0.0))
                        if empty:
                            dropped += 1
                            continue
                        pruned.append(pt)
                    if dropped:
                        logger.info(f"[CHART_PRUNE_EMPTY] Wyrzucono {dropped} punktów zerowych z chart '{getattr(b,'title',None) or ''}'")
                        if isinstance(b, dict): b['data'] = pruned
                        else: b.data = pruned

            # ── POST-PROCESSING: wywal data_table zdublowany z chartem (te same miesiące) ──
            if hasattr(resp, 'layout') and resp.layout:
                chart_label_sets: list[set] = []
                for b in resp.layout:
                    bt = getattr(b, 'block_type', None) or (b.get('block_type') if isinstance(b, dict) else None)
                    if bt != "chart":
                        continue
                    pts = getattr(b, 'data', None) or (b.get('data') if isinstance(b, dict) else None) or []
                    labels = {str(_pt_val(p, 'label')).strip().lower() for p in pts if _pt_val(p, 'label')}
                    if labels:
                        chart_label_sets.append(labels)

                kept = []
                for b in resp.layout:
                    bt = getattr(b, 'block_type', None) or (b.get('block_type') if isinstance(b, dict) else None)
                    if bt == "data_table" and chart_label_sets:
                        rows = getattr(b, 'rows', None) or (b.get('rows') if isinstance(b, dict) else None) or []
                        first_col = set()
                        for r in rows:
                            cells = getattr(r, 'cells', None) or (r.get('cells') if isinstance(r, dict) else r) or []
                            if cells:
                                first_col.add(str(cells[0]).strip().lower())
                        # Jeśli ≥70% wierszy tabeli pokrywa się z labelami któregoś chartu → redundant
                        if first_col:
                            for chart_labels in chart_label_sets:
                                overlap = len(first_col & chart_labels) / len(first_col)
                                if overlap >= 0.7:
                                    logger.info(f"[DATA_TABLE_DEDUP] Usunięto data_table redundantny z chart (overlap {overlap:.0%})")
                                    break
                            else:
                                kept.append(b)
                                continue
                            continue
                    kept.append(b)
                resp.layout = kept

            # ── POST-PROCESSING: filter out invalid/empty charts + conditional auto-chart ──
            # Usunięto wymuszanie chartów w każdym raporcie. Chart generowany tylko gdy
            # sub-agenci dostarczyli ≥3 punktów danych scenariuszowych. 0-point chart = halucynacja.
            if hasattr(resp, 'layout') and resp.layout:
                # 1. Usuń chart bloki z pustym lub za małym data[]
                filtered = []
                for b in resp.layout:
                    bt = getattr(b, 'block_type', None) or (b.get('block_type') if isinstance(b, dict) else None)
                    if bt == "chart":
                        data_pts = getattr(b, 'data', None) or (b.get('data') if isinstance(b, dict) else None) or []
                        ctype = getattr(b, 'chart_type', None) or (b.get('chart_type') if isinstance(b, dict) else None)
                        min_pts = 2 if ctype in ("bar_horizontal", "bar_vertical", "donut", "pie", "waterfall") else 3
                        if len(data_pts) < min_pts:
                            logger.warning(f"[CHART_FILTER] Usunięto chart {ctype} z {len(data_pts)} punktów (min {min_pts})")
                            continue
                    filtered.append(b)
                resp.layout = filtered

                has_chart = any(
                    (getattr(b, 'block_type', None) or (b.get('block_type') if isinstance(b, dict) else None)) == "chart"
                    for b in resp.layout
                )
                # 2. Conditional auto-chart: TYLKO gdy sub-agenci dali realne scenariusze
                #    z ≥3 punktami, i to tylko przy strategic_debate. W innych przypadkach
                #    brak chartu jest ŚWIADOMY — user_prompt/dane tego nie wymagały.
                if not has_chart and triage.route == "strategic_debate":
                    try:
                        from app.schemas import UIBlock, ChartDataPoint
                        scenarios = getattr(resp, 'scenarios_analysis', None) or []
                        if scenarios and len(scenarios) >= 2:
                            # Build prediction_band from first 2 scenarios (pessimist=lower, optimist=upper)
                            sc_a = scenarios[0]
                            sc_b = scenarios[1]
                            cd_a = getattr(sc_a, 'chart_data', None) or (sc_a.get('chart_data') if isinstance(sc_a, dict) else []) or []
                            cd_b = getattr(sc_b, 'chart_data', None) or (sc_b.get('chart_data') if isinstance(sc_b, dict) else []) or []

                            # Min 3 punktów — dla prediction_band z sensem (min_pts z filtra wyżej)
                            if len(cd_a) >= 3:
                                chart_points = []
                                for i, pt_a in enumerate(cd_a):
                                    month = pt_a.get('month', f'M{i+1}') if isinstance(pt_a, dict) else getattr(pt_a, 'month', f'M{i+1}')
                                    val_a = pt_a.get('simulated_cashflow', pt_a.get('baseline_cashflow', 0)) if isinstance(pt_a, dict) else 0
                                    val_b = 0
                                    if i < len(cd_b):
                                        pt_b = cd_b[i]
                                        val_b = pt_b.get('simulated_cashflow', pt_b.get('baseline_cashflow', 0)) if isinstance(pt_b, dict) else 0
                                    lo = min(val_a, val_b) if val_b else val_a * 0.9
                                    hi = max(val_a, val_b) if val_b else val_a * 1.1
                                    chart_points.append(ChartDataPoint(label=str(month), value=lo, value2=hi))

                                auto_chart = UIBlock(
                                    block_type="chart",
                                    chart_type="prediction_band",
                                    title="Prognoza cashflow — scenariusze",
                                    data=chart_points,
                                    series_labels=["Konserwatywny", "Optymistyczny"],
                                )
                                # Insert at position 1 (after metric_grid, before data_table)
                                insert_pos = min(2, len(resp.layout))
                                resp.layout.insert(insert_pos, auto_chart)
                                logger.info(f"[AUTO_CHART] Generated prediction_band from scenarios ({len(chart_points)} points)")
                        elif not scenarios:
                            # No scenarios — try to build from data_table time series
                            for b in resp.layout:
                                bt = getattr(b, 'block_type', None) or (b.get('block_type') if isinstance(b, dict) else None)
                                if bt != "data_table":
                                    continue
                                rows_raw = getattr(b, 'rows', None) or (b.get('rows') if isinstance(b, dict) else None)
                                cols = getattr(b, 'columns', None) or (b.get('columns') if isinstance(b, dict) else None)
                                if not rows_raw or not cols or len(rows_raw) < 2:
                                    continue
                                chart_points = []
                                months_kw = ["sty", "lut", "mar", "kwi", "maj", "cze", "lip", "sie", "wrz", "paź", "lis", "gru"]
                                for row in rows_raw:
                                    cells = getattr(row, 'cells', None) or (row.get('cells') if isinstance(row, dict) else row)
                                    if not cells or len(cells) < 2:
                                        continue
                                    label = str(cells[0]).lower()
                                    if not any(m in label for m in months_kw + ["q1","q2","q3","q4","2025","2026","2027"]):
                                        continue
                                    val_str = str(cells[1]).replace(" ", "").replace("PLN", "").replace(",", ".").replace("+", "")
                                    try:
                                        val = float(val_str)
                                    except (ValueError, TypeError):
                                        continue
                                    v2 = None
                                    if len(cells) > 2:
                                        v2_str = str(cells[2]).replace(" ", "").replace("PLN", "").replace(",", ".").replace("+", "")
                                        try: v2 = float(v2_str)
                                        except: pass
                                    chart_points.append(ChartDataPoint(label=str(cells[0]), value=val, value2=v2))
                                # Min 3 punktów czasowych — 2 to za mało na linię trendu
                                if len(chart_points) >= 3:
                                    ct = "line" if not chart_points[0].value2 else "prediction_band"
                                    sl = [cols[1]] if not chart_points[0].value2 else [cols[1], cols[2]] if len(cols) > 2 else ["Wartość"]
                                    auto_chart = UIBlock(
                                        block_type="chart", chart_type=ct,
                                        title=getattr(b, 'title', None) or (b.get('title') if isinstance(b, dict) else ""),
                                        data=chart_points, series_labels=sl,
                                    )
                                    idx = resp.layout.index(b)
                                    resp.layout.insert(idx, auto_chart)
                                    logger.info(f"[AUTO_CHART] Generated {ct} from data_table ({len(chart_points)} points)")
                                    break
                    except Exception as chart_err:
                        logger.warning(f"[AUTO_CHART] Failed: {chart_err}")

            # ── AUTO-CHART DLA RAPORTOWANIA: uzupełnij brakujące charty z prefetch SQL ──
            # Gdy category='raportowanie' i synthesis dał <2 charty a prefetch ma dane —
            # dobudowujemy line chart (cashflow) i/lub bar_horizontal (top kontrahenci).
            if triage.category == "raportowanie" and hasattr(resp, 'layout') and resp.layout:
                _PL_MSC = {'01':'Styczeń','02':'Luty','03':'Marzec','04':'Kwiecień','05':'Maj',
                           '06':'Czerwiec','07':'Lipiec','08':'Sierpień','09':'Wrzesień',
                           '10':'Październik','11':'Listopad','12':'Grudzień'}

                def _layout_get(b, field):
                    return getattr(b, field, None) or (b.get(field) if isinstance(b, dict) else None)

                def _parse_tool_rows(query_hint: str):
                    for tc in all_tool_calls:
                        q = (tc.get('args') or {}).get('query', '')
                        if query_hint not in q:
                            continue
                        res = tc.get('result')
                        if res is None:
                            continue
                        try:
                            if isinstance(res, str):
                                res = json.loads(res)
                            return res.get('rows', []) if isinstance(res, dict) else []
                        except Exception:
                            return []
                    return []

                has_trend_chart = any(
                    _layout_get(b, 'block_type') == 'chart' and
                    _layout_get(b, 'chart_type') in ('line', 'area', 'prediction_band', 'combo', 'stacked_bar')
                    for b in resp.layout
                )
                has_ranking_chart = any(
                    _layout_get(b, 'block_type') == 'chart' and
                    _layout_get(b, 'chart_type') in ('bar_horizontal', 'bar_vertical')
                    for b in resp.layout
                )

                try:
                    from app.schemas import UIBlock, ChartDataPoint

                    # 1. Line chart: Przychody vs Koszty per msc
                    if not has_trend_chart:
                        rows = _parse_tool_rows('przychody_net')
                        pts = []
                        for row in rows:
                            msc_str = str(row.get('msc', ''))
                            parts = msc_str.split('-')
                            if len(parts) >= 2:
                                year, mm = parts[0], parts[1][:2]
                                label = f"{_PL_MSC.get(mm, mm)} {year}"
                            else:
                                label = msc_str
                            przych = float(row.get('przychody_net') or 0)
                            koszty = float(row.get('koszty_net') or 0)
                            if przych == 0 and koszty == 0:
                                continue
                            pts.append(ChartDataPoint(label=label, value=przych, value2=koszty))
                        if len(pts) >= 3:
                            auto_trend = UIBlock(
                                block_type="chart", chart_type="line",
                                title="Cashflow miesięczny — Przychody vs Koszty (NET)",
                                data=pts, series_labels=["Przychody", "Koszty"],
                            )
                            insert_pos = 0
                            for idx, b in enumerate(resp.layout):
                                if _layout_get(b, 'block_type') == 'metric_grid':
                                    insert_pos = idx + 1
                                    break
                            resp.layout.insert(insert_pos, auto_trend)
                            logger.info(f"[AUTO_CHART_REPORT] Wstawiono line chart ({len(pts)} msc)")

                    # 2. Bar_horizontal: top kontrahenci — tylko gdy brak rankingu
                    if not has_ranking_chart:
                        rows = _parse_tool_rows('contractor_name')
                        rows_sorted = [r for r in rows if r.get('contractor_name')]
                        # Bierzemy po 6 najlepszych income i 6 wydatków (żeby chart miał sens)
                        inc = [r for r in rows_sorted if r.get('invoice_type') == 'income'][:6]
                        exp = [r for r in rows_sorted if r.get('invoice_type') == 'expense'][:6]
                        bucket = inc if len(inc) >= 3 else exp
                        kind = "Przychody" if bucket is inc else "Wydatki"
                        if len(bucket) >= 3:
                            pts = [ChartDataPoint(
                                label=str(r.get('contractor_name'))[:35],
                                value=float(r.get('obrot_net') or 0),
                            ) for r in bucket]
                            auto_rank = UIBlock(
                                block_type="chart", chart_type="bar_horizontal",
                                title=f"Top kontrahenci — {kind} (NET)",
                                data=pts, series_labels=[kind],
                            )
                            resp.layout.append(auto_rank)
                            logger.info(f"[AUTO_CHART_REPORT] Wstawiono bar_horizontal ({len(pts)} kontrahentów, {kind})")
                except Exception as e:
                    logger.warning(f"[AUTO_CHART_REPORT] Failed: {e}")

            # ── SAFETY NET: pusty layout = zbuduj minimalny fallback z hero/exec_summary ──
            # Zapobiega "white screen" gdy Gemini zwróci DashboardResponse bez layoutu
            # (np. dla nieobsłużonych kształtów pytań jak "1 vs 3 osoby").
            if hasattr(resp, 'layout') and not resp.layout:
                try:
                    from app.schemas import UIBlock
                    hero = getattr(resp, 'hero_recommendation', None) or ""
                    exec_sum = getattr(resp, 'executive_summary', None) or "Brak syntezy."
                    fallback_body = (hero + ("\n\n" + exec_sum if exec_sum and exec_sum != hero else "")) or "Nie udało się zsyntetyzować odpowiedzi."
                    resp.layout = [UIBlock(
                        block_type="text_card",
                        title="Podsumowanie",
                        body=fallback_body,
                    )]
                    logger.warning(f"[LAYOUT_FALLBACK] Pusty layout z Gemini — wygenerowano text_card z hero/executive_summary")
                except Exception as fallback_err:
                    logger.error(f"[LAYOUT_FALLBACK] Failed: {fallback_err}")

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
                logger.error(
                    f"[SYNTHESIS_ERROR_FINAL] last_error_type={type(last_error).__name__} "
                    f"last_error_msg={str(last_error)[:500]}"
                )
                yield json.dumps({"status": "error", "message": _sanitize_error(e)})
                return  # Explicit end — nie kontynuuj po błędzie obu modeli
