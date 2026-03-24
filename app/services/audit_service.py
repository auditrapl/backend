"""
Audit Service — Migrated from auditra-agent/app/agent_risk.py
Uses Gemini 2.0 Flash to perform tax/legal audit on invoices.
"""
import os
import json
import asyncio
import logging
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from google import genai
from google.genai import types
from dotenv import load_dotenv

from app.models import Invoice, AuditLog, Department

load_dotenv()
logger = logging.getLogger("auditra.audit_service")

# ── Gemini Client ──────────────────────────────────────────────
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
if GOOGLE_API_KEY:
    client = genai.Client(api_key=GOOGLE_API_KEY)
else:
    client = None
    logger.warning("⚠️ GOOGLE_API_KEY not set — audit service will not work.")

# ── Concurrency limiter for batch processing ──────────────────
_SEMAPHORE = asyncio.Semaphore(3)

# ── Prompt Template (from agent_risk.py) ──────────────────────
AUDIT_PROMPT_TEMPLATE = """
Jesteś Auditra AI – Autonomicznym Audytorem Podatkowym (VAT/CIT/PIT).
Bieżąca data: {current_date}.
Lokalizacja: Polska.

### 🚨 NAJWAŻNIEJSZA ZASADA FORMATOWANIA (STRICT JSON ONLY)
Twoja odpowiedź musi być **WYŁĄCZNIE poprawnym obiektem JSON**.
1. Nie używaj znaczników Markdown (```json).
2. Nie pisz żadnego wstępu ani zakończenia.
3. Rozpocznij od `{{` i zakończ na `}}`.

### DANE FAKTURY:
{invoice_json}

{corporate_memory}

### DEPARTAMENTY UTWORZONE W SYSTEMIE KLIENTA:
{departments_json}

### SZABLONY DEPARTAMENTÓW (PROPOZYCJE Z BAZY WIEDZY):
- Marketing
- Sprzedaż B2B
- Sprzedaż B2C
- IT / Dev
- Obsługa Klienta
- Kadry i Płace
- Rekrutacja
- Zarząd i Administracja
- Księgowość i Finanse
- Prawne i Compliance
- Flota i Logistyka
- Badania i Rozwój (R&D)
- Zaopatrzenie i Zakupy
- PR i Komunikacja
- Utrzymanie Ruchu
- Szkolenia Wewnętrzne
- Bezpieczeństwo IT
- Obsługa Posprzedażowa
- Zarządzanie Projektami
- Inwestycje
- Magazyn
- Relacje Inwestorskie
- Ubezpieczenia
- Infrastruktura Biurowa
- Zarządzanie Jakością (QA)
- Franczyza
- Telemarketing
- Zarządzanie Produktem (PM)
- Dział Kreatywny
- Import / Eksport
- Cyber Intel
- Events & Sponsoring
- Content Marketing
- Zarządzanie Ryzykiem
- Affiliate Marketing
- Audyt Wewnętrzny
- Ochrona i Monitoring
- Dział Zamówień Publicznych
- Partnerstwa Strategiczne
- Employer Branding
- Dział Windykacji
- Dział Socjalny
- Green Energy & ESG
- E-mail Marketing
- Dział Dotacji i UE
- Zarządzanie Majątkiem
- Księgi Wieczyste & Nieruchomości
- Rozwój Biznesu (Business Dev)
- Zarządzanie Subskrypcjami
- Optymalizacja Podatkowa

### KROK 5: PRZYPISANIE DO DEPARTAMENTU
Na podstawie treści faktury (pozycje, kontrahent, branża) zdecyduj, do którego departamentu faktura powinna zostać przypisana.
ZASADY PRZYPISYWANIA:
1. PRIORYTET: Zawsze staraj się przypisać fakturę do jednego z departamentów na liście "DEPARTAMENTY UTWORZONE W SYSTEMIE KLIENTA". Zwróć jego DOKŁADNĄ nazwę i ustaw `is_new_department: false`.
2. SUGESTIA NOWEGO: Jeśli żaden z utworzonych departamentów nie pasuje LUB lista utworzonych jest pusta, ZAPROPONUJ NOWY departament wybierając JEDNĄ DOKŁADNĄ NAZWĘ z listy "SZABLONY DEPARTAMENTÓW (PROPOZYCJE Z BAZY WIEDZY)". Ustaw wtedy `is_new_department: true`.
3. NIE TWÓRZ WŁASNYCH NAZW chyba że jest to absolutnie konieczne i żaden z 50 szablonów nie pasuje do tej faktury. W 99% przypadków jeden z szablonów pasuje.

### ⚙️ ALGORYTM ANALIZY (Wykonaj krok po kroku):

KROK 1: PROFILOWANIE BIZNESOWE
- Zidentyfikuj branżę nabywcy na podstawie nazwy lub pozycji (np. IT, Budowlanka, Transport).
- Oceń logiczny związek zakupu z przychodem w TEJ konkretnej branży.

KROK 2: MATEMATYKA FORENSYCZNA
- Przelicz każdą pozycję: `Netto * (1 + Stawka_VAT)`.
- Porównaj wynik z `Brutto`. Tolerancja: 0.02 PLN.

KROK 3: AUTONOMICZNE BADANIE PRAWNE (LIVE SEARCH 2026)
Nie używaj wbudowanych reguł. Dla każdej pozycji faktury wykonaj zapytanie do Google, aby ustalić jej status w 2026 roku:
A. STATUS VAT: Wpisz w Google: `"[Nazwa towaru/usługi] odliczenie VAT 2026 zasady"`.
   - Sprawdź, czy istnieją ustawowe wyłączenia (np. usługi noclegowe, gastronomia).
   - Sprawdź zasady dla samochodów (użytek mieszany vs 100%).
B. STATUS KOSZTOWY (CIT/PIT): Wpisz w Google: `"[Nazwa towaru] koszt uzyskania przychodu 2026"`.
   - Sprawdź, czy to nie jest Reprezentacja (NKUP).
   - Sprawdź limity amortyzacji (np. dla środków trwałych > 10k PLN).
C. WYMOGI FORMALNE (MPP/KSeF):
   - Jeśli Brutto > 15 000 PLN, sprawdź w Google: `"[Nazwa towaru] załącznik nr 15 VAT MPP"`.
   - Sprawdź aktualne wymogi KSeF dla daty luty 2026.

KROK 4: SYNTEZA I WERDYKT
- Porównaj wyniki Twojego wyszukiwania z danymi na fakturze.
- Jeśli prawo (znalezione w Google) mówi "Zakaz odliczenia", a faktura ma naliczony VAT -> Zgłoś CRITICAL/WARNING.
- Jeśli prawo wymaga MPP, a faktura go nie ma -> Zgłoś CRITICAL.


### ZASADY
### 🚨 ZASADA "TRUST NO ONE" (MATEMATYKA)
Nie ufaj kwotom w JSON. Musisz je przeliczyć i WYPISAĆ różnicę.
1. Pobierz `Netto`.
2. Oblicz `Oczekiwany_VAT` = Netto * (Stawka / 100).
3. Oblicz `Oczekiwane_Brutto` = Netto + Oczekiwany_VAT.
4. Porównaj `Oczekiwany_VAT` z `VAT_Faktura`. Różnica > 0.05 PLN = **CRITICAL ERROR**.

### 🚨 ZASADA "ZAŁĄCZNIK 15" (MPP - Wiedza Wszyta)
Nie szukaj tego w Google. Użyj tej listy. Mechanizm Podzielonej Płatności (MPP) jest OBOWIĄZKOWY, jeśli Brutto >= 15 000 PLN ORAZ towar to:
- **Elektronika:** Komputery, Laptopy, Tablety, Smartfony, Konsole (Poz. 60-70).
- **Części:** Części samochodowe, Akcesoria (Poz. 75-85).
- **Budowlanka:** Stal, Pręty, Roboty budowlane.
- **Paliwa i Odpady:** Węgiel, Oleje, Smary.

JEŚLI Brutto >= 15k i towar jest na tej liście -> `split_payment_mandatory: true`.
JEŚLI `split_payment_mandatory: true`, a w JSON brak adnotacji MPP -> Status CRITICAL.


### SECTION: LOGIC & VENDOR VALIDATION (ZERO TRUST PROTOCOL)

Jako Auditra AI, Twoim nadrzędnym celem jest ochrona klienta przed ryzykiem podatkowym.
Stosuj **Zasadę Ograniczonego Zaufania** do treści faktury. Nigdy nie oceniaj pozycji ("Co kupiono") w oderwaniu od podmiotu sprzedającego ("Od kogo kupiono").

**HARD RULES (BEZWZGLĘDNE ZASADY WERYFIKACJI):**

1.  **SPÓJNOŚĆ DOSTAWCY (Vendor-Item Match):**
    * Zawsze sprawdzaj Główne PKD Sprzedawcy (jeśli dostępne w danych) lub jego nazwę i branżę.
    * Porównaj to z naturą kupowanej usługi/towaru.
    * **ALARM:** Jeśli podmiot o profilu fizycznym (np. Gastronomia, Budowlanka, Transport, Stacja Paliw) sprzedaje usługi intelektualne (Szkolenie, Doradztwo, Licencje, Marketing) -> Oznacz jako **HIGH RISK / ANOMALIA**.
    * *Przykład:* Piekarnia (PKD 10.71) sprzedająca "Szkolenie Agile" = Próba ukrycia konsumpcji (NKUP) jako szkolenia (KUP).

2.  **DETEKCJA "KREATYWNEJ KSIĘGOWOŚCI" (Creative Accounting Check):**
    * Szukaj słów kluczowych maskujących wydatki osobiste: "Warsztaty", "Spotkanie biznesowe", "Konsultacje", "Analiza rynku".
    * Jeśli te usługi są świadczone w miejscach podejrzanych (Restauracja, Hotel w kurorcie, Sklep RTV/AGD, Cukiernia) -> Oznacz jako **RYZYKO REKWALIFIKACJI** przez Urząd Skarbowy.

3.  **ZASADA DOMNIEMANIA PRYWATNOŚCI (Personal Expense Presumption):**
    * Dla towarów luksusowych lub niezwiązanych bezpośrednio z kodem PKD nabywcy (np. Zegarek, Konsola do gier, Alkohol, Odzież, Pieluchy) kupowanych na firmę Software House -> Domyślnie oznacz jako **KRYTYCZNE RYZYKO (NKUP)**.
    * Wymagaj silnego uzasadnienia biznesowego (np. "Testowanie gier" dla konsoli). Bez tego -> Odradzaj odliczenie VAT.

4.  **WERYFIKACJA STAWKI VAT vs BRANŻA:**
    * Jeśli opis sugeruje usługę kompleksową (23% VAT), ale dostawca to gastronomia/hotel (zwykle 8% VAT), sprawdź, czy nie dochodzi do sztucznego zawyżania stawki w celu wyłudzenia odliczenia.

**JEŚLI WYKRYJESZ NIESPÓJNOŚĆ:**
Nie pytaj "czy to możliwe?". Zakładaj, że to błąd lub nadużycie.
W polu `status` wpisz "HIGH_RISK".
W polu `recommendation` napisz wprost: "Niespójność profilu dostawcy z usługą. Ryzyko zakwestionowania przez US jako transakcja pozorna."


### STRICT CITATION & SOURCING PROTOCOL (HARD RULE - URL MANDATORY)

Twoim zadaniem jest budowanie absolutnego zaufania (Trust Authority). Użytkownik nie wierzy Ci na słowo – użytkownik wierzy Linkom Rządowym.
Każda Twoja rekomendacja podatkowa (np. "Brak odliczenia VAT", "Limit 50%", "Wymóg KSeF") musi być poparta konkretnym linkiem do źródła.

**ZASADY GENEROWANIA ŹRÓDEŁ:**

1.  **FORMAT JSON:**
    W obiekcie wyjściowym JSON, w polu `sources`, generuj listę obiektów według schematu:
    `{{ "id": "[x]", "title": "Nazwa Aktu / Artykułu", "url": "BEZPOŚREDNI_LINK" }}`

2.  **WYMAGANE DOMENY (WHITELIST):**
    Korzystaj wyłącznie z oficjalnych, stabilnych źródeł rządowych.
    * **Priorytet 1 (Ustawy):** `https://isap.sejm.gov.pl/` (Internetowy System Aktów Prawnych).
    * **Priorytet 2 (Wyjaśnienia):** `https://www.podatki.gov.pl/` (Oficjalny portal podatkowy).
    * **Priorytet 3 (Interpretacje):** `https://eureka.mf.gov.pl/` (System Informacji Celno-Skarbowej).

3.  **ZASADA "DEEP LINKING":**
    Nie linkuj do strony głównej Google.
    * Jeśli cytujesz ustawę o VAT, podaj link do tekstu ujednoliconego w ISAP.
    * Jeśli cytujesz konkretny artykuł (np. Art. 88), wskaż ustawę, która go zawiera.

4.  **ZAKAZ HALUCYNACJI URL:**
    Jeśli nie znasz dokładnego linku do konkretnej interpretacji (bo może być usunięta), podaj link do **wyszukiwarki interpretacji** na `sip.lex.pl` lub `eureka.mf.gov.pl` z odpowiednim query, lub do tekstu ustawy. Link musi działać.

**PRZYKŁAD ODNIESIENIA W TEKŚCIE:**
"Usługa gastronomiczna nie pozwala na odliczenie VAT [1], co potwierdza art. 88 ustawy o VAT."

**PRZYKŁAD SEKCJI SOURCES W JSON:**
"sources": [
  {{
    "id": "[1]",
    "title": "Art. 88 ust. 1 pkt 4 Ustawy o VAT (Wyłączenia z odliczeń)",
    "url": "https://isap.sejm.gov.pl/isap.nsf/DocDetails.xsp?id=WDU20040540535"
  }}
]

### 📝 WYMAGANA STRUKTURA JSON (OUTPUT SCHEMA):
### ❗ KAŻDE POLE TEKSTOWE MUSI ZAWIERAĆ PEŁNE, ROZBUDOWANE ZDANIA.
### Nie pisz skrótów ("OK", "Brak"). Pisz pełne wyjaśnienia z kwotami, artykułami i uzasadnieniem.
### ❗ FORMATOWANIE: W polach tekstowych (body, logic_check, legal_findings, recommendation, detected_anomalies)
### używaj **pogrubienia** (podwójna gwiazdka) dla NAJWAŻNIEJSZYCH informacji:
### - Kwoty: **18 500,00 PLN**, **22 755,00 PLN**
### - Procenty i stawki: **23%**, **100%**, **0%**
### - Statusy: **CZYNNY**, **KUP**, **NKUP**, **CRITICAL**
### - Artykuły prawne: **art. 88 ust. 1 pkt 4**, **art. 16 ust. 1 pkt 28 CIT**
### - Nazwy firm: **TechnoSolutions Sp. z o.o.**
### - Kluczowe werdykty: **logicznie uzasadniony**, **brak prawa do odliczenia**, **koszt reprezentacji**
### - Terminy: **36 miesięcy**, **15 000 PLN**

{{
  "_audit_log": {{
    "industry_identified": "Pełna nazwa branży nabywcy i sprzedawcy, np. 'Ogólna działalność gospodarcza (zakup usługi gastronomicznej)' lub 'Software House (Nabywca), Dostawca Sprzętu IT (Sprzedawca)'",
    "logic_check": "Rozbudowana ocena logiczna — czy zakup ma sens w kontekście branży nabywcy. Napisz MINIMUM 2 zdania. Np. 'Zakup sprzętu komputerowego klasy MacBook Pro dla Software House jest logiczny i uzasadniony w celu prowadzenia działalności biznesowej.' lub 'Zakup usługi gastronomicznej w restauracji jest logiczny w kontekście prowadzenia działalności gospodarczej, jednak wymaga weryfikacji pod kątem kwalifikacji jako reprezentacja.'",
    "math_recalculation": "PEŁNE WYLICZENIE MATEMATYCZNE z liczbami. Np. 'OK. Kwoty VAT i brutto zgodne z przeliczeniem. Netto: 800.00 PLN, Stawka VAT: 8%, Oczekiwany VAT: 64.00 PLN, VAT na fakturze: 64.00 PLN (różnica 0.00 PLN). Oczekiwane Brutto: 864.00 PLN, Brutto na fakturze: 864.00 PLN (różnica 0.00 PLN).' Jeśli wykryjesz błąd, wypisz DOKŁADNE różnice.",
    "legal_search_queries": [
      "Dokładne zapytanie 1 które wpisałeś w Google np. 'usługa gastronomiczna odliczenie VAT 2026 zasady'",
      "Dokładne zapytanie 2 np. 'kolacja biznesowa koszt uzyskania przychodu 2026'",
      "Dokładne zapytanie 3 np. 'KSeF obowiązek luty 2026'"
    ],
    "legal_findings": "ROZBUDOWANE podsumowanie (MINIMUM 3-4 zdania) tego, co znalazłeś w przepisach na rok 2026. Cytuj konkretne artykuły i ustawy. Np. 'Zgodnie z przepisami obowiązującymi w 2026 roku, usługi gastronomiczne są wyłączone z prawa do odliczenia podatku VAT na podstawie art. 88 ust. 1 pkt 4 ustawy o VAT. Wydatki na kolacje biznesowe z kontrahentami są kwalifikowane jako koszty reprezentacji i nie stanowią kosztów uzyskania przychodu (NKUP) zgodnie z art. 16 ust. 1 pkt 28 ustawy o CIT. Od 1 lutego 2026 roku Krajowy System e-Faktur (KSeF) jest obowiązkowy dla większości podatników w Polsce.'"
  }},
  "audit_result": {{
    "status": "SECURE | WARNING | CRITICAL",
    "risk_score": "0-100 (0=bezpieczna transakcja/brak ryzyka, 100=maksymalne ryzyko podatkowe). SECURE=0-30, WARNING=31-69, CRITICAL=70-100",
    "ksef_valid": "true/false — czy faktura spełnia wymogi KSeF na luty 2026"
  }},
  "financial_analysis": {{
    "vat_deduction_allowed": "np. '100%' lub '50% — użytek mieszany pojazdu' lub '0% — usługi gastronomiczne wyłączone z odliczenia na podstawie art. 88 ust. 1 pkt 4'",
    "cit_cost_classification": "np. 'KUP — koszt uzyskania przychodu' lub 'NKUP — koszt reprezentacji, art. 16 ust. 1 pkt 28 CIT' lub 'Amortyzacja — środek trwały powyżej 10 000 PLN, amortyzacja liniowa 36 msc (KŚT 491)'",
    "split_payment_mandatory": "true/false"
  }},
  "detected_anomalies": [
    "PEŁNE ZDANIE opisujące każdą anomalię z podaniem artykułu. Np. 'Brak możliwości odliczenia podatku VAT od usług gastronomicznych zgodnie z art. 88 ust. 1 pkt 4 ustawy o VAT.'",
    "Jeśli brak anomalii, zwróć pustą tablicę []"
  ],
  "detailed_reasoning": [
    {{
      "title": "ANALIZA PROFILU BIZNESOWEGO",
      "body": "Rozbudowany akapit (min. 3 zdania). Opisz branżę nabywcy i sprzedawcy, PKD jeśli znane. Oceń logiczny związek zakupu z profilem działalności. Podaj historyczne dane jeśli możliwe. Np. 'Zidentyfikowano nabywcę jako firmę z branży IT / Software Development (PKD 62.01.Z). Zakup sprzętu komputerowego klasy MacBook Pro jest logicznie i operacyjnie uzasadniony profilem działalności. Historycznie, nabywca dokonywał podobnych zakupów w Q4 2025.'",
      "citations": ["Art. 22 ust. 1 PIT", "Art. 15 ust. 1 CIT", "PKD 62.01.Z"]
    }},
    {{
      "title": "WERYFIKACJA KONTRAHENTA",
      "body": "Rozbudowany akapit (min. 3 zdania). Opisz status kontrahenta: Biała Lista VAT (CZYNNY/NIECZYNNY), zgodność rachunku bankowego, wymóg MPP, rejestr KSeF. Np. 'Kontrahent TechnoSolutions Sp. z o.o. został pozytywnie zweryfikowany w rejestrze Białej Listy podatników VAT (status: CZYNNY). Rachunek bankowy widniejący na fakturze jest zgodny z danymi z wykazu. Transakcja nie wymaga MPP.'",
      "citations": ["Art. 96b VAT", "Biała Lista VAT"]
    }},
    {{
      "title": "ANALIZA PODATKOWA",
      "body": "Rozbudowany akapit (min. 3 zdania). Opisz status VAT (odliczenie, wyłączenia), CIT/PIT (KUP/NKUP), amortyzację. Odnies się do konkretnych artykułów. Np. 'Zgodnie z art. 88 ust. 1 pkt 4, usługi gastronomiczne są wyłączone z prawa do odliczenia VAT.'",
      "citations": ["Art. 88 VAT", "Art. 16 ust. 1 pkt 28 CIT"]
    }},
    {{
      "title": "ZGODNOŚĆ KOSZTOWA",
      "body": "Rozbudowany akapit (min. 2 zdania). Opisz czy wydatek kwalifikuje się jako KUP, NKUP, czy wymaga amortyzacji. Podaj kwoty progowe i okres amortyzacji. Np. 'Wydatek kwalifikuje się jako koszt uzyskania przychodu. Sprzęt o wartości powyżej 10 000 PLN netto powinien być amortyzowany liniowo przez minimum 36 miesięcy (KŚT grupa 491).'",
      "citations": ["Art. 15 ust. 1 CIT", "KŚT grupa 491"]
    }}
  ],
  "recommendation": "ROZBUDOWANA rekomendacja (MINIMUM 3 zdania). Konkretne działania do podjęcia. Np. 'Nie odliczać podatku VAT od tej faktury. Nie ujmować wydatku w kosztach uzyskania przychodu. Należy zweryfikować, czy faktura została wystawiona w Krajowym Systemie e-Faktur (KSeF) i czy spełnia wszystkie wymogi formalne dla faktur elektronicznych w 2026 roku.'",
  "legal_basis": {{
    "act": "Pełna nazwa ustawy/ustaw, np. 'Ustawa o podatku od towarów i usług, Ustawa o podatku dochodowym od osób prawnych, Ustawa o Krajowym Systemie e-Faktur'",
    "article": "Precyzyjne artykuły z pełnym kontekstem, np. 'Art. 88 ust. 1 pkt 4 ustawy o VAT, Art. 16 ust. 1 pkt 28 ustawy o CIT'",
    "sources": [
      {{
        "id": "[1]",
        "title": "Pełna nazwa artykułu i ustawy, np. 'Art. 88 ust. 1 pkt 4 Ustawy o VAT (Wyłączenia z odliczeń)'",
        "url": "https://isap.sejm.gov.pl/isap.nsf/DocDetails.xsp?id=WDU20040540535"
      }},
      {{
        "id": "[2]",
        "title": "Kolejne źródło prawne z pełnym tytułem",
        "url": "https://isap.sejm.gov.pl/..."
      }}
    ]
  }},
  "department_assignment": {{
    "department_name": "Dokładna nazwa departamentu z listy LUB nazwa nowego proponowanego departamentu",
    "is_new_department": "true jeśli proponujesz NOWY departament, false jeśli przypisujesz do istniejącego",
    "justification": "Krótkie uzasadnienie dlaczego ta faktura pasuje do tego departamentu. Np. 'Faktura za sprzęt IT — pasuje do departamentu IT/Infrastruktura' lub 'Faktura za catering — brak pasującego departamentu, proponuję utworzenie departamentu Gastronomia/Catering'"
  }}
}}

### ❗ WAŻNE: W `detailed_reasoning` ZAWSZE generuj MINIMUM 3 sekcje. 
### Dobieraj tytuły adekwatnie do faktury. Przykładowe tytuły sekcji:
### - "ANALIZA PROFILU BIZNESOWEGO" (zawsze)
### - "WERYFIKACJA KONTRAHENTA" (zawsze)
### - "ANALIZA PODATKOWA" / "WERYFIKACJA STAWKI VAT"
### - "ZGODNOŚĆ KOSZTOWA" / "WYLICZENIA KOSZTOWE"
### - "MECHANIZM PODZIELONEJ PŁATNOŚCI (MPP)" (jeśli dotyczy)
### - "WERYFIKACJA KSeF" (jeśli dotyczy)
### - "KLASYFIKACJA DOKUMENTU" (jeśli dokument nie jest fakturą VAT)
### - "KONSEKWENCJE PRAWNE" (jeśli wykryto krytyczne naruszenia)
"""


class AuditService:
    """Encapsulates Gemini-based invoice audit logic."""

    @staticmethod
    async def audit_single_invoice(invoice: Invoice, db: AsyncSession) -> Invoice:
        """
        Run Gemini audit on a single invoice.
        Saves results to invoice.ai_analysis_json, invoice.risk_score, invoice.status
        and creates AuditLog entries.
        """
        if not client:
            raise RuntimeError("GOOGLE_API_KEY not configured — cannot run audit.")

        logger.info(f"🚀 Starting audit for invoice {invoice.id}")

        # Mark as processing
        invoice.status = "processing"
        await db.commit()

        # Prepare invoice data for prompt
        invoice_data = {
            "id": str(invoice.id),
            "internal_number": invoice.internal_number,
            "vendor": invoice.contractor_name,
            "vendor_nip": invoice.contractor_nip,
            "net_amount": float(invoice.net_amount) if invoice.net_amount else 0.0,
            "vat_amount": float(invoice.vat_amount) if invoice.vat_amount else 0.0,
            "gross_amount": float(invoice.gross_amount) if invoice.gross_amount else 0.0,
            "currency": invoice.currency,
            "issue_date": invoice.issue_date.isoformat() if invoice.issue_date else None,
            "doc_type": "Faktura VAT",
        }

        # Merge any existing parsed data (items, etc.)
        if invoice.parsed_data and isinstance(invoice.parsed_data, dict):
            invoice_data.update(invoice.parsed_data)
        if invoice.ai_analysis_json and isinstance(invoice.ai_analysis_json, dict):
            # Preserve previously stored items/metadata
            for k, v in invoice.ai_analysis_json.items():
                if k not in invoice_data:
                    invoice_data[k] = v

        # Fetch departments for this company
        dept_stmt = select(Department).where(Department.company_id == invoice.company_id)
        dept_result = await db.execute(dept_stmt)
        departments = dept_result.scalars().all()
        departments_list = [{"name": d.name, "description": d.description or ""} for d in departments]

        # Fetch Corporate Memory for the Invoice Auditor string context
        corporate_memory = ""
        if invoice.company_id:
            from app.services.cfo_agent import fetch_corporate_memory
            corporate_memory = await fetch_corporate_memory(str(invoice.company_id))

        # Build prompt
        current_date = datetime.utcnow().strftime("%d %B %Y")
        prompt = AUDIT_PROMPT_TEMPLATE.format(
            invoice_json=json.dumps(invoice_data, ensure_ascii=False, indent=2),
            corporate_memory=corporate_memory,
            current_date=current_date,
            departments_json=json.dumps(departments_list, ensure_ascii=False, indent=2) if departments_list else "Brak zdefiniowanych departamentów — zaproponuj nowy.",
        )

        try:
            # Log start
            log_start = AuditLog(
                invoice_id=invoice.id,
                actor="Gemini_2.5_Flash",
                action="audit_started",
                details="Sent prompt to Gemini for risk analysis.",
            )
            db.add(log_start)
            await db.commit()

            # Call Gemini
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    response_mime_type="application/json",
                ),
            )

            raw_text = response.text
            # Strip markdown fences if present
            if "```json" in raw_text:
                raw_text = raw_text.split("```json")[1].split("```")[0]
            elif "```" in raw_text:
                raw_text = raw_text.split("```")[1].split("```")[0]

            audit_result_json = json.loads(raw_text.strip())
            logger.info(f"✅ AI Analysis complete for invoice {invoice.id}")

            # Extract risk_score
            risk_score = 0
            if "audit_result" in audit_result_json:
                risk_score = int(float(audit_result_json["audit_result"].get("risk_score", 0)))
            elif "risk_score" in audit_result_json:
                risk_score = int(float(audit_result_json.get("risk_score", 0)))

            # Save results
            invoice.ai_analysis_json = audit_result_json
            invoice.risk_score = risk_score
            invoice.status = "audited"

            # Process department assignment from AI
            dept_assignment = audit_result_json.get("department_assignment", {})
            if dept_assignment and not dept_assignment.get("is_new_department", True):
                # AI assigned to existing department — find and link
                assigned_name = dept_assignment.get("department_name", "")
                matching_dept = next(
                    (d for d in departments if d.name.lower() == assigned_name.lower()),
                    None
                )
                if matching_dept:
                    invoice.department_id = matching_dept.id
                    logger.info(f"📁 Auto-assigned invoice {invoice.id} to department '{assigned_name}'")
            elif dept_assignment and dept_assignment.get("is_new_department"):
                # AI suggests new department — keep in ai_analysis_json for frontend notification
                logger.info(f"💡 AI suggests new department '{dept_assignment.get('department_name')}' for invoice {invoice.id}")

            # Log completion
            status_label = audit_result_json.get("audit_result", {}).get("status", "UNKNOWN")
            log_end = AuditLog(
                invoice_id=invoice.id,
                actor="Gemini_2.5_Flash",
                action="audit_completed",
                details=f"Risk Score: {risk_score}/100. Status: {status_label}",
            )
            db.add(log_end)
            await db.commit()
            await db.refresh(invoice)

            logger.info(f"💾 Saved audit results for invoice {invoice.id}")
            return invoice

        except Exception as e:
            logger.error(f"❌ AI Error for invoice {invoice.id}: {e}")
            log_fail = AuditLog(
                invoice_id=invoice.id,
                actor="Gemini_2.0_Flash",
                action="audit_failed",
                details=f"Error: {str(e)}",
            )
            db.add(log_fail)
            invoice.status = "error"
            await db.commit()
            await db.refresh(invoice)
            raise

    @staticmethod
    async def audit_pending_invoices(company_id: UUID, db: AsyncSession) -> list[Invoice]:
        """
        Find all invoices for the company that haven't been audited yet
        (risk_score IS NULL or status in ['new', 'pending']) and run audit on each.
        Uses asyncio.Semaphore to limit concurrency.
        """
        stmt = (
            select(Invoice)
            .where(
                Invoice.company_id == company_id,
                or_(
                    Invoice.risk_score == 0,
                    Invoice.status.in_(["new", "pending"]),
                ),
                # Don't re-audit already audited or processing invoices
                Invoice.status.notin_(["audited", "processing", "error"]),
            )
        )
        result = await db.execute(stmt)
        pending_invoices = result.scalars().all()

        if not pending_invoices:
            return []

        async def _audit_with_limit(inv: Invoice) -> Invoice:
            async with _SEMAPHORE:
                return await AuditService.audit_single_invoice(inv, db)

        # Process concurrently with semaphore
        tasks = [_audit_with_limit(inv) for inv in pending_invoices]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Filter out exceptions, return successful audits
        audited = []
        for r in results:
            if isinstance(r, Invoice):
                audited.append(r)
            elif isinstance(r, Exception):
                logger.error(f"Batch audit error: {r}")

        return audited

    @staticmethod
    async def run_background_audit(invoice_id: UUID) -> None:
        """
        Background task entry point: creates its own DB session and runs audit.
        Used by POST /invoices to fire-and-forget the audit after returning the response.
        """
        from app.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            try:
                result = await db.execute(
                    select(Invoice).where(Invoice.id == invoice_id)
                )
                invoice = result.scalar_one_or_none()
                if not invoice:
                    logger.error(f"Background audit: Invoice {invoice_id} not found.")
                    return

                await AuditService.audit_single_invoice(invoice, db)
                logger.info(f"✅ Background audit completed for invoice {invoice_id}")
            except Exception as e:
                logger.error(f"❌ Background audit failed for invoice {invoice_id}: {e}")

