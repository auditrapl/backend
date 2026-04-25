import os
import json
import uuid
from datetime import datetime
from pydantic import BaseModel, Field
from typing import List
from sqlalchemy import text
from app.database import engine
from google import genai
from google.genai import types

LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api", "logs")

# Make sure you have the GEMINI_API_KEY environment variable set
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

class ChartPoint(BaseModel):
    month: str = Field(description="Miesiąc w krótkim formacie, np. 'Mar', 'Kwi' lub '2026-03'")
    baseline_cashflow: float = Field(description="Twardy baseline_cashflow wyliczony na stałe z historii (bez wdrażania strategii)")
    simulated_cashflow: float = Field(description="Symulowany bilans po uwzględnieniu proponowanych zmian")
    simulated_income: float = Field(description="Symulowane przychody w tym miesiącu (kluczowe przy J-Curve)")
    simulated_expenses: float = Field(description="Symulowane koszty w tym miesiącu (np. inwestycje)")
    math_reasoning: str = Field(description="Chain of Thought (CoT) z twardymi liczbami (np. 100k baseline + 50k wzrost - 10k prowizji = 140k)")
    event_trigger: str = Field(description="Kluczowe zdarzenie rynkowe w tym miesiącu (np. 'Faza inwestycji', 'Zwrot z ROI (Krzywa J)')")
    confidence_score: float = Field(description="Jak bardzo AI jest pewne tej prognozy (0.0 - 1.0)")

class StrategyResponse(BaseModel):
    summary: str = Field(description="Zwięzłe podsumowanie – wnioski zarządcze. Użyj formatowania markdown **pogrubienie** dla najważniejszych fraz, kwot i dat.")
    opportunities: List[str] = Field(description="Lista zidentyfikowanych szans/sugestii inwestycyjnych")
    risks: List[str] = Field(description="Lista zagrożeń i słabych punktów w obecnej płynności")
    justification: str = Field(description="Główne uzasadnienie biznesowe całej strategii")
    roi_prediction: str = Field(description="Ocena zwrotu z inwestycji (ROI). Kiedy na wykresie krzywa powraca do punktu wyjścia/rośnie względem inwestycji?")
    efficiency_metrics: List[str] = Field(description="Twarde metryki biznesowe pod lupą, np. 'Wzrost przychodów o Y%' albo 'Spadek marży o X%'")
    chart_data: List[ChartPoint] = Field(description="Wykres musi obejmować dokładnie 6 punktów (bieżący miesiąc + 5 przyszłych)")

# ─── SCHEMA DEFINITION ───────────────────────────────────────
DB_SCHEMA = """
Masz dostęp do następujących tabel. Twoje zapytania MUST ALWAYS filtrować po `company_id`!

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

3. Tabela: invoices
Kolumny:
- id (UUID)
- company_id (UUID)
- department_id (UUID)
- invoice_type (VARCHAR: 'expense' lub 'income')
- net_amount (numeric)
- vat_amount (numeric)
- gross_amount (numeric)
- currency (VARCHAR, default: 'PLN')
- issue_date (datetime)
- status (VARCHAR)
- is_paid (BOOLEAN)
- category_name (VARCHAR)
- contractor_name (VARCHAR)
- contractor_nip (VARCHAR)
- parsed_data (JSON)
- ai_analysis_json (JSON)

Uwaga: Status faktury (czy opłacona) określa pole `is_paid` (boolean). Kwoty mogą być liczone tylko dla poszczególnych `company_id`.
"""

# ─── STEP 1: TEXT-TO-SQL ─────────────────────────────────────
async def generate_sql(user_prompt: str, db_schema: str, company_id: str, date_from: str = None, date_to: str = None) -> str:
    """Uses LLM to generate a safe SELECT SQL query based on user prompt."""
    now = datetime.now()
    current_date = now.strftime("%Y-%m-%d")
    current_year = now.year
    
    date_context = ""
    if date_from and date_to:
        date_context = f"\nUWAGA: Użytkownik jawnie zdefiniował zakres dat od {date_from} do {date_to}. TWOJE ZAPYTANIE SQL MUSI BEZWZGLĘDNIE ZAWIERAĆ FILTR: `issue_date >= '{date_from}' AND issue_date <= '{date_to}'`."
    elif date_from:
        date_context = f"\nUWAGA: Użytkownik jawnie zdefiniował datę początkową {date_from}. TWOJE ZAPYTANIE SQL MUSI BEZWZGLĘDNIE ZAWIERAĆ FILTR: `issue_date >= '{date_from}'`."
    elif date_to:
        date_context = f"\nUWAGA: Użytkownik jawnie zdefiniował datę końcową {date_to}. TWOJE ZAPYTANIE SQL MUSI BEZWZGLĘDNIE ZAWIERAĆ FILTR: `issue_date <= '{date_to}'`."
    
    sys_prompt = f"""
Jesteś ekspertem SQL PostgreSQL. Twoim zadaniem jest przetłumaczenie pytania biznesowego na zapytanie (tylko odczyt, SELECT).
Dzisiejsza data i czas: {current_date}, (rok {current_year}). Używaj tego jako punktu odniesienia do analiz ("obecny kwartał", "w tym roku", itd.).{date_context}

Masz dostęp wyłącznie do następującego schematu powiązanych tabel:
{db_schema}

ZASADY KRYTYCZNE (BEZPIECZEŃSTWO WZGLĘDEM ROLI):
- **BEZWZGLĘDNIE** przy każdym zapytaniu DO TABEL `invoices` lub `departments` MUSISZ dodać filtr: `company_id = '{company_id}'`. Użytkownik nie może zobaczyć żadnych innych faktur ani departamentów poza swoimi.
- Używaj wysoce bezpiecznych JOINów jeśli wiążesz dane.

POZOSTAŁE ZASADY:
1. Zwróć TYLKO kod SQL. Bez formatowania markdown, bez "```sql". Wyłącznie sam czysty kod.
2. Nigdy nie używaj poleceń typu DROP, DELETE, UPDATE, INSERT, ALTER (bezpośrednie zagrożenie).
3. **KLUCZOWE DLA SYMULACJI**: Generuj kod SQL (np. z użyciem `GROUP BY date_trunc('month', issue_date)` i `invoice_type`), który dla ostatnich N miesięcy wyciągnie total `income`, total `expense` oraz `monthly_balance` (przychody - wydatki) jako wyznaczniki płynności. Twoje zapytanie musi stać się mocną bazą dla Pythona, aby mógł on obliczyć i przeprojektować stały, niezmienialny Baseline Cashflow 6 miesięcy do przodu bez interwencji AI.
4. **WYCIĄGANIE LUKI PŁATNICZEJ**: Wyciągnij średnią różnicę dni między terminem płatności a datą rzeczywistej płatności dla ostatnich 6 miesięcy. To pozwoli Agentowi Strategii zrozumieć, że gotówka nie wpływa do firmy w dniu wystawienia faktury.
"""
    
    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=sys_prompt,
            temperature=0.0
        )
    )
    
    sql_query = response.text.strip()
    
    # Remove markdown formatting if standard prompt rules failed
    if sql_query.startswith("```sql"):
        sql_query = sql_query[6:]
    if sql_query.endswith("```"):
        sql_query = sql_query[:-3]
    sql_query = sql_query.strip()
    
    # Basic safety check (though should be enforced by DB user rights)
    if any(forbidden in sql_query.upper() for forbidden in ["DROP", "DELETE", "UPDATE", "INSERT", "ALTER"]):
        raise ValueError("Generated SQL contains forbidden modifying keywords.")
        
    return sql_query

# ─── STEP 2: EXECUTE SQL ─────────────────────────────────────
async def execute_sql(sql_query: str) -> List[dict]:
    """Executes the given SQL against the database using SQLAlchemy."""
    async with engine.connect() as conn:
        try:
            result = await conn.execute(text(sql_query))
            # Convert results to list of dicts for LLM processing
            columns = result.keys()
            rows = [dict(zip(columns, row)) for row in result.fetchall()]
            return rows
        except Exception as e:
            # Re-raise with a clear message for the calling function to handle
            raise Exception(f"Database error executing SQL: {e}")

# ─── STEP 3: STRATEGIC ANALYSIS ──────────────────────────────
async def generate_strategy_analysis(user_prompt: str, db_results: List[dict], baseline_projection: List[dict]) -> StrategyResponse:
    """Uses LLM 3.0 Pro structured outputs to generate final business insights and J-Curve projections."""
    
    sys_prompt = """
Jesteś Dyrektorem Finansowym (CFO) Auditra klasy Enterprise. Twoim zadaniem jest stworzenie DYNAMICZNEJ prognozy uwzględniającej twardą matematykę, inwestycje oraz zwrot (Krzywa J / J-Curve).

ZASADY DYNAMIKI (J-CURVE):
1. **Twardy Baseline**: Otrzymujesz na wejściu 'baseline_projection' z wyliczonym na sztywno, stałym cashflow na najbliższe 6 miesięcy (M1-M6). Twoim celem jest wygenerowanie `simulated_cashflow_points`, które ZAWSZE modyfikują ten Baseline.
2. **Krzywa J (Inwestycja i Zwrot)**: Jeśli Prezes pyta o inwestycję (np. nowy sprzęt, zatrudnienie, marketing o 50k), NIE MOŻESZ narysować tylko spadku zapasu gotówki. 
    - Miesiąc 1-2 (Inwestycja): `simulated_expenses` rosną w dół, `simulated_cashflow` spada poniżej `baseline_cashflow`.
    - Miesiąc 3-6 (Zwrot/ROI): `simulated_income` agresywnie rośnie, `simulated_cashflow` ZNACZNIE PRZEBIJA `baseline_cashflow`.
3. **Luka Płatnicza**: Załóż przesunięcia gotówki względem faktur w oparciu o opóźnienia, których doszukałeś się w wyciągu bazy. Rozłóż efekty inwestycji w czasie (Efekt Domina).
4. **Twarda Matematyka (Chain of Thought)**: Dla KAŻDEGO z 6 miesięcy musisz bezwzględnie wypełnić pole `math_reasoning`. Pokaż dokładne wyliczenie np: "Baseline 100k + Wzrost przychodów 20k - Koszty wdrożenia 10k = 110k".
5. **Wskaźniki Biznesowe**: Ocen ROI w polu `roi_prediction` (np. "Break-even w 4. miesiącu") oraz wypisz 2-3 twarde wskaźniki w `efficiency_metrics` (np. "Wzrost CAC o 15%").

WYKRES (chart_data):
- Wykres musi obejmować DOKŁADNIE 6 PUNKTÓW, reprezentujących miesiące (od M1 do M6 dopasowane do baseline_projection).
Dla każdego punktu przypisz `simulated_income`, `simulated_expenses`, `event_trigger` oraz `confidence_score` (0.0-1.0).
"""

    context_str = json.dumps(db_results, default=str, ensure_ascii=False)
    baseline_str = json.dumps(baseline_projection, default=str, ensure_ascii=False)
    
    user_message = f"""
Pytanie Prezesa: {user_prompt}

Twarde dane z SQL (Odpowiedź z bazy, historia wyciągu): 
{context_str}

TWARDY BASELINE NA NAJBLIŻSZE 6 MIESIĘCY (Wyliczony przez skrypt w Pythonie):
{baseline_str}

Na podstawie tych danych i prośby Prezesa, wygeneruj dokładną analizę i 6-miesięczną symulację finansową z wykorzystaniem J-Curve. Pamiętaj o uzupełnieniu pola `math_reasoning` dla każdego miesiąca!
"""

    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=user_message,
        config=types.GenerateContentConfig(
            system_instruction=sys_prompt,
            response_mime_type="application/json",
            response_schema=StrategyResponse,
        )
    )

    # The Google GenAI SDK returns the parsed text directly as json string when schema is provided
    # Let's parse it into Pydantic model (defensywnie)
    from app.services._json_parse import safe_model_validate_json
    return safe_model_validate_json(StrategyResponse, response.text)

# ─── MAIN ORCHESTRATOR ───────────────────────────────────────
async def run_strategy_agent(user_prompt: str, company_id: str, date_from: str = None, date_to: str = None) -> dict:
    """Main function integrating Text-to-SQL, execution, and LLM Analysis."""
    session_id = str(uuid.uuid4())
    log_file_name = f"strategy_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{session_id}.json"
    log_path = os.path.join(LOGS_DIR, log_file_name)
    os.makedirs(LOGS_DIR, exist_ok=True)
    
    log_data = {
        "user_prompt": user_prompt,
        "company_id": company_id,
        "date_from": date_from,
        "date_to": date_to,
        "sql_query": None,
        "db_results": None,
        "sql_error": None,
        "strategy_response": None,
        "final_error": None
    }
    
    def save_log():
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(log_data, f, ensure_ascii=False, indent=2)
            
    try:
        # Step 1
        print(f"Generating SQL for prompt: '{user_prompt}' restricted to company: {company_id} (dates: {date_from} - {date_to})")
        sql_query = await generate_sql(user_prompt, DB_SCHEMA, company_id, date_from, date_to)
        log_data["sql_query"] = sql_query
        save_log()
        print(f"Generated SQL:\n{sql_query}")
        
        # Step 2
        print("Executing SQL...")
        try:
            db_results = await execute_sql(sql_query)
        except Exception as query_err:
            # Fallback if query fails (e.g. wrong syntax generated by LLM)
            print(f"SQL Execution failed! {query_err}. Passing empty context to analysis.")
            log_data["sql_error"] = str(query_err)
            db_results = []
            
        # Convert date/datetime/uuid so it can be JSON serialized
        serializable_results = json.loads(json.dumps(db_results, default=str))
        log_data["db_results"] = serializable_results
        save_log()
            
        print(f"Fetched {len(db_results)} rows from database.")

        # --- Python Baseline Calculation (Enterprise Paradigm) ---
        print("Calculating deterministic Baseline Cashflow in Python...")
        # A simple naive average baseline from history
        historical_balance = 0.0
        historical_income = 0.0
        historical_expenses = 0.0
        months_counted = 0
        
        for r in db_results:
            # We attempt to find income/expense/balance fields regardless of exact aliasing
            income = float(r.get('total_income', r.get('income', 0)))
            expense = float(r.get('total_expense', r.get('expense', 0)))
            balance = float(r.get('monthly_balance', r.get('balance', r.get('sum', 0))))
            
            if balance == 0.0 and income == 0.0 and expense == 0.0:
                # Fallback to pure net_amount if the strict SQL structure wasn't followed
                val = float(r.get('net_amount', r.get('sum(net_amount)', 0)))
                # Best guess constraint (usually expenses are positive in invoice table if expense type)
                balance -= val 
            
            historical_balance += balance
            months_counted += 1
            
        if months_counted > 0:
            avg_balance = historical_balance / months_counted
        else:
            avg_balance = 0.0  # Fallback if no history
            
        # Build 6 months baseline mapping
        current_month_index = datetime.now().month
        month_names = ["Sty", "Lut", "Mar", "Kwi", "Maj", "Cze", "Lip", "Sie", "Wrz", "Paź", "Lis", "Gru"]
        
        baseline_projection = []
        # Current accumulated base pool (could be extracted from accounts, assuming 0/relative for projection)
        current_pool = avg_balance 
        
        for i in range(6):
            m_idx = (current_month_index - 1 + i) % 12
            # For simplicity, baseline assumes straight average line for future cashflow pool addition each month
            baseline_projection.append({
                "month": month_names[m_idx],
                "expected_added_balance": round(avg_balance, 2),
                "cumulative_baseline": round(current_pool, 2)
            })
            current_pool += avg_balance
            
        log_data["baseline_projection"] = baseline_projection
        save_log()
        print(f"Baseline projection generated: {baseline_projection}")

        # Step 3
        print("Running strategic analysis with Gemini 3.1 Pro Preview...")
        strategy_response = await generate_strategy_analysis(user_prompt, db_results, baseline_projection)
        
        log_data["strategy_response"] = strategy_response.model_dump()
        save_log()
        
        # Format cleanly to be returned by an API Endpoint
        return {
            "status": "success",
            "sql_executed": sql_query,
            "data_count": len(db_results),
            "raw_data": serializable_results,
            "result": strategy_response.model_dump()
        }
        
    except Exception as e:
        print(f"Strategy Agent failed critically: {e}")
        log_data["final_error"] = str(e)
        save_log()
        return {
            "status": "error",
            "message": str(e)
        }
