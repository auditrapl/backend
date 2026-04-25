import logging
import json
from uuid import UUID
from typing import Dict, Any

import os
from google import genai
from google.genai import types

from app.schemas import TaxAnalysisReport, TaxRequest

logger = logging.getLogger(__name__)

MODEL_NAME = "gemini-2.5-flash"

SYSTEM_PROMPT = """Jesteś zaawansowanym Analitykiem Podatkowym AI specjalizującym się w polskim prawie podatkowym (VAT, CIT, PIT, WHT). 
Twoim zadaniem jest generowanie zwięzłych, wysoce merytorycznych i ustrukturyzowanych analiz podatkowych na podstawie zapytań użytkownika. 
Twoje odpowiedzi muszą być w 100% oparte na aktualnych przepisach prawa oraz oficjalnych interpretacjach podatkowych (stan prawny na 2024-2025 r.).

Ton i Styl:
Język profesjonalny, prawno-księgowy, zwięzły. Unikaj lania wody. Skupiaj się na konkretnych limitach, stawkach, warunkach i numerach artykułów.

Zakres Tematyczny:
Jesteś ekspertem w m.in.: odliczeniach VAT (paliwo, hotele), kosztach reprezentacji (KUP vs NKUP), amortyzacji jednorazowej, limitach gotówkowych B2B, kilometrówkach, podatku u źródła (WHT - software, reklamy, certyfikaty), imporcie usług (Reverse Charge), ulgach (B+R, IP Box, Robotyzacja), Estońskim CIT, różnicach kursowych, kosztach finansowania dłużnego, cenach transferowych, przedawnieniach i darowiznach.

Wymagana Struktura Odpowiedzi:
Posługujesz się schematem JSON zawierającym sekcje (sections) typu `TaxSection`.
ZAWSZE wygeneruj DOKŁADNIE 2 sekcje (i ani jednej mniej lub więcej):
1. sekcja o `heading` = "Stan Prawny"
   - W `content` opisz zwięźle zasady ogólne, limity, stawki oraz warunki konieczne do spełnienia zgodnie z ustawą. Używaj wypunktowań (Markdown) dla czytelności.
   - W `citations` wylistuj konkretne artykuły, np. `label` = "Art. 86a Ustawy o VAT", `code` = "art-86a"
2. sekcja o `heading` = "Interpretacje DKIS / Orzecznictwo"
   - W `content` przywołaj stanowisko Dyrektora Krajowej Informacji Skarbowej (DKIS) lub sądów administracyjnych (WSA/NSA). Opisz, jak przepisy są stosowane w praktyce, wskaż ewentualne pułapki.
   - W `citations` podaj przykładową lub rzeczywistą sygnaturę interpretacji, np. `label` = "0114-KDIP4.4012...", `code` = "dkis-1"

Musisz bezwzględnie zwracać dane w żądanym formacie JSON pasującym do przekazanego schematu (Structured Outputs).
"""

# MAPPING z kluczy queryKey używanych we frontendzie (data.tsx) do pytania, w którym osadzimy zmienne
TAX_TEMPLATES = {
    'vat-fuel': 'Czy mogę odliczyć 100% VAT od paliwa do auta typu: {vehicleType} zgłoszonego do formularza: {form}?',
    'vat-hotel': 'Czy przysługuje odliczenie VAT od faktury za usługę hotelową dla: {beneficiary}?',
    'representation': 'Czy {expense} na kwotę {amount} PLN jest Kosztem (KUP) czy Reprezentacją (NKUP)?',
    'one-time-deprec': 'Jaki jest limit amortyzacji jednorazowej dla podatnika typu: {taxpayerType} w roku podatkowym: {year}?',
    'cash-limit': 'Jaka jest maksymalna kwota płatności gotówkowej między firmami B2B w polskim prawie?',
    'mileage': 'Jak rozliczyć przejazdy autem prywatnym w celach służbowych (Kilometrówka) po ostatnich zmianach w Nowym Ładzie?',
    'wht-software': 'Czy zakup licencji oprogramowania z kraju: {country} podlega podatkowi u źródła WHT? Kwota transakcji to {amount} PLN.',
    'wht-ads': 'Czy usługa reklamowa (np. na Facebooku) zakupiona z kraju: {country} wymaga pobrania WHT?',
    'reverse-charge': 'Jak zaksięgować fakturę za usługi z regionu: {region} (Reverse Charge) pod kątem VAT?',
    'r-and-d': 'Jakie koszty pracownicze kwalifikują się do Ulgi Badawczo-Rozwojowej w branży IT/Tech?',
    'ip-box': 'Jakie warunki trzeba spełnić, by skorzystać z 5% stawki podatku IP Box (kwalifikowane prawo własności intelektualnej)?',
    'gifts': 'Do jakiej kwoty prezent o typie: "{type}" (np. z logo firmy lub alkoholem) jest zwolniony z opodatkowania i stanowi KUP?',
    'estonian-cit': 'Jakie są podstawowe warunki wejścia w ryczałt od dochodów spółek ustrukturyzowanych (Estoński CIT) i jakie niesie to korzyści?',
    'fx-diff': 'Jak rozliczyć podatkowo (dodatnie/ujemne) różnice kursowe od faktury w EUR zapłaconej z konta w PLN?',
    'debt-financing': 'Jaki jest limit zaliczenia odsetek do kosztów podatkowych na podstawie Art. 15c ustawy o CIT?',
    'transfer-pricing': 'Czy transakcja usługowa z podmiotem powiązanym na łączną kwotę {amount} PLN w roku wymaga przygotowania dokumentacji Local File?',
    'statute-limit': 'Kiedy dokładnie przedawnia się zobowiązanie podatkowe (VAT/CIT) za rok podatkowy: {year}?',
    'donations': 'Czy darowizna pieniężna na rzecz odbiorcy typu: "{recipient}" może być w całości kosztem uzyskania przychodu lub odliczona od podstawy?',
    'robotization': 'Na czym polega ulga na robotyzację w firmie produkcyjnej i jakie konkretnie wydatki można z niej odliczyć w 2024/2025?',
    'wht-cert': 'Czy posiadanie tylko elektronicznej kopii certyfikatu rezydencji jest wystarczające przy płatnościach zagranicznych do kwoty: {amount} PLN w ciągu roku?'
}

async def run_taxes_agent(
    request: TaxRequest,
    user_id: str
) -> TaxAnalysisReport:
    """
    Główne entry-point do agenta podatkowego.
    Wypełnia template zapytania na podstawie parametrów przysyłanych z frontendu.
    """
    query_key = request.queryKey
    params = request.parameters or {}
    
    template = TAX_TEMPLATES.get(query_key)
    if not template:
        raise ValueError(f"Nie znaleziono szablonu dla klucza {query_key}.")
        
    # Bezpiecznie formatuj stringa wypełniając go parametrami (ignoruj brakujące)
    user_query = template
    for key, val in params.items():
        user_query = user_query.replace(f"{{{key}}}", str(val))
    
    prompt = f"Zapytanie od przedsiębiorcy/księgowego:\n\"{user_query}\"\n\nProszę wygeneruj TaxAnalysisReport z dokładnymi cytowaniami."

    try:
        client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
        
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=TaxAnalysisReport,
                temperature=0.2 # Niski, chcemy fakty nie fantazje
            ),
        )
        
        result_json = json.loads(response.text)
        return TaxAnalysisReport(**result_json)
        
    except Exception as e:
        logger.error(f"LLM Generation Error in Taxes Agent: {e}")
        try:
             logger.info("Falling back to gemini-1.5-pro for Taxes Agent...")
             response = client.models.generate_content(
                model='gemini-1.5-pro',
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    response_schema=TaxAnalysisReport,
                    temperature=0.2
                ),
             )
             result_json = json.loads(response.text)
             return TaxAnalysisReport(**result_json)
        except Exception as fallback_e:
             logger.error(f"Fallback failure. {fallback_e}")
             raise ValueError("Nie udało się przeanalizować danych. API LLM niedostępne.")
