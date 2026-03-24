# CFO Dyrektor Finansowy — Schemat Zarządzania Kontekstem i Pobierania Danych

## Spis treści
1. [Ogólny przegląd architektury](#1-ogólny-przegląd-architektury)
2. [Dwa rodzaje wiedzy systemu](#2-dwa-rodzaje-wiedzy-systemu)
3. [Ścieżka A: Zasilanie wiedzy przez dokumenty](#3-ścieżka-a-zasilanie-wiedzy-przez-dokumenty)
4. [Ścieżka B: Implicit Learning z promptów czatu](#4-ścieżka-b-implicit-learning-z-promptów-czatu)
5. [Ścieżka C: Pasywne API (webhooks)](#5-ścieżka-c-pasywne-api-webhooks)
6. [Schemat CompanyContext (Pamięć Korporacyjna)](#6-schemat-companycontext-pamięć-korporacyjna)
7. [Pełny przepływ zapytania CFO — krok po kroku](#7-pełny-przepływ-zapytania-cfo--krok-po-kroku)
8. [Intelligent Dispatcher — dobór agentów](#8-intelligent-dispatcher--dobór-agentów)
9. [Sub-agenci — role i tryby pracy](#9-sub-agenci--role-i-tryby-pracy)
10. [Major CFO — synteza i Generative UI](#10-major-cfo--synteza-i-generative-ui)
11. [Zapis raportu i zwrot do frontendu](#11-zapis-raportu-i-zwrot-do-frontendu)
12. [Mapa plików i odpowiedzialności](#12-mapa-plików-i-odpowiedzialności)
13. [Diagram przepływu danych](#13-diagram-przepływu-danych)

---

## 1. Ogólny przegląd architektury

System CFO działa jako **Mixture of Experts (MoE)** — kilka wyspecjalizowanych agentów AI analizuje to samo zapytanie z różnych perspektyw, a następnie "Major CFO" syntetyzuje ich wyniki w jeden spójny raport z dynamicznym interfejsem UI.

Jakość odpowiedzi AI jest wprost proporcjonalna do jakości i kompletności danych w `company_contexts`. Im więcej twardych faktów (gotówka, MRR, forma prawna, flagi ryzyka), tym precyzyjniejsza analiza.

```
Wiedza o firmie (dwa źródła)
    ├── company_contexts   (JSONB) ← "Pamięć Korporacyjna" — ustrukturyzowane fakty
    └── company_documents  (JSONB) ← "Archiwum Dokumentów" — compressed AI summaries
         ↓                              ↓
         └─────────── połączone podczas zapytania CFO ──────────────────┐
                                                                        ↓
Zapytanie Prezesa → [Text-to-SQL] → [Dispatcher] → [Sub-agenci równolegle] → [Major CFO] → Raport
```

---

## 2. Dwa rodzaje wiedzy systemu

### 2.1 Pamięć Korporacyjna (`company_contexts`)

**Plik:** `auditra/app/services/context_manager.py`
**Model DB:** `app/models.py → CompanyContextModel`
**Tabela:** `company_contexts`

Jest to żywy, ewoluujący obiekt JSON przechowywany w PostgreSQL. Zawiera **ustrukturyzowane fakty** o firmie wyekstrahowane z dokumentów i promptów czatu. Nigdy nie jest kasowany — tylko aktualizowany (upsert).

Pola (schemat Pydantic `CompanyContext`):
- `strategic_compass` — cel firmy, strategia (np. "Runda A Q2 2027")
- `company_profile` — forma prawna, opodatkowanie, branża, model biznesowy
- `live_financial_pulse` — gotówka, linia kredytowa, MRR, Net Burn Rate
- `unit_economics` — LTV, CAC, Gross Margin, Churn
- `seasonality_and_patterns` — lista zaobserwowanych wzorców (np. "Q4 = 40% przychodów")
- `active_risk_flags` — flagi ryzyka z poziomem severity: `high/medium/low`

### 2.2 Archiwum Dokumentów (`company_documents`)

**Plik:** `auditra/app/services/document_indexer.py`
**Model DB:** `app/models.py → CompanyDocument`
**Tabela:** `company_documents`

Każdy przesłany plik jest **indeksowany** przez Małego Agenta Indeksującego (gemini-2.5-flash). Wynikiem jest skompresowane `ai_summary` (JSONB) z polami:
- `typ_dokumentu` — co to jest (faktura, umowa, wyciąg bankowy...)
- `wartosc_finansowa` — główna kwota/zakres kwot
- `krytyczne_terminy` — daty, deadliny, terminy płatności
- `anomalie_lub_ryzyka` — niepokojące klauzule, ukryte opłaty
- `ai_reasoning` — Chain of Thought agenta (dlaczego zwrócił uwagę na konkretne ryzyka)

---

## 3. Ścieżka A: Zasilanie wiedzy przez dokumenty

Plik przesyłany przez użytkownika przechodzi przez **dwa równoległe procesy w tle** (`BackgroundTasks`):

```
[POST /api/documents/upload]
    │
    ├─► Krok 1: WebDAV — zapis surowego pliku na Hetzner Storage Box
    │
    ├─► BackgroundTask #1: document_indexer.process_and_save_document()
    │       ├── Ekstrakcja tekstu z pliku (PDF→PyMuPDF, CSV→pandas, XML→lxml, TXT→direct)
    │       ├── Gemini 2.5-flash + DOCUMENT_INDEXER_PROMPT (Structured Outputs)
    │       │   temperature=0.2 (precyzja faktów, nie kreatywność)
    │       └── Zapis do tabeli company_documents (JSONB ai_summary)
    │
    └─► BackgroundTask #2: context_manager.process_and_upsert_context()
            ├── Pobranie surowego tekstu pliku (max 30 000 znaków)
            ├── Gemini 3.1-pro-preview + CONTEXT_EXTRACTOR_PROMPT
            │   temperature=0.0 (zero halucynacji — tylko fakty)
            ├── Zwraca CompanyContext Pydantic object
            └── merge_company_context(stary, nowy) → upsert do company_contexts
```

**Plik źródłowy:** `auditra/app/api/documents.py` (linie ~77-106)

### Logika merge (smart upsert)

Funkcja `merge_company_context()` stosuje zasadę "Małego Agenta":
- **Pola skalarne** (`cash_on_hand`, `legal_form`, itp.): nowa wartość nadpisuje starą, TYLKO jeśli nowa nie jest `null`
- **Listy wzorców** (`seasonality_and_patterns`): nowe wpisy są **dopisywane** (deduplikacja case-insensitive), nigdy nie kasowane
- **Flagi ryzyka** (`active_risk_flags`): nowe alerty trafiają na **górę kolejki** (najświeższe na top), duplikaty odfiltrowane

---

## 4. Ścieżka B: Implicit Learning z promptów czatu

Każdy prompt wpisany przez Prezesa w interfejsie CFO jest analizowany w tle jako potencjalne źródło faktów o firmie.

```
[POST /api/cfo/simulate]
    │
    └─► BackgroundTask: background_chat_upsert()
            └── process_and_upsert_context(db, company_uuid, request.prompt)
                    ├── Gemini 3.1-pro wyciąga fakty z prompta
                    └── Merge + upsert do company_contexts
```

Przykład: Prezes pisze *"Mamy 840k gotówki i planujemy zatrudnić 3 handlowców"* → system automatycznie aktualizuje `live_financial_pulse.cash_on_hand = 840000`.

**Plik źródłowy:** `auditra/app/api/routers/cfo.py` (linie 34-42)

---

## 5. Ścieżka C: Pasywne API (webhooks)

**Funkcja:** `sync_passive_metrics()` w `context_manager.py`

Placeholder przeznaczony na odbieranie danych z webhooków zewnętrznych systemów (Stripe, Paddle, Fakturownia). Pomija LLM — bezpośredni zapis twardych liczb.

```python
payload = {"mrr": 182000.0, "cash_on_hand": 450000.0}
# → direct upsert do live_financial_pulse, bez LLM call
```

**Status:** Zaimplementowany, brak aktywnych integracji (roadmap: KSeF + ERP adapters).

---

## 6. Schemat CompanyContext (Pamięć Korporacyjna)

```python
class CompanyContext(BaseModel):
    strategic_compass: StrategicCompass
    #   primary_goal: str        # "Runda A Q2 2027"
    #   strategy: str            # "Growth over Profit"

    company_profile: CompanyProfile
    #   legal_form: str          # "Sp. z o.o."
    #   taxation_form: str       # "CIT estoński"
    #   industry: str            # "SaaS B2B"
    #   business_model: str      # "Subskrypcje roczne"

    live_financial_pulse: LiveFinancialPulse
    #   cash_on_hand: float      # 840000.0 PLN
    #   credit_line: float       # 500000.0 PLN
    #   mrr: float               # 145000.0 PLN
    #   net_burn_rate: float     # 35000.0 PLN/msc

    unit_economics: UnitEconomics
    #   ltv: float               # 38000.0 PLN
    #   cac: float               # 4200.0 PLN
    #   gross_margin_pct: float  # 74.0 %
    #   churn_pct: float         # 2.3 %

    seasonality_and_patterns: List[str]
    # ["Q4 odpowiada za 40% przychodów", "Sierpień: -15% aktywność klientów B2B"]

    active_risk_flags: List[RiskFlag]
    # [{"description": "Wysoka koncentracja przychodów — 1 klient to 60% MRR", "severity": "high"}]
```

---

## 7. Pełny przepływ zapytania CFO — krok po kroku

Wejście: `POST /api/cfo/simulate` z `{"prompt": "..."}`

### KROK 1 — Text-to-SQL (pobieranie danych z bazy faktur)

**Plik:** `cfo_agent.py → run_cfo_simulation_stream()`

```
Prompt Prezesa
    ↓
generate_sql(prompt, DB_SCHEMA, company_id)
    ↓ [Gemini 2.5-flash, temperature=0.0]
    SQL query (zawsze filtrowany po company_id)
    ↓
execute_sql(sql_query)
    ↓ [asyncpg → PostgreSQL/Supabase]
    db_results: list[Row]
    ↓
generate_baseline_projection(db_results) → baseline: list[dict]
    ↓
Ekstrakcja CfoDataSource (max 50 rekordów, deduplikacja po sygnaturze label|kwota|data)
```

**Schemat bazy (DB_SCHEMA):** tabele `companies`, `departments`, `invoices` z pełną dokumentacją kolumn i zasadami SQL (brak kolumny `risk_score` bezpośrednio — tylko przez JSONB, `due_date` tylko przez `parsed_data`).

Fallback: jeśli SQL zawiedzie → `baseline = [{"Info": "Brak danych..."}]`, `data_sources = []`

### KROK 1.5 — Pobieranie wiedzy kontekstowej (równolegle)

```python
corporate_memory_str, doc_summaries_str = await asyncio.gather(
    fetch_corporate_memory(company_id),    # ← company_contexts tabela
    fetch_document_summaries(company_id),  # ← company_documents tabela (max 10)
)
```

**`fetch_corporate_memory()`** — pobiera `CompanyContext` z bazy, formatuje jako tekst:
```
== GLOBALNA PAMIĘĆ KORPORACYJNA (TWARDE FAKTY) ==
[Kompas Strategiczny]
Cel: Runda A Q2 2027
...
[Puls Finansowy]
Gotówka: 840000.0 PLN
...
[Flagi Ryzyka]
- [HIGH] Wysoka koncentracja przychodów...
```

**`fetch_document_summaries()`** — pobiera max 10 najnowszych rekordów z `company_documents`, formatuje jako tekst:
```
[Dokument #1: umowa_techcorp.pdf]
  Typ: Umowa handlowa
  Wartość: 245,000 PLN netto
  Terminy: 30 dni od wystawienia, kontrakt do 31.12.2026
  Ryzyka: Kara umowna 50k PLN za opóźnienie >14 dni
  Reasoning: Kara stanowi 33% wartości kontraktu — ryzyko cashflow
```

Oba stringi są **łączone** w `doc_context_str` i wstrzykiwane do każdego Sub-agenta.

### KROK 1.7 — Intelligent Dispatcher

Patrz [Sekcja 8](#8-intelligent-dispatcher--dobór-agentów).

### KROK 2 — Sub-agenci (równoległe wywołania)

Patrz [Sekcja 9](#9-sub-agenci--role-i-tryby-pracy).

### KROK 3 — Major CFO syntezuje

Patrz [Sekcja 10](#10-major-cfo--synteza-i-generative-ui).

---

## 8. Intelligent Dispatcher — dobór agentów

**Funkcja:** `select_best_agents()` w `cfo_agent.py`
**Model:** Gemini 2.5-flash, `temperature=0.0` (deterministyczna klasyfikacja)
**Output schema:** `ActiveAgents { selected_agents: list[AgentKey] }`

Dyspozytor klasyfikuje prompt i wybiera 1-3 agentów:

| Klucz | Agent | Kiedy wybierany |
|---|---|---|
| `VP_GROWTH` | VP of Growth (Agresywna) | Inwestycje, skalowanie, marketing, wzrost przychodów |
| `RISK_OFFICER` | Chief Risk Officer (Płynność i Ryzyko) | Zatory, kryzysy cashflow, dylematy strategiczne |
| `ACTUARY` | Główny Aktuariusz (Status Quo) | Symulacje, projekcje cashflow, break-even, runway |
| `TAX_STRATEGIST` | Strateg Podatkowy | Optymalizacja VAT/CIT, efektywność faktur |
| `INFO_RETRIEVER` | Tryb Informacyjny | Proste pytania o suche fakty ("ile mamy gotówki?") |

**Fallback:** Jeśli Dispatcher zawiedzie → domyślnie `["VP_GROWTH", "RISK_OFFICER", "ACTUARY"]`

**Tryb INFO_RETRIEVER:** Zero wywołań sub-agentów — system zwraca tylko dane SQL i pamięć korporacyjną bezpośrednio do Major CFO. Brak wykresów symulacyjnych w raporcie.

---

## 9. Sub-agenci — role i tryby pracy

Każdy Sub-agent uruchamiany jest przez `generate_sub_agent_analysis()` z:
- Własnym `system_prompt` (rola i persona)
- `user_prompt` (zapytanie Prezesa)
- `baseline_str` (dane historyczne z SQL jako JSON)
- `doc_context_str` (Pamięć Korporacyjna + Summaries dokumentów)

Model: `gemini-3.1-pro-preview`, `temperature=0.4`
Output schema: `CfoScenarioAnalysis`

### Prompt Sub-agenta (struktura kontekstu wejściowego)

```
[System Prompt: rola agenta]
─────────────────────────────────
== ZAPYTANIE PREZESA ==
{user_prompt}

== KONTEKST HISTORYCZNY (SQL) ==
{baseline_str}

== KONTEKST DOKUMENTOWY (Skompresowane AI summaries) ==
{doc_context_str}    ← TYLKO jeśli istnieją dokumenty/pamięć

Zadanie: Opierając się WYŁĄCZNIE na swoich priorytetach...
```

### Sub-agent: VP of Growth (`AGENT_OPTIMIST_PROMPT`)

**Persona:** Analityk Wall Street, szuka ukrytych marż i dźwigni wzrostu.

**Tryb 1 — Inwestycja:** Rysuje "Krzywą J" — przepalenie w miesiącach 1-2, eksplozja konwersji od miesiąca 3. CAC -40%, sprzedaż upfrontowa, DSO = optymalne.

**Tryb 2 — Diagnostyka:** Zero nowych wydatków. Szuka "darmowego wzrostu": najbardziej rentowny departament, up-sell do obecnych klientów, renegocjacja warunków płatności (50% z góry).

### Sub-agent: Chief Risk Officer (`AGENT_RISK_OFFICER_PROMPT`)

**Persona:** Ekspert od płynności i zarządzania ryzykiem.

Szuka najlepszego scenariusza zdejmującego presję płynnościową. Harmonogramuje płatności, analizuje `risk_score` z `ai_analysis_json`, rozważa faktoring, skonta, odroczenia. Zawsze priorytetyzuje payroll i podatki.

### Sub-agent: Główny Aktuariusz (`AGENT_BASELINE_PROMPT`)

**Persona:** Zimny model statystyczny, "Koszt Zaniechania".

Ignoruje zamiary Prezesa. Liczy średnie ruchome z danych historycznych, ekstrapoluje liniowo na 6 miesięcy. Sezonowość = dokładna kopia wzorca z przeszłości. Zero szoków makro.

### Output Sub-agenta (`CfoScenarioAnalysis`)

```python
class CfoScenarioAnalysis(BaseModel):
    name: str                      # np. "VP of Growth (Agresywna)"
    processed_raw_context: str     # Lista WSZYSTKICH faktów wejściowych (nie wniosków)
    internal_analysis: str         # Chain of Thought — tok rozumowania agenta
    scenario_conclusion: str       # Wniosek końcowy
    health_score: int              # 0-100
    risk_level: str                # "Niskie" / "Średnio" / "Wysokie"
    impact_description: str        # 1-zdaniowy opis scenariusza
    chart_data: list[ChartPoint]   # 6 punktów miesięcznych (cashflow timeline)
```

---

## 10. Major CFO — synteza i Generative UI

**Funkcja:** `generate_major_cfo_recommendation()` w `cfo_agent.py`
**Model:** `gemini-3.1-pro-preview`, `temperature=0.4`
**Output schema:** `MajorCfoLayoutOutput`
**System prompt:** `MAJOR_CFO_SYSTEM_PROMPT` z `cfo_prompts.py`

### Dane wejściowe dla Major CFO

```
== ZAPYTANIE PREZESA ==
{user_prompt}

== HISTORYCZNE DANE BAZOWE ==
{baseline_str}

== RAPORT: VP of Growth (Agresywna) ==
{scenarioA.model_dump_json()}

== RAPORT: Chief Risk Officer (Płynność i Ryzyko) ==
{scenarioB.model_dump_json()}

== RAPORT: Główny Aktuariusz (Status Quo) ==
{scenarioC.model_dump_json()}
```

### Zadanie Major CFO

1. **Określa tryb:** Inwestycja vs. Diagnostyka
2. **Waliduje spójność:** Odrzuca skrajne halucynacje sub-agentów (np. Optymista mówi zwrot za 2 msc, matematyka mówi 5 msc → przyjmuje 5)
3. **Wyciąga "Złoty Środek"** — zbilansowaną rekomendację między skrajnościami
4. **Buduje `layout[]`** — tablicę klocków Generative UI:

### Dostępne klocki UI

| Klocek | Zastosowanie |
|---|---|
| `parsed_data_list` | Twarde fakty z SQL (dowody) |
| `alert_banner` | Jednozdaniowy alert (urgency: high/medium/low/success) |
| `text_card` | Główny tekst narracyjny (Executive Summary, markdown) |
| `metric_grid` | 2-4 kluczowe KPI do porównania |
| `scoring` | Health Score 0-100 z poziomem ryzyka |
| `chart` | Wykres (line, area, bar_vertical, bar_horizontal, waterfall, pie, donut) |
| `carousel` | Karuzela z kartami scenariuszy |
| `swot_analysis` | Macierz SWOT |
| `decision_tree` | Drzewo decyzji warunkowych |
| `payment_calendar` | Harmonogram krytycznych płatności z datami |
| `data_table` | Tabela z danymi z SQL |
| `timeline` | Plan wdrożeniowy (Playbook) — zawsze ostatni |

### Zasada "Skin in the Game"

W każdym raporcie Major CFO MUSI dodać w `text_card` (Executive Summary) pogrubiony akapit `**MOJA SZCZERA RADA:**` — jedna konkretna decyzja, jakby to były jego prywatne pieniądze. Zero korporacyjnego żargonu.

### Output (`MajorCfoLayoutOutput`)

```python
class MajorCfoLayoutOutput(BaseModel):
    internal_thought_process: str   # Chain of Thought (niewidoczny dla Prezesa)
    executive_summary: str          # Syntetyczny wstęp
    layout: list[UIBlock]           # Tablica klocków Generative UI
```

---

## 11. Zapis raportu i zwrot do frontendu

Po wygenerowaniu raportu:

```
1. Złoty Środek = avg(health_score sub-agentów)
   → dodawany jako 4. scenariusz do scenarios_analysis

2. Agregacja Chain of Thought
   → "=== VP of Growth === ...\n=== CRO === ...\n=== MAJOR CFO ==="

3. Zapis na Hetzner WebDAV (WebDAVService)
   → cfo_report_YYYYMMDD_HHMMSS.json
   → Zapis rekordu do tabeli `documents`

4. SSE yield { status: "success", report: CfoReport, sql: "...", indexed_documents: [...] }
```

### Struktura `CfoReport` (zwracana do frontendu)

```python
class CfoReport(BaseModel):
    internal_thought_process: str        # Chain of Thought (debug, ukryty w UI)
    executive_summary: str               # Synteza Majora
    hero_recommendation: str             # 1-2 zdania kluczowej rekomendacji
    scenarios_analysis: list[...]        # 3-4 scenariusze sub-agentów + Złoty Środek
    major_action_plan: CfoActionPlan     # (Oportunities, Risks, Playbook, Metrics)
    data_sources: list[CfoDataSource]    # Źródła z SQL (max 50 rekordów)
    layout: list[dict]                   # Generative UI layout (klocki jako dicts)
```

### SSE (Server-Sent Events) — statusy w czasie rzeczywistym

Frontend odbiera strumień komunikatów przed finalnym raportem:
```
"Inicjuję środowisko bazodanowe Majora..."
"Generuję zapytanie SQL do weryfikacji danych historycznych..."
"Uruchamiam silnik analityczny na bazie PostgreSQL..."
"Synchronizuję Globalną Pamięć Korporacyjną i skanuję zaindeksowane dokumenty..."
"Dyspozytor analizuje zapytanie i dobiera ekspertów..."
"Aktywni eksperci: VP of Growth, Chief Risk Officer. Uruchamiam równolegle..."
"Zakończono analizy cząstkowe. Przekazuję do Major CFO..."
"Rekomendacja Majora gotowa. Kompiluję JSON..."
"Zapisuję zrzut pamięci oraz pełny raport na Hetzner Storage..."
{ status: "success", report: {...} }
```

---

## 12. Mapa plików i odpowiedzialności

| Plik | Odpowiedzialność |
|---|---|
| `api/routers/cfo.py` | Routing HTTP, BackgroundTask Implicit Learning, SSE wrapper |
| `services/cfo_agent.py` | Główna logika: SQL, kontekst, Dispatcher, Sub-agenci, Major CFO |
| `services/context_manager.py` | CompanyContext schema + extract/merge/upsert logika (Pamięć Korporacyjna) |
| `services/document_indexer.py` | Mały Agent Indeksujący — ekstrakcja tekstu + AI summary → company_documents |
| `prompts/cfo_prompts.py` | Wszystkie system prompty: 4 sub-agenci + Major CFO + Document Indexer |
| `services/strategy_agent.py` | `generate_sql()` + `execute_sql()` — Text-to-SQL engine |
| `models.py` | SQLAlchemy models: CompanyContextModel, CompanyDocument, SharedCfoReport |
| `schemas.py` | Pydantic: CfoReport, CfoScenarioAnalysis, MajorCfoLayoutOutput, UIBlock union |

---

## 13. Diagram przepływu danych

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ZASILANIE WIEDZY (pre-query, w tle)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[Plik PDF/CSV/XML]──►[Document Indexer]──►[company_documents] (ai_summary JSONB)
                           │
                           └──►[Context Extractor]──►[company_contexts] (CompanyContext JSONB)

[Prompt czatu]──────────────►[Context Extractor]──►[company_contexts] (Implicit Learning)

[Webhook/API]───────────────────────────────────────►[company_contexts] (sync_passive_metrics)


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PRZEPŁYW ZAPYTANIA CFO (on-demand)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[Prezes: prompt]
    │
    ├─►[Text-to-SQL]──►[PostgreSQL/invoices]──►[baseline_str + data_sources]
    │
    ├─►[fetch_corporate_memory]──►[company_contexts]──►[corporate_memory_str]  ┐
    │                                                                           ├─►[combined doc_context_str]
    └─►[fetch_document_summaries]──►[company_documents]──►[doc_summaries_str]  ┘
                                                                  │
                                                    [Intelligent Dispatcher]
                                                    [Gemini 2.5-flash, temp=0.0]
                                                    → selected_keys: [VP_GROWTH, RISK_OFFICER, ACTUARY]
                                                                  │
                                    ┌─────────────────────────────┼─────────────────────────┐
                                    ↓                             ↓                         ↓
                              [VP_GROWTH]               [RISK_OFFICER]               [ACTUARY]
                              Agresywna                 Płynność                     Status Quo
                              [Gemini 3.1-pro]          [Gemini 3.1-pro]             [Gemini 3.1-pro]
                              CfoScenarioAnalysis       CfoScenarioAnalysis          CfoScenarioAnalysis
                                    │                             │                         │
                                    └─────────────────────────────┴─────────────────────────┘
                                                                  │
                                                    [Major CFO: generate_major_cfo_recommendation]
                                                    [Gemini 3.1-pro, temp=0.4]
                                                    [MAJOR_CFO_SYSTEM_PROMPT]
                                                                  │
                                                    MajorCfoLayoutOutput
                                                    { executive_summary, layout[] }
                                                                  │
                                                    ┌─────────────┴──────────────┐
                                                    ↓                            ↓
                                              [CfoReport]               [WebDAV + DB]
                                              SSE → Frontend             Archiwizacja
```

---

*Dokument wygenerowany: 2026-03-09 | Wersja systemu: MoE v2 (Mixture of Experts + Generative UI)*
