import os
import re
import json
import logging
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from google import genai
from google.genai import types

from app.models import Report, User

logger = logging.getLogger("auditra.report_orchestrator")

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
client = genai.Client(api_key=GOOGLE_API_KEY) if GOOGLE_API_KEY else None

NARRATOR_SYSTEM_PROMPT = """
Jesteś CFO. Otrzymałeś twarde dane JSON z systemu analitycznego. Napisz zwięzłe podsumowanie dla zarządu. Użyj formatu punktatorów. Wskaż jedną szansę (np. zwiększenie zysku) i jedno zagrożenie (np. wysokie koszty operacyjne). Nie wymyślaj danych, opieraj się TYLKO na podanym JSON-ie. Zwróć tekst jako czysty Markdown bez dodatkowego owijania.

WIZUALIZACJA DANYCH (AUTONOMICZNA DECYZJA):
Jako CFO, oceń, czy przekazane surowe dane zyskają na czytelności, jeśli pokażemy je na wykresie.
- Wykres JEST ZALECANY dla: trendów w czasie, zestawień wielu kategorii (np. koszty per dział, ranking dostawców).
- Wykres NIE JEST ZALECANY dla: pojedynczych wskaźników lub płaskich danych.

JEŚLI zalecasz wykres, na samym końcu odpowiedzi dodaj blok danych w formacie:
[CHART_JSON]
{
  "chart_type": "bar", // Wybierz jedno z: "bar", "line", "pie"
  "title": "Tytuł wykresu",
  "data": [
    {"name": "Etykieta (np. Dział, Miesiąc)", "value": 15000.00}
  ]
}
[/CHART_JSON]

Jeśli wykres nie ma sensu, pomiń ten blok całkowicie.
"""

def _custom_json_serializer(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, UUID):
        return str(obj)
    return str(obj)

class ReportOrchestrator:

    @staticmethod
    async def stream_generate_and_save_report(user: User, db: AsyncSession, template_name: str, parameters: dict):
        if not client:
            yield f"data: {json.dumps({'step': 'error', 'detail': 'Brak klucza API dla Gemini.'})}\n\n"
            return
            
        logger.info(f"Rozpoczęto generowanie raportu '{template_name}' dla usera: {user.id}")

        params = parameters or {}
        user_query = params.get("user_query", "")

        # KROK 1: Wywołaj Gemini z użyciem treści z Raport_Prompt.md
        prompt_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "Raport_Prompt.md")
        try:
            with open(prompt_path, "r", encoding="utf-8") as f:
                prompt_template = f.read()
        except FileNotFoundError:
            logger.error(f"Nie znaleziono pliku: {prompt_path}")
            yield f"data: {json.dumps({'step': 'error', 'detail': 'Nie znaleziono pliku promptu.'})}\n\n"
            return

        current_date_str = datetime.utcnow().strftime("%Y-%m-%d")

        sql_prompt = prompt_template.replace(
            "{current_date}", current_date_str
        ).replace(
            "{report_template_name}", template_name
        ).replace(
            "{user_query}", user_query
        )

        # KROK 1-3: Generuj SQL, Waliduj, Wykonaj (Z systemem auto-korekty)
        max_retries = 2
        retry_count = 0
        current_sql_prompt = sql_prompt
        json_data = "[]"
        clean_sql = ""

        while retry_count <= max_retries:
            try:
                sql_response = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=current_sql_prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.0
                    )
                )
            except Exception as e:
                logger.error(f"Błąd modelu przy Text-to-SQL: {e}")
                yield f"data: {json.dumps({'step': 'error', 'detail': 'Błąd połączenia z modelem AI (Text-to-SQL).'})}\n\n"
                return

            raw_sql = sql_response.text
            match = re.search(r'```(?:sql)?\s*(.*?)\s*```', raw_sql, re.DOTALL | re.IGNORECASE)
            if match:
                clean_sql = match.group(1).strip()
            else:
                clean_sql = raw_sql.replace("```sql", "").replace("```", "").strip()

            # Emit SQL Preview only on first try or if we want to show corrections
            if retry_count == 0:
                yield f"data: {json.dumps({'step': 'sql', 'content': clean_sql})}\n\n"
            else:
                yield f"data: {json.dumps({'step': 'sql', 'content': f'-- AUTO-KOREKTA (Próba {retry_count}):\n{clean_sql}'})}\n\n"

            # SECURITY CHECK
            upper_sql = clean_sql.upper()
            forbidden_words = ["UPDATE", "DELETE", "INSERT", "DROP", "ALTER", "GRANT", "REVOKE"]
            is_secure = True
            for word in forbidden_words:
                if re.search(r'\b' + word + r'\b', upper_sql):
                    logger.warning(f"Zablokowano kwerendę. Wykryto komendę mutating: {word}")
                    yield f"data: {json.dumps({'step': 'error', 'detail': 'Zablokowano niebezpieczne zapytanie. Wykryto niedozwoloną komendę modyfikującą bazę.'})}\n\n"
                    return

            if re.search(r'SELECT\s+\*\s+FROM', upper_sql):
                logger.warning("Zablokowano kwerendę. Użyto SELECT * FROM.")
                yield f"data: {json.dumps({'step': 'error', 'detail': 'Zablokowano nieoptymalne zapytanie (użyto SELECT *). Wymagane jest jawne podanie kolumn.'})}\n\n"
                return

            # Wykonanie zapytania
            data_dump = []
            try:
                await db.execute(text(f"SET LOCAL request.jwt.claims = '{{\"sub\": \"{user.id}\"}}';"))
                result = await db.execute(text(clean_sql))
                rows = result.fetchmany(100)
                
                column_names = result.keys()
                for row in rows:
                    data_dump.append(dict(zip(column_names, row)))
                    
                json_data = json.dumps(data_dump, default=_custom_json_serializer, ensure_ascii=False)
                
                # Emit data preview and break loop on success
                yield f"data: {json.dumps({'step': 'data', 'content': json.loads(json_data)})}\n\n"
                break
                
            except DBAPIError as e:
                error_msg = str(e.__cause__) if e.__cause__ else str(e)
                logger.warning(f"Błąd wykonania SQL (Próba {retry_count}): {error_msg}")
                
                if retry_count < max_retries:
                    retry_count += 1
                    current_sql_prompt = (
                        f"Twoje poprzednie zapytanie SQL wywołało błąd bazy danych PostgreSQL:\n"
                        f"{error_msg}\n\n"
                        f"Zwrócony kod, który spowodował błąd to:\n```sql\n{clean_sql}\n```\n\n"
                        f"ZADANIE: Napraw ten błąd zapytania. Sprawdź warunki GROUP BY, agregacje oraz typy danych. "
                        f"Zwróć TYLKO nowy i poprawny kod SQL w bloku ```sql, żadnych innych wyjaśnień."
                    )
                    # Notify frontend about retrying
                    yield f"data: {json.dumps({'step': 'sql', 'content': f'-- Poprzednie zapytanie zwróciło błąd. AI naprawia kwerendę... (Próba {retry_count}/{max_retries})'})}\n\n"
                else:
                    yield f"data: {json.dumps({'step': 'error', 'detail': f'Błąd po {max_retries} próbach auto-korekty SQL: {error_msg}'})}\n\n"
                    return
            except Exception as e:
                logger.error(f"Nieznany błąd zapytania DB: {e}")
                yield f"data: {json.dumps({'step': 'error', 'detail': f'Błąd obróbki danych: {str(e)}'})}\n\n"
                return

        # KROK 5 (AGENT NARRATOR STRUMIENIOWY)
        narrator_prompt = f"{NARRATOR_SYSTEM_PROMPT}\n\nDANE RAPORTU:\n{json_data}"
        
        full_narrative = ""
        try:
            response_stream = client.models.generate_content_stream(
                model="gemini-2.5-flash",
                contents=narrator_prompt,
                config=types.GenerateContentConfig(
                    temperature=0.2
                )
            )
            for chunk in response_stream:
                if chunk.text:
                    full_narrative += chunk.text
                    yield f"data: {json.dumps({'step': 'narrative_chunk', 'content': chunk.text})}\n\n"
                    
        except Exception as e:
            logger.error(f"Błąd modelu Narratora (Stream): {e}")
            yield f"data: {json.dumps({'step': 'error', 'detail': 'Błąd połączenia z modelem Narratora.'})}\n\n"
            return

        # KROK 6: Wyciągnięcie [CHART_JSON], wyczyszczenie tekstu i zapis raportu
        chart_config_dict = None
        match = re.search(r'\[CHART_JSON\](.*?)\[/CHART_JSON\]', full_narrative, re.DOTALL | re.IGNORECASE)
        if match:
            chart_json_str = match.group(1).strip()
            # Oczyszczenie tekstu z bloku i ewentualnych znaczników markdown doczepionych przez model (np. ```json)
            full_narrative = re.sub(r'\[CHART_JSON\].*?\[/CHART_JSON\]', '', full_narrative, flags=re.DOTALL | re.IGNORECASE).strip()
            
            # Wyczyść sam json_str z nadmiarowych bloków markdown jak ```json
            chart_json_str = re.sub(r'```(?:json)?', '', chart_json_str).strip()
            
            try:
                chart_config_dict = json.loads(chart_json_str)
            except Exception as e:
                logger.error(f"Nie udało się sparsować chartConfig JSON: {e}")
                chart_config_dict = None

        try:
            # Ponieważ zmieniamy schemat na wylocie, parametry mogą przetrzymywać chartConfig do ponownego wczytania z frontendu, 
            # albo dodajemy nową kolumnę? Zapsizmy w parameters.
            if chart_config_dict:
                params["chartConfig"] = chart_config_dict

            new_report = Report(
                company_id=user.company_id,
                user_id=user.id,
                template_name=template_name,
                parameters=params,
                generated_sql=clean_sql,
                data_records=json.loads(json_data),
                narrative_text=full_narrative
            )
            db.add(new_report)
            await db.commit()
            await db.refresh(new_report)
            yield f"data: {json.dumps({'step': 'done', 'report_id': str(new_report.id), 'narrative': full_narrative, 'chartConfig': chart_config_dict})}\n\n"
        except Exception as e:
            logger.error(f"Błąd przy zapisie raportu: {e}")
            yield f"data: {json.dumps({'step': 'error', 'detail': 'Błąd podczas zapisywania raportu w bazie.'})}\n\n"

