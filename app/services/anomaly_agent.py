import os
import json
import logging
from uuid import UUID
from datetime import datetime
from typing import Dict, Any, Optional

from google import genai
from google.genai import types
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.schemas import AnomalyReport, AnomalyRequest

logger = logging.getLogger(__name__)

# Flash wszędzie — Pro zarezerwowany WYŁĄCZNIE dla CFO synthesis
MODEL_NAME = "gemini-2.5-flash"

# Configured in main.py usually, but we ensure it's set if we call it directly
# genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))

# 1. PREDEFINIOWANE ZAPYTANIA SQL DLA ANOMALII
ANOMALY_SQL_TEMPLATES = {
    # Duplikaty (Kwota/Data/Dostawca)
    "dup-exact": """
        SELECT i1.id, i1.internal_number, i1.contractor_name, i1.net_amount, i1.issue_date
        FROM invoices i1
        JOIN invoices i2 ON i1.id != i2.id
            AND i1.company_id = i2.company_id
            AND i1.net_amount = i2.net_amount
            AND i1.contractor_name = i2.contractor_name
            AND i1.issue_date = i2.issue_date
        WHERE i1.company_id = :company_id
        ORDER BY i1.issue_date DESC
        LIMIT 50;
    """,
    # Wydatki Weekendowe
    "weekend": """
        SELECT id, internal_number, contractor_name, net_amount, issue_date
        FROM invoices
        WHERE company_id = :company_id
          AND EXTRACT(DOW FROM issue_date) IN (0, 6) -- 0: Sunday, 6: Saturday
        ORDER BY issue_date DESC
        LIMIT 50;
    """,
    # Skoki abonamentów / Kosztów u jednego dostawcy > pewien %
    "subs-jump": """
        WITH monthly_spend AS (
            SELECT contractor_name, 
                   date_trunc('month', issue_date) as month, 
                   SUM(net_amount) as total_amount
            FROM invoices
            WHERE company_id = :company_id
            GROUP BY contractor_name, date_trunc('month', issue_date)
        ),
        spend_with_prev AS (
            SELECT contractor_name, month, total_amount,
                   LAG(total_amount) OVER (PARTITION BY contractor_name ORDER BY month) as prev_amount
            FROM monthly_spend
        )
        SELECT contractor_name, month, total_amount as net_amount, prev_amount,
               ((total_amount - prev_amount) / NULLIF(prev_amount, 0)) * 100 as increase_pct
        FROM spend_with_prev
        WHERE prev_amount IS NOT NULL 
          AND ((total_amount - prev_amount) / NULLIF(prev_amount, 0)) > 0.5 -- Wzrost o ponad 50%
        ORDER BY increase_pct DESC
        LIMIT 50;
    """
}

# 2. SYSTEM PROMPT
SYSTEM_PROMPT = """Jesteś Biegłym Rewidentem (Forensic Accountant) i Głównym Audytorem w systemie finansowym Auditra.
Twoim zadaniem jest analiza wstępnie przefiltrowanych transakcji firmy z bazy SQL i klasyfikacja ich pod kątem ryzyka nadużyć (Fraud), niegospodarności lub błędów ludzkich.

Otrzymasz JSON z podejrzanymi fakturami wyłapanymi przez skrypty bazy danych (np. potencjalne duplikaty, wydatki weekendowe, nagłe nagłe skoki kosztów).
Twoim zadaniem jest nadanie im KONTEKSTU BIZNESOWEGO i kategoryzacja.

ZASADY KWALIFIKACJI RYZYKA (risk_level - MUSI BYĆ DOKŁADNIE: 'Wysoki', 'Średni' lub 'Niski'):
1. WYSOKI (Krytyczne ryzyko wycieku gotówki / oszustwa): 
   - Dokładne duplikaty (Kwota + Data + Dostawca).
   - "Uśpieni Dostawcy" nagle wystawiający fakturę na duże kwoty bez historii.
   - Nowi kontrahenci z absurdalnymi kwotami.
2. ŚREDNI (Ryzyko operacyjne / budżetowe):
   - "Skoki Abonamentów" (np. dostawca chmurowy wystawił fakturę o 200% wyższą niż miesiąc temu).
   - "Słowa kluczowe NKUP" ("Kino", "SPA", "Konsola") w uwagach, jeśli dostawca nie pasuje.
   - Faktury "Tuż pod Limitem" akceptacyjnym.
3. NISKI (Błędy proceduralne / polityka firmy):
   - Wydatki Weekendowe (np. stacja paliw w niedzielę - normalne, ale warte odnotowania).
   - Dziwne Godziny, Brak Numeru Faktury (błędy księgowe).
   - Zaokrąglone Kwoty (np. równo 10 000.00 PLN za "usługi marketingowe", może wymagać spojrzenia).

TWOJE ZADANIE:
Dla każdego przekazanego poniżej w formacie JSON rekordu ułóż PROFESJONALNY i KRÓTKI opis (`description`) zrozumiały dla Prezesa/CFO.
Zamiast pisać "Zbyt wysoka faktura", napisz: "Faktura od AWS wzrosła o 130% względem poprzedniego miesiąca. Wymaga weryfikacji architektury."
Dopasuj odpowiednio nazwę dostawcy, kwotę oraz kategoryzację z 20 podanych pre-definiowanych klas w systemie (np. 'Duplikaty', 'Wydatki Weekendowe', 'Skoki Kosztów').
Dla dat stwórz zrozumiały `time_context` np. '2023-11-05' to po prostu 'Listopad 2023' albo '5 dni temu' (jeśli wiesz ile to dni od dziś).

Wygeneruj logicznie podsumowanie dla Prezesa w polu `summary` w obiekcie AnomalyReport. Całkowita ilość w `total_anomalies_count`. Zwróć obiekt zgodny ze schematem Pydantic.
"""

async def run_anomaly_agent(
    request: AnomalyRequest, 
    db: AsyncSession, 
    company_id: UUID
) -> AnomalyReport:
    """
    Główne entry-point do agenta anomalii.
    1. Generuje/wybiera zapytanie SQL.
    2. Odpytuje bazę po rekordy o potencjalnym ryzyku.
    3. Pakuje w JSON-a.
    4. Wysyła do Gemini 2.5 Pro z wymuszonym Structured Outputs.
    """
    query_key = request.queryKey
    params = request.parameters or {}
    
    # Krok 1: Wykonanie SQL na bazie dla danej kategorii
    raw_data = []
    
    sql_template = ANOMALY_SQL_TEMPLATES.get(query_key)
    
    if sql_template:
        # Predefiniowany wariant (bezpieczniejszy i szybszy)
        try:
            result = await db.execute(text(sql_template), {"company_id": company_id})
            rows = result.mappings().fetchall()
            # Convert row mapped dicts to standard dicts handling UUID/Dates
            for row in rows:
                r_dict = dict(row)
                for key, val in r_dict.items():
                    if isinstance(val, UUID):
                        r_dict[key] = str(val)
                    elif isinstance(val, datetime):
                        r_dict[key] = val.isoformat()
                    # Decimal to float
                    elif hasattr(val, 'to_eng_string'): 
                         r_dict[key] = float(val)
                raw_data.append(r_dict)
        except Exception as e:
            logger.error(f"SQL Execution Error in Anomaly Agent: {e}")
            raise ValueError(f"Błąd wykonywania zapytania do bazy danych: {str(e)}")
            
    else:
        # TODO: Dynamiczne generowanie z Gemini Text-To-SQL w wypadku braku predefiniowanego (możesz dodać później)
        # Zignorujmy to na MVP, fallback do losowej puli najnowszych
        logger.warning(f"No predefined SQL for anomaly key: {query_key}. Fetching sample latest to avoid empty mock.")
        result = await db.execute(text("SELECT id, contractor_name, net_amount, issue_date FROM invoices WHERE company_id = :cid ORDER BY created_at DESC LIMIT 10"), {"cid": company_id})
        rows = result.mappings().fetchall()
        for row in rows:
            r_dict = dict(row)
            for k, v in r_dict.items():
                if isinstance(v, UUID): r_dict[k] = str(v)
                elif isinstance(v, datetime): r_dict[k] = v.isoformat()
                elif hasattr(v, 'to_eng_string'): r_dict[k] = float(v)
            raw_data.append(r_dict)
            
    # Jeśli zapytanie z bazy nic nie zwróciło (firma 100% czysta), wracamy pusty raport
    if not raw_data:
        return AnomalyReport(
            summary=f"System (kategoria: {query_key}) nie zarejestrował żadnych anomalii podatno-ryzykownych według zadanego kryterium w tym okresie w bazie danych. Dane są czyste.",
            total_anomalies_count=0,
            anomalies=[]
        )

    # Krok 2: Generowanie z użyciem Gemini Structured Outputs
    prompt = f"""
Przekazuję Ci paczkę DANYCH Z BAZY JSON wyłonionych przez zapytanie dla typu: {query_key}.
Oto surowe dane wejściowe:
{json.dumps(raw_data, indent=2, ensure_ascii=False)}

Zanalizuj powyższe rekordy, nadaj im biznesowy kontekst wg SYSTEM_PROMPT. 
Pamiętaj o sklasyfikowaniu w skali 'Wysoki', 'Średni' lub 'Niski'. Zwróć TYLKO ustrukturyzowany AnomalyReport.
    """
    
    try:
        # Fallback na model testowany jeśli nie mamy 2.5
        client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
        
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=AnomalyReport,
                temperature=0.3 # Niski żeby nie halucynował zanadto na kwotach
            ),
        )
        
        # Pydantic validation of raw json text
        result_json = json.loads(response.text)
        return AnomalyReport(**result_json)
        
    except Exception as e:
        logger.error(f"LLM Generation Error in Anomaly Agent: {e}")
        # Spróbujmy na zwykłym jeśli 2.5-pro nie jest jeszcze w pełni w API klienta
        try:
             logger.info("Falling back to gemini-1.5-pro for Anomaly Agent...")
             response = client.models.generate_content(
                model='gemini-1.5-pro',
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    response_schema=AnomalyReport,
                    temperature=0.3
                ),
             )
             result_json = json.loads(response.text)
             return AnomalyReport(**result_json)
        except Exception as fallback_e:
             logger.error(f"Fallback didn't work. Return graceful error. {fallback_e}")
             raise ValueError("Nie udało się przeanalizować danych przez AI ze względu na przeciążenie API LLM.")